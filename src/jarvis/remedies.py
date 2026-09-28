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
import re
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

#: `INTENT`'s sibling for a USER-INITIATED fix — §11 of
#: docs/specs/2026-09-24-order-observability.md. IT IS NOT A COMMAND EITHER and nothing
#: will execute it. It names NO alarm because there is none: the user reached the remedy
#: off §6's diagnosis, and `apply` recovers the remedy and the subject from this string
#: rather than from an alarm row (Neo q795). The prefix differs from `INTENT`'s so the two
#: can never be read as each other.
USER_INTENT = "apply {remedy} to {subject_id}, asked for by the user — {argument}"

#: The parser for the line above, and the two must stay in step — a round-trip test pins
#: them. A string that is not one of ours yields None rather than a guess: `apply` refuses
#: on None, and guessing a remedy out of an unrecognised grant is how a reviewer comes to
#: have authorised something other than what they read.
_USER_INTENT_RE = re.compile(
    r"^apply (?P<remedy>[A-Za-z_][A-Za-z0-9_-]*) to (?P<subject_id>\S+), "
    r"asked for by the user — (?P<argument>.*)$", re.DOTALL)


def user_intent(subject_id: str, remedy: str, argument: str) -> str:
    """`approvals.command` for one user-initiated fix. See `USER_INTENT`."""
    return USER_INTENT.format(remedy=remedy, subject_id=subject_id,
                              argument=(argument or "").strip())


def parse_user_intent(command: str | None) -> dict[str, str] | None:
    """`{"remedy", "subject_id", "argument"}`, or None when this grant is not one of ours.

    ONE HOME FOR BOTH DIRECTIONS, beside the formatter: `apply` reads back what `propose_fix`
    wrote, and a second place that knows the shape is how the two come to disagree.
    """
    match = _USER_INTENT_RE.match(command or "")
    if match is None:
        return None
    return {"remedy": match["remedy"], "subject_id": match["subject_id"],
            "argument": match["argument"]}


#: The one message a nudge sends. Short on purpose: delivering it re-sends the worker's
#: whole conversation at the cache-write rate, which is the very cost the `big-rewrite`
#: alarm exists to report.
NUDGE = """[supervisor] The OS flagged this order as possibly unhealthy and was given \
permission to ask you about it: {reason}

{argument}

Reply on the record with where you are: what you are working on right now, what you are
waiting on, and whether you are stuck. This is a question, not an instruction — do not
change course on the strength of it."""

#: The same message when the USER asked for it (§11). A separate literal and not a
#: parameter: `NUDGE` states that the OS FLAGGED this order and was GIVEN PERMISSION to ask
#: about it, and both halves are false here — nothing flagged it, and the permission was
#: granted for the user's own request. A worker told an alarm exists goes looking for one.
USER_NUDGE = """[the OS] The user asked us to check in with you: {reason}

{argument}

Reply on the record with where you are: what you are working on right now, what you are
waiting on, and whether you are stuck. This is a question, not an instruction — do not
change course on the strength of it."""

#: `verdict_reason` on an alarm whose proposal the reviewer refused. Names the verdict
#: and the reason, because the row is read on `/alarms` beside alarms the supervisor
#: settled alone and "escalated" alone does not say that an action was asked for.
REFUSED_REASON = "the {remedy} remedy was {verdict} by {by}: {reason}"

#: How much of the judge's argument becomes the new work order's TITLE. A title is an
#: index entry — it is what `jarvis wo list`, the dashboard and every attention line show
#: — so it is clipped rather than wrapped, and the whole argument survives in the brief.
TITLE_CHARS = 120

#: The fix order's brief. The JUDGE supplies the prose and the OS supplies the frame: the
#: worker sees only this text (prime directive 3), so the alarm it came from has to travel
#: with it or the order arrives as an assertion with no evidence behind it.
FIX_BRIEF = """The supervisor raised this off cost alarm {alarm_id}.

WHAT IT SAW: {reason}

WHAT IT WANTS DONE: {argument}

Read the alarm before you start — `jarvis alarms show {alarm_id}` — and fix the ROOT
CAUSE rather than the symptom it was measured by. A separate work order is already filed
to ship your fix once this one completes, so finish behind a pull request as usual and do
not release anything yourself."""

