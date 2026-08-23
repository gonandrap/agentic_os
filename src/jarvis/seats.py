"""A roster of profiled seats, run blind and in parallel — the primitive, and nothing else.

This is the reusable half of `panel.py`, extracted rather than copied. Neo's panel and the
validation panel are two rosters of the same machine: markdown seat definitions on disk, a
system prompt per seat, one concurrent round in which no seat can read another's reply, and
one `Opinion` per seat carrying what it said, what that cost, and whether it said anything
at all.

**A NEAR-LEAF ON PURPOSE.** It imports `claude_cli` and `structured` and NOTHING else — no
store, no catalog, no bootstrap, no `neo`. `panel.py` imports `bootstrap.ASSETS` for its
seat directory; carrying that here would drag an adapter under a leaf, so the asset
directory is a field of `Roster` and the caller supplies it.

THREE THINGS THAT LOOK LIKE DETAILS AND ARE NOT:

**THE CACHE IS KEYED ON THE ROSTER, NOT ON THE SEAT NAME.** `chair.md` exists in
`assets/neo-seats/` AND in `assets/validator-seats/`, and a name-only key means whichever
one is read first answers for both — a Neo chair silently mandating a validation outcome,
or the reverse, with every other test in the suite green. `definition` therefore takes the
roster as its first argument, and `tests/test_seats.py` proves the two do not collide by
loading the Neo chair, the validator chair, and THEN THE NEO CHAIR AGAIN.

**`run_blind` TAKES PROMPTS ALREADY BUILT, AND THAT SIGNATURE IS THE THREAD-LOCALITY RULE
EXPRESSED AS A TYPE.** A sqlite connection belongs to the thread that opened it, so a seat
running on a pool thread must not query one. There is no store in this module's signatures
at all, so a caller physically cannot query one from a pool thread: it builds every prompt
first, on its own thread, and hands over strings.

**`Opinion.replied` IS NOT `status == "ok"`.** A seat that answered with something that will
not parse HAS answered — the caller can route on unusable output — while a seat whose call
never happened has said nothing at all. Losing that distinction is the first thing a copy of
this module would lose, and Neo's fallback path turns on it (`panel.decide`).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import claude_cli, structured

log = logging.getLogger("jarvis.seats")


class SeatError(RuntimeError):
    """A seat could not be run at all — no definition ships for it in this build."""


@dataclass(frozen=True)
class Roster:
    """One panel's seats: where their markdown lives, what names are legal, and the
    machine-readable first line of every one of their system prompts.

    Frozen because it is a cache key (see `definition`). Two rosters that name the same
    directory, vocabulary and header ARE the same roster and share cache entries; two that
    differ in any of the three share none.
    """

    #: The directory the seat markdown ships in, e.g. `bootstrap.ASSETS / "neo-seats"`.
    assets: Path
    #: The legal seat names, in the order a shipped-set answer should list them.
    vocabulary: tuple[str, ...]
    #: A `{seat}` template, e.g. `"# Jarvis validation seat: {seat}"`. It is the first line
    #: of every seat system prompt: identifying a call by its system prompt rather than by
    #: its mandate's prose is what lets a test fake — and a reader of the record — tell one
    #: roster's `chair` from another's.
    header: str

    def path(self, seat: str) -> Path:
        return self.assets / f"{seat}.md"

    def header_line(self, seat: str) -> str:
        return self.header.format(seat=seat)


# -- seat definitions -------------------------------------------------------------------


def parse_definition(text: str) -> tuple[dict[str, str], str]:
    """Split a seat definition into (frontmatter, mandate).

    Same authoring format as `assets/agents/*.md` — a `---` fenced block of `key: value`
    lines, then the mandate as the body — parsed by a two-line splitter rather than by a
    YAML library. The core of `jarvis` is stdlib-only and this is not the feature that
    gets to add a dependency.

    A `tools:` key is meaningful there and meaningless here: a seat is a headless
    `claude -p` call, not a subagent, so it has no tool set to allow-list.
    """
    if not text.startswith("---\n"):
        raise SeatError("a seat definition must open with `---` frontmatter")
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise SeatError("a seat definition's frontmatter is never closed")
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, parts[2].strip()


@lru_cache(maxsize=None)
def definition(roster: Roster, seat: str) -> tuple[dict[str, str], str]:
    """The shipped (frontmatter, mandate) for one seat of one roster. Cached: it is a file
    on disk that only changes when the build does.

    **KEYED ON THE ROSTER AS WELL AS THE SEAT**, and that is the whole reason this
    signature is not `definition(seat)`. `chair` is a legal seat name in more than one
    roster, so a name-only key would let the first `chair.md` read in a process answer for
    every other one — silently, and in the direction that hands one panel another panel's
    mandate.
    """
    path = roster.path(seat)
    if not path.is_file():
        raise SeatError(f"no definition ships for the {seat!r} seat ({path})")
    meta, body = parse_definition(path.read_text())
    if meta.get("name") != seat:
        raise SeatError(f"{path} declares name={meta.get('name')!r}, not {seat!r}")
    return meta, body


def shipped(roster: Roster) -> tuple[str, ...]:
    """The seats whose definition ships in this build, in the roster's own order."""
    return tuple(s for s in roster.vocabulary if roster.path(s).is_file())


