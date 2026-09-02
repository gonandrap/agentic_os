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
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE  # likewise; a feature's clock runs in days


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
    cached prefix.

    `neo_store.SUPERVISOR_SEAT` is a LEARNING SCOPE, not a panel seat — see
    `neo_store.LEARNING_SCOPES`. Rendering goes through `neo.render_learnings` so the
    character budget, the oldest-first truncation that keeps this block append-only and
    the "N older learnings not shown" note are the ones Neo and every panel seat already
    obey; a second renderer is how the blocks come to differ.

    `None` means `catalog.SupervisorConfig.learnings_limit` — the default lives there,
    not here.
    """
    from . import neo
    from .catalog import SupervisorConfig
    from .neo_store import SUPERVISOR_SEAT

    if learnings_limit is None:
        learnings_limit = SupervisorConfig().learnings_limit
    return "\n".join([
        SUPERVISOR_PERSONA,
        "",
        "# Learnings (from the user's corrections of your past decisions)",
        *neo.render_learnings(
            store.learnings(project, limit=learnings_limit, seat=SUPERVISOR_SEAT)),
    ])


def learning_from_review(alarm: dict[str, Any], feedback: str) -> str:
    """Distil the user's correction of a verdict into what the next review is shown.

    Separate from `neo.learning_from_review` rather than a widening of it: that one takes
    a Neo question and an alarm is not one, so accepting both shapes would put a branch in
    the middle of Neo's own review path for no gain (§6).

    Nothing here is clipped. Every component but `feedback` is already bounded by a
    `SupervisorConfig` setting at the moment it was written, and the one that is not is
    the user's own ruling — which is exactly the text `render_learnings` is written never
    to drop silently.
    """
    return (f"On a {alarm.get('kind')} alarm ({alarm.get('reason')}) the supervisor "
            f"decided {alarm.get('verdict')} because {alarm.get('verdict_reason')}. "
            f"The user's ruling: {feedback}")


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


#: A child the plan has finished with. Everything else is still in flight, and a health
#: probe about a feature is nearly always about what is still in flight.
SETTLED_CHILD_STATUSES = ("completed", "cancelled")

#: The order the child tree is CLIPPED in, worst first. Named buckets rather than four
#: integer ranks because `test_nothing_in_the_module_hard_codes_a_threshold` allows this
#: module no numeric literal, and the names read better than the numbers would anyway.
CHILD_TRIAGE = ("failed", "blocked", "unfinished", "settled")

#: A feature with no carrier has nothing on record that anyone said about it, and saying
#: so is not the same as saying nothing.
NO_CARRIER_LINE = ("(no work order carries this feature, so nothing said about it is on "
                   "record)")


def _feature_lines(fo: dict[str, Any], carrier: dict[str, Any] | None,
                   cfg: Any) -> list[str]:
    """`updated_at` IS the status clock: `set_feature_status` goes through
    `update_feature_order`, which stamps it on every move."""
    from . import db

    held = max(0.0, db.now() - float(fo.get("updated_at") or 0.0)) / SECONDS_PER_HOUR
    attention = (f"yes — {fo.get('attention_reason') or '(no reason recorded)'}"
                 if fo.get("needs_attention") else "no")
    return [
        "# The feature",
        f"{fo.get('id')} [{fo.get('status')}] for {held:.0f} hour(s)",
        f"title: {fo.get('title')}",
        f"carrier: {carrier['id'] if carrier else '(none)'}",
        f"needs attention: {attention}",
        f"brief: {_clip(str(fo.get('description') or ''), cfg.description_chars)}",
    ]


def _child_rank(child: dict[str, Any]) -> tuple[int, float]:
    """What survives the clip when the tree does not fit: what is wrong, then what is
    stuck, then what is oldest and unfinished. A judge shown only the completed half of
    a feature would call a collapsing one healthy."""
    from . import db

    status = str(child.get("status") or "")
    if status == "failed":
        bucket = "failed"
    elif status == "pending" and db.from_json(child.get("depends_on"), []):
        bucket = "blocked"
    elif status not in SETTLED_CHILD_STATUSES:
        bucket = "unfinished"
    else:
        bucket = "settled"
    return (CHILD_TRIAGE.index(bucket), float(child.get("created_at") or 0.0))


def _child_line(child: dict[str, Any]) -> str:
    from . import db

    deps = db.from_json(child.get("depends_on"), []) or []
    facts = [f"spec section: {child.get('spec_section') or '(none)'}",
             f"depends on {', '.join(deps)}" if deps else "depends on nothing",
             "has a PR" if child.get("pr_url") else "no PR"]
    if child.get("superseded"):
        facts.append("superseded")
    return (f"- {child.get('id')} [{child.get('status')}] {child.get('title')} — "
            + ", ".join(facts))


def _child_lines(children: list[dict[str, Any]], budget: int) -> list[str]:
    """The tree, bounded WITHIN its section — it is the one section that scales with the
    feature rather than with the settings, so whole-section clipping alone would let a
    twelve-child feature push every later section off the end.

    Chosen by `_child_rank`, RENDERED in the plan's own order: `feature_children` is
    oldest-first because creation order IS the dependency order, and reading the graph
    out of order costs the judge its structure. The omission is counted for the same
    reason the packet's own is stated.
    """
    kept: set[str] = set()
    spent = 0
    for child in sorted(children, key=_child_rank):
        line = _child_line(child)
        if kept and spent + len(line) > budget:
            break
        spent += len(line) + len("\n")
        kept.add(str(child.get("id")))

    lines = [_child_line(c) for c in children if str(c.get("id")) in kept]
    dropped = [c for c in children if str(c.get("id")) not in kept]
    if dropped:
        statuses = sorted({str(c.get("status") or "") for c in dropped})
        tail = f"all {statuses[0]}" if len(statuses) == 1 else "of mixed status"
        lines.append(f"…and {len(dropped)} further children, {tail}")
    return lines or ["(this feature has no children yet)"]


def _answered_lines(pstore: Any, fo_id: str) -> list[str]:
    """What the user already ruled on with `jarvis fo resume`. Without it the supervisor
    re-reports a decision that has been taken — see §3 on why that is the worst thing
    this feature could do."""
    lines = ["# What the user has already answered for"]
    for entry in pstore.superseded_children(fo_id):
        lines.append(f"- {entry.get('wo_id')}: {entry.get('note') or '(no note given)'}")
    if len(lines) == 1:
        lines.append("(nothing about this feature has been ruled on)")
    return lines


def _validation_lines(pstore: Any, fo_id: str, cfg: Any) -> list[str]:
    lines = ["# How validation has judged it"]
    for round_ in pstore.validation_rounds(fo_id=fo_id):
        reason = _clip(str(round_.get("reason") or ""), cfg.conversation_quote_chars)
        lines.append(f"- round {round_.get('round')}: {round_.get('outcome')} — "
                     f"{reason or '(no reason recorded)'}")
    if len(lines) == 1:
        lines.append("(it has never been through validation)")
    return lines


def _said_lines(pstore: Any, wo_id: str | None, cfg: Any) -> list[str]:
    """The last `cfg.quoted_turns` of `timeline.build_conversation`, for a work order or
    for a feature's CARRIER — one read, because two renderings of what was said is how
    two surfaces come to quote different things."""
    from . import timeline

    said = ["# What was last said about this order"]
    if wo_id is None:
        said.append(NO_CARRIER_LINE)
        return said
    conversation = timeline.build_conversation(
        pstore.list_events(wo_id), pstore.list_messages(wo_id))
    for turn in conversation[-cfg.quoted_turns:]:
        said.append(f"- {turn['who']}: "
                    f"{_clip(str(turn.get('content') or ''), cfg.conversation_quote_chars)}")
    if len(said) == 1:
        said.append("(nothing has been said about it since it was dispatched)")
    return said


def build_evidence(pstore: Any, subject: dict[str, Any], alarm: dict[str, Any],
                   cfg: Any = None, inspect_cfg: Any = None) -> str:
    """Everything the supervisor is shown, under `cfg.evidence_budget_chars`.

    `subject` is `{"kind": "work_order" | "feature_order", "row": <the store row>}`.
    One builder for both, not two: the alarm section, the budget and the stated omission
    are the same discipline whichever is being judged, and the work-order packet's bytes
    are a cached prompt prefix that a second implementation would drift from (§3).

    THE WORKER'S TRANSCRIPT IS NOT IN HERE and must not be added — see §2 for why. The
    clip is stated rather than silent: a judge that cannot see it was shown a fraction
    weighs the fraction as the whole.
    """
    from . import db
    from .catalog import InspectConfig, SupervisorConfig
    from .project_store import NO_TURN

    cfg = cfg or SupervisorConfig()
    inspect_cfg = inspect_cfg or InspectConfig()
    standing = max(0.0, db.now() - float(alarm.get("ts") or 0.0))
    minutes = standing / SECONDS_PER_MINUTE
    # A subject-level finding was raised on no turn, and `-1` reaching the supervisor's
    # own prompt is nonsense it would have to interpret (§3).
    seq = alarm.get("seq")
    raised = ("raised on no particular turn" if seq == NO_TURN
              else f"raised on turn {seq}")

    sections: list[list[str]] = [[
        "# The alarm",
        f"kind: {alarm.get('kind')}",
        f"reason: {alarm.get('reason')}",
        f"{raised}, {minutes:.0f} minute(s) ago",
    ]]

    row = subject["row"]
    if subject["kind"] == "feature_order":
        # No `_session_lines` here: a feature has no session, and reading the carrier's
        # would show the judge a transcript of work that is not what it is judging.
        fo_id = row["id"]
        carrier = pstore.carrier_for_feature(fo_id)
        rest = [_feature_lines(row, carrier, cfg),
                _answered_lines(pstore, fo_id),
                _validation_lines(pstore, fo_id, cfg),
                _said_lines(pstore, carrier["id"] if carrier else None, cfg)]
        # An equal share of the budget, so the tree's bound moves with the setting and
        # this module goes on holding no numbers of its own. `+ 1` counts the tree.
        tree = ["# The child tree",
                *_child_lines(pstore.feature_children(fo_id),
                              cfg.evidence_budget_chars
                              // (len(sections) + len(rest) + 1))]
        sections += [rest[0], tree, *rest[1:]]
    else:
        sections.append([
            "# The work order",
            f"{row.get('id')} [{row.get('status')}] on "
            f"{row.get('model') or '(default model)'}",
            f"title: {row.get('title')}",
            f"brief: {_clip(str(row.get('description') or ''), cfg.description_chars)}",
        ])
        sections.append(["# The session, turn by turn",
                         *_session_lines(row, inspect_cfg)])
        sections.append(_said_lines(pstore, row["id"], cfg))

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
            build_evidence(pstore, {"kind": "work_order", "row": wo}, alarm,
                           cfg, inspect_cfg),
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