#: The same brief when the USER asked (§11). `FIX_BRIEF` sends the worker to
#: `jarvis alarms show <alarm>`, which on this path is a row that does not exist; the
#: evidence lives on the order's own diagnosis instead (§6's `jarvis wo why`), so that is
#: where this points. Same frame otherwise: the worker sees only this text.
USER_FIX_BRIEF = """The user asked for this off the diagnosis of {subject_id}.

WHAT THEY SAW: {reason}

WHAT THEY WANT DONE: {argument}

Read the diagnosis before you start — `jarvis wo why {subject_id}` — and fix the ROOT
CAUSE rather than the symptom it was noticed by. A separate work order is already filed
to ship your fix once this one completes, so finish behind a pull request as usual and do
not release anything yourself."""

#: `_apply_file_work_order`'s refusal, hoisted to module level so §11's entry point can
#: state it BEFORE a grant is filed instead of after one was spent — one home for the rule,
#: because a second copy passes every behavioural test and drifts anyway (kn-4ea33fe6).
NO_ARGUMENT = ("{origin} proposed `file_work_order` with no argument — there is nothing "
               "to brief a work order with")

#: The ship order's title and brief, and these words are the OS's rather than the judge's
#: on purpose: "ship it" is the same job every time, and a judge writing its own release
#: instructions each time is a judge inventing a procedure the `shipit` skill already owns.
SHIP_TITLE = "Ship the fix for {alarm_id} to production"
#: The same title with the subject in place of the alarm, for §11's alarm-less path.
USER_SHIP_TITLE = "Ship the fix for {subject_id} to production"
SHIP_BRIEF = """This order ships {fix_id}, and runs only after it completes.

Cut a release from ALREADY-MERGED main and deploy it with the `shipit` skill, which is the
only supported route. The release is a PRIVILEGED ACTION: you will be blocked, the request
is filed, and a reviewer decides. That is expected — make the case in the request rather
than looking for a way around it.

Why it is worth shipping promptly: {reason}"""

#: Inbox titles. User-facing copy, so they live here rather than at their call sites —
#: every inbox row reaches every sink, Telegram included. There is deliberately NO row
#: for a proposal being FILED: telling the user before the reviewer has even looked is
#: the attention cost this gate exists to avoid.
APPROVED_INBOX_TITLE = "{by} approved the {remedy} remedy on {alarm_id}"
APPLIED_INBOX_TITLE = "The supervisor acted on {subject_id}"
REFUSED_INBOX_TITLE = "A remedy was refused, and {alarm_id} still needs you"

#: The same three for §11, naming the SUBJECT because there is no alarm to name. The
#: refusal is not an attention item here and the title says why: the user asked, the
#: reviewer said no, and nothing was done — there is no unresolved symptom left behind.
USER_APPROVED_INBOX_TITLE = "{by} approved the {remedy} fix on {subject_id}"
USER_APPLIED_INBOX_TITLE = "The {remedy} fix you asked for on {subject_id} was applied"
USER_REFUSED_INBOX_TITLE = ("The {remedy} fix on {subject_id} was refused — nothing was "
                            "done")


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
    #: The `ops.waiting_on` slugs this remedy clears. THE MAPPING LIVES ON THE REMEDY, and
    #: that is the point of deleting `ops.FIX_MATCHES` (Neo q839): one home per rule
    #: (kn-4ea33fe6), so a remedy added later is reachable from `ops.fix` with no edit to
    #: `ops` at all — and a slug no remedy claims falls to `fix`'s default arm, which asks
    #: for the remedy rather than shrugging.
    covers: tuple[str, ...]
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


def subject_kind_of(subject: dict[str, Any]) -> str:
    """`work_order` or `feature_order` for a SUBJECT dict — §11's path has no alarm row.

    Off the id prefix, because the two live in different tables and nothing on the row
    says which one it came from. A feature order and an improvement order are both
    `feature_orders` rows and both are `feature_order` here, which is what `Remedy.subjects`
    means by it.
    """
    return "work_order" if str(subject.get("id") or "").startswith("wo-") else \
        "feature_order"


