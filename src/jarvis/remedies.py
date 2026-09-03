"""The only module in the OS that acts on a work order it was not asked to touch.

docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §5. `supervisor.py`
judges and names an id from the registry below; nothing there reaches a session, and an
AST walk over that file pins it. This module is the other half, under a different
authority, and four independent things refuse — any ONE of them is enough:

1. the registry here is CLOSED. A remedy not in `REMEDIES` does not exist, and
   `tuple(REMEDIES) == SHIPPED_REMEDIES` is asserted, so adding one is a reviewed diff
   rather than a prompt edit;
2. `catalog.RemedyConfig` — off, with an empty allow-list, on every project as shipped;
3. a `self_heal` gate grant that is approved, unexpired and unspent, consumed through
   `gates.open_gate`;
4. every acting call lives inside a handler, pinned by an AST walk in
   `tests/test_remedies.py` that keys on the enclosing function's name.

Excluded on purpose, and this is the boundary rather than an oversight: cancelling a
turn, `set_status`, `wo done`, `fo resume`, killing a process. Each destroys work that
has no other record, and each needs the proposal loop to have earned trust first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("remedies")

#: The gate a remedy rides. Not a command gate: nothing here is ever run by a shell.
GATE_KIND = "self_heal"

#: `timeline._message_label`'s contract with §6, written as a literal because neither
#: section can see the other's text. `source="jarvis"` falls into the label function's
#: default arm and renders the supervisor's nudge in the conversation AS THE USER
#: SPEAKING, which is a lie about who decided.
MESSAGE_SOURCE = "supervisor"

#: A grant covers ONE application. Not a threshold — the shape of the permission: the
#: user authorised an act, not a budget of them, and `apply` is not a retry loop.
GRANT_USES = 1

#: What `approvals.command` holds for a `self_heal` row. IT IS NOT A COMMAND and nothing
#: will execute it; the column is reused because every gate surface — `jarvis gate
#: show`, `/gates`, the request Neo reads — renders it as the thing being authorised.
#: `gates.approved_message`'s "run this command again" wording never reaches anyone here
#: because `apply_decision` queues no message for this kind.
INTENT = "heal {alarm_id}: {remedy} {subject_id} — {argument}"

#: The one message a nudge sends. Short on purpose: delivering it re-sends the worker's
#: whole conversation at the cache-write rate, which is the very cost the `big-rewrite`
#: alarm exists to report.
NUDGE = """[supervisor] The OS flagged this order as possibly unhealthy and was given \
permission to ask you about it: {reason}

{argument}

