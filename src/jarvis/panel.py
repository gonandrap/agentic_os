"""Neo as a panel — a roster of profiled seats, one blind round, one verdict out (docs/superpowers/specs/2026-08-02-neo-team-design.md)"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import claude_cli, seats, structured
from .bootstrap import ASSETS
from .neo_store import SEATS, NeoStore
from .seats import Opinion, SeatError, parse_definition

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .catalog import NeoConfig

log = logging.getLogger("neo.panel")

#: Where the seat definitions live
SEAT_ASSETS = ASSETS / "neo-seats"

#: First line of every seat's system prompt
SEAT_HEADER = "# Neo panel seat: {seat}"

#: Routes
ROUTES = ("fast", "panel", "single")


#: Neo's roster, as `seats.py` sees it: where the mandates live
def neo_roster() -> seats.Roster:
    return seats.Roster(assets=SEAT_ASSETS, vocabulary=SEATS, header=SEAT_HEADER)


def seat_path(seat: str) -> Path:
    return neo_roster().path(seat)


def shipped_seats() -> tuple[str, ...]:
    """The seats whose definition ships in this build, in `SEATS` order"""
    return seats.shipped(neo_roster())


def definition(seat: str) -> tuple[dict[str, str], str]:
    """The shipped (frontmatter, mandate) for one Neo seat"""
    return seats.definition(neo_roster(), seat)


definition.cache_clear = seats.definition.cache_clear  # type: ignore[attr-defined]


def seat_model(seat: str, cfg: NeoConfig) -> str:
    """Which model this seat runs on"""
    panel = cfg.panel
    explicit = panel.seat_models.get(seat) or (panel.chair_model if seat == "chair" else "")
    try:
        declared = definition(seat)[0].get("model", "")
    except SeatError:
        declared = ""
    return explicit or declared or cfg.model


def build_seat_system_prompt(store: NeoStore, project: str, seat: str,
                             learnings_limit: int = 50) -> str:
    """One seat's mandate + the learnings scoped to it"""
    from . import neo

    _, mandate = definition(seat)
    parts = [SEAT_HEADER.format(seat=seat), "", mandate, "",
             "# Learnings (from the user's reviews of your past answers)"]
    parts += neo.render_learnings(
        store.learnings(project, limit=learnings_limit, seat=seat))
    return "\n".join(parts)


# -- one seat's contribution


#: One seat's contribution, and the runner that produces it, both live in `seats.py`
_run_seat = seats._run_seat


def _round(store: NeoStore, q: dict[str, Any], cfg: NeoConfig,
           names: list[str], record: Any = None) -> list[Opinion]:
    """Run every seat blind, concurrently, and record what each said"""
    from .paths import ensure_home

    # Every prompt is built HERE, on the calling thread
    home = ensure_home()
    prompt = _question_prompt(q)
    systems: dict[str, str] = {}
    missing: list[Opinion] = []
    for seat in names:
        try:
            systems[seat] = build_seat_system_prompt(store, q["project"], seat,
                                                     cfg.learnings_limit)
        except SeatError as e:
            # Not an outage: this build has no such seat
            log.error("panel seat %s cannot run: %s", seat, e)
            # `unavailable`, not the word `failed`: this is the ONE construction site for "no such seat in this...
            missing.append(Opinion(seat=seat, raw=str(e), status="failed",
                                   replied=False, unavailable=True))

    opinions = seats.run_blind(
        {seat: (system, prompt) for seat, system in systems.items()},
        models={seat: seat_model(seat, cfg) for seat in systems},
        # `_record` writes every seat's row under this kind
        timeout=cfg.panel.timeout, cwd=home, kind="panel_seat")
    opinions += missing
    for op in opinions:
        _record(store, q, op, record)
    return opinions


def _record(store: NeoStore, q: dict[str, Any], op: Opinion,
            record: Any = None) -> None:
    """Persist one seat's contribution: what it said, and what it cost"""
    from . import agent_usage

    store.record_opinion(q["id"], op.seat, reply=op.raw, verdict=op.verdict,
                         route=op.route, status=op.status, model=op.model,
                         latency_ms=op.latency_ms)
    if op.usage is None:
        return
    (record or agent_usage.record)(
        "panel_seat", usage=op.usage, project=q.get("project") or "",
        wo_id=q.get("wo_id") or "", label=op.seat, model=op.model,
        question_id=q.get("id"), ok=op.status == "ok")