def _carrier_for_subject(pstore: Any, subject: dict[str, Any]) -> str | None:
    """`_carrier_id`'s sibling for a subject with no alarm. Same rule, same reason: a
    feature order has no timeline of its own, so its record rides §1's carrier."""
    sid = str(subject.get("id") or "")
    if subject_kind_of(subject) == "work_order":
        return sid or None
    carrier = pstore.carrier_for_feature(sid)
    return str(carrier["id"]) if carrier else None


@dataclass(frozen=True)
class Intent:
    """Exactly what a handler needs, from EITHER authority — §11 (Neo q795).

    Two constructors and one shape, so the handlers cannot know which path they are on
    except by asking `alarm_id is None` — which is the one thing they must know, because
    an OS that names an alarm the user never saw raised is telling them something false.
    """

    remedy: str
    subject_id: str
    carrier_id: str | None
    argument: str
    reason: str
    alarm_id: str | None = None
    fo_id: str | None = None

    @classmethod
    def from_alarm(cls, pstore: Any, alarm: dict[str, Any]) -> Intent:
        """Today's supervisor path. `reason` is left UNSTRIPPED: it goes verbatim into
        `NUDGE`, and the message a running session receives must not change here."""
        feature = subject_kind(alarm) == "feature_order"
        return cls(
            remedy=str(alarm.get("remedy") or ""),
            subject_id=str((alarm.get("fo_id") if feature else alarm.get("wo_id")) or ""),
            carrier_id=_carrier_id(pstore, alarm),
            argument=(alarm.get("remedy_argument") or "").strip(),
            reason=str(alarm.get("reason") or ""),
            alarm_id=str(alarm["id"]),
            fo_id=alarm.get("fo_id"),
        )

    @classmethod
    def from_grant(cls, pstore: Any, approval: dict[str, Any],
                   subject: dict[str, Any]) -> Intent:
        """§11's path: the remedy and the subject come from the grant's own command string.

        Raises `RemedyRefused` on a grant that is not one of ours, and on one whose subject
        is not the subject it was handed — a grant is a receipt for one act on one order.
        """
        parsed = parse_user_intent(approval["command"])
        if parsed is None:
            raise RemedyRefused(
                f"gate request {approval['id']} does not name a remedy and a subject "
                f"this OS can read")
        if str(subject.get("id") or "") != parsed["subject_id"]:
            raise RemedyRefused(
                f"gate request {approval['id']} authorises a fix on "
                f"{parsed['subject_id']}, not on {subject.get('id')}")
        return cls(
            remedy=parsed["remedy"],
            subject_id=parsed["subject_id"],
            # The carrier is the order the request was FILED on, which `propose_fix`
            # already resolved through `_carrier_for_subject`. Re-deriving it here could
            # pick a different carrier than the one the record is on.
            carrier_id=str(approval["wo_id"]),
            argument=parsed["argument"],
            reason=str(approval.get("justification") or ""),
            alarm_id=None,
            fo_id=(parsed["subject_id"]
                   if subject_kind_of(subject) == "feature_order" else None),
        )


# -- the handlers. EVERY ACTING CALL IN THIS MODULE IS INSIDE ONE OF THESE ------------
#
# `tests/test_remedies.py::test_the_acting_calls_stay_inside_the_handlers` walks this
# file's AST for `send_message`, `queue_message`, `unblock_work_order`,
# `create_work_order`, `cancel`, `cancel_work_order` and `set_status` as an attribute or a
# bare name, and requires the nearest enclosing function to be one of `REMEDIES[*].apply`.
# Reachability is not decidable from an AST; enclosure is, and it is the property that
# actually matters.