Reply on the record with where you are: what you are working on right now, what you are
waiting on, and whether you are stuck. This is a question, not an instruction — do not
change course on the strength of it."""

#: `verdict_reason` on an alarm whose proposal the reviewer refused. Names the verdict
#: and the reason, because the row is read on `/alarms` beside alarms the supervisor
#: settled alone and "escalated" alone does not say that an action was asked for.
REFUSED_REASON = "the {remedy} remedy was {verdict} by {by}: {reason}"

#: Inbox titles. User-facing copy, so they live here rather than at their call sites —
#: every inbox row reaches every sink, Telegram included. There is deliberately NO row
#: for a proposal being FILED: telling the user before the reviewer has even looked is
#: the attention cost this gate exists to avoid.
APPROVED_INBOX_TITLE = "{by} approved the {remedy} remedy on {alarm_id}"
APPLIED_INBOX_TITLE = "The supervisor acted on {subject_id}"
REFUSED_INBOX_TITLE = "A remedy was refused, and {alarm_id} still needs you"


class RemedyRefused(Exception):
    """Nothing was done. Raised by `apply` for every reason a remedy must not run, so a
    caller cannot mistake a refusal for a no-op that succeeded."""


@dataclass(frozen=True)
class Remedy:
    """One thing the OS may do, and everything a reviewer needs to rule on it.

    `headline` and `blast` are the whole reason this is a dataclass rather than a
    function: they go verbatim into the gate request and into the supervisor's own
    system prompt, so the words a reviewer reads and the words the judge was shown are
    the same words, and neither can drift from the code that runs.
    """

    id: str
    headline: str          # what it does, in the terms a reviewer needs
    blast: str             # what it touches, and what it cannot undo
    subjects: tuple[str, ...]
    apply: Callable[..., str]


def subject_kind(alarm: dict[str, Any]) -> str:
    """`work_order` or `feature_order` for one alarm row.

    Defaulted rather than required: a row raised before §1 widened `wo_alarms` carries no
    column and is always a work order.
    """
    return str(alarm.get("subject_kind") or "work_order")


def _carrier_id(pstore: Any, alarm: dict[str, Any]) -> str | None:
    """The work order a message about this alarm's subject is delivered to, or None.

    A feature order has no session and no timeline of its own — `wo_events.wo_id` is a
    real foreign key — so §1's `carrier_for_feature` resolves the work order that
    carries it. `None` means the feature has never been planned and there is nothing to
    speak to.
    """
    if subject_kind(alarm) == "work_order":
        return str(alarm["wo_id"])
    carrier = pstore.carrier_for_feature(alarm.get("fo_id"))
    return str(carrier["id"]) if carrier else None


# -- the handlers. EVERY ACTING CALL IN THIS MODULE IS INSIDE ONE OF THESE ------------
#
# `tests/test_remedies.py::test_the_acting_calls_stay_inside_the_handlers` walks this
# file's AST for `send_message`, `queue_message`, `unblock_work_order`, `cancel`,
# `cancel_work_order` and `set_status` as an attribute or a bare name, and requires the
# nearest enclosing function to be one of `REMEDIES[*].apply`. Reachability is not
# decidable from an AST; enclosure is, and it is the property that actually matters.


def _apply_nudge(pstore: Any, central: Any, project: str, subject: dict[str, Any],
                 alarm: dict[str, Any]) -> str:
    """Ask the session where it is. One message, and nothing else.

    NOT `ops.send_message`, and this is not a style choice: that function ends with
    `clear_attention`, which sets `acknowledged_blockers` to NULL and so discards the
    user's own earlier dismissals — before `apply` ever reaches `ops.ack_attention`.
    `ops.nudge_pr_conflict` is the precedent for an OS-authored message and does the two
    right things; the event half of it is written by `apply`, once, for every remedy.
    """
    wo_id = _carrier_id(pstore, alarm)
    if wo_id is None:
        raise RemedyRefused(
            f"{alarm.get('fo_id')} has no session to speak to — nothing was sent")
    pstore.queue_message(
        wo_id,
        NUDGE.format(reason=alarm.get("reason") or "",
                     argument=(alarm.get("remedy_argument") or "").strip()),
        source=MESSAGE_SOURCE,
    )
    return (f"queued one message on {wo_id} asking it to say where it is "
            f"(delivered on its next turn)")


def _apply_unblock(pstore: Any, central: Any, project: str, subject: dict[str, Any],
                   alarm: dict[str, Any]) -> str:
    """Cut the dependency edges that can never clear — the DEFAULT mode, never `--all`.

    A dependency still working is doing exactly what the edge was drawn for, and
    releasing the dependent early hands it a worktree without the code it was told to
    build on. So `drop_all` stays out of reach of this path entirely.

    Note the interaction the reader will want to know about: `unblock_work_order` calls
    `clear_attention` when nothing is left holding the order back. That is its own
    settled behaviour for a user running `jarvis wo unblock` and is not widened here.
    """
    from . import ops

    try:
        result = ops.unblock_work_order(subject["id"], project_name=project)
    except ops.OpsError as exc:
        # A refusal, not a failure: `unblock_work_order` declines when the order is not
        # blocked or is waiting on live work, and in both cases nothing was done.
        raise RemedyRefused(str(exc)) from exc
    dropped = result["dropped"]
    remaining = result["still_blocked_by"]
    return (f"cut {len(dropped)} dead dependency edge(s) on {subject['id']} "
            f"({', '.join(dropped) or 'none'}); "
            f"still blocked by: {', '.join(remaining) or 'nothing'}")


REMEDIES: dict[str, Remedy] = {
    "nudge": Remedy(
        id="nudge",
        headline="ask the session to say where it is, in one short message",
        blast="reaches a RUNNING session and costs one delivered turn, which re-sends "
              "the whole conversation at the cache-write rate. It changes no state and "
              "gives no instruction, but a message cannot be unsent and the spend "
              "cannot be undone.",
        subjects=("work_order", "feature_order"),
        apply=_apply_nudge,
    ),
    "unblock": Remedy(
        id="unblock",
        headline="cut the dependency edges that can never clear, so a stranded work "
                 "order can be dispatched",
        blast="touches only edges whose dependency was cancelled, failed or deleted — "
              "never a live one. The order becomes ordinary `pending` and will be "
              "dispatched without the work it was told to build on, which is the point "
              "and is also what cannot be taken back once it runs.",
        subjects=("work_order",),
        apply=_apply_unblock,
    ),
}

#: Asserted equal to `tuple(REMEDIES)`. The registry is closed BY A TEST rather than by
#: a convention, so widening what the OS may do fails a suite and is read by a human.
SHIPPED_REMEDIES: tuple[str, ...] = ("nudge", "unblock")


def get(remedy_id: str) -> Remedy:
    """The remedy, or `KeyError`. There is no free-text action and no "other"."""
    return REMEDIES[remedy_id]


def render_catalogue(allowed: tuple[str, ...]) -> str:
    """The armed remedies, as the judge and the reviewer are both shown them.

    One renderer for both readers on purpose: a model told it may act and shown a
    different list from the one the code enforces asks for things that are refused, and
    a reviewer shown a different `blast` line rules on an action nobody proposed.
    """
    lines = ["# Remedies you may propose"]
    armed = [REMEDIES[r] for r in allowed if r in REMEDIES]
    if not armed:
        lines.append("NONE are armed for this project. You may not use the `propose` "
                     "decision at all here — `ack` or `escalate`.")
        return "\n".join(lines)
    for remedy in armed:
        lines += [
            f"- `{remedy.id}` (subjects: {', '.join(remedy.subjects)}) — "
            f"{remedy.headline}",
            f"  What it costs and what it cannot undo: {remedy.blast}",
        ]
    return "\n".join(lines)


# -- proposing -------------------------------------------------------------------------


def _refusal(pstore: Any, alarm: dict[str, Any], remedy_id: str,
             cfg: Any) -> str | None:
    """Why this proposal may not be filed, or None. Reads only; writes nothing.

    Ordered cheapest-first, and the allow-list comes BEFORE anything that would reach the
    user: they must never be asked to approve something their own catalog forbids.
    """
    if not getattr(cfg, "enabled", False):
        return ("`supervisor.remedies.enabled` is false for this project — the "
                "supervisor may judge but may not propose")
    remedy = REMEDIES.get(remedy_id)
    if remedy is None:
        return f"{remedy_id!r} is not a remedy this OS has"
    if remedy_id not in tuple(getattr(cfg, "allowed", ())):
        return (f"`{remedy_id}` is not in this project's "
                f"`supervisor.remedies.allowed`")
    kind = subject_kind(alarm)
    if kind not in remedy.subjects:
        return (f"`{remedy_id}` does not apply to a {kind} "
                f"(it applies to: {', '.join(remedy.subjects)})")
    existing = alarm.get("remedy_approval_id")
    if existing:
        approval = pstore.get_approval(int(existing))
        if approval is not None and approval["status"] == "pending":
            return (f"a remedy for this alarm is already awaiting a verdict "
                    f"(gate request {approval['id']})")
    return None


def _request_question(project: str, subject_id: str, remedy: Remedy, argument: str,
                      evidence: str, reason: str) -> str:
    """What the reviewer reads. The alarm's own evidence packet, then the supervisor's
    reading, then the remedy in words.

    All three, because the reviewer is being asked to rule on the ACTION and not merely
    on the symptom: an approval here permits something to reach a running session, and
    a request that showed only "this turn looks stuck" would be answered on a different
    question from the one it is asking.
    """
    return "\n\n".join([
        f"SELF-HEAL REQUEST — gate `{GATE_KIND}`",
        f"The supervisor judged {subject_id} in {project} unhealthy and wants to apply "
        f"the `{remedy.id}` remedy. Nothing has been done; this authorises it.",
        f"# The remedy\n"
        f"What it does: {remedy.headline}\n"
        f"What it touches, and what it cannot undo: {remedy.blast}\n"
        f"What the supervisor would say or do: {argument.strip() or '(nothing given)'}",
        f"# Why the supervisor wants it\n{reason.strip() or '(no reason given)'}",
        evidence.strip() or "(the evidence packet was empty)",
        "Approve it to let the OS act, or deny it with a reason. Denying leaves the "
        "alarm open and with the user, which is the safe answer whenever the case for "
        "acting is not made.",
    ])


def propose(pstore: Any, neo: Any, project: str, subject: dict[str, Any],
            alarm: dict[str, Any], remedy_id: str, argument: str, cfg: Any,
            *, evidence: str = "", reason: str = "", note: str = "") -> dict[str, Any]:
    """File a `self_heal` gate request for one remedy, or refuse and say why.

    Returns `{"proposed": bool, "reason": str, "approval": …|None, "question": …|None}`.

    NOT `gates.file_request`, and each of the three differences is a defect if inherited:

    * that function moves a `running`/`dispatching` work order to `waiting_input`. A
      worker that asked for a gate has nothing to do until it is answered; here the
      worker did not ask and is very likely mid-turn, and `waiting_input` is read as
      "the worker is waiting on YOUR input" by `jarvis status`, the dashboard and
      `invariants.true_blockers` — the forty-minute defect the long comment at the end
      of `gates.apply_decision` was written about;
    * `approvals.command` would hold a lie. See `INTENT`;
    * the question's context must carry the remedy, not only the symptom. See
      `_request_question`.

    A REFUSAL WRITES THE REASON ON THE ALARM AND FILES NOTHING. The alarm goes to
    `escalated`, because the supervisor believes an action is needed and is not
    permitted to take it — which makes the user the right next reader.
    """
    from . import db

    refused = _refusal(pstore, alarm, remedy_id, cfg)
    if refused is not None:
        pstore.update_alarm(alarm["id"], status="escalated", verdict="propose",
                            verdict_reason=refused, decided_at=db.now())
        carrier = _carrier_id(pstore, alarm) or alarm["wo_id"]
        pstore.add_event(carrier, "remedy_refused", {
            "alarm_id": alarm["id"], "remedy": remedy_id, "reason": refused,
            "by": "the catalog"})
        log.info("remedy %s refused on %s: %s", remedy_id, alarm["id"], refused)
        return {"proposed": False, "reason": refused, "approval": None,
                "question": None}

    remedy = REMEDIES[remedy_id]
    subject_id = str(subject.get("id") or alarm["wo_id"])
    carrier = _carrier_id(pstore, alarm) or alarm["wo_id"]
    command = INTENT.format(alarm_id=alarm["id"], remedy=remedy_id,
                            subject_id=subject_id,
                            argument=(argument or "").strip())
    approval = pstore.add_approval(
        carrier, GATE_KIND, command,
        # No recogniser fired: this request was filed by the OS, not matched out of a
        # command line. `matched` is what `gates.learn_from_dismissal` would build an
        # exemption from, and there is no pattern here to generalise.
        matched="",
        justification=reason, evidence=evidence, max_uses=GRANT_USES,
    )
    question = neo.ask(
        project, carrier,
        _request_question(project, subject_id, remedy, argument, evidence, reason),
        context=f"{subject.get('title') or ''}\n{alarm.get('reason') or ''}",
        # THE EXISTING KIND. A `self_heal` request is an approval, `neo_store.Q_KINDS`
        # already carries it, and `Daemon._deliver_gate_verdict` looks its subject up in
        # `approvals` — which is where this row lives. Adding a kind would need a
        # `deliver()` arm, and a kind without one falls through to `queue_message` and
        # messages the worker, which is the one act this feature is fenced against.
        kind="approval",
    )
    pstore.link_neo_question(approval["id"], question["id"])
    pstore.update_alarm(
        alarm["id"], status="proposed", verdict="propose", remedy=remedy_id,
        remedy_argument=(argument or "").strip(), remedy_approval_id=approval["id"],
        verdict_reason=reason, note=note, decided_at=db.now())
    pstore.add_event(carrier, "remedy_proposed", {
        "alarm_id": alarm["id"], "remedy": remedy_id, "approval_id": approval["id"],
        "neo_question_id": question["id"], "subject_id": subject_id,
        "argument": (argument or "").strip(), "reason": reason})
    log.info("remedy %s proposed for %s as gate request %s", remedy_id, alarm["id"],
             approval["id"])
    return {"proposed": True, "reason": reason, "approval": approval,
            "question": question}


# -- the verdict, and applying ---------------------------------------------------------


def record_verdict(pstore: Any, approval: dict[str, Any], verdict: str, reason: str,
                   decided_by: str, central: Any = None, project: str = "") -> None:
    """What a `self_heal` verdict does INSTEAD of messaging the worker.

    `gates.apply_decision` queues a resume message on every verdict — right for a worker
    that ran a command and is waiting to retry it, wrong here twice over. Nobody asked:
    on a denial the message is noise delivered into a running turn, the exact act this
    feature is fenced against, and on an approval it is redundant because the remedy
    itself is the intervention.

    An approval changes nothing on the alarm: it stays `proposed` with its flag up until
    `Daemon.remedy_tick` has actually applied it, because "permitted" and "done" are two
    different facts and the flag answers the second one.
    """
    from . import db
    from .central_store import CentralStore

    alarm = pstore.alarm_for_remedy_approval(approval["id"])
    if alarm is None:
        log.warning("self_heal approval %s judges no alarm (work order deleted?)",
                    approval["id"])
        return
    own = central is None
    central = central or CentralStore()
    try:
        if verdict == "approved":
            central.add_inbox(
                project=project, level="info",
                title=APPROVED_INBOX_TITLE.format(
                    by=decided_by, remedy=alarm["remedy"], alarm_id=alarm["id"]),
                body=f"{reason}\n"
                     f"The OS will apply it on the next tick and say what it did.\n"
                     f"Read it with: jarvis alarms show {alarm['id']}",
                wo_id=approval["wo_id"])
            return

        # Denied or dismissed. THE FLAG GOES BACK UP: the user refused the remedy and
        # the symptom it was for has not gone anywhere.
        why = REFUSED_REASON.format(remedy=alarm["remedy"], verdict=verdict,
                                    by=decided_by, reason=reason)
        pstore.update_alarm(alarm["id"], status="escalated", verdict_reason=why,
                            decided_at=db.now())
        pstore.add_event(approval["wo_id"], "remedy_refused", {
            "alarm_id": alarm["id"], "remedy": alarm["remedy"],
            "approval_id": approval["id"], "verdict": verdict, "by": decided_by,
            "reason": reason})
        _flag_and_tell(pstore, central, project, approval["wo_id"], alarm, why)
    finally:
        if own:
            central.close()


def _flag_and_tell(pstore: Any, central: Any, project: str, wo_id: str,
                   alarm: dict[str, Any], why: str) -> None:
    """Put the unresolved alarm back in front of the user.

    THE INBOX ROW IS THE DURABLE HALF, `supervisor._flag_the_user`'s reason:
    `invariants.check_no_phantom_attention` clears the flag on any work order that has
    settled, so a refusal that only raised a flag would evaporate on the next tick.
    """
    from . import supervisor

    pstore.flag_attention(wo_id, supervisor.ALARM_BLOCKER.format(alarm_id=alarm["id"]))
    central.add_inbox(
        project=project, level="warning",
        title=REFUSED_INBOX_TITLE.format(alarm_id=alarm["id"]),
        body=f"{alarm['reason']}\n{why}\n"
             f"Read it with: jarvis alarms show {alarm['id']}",
        wo_id=wo_id)


def apply(pstore: Any, central: Any, project: str, approval: dict[str, Any] | None,
          alarm: dict[str, Any], subject: dict[str, Any]) -> str:
    """Run one approved remedy, once, and record what it did. Returns the result string.

    REFUSES unless the approval is `approved` AND `usable_grant` still yields it — every
    other path raises `RemedyRefused` and performs nothing.

    The grant is spent through `gates.open_gate` and never by hand. Be honest about what
    that buys here: `open_gate`'s second half closes the pending requests a grant makes
    moot, and for THIS kind it is inert, because two proposals on one work order name
    different alarms and so never wrap each other's command string. It is used anyway so
    that one function spends every grant in the OS — the alternative is a second place
    that knows how, which is how the two come to disagree (production question 118 was
    exactly that divergence, on the other side).

    The grant is spent BEFORE the handler runs. A handler that then refuses has burned
    the permission, which is the right way round: a permission is to attempt the act,
    and re-attempting it needs a fresh review rather than a free retry.
    """
    from . import db, gates, ops

    remedy = REMEDIES.get(str(alarm.get("remedy") or ""))
    if remedy is None:
        raise RemedyRefused(
            f"alarm {alarm['id']} names no remedy this OS has ({alarm.get('remedy')!r})")
    if approval is None:
        raise RemedyRefused(f"alarm {alarm['id']} has no gate request")
    if approval["kind"] != GATE_KIND:
        raise RemedyRefused(
            f"gate request {approval['id']} is a {approval['kind']}, not a {GATE_KIND}")
    if approval["status"] != "approved":
        raise RemedyRefused(
            f"gate request {approval['id']} is {approval['status']}, not approved")
    grant = pstore.usable_grant(approval["wo_id"], approval["kind"],
                               approval["command"])
    if grant is None or grant["id"] != approval["id"]:
        raise RemedyRefused(
            f"gate request {approval['id']} is no longer a live grant — it has expired "
            f"or its uses are spent")

    spent = gates.open_gate(pstore, grant)
    result = remedy.apply(pstore, central, project, subject, alarm)
    wo_id = approval["wo_id"]
    pstore.update_alarm(alarm["id"], status="acked", decided_at=db.now())
    pstore.add_event(wo_id, "remedy_applied", {
        "alarm_id": alarm["id"], "remedy": remedy.id, "approval_id": approval["id"],
        "use": spent["uses"], "result": result})
    central.add_inbox(
        project=project, level="info",
        title=APPLIED_INBOX_TITLE.format(subject_id=subject.get("id") or wo_id),
        body=f"{alarm['reason']}\nThe `{remedy.id}` remedy was applied: {result}\n"
             f"Read it with: jarvis alarms show {alarm['id']}",
        wo_id=wo_id)

    # THROUGH `ops.ack_attention`, NEVER `ProjectStore.clear_attention`, which wipes
    # `acknowledged_blockers` and so discards the user's own earlier dismissals. It also
    # refuses an order with a pending assumption — the louder ask — and then the alarm
    # stays `acked` (it WAS addressed) with the flag up.
    try:
        ops.ack_attention(wo_id)
    except ops.OpsError as exc:
        log.info("remedy applied on %s; attention left up: %s", alarm["id"], exc)
    log.info("remedy %s applied for %s: %s", remedy.id, alarm["id"], result)
    return result
