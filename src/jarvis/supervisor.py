"""The supervisor: it reads an alarm and acks it, wants Neo, or proposes a remedy (docs/superpowers/specs/2026-08-31-the-supervisor.md §2)"""

from __future__ import annotations

import logging
from typing import Any

from . import claude_cli, db, health, holds, structured
from .project_store import WORKER_CHILDREN

log = logging.getLogger("supervisor")

SECONDS_PER_MINUTE = 60  # a unit, not a setting
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE  # likewise; a feature's clock runs in days


SUPERVISOR_PERSONA = """You are the SUPERVISOR inside the Jarvis agentic OS.

The OS raised a cost alarm on a work order whose turn is STILL RUNNING — it has been going
too long, has been blocked on a subagent too long, or re-sent too much of its conversation
in one call. Left alone that alarm becomes an attention item: the user is interrupted and
has to go and look. Your job is to look instead, and to decide whether it needed them.

You are shown evidence, never the worker's transcript. Judge on what you are given.

NOT EVERY ALARM IS A BURNING TURN. Two kinds — `rewrite-tax-prefix` and `rewrite-tax-ttl`
— are a STANDING condition instead: a whole project's re-write tax over a window of
ALREADY-SETTLED orders. Three things follow, and getting any of them wrong wastes the
call. The work order they are attached to is the biggest single contributor, shown to you
as the EXEMPLAR of the number — it is not itself in trouble, nothing about it is still
running, and there is no turn to interrupt. Nothing you decide can prevent the spend
already in the alarm; what is preventable is the NEXT window's. And the two kinds are two
different failures with opposite cures — the prompt prefix moving is fixed by finding what
changes the head of the prompt between calls, the cache entry expiring is fixed by keeping
the gap between calls shorter or by a longer TTL — so an answer that blurs them sends the
fix at the wrong one. Never propose changing a cache TTL off a `rewrite-tax-ttl` alarm
alone: its threshold is a share of the BILL and the TTL decision is taken against a
different ratio, which the alarm's own text names along with the command that measures it.

AND ONE MORE IS NOT ABOUT THIS PROJECT AT ALL. `cache-1h-dispatched` is the FLEET's
one-hour cache writes by turns Jarvis armed for five minutes — every transcript on the
machine. The work order it hangs off carries the foreign key and NOTHING else: it is not
the exemplar of the number, it did not cause it, and it may have nothing to do with it.
It says Jarvis's own transport bought the expensive write, which is a DEFECT IN THIS
CODEBASE and a work order anyone can act on. A person's own `claude` sessions are not
alarmed on: Jarvis must never write a user's `~/.claude/settings.json`, so never propose
a remedy that edits it. An old `cache-1h-foreign` row is historical — ack it.

`cache-arm-missed` is fleet-wide in the same way, and it is always the OS's own defect.
It compares the arm a turn was STAMPED with against the TTL its calls paid: an arm the CLI
never honoured bought the other rate, so `wo_turns.cache_ttl` records intent only. The
cure is never the user's settings file — it is the OS setting a variable that OUTRANKS
every user-owned key for the same behaviour.

READ WHAT A TURN COST BEFORE YOU CALL IT EXPENSIVE. Every turn in the packet carries its
API call count and the tokens and dollars it actually bought. A turn that made NO API call
has spent nothing however long its wall clock is: it STALLED, and it is the work never
starting rather than effort being re-spent. Saying it was billed, burned or wasted puts a
false claim about money in front of the user, which is worse than the silence it describes.

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

PROPOSE when one of the remedies listed below would plainly help and you can say exactly
why. A proposal is not an action: it names a remedy from a closed list and files a request
that a reviewer must approve before anything happens, and the flag stays UP until it does.
Propose only what is listed for you — the list below is the whole vocabulary, and there is
no free-text action. Two ways of asking wrongly are both recorded as your mistake rather
than quietly turned into something else:

- naming no remedy, or a remedy that does not exist, FAILS the verdict outright. The alarm
  is recorded as unjudged and the user is interrupted. It is not read as an escalation,
  because a judge that asked for an action it could not name did not mean to escalate;
- naming a remedy this project has not armed, or one that does not apply to this kind of
  subject, is refused and the alarm goes to the user with your reasoning attached.

WHAT YOU MAY NOT DO, and the OS enforces it in code rather than trusting this paragraph:
you do not message the worker, cancel its turn, change its status, or act on the work order
in any way. Even a proposal you make is carried out by a different part of the OS, only
after a reviewer has agreed and only for the exact remedy you named. Your entire output is
a judgement. Do not offer to intervene and do not phrase the note as though you had.

Output STRICT JSON, nothing else:
  {"decision": "ack", "reason": "<why, 1-2 sentences, for the record>",
   "note": "<what the user is told, <= 200 chars, plain words>", "question": ""}
  or
  {"decision": "escalate", "reason": "<why, 1-2 sentences, for the record>",
   "note": "", "question": "<the one question to put to Neo>"}
  or
  {"decision": "propose", "remedy": "<one of the ids listed below>",
   "argument": "<what to say, or why>",
   "reason": "<why, 1-2 sentences, for the record>",
   "note": "<what the user is told, plain words>"}"""