def _apply_nudge(pstore: Any, central: Any, project: str, subject: dict[str, Any],
                 intent: Intent) -> str:
    """Ask the session where it is. One message, and nothing else.

    NOT `ops.send_message`, and this is not a style choice: that function ends with
    `clear_attention`, which sets `acknowledged_blockers` to NULL and so discards the
    user's own earlier dismissals — before `apply` ever reaches `ops.ack_os_flag`.
    `ops.nudge_pr_repair` is the precedent for an OS-authored message and does the two
    right things; the event half of it is written by `apply`, once, for every remedy.
    """
    wo_id = intent.carrier_id
    if wo_id is None:
        raise RemedyRefused(
            f"{intent.fo_id} has no session to speak to — nothing was sent")
    # §11: the alarm wording claims the OS flagged this order, so it may only be used when
    # an alarm actually exists. See `USER_NUDGE`.
    template = NUDGE if intent.alarm_id is not None else USER_NUDGE
    pstore.queue_message(
        wo_id,
        template.format(reason=intent.reason, argument=intent.argument),
        source=MESSAGE_SOURCE,
    )
    return (f"queued one message on {wo_id} asking it to say where it is "
            f"(delivered on its next turn)")


def _apply_unblock(pstore: Any, central: Any, project: str, subject: dict[str, Any],
                   intent: Intent) -> str:
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


def _title_from(argument: str) -> str:
    """The judge's first line, as a work order's title.

    Its FIRST LINE and not a summary of it: asking a model for a title as well as an
    argument is a second thing to get wrong, and the first line of an argument written to
    be read by a human already is one.
    """
    first = next((line.strip() for line in argument.splitlines() if line.strip()), "")
    return first[:TITLE_CHARS]


def _apply_file_work_order(pstore: Any, central: Any, project: str,
                           subject: dict[str, Any], intent: Intent) -> str:
    """File the fix, then the order that ships it, joined by a dependency edge.

    TWO ORDERS IN ONE APPLICATION, AND THAT IS THE REMEDY RATHER THAN TWO REMEDIES: a fix
    nobody ships is not a fix (issue 164 item 1), and `--depends-on` is the OS's own idiom
    for ordering a two-step job in one go. Nothing reaches production on the strength of
    this permission — the ship order still meets the release gate when it runs, and a
    reviewer still decides there.

    THE JUDGE SUPPLIES PROSE AND THE MODULE SUPPLIES STRUCTURE, which is the same
    closed-vocabulary discipline as the rest of this file: there is no free-text action
    here either, only a free-text BRIEF inside one.

    THE SHIP ORDER'S FAILURE IS REPORTED, NEVER RAISED. `RemedyRefused` means nothing was
    done, and by then the fix order exists — so a raise would write a false statement into
    the record of the one path that already did half its work.
    """
    from . import ops

    argument = intent.argument
    origin = intent.alarm_id or intent.subject_id
    if not argument:
        raise RemedyRefused(NO_ARGUMENT.format(origin=origin))
    reason = intent.reason.strip()
    # §11: the alarm brief sends the worker to `jarvis alarms show`, which is a row that
    # does not exist when the user asked. See `USER_FIX_BRIEF`.
    if intent.alarm_id is not None:
        brief = FIX_BRIEF.format(alarm_id=intent.alarm_id, reason=reason,
                                 argument=argument)
        ship_title = SHIP_TITLE.format(alarm_id=intent.alarm_id)
    else:
        brief = USER_FIX_BRIEF.format(subject_id=intent.subject_id, reason=reason,
                                      argument=argument)
        ship_title = USER_SHIP_TITLE.format(subject_id=intent.subject_id)
    try:
        fix = ops.create_work_order(project, _title_from(argument), description=brief)
    except ops.OpsError as exc:
        raise RemedyRefused(str(exc)) from exc
    try:
        ship = ops.create_work_order(
            project, ship_title,
            description=SHIP_BRIEF.format(fix_id=fix["id"], reason=reason),
            depends_on=[fix["id"]])
    except ops.OpsError as exc:
        log.warning("filed %s for %s but could not file its ship order: %s",
                    fix["id"], origin, exc)
        return (f"filed {fix['id']} to fix the root cause; its ship order could NOT be "
                f"filed ({exc}) — file one by hand with `jarvis wo create {project} "
                f"\"…\" --depends-on {fix['id']}`")
    return (f"filed {fix['id']} to fix the root cause, and {ship['id']} to ship it "
            f"(blocked until {fix['id']} completes; the release itself is still gated)")


