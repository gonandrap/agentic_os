"""Neo as a panel — a roster of profiled seats, one blind round, one verdict out.

Today Neo is ONE headless call: `neo.answer_question` builds one system prompt and asks
one model for one strict-JSON verdict. A single prompt has exactly one FRONT, and the
only failure the record ever showed was a concern that was not first getting outcompeted
by one that was (`gates.REVIEWER_PERSONA`'s premise check had to be moved to the top
before Neo stopped denying commands that performed no privileged action at all). Four
concerns cannot each be first in one prompt. See
docs/superpowers/specs/2026-08-02-neo-team-design.md.

This module is that primitive. It ships with two of its five seats — `premise`, which
asks whether this was even the question that was asked, and `chair`, which turns the
seats' opinions into the one verdict `neo.parse_verdict` already accepts. `record`,
`blast` and `taste` drop in as DATA: a markdown file in `assets/neo-seats/` and a name in
the catalog roster, with no change here.

THREE THINGS THAT LOOK LIKE DETAILS AND ARE NOT:

**The panel ships DISABLED, and at that default the OS must behave byte-identically to
today** — same number of Claude calls, same system prompt bytes, same message to the
worker. Nothing in this module runs unless a catalog turns it on.

**`neo` MUST NEVER IMPORT THIS MODULE.** The design says "`neo.answer_question` becomes a
caller of it" and "if the Premise Sceptic fails, fall back to today's single-agent path",
which taken literally is an import cycle in a codebase that has none. The seam is instead
a `neo.drain_queue(answer=…)` keyword the daemon injects: this module imports `neo`, `neo`
knows nothing about panels, and the fallback is simply not passing `answer=`.

**ON `route=fast` THE PREMISE SEAT'S OWN REPLY IS THE VERDICT AND THE CHAIR IS SKIPPED.**
The design's fast path claimed "one call total" for the ~95% of gate reviews that are
classifier false positives, but a premise call plus a chair call is TWO — a 2x cost and
latency regression on Neo's highest-volume channel. So the premise seat emits the full
verdict shape plus a `route`, and on the fast route that reply is what the worker reads.
Two safety rules live in CODE rather than in a prompt, because a prompt is a preference a
model can talk itself out of: see `fast_is_permitted`.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import claude_cli, structured
from .bootstrap import ASSETS
from .neo_store import SEATS, NeoStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .catalog import NeoConfig

log = logging.getLogger("neo.panel")

#: Where the seat definitions live. DELIBERATELY NOT `assets/agents/`: `bootstrap._rebuild`
#: copytrees that directory wholesale into every feature-order planner's `.claude/agents/`,
#: so a Neo seat dropped there — subdirectory or not — becomes a bogus subagent every
#: planner session can invoke.
SEAT_ASSETS = ASSETS / "neo-seats"

#: First line of every seat's system prompt. Machine-readable on purpose: it identifies
#: the seat to anything reading the call (the test fake keys on it) without depending on
#: the mandate's prose, and it is constant per seat, so it does not disturb the per-seat
#: cached prompt prefix.
SEAT_HEADER = "# Neo panel seat: {seat}"

#: Routes. `fast` = the premise seat answered and the chair was skipped; `panel` = the
#: seats deliberated and the chair synthesised; `single` = the panel stepped aside and
#: today's single-agent path answered (see `decide`).
ROUTES = ("fast", "panel", "single")


class SeatError(RuntimeError):
    """A seat could not be run at all — no definition ships for it in this build."""


# -- seat definitions -------------------------------------------------------------------


def seat_path(seat: str) -> Path:
    return SEAT_ASSETS / f"{seat}.md"


def shipped_seats() -> tuple[str, ...]:
    """The seats whose definition ships in this build, in `SEATS` order."""
    return tuple(s for s in SEATS if seat_path(s).is_file())


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
def definition(seat: str) -> tuple[dict[str, str], str]:
    """The shipped (frontmatter, mandate) for one seat. Cached: it is a file on disk that
    only changes when the build does."""
    path = seat_path(seat)
    if not path.is_file():
        raise SeatError(f"no definition ships for the {seat!r} seat ({path})")
    meta, body = parse_definition(path.read_text())
    if meta.get("name") != seat:
        raise SeatError(f"{path} declares name={meta.get('name')!r}, not {seat!r}")
    return meta, body


def seat_model(seat: str, cfg: NeoConfig) -> str:
    """Which model this seat runs on.

    Most specific wins: the catalog's per-seat map, then `chair_model` for the chair,
    then the definition's own `model:` key, then Neo's model. The chair should keep the
    expensive model when the seats do not — it is the one writing what a human reads.
    """
    panel = cfg.panel
    explicit = panel.seat_models.get(seat) or (panel.chair_model if seat == "chair" else "")
    try:
        declared = definition(seat)[0].get("model", "")
    except SeatError:
        declared = ""
    return explicit or declared or cfg.model


def build_seat_system_prompt(store: NeoStore, project: str, seat: str,
                             learnings_limit: int = 50) -> str:
    """One seat's mandate + the learnings scoped to it. Byte-stable per seat.

    Mirrors `neo.build_system_prompt`, and for the same reason: the prefix has to be
    identical call to call or every seat pays a full prompt-cache miss. Running seats in
    parallel does not disturb that — the prefix is per SEAT, not per queue position.

    `store.learnings(project, seat=seat)` returns the global learnings PLUS this seat's.
    The default (no seat) returns only the global ones, which is what keeps a seat-scoped
    correction out of the single-agent path's cached prefix.
    """
    _, mandate = definition(seat)
    parts = [SEAT_HEADER.format(seat=seat), "", mandate, "",
             "# Learnings (from the user's reviews of your past answers)"]
    rows = store.learnings(project, limit=learnings_limit, seat=seat)
    if not rows:
        parts.append("(none yet — escalate when unsure)")
    for r in rows:
        scope = r["project"] or "global"
        parts.append(f"- [{scope}] {r['content']}")
    return "\n".join(parts)


# -- one seat's contribution ------------------------------------------------------------


@dataclass
class Opinion:
    """One seat's contribution to one decision, as stored in `panel_opinions`."""

    seat: str
    raw: str = ""
    verdict: str = ""
    route: str = ""
    status: str = "ok"          # ok | abstained | failed
    model: str = ""
    latency_ms: int = 0
    #: Did a model actually reply? False when the call never happened at all — it errored,
    #: timed out, or no definition for the seat ships in this build. NOT the same as
    #: `status == "ok"`: a seat that replied with something unusable still replied, and
    #: unusable output is a thing the panel can route on (toward `panel`) while silence is
    #: not. Derived, in-memory, and deliberately not a column: the store records what the
    #: seat said, and this is about whether it said anything.
    replied: bool = True

    @property
    def data(self) -> dict[str, Any] | None:
        """The seat's reply as an object, or None if it did not emit one."""
        if self.status != "ok":
            return None
        return structured.parse_json_object(self.raw)

    def summary(self) -> dict[str, Any]:
        """What travels back to the caller in the verdict's additive `panel` key.

        The seat's RAW reply is deliberately not in here. Panel deliberation is stored and
        inspectable on demand; it is never pushed to a worker or into the inbox, and the
        surest way to keep it that way is for it never to leave this module.
        """
        return {"seat": self.seat, "status": self.status, "verdict": self.verdict,
                "route": self.route, "model": self.model, "latency_ms": self.latency_ms}


