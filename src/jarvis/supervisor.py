"""The supervisor — the OS-level agent that answers a cost alarm before the user has to.

PR 159 gave the OS an alarm on a turn that is still burning, and gave the user a flag to
put down. This reads the alarm first and says whether it is explicable: an hour-long turn
on a design document is normal, the same turn on a one-file fix is not, and only one of
those is worth an interruption. §2 of docs/superpowers/specs/2026-08-31-the-supervisor.md.

THE VERDICT VOCABULARY IS EXACTLY `{ack, escalate}`, AND THAT IS A SAFETY PROPERTY RATHER
THAN A SCOPE DECISION. The tempting "helpful" move on a ninety-minute turn is to kill it,
and killing a turn destroys work that has no other record — the transcript is Claude
Code's, not the OS's. So the supervisor reads and reports; it never messages a worker,
never cancels a turn and never sets a status. That rule lives in Python and is pinned by
an AST walk over this file (`tests/test_supervisor.py`), on the `panel.fast_is_permitted`
precedent: a safety rule that matters is not left to a prompt.

`escalate` currently records the intent and stops there — the alarm reaches `escalated`
with no Neo question behind it yet, which degrades exactly to the pre-supervisor
behaviour: the flag stays up and the user still sees it. §3 fills in the question.

Ships DISABLED (`catalog.SupervisorConfig`). A wrong ack makes a burning turn invisible,
which is a strict regression on what PR 159 shipped, so it is the one failure mode worth
holding a whole feature off the road for.
"""

from __future__ import annotations

import logging
from typing import Any

from . import claude_cli, structured

log = logging.getLogger("supervisor")

# An alarm claimed for review longer than this is treated as stranded and returned to the
# queue (`ProjectStore.reclaim_stale_alarms`). The name and the value are `neo_store`'s
# for the identical job, because it is the identical job.
#
# THIS MUST EXCEED THE SUPERVISOR'S CALL TIMEOUT (`catalog.SupervisorConfig.timeout`,
# 300s). A cutoff below it re-claims an alarm out from under a call that is still running,
# the same alarm is judged twice, and the second verdict overwrites the first. That is not
# left to this comment: `catalog._parse_supervisor` refuses a configuration that breaks
# the relation, and a test pins it.
STALE_REVIEWING_SECONDS = 900

# How many times an alarm may be returned to the queue before it is given up on. The
# initial claim is not an attempt — `attempts` counts the reclaims — so this is the number
# of RETRIES. At the ceiling the alarm goes to `failed` rather than back to `raised`:
# `raised` would loop for ever, and `failed` leaves the attention flag up, which is the
# right end state for an alarm nobody managed to judge.
MAX_REVIEW_ATTEMPTS = 3

# The hard ceiling on the evidence packet, `worker_brief.CORE_BUDGET_CHARS`'s precedent
# and its reasoning: a budget nobody measures is a wish.
#
# THE SUPERVISOR IS JUDGING A TURN THAT IS STILL BURNING, SO A SLOW INSTRUMENT IS PART OF
# THE PROBLEM. The alarm is very often ABOUT a 300k re-write, and pasting the worker's
# conversation into the judge's prompt would make the diagnosis one of the largest calls
# the OS makes — measuring the fire by adding to it. Everything below is composed from
# reads the OS has already paid for.
EVIDENCE_BUDGET_CHARS = 8000

#: Per conversation turn quoted in the packet. Enough to see what the worker said it was
#: doing, not enough to reproduce the transcript this file exists to avoid sending.
QUOTE_CHARS = 400

#: How many of the worker's last turns are quoted.
QUOTED_TURNS = 3

#: How much of the work order's own brief travels. The alarm is about how the work is
#: going, and the first paragraph of the ask is what says whether an hour is plausible.
DESCRIPTION_CHARS = 500


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


#: What the user is told when an alarm is acked, as an inbox title. Specified here rather
#: than left to the call site because inbox rows reach every sink, Telegram included, so
#: this is user-facing copy.
ACK_INBOX_TITLE = "Supervisor cleared an alarm on {wo_id}"

#: Prefix of the `reason` a failed review carries. A constant because `review` recognises
#: this path by it, exactly as `neo.UNPARSEABLE_PREFIX` is recognised.
UNREADABLE_PREFIX = "unreadable supervisor output: "