ALARM_REVIEWER_PERSONA = """You are Neo, reviewing a COST ALARM inside the Jarvis \
agentic OS.

The OS raised an alarm on a session whose turn was still running — too long, blocked on a
subagent too long, or re-sending too much of its conversation. A supervisor agent looked
first and could not settle it, so it is yours. You are the second reader and the last one
before the user is interrupted.

READ WHAT THE TURN COST, NEVER INFER IT FROM THE CLOCK. The packet gives every turn its
API call count and its actual tokens and dollars. A turn with no API call bought nothing
however long it ran — it stalled, and an answer or escalation that describes it as billed
is a false claim about money reaching the user.

A TURN THAT IS AWAITING THE MODEL IS NOT A STALLED TURN. The transcript gains an assistant
row only when a content block COMPLETES, so a turn whose last row is an input — a prompt
or a tool result — with nothing after it has a request IN FLIGHT and is spending output
tokens right now. The packet says so in those words. Never describe such a turn as
stalled, as costing nothing, or as never having started, and never propose anything that
would end it: a measured case ran 19 minutes at 3.5 tokens per second and was escalated as
a dead turn.

SOME ALARMS ARE NOT ABOUT A TURN AT ALL. `rewrite-tax-prefix` and `rewrite-tax-ttl` are a
whole project's re-write tax over a window of already-settled orders; the work order they
hang off is the biggest single contributor, shown as the EXEMPLAR and not as the culprit.
Judge the project's figure, not that order's, and keep the two causes apart — the prefix
moving and the entry expiring have opposite cures, and one answer covering both is the
misreading these two kinds were split to prevent. `cache-1h-dispatched` goes further
still: it is about the whole FLEET's one-hour cache writes by 5m-armed Jarvis turns, and
the order under it carries only the foreign key. Jarvis must never write a person's own
`~/.claude/settings.json`; never propose a remedy that edits that file. An old
`cache-1h-foreign` row is historical (nothing raises it now) — ack it.
`cache-arm-missed` is fleet-wide too and is always an OS fault: the arm a turn was STAMPED
with is not the TTL its calls paid, so the stamp recorded intent only. Its cure is the OS
setting a variable that OUTRANKS the user-owned key for the same behaviour, never a change
to that file either.

READ THE PACKET'S "this session is" LINE BEFORE YOU JUDGE THE NUMBERS. The alarm is
always raised against a work order, but a work order is not always a worker: it may be a
FEATURE ORDER's planner, decomposing one ask by reading a whole codebase, or its manager,
which sits idle for the feature's entire life and wakes only on a message. What counts as
a long turn differs between the three, and the same figure is routine on one and a symptom
on another.

YOU DECIDE NOTHING ABOUT THE WORK ORDER. Nobody messages the worker, cancels the turn or
changes a status on the strength of your reply — the OS records your reading against the
alarm and stops. So do not tell anyone to intervene, and do not phrase your answer as
though someone would.

THAT REMAINS TRUE EVEN THOUGH THE OS CAN NOW ACT. The supervisor may propose one of a
closed, non-destructive set of remedies, and the ONLY thing that authorises one is a
separate `self_heal` gate request reviewed on its own terms. This reply is not that
request and cannot become it. If the packet says a remedy was proposed and refused, the
refusal already happened and the symptom is what is left for you to read; if it says one
was applied, say what that changes about the reading and nothing more.

ANSWER when the evidence accounts for the spend, or accounts for it well enough that a
person reading your words would not go and look. Your answer is what the user is shown
INSTEAD of an interruption, so it must stand alone: say what the order is doing and why
the number is what it is, in plain words, without the reader opening anything. That is
the whole value of this call — an answer costs the user nothing and an escalation costs
them a glance.

ESCALATE when the spend genuinely needs the user's judgement: a turn with no visible
progress at any point, a conversation re-sent again and again rather than once, a join
that has outlived any plausible subagent, or evidence so thin that any answer you gave
would be a guess dressed as a reading. Also escalate when what you can see suggests the
work order was briefed wrongly — that is a decision about the WORK, and only the user
takes those.

DO NOT ESCALATE MERELY BECAUSE MONEY WAS SPENT. The general answerer persona sends
anything touching production to the user; that instinct is wrong here. Every alarm is
about spend by construction, so applying it would send every alarm up and this whole path
would have bought nothing. The question is not "is this expensive" but "does this need
THEM".

You cannot look anything up: this call is headless and the packet below is all there is.
When it is not enough to answer, say so and escalate — that is an honest reading, not a
failure.

Output STRICT JSON, nothing else:
  {"escalate": false, "answer": "<what the user is told, plain words, stands alone>",
   "reason": "<one line: what you read it against, for the record>"}
  {"escalate": true,  "answer": "",
   "reason": "<one line: why this needs the user>"}
An escalation may carry one optional label, which is grouped and reported:
  "cause": "<on an escalation, name the cause from this list; omit it if none fits: \
evidence-insufficient | user-decision>\""""

#: User-facing copy: inbox rows reach every sink, Telegram included
ACK_INBOX_TITLE = "Supervisor cleared an alarm on {wo_id}"
ADVICE_INBOX_TITLE = "Neo cleared an alarm on {wo_id}"
ESCALATED_INBOX_TITLE = "A cost alarm needs you: {alarm_id}"

#: The alarm's own page — the anchor `/alarms` cannot be, since a list has no per-row identity
ALARM_PATH = "/alarms/{project}/{alarm_id}"

#: The attention reason an unsettled alarm carries
ALARM_BLOCKER = "cost alarm {alarm_id} needs you — `jarvis alarms show {alarm_id}`"

#: `verdict_reason` on an alarm Neo answered
NEO_ANSWERED_REASON = "escalated to Neo, which answered: {reason}"

#: `review` recognises the unreadable-output path by this prefix, as `neo` does
UNREADABLE_PREFIX = "unreadable supervisor output: "


def build_system_prompt(store: Any, project: str,
                        learnings_limit: int | None = None,
                        probes: Any = (), armed_remedies: Any = ()) -> str:
    """Persona + learnings + (optionally) a symptom checklist (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §4)"""
    from . import neo, probes as probes_mod, remedies as remedies_mod
    from .catalog import SupervisorConfig
    from .neo_store import SUPERVISOR_SEAT

    if learnings_limit is None:
        learnings_limit = SupervisorConfig().learnings_limit
    parts = [
        SUPERVISOR_PERSONA,
        "",
        "# Learnings (from the user's corrections of your past decisions)",
        *neo.render_learnings(
            store.learnings(project, limit=learnings_limit, seat=SUPERVISOR_SEAT)),
    ]
    if probes:
        parts += ["", probes_mod.render_checklist(probes)]
    if armed_remedies:
        parts += ["", remedies_mod.render_catalogue(tuple(armed_remedies))]
    return "\n".join(parts)