REMEDIES: dict[str, Remedy] = {
    "nudge": Remedy(
        id="nudge",
        headline="ask the session to say where it is, in one short message",
        blast="reaches a RUNNING session and costs one delivered turn, which re-sends "
              "the whole conversation at the cache-write rate. It changes no state and "
              "gives no instruction, but a message cannot be unsent and the spend "
              "cannot be undone.",
        subjects=("work_order", "feature_order"),
        # `prompt` is the ONE answer `waiting_on` reports as `stalled` — nothing is coming
        # for it by itself, and a message is the only thing that can move it.
        covers=("prompt",),
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
        # `pending` is an order held behind dependency edges. Claimed here and QUALIFIED in
        # `ops._fix_match`: this handler is the default mode and cuts only edges that can
        # never clear, so an order whose every edge is live is not this case.
        covers=("pending",),
        apply=_apply_unblock,
    ),
    "file_work_order": Remedy(
        id="file_work_order",
        headline="file a work order to fix the root cause, and a second one to ship "
                 "that fix, blocked until the first completes",
        blast="creates TWO NEW work orders in this project and touches nothing that "
              "already exists: no running session is reached, no status is changed and "
              "no existing order is altered. The first is dispatched as soon as a slot "
              "is free and will spend a whole worker session; the second waits for it, "
              "and when it runs the release is still a gated action a reviewer must "
              "approve. An order can be cancelled, but the tokens the first one spends "
              "before anyone looks cannot be taken back.",
        subjects=("work_order", "feature_order"),
        # EMPTY ON PURPOSE, and it is a decision rather than an omission: this writes code
        # and clears no blocker mechanically, so it stays reachable only by being NAMED —
        # by the user with `--remedy`, or by `ops.fix`'s default arm asking for the remedy
        # an unclassified blocker has no.
        covers=(),
        apply=_apply_file_work_order,
    ),
}

#: Asserted equal to `tuple(REMEDIES)`. The registry is closed BY A TEST rather than by
#: a convention, so widening what the OS may do fails a suite and is read by a human.
SHIPPED_REMEDIES: tuple[str, ...] = ("nudge", "unblock", "file_work_order")


def get(remedy_id: str) -> Remedy:
    """The remedy, or `KeyError`. There is no free-text action and no "other"."""
    return REMEDIES[remedy_id]


def covering(slug: str) -> str | None:
    """The one remedy whose `covers` holds this `ops.waiting_on` slug, or None.

    THE REGISTRY ANSWERS THIS AND NOT A TABLE IN `ops` (Neo q839): the user's ruling is
    that auto-matching a blocker against a closed table there means the OS never learns
    which blockers it has no remedy for. None is therefore a USEFUL answer and `ops.fix`
    acts on it — it asks for a remedy to be written — so this function must never guess.

    RAISES on two owners. A slug with two remedies is a registry defect, and picking one
    of them is a choice `ops` is in no position to make on a caller's behalf.
    """
    found = [r.id for r in REMEDIES.values() if slug in r.covers]
    if len(found) > 1:
        raise ValueError(f"{slug} is covered by more than one remedy: "
                         f"{', '.join(sorted(found))}")
    return found[0] if found else None


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


def _config_refusal(remedy_id: str, kind: str, cfg: Any) -> str | None:
    """Everything the CATALOG refuses, whoever asked — §11 shares this verbatim.

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
    if kind not in remedy.subjects:
        return (f"`{remedy_id}` does not apply to a {kind} "
                f"(it applies to: {', '.join(remedy.subjects)})")
    return None


def _refusal(pstore: Any, alarm: dict[str, Any], remedy_id: str,
             cfg: Any) -> str | None:
    """Why this ALARM's proposal may not be filed, or None. Reads only; writes nothing."""
    refused = _config_refusal(remedy_id, subject_kind(alarm), cfg)
    if refused is not None:
        return refused
    existing = alarm.get("remedy_approval_id")
    if existing:
        approval = pstore.get_approval(int(existing))
        if approval is not None and approval["status"] == "pending":
            return (f"a remedy for this alarm is already awaiting a verdict "
                    f"(gate request {approval['id']})")
    return None


