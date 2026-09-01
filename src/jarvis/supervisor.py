"""The supervisor: it reads a cost alarm and either acks it or wants Neo.

docs/superpowers/specs/2026-08-31-the-supervisor.md §2. Every threshold is
`catalog.SupervisorConfig`; the module holds no numbers of its own.

THE VERDICT VOCABULARY IS EXACTLY `{ack, escalate}`. It never messages a worker, cancels
a turn or sets a status — a safety rule pinned by an AST walk over this file, not by the
persona (`tests/test_supervisor.py`).
"""

from __future__ import annotations

import logging
from typing import Any

from . import claude_cli, structured

log = logging.getLogger("supervisor")

SECONDS_PER_MINUTE = 60  # a unit, not a setting


SUPERVISOR_PERSONA = """You are the SUPERVISOR inside the Jarvis agentic OS.

The OS raised a cost alarm on a work order whose turn is STILL RUNNING — it has been going
too long, has been blocked on a subagent too long, or re-sent too much of its conversation
in one call. Left alone that alarm becomes an attention item: the user is interrupted and
has to go and look. Your job is to look instead, and to decide whether it needed them.

You are shown evidence, never the worker's transcript. Judge on what you are given.

ACK when the spend is EXPLICABLE — the shape of the work accounts for it. A long turn on a
design document, a planning session, a large refactor or a test suite that takes an hour is
the work costing what the work costs. A single large cache write at the start of a session
is a cold start and is not a defect. When you ack, the flag comes down and the user gets
your `note` instead of an interruption, so the note must stand alone: say what the order is
doing and why the number is what it is, in plain words, without the reader opening anything.

ESCALATE when you cannot account for it, or when what you can see suggests something is
actually wrong — a turn with no visible progress, a join that has outlived any plausible
subagent, a conversation re-sent repeatedly rather than once. Escalating is not a failure:
it is the honest answer whenever the evidence does not settle the question, and it costs
the user exactly what they were going to pay anyway. PREFER IT WHEN UNSURE. A wrong ack
hides a turn that is still burning money; a wrong escalation costs one glance.

WHAT YOU MAY NOT DO, and the OS enforces it in code rather than trusting this paragraph:
you do not message the worker, cancel its turn, change its status, or act on the work order
in any way. Your entire output is a judgement. Do not offer to intervene and do not phrase
the note as though you had.

Output STRICT JSON, nothing else:
  {"decision": "ack", "reason": "<why, 1-2 sentences, for the record>",
   "note": "<what the user is told, <= 200 chars, plain words>", "question": ""}
  or
  {"decision": "escalate", "reason": "<why, 1-2 sentences, for the record>",
   "note": "", "question": "<the one question to put to Neo>"}"""

#: User-facing copy: inbox rows reach every sink, Telegram included.
ACK_INBOX_TITLE = "Supervisor cleared an alarm on {wo_id}"

#: `review` recognises the unreadable-output path by this prefix, as `neo` does.
UNREADABLE_PREFIX = "unreadable supervisor output: "


def build_system_prompt(store: Any, project: str,
                        learnings_limit: int | None = None) -> str:
    """Persona + learnings, byte-stable per project so consecutive reviews share a
    cached prefix. `store`/`learnings_limit` are the seam §6 fills; the empty block is
    rendered rather than omitted so §6 extends the prefix instead of moving it.

    `None` means `catalog.SupervisorConfig.learnings_limit` — the default lives there,
    not here.
    """
    return "\n".join([
        SUPERVISOR_PERSONA,
        "",
        "# Learnings (from the user's corrections of your past decisions)",
        "(none yet — escalate when unsure)",
    ])


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + " […]"


def _session_lines(wo: dict[str, Any], inspect_cfg: Any) -> list[str]:
    """The per-turn split — `live_alarms`' own read, re-rendered. No model call."""
    from . import inspection

    session_id = wo.get("session_id") or ""
    if not session_id:
        return ["(the work order has no session)"]
    try:
        anatomy = inspection.read_session(session_id, inspect_cfg)
    except OSError:
        return ["(the session transcript could not be read)"]
    if not anatomy.found:
        return ["(no transcript found for this session)"]

    lines = []
    for turn in anatomy.turns:
        lines.append(
            f"- turn {turn.seq}: {turn.wall:.0f}s wall "
            f"({turn.generating:.0f}s generating, {turn.blocked:.0f}s blocked, "
            f"{turn.tools:.0f}s tools, {turn.idle:.0f}s idle), "
            f"context peak {turn.context_peak:,}")
        for trigger in turn.triggers:
            lines.append(f"    started by [{trigger.kind}] {trigger.quote}")
    for join in anatomy.joins():
        lines.append(f"- blocked {join.seconds:.0f}s on {join.name}: {join.detail}")
    for write in anatomy.writes:
        # The cause label is what separates an explicable re-write from a defect.
        lines.append(f"- cache write {write.written:,} tokens [{write.cause}]: "
                     f"{write.note}")
    return lines or ["(the session has no turns yet)"]