def learning_from_review(alarm: dict[str, Any], feedback: str) -> str:
    """Distil the user's correction of a verdict into what the next review is shown (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""
    subject = (f"a {alarm.get('probe')} health finding"
               if alarm.get("source") == "health" else f"a {alarm.get('kind')} alarm")
    decided = alarm.get("verdict")
    if decided == "propose":
        decided = (f"propose the {alarm.get('remedy')} remedy "
                   f"({alarm.get('remedy_argument')})")
    return (f"On {subject} ({alarm.get('reason')}) the supervisor "
            f"decided {decided} because {alarm.get('verdict_reason')}. "
            f"The user's ruling: {feedback}")


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + " […]"


def _what_it_is(wo: dict[str, Any]) -> str:
    """What KIND of session is burning, in the judge's words"""
    parent = wo.get("parent_id")
    kind = wo.get("kind") or "worker"
    belongs = f" of feature order {parent}" if parent else ""
    if kind == "analyst":
        # §3.1 of the improvement-orders spec: it reads records and writes no code
        owns = f" of improvement order {parent}" if parent else ""
        return (f"the ANALYST{owns} — one session reading OS "
                f"records through the CLI to diagnose a root cause, so it is expected "
                f"to be read-heavy; a long GENERATING stretch, or any sign of it "
                f"editing product code, is not")
    if kind == "investigator":
        # §2.2 of docs/superpowers/specs/2026-09-27-investigation-orders.md
        owns = f" of investigation {parent}" if parent else ""
        return (f"the INVESTIGATOR{owns} — one read-only session diagnosing one stuck "
                f"order through the CLI, so it is expected to be read-heavy and to end "
                f"in a verdict; a long GENERATING stretch, or a run of DENIED tool calls "
                f"it keeps retrying (its writes and mutating commands are refused by a "
                f"hook), is not")
    if kind == "researcher":
        # §3 of docs/superpowers/specs/2026-10-31-decision-orders.md
        owns = f" of decision order {parent}" if parent else ""
        return (f"the RESEARCHER{owns} — one read-and-measure session gathering data for "
                f"a decision the user takes, ending by submitting a report; long reads and "
                f"many commands are normal; a commit or a pull request is NOT")
    if kind == "planner":
        return (f"the PLANNER{belongs} — one session reading the codebase to decompose "
                f"a single ask into work orders, so it is expected to be long and "
                f"read-heavy")
    if kind == "manager":
        return (f"the MANAGER{belongs} — it sits idle for that feature's whole life and "
                f"wakes only on a message, so a long WALL clock is normal and long "
                f"GENERATING time is not")
    return "an ordinary work order" + (f", a child{belongs}" if parent else "")


def _session_lines(wo: dict[str, Any], inspect_cfg: Any,
                   pstore: Any = None) -> list[str]:
    """The per-turn split — `live_alarms`' own read, re-rendered"""
    from . import autopsy, holds, inspection, ops

    session_id = wo.get("session_id") or ""
    if not session_id:
        return ["(the work order has no session)"]
    spans = holds.held(pstore, wo["id"]) if pstore is not None else []
    # Spec 2026-09-27 §3: the OS's own turn numbers, degrading to 1..N without a store
    turn_starts = pstore.turn_starts(wo["id"]) if pstore is not None else []
    try:
        # Spec 2026-09-27 §4: through the chokepoint
        anatomy, provenance = autopsy.anatomy_for(
            wo, inspect_cfg, spans=spans, turn_starts=turn_starts,
            cold_prefix_floor=ops.cold_prefix_floor())
    except OSError:
        return ["(the session transcript could not be read)"]
    if not anatomy.found:
        return ["(no transcript found for this session)"]

    lines = []
    # ONLY when a seal answered (Neo q1080): the derived case is what this packet has always said
    if provenance["source"] == autopsy.SEALED:
        lines.append(f"- {provenance['note']} {provenance['level_note']}"
                     + (f" {provenance['floor_note']}" if provenance["floor_note"]
                        else ""))
    for turn in anatomy.turns:
        # THE COST IS AN INPUT, NOT AN INFERENCE FROM THE DURATION (issue 227)
        spend = turn.usage
        split = ", ".join(f"{getattr(turn, part):.0f}s {part}"
                          for part in inspection.PARTS)
        # AWAITING FIRST (spec of 2026-09-27 §1): THIS is the line that fed the judge
        if turn.awaiting:
            stalled = (f"{inspection.awaiting_note(turn)} — nothing has COMPLETED yet; "
                       "this is not a stall. ")
        elif turn.observed:
            stalled = ""
        else:
            stalled = "NO API CALL WAS EVER MADE — it cost nothing. "
        # Said BEFORE the split, like `stalled` above and for its reason: it changes what every number after...
        held_by = turn.held_by()
        for cause, seconds in held_by.items():
            stalled += (f"{seconds:.0f}s of this turn was HELD by "
                        f"{holds.HOLD_CAUSES.get(cause, cause)} — the OS was not "
                        f"permitting it to run and nothing was being spent. ")
        active = f" ({turn.active:.0f}s of it active)" if held_by else ""
        lines.append(
            f"- {inspection.turn_name(turn.seq)}: {stalled}"
            f"{turn.wall:.0f}s wall{active} ({split}), "
            f"{len(turn.calls)} API call{'' if len(turn.calls) == 1 else 's'} costing "
            f"{spend.total_tokens:,} tokens / ${spend.list_cost_usd:.2f}, "
            f"context peak {turn.context_peak:,}")
        for trigger in turn.triggers:
            lines.append(f"    started by [{trigger.kind}] {trigger.quote}")
    for join in anatomy.joins():
        lines.append(f"- blocked {join.seconds:.0f}s on {join.name}: {join.detail}")
    for write in anatomy.writes:
        # The cause label is what separates an explicable re-write from a defect
        lines.append(f"- cache write {write.written:,} tokens [{write.cause}]: "
                     f"{write.note}")
    return lines or ["(the session has no turns yet)"]