def _run_seat(seat: str, prompt: str, system: str, model: str, timeout: int,
              cwd: Path) -> Opinion:
    """Call one seat. Never raises: a seat that fails abstains and the panel proceeds.

    Runs on a pool thread, so it touches NO database — sqlite connections belong to the
    thread that opened them. Opinions are recorded by the caller.
    """
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        raw = claude_cli.run_headless(prompt, system_prompt=system, model=model,
                                      timeout=timeout, cwd=cwd)
    except claude_cli.ClaudeCliError as e:
        log.warning("panel seat %s abstained: %s", seat, e)
        return Opinion(seat=seat, raw=str(e), status="abstained", model=model,
                       latency_ms=elapsed(), replied=False)
    data = structured.parse_json_object(raw)
    if not isinstance(data, dict):
        return Opinion(seat=seat, raw=raw, status="failed", model=model,
                       latency_ms=elapsed())
    return Opinion(seat=seat, raw=raw, model=model, latency_ms=elapsed(),
                   verdict=str(data.get("verdict") or "").strip(),
                   route=str(data.get("route") or "").strip().lower())


def _round(store: NeoStore, q: dict[str, Any], cfg: NeoConfig,
           seats: list[str]) -> list[Opinion]:
    """Run every seat blind, concurrently, and record what each said.

    Concurrent INSIDE the daemon's single Neo thread: the queue still drains FIFO, one
    question at a time, so the prompt-cache economics of the drain are untouched. The
    per-seat timeout is `PanelConfig.timeout` and is bounded well below Neo's 300s call
    timeout on purpose — the whole FIFO drain, and every worker parked behind it, waits
    on the slowest seat.
    """
    from .paths import ensure_home

    # Every prompt is built HERE, on the calling thread, before anything fans out: the
    # store's connection is thread-local, and building them up front also makes what each
    # seat was asked deterministic.
    home = ensure_home()
    prompt = _question_prompt(q)
    systems: dict[str, str] = {}
    missing: list[Opinion] = []
    for seat in seats:
        try:
            systems[seat] = build_seat_system_prompt(store, q["project"], seat,
                                                     cfg.learnings_limit)
        except SeatError as e:
            # Not an outage: this build has no such seat. The panel proceeds without it
            # rather than stalling the drain, but it says so loudly and the row records
            # `failed` rather than `abstained` — a seat that CANNOT run is not one that
            # timed out.
            log.error("panel seat %s cannot run: %s", seat, e)
            missing.append(Opinion(seat=seat, raw=str(e), status="failed",
                                   replied=False))

    opinions: list[Opinion] = []
    if systems:
        with ThreadPoolExecutor(max_workers=len(systems),
                                thread_name_prefix="neo-seat") as pool:
            futures = {
                seat: pool.submit(_run_seat, seat, prompt, system,
                                  seat_model(seat, cfg), cfg.panel.timeout, home)
                for seat, system in systems.items()
            }
            opinions = [futures[seat].result() for seat in seats if seat in futures]
    opinions += missing
    for op in opinions:
        _record(store, q, op)
    return opinions