# -- one seat's contribution ------------------------------------------------------------


@dataclass
class Opinion:
    """One seat's contribution to one decision, as stored in an opinions table."""

    seat: str
    raw: str = ""
    verdict: str = ""
    #: A routing hint, for rosters whose schema has one (Neo's premise seat). Empty
    #: everywhere else, and read by nothing here: this module parses it and stops.
    route: str = ""
    status: str = "ok"          # ok | abstained | failed
    model: str = ""
    latency_ms: int = 0
    #: Did a model actually reply? False when the call never happened at all — it errored,
    #: timed out, or no definition for the seat ships in this build. NOT the same as
    #: `status == "ok"`: a seat that replied with something unusable still replied, and
    #: unusable output is a thing a caller can route on while silence is not. Derived,
    #: in-memory, and deliberately not a column: the store records what the seat said, and
    #: this is about whether it said anything.
    replied: bool = True
    #: What this seat's call cost (`claude_cli.derive_turn_usage` envelope), carried here
    #: rather than written where it is produced: `_run_seat` runs on a pool thread and
    #: touches no database. The caller persists it on its own thread with the rest.
    usage: dict[str, Any] | None = None

    @property
    def data(self) -> dict[str, Any] | None:
        """The seat's reply as an object, or None if it did not emit one."""
        if self.status != "ok":
            return None
        return structured.parse_json_object(self.raw)

    def summary(self) -> dict[str, Any]:
        """What travels back to a caller that reports per-seat outcomes.

        The seat's RAW reply is deliberately not in here. Deliberation is stored and
        inspectable on demand; it is never pushed to a worker or into the inbox, and the
        surest way to keep it that way is for it never to leave by this door. A caller
        that must persist the reply (the validation round machine does) reads `raw`.
        """
        return {"seat": self.seat, "status": self.status, "verdict": self.verdict,
                "route": self.route, "model": self.model, "latency_ms": self.latency_ms}


def _run_seat(seat: str, prompt: str, system: str, model: str, timeout: int,
              cwd: Path, tools: str | None = None) -> Opinion:
    """Call one seat. Never raises: a seat that fails abstains and the panel proceeds.

    Runs on a pool thread, so it touches NO database — sqlite connections belong to the
    thread that opened them. Opinions are recorded by the caller.
    """
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        # `attribute=False`: the caller records this seat itself, by name. See `neo`.
        result = claude_cli.run_headless_result(prompt, system_prompt=system, model=model,
                                                timeout=timeout, cwd=cwd, tools=tools,
                                                attribute=False)
    except claude_cli.ClaudeCliError as e:
        log.warning("seat %s abstained: %s", seat, e)
        return Opinion(seat=seat, raw=str(e), status="abstained", model=model,
                       latency_ms=elapsed(), replied=False)
    raw, usage = result.text, result.usage
    data = structured.parse_json_object(raw)
    if not isinstance(data, dict):
        return Opinion(seat=seat, raw=raw, status="failed", model=model,
                       latency_ms=elapsed(), usage=usage)
    return Opinion(seat=seat, raw=raw, model=model, latency_ms=elapsed(), usage=usage,
                   verdict=str(data.get("verdict") or "").strip(),
                   route=str(data.get("route") or "").strip().lower())


def run_blind(prompts: dict[str, tuple[str, str]], *, models: dict[str, str],
              timeout: int, cwd: Path, tools: str | None = None) -> list[Opinion]:
    """Run every seat concurrently and blind, and return one Opinion per seat, in the
    order the prompts were given.

    `prompts` is `{seat: (system_prompt, user_prompt)}` **already built**. That is the
    whole signature argument: there is no store here, so a caller cannot query one from a
    pool thread, and what each seat was asked is fixed before anything fans out.

    Blind is not a wish: every seat is submitted before any result is read, so none of
    them can see another's reply. Being second in wall-clock order is not being second in
    knowledge, and agreement is only evidence if no seat could read another's answer.

    `tools` rides through to `claude_cli.run_headless_result` unchanged: `None` leaves the
    callee's tool set alone (Neo's panel, whose behaviour must not change) and `""` strips
    every tool (the validation seats, which judge the packet and only the packet).
    """
    if not prompts:
        return []
    with ThreadPoolExecutor(max_workers=len(prompts),
                            thread_name_prefix="seat") as pool:
        futures = {
            seat: pool.submit(_run_seat, seat, user, system, models.get(seat, ""),
                              timeout, cwd, tools)
            for seat, (system, user) in prompts.items()
        }
        return [futures[seat].result() for seat in prompts]