#: A child the plan has finished with
SETTLED_CHILD_STATUSES = ("completed", "cancelled")

#: The order the child tree is CLIPPED in, worst first
CHILD_TRIAGE = ("failed", "blocked", "unfinished", "settled")

#: A feature with no carrier has nothing on record that anyone said about it
NO_CARRIER_LINE = ("(no work order carries this feature, so nothing said about it is on "
                   "record)")


def _feature_lines(fo: dict[str, Any], carrier: dict[str, Any] | None,
                   cfg: Any) -> list[str]:
    """`updated_at` IS the status clock: `set_feature_status` goes through `update_feature_order`"""
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


#: A feature in these statuses has no approved plan yet
UNAPPROVED_PLAN_STATUSES = ("pending", "planning", "plan_review")


def _plan_lines(fo: dict[str, Any], budget: int) -> list[str]:
    """What the children answer to once plan review passed it, an equal share per line (wo-ad18c743)"""
    from . import db

    plan = db.from_json(fo.get("plan"), None)
    if not isinstance(plan, dict) or str(fo.get("status")) in UNAPPROVED_PLAN_STATUSES:
        return []
    body = [str(plan.get("summary") or "")]
    body += [f"- [{c.get('key')}] {c.get('title')}: {c.get('description') or ''}"
             for c in plan.get("children") or [] if isinstance(c, dict)]
    each = budget // len(body)
    return ["# The approved plan (settled against the brief at plan review)",
            *[_clip(line, each) for line in body if line]]


def _child_rank(child: dict[str, Any]) -> tuple[int, float]:
    """What survives the clip when the tree does not fit: what is wrong"""
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
    """The tree, bounded WITHIN its section"""
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
    """What the user already ruled on with `jarvis fo resume`"""
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


#: What a work-order packet still has to fit after `_history_lines`, for its equal share
WO_SECTIONS_AFTER_HISTORY = ("# The session, turn by turn",
                             "# What was last said about this order")


def _for(seconds: float) -> str:
    hours = seconds / SECONDS_PER_HOUR
    return f"{hours:.1f} hour(s)" if hours >= 1 else f"{seconds / SECONDS_PER_MINUTE:.0f} minute(s)"


def _history_lines(pstore: Any, kind: str, order_id: str, budget: int) -> list[str]:
    """The spans `ops.state_durations` reads, so the packet cannot be blind to a transition the OS recorded"""
    from . import db, ops

    now = db.now()
    reading = (ops.state_durations(pstore, wo_id=order_id, now=now) if kind == "wo"
               else ops.state_durations(pstore, fo_id=order_id, now=now))
    lines = ["# Where it has been"]
    if reading.current_status_since is not None:
        lines.append(f"now: {reading.current_status} for "
                     f"{_for(now - reading.current_status_since)}")
    elif reading.current_status:
        lines.append(f"now: {reading.current_status} (the recorded spans stop short of it)")
    else:
        lines.append("now: settled")
    # Newest first within an equal share of the budget, then printed oldest first
    kept: list[str] = []
    spent = 0
    for span in reversed(reading.spans):
        line = (f"- {span.status} for {_for(span.seconds(now))}"
                + (f" (entered by {span.trigger})" if span.trigger else "")
                + (" [approximate]" if span.approximate else ""))
        if kept and spent + len(line) > budget:
            kept.append(f"…and {len(reading.spans) - len(kept)} earlier status(es)")
            break
        spent += len(line) + len("\n")
        kept.append(line)
    lines.extend(reversed(kept))
    if kind == "wo":
        # The verdict the stillness may be waiting on, which no status name says
        rounds = pstore.validation_rounds(wo_id=order_id)
        settled = [r for r in rounds if r["outcome"] != "pending"]
        if rounds and rounds[-1]["outcome"] == "pending":
            lines.append(f"validation round {rounds[-1]['round']} is open, opened "
                         f"{_for(now - float(rounds[-1]['ts'] or now))} ago")
        if settled:
            last = settled[-1]
            lines.append(f"latest validation round {last['round']}: {last['outcome']}, "
                         f"opened {_for(now - float(last['ts'] or now))} ago")
        elif not rounds:
            lines.append("(it has never been through validation)")
    return lines


def _said_lines(pstore: Any, wo_id: str | None, cfg: Any) -> list[str]:
    """The last `cfg.quoted_turns` of `timeline.build_conversation`"""
    from . import timeline

    said = ["# What was last said about this order"]
    if wo_id is None:
        said.append(NO_CARRIER_LINE)
        return said
    # The NEWEST page: what was LAST said is the tail, which an ascending LIMIT drops
    conversation = timeline.build_conversation(
        pstore.list_events(wo_id, newest=True), pstore.list_messages(wo_id))
    for turn in conversation[-cfg.quoted_turns:]:
        said.append(f"- {turn['who']}: "
                    f"{_clip(str(turn.get('content') or ''), cfg.conversation_quote_chars)}")
    if len(said) == 1:
        said.append("(nothing has been said about it since it was dispatched)")
    return said


#: What `blocked: …` says for each `health.BLOCKERS` id, plus the feature answer
BLOCKED_SENTENCES = {
    "user": "it is waiting on a decision only the user can take",
    "dependency": "it is waiting on a dependency that has not completed",
    "pull-request": "its work is delivered and its pull request has not merged yet",
    **{blocker_id: f"the account is holding it — {holds.HOLD_CAUSES[cause]}"
       for blocker_id, cause in health.transport_blockers().items()},
    "assumptions": "an assumption of its own is still waiting to be reviewed",
    "children": "every child of it that has not settled is itself blocked",
}