def build_evidence(pstore: Any, wo: dict[str, Any], alarm: dict[str, Any],
                   cfg: Any = None, inspect_cfg: Any = None) -> str:
    """Everything the supervisor is shown, under `cfg.evidence_budget_chars`.

    THE WORKER'S TRANSCRIPT IS NOT IN HERE and must not be added — see §2 for why. The
    clip is stated rather than silent: a judge that cannot see it was shown a fraction
    weighs the fraction as the whole.
    """
    from . import db, timeline
    from .catalog import InspectConfig, SupervisorConfig

    cfg = cfg or SupervisorConfig()
    inspect_cfg = inspect_cfg or InspectConfig()
    standing = max(0.0, db.now() - float(alarm.get("ts") or 0.0))
    minutes = standing / SECONDS_PER_MINUTE

    sections: list[list[str]] = [[
        "# The alarm",
        f"kind: {alarm.get('kind')}",
        f"reason: {alarm.get('reason')}",
        f"raised on turn {alarm.get('seq')}, {minutes:.0f} minute(s) ago",
    ], [
        "# The work order",
        f"{wo.get('id')} [{wo.get('status')}] on {wo.get('model') or '(default model)'}",
        f"title: {wo.get('title')}",
        f"brief: {_clip(str(wo.get('description') or ''), cfg.description_chars)}",
    ], [
        "# The session, turn by turn",
        *_session_lines(wo, inspect_cfg),
    ]]

    conversation = timeline.build_conversation(
        pstore.list_events(wo["id"]), pstore.list_messages(wo["id"]))
    said = ["# What was last said about this order"]
    for turn in conversation[-cfg.quoted_turns:]:
        said.append(f"- {turn['who']}: "
                    f"{_clip(str(turn.get('content') or ''), cfg.conversation_quote_chars)}")
    if len(said) == 1:
        said.append("(nothing has been said about it since it was dispatched)")
    sections.append(said)

    # Whole sections, never a mid-line cut: half a cache-write line reads as a fact.
    packet: list[str] = []
    spent = 0
    for section in sections:
        body = "\n".join(section)
        if packet and spent + len(body) > cfg.evidence_budget_chars:
            packet.append(f"({len(sections) - len(packet)} further section(s) omitted — "
                          f"this packet is capped at {cfg.evidence_budget_chars} "
                          f"characters. Escalate rather than judge on what you cannot "
                          f"see.)")
            break
        spent += len(body) + len("\n\n")
        packet.append(body)
    return "\n\n".join(packet)


def _validate(data: dict[str, Any], note_chars: int) -> dict[str, Any]:
    """Normalise a parsed reply into the verdict dict, or raise.

    A MISSING `decision` IS A BAD SHAPE, NOT A DEFAULT — it is what says this is a
    supervisor verdict at all. `neo._validate_verdict` makes the same call about
    `escalate`. Defaulting to `ack` would hide a burning turn; to `escalate` would
    record a judgement nobody made.
    """
    decision = str(data.get("decision") or "").strip().lower()
    if not decision:
        raise structured.InvalidOutput("no `decision` field in the supervisor's reply")
    if decision not in ("ack", "escalate"):
        raise structured.InvalidOutput(
            f"`decision` must be 'ack' or 'escalate', got {decision!r}")
    return {
        "decision": decision,
        "reason": str(data.get("reason") or ""),
        "note": str(data.get("note") or "")[:note_chars] if decision == "ack" else "",
        "question": str(data.get("question") or "") if decision == "escalate" else "",
        "failed": False,
    }


def _failed_verdict(raw: str, reason_chars: int) -> dict[str, Any]:
    """The fail-safe, and IT ESCALATES: a failure must never become an ack, which would
    put the flag down on a turn that is still spending money.

    `failed` is what puts the alarm at `failed` rather than at `escalated` — nobody
    judged it. `Daemon._neo_drain` reads Neo's equivalent flag the same way.
    """
    return {"decision": "escalate", "note": "", "question": "", "failed": True,
            "reason": f"{UNREADABLE_PREFIX}{(raw or '')[:reason_chars]}"}


def _transport_failure(exc: Exception, reason_chars: int) -> dict[str, Any]:
    """A call that never happened, which `structured.request`'s `on_invalid` does NOT
    cover — `ClaudeCliError` propagates untouched by design (kn-9b18a8eb). Without this
    the review raises out of the daemon's own thread pool."""
    return {"decision": "escalate", "note": "", "question": "", "failed": True,
            "reason": f"the supervisor could not be reached: {str(exc)[:reason_chars]}"}