def build_system_prompt(store: Any, project: str, learnings_limit: int = 50) -> str:
    """Persona + the supervisor's learnings. Byte-stable per project.

    Stable so that consecutive reviews share a cached prompt prefix, which is the same
    property `neo.build_system_prompt` is built for and the reason `claim_next_alarm`
    drains FIFO.

    `store` and `learnings_limit` are the seam §6 fills — the memory is `neo.db`'s
    existing learnings table under `seat='supervisor'`. Until then this renders the empty
    block, and it renders one rather than omitting the heading so that §6 extends the
    prefix instead of moving it.
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


def _session_lines(wo: dict[str, Any], cfg: Any) -> list[str]:
    """The per-turn split for the order's session — `live_alarms`' own read, re-rendered.

    Costs no model call and no new file read the OS was not already making on this tick,
    which is the whole reason the packet is shaped around it rather than around a
    transcript.
    """
    from . import inspection

    session_id = wo.get("session_id") or ""
    if not session_id:
        return ["(the work order has no session)"]
    try:
        anatomy = inspection.read_session(session_id, cfg)
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
        # THE LABEL IS THE POINT. `ttl-expiry` and `prefix-miss` cost the same and look
        # identical on a bill; only the second is a defect, and telling them apart is
        # most of what makes an alarm about a re-write explicable or not.
        lines.append(f"- cache write {write.written:,} tokens [{write.cause}]: "
                     f"{write.note}")
    return lines or ["(the session has no turns yet)"]


def build_evidence(pstore: Any, wo: dict[str, Any], alarm: dict[str, Any],
                   cfg: Any = None) -> str:
    """Everything the supervisor is shown, under a hard character ceiling.

    READ-ONLY AND CHEAP, by construction: every section is composed from something the OS
    already has — the alarm row, the work order row, the session read `Daemon
    .check_burning_turns` just made, and the conversation the record is built from. No
    model call, no network.

    THE WORKER'S TRANSCRIPT IS NOT IN HERE and must not be added. See
    EVIDENCE_BUDGET_CHARS. What travels instead is the last few things the worker SAID,
    clipped, which answers "what does it think it is doing" at a thousandth of the size.

    The clip is STATED rather than silent, `neo.render_learnings`' rule: a judge that
    cannot see it was shown a fraction of something will weigh the fraction as the whole.
    """
    from . import db, timeline
    from .catalog import InspectConfig

    cfg = cfg or InspectConfig()
    standing = max(0.0, db.now() - float(alarm.get("ts") or 0.0))

    sections: list[list[str]] = [[
        "# The alarm",
        f"kind: {alarm.get('kind')}",
        f"reason: {alarm.get('reason')}",
        f"raised on turn {alarm.get('seq')}, {standing / 60:.0f} minute(s) ago",
    ], [
        "# The work order",
        f"{wo.get('id')} [{wo.get('status')}] on {wo.get('model') or '(default model)'}",
        f"title: {wo.get('title')}",
        f"brief: {_clip(str(wo.get('description') or ''), DESCRIPTION_CHARS)}",
    ], [
        "# The session, turn by turn",
        *_session_lines(wo, cfg),
    ]]

    conversation = timeline.build_conversation(
        pstore.list_events(wo["id"]), pstore.list_messages(wo["id"]))
    said = ["# What was last said about this order"]
    for turn in conversation[-QUOTED_TURNS:]:
        said.append(f"- {turn['who']}: {_clip(str(turn.get('content') or ''), QUOTE_CHARS)}")
    if len(said) == 1:
        said.append("(nothing has been said about it since it was dispatched)")
    sections.append(said)

    # Whole sections, never a mid-line cut: the packet is read by a model, and half a
    # cache-write line reads as a fact rather than as a truncation.
    packet: list[str] = []
    spent = 0
    for section in sections:
        body = "\n".join(section)
        if packet and spent + len(body) > EVIDENCE_BUDGET_CHARS:
            packet.append(f"({len(sections) - len(packet)} further section(s) omitted — "
                          f"this packet is capped at {EVIDENCE_BUDGET_CHARS} characters. "
                          f"Escalate rather than judge on what you cannot see.)")
            break
        spent += len(body) + 2
        packet.append(body)
    return "\n\n".join(packet)


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise a parsed reply into the verdict dict, or raise.

    `decision` is what says this is a supervisor verdict at all rather than some other
    JSON the model happened to emit, so its ABSENCE IS A BAD SHAPE AND NOT A DEFAULT —
    `neo._validate_verdict` makes the identical call about `escalate` for the identical
    reason. Defaulting it either way would be worse than raising: defaulting to `ack`
    hides a burning turn, and defaulting to `escalate` records a judgement nobody made.
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
        # Clipped to the column the user reads it from, and only on the ack path: a note
        # on an escalation would be shown to nobody and read as an answer if it were.
        "note": str(data.get("note") or "")[:200] if decision == "ack" else "",
        "question": str(data.get("question") or "") if decision == "escalate" else "",
        "failed": False,
    }


def _failed_verdict(raw: str) -> dict[str, Any]:
    """The fail-safe, and IT ESCALATES.

    A FAILURE MUST NEVER BECOME AN ACK. An ack puts the attention flag down on a turn
    that is still spending money, so output nobody can read defaulting to one would make
    the OS quieter exactly when it has least idea what is going on — a strict regression
    on the alarm PR 159 shipped. Failing toward the user's attention is the failure the
    OS can recover from.

    `failed` is what separates this from a judgement: the alarm reaches `failed`, not
    `escalated`, so `jarvis alarms` shows an alarm nobody judged rather than one the
    supervisor decided to hand on. `Daemon._neo_drain` reads Neo's equivalent flag the
    same way.

    Deliberately NOT a retry (`attempts=1`): a second call spends money to discover the
    same thing, and there is nothing riding on the answer that the user's own attention
    does not already cover.
    """
    return {"decision": "escalate", "note": "", "question": "",
            "failed": True,
            "reason": f"{UNREADABLE_PREFIX}{(raw or '')[:120]}"}


def _transport_failure(exc: Exception) -> dict[str, Any]:
    """A call that never happened. `structured.request`'s `on_invalid` DOES NOT COVER
    THIS — transport errors propagate untouched, by design, because a call that never
    happened is not invalid output (kn-9b18a8eb). Without this the review raises out of
    the daemon's own thread pool.
    """
    return {"decision": "escalate", "note": "", "question": "", "failed": True,
            "reason": f"the supervisor could not be reached: {str(exc)[:160]}"}


def review(pstore: Any, neo_store: Any, project: str, wo: dict[str, Any],
           alarm: dict[str, Any], model: str, timeout: int, record: Any = None,
           central: Any = None, cfg: Any = None) -> dict[str, Any]:
    """Judge one claimed alarm and record the outcome. Returns the verdict.

    `project` is named rather than derived: `ProjectStore` holds a path, not a name, and
    the name is what the system prompt is stable PER, what the `agent_calls` row is filed
    under, and what routes the inbox notification.

    `record(kind, usage=…, …)` is the accounting seam (`agent_usage.record` by default):
    the review costs tokens and they belong to the work order that caused them, so
    `jarvis cost <wo-id>` shows what answering its alarm cost. Recorded per ATTEMPT,
    through `on_usage`, because an unreadable reply was paid for just the same.

    EVERY FAILURE LEAVES THE ALARM UNRESOLVED AND THE FLAG UP. There are three shapes and
    they arrive by two different routes — unreadable output and a valid object with no
    `decision` come back through `on_invalid`, a transport error through the `except`
    below — and all three end at `status='failed'`.
    """
    from . import agent_usage
    from .central_store import CentralStore
    from .paths import ensure_home

    record = record or agent_usage.record
    system = build_system_prompt(neo_store, project)
    prompt = build_evidence(pstore, wo, alarm, cfg)

    try:
        verdict = structured.request(
            prompt,
            validate=_validate,
            system_prompt=system,
            model=model,
            # Neo's FAIL-SAFE shape, not the panel chair's retry shape, and the two are
            # opposite policies. There is nothing to gain from asking again: the
            # fallback already reaches the user, which is where an unanswerable alarm
            # was going anyway.
            attempts=1,
            on_invalid=_failed_verdict,
            timeout=timeout,
            # Neutral cwd, Neo's reason: running from the project directory would pull
            # that repo's CLAUDE.md into the prompt and break prefix stability.
            cwd=ensure_home(),
            on_usage=agent_usage.recorder(
                "supervisor", project=project, wo_id=wo["id"],
                label=str(alarm.get("kind") or ""), model=model, record=record),
        )
    except claude_cli.ClaudeCliError as exc:
        verdict = _transport_failure(exc)

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
    """Write the verdict down. THE ROW IS THE MEMORY, and that is load-bearing.

    `invariants.true_blockers` has no branch for a live cost alarm — an alarm fires on a
    `running` order and none of its branches match that status — so `ack_attention(wo_id,
    [])` records nothing durable. The flag stays down because §1's dedupe guarantees
    nothing re-raises it, NOT because the acknowledgement was remembered anywhere. So
    every answer the supervisor reaches goes onto `wo_alarms`, and that row is what
    `jarvis alarms` and `/alarms` read.
    """
    from . import db, ops

    alarm_id = alarm["id"]
    decided = db.now()
    if verdict["failed"]:
        # Not `escalated`: nobody judged this. `failed` says so, and leaves the flag.
        pstore.update_alarm(alarm_id, status="failed",
                            verdict_reason=verdict["reason"], decided_at=decided)
        log.warning("supervisor review of %s failed: %s", alarm_id, verdict["reason"])
        return

    if verdict["decision"] == "escalate":
        # §3 fills in `neo_question_id` and actually asks. Until it lands this records
        # the intent and stops, which degrades to exactly the pre-supervisor behaviour:
        # the flag stays up and the user still sees the alarm.
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

    # THROUGH `ops.ack_attention`, NEVER `ProjectStore.clear_attention`. The store method
    # wipes `acknowledged_blockers` — "any ack against it is spent" — so a supervisor
    # using it would silently discard the user's OWN earlier dismissals on that order.
    # `ops.ack_attention` remembers them, and it inherits the refusal on pending
    # assumptions, which is exactly right: a decision waiting for the user is the louder
    # ask, and the supervisor must not bury it. When it refuses, the alarm stays `acked`
    # — it WAS judged — and the flag stays up.
    try:
        ops.ack_attention(wo["id"])
    except ops.OpsError as exc:
        log.info("alarm %s acked; attention left up: %s", alarm_id, exc)

    central.add_inbox(
        project=project,
        level="info",
        title=ACK_INBOX_TITLE.format(wo_id=wo["id"]),
        body=verdict["note"],
        wo_id=wo["id"],
    )