def _record(store: NeoStore, q: dict[str, Any], op: Opinion) -> None:
    store.record_opinion(q["id"], op.seat, reply=op.raw, verdict=op.verdict,
                         route=op.route, status=op.status, model=op.model,
                         latency_ms=op.latency_ms)


def _question_prompt(q: dict[str, Any]) -> str:
    from . import neo

    return neo.build_question_prompt(q)


# -- routing ------------------------------------------------------------------------------


def fast_is_permitted(kind: str, proposed: str) -> bool:
    """May this decision take the fast route, given what the premise seat proposed?

    THIS IS THE SAFETY RULE, AND IT IS IN CODE BECAUSE A PROMPT IS A PREFERENCE. The fast
    route skips the seat that owns the blast radius, so it is available only where being
    wrong is cheap:

    * a proposed `dismiss` — the command performed no privileged action, so there is
      nothing to authorise and nothing to get wrong except the classifier; or
    * an open question, which authorises nothing by construction.

    And a `approval` never reaches `approved` this way, however the premise seat routed
    it: a real privileged action always costs the full panel. That clause is redundant
    with the one below it today and is written out anyway, because it is the rule, and a
    rule that survives only as a consequence of another rule stops holding the moment
    that other rule is relaxed.
    """
    if kind == "approval" and proposed == "approved":
        return False
    return proposed == "dismissed" or kind == "question"


def _premise_route(op: Opinion, kind: str, cfg: NeoConfig) -> tuple[str, dict | None]:
    """(route, the premise seat's reply) — `panel` whenever anything is off.

    A reply that will not parse, that is not a verdict at all, or that carries no `route`
    key is treated as `panel`: fail toward the expensive-but-safe side, never toward the
    route that skips four seats.
    """
    data = op.data
    if not isinstance(data, dict) or "escalate" not in data:
        return "panel", None
    if not cfg.panel.fast_path or op.route != "fast":
        return "panel", data
    proposed = _proposed_verdict(op, data)
    if not fast_is_permitted(kind, proposed):
        log.info("panel: premise asked for the fast route on a %s proposing %r; "
                 "running the full panel", kind, proposed or "nothing")
        return "panel", data
    return "fast", data


def _proposed_verdict(op: Opinion, data: dict[str, Any]) -> str:
    """The premise seat's proposal, normalised to the store's vocabulary (`dismissed`,
    `approved`, `denied`) — or `""` when it proposed none.

    Normalised through `neo.parse_verdict` rather than by a second alias table here, so
    the panel and the single agent can never disagree about what "dismiss" means. The
    empty case is kept distinct: `parse_verdict` reads a missing verdict as `denied`,
    which is the right default for a gate but would read as a proposal the seat did not
    make.
    """
    from . import neo

    if not str(data.get("verdict") or "").strip():
        return ""
    return neo.parse_verdict(op.raw)["verdict"]