def _question_prompt(q: dict[str, Any]) -> str:
    from . import neo

    return neo.build_question_prompt(q)


# -- routing


def fast_is_permitted(kind: str, proposed: str) -> bool:
    """May this decision take the fast route, given what the premise seat proposed?"""
    if kind == "approval" and proposed == "approved":
        return False
    return proposed == "dismissed" or kind == "question"


def _premise_route(op: Opinion, kind: str, cfg: NeoConfig) -> tuple[str, dict | None]:
    """(route, the premise seat's reply) — `panel` whenever anything is off"""
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
    """The premise seat's proposal, normalised to the store's vocabulary (`dismissed`"""
    from . import neo

    if not str(data.get("verdict") or "").strip():
        return ""
    return neo.parse_verdict(op.raw)["verdict"]


# -- arbitration: the veto table, as code

#: The seats the arbitration reads AT ALL
FORCES_ESCALATE = ("blast", "record", "premise")


def _reply(op: Mapping[str, Any]) -> dict[str, Any]:
    """One opinion's reply as an object, or `{}` when the seat said nothing usable"""
    if str(op.get("status") or "") != "ok":
        return {}
    data = structured.parse_json_object(str(op.get("reply") or ""))
    return data if isinstance(data, dict) else {}


def _raised(data: dict[str, Any], key: str) -> bool:
    """Did the seat raise this flag?"""
    return bool(data.get(key))


def _unresolvable(data: dict[str, Any]) -> bool:
    """Did the record seat report a contradiction it cannot square?"""
    return str(data.get("contradiction") or "").strip().lower() == "unresolvable"


def refused_veto_seats(
        opinions: Sequence[Opinion],
        roster: Sequence[str]) -> list[tuple[str, claude_cli.UsageLimit]]:
    """Veto seats the ACCOUNT refused, in roster order — a spent usage window (#235)"""
    by_seat = {op.seat: op for op in opinions}
    out: list[tuple[str, claude_cli.UsageLimit]] = []
    for seat in roster:
        if seat not in FORCES_ESCALATE:
            continue
        op = by_seat.get(seat)
        if op is not None and op.refused is not None:
            out.append((seat, op.refused))
    return out


def unreachable_veto_seats(opinions: Sequence[Opinion],
                           roster: Sequence[str]) -> list[str]:
    """Seats that could have forced an escalation and were never reached, in roster order"""
    by_seat = {op.seat: op for op in opinions}
    unreached = []
    for seat in roster:
        if seat not in FORCES_ESCALATE:
            continue
        op = by_seat.get(seat)
        if op is None or (not op.replied and not op.unavailable):
            unreached.append(seat)
    return unreached