def review(pstore: Any, neo_store: Any, project: str, wo: dict[str, Any],
           alarm: dict[str, Any], cfg: Any, record: Any = None,
           central: Any = None, inspect_cfg: Any = None) -> dict[str, Any]:
    """Judge one claimed alarm and record the outcome. Returns the verdict.

    `project` is named because `ProjectStore` holds a path, not a name, and the name is
    what the prompt is stable per, what the `agent_calls` row is filed under and what
    routes the inbox row. `record` is the accounting seam (`agent_usage.record`), called
    per ATTEMPT: an unreadable reply was paid for just the same.

    ALL THREE FAILURE SHAPES END AT `status='failed'` WITH THE FLAG UP, and they arrive
    by two routes — see `_failed_verdict` and `_transport_failure`.
    """
    from . import agent_usage
    from .central_store import CentralStore
    from .paths import ensure_home

    record = record or agent_usage.record
    try:
        verdict = structured.request(
            build_evidence(pstore, wo, alarm, cfg, inspect_cfg),
            validate=lambda data: _validate(data, cfg.note_chars),
            system_prompt=build_system_prompt(neo_store, project, cfg.learnings_limit),
            model=cfg.model,
            # Neo's FAIL-SAFE shape, not the panel chair's retry shape: asking again
            # spends a call to learn the same thing, and the fallback already reaches
            # the user, which is where an unanswerable alarm was going anyway.
            attempts=1,
            on_invalid=lambda raw: _failed_verdict(raw, cfg.reason_chars),
            timeout=cfg.timeout,
            # Neutral cwd, Neo's reason: a project directory would pull its CLAUDE.md in
            # and break prefix stability.
            cwd=ensure_home(),
            on_usage=agent_usage.recorder(
                "supervisor", project=project, wo_id=wo["id"],
                label=str(alarm.get("kind") or ""), model=cfg.model, record=record),
        )
    except claude_cli.ClaudeCliError as exc:
        verdict = _transport_failure(exc, cfg.reason_chars)

    own_central = central is None
    central = central or CentralStore()
    try:
        _apply(pstore, central, project, wo, alarm, verdict)
    finally:
        if own_central:
            central.close()
    return verdict


def _apply(pstore: Any, central: Any, project: str, wo: dict[str, Any],
           alarm: dict[str, Any], verdict: dict[str, Any]) -> None:
    """Write the verdict down. THE ALARM ROW IS THE MEMORY: `invariants.true_blockers`
    has no branch for a live cost alarm, so `ack_attention(wo_id, [])` records nothing
    durable and only §1's dedupe keeps the flag down. See §2.
    """
    from . import db, ops

    alarm_id = alarm["id"]
    decided = db.now()
    if verdict["failed"]:
        pstore.update_alarm(alarm_id, status="failed",
                            verdict_reason=verdict["reason"], decided_at=decided)
        log.warning("supervisor review of %s failed: %s", alarm_id, verdict["reason"])
        return

    if verdict["decision"] == "escalate":
        # §3 fills in `neo_question_id` and actually asks; until then this degrades to
        # the pre-supervisor behaviour — the flag stays up.
        pstore.update_alarm(alarm_id, status="escalated", verdict="escalate",
                            verdict_reason=verdict["reason"], decided_at=decided)
        pstore.add_event(wo["id"], "alarm_reviewed",
                         {"alarm_id": alarm_id, "verdict": "escalate",
                          "reason": verdict["reason"], "note": ""})
        return

    pstore.update_alarm(alarm_id, status="acked", verdict="ack",
                        verdict_reason=verdict["reason"], note=verdict["note"],
                        decided_at=decided)
    pstore.add_event(wo["id"], "alarm_reviewed",
                     {"alarm_id": alarm_id, "verdict": "ack",
                      "reason": verdict["reason"], "note": verdict["note"]})

    # THROUGH `ops.ack_attention`, NEVER `ProjectStore.clear_attention`, which wipes
    # `acknowledged_blockers` and would discard the user's own earlier dismissals. It
    # also refuses an order with a pending assumption — the louder ask — and then the
    # alarm stays `acked` (it WAS judged) with the flag up.
    try:
        ops.ack_attention(wo["id"])
    except ops.OpsError as exc:
        log.info("alarm %s acked; attention left up: %s", alarm_id, exc)

    central.add_inbox(project=project, level="info",
                      title=ACK_INBOX_TITLE.format(wo_id=wo["id"]),
                      body=verdict["note"], wo_id=wo["id"])