# -- the entry point ------------------------------------------------------------------------


def decide(store: NeoStore, q: dict[str, Any], cfg: NeoConfig) -> dict[str, Any]:
    """Answer one claimed question with the panel. Injected via `neo.drain_queue(answer=…)`.

    Returns EXACTLY what `neo.parse_verdict` returns — `escalate`, `answer`, `reason`,
    `verdict`, `approve`, `dispatch` — plus one additive `panel` key carrying the route
    and a per-seat summary. Additive is safe because every consumer (the daemon's
    `deliver`, the gate path, the plan path) reads by key and never by shape.

    Raises `claude_cli.ClaudeCliError` on total failure, so `drain_queue`'s existing
    rescue — mark the question failed, tell the worker — still applies unchanged.
    """
    from . import neo

    kind = q.get("kind") or "question"
    seats = [s for s in cfg.panel.roster if s != "chair"]
    opinions = _round(store, q, cfg, seats)
    premise = next((op for op in opinions if op.seat == "premise"), None)

    if premise is None or not premise.replied:
        # The seat that routes never spoke, so there is nothing to route on. Fall back to
        # today's single-agent path: a Neo outage must never become a fleet stall, and the
        # single agent is a decision the OS already trusts.
        #
        # SILENCE, NOT UNUSABLE OUTPUT. A premise seat that replied with something that
        # will not parse HAS routed — toward `panel`, the expensive-but-safe side — and
        # `_premise_route` sends it there. Only a call that never produced a reply at all
        # falls back here.
        log.warning("panel: no usable premise opinion for question %s; "
                    "falling back to the single agent", q["id"])
        verdict = neo.answer_question(store, q, cfg.model, cfg.learnings_limit)
        return {**verdict, "panel": _summary("single", opinions)}

    route, _ = _premise_route(premise, kind, cfg)
    if route == "fast":
        # The premise seat's own reply IS the verdict; the chair is skipped. This is what
        # keeps the panel from being a 2x regression on the OS's highest-volume channel.
        return {**neo.parse_verdict(premise.raw), "panel": _summary("fast", opinions)}

    chair = _run_chair(store, q, cfg, opinions)
    opinions = [*opinions, chair]
    return {**neo.parse_verdict(chair.raw), "panel": _summary("panel", opinions)}


def _summary(route: str, opinions: list[Opinion]) -> dict[str, Any]:
    return {"route": route, "seats": [op.summary() for op in opinions]}


def _run_chair(store: NeoStore, q: dict[str, Any], cfg: NeoConfig,
               opinions: list[Opinion]) -> Opinion:
    """Synthesise. The chair is the one seat that is not blind — that is its whole job.

    A chair that cannot be reached is total failure, not a seat abstaining: there is no
    decision without it. The abstention is recorded first so the deliberation survives
    the exception, then `ClaudeCliError` propagates to `drain_queue`.
    """
    from .paths import ensure_home

    model = seat_model("chair", cfg)
    system = build_seat_system_prompt(store, q["project"], "chair", cfg.learnings_limit)
    prompt = build_chair_prompt(q, opinions)
    started = time.monotonic()
    try:
        raw = claude_cli.run_headless(prompt, system_prompt=system, model=model,
                                      timeout=cfg.panel.timeout, cwd=ensure_home())
    except claude_cli.ClaudeCliError as e:
        op = Opinion(seat="chair", raw=str(e), status="abstained", model=model,
                     latency_ms=int((time.monotonic() - started) * 1000))
        _record(store, q, op)
        raise
    data = structured.parse_json_object(raw) or {}
    op = Opinion(seat="chair", raw=raw, model=model,
                 latency_ms=int((time.monotonic() - started) * 1000),
                 verdict=str(data.get("verdict") or "").strip())
    _record(store, q, op)
    return op


def build_chair_prompt(q: dict[str, Any], opinions: list[Opinion]) -> str:
    """The question, then every seat's reply verbatim.

    Verbatim rather than summarised: a summariser between the seats and the chair is one
    more place for the compliance wording a learning mandates to be silently dropped.
    """
    parts = [_question_prompt(q), "", "# The panel's opinions",
             "Each seat answered blind — none of them saw another's reply, and none of "
             "them saw yours."]
    for op in opinions:
        if op.status == "ok":
            parts += [f"\n## Seat: {op.seat}", op.raw.strip()]
        else:
            parts.append(f"\n## Seat: {op.seat}\n(no opinion — the seat {op.status})")
    return "\n".join(parts)