def build_evidence(pstore: Any, subject: dict[str, Any],
                   alarm: dict[str, Any] | None,
                   cfg: Any = None, inspect_cfg: Any = None) -> str:
    """Everything the supervisor is shown, under `cfg.evidence_budget_chars`"""
    from . import db
    from .catalog import InspectConfig, SupervisorConfig
    from .project_store import NO_TURN

    cfg = cfg or SupervisorConfig()
    inspect_cfg = inspect_cfg or InspectConfig()

    sections: list[list[str]] = []
    if alarm is not None:
        standing = max(0.0, db.now() - float(alarm.get("ts") or 0.0))
        minutes = standing / SECONDS_PER_MINUTE
        # A subject-level finding was raised on no turn
        seq = alarm.get("seq")
        raised = ("raised on no particular turn" if seq == NO_TURN
                  else f"raised on turn {seq}")
        sections.append([
            "# The alarm",
            f"kind: {alarm.get('kind')}",
            f"reason: {alarm.get('reason')}",
            f"{raised}, {minutes:.0f} minute(s) ago",
        ])

    row = subject["row"]
    if subject["kind"] == "feature_order":
        # No `_session_lines` here: a feature has no session
        fo_id = row["id"]
        carrier = pstore.carrier_for_feature(fo_id)
        rest = [_feature_lines(row, carrier, cfg),
                _answered_lines(pstore, fo_id),
                _validation_lines(pstore, fo_id, cfg),
                _said_lines(pstore, carrier["id"] if carrier else None, cfg)]
        # An equal share of the budget, so the tree's bound moves with the setting and this module goes on...
        share = cfg.evidence_budget_chars // (len(sections) + len(rest) + 1)
        tree = ["# The child tree",
                *_child_lines(pstore.feature_children(fo_id, kinds=WORKER_CHILDREN), share)]
        history = _history_lines(pstore, "fo", fo_id, share)
        plan = _plan_lines(row, share)
        sections += [rest[0], *([plan] if plan else []), history, tree, *rest[1:]]
    else:
        from . import health, invariants

        # ABSENT when there is no blocker
        blocked = health.blocker(pstore, subject, cfg)
        # ABSENT with no actor for the reason just above
        actor = health.actor(pstore, subject)
        # A quota or API pause has an owner and a clock: quiet, not wedged (#247)
        paused = invariants.pause_state(pstore, row)
        sections.append([
            "# The work order",
            f"{row.get('id')} [{row.get('status')}] on "
            f"{row.get('model') or '(default model)'}",
            f"this session is {_what_it_is(row)}",
            *([f"scheduled_actor: {actor.pass_name} - {actor.why}"] if actor else []),
            *([f"blocked: {BLOCKED_SENTENCES.get(blocked, blocked)}"] if blocked else []),
            *([f"paused ({paused['reason']}): {paused['note']} — the OS resumes "
               "it by itself; this is not a stall"] if paused and paused["note"] else []),
            f"title: {row.get('title')}",
            f"brief: {_clip(str(row.get('description') or ''), cfg.description_chars)}",
        ])
        # Ahead of the transcript: a capped packet drops the session read before the status history
        sections.append(_history_lines(
            pstore, "wo", row["id"], cfg.evidence_budget_chars
            // (len(sections) + 1 + len(WO_SECTIONS_AFTER_HISTORY))))
        sections.append(["# The session, turn by turn",
                         *_session_lines(row, inspect_cfg, pstore)])
        sections.append(_said_lines(pstore, row["id"], cfg))

    # Whole sections, never a mid-line cut: half a cache-write line reads as a fact
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
    """Normalise a parsed reply into the verdict dict, or raise"""
    from . import remedies

    decision = str(data.get("decision") or "").strip().lower()
    if not decision:
        raise structured.InvalidOutput("no `decision` field in the supervisor's reply")
    if decision not in ("ack", "escalate", "propose"):
        raise structured.InvalidOutput(
            f"`decision` must be 'ack', 'escalate' or 'propose', got {decision!r}")
    remedy = str(data.get("remedy") or "").strip()
    if decision == "propose":
        if not remedy:
            raise structured.InvalidOutput(
                "`decision` is 'propose' with no `remedy` — name one of "
                f"{list(remedies.SHIPPED_REMEDIES)}")
        if remedy not in remedies.REMEDIES:
            raise structured.InvalidOutput(
                f"no such remedy {remedy!r} — the OS has "
                f"{list(remedies.SHIPPED_REMEDIES)}")
    return {
        "decision": decision,
        "reason": str(data.get("reason") or ""),
        "note": (str(data.get("note") or "")[:note_chars]
                 if decision in ("ack", "propose") else ""),
        "question": str(data.get("question") or "") if decision == "escalate" else "",
        "remedy": remedy if decision == "propose" else "",
        "argument": str(data.get("argument") or "") if decision == "propose" else "",
        "failed": False,
    }


def _failed_verdict(raw: str, reason_chars: int) -> dict[str, Any]:
    """The fail-safe, and IT ESCALATES: a failure must never become an ack"""
    return {"decision": "escalate", "note": "", "question": "", "remedy": "",
            "argument": "", "failed": True,
            "reason": f"{UNREADABLE_PREFIX}{(raw or '')[:reason_chars]}"}


#: `verdict_reason` on an alarm nobody could put to a model
UNREACHABLE_PREFIX = "the supervisor could not be reached: "


def _transport_failure(exc: Exception, reason_chars: int) -> dict[str, Any]:
    """A call that never happened, which `structured.request`'s `on_invalid` does NOT cover (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §4.)"""
    return {"decision": "", "note": "", "question": "", "remedy": "",
            "argument": "", "failed": True, "unreachable": True,
            "reason": f"{UNREACHABLE_PREFIX}{str(exc)[:reason_chars]}"}


def review(pstore: Any, neo_store: Any, project: str, wo: dict[str, Any],
           alarm: dict[str, Any], cfg: Any, record: Any = None,
           central: Any = None, inspect_cfg: Any = None) -> dict[str, Any]:
    """Judge one claimed alarm and record the outcome"""
    from . import agent_usage
    from .central_store import CentralStore
    from .paths import ensure_home

    record = record or agent_usage.record
    # Hoisted out of the call because the ESCALATION carries it: Neo answers from the question and its...
    evidence = build_evidence(pstore, {"kind": "work_order", "row": wo}, alarm,
                              cfg, inspect_cfg)
    try:
        verdict = structured.request(
            evidence,
            validate=lambda data: _validate(data, cfg.note_chars),
            system_prompt=build_system_prompt(neo_store, project, cfg.learnings_limit,
                                              armed_remedies=cfg.remedies.allowed
                                              if cfg.remedies.enabled else ()),
            model=cfg.model,
            # Neo's FAIL-SAFE shape, not the panel chair's retry shape: asking again spends a call to learn the...
            attempts=1,
            on_invalid=lambda raw: _failed_verdict(raw, cfg.reason_chars),
            timeout=cfg.timeout,
            # Neutral cwd, Neo's reason: a project directory would pull its CLAUDE.md in and break prefix...
            cwd=ensure_home(),
            # This call is recorded by `on_usage`, below, under the same kind
            records_itself="supervisor",
            on_usage=agent_usage.recorder(
                "supervisor", project=project, wo_id=wo["id"],
                label=str(alarm.get("kind") or ""), model=cfg.model, record=record),
        )
    except claude_cli.ClaudeCliError as exc:
        verdict = _transport_failure(exc, cfg.reason_chars)

    own_central = central is None
    central = central or CentralStore()
    try:
        _apply(pstore, neo_store, central, project, wo, alarm, verdict, evidence,
               cfg)
    finally:
        if own_central:
            central.close()
    return verdict


# -- the health sweep

#: What the judge is told about WHY it is being asked now
HEALTH_WHY = {
    "first-look": "This unit has never been checked before. Judge it as it stands.",
    "changed": "Its record has moved since the last check.",
    "stale": "Its record has NOT moved at all since the last check, and that stillness "
             "is why you are being asked now.",
}

#: The sweep's user prompt opens with this, above the evidence packet
HEALTH_HEADER = "# Why you are looking now"

#: The attention reason a finding puts on the CARRIER
HEALTH_BLOCKER = "{reason}"
HEALTH_FEATURE_BLOCKER = "{fo_id}: {reason}"

#: `health_reviews.detail` recognises the unreadable-output path by this prefix
UNREADABLE_SWEEP_PREFIX = "unreadable health sweep output: "

NO_CARRIER_DETAIL = ("no work order carries this feature, so a finding about it has "
                     "nowhere to be recorded")


def _validate_findings(data: dict[str, Any], armed_ids: set[str],
                       cfg: Any) -> dict[str, Any]:
    """Normalise a parsed sweep reply, or raise"""
    if "findings" not in data:
        raise structured.InvalidOutput("no `findings` field in the sweep's reply")
    findings = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        probe = str(item.get("probe") or "").strip()
        if probe not in armed_ids:
            log.warning("health sweep named a probe that is not armed: %r", probe)
            continue
        findings.append({"probe": probe,
                         "reason": _clip(str(item.get("reason") or ""),
                                         cfg.reason_chars),
                         "evidence": _clip(str(item.get("evidence") or ""),
                                           cfg.reason_chars)})
    return {"findings": findings, "failed": False, "reason": ""}


def _nothing_found(reason: str) -> dict[str, Any]:
    """THE FAIL-SAFE, AND ITS POLARITY IS THE OPPOSITE OF `_failed_verdict`'S"""
    return {"findings": [], "failed": True, "reason": reason}


def review_health(pstore: Any, neo_store: Any, project: str, subject: dict[str, Any],
                  probes: Any, cfg: Any, trigger: str, record: Any = None,
                  inspect_cfg: Any = None) -> dict[str, Any]:
    """Judge one open unit against the project's armed probes; raise what is wrong"""
    from . import agent_usage, health, probes as probes_mod
    from .paths import ensure_home
    from .project_store import NO_TURN

    record = record or agent_usage.record
    kind, row = subject["kind"], subject["row"]
    subject_id = row["id"]
    fingerprint = health.fingerprint(pstore, subject)
    carrier = (pstore.carrier_for_feature(subject_id) if kind == "feature_order"
               else row)
    if carrier is None:
        # Refused BEFORE the call, not after: a finding with nowhere to be recorded is a model call bought...
        pstore.record_health_review(kind, subject_id, fingerprint=fingerprint,
                                    trigger=trigger, outcome="failed",
                                    detail=NO_CARRIER_DETAIL)
        return {**_nothing_found(NO_CARRIER_DETAIL), "raised": [], "outcome": "failed"}

    armed = probes_mod.armed(probes, kind)
    armed_ids = {p.id for p in armed}
    packet = "\n\n".join([
        "\n".join([HEALTH_HEADER, HEALTH_WHY.get(trigger, trigger)]),
        build_evidence(pstore, subject, None, cfg, inspect_cfg),
    ])
    try:
        reply = structured.request(
            packet,
            validate=lambda data: _validate_findings(data, armed_ids, cfg),
            system_prompt=build_system_prompt(neo_store, project, cfg.learnings_limit,
                                              probes=armed),
            model=cfg.model,
            # The FAIL-SAFE shape, as `review` uses it: asking again spends a call to learn the same thing
            attempts=1,
            on_invalid=lambda raw: _nothing_found(
                f"{UNREADABLE_SWEEP_PREFIX}{_clip(raw, cfg.reason_chars)}"),
            timeout=cfg.timeout,
            # Neutral cwd, `review`'s reason: a project directory pulls its CLAUDE.md in and breaks prefix...
            cwd=ensure_home(),
            # This call is recorded by `on_usage`, below, under the same kind
            records_itself="health",
            on_usage=agent_usage.recorder(
                "health", project=project, wo_id=carrier["id"], label=trigger,
                model=cfg.model, record=record),
        )
    except claude_cli.UsageLimitError as exc:
        # BEFORE the generic outage below (#235)
        from .worker_session import RATE_LIMIT_FALLBACK_DELAY

        reopens = exc.limit.reset_at or (db.now() + RATE_LIMIT_FALLBACK_DELAY)
        pstore.record_health_review(kind, subject_id, fingerprint=fingerprint,
                                    trigger=trigger, outcome="held",
                                    detail=_clip(exc.limit.message, cfg.reason_chars),
                                    reopens_at=reopens)
        log.info("[%s] health sweep of %s held until the usage window reopens",
                 project, subject_id)
        return {**_nothing_found(exc.limit.message), "raised": [], "outcome": "held"}
    except claude_cli.ClaudeCliError as exc:
        # `on_invalid` does NOT cover this: `ClaudeCliError` propagates untouched by design (kn-9b18a8eb)
        reply = _nothing_found(f"the health sweep could not be reached: "
                               f"{_clip(str(exc), cfg.reason_chars)}")

    if reply["failed"]:
        # THE FINGERPRINT IS NOT RECORDED AS REVIEWED
        pstore.record_health_review(kind, subject_id, fingerprint=fingerprint,
                                    trigger=trigger, outcome="failed",
                                    detail=reply["reason"])
        log.warning("[%s] health sweep of %s failed: %s", project, subject_id,
                    reply["reason"])
        return {**reply, "raised": [], "outcome": "failed"}

    # READ BEFORE THIS SWEEP'S OWN ROW IS WRITTEN, or it deduplicates its own findings
    already = pstore.probes_reported_at(kind, subject_id, fingerprint)
    raised = []
    for finding in reply["findings"]:
        if finding["probe"] in already:
            continue
        alarm = pstore.add_finding(
            carrier["id"], kind=finding["probe"], reason=finding["reason"],
            seq=NO_TURN, source="health", probe=finding["probe"], subject_kind=kind,
            fo_id=subject_id if kind == "feature_order" else None)
        pstore.add_event(carrier["id"], "health_finding",
                         {"alarm_id": alarm["id"], "probe": finding["probe"],
                          "subject_kind": kind, "subject_id": subject_id,
                          "reason": finding["reason"]})
        raised.append(alarm["id"])
        log.info("[%s] health finding on %s: %s", project, subject_id,
                 finding["reason"])

    outcome = "findings" if reply["findings"] else "clear"
    pstore.record_health_review(
        kind, subject_id, fingerprint=fingerprint, trigger=trigger, outcome=outcome,
        findings=len(reply["findings"]),
        # What the sweep REPORTED, not what it raised: this is the dedupe memory
        detail=",".join(f["probe"] for f in reply["findings"]))
    pstore.add_event(carrier["id"], "health_reviewed",
                     {"subject_kind": kind, "subject_id": subject_id,
                      "trigger": trigger, "findings": len(reply["findings"])})
    if raised and not carrier.get("needs_attention"):
        first = reply["findings"][0]
        pstore.flag_attention(carrier["id"], (
            HEALTH_FEATURE_BLOCKER.format(fo_id=subject_id, reason=first["reason"])
            if kind == "feature_order"
            else HEALTH_BLOCKER.format(reason=first["reason"])))
    return {**reply, "raised": raised, "outcome": outcome}


def reassert_health(pstore: Any, project: str, subject: dict[str, Any]) -> dict[str, Any]:
    """Carry the prior judgement forward at an unchanged fingerprint — ONE ROW, NO CALL"""
    from . import health

    kind, subject_id = subject["kind"], subject["row"]["id"]
    prior = pstore.last_health_review(kind, subject_id)
    if prior is None:
        # Unreachable through `due`, which only answers `re-assert` with a prior `findings` row
        log.warning("[%s] nothing to re-assert about %s", project, subject_id)
        return {"outcome": None, "trigger": health.REASSERT}
    pstore.record_health_review(
        kind, subject_id, fingerprint=health.fingerprint(pstore, subject),
        trigger=health.REASSERT, outcome=prior["outcome"],
        findings=prior["findings"], detail=prior["detail"])
    log.debug("[%s] re-asserted the last judgement of %s for free", project, subject_id)
    return {"outcome": prior["outcome"], "trigger": health.REASSERT}


def escalation_context(evidence: str, verdict: dict[str, Any]) -> str:
    """What Neo is handed with the question"""
    # Decision #11: packet and model reasoning are both borrowed text, both bounded
    from .provenance import (DESCRIPTION_LIMIT, EVIDENCE_LIMIT, Borrowed,
                             borrowed_block)

    return "\n\n".join([
        borrowed_block(Borrowed(
            label="the alarm's evidence packet",
            whose="the OS's detectors, quoting the worker's own session",
            text=evidence, limit=EVIDENCE_LIMIT)),
        "\n".join([
            "# What the supervisor made of it",
            "It could not settle this alarm and handed it to you.",
            "Its reasoning:",
            borrowed_block(Borrowed(label="the supervisor's reasoning",
                                    whose="the supervisor model",
                                    text=verdict["reason"],
                                    limit=DESCRIPTION_LIMIT)),
        ]),
    ])


def _question_for(verdict: dict[str, Any], alarm: dict[str, Any]) -> str:
    """The one question put to Neo, never empty: a model that escalates without filling `question` in has..."""
    # Decision #11: the model's question and the alarm's reason are borrowed text, bounded
    from .provenance import DESCRIPTION_LIMIT, Borrowed, borrowed_block

    asked = verdict["question"].strip()
    if asked:
        return borrowed_block(Borrowed(label="the supervisor's question",
                                       whose="the supervisor model", text=asked,
                                       limit=DESCRIPTION_LIMIT))
    return "\n".join([
        "The supervisor could not settle this alarm:",
        borrowed_block(Borrowed(label="the alarm's reason",
                                whose="the OS detector that raised it",
                                text=alarm["reason"], limit=DESCRIPTION_LIMIT)),
        "Does this spend need the user?",
    ])


def _apply(pstore: Any, neo_store: Any, central: Any, project: str, wo: dict[str, Any],
           alarm: dict[str, Any], verdict: dict[str, Any], evidence: str,
           cfg: Any) -> None:
    """Write the verdict down"""
    from . import db, ops

    alarm_id = alarm["id"]
    decided = db.now()
    if verdict.get("unreachable"):
        # NOT `failed`: nobody judged this
        outcome = pstore.release_alarm_claim(alarm_id, verdict["reason"],
                                             cfg.max_review_attempts)
        log.warning("supervisor could not be reached for %s (%s): %s",
                    alarm_id, outcome, verdict["reason"])
        return
    if verdict["failed"]:
        pstore.update_alarm(alarm_id, status="failed",
                            verdict_reason=verdict["reason"], decided_at=decided)
        log.warning("supervisor review of %s failed: %s", alarm_id, verdict["reason"])
        return

    if verdict["decision"] == "escalate":
        _escalate(pstore, neo_store, central, project, wo, alarm, verdict, evidence,
                  decided)
        return

    if verdict["decision"] == "propose":
        _propose(pstore, neo_store, central, project, wo, alarm, verdict, evidence, cfg)
        return

    pstore.update_alarm(alarm_id, status="acked", verdict="ack",
                        verdict_reason=verdict["reason"], note=verdict["note"],
                        decided_at=decided)
    pstore.add_event(wo["id"], "alarm_reviewed",
                     {"alarm_id": alarm_id, "verdict": "ack",
                      "reason": verdict["reason"], "note": verdict["note"]})

    # THROUGH `ops.ack_os_flag`, which takes down the flag THIS alarm raised and leaves every other...
    try:
        ops.ack_os_flag(wo["id"])
    except ops.OpsError as exc:
        log.info("alarm %s acked; attention left up: %s", alarm_id, exc)

    central.add_inbox(project=project, level="info",
                      title=ACK_INBOX_TITLE.format(wo_id=wo["id"]),
                      body=verdict["note"], wo_id=wo["id"])


def _propose(pstore: Any, neo_store: Any, central: Any, project: str,
             wo: dict[str, Any], alarm: dict[str, Any], verdict: dict[str, Any],
             evidence: str, cfg: Any) -> None:
    """Hand the named remedy to `remedies.propose`, which owns every write — §5"""
    from . import remedies

    outcome = remedies.propose(
        pstore, neo_store, project, wo, alarm, verdict["remedy"], verdict["argument"],
        cfg.remedies, evidence=evidence, reason=verdict["reason"],
        note=verdict["note"])
    if outcome["proposed"]:
        log.info("alarm %s proposes %s (gate request %s)", alarm["id"],
                 verdict["remedy"], outcome["approval"]["id"])
        return
    _flag_the_user(pstore, central, project, wo, alarm, verdict["reason"],
                   why=f"the `{verdict['remedy']}` remedy it wanted was refused — "
                       f"{outcome['reason']}")


def _escalate(pstore: Any, neo_store: Any, central: Any, project: str,
              wo: dict[str, Any], alarm: dict[str, Any], verdict: dict[str, Any],
              evidence: str, decided: float) -> None:
    """Hand the alarm to Neo — §3"""
    from .project_store import OPEN_STATUSES

    alarm_id = alarm["id"]
    fields = {"status": "escalated", "verdict": "escalate",
              "verdict_reason": verdict["reason"], "decided_at": decided}
    reviewed = {"alarm_id": alarm_id, "verdict": "escalate",
                "reason": verdict["reason"], "note": ""}

    # NOTHING TO ASK ABOUT
    if wo["status"] not in OPEN_STATUSES:
        pstore.update_alarm(alarm_id, **fields)
        pstore.add_event(wo["id"], "alarm_reviewed", reviewed)
        _flag_the_user(pstore, central, project, wo, alarm, verdict["reason"],
                       why=f"the work order is {wo['status']} — Neo has nothing to "
                           f"advise about a session that has stopped")
        return

    question = neo_store.ask(project, wo["id"], _question_for(verdict, alarm),
                             context=escalation_context(evidence, verdict),
                             kind="alarm")
    pstore.update_alarm(alarm_id, neo_question_id=question["id"], **fields)
    pstore.add_event(wo["id"], "alarm_reviewed", reviewed)
    pstore.add_event(wo["id"], "alarm_escalated",
                     {"alarm_id": alarm_id, "neo_question_id": question["id"]})
    log.info("alarm %s escalated to neo as question %s", alarm_id, question["id"])


def _flag_the_user(pstore: Any, central: Any, project: str, wo: dict[str, Any],
                   alarm: dict[str, Any], reason: str, why: str) -> None:
    """Put an unsettled alarm in front of the user without going through Neo"""
    alarm_id = alarm["id"]
    pstore.flag_attention(wo["id"], ALARM_BLOCKER.format(alarm_id=alarm_id))
    central.add_inbox(project=project, level="warning",
                      title=ESCALATED_INBOX_TITLE.format(alarm_id=alarm_id),
                      body=f"{alarm['reason']}\n"
                           f"The supervisor could not settle it: {reason}\n"
                           f"It was not put to Neo: {why}.\n"
                           f"Read it with: jarvis alarms show {alarm_id}",
                      wo_id=wo["id"])