def _request_question(project: str, subject_id: str, remedy: Remedy, argument: str,
                      evidence: str, reason: str, *, by_user: bool = False) -> str:
    """What the reviewer reads. The alarm's own evidence packet, then the supervisor's
    reading, then the remedy in words.

    All three, because the reviewer is being asked to rule on the ACTION and not merely
    on the symptom: an approval here permits something to reach a running session, and
    a request that showed only "this turn looks stuck" would be answered on a different
    question from the one it is asking.
    """
    # §11 changes WHO ASKED and nothing else about the request. A reviewer told the
    # supervisor judged an order unhealthy, when in fact the user asked off a diagnosis,
    # would be ruling on a symptom nobody reported.
    if by_user:
        lead = (f"The user asked the OS to apply the `{remedy.id}` remedy to "
                f"{subject_id} in {project}, off that order's own diagnosis. Nothing has "
                f"been done; this authorises it.")
        actor, wants = "What the OS would say or do", "# Why it was asked for"
        tail = ("Approve it to let the OS act, or deny it with a reason. Denying does "
                "nothing at all and tells the user it was refused, which is the safe "
                "answer whenever the case for acting is not made.")
    else:
        lead = (f"The supervisor judged {subject_id} in {project} unhealthy and wants to "
                f"apply the `{remedy.id}` remedy. Nothing has been done; this "
                f"authorises it.")
        actor, wants = "What the supervisor would say or do", "# Why the supervisor wants it"
        tail = ("Approve it to let the OS act, or deny it with a reason. Denying leaves "
                "the alarm open and with the user, which is the safe answer whenever the "
                "case for acting is not made.")
    parts = [
        f"SELF-HEAL REQUEST — gate `{GATE_KIND}`",
        lead,
        f"# The remedy\n"
        f"What it does: {remedy.headline}\n"
        f"What it touches, and what it cannot undo: {remedy.blast}\n"
        f"{actor}: {argument.strip() or '(nothing given)'}",
        f"{wants}\n{reason.strip() or '(no reason given)'}",
    ]
    # An empty evidence packet is a FACT about an alarm's proposal and worth saying; on
    # the user's path there is no packet to be empty, so saying it would invent a gap.
    if evidence.strip():
        parts.append(evidence.strip())
    elif not by_user:
        parts.append("(the evidence packet was empty)")
    parts.append(tail)
    return "\n\n".join(parts)


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


def propose_fix(pstore: Any, neo: Any, project: str, subject: dict[str, Any],
                remedy_id: str, argument: str, cfg: Any,
                *, reason: str = "") -> dict[str, Any]:
    """`propose` for a subject with NO ALARM — §11 of
    docs/specs/2026-09-24-order-observability.md, on Neo's q795 ruling.

    Same return shape, same registry, same allow-list, same `self_heal` grant, same
    reviewer, same `GRANT_USES`. THE ONLY NEW THING IS WHO ASKED: the user reached one of
    the three shipped remedies from §6's diagnosis, which today is reachable only from a
    supervisor alarm. No remedy is added, no allow-list widened and no gate skipped.

    IT WRITES NO ALARM ROW and calls `update_alarm` for nothing. An alarm is the
    supervisor's record of a judgement it made; synthesising one here would put a finding
    nobody found on `/alarms` and hand the user an attention item for their own request.

    A REFUSAL WRITES NOTHING AT ALL, which is where it differs from `propose`: that one
    escalates the alarm it was refused on, and there is no row here to escalate. The
    caller has the reason in hand and shows it to the user directly.
    """
    refused = _config_refusal(remedy_id, subject_kind_of(subject), cfg)
    subject_id = str(subject.get("id") or "")
    carrier = None if refused else _carrier_for_subject(pstore, subject)
    command = user_intent(subject_id, remedy_id, argument)
    if refused is None and carrier is None:
        refused = (f"{subject_id} has no work order to carry the request — nothing has "
                   f"been planned for it yet")
    if refused is None:
        # A DUPLICATE IS REFUSED BY NUMBER, `_refusal`'s alarm-side check in the only form
        # available here: two live grants for one command are two authorisations for one
        # act, and the user's next step is to answer the request they already have.
        for pending in pstore.pending_approvals(carrier):
            if pending["kind"] == GATE_KIND and pending["command"] == command:
                refused = (f"this fix is already awaiting a verdict "
                           f"(gate request {pending['id']})")
                break
    if refused is not None:
        log.info("user fix %s refused on %s: %s", remedy_id, subject_id, refused)
        return {"proposed": False, "reason": refused, "approval": None, "question": None}

    remedy = REMEDIES[remedy_id]
    approval = pstore.add_approval(
        carrier, GATE_KIND, command,
        matched="",  # no recogniser fired — `propose`'s reason, unchanged
        justification=reason, evidence="", max_uses=GRANT_USES,
    )
    question = neo.ask(
        project, carrier,
        _request_question(project, subject_id, remedy, argument, "", reason,
                          by_user=True),
        context=f"{subject.get('title') or ''}\n{reason}",
        kind="approval",   # the existing kind, for `propose`'s reasons
    )
    pstore.link_neo_question(approval["id"], question["id"])
    pstore.add_event(carrier, "remedy_proposed", {
        # NO `alarm_id` KEY, and `timeline._describe` forks on its absence: an entry that
        # said "the supervisor asked permission" would credit a judgement nobody made.
        "remedy": remedy_id, "approval_id": approval["id"],
        "neo_question_id": question["id"], "subject_id": subject_id,
        "argument": (argument or "").strip(), "reason": reason,
        "at_user_request": True})
    log.info("user fix %s proposed for %s as gate request %s", remedy_id, subject_id,
             approval["id"])
    return {"proposed": True, "reason": reason, "approval": approval,
            "question": question}