def arbitrate(opinions: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The veto table"""
    forcing: dict[str, str] = {}
    for op in opinions:
        seat = str(op.get("seat") or "")
        if seat not in FORCES_ESCALATE:
            continue
        data = _reply(op)
        if not data:
            continue
        forced = _raised(data, "escalate")
        if seat == "blast":
            forced = forced or _raised(data, "veto")
        elif seat == "record":
            forced = forced or _unresolvable(data)
        if forced:
            forcing[seat] = str(data.get("reason") or "").strip()

    if not forcing:
        return None

    # The forcing seat's own one line, verbatim and UNATTRIBUTED
    reason = next((forcing[s] for s in FORCES_ESCALATE if forcing.get(s)), "")
    return {"escalate": True, "answer": "", "approve": False, "verdict": "denied",
            "dispatch": None,
            "reason": reason or "the panel could not settle this without you"}


# -- the entry point


def decide(store: NeoStore, q: dict[str, Any], cfg: NeoConfig,
           record: Any = None) -> dict[str, Any]:
    """Answer one claimed question with the panel"""
    from . import neo

    kind = q.get("kind") or "question"
    roster = [s for s in cfg.panel.roster if s != "chair"]

    # THE PREMISE SEAT RUNS FIRST AND ALONE
    opinions = _round(store, q, cfg, [s for s in roster if s == "premise"], record)
    premise = next((op for op in opinions if op.seat == "premise"), None)

    if premise is None or not premise.replied:
        # The seat that routes never spoke, so there is nothing to route on
        log.warning("panel: no usable premise opinion for question %s; "
                    "falling back to the single agent", q["id"])
        verdict = neo.answer_question(store, q, cfg.model, cfg.learnings_limit,
                                      record=record)
        return {**verdict, "panel": _summary("single", opinions)}

    route, _ = _premise_route(premise, kind, cfg)
    if route == "fast":
        # The premise seat's own reply IS the verdict; the chair is skipped, and so is every other seat
        return {**neo.parse_verdict(premise.raw), "panel": _summary("fast", opinions)}

    # The full panel: the remaining seats run blind and in parallel
    rest = [s for s in roster if s != "premise"]
    if rest:
        opinions = [*opinions, *_round(store, q, cfg, rest, record)]

    refused = refused_veto_seats(opinions, roster)
    if refused:
        # BEFORE the generic outage below, and the ordering IS the fix (#235)
        seat, limit = refused[0]
        log.info("panel: %s was refused by the usage window on question %s; "
                 "holding the question rather than deciding without it", seat, q["id"])
        raise claude_cli.UsageLimitError(limit)

    silent = unreachable_veto_seats(opinions, roster)
    if silent:
        # A VETO SEAT NOBODY HEARD FROM SHRINKS THE QUORUM SILENTLY (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §3.)
        raise claude_cli.ClaudeCliError(
            f"the panel could not reach {', '.join(silent)} — "
            f"no verdict can be taken without every seat that may veto one")

    forced = arbitrate([{"seat": op.seat, "status": op.status, "reply": op.raw}
                        for op in opinions])
    if forced is not None:
        # The seats settled it and the chair does not get a vote on the safety rule
        log.info("panel: the seats forced an escalation on question %s; "
                 "the chair was not run", q["id"])
        return {**forced, "panel": _summary("panel", opinions)}

    chair = _run_chair(store, q, cfg, opinions, record)
    opinions = [*opinions, chair]
    return {**neo.parse_verdict(chair.raw), "panel": _summary("panel", opinions)}


def _summary(route: str, opinions: list[Opinion]) -> dict[str, Any]:
    return {"route": route, "seats": [op.summary() for op in opinions]}


def _run_chair(store: NeoStore, q: dict[str, Any], cfg: NeoConfig,
               opinions: list[Opinion], record: Any = None) -> Opinion:
    """Synthesise"""
    from .paths import ensure_home

    model = seat_model("chair", cfg)
    system = build_seat_system_prompt(store, q["project"], "chair", cfg.learnings_limit)
    prompt = build_chair_prompt(q, opinions)
    started = time.monotonic()
    try:
        result = claude_cli.run_headless_result(prompt, system_prompt=system, model=model,
                                                timeout=cfg.panel.timeout,
                                                cwd=ensure_home(),
                                                # `_record` writes this row itself
                                                records_itself="panel_seat")
    except claude_cli.ClaudeCliError as e:
        op = Opinion(seat="chair", raw=str(e), status="abstained", model=model,
                     latency_ms=int((time.monotonic() - started) * 1000))
        _record(store, q, op, record)
        raise
    data = structured.parse_json_object(result.text) or {}
    op = Opinion(seat="chair", raw=result.text, model=model, usage=result.usage,
                 latency_ms=int((time.monotonic() - started) * 1000),
                 verdict=str(data.get("verdict") or "").strip())
    _record(store, q, op, record)
    return op


def build_chair_prompt(q: dict[str, Any], opinions: list[Opinion]) -> str:
    """The question, then every seat's reply verbatim"""
    parts = [_question_prompt(q), "", "# The panel's opinions",
             "Each seat answered blind — none of them saw another's reply, and none of "
             "them saw yours."]
    for op in opinions:
        if op.status == "ok":
            parts += [f"\n## Seat: {op.seat}", op.raw.strip()]
        else:
            parts.append(f"\n## Seat: {op.seat}\n(no opinion — the seat {op.status})")
    return "\n".join(parts)
