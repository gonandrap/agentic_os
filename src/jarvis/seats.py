"""A roster of profiled seats, run blind and in parallel — the primitive, and nothing else"""

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
    """A seat could not be run at all — no definition ships for it in this build"""


@dataclass(frozen=True)
class Roster:
    """One panel's seats: where their markdown lives"""

    #: The directory the seat markdown ships in, e.g. `bootstrap.ASSETS / "neo-seats"`
    assets: Path
    #: The legal seat names, in the order a shipped-set answer should list them
    vocabulary: tuple[str, ...]
    #: A `{seat}` template, e.g. `"# Jarvis validation seat: {seat}"`
    header: str

    def path(self, seat: str) -> Path:
        return self.assets / f"{seat}.md"

    def header_line(self, seat: str) -> str:
        return self.header.format(seat=seat)


# -- seat definitions


def parse_definition(text: str) -> tuple[dict[str, str], str]:
    """Split a seat definition into (frontmatter, mandate)"""
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
    """The shipped (frontmatter, mandate) for one seat of one roster"""
    path = roster.path(seat)
    if not path.is_file():
        raise SeatError(f"no definition ships for the {seat!r} seat ({path})")
    meta, body = parse_definition(path.read_text())
    if meta.get("name") != seat:
        raise SeatError(f"{path} declares name={meta.get('name')!r}, not {seat!r}")
    return meta, body


def shipped(roster: Roster) -> tuple[str, ...]:
    """The seats whose definition ships in this build, in the roster's own order"""
    return tuple(s for s in roster.vocabulary if roster.path(s).is_file())


# -- one seat's contribution


@dataclass
class Opinion:
    """One seat's contribution to one decision, as stored in an opinions table"""

    seat: str
    raw: str = ""
    verdict: str = ""
    #: A routing hint, for rosters whose schema has one (Neo's premise seat)
    route: str = ""
    status: str = "ok"          # ok | abstained | failed
    model: str = ""
    latency_ms: int = 0
    #: Did a model actually reply?
    replied: bool = True
    #: What this seat's call cost (`claude_cli.derive_turn_usage` envelope)
    usage: dict[str, Any] | None = None
    #: The account's window was spent and this seat was never asked (#235)
    refused: claude_cli.UsageLimit | None = None
    #: CLAUDE CODE COULD NOT AUTHENTICATE and this seat was never asked (#778)
    auth: claude_cli.AuthFailure | None = None
    #: THE SEAT'S PROCESS WAS KILLED and it was never asked (#1039)
    killed: claude_cli.Killed | None = None
    #: NO DEFINITION FOR THIS SEAT SHIPS IN THIS BUILD
    unavailable: bool = False

    @property
    def data(self) -> dict[str, Any] | None:
        """The seat's reply as an object, or None if it did not emit one"""
        if self.status != "ok":
            return None
        return structured.parse_json_object(self.raw)

    def summary(self) -> dict[str, Any]:
        """What travels back to a caller that reports per-seat outcomes"""
        return {"seat": self.seat, "status": self.status, "verdict": self.verdict,
                "route": self.route, "model": self.model, "latency_ms": self.latency_ms}


def _run_seat(seat: str, prompt: str, system: str, model: str, timeout: int,
              cwd: Path, tools: str | None = None, *,
              kind: str | claude_cli.Authorisation) -> Opinion:
    """Call one seat"""
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        # The caller records this seat itself, by name, under `kind`
        result = claude_cli.run_headless_result(prompt, system_prompt=system, model=model,
                                                timeout=timeout, cwd=cwd, tools=tools,
                                                records_itself=kind)
    except claude_cli.ClaudeCliError as e:
        log.warning("seat %s abstained: %s", seat, e)
        return Opinion(seat=seat, raw=str(e), status="abstained", model=model,
                       latency_ms=elapsed(), replied=False,
                       refused=getattr(e, "limit", None),
                       auth=getattr(e, "auth", None),
                       killed=getattr(e, "killed", None))
    raw, usage = result.text, result.usage
    data = structured.parse_json_object(raw)
    if not isinstance(data, dict):
        return Opinion(seat=seat, raw=raw, status="failed", model=model,
                       latency_ms=elapsed(), usage=usage)
    return Opinion(seat=seat, raw=raw, model=model, latency_ms=elapsed(), usage=usage,
                   verdict=str(data.get("verdict") or "").strip(),
                   route=str(data.get("route") or "").strip().lower())


def prime_cache(system: str, user: str, model: str, *, timeout: int, cwd: Path,
                tools: str | None = None, kind: str) -> dict[str, Any] | None:
    """Write a shared system prefix into the prompt cache, and WAIT for it"""
    # On the CALLING thread and before the call: this module only forwards `kind`
    authorised = claude_cli.authorise(kind)
    try:
        result = claude_cli.run_headless_result(user, system_prompt=system, model=model,
                                                timeout=timeout, cwd=cwd, tools=tools,
                                                # The caller records this call, `kind`
                                                records_itself=authorised)
    except claude_cli.ClaudeCliError as e:
        log.warning("prompt-cache priming failed, seats will each write: %s", e)
        return None
    return result.usage


def run_blind(prompts: dict[str, tuple[str, str]], *, models: dict[str, str],
              timeout: int, cwd: Path, tools: str | None = None,
              kind: str) -> list[Opinion]:
    """Run every seat concurrently and blind"""
    if not prompts:
        return []
    # BEFORE THE FAN-OUT, on this thread: a pool thread's only jarvis frame is this module
    authorised = claude_cli.authorise(kind)
    with ThreadPoolExecutor(max_workers=len(prompts),
                            thread_name_prefix="seat") as pool:
        futures = {
            seat: pool.submit(_run_seat, seat, user, system, models.get(seat, ""),
                              timeout, cwd, tools, kind=authorised)
            for seat, (system, user) in prompts.items()
        }
        return [futures[seat].result() for seat in prompts]