def user_grants(pstore: Any) -> list[dict[str, Any]]:
    """The approved, unexpired, unspent §11 grants no alarm claims. Reads only.

    `Daemon.remedy_tick` scans alarms at `proposed`, so an alarm-less grant would sit
    approved for ever without this. The live-grant test is `usable_grant`'s, not a fresh
    reading of the columns — `apply` refuses on exactly that predicate, and a tick that
    used a different one would queue work the applier then declines.
    """
    out: list[dict[str, Any]] = []
    # SCOPED TO THE KIND IN SQL. `list_approvals` is bounded to the 200 newest rows of
    # every kind, so ordinary gate traffic could bury a grant between its approval and its
    # application — an approved act that then silently never happens.
    for approval in pstore.approvals_of_kind(GATE_KIND, "approved"):
        if parse_user_intent(approval["command"]) is None:
            continue
        if pstore.alarm_for_remedy_approval(approval["id"]) is not None:
            continue
        grant = pstore.usable_grant(approval["wo_id"], approval["kind"],
                                    approval["command"])
        if grant is None or grant["id"] != approval["id"]:
            continue
        out.append(approval)
    return out


def subject_of_grant(pstore: Any, approval: dict[str, Any]) -> dict[str, Any] | None:
    """The work order or feature order one §11 grant is about, or None if it is gone."""
    parsed = parse_user_intent(approval["command"])
    if parsed is None:
        return None
    sid = parsed["subject_id"]
    try:
        return (pstore.get_work_order(sid) if sid.startswith("wo-")
                else pstore.get_feature_order(sid))
    except KeyError:
        return None


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
    intent = parse_user_intent(approval["command"])
    if alarm is None and intent is None:
        log.warning("self_heal approval %s judges no alarm (work order deleted?)",
                    approval["id"])
        return
    own = central is None
    central = central or CentralStore()
    try:
        if alarm is None:
            # §11: an alarm-less grant is the USER's own request, so the verdict is
            # reported in their terms and there is no alarm to re-raise.
            assert intent is not None
            _user_verdict(pstore, central, project, approval, intent, verdict, reason,
                          decided_by)
            return
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


def _user_verdict(pstore: Any, central: Any, project: str, approval: dict[str, Any],
                  intent: dict[str, str], verdict: str, reason: str,
                  decided_by: str) -> None:
    """The verdict on a §11 fix: an inbox row either way, and NEVER a queued message.

    NO ATTENTION FLAG on a refusal, which is where this differs from `_flag_and_tell`:
    that one puts an unresolved ALARM back in front of the user, and here there is no
    finding left outstanding — the user asked, the reviewer said no, nothing was done, and
    they are told so. Flagging would be the OS inventing a blocker out of its own refusal.
    """
    remedy = intent["remedy"]
    subject_id = intent["subject_id"]
    if verdict == "approved":
        central.add_inbox(
            project=project, level="info",
            title=USER_APPROVED_INBOX_TITLE.format(by=decided_by, remedy=remedy,
                                                   subject_id=subject_id),
            body=f"{reason}\n"
                 f"The OS will apply it on the next tick and say what it did.\n"
                 f"Read it with: jarvis wo show {subject_id}",
            wo_id=approval["wo_id"])
        return
    central.add_inbox(
        project=project, level="warning",
        title=USER_REFUSED_INBOX_TITLE.format(remedy=remedy, subject_id=subject_id),
        body=f"{decided_by} {verdict} it: {reason}\n"
             f"Nothing was done to {subject_id}. Ask again if you still want it.",
        wo_id=approval["wo_id"])
    pstore.add_event(approval["wo_id"], "remedy_refused", {
        "remedy": remedy, "approval_id": approval["id"], "verdict": verdict,
        "by": decided_by, "reason": reason, "subject_id": subject_id,
        "at_user_request": True})


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
          alarm: dict[str, Any] | None, subject: dict[str, Any]) -> str:
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

    # `alarm=None` is §11: the remedy and the subject come from the GRANT, because there is
    # no alarm row holding them (Neo q795). Every refusal below is the same one, in the
    # same order.
    if alarm is not None:
        remedy = REMEDIES.get(str(alarm.get("remedy") or ""))
        if remedy is None:
            raise RemedyRefused(
                f"alarm {alarm['id']} names no remedy this OS has "
                f"({alarm.get('remedy')!r})")
        if approval is None:
            raise RemedyRefused(f"alarm {alarm['id']} has no gate request")
        intent = Intent.from_alarm(pstore, alarm)
    else:
        if approval is None:
            raise RemedyRefused(
                f"a fix on {subject.get('id')} has no gate request — nothing was done")
        intent = Intent.from_grant(pstore, approval, subject)
        remedy = REMEDIES.get(intent.remedy)
        if remedy is None:
            raise RemedyRefused(
                f"gate request {approval['id']} names no remedy this OS has "
                f"({intent.remedy!r})")
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
    result = remedy.apply(pstore, central, project, subject, intent)
    wo_id = approval["wo_id"]
    if alarm is not None:
        pstore.update_alarm(alarm["id"], status="acked", decided_at=db.now())
    pstore.add_event(wo_id, "remedy_applied", {
        # `alarm_id` only when one exists: `timeline._describe` forks on its absence, and
        # a payload naming an alarm that was never raised is a false record (§11).
        **({"alarm_id": alarm["id"]} if alarm is not None
           else {"at_user_request": True}),
        "remedy": remedy.id, "approval_id": approval["id"],
        "use": spent["uses"], "result": result})
    if alarm is not None:
        central.add_inbox(
            project=project, level="info",
            title=APPLIED_INBOX_TITLE.format(subject_id=subject.get("id") or wo_id),
            body=f"{alarm['reason']}\nThe `{remedy.id}` remedy was applied: {result}\n"
                 f"Read it with: jarvis alarms show {alarm['id']}",
            wo_id=wo_id)
    else:
        central.add_inbox(
            project=project, level="info",
            title=USER_APPLIED_INBOX_TITLE.format(remedy=remedy.id,
                                                  subject_id=intent.subject_id),
            body=f"The `{remedy.id}` remedy was applied: {result}\n"
                 f"Read it with: jarvis wo show {intent.subject_id}",
            wo_id=wo_id)

    # `supervisor._apply`'s ack path exactly: `ops.ack_os_flag` takes down the flag the
    # alarm raised and re-raises whatever else is blocking. Never `ack_attention` (that
    # is the user's blanket dismissal and would bury blockers they never saw — issue
    # 573), never `clear_attention` (that discards their earlier dismissals).
    try:
        ops.ack_os_flag(wo_id)
    except ops.OpsError as exc:
        log.info("remedy applied on %s; attention left up: %s", wo_id, exc)
    log.info("remedy %s applied for %s: %s", remedy.id,
             intent.alarm_id or intent.subject_id, result)
    return result
