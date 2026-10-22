"""The only module in the OS that acts on a work order it was not asked to touch (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §5.)"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from functools import lru_cache
from typing import Any, Callable, Mapping

# User decision #11: every borrowed field a reviewer reads is tagged and bounded here
from .provenance import (CONTEXT_LIMIT, DESCRIPTION_LIMIT, EVIDENCE_LIMIT, TITLE_LIMIT,
                         Borrowed, borrowed_context, borrowed_sections)

log = logging.getLogger("remedies")

#: The gate a remedy rides
GATE_KIND = "self_heal"

#: `timeline._message_label`'s contract with §6
MESSAGE_SOURCE = "supervisor"

#: A grant covers ONE application
GRANT_USES = 1

#: What `approvals.command` holds for a `self_heal` row
INTENT = "heal {alarm_id}: {remedy} {subject_id} — {argument}"

#: `INTENT`'s sibling for a USER-INITIATED fix (docs/superpowers/specs/2026-09-24-order-observability.md)
USER_INTENT = "apply {remedy} to {subject_id}, asked for by the user — {argument}"

#: The parser for the line above, and the two must stay in step — a round-trip test pins them
_USER_INTENT_RE = re.compile(
    r"^apply (?P<remedy>[A-Za-z_][A-Za-z0-9_-]*) to (?P<subject_id>\S+), "
    r"asked for by the user — (?P<argument>.*)$", re.DOTALL)


def user_intent(subject_id: str, remedy: str, argument: str) -> str:
    """`approvals.command` for one user-initiated fix"""
    return USER_INTENT.format(remedy=remedy, subject_id=subject_id,
                              argument=(argument or "").strip())


def parse_user_intent(command: str | None) -> dict[str, str] | None:
    """`{"remedy", "subject_id", "argument"}`, or None when this grant is not one of ours"""
    match = _USER_INTENT_RE.match(command or "")
    if match is None:
        return None
    return {"remedy": match["remedy"], "subject_id": match["subject_id"],
            "argument": match["argument"]}


#: The one message a nudge sends
NUDGE = """[supervisor] The OS flagged this order as possibly unhealthy and was given \
permission to ask you about it: {reason}

{argument}

Reply on the record with where you are: what you are working on right now, what you are
waiting on, and whether you are stuck. This is a question, not an instruction — do not
change course on the strength of it."""

#: The same message when the USER asked for it (§11)
USER_NUDGE = """[the OS] The user asked us to check in with you: {reason}

{argument}

Reply on the record with where you are: what you are working on right now, what you are
waiting on, and whether you are stuck. This is a question, not an instruction — do not
change course on the strength of it."""

#: `verdict_reason` on an alarm whose proposal the reviewer refused
REFUSED_REASON = "the {remedy} remedy was {verdict} by {by}: {reason}"

#: How much of the judge's argument becomes the new work order's TITLE
TITLE_CHARS = 120

#: The fix order's brief
FIX_BRIEF = """The supervisor raised this off cost alarm {alarm_id}.

WHAT IT SAW: {reason}

WHAT IT WANTS DONE: {argument}

Read the alarm before you start — `jarvis alarms show {alarm_id}` — and fix the ROOT
CAUSE rather than the symptom it was measured by. A separate work order is already filed
to ship your fix once this one completes, so finish behind a pull request as usual and do
not release anything yourself."""

#: The same brief when the USER asked (§11)
USER_FIX_BRIEF = """The user asked for this off the diagnosis of {subject_id}.

WHAT THEY SAW: {reason}

WHAT THEY WANT DONE: {argument}

Read the diagnosis before you start — `jarvis wo why {subject_id}` — and fix the ROOT
CAUSE rather than the symptom it was noticed by. A separate work order is already filed
to ship your fix once this one completes, so finish behind a pull request as usual and do
not release anything yourself."""

#: `_apply_file_work_order`'s refusal
STEWARD_REFUSED = ("{origin} proposed `file_work_order`, and the steward declined: "
                   "{reason}")
SELF_DISPATCH_OFF = ("{origin} proposed `file_work_order`, but os.autonomy.self_dispatch is "
                     "off: an alarm files no work nobody typed — nothing was done")
NO_ARGUMENT = ("{origin} proposed `file_work_order` with no argument — there is nothing "
               "to brief a work order with")

#: The ship order's title and brief
SHIP_TITLE = "Ship the fix for {alarm_id} to production"
#: The same title with the subject in place of the alarm, for §11's alarm-less path
USER_SHIP_TITLE = "Ship the fix for {subject_id} to production"
SHIP_BRIEF = """This order ships {fix_id}, and runs only after it completes.

Cut a release from ALREADY-MERGED main and deploy it with the `shipit` skill, which is the
only supported route. The release is a PRIVILEGED ACTION: you will be blocked, the request
is filed, and a reviewer decides. That is expected — make the case in the request rather
than looking for a way around it.

Why it is worth shipping promptly: {reason}"""

#: Inbox titles
APPROVED_INBOX_TITLE = "{by} approved the {remedy} remedy on {alarm_id}"
APPLIED_INBOX_TITLE = "The supervisor acted on {subject_id}"
REFUSED_INBOX_TITLE = "A remedy was refused, and {alarm_id} still needs you"

#: The same three for §11, naming the SUBJECT because there is no alarm to name
USER_APPROVED_INBOX_TITLE = "{by} approved the {remedy} fix on {subject_id}"
USER_APPLIED_INBOX_TITLE = "The {remedy} fix you asked for on {subject_id} was applied"
USER_REFUSED_INBOX_TITLE = ("The {remedy} fix on {subject_id} was refused — nothing was "
                            "done")


#: The types a remedy parameter may have, as STRINGS keying Python types (docs/superpowers/specs/2026-09-27-self-evolution.md §4)
PARAM_TYPES: dict[str, type] = {"str": str, "int": int, "bool": bool}

#: The closed TEMPLATE table `raise_attention` renders from: our own short key -> the `invariants`... (docs/superpowers/specs/2026-09-27-self-evolution.md)
ATTENTION_TEMPLATES: dict[str, tuple[str, tuple[str, ...]]] = {
    "pr_closed": ("PR_CLOSED_BLOCKER", ()),
    "unlanded": ("UNLANDED_BLOCKER", ()),
    "validation_stuck": ("VALIDATION_STUCK_BLOCKER", ()),
    "sha_moved": ("SHA_MOVED_BLOCKER", ()),
    "automerge_denied": ("AUTOMERGE_DENIED_BLOCKER", ()),
    "dead_dependency": ("DEAD_DEPENDENCY_BLOCKER", ()),
    "message_stuck": ("MESSAGE_STUCK_BLOCKER", ()),
    "idle_no_finish": ("IDLE_NO_FINISH_BLOCKER", ()),
    "auth": ("AUTH_BLOCKER", ()),
    # Two keys and no slot: §1 of... (docs/superpowers/specs/2026-10-14-an-unanswered-refusal-must-name-itself.md)
    "refusal_unanswered": ("REFUSAL_UNANSWERED_BLOCKER", ()),
    "refusal_unanswered_document": ("REFUSAL_UNANSWERED_DOC_BLOCKER", ()),
}

#: The ONLY shape a value may have to reach a rendered sentence: a record id, or an issue number
_ID_PATTERN = re.compile(r"\A(?:(?:wo|fo|io|al)-[0-9a-f]{4,}|#[0-9]+)\Z")


def _is_id(value: Any) -> bool:
    """True for `wo-`/`fo-`/`io-`/`al-` plus hex, or `#<digits>`"""
    return isinstance(value, str) and bool(_ID_PATTERN.match(value))


@lru_cache(maxsize=None)
def _attention_sentence(name: str) -> str:
    """The `invariants` constant, fetched lazily and cached"""
    from . import invariants

    return getattr(invariants, name)


def render_attention(key: str, params: dict[str, Any] | None = None) -> str:
    """The sentence a `raise_attention` flag carries, BUILT IN CODE from a template key"""
    template = ATTENTION_TEMPLATES.get(key)
    if template is None:
        raise RemedyRefused(
            f"{key!r} is not an attention template — `raise_attention` RENDERS its reason "
            f"from a closed table and never relays one, so a reason that is not a key of "
            f"that table cannot be flagged. Use one of: "
            f"{', '.join(ATTENTION_TEMPLATES)}")
    name, slots = template
    sentence = _attention_sentence(name)
    if not slots:
        # The constant ITSELF, untouched: no `.format` call
        return sentence
    given = params or {}
    values: dict[str, str] = {}
    for slot in slots:
        value = given.get(slot)
        if not _is_id(value):
            raise RemedyRefused(
                f"{slot} was given as {value!r}, which is not a record id — an attention "
                f"reason interpolates ids and nothing else, so this was refused rather "
                f"than rendered")
        values[slot] = str(value)
    return sentence.format(**values)


class RemedyRefused(Exception):
    """Nothing was done"""


@dataclass(frozen=True)
class Param:
    """One parameter a remedy takes, and everything the rule that names it must satisfy"""

    name: str
    type: str              # a key of PARAM_TYPES, never a Python type — see that table
    required: bool = True
    help: str = ""

    #: The closed set of values this parameter may take; empty means unconstrained
    choices: tuple[str, ...] = ()


def _always_possible(pstore: Any, subject: dict[str, Any],
                     params: dict[str, Any]) -> str:
    """The default `can_apply`: this act has no precondition a local read can decide"""
    return ""


@dataclass(frozen=True)
class Remedy:
    """One thing the OS may do, and everything a reviewer needs to rule on it"""

    id: str
    headline: str          # what it does, in the terms a reviewer needs
    blast: str             # what it touches, and what it cannot undo
    subjects: tuple[str, ...]
    apply: Callable[..., str]

    #: The `ops.waiting_on` slugs this remedy clears
    covers: tuple[str, ...] = ()

    #: What a rule must supply to name this act
    params: tuple[Param, ...] = ()

    #: `can_apply(pstore, subject, params) -> ""` when the act is CURRENTLY possible
    can_apply: Callable[[Any, dict[str, Any], dict[str, Any]], str] = field(
        default=_always_possible)

    def check_params(self, params: dict[str, Any] | None) -> str:
        """`""` when this mapping satisfies the schema, else ONE sentence naming EVERY problem at once"""
        given = dict(params or {})
        schema = {p.name: p for p in self.params}
        problems: list[str] = []
        missing = [p.name for p in self.params if p.required and p.name not in given]
        if missing:
            problems.append(f"{', '.join(missing)} is required and was not given")
        unknown = [name for name in given if name not in schema]
        if unknown:
            problems.append(f"{', '.join(unknown)} is not a parameter it takes")
        wrong = []
        for name, value in given.items():
            param = schema.get(name)
            if param is None:
                continue
            want = PARAM_TYPES[param.type]
            ok = (isinstance(value, bool) if want is bool else
                  isinstance(value, int) and not isinstance(value, bool) if want is int
                  else isinstance(value, want))
            if not ok:
                wrong.append(f"{name} was given as {type(value).__name__} where "
                             f"{param.type} is wanted")
            elif param.choices and value not in param.choices:
                wrong.append(f"{name} was given as {value!r}, which is not one of "
                             f"{', '.join(param.choices)}")
        problems += wrong
        if not problems:
            return ""
        takes = ", ".join(f"{p.name} ({p.type}"
                          f"{'' if p.required else ', optional'})" for p in self.params)
        return (f"`{self.id}` takes {takes or 'no parameters'} and this rule's "
                f"parameters do not fit: {'; '.join(problems)}")


def subject_kind(alarm: dict[str, Any]) -> str:
    """`work_order` or `feature_order` for one alarm row"""
    return str(alarm.get("subject_kind") or "work_order")


def _carrier_id(pstore: Any, alarm: dict[str, Any]) -> str | None:
    """The work order a message about this alarm's subject is delivered to, or None"""
    if subject_kind(alarm) == "work_order":
        return str(alarm["wo_id"])
    carrier = pstore.carrier_for_feature(alarm.get("fo_id"))
    return str(carrier["id"]) if carrier else None


def subject_kind_of(subject: dict[str, Any]) -> str:
    """`work_order` or `feature_order` for a SUBJECT dict — §11's path has no alarm row"""
    return "work_order" if str(subject.get("id") or "").startswith("wo-") else \
        "feature_order"


def _carrier_for_subject(pstore: Any, subject: dict[str, Any]) -> str | None:
    """`_carrier_id`'s sibling for a subject with no alarm"""
    sid = str(subject.get("id") or "")
    if subject_kind_of(subject) == "work_order":
        return sid or None
    carrier = pstore.carrier_for_feature(sid)
    return str(carrier["id"]) if carrier else None


@dataclass(frozen=True)
class Intent:
    """Exactly what a handler needs, from EITHER authority — §11 (Neo q795)"""

    remedy: str
    subject_id: str
    carrier_id: str | None
    argument: str
    reason: str
    alarm_id: str | None = None
    fo_id: str | None = None

    @classmethod
    def from_alarm(cls, pstore: Any, alarm: dict[str, Any]) -> Intent:
        """Today's supervisor path"""
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
        """§11's path: the remedy and the subject come from the grant's own command string"""
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
            # The carrier is the order the request was FILED on
            carrier_id=str(approval["wo_id"]),
            argument=parsed["argument"],
            reason=str(approval.get("justification") or ""),
            alarm_id=None,
            fo_id=(parsed["subject_id"]
                   if subject_kind_of(subject) == "feature_order" else None),
        )


# -- the handlers


def _apply_nudge(pstore: Any, central: Any, project: str, subject: dict[str, Any],
                 intent: Intent, *,
                 params: dict[str, Any] | None = None) -> str:
    """Ask the session where it is"""
    wo_id = intent.carrier_id
    if wo_id is None:
        raise RemedyRefused(
            f"{intent.fo_id} has no session to speak to — nothing was sent")
    # §11: the alarm wording claims the OS flagged this order
    template = NUDGE if intent.alarm_id is not None else USER_NUDGE
    pstore.queue_message(
        wo_id,
        template.format(reason=intent.reason, argument=intent.argument),
        source=MESSAGE_SOURCE,
    )
    return (f"queued one message on {wo_id} asking it to say where it is "
            f"(delivered on its next turn)")


def _apply_unblock(pstore: Any, central: Any, project: str, subject: dict[str, Any],
                   intent: Intent, *,
                   params: dict[str, Any] | None = None) -> str:
    """Cut the dependency edges that can never clear — the DEFAULT mode, never `--all`"""
    from . import ops

    try:
        result = ops.unblock_work_order(subject["id"], project_name=project)
    except ops.OpsError as exc:
        # A refusal, not a failure: `unblock_work_order` declines when the order is not blocked or is waiting...
        raise RemedyRefused(str(exc)) from exc
    dropped = result["dropped"]
    remaining = result["still_blocked_by"]
    return (f"cut {len(dropped)} dead dependency edge(s) on {subject['id']} "
            f"({', '.join(dropped) or 'none'}); "
            f"still blocked by: {', '.join(remaining) or 'nothing'}")


def _title_from(argument: str) -> str:
    """The judge's first line, as a work order's title"""
    first = next((line.strip() for line in argument.splitlines() if line.strip()), "")
    return first[:TITLE_CHARS]


def _apply_file_work_order(pstore: Any, central: Any, project: str,
                           subject: dict[str, Any], intent: Intent, *,
                           params: dict[str, Any] | None = None,
                           decided: Any = None) -> str:
    """File the fix, then the order that ships it, joined by a dependency edge"""
    from . import ops

    from . import steward
    from .issues import self_dispatch_configured

    argument = intent.argument
    origin = intent.alarm_id or intent.subject_id
    if not argument:
        raise RemedyRefused(NO_ARGUMENT.format(origin=origin))
    # `decided`: a queued remedy the steward has since re-offered (`reoffer_file_work_order`)
    decision = decided
    if intent.alarm_id is not None and decision is None:
        # An alarm-born fix is work nobody typed: the steward's call (spec 2026-10-21 §3.2)
        from .ops import resolve_catalog

        switch = self_dispatch_configured()
        # The grant's one use is spent by now, so nothing re-asks: a `queue` waits for `drain`
        decision = steward.gate(
            steward.Candidate("remedy", "major", project, _title_from(argument),
                              evidence=(f"alarm {intent.alarm_id}",),
                              payload=asdict(intent)),
            catalog=resolve_catalog(), self_dispatch=switch)
        if not switch:
            raise RemedyRefused(SELF_DISPATCH_OFF.format(origin=origin))
        if not decision.allowed:
            raise RemedyRefused(STEWARD_REFUSED.format(origin=origin,
                                                       reason=decision.reason))
    stamp: dict[str, Any] = {"metadata": steward.stamped({}, decision),
                             "budget_usd": decision.budget_usd} if decision else {}
    reason = intent.reason.strip()
    # §11: the alarm brief sends the worker to `jarvis alarms show`
    if intent.alarm_id is not None:
        brief = FIX_BRIEF.format(alarm_id=intent.alarm_id, reason=reason,
                                 argument=argument)
        ship_title = SHIP_TITLE.format(alarm_id=intent.alarm_id)
    else:
        brief = USER_FIX_BRIEF.format(subject_id=intent.subject_id, reason=reason,
                                      argument=argument)
        ship_title = USER_SHIP_TITLE.format(subject_id=intent.subject_id)
    try:
        fix = ops.create_work_order(project, _title_from(argument), description=brief,
                                    **stamp)
        if decision:
            steward.linked(decision, fix["id"])
    except ops.OpsError as exc:
        raise RemedyRefused(str(exc)) from exc
    try:
        ship = ops.create_work_order(
            project, ship_title,
            description=SHIP_BRIEF.format(fix_id=fix["id"], reason=reason),
            depends_on=[fix["id"]], **stamp)
    except ops.OpsError as exc:
        log.warning("filed %s for %s but could not file its ship order: %s",
                    fix["id"], origin, exc)
        return (f"filed {fix['id']} to fix the root cause; its ship order could NOT be "
                f"filed ({exc}) — file one by hand with `jarvis wo create {project} "
                f"\"…\" --depends-on {fix['id']}`")
    return (f"filed {fix['id']} to fix the root cause, and {ship['id']} to ship it "
            f"(blocked until {fix['id']} completes; the release itself is still gated)")


def reoffer_file_work_order(row: Mapping[str, Any], candidate: Any,
                            decision: Any) -> str:
    """File a queued alarm-born fix the steward now has room for (`steward.drain`)"""
    from . import db, ops
    from .project_store import ProjectStore

    held = candidate.payload or {}
    intent = Intent(**{k: held[k] for k in Intent.__dataclass_fields__ if k in held})
    if not intent.argument:
        raise RemedyRefused(NO_ARGUMENT.format(origin=intent.alarm_id or row["id"]))
    paths = ops.registered_project_paths()
    if candidate.project not in paths:
        raise RemedyRefused(f"project {candidate.project!r} is not registered")
    pstore = ProjectStore(paths[candidate.project])
    try:
        # The handler acts, as for an approved grant; the steward has already decided
        result = _apply_file_work_order(pstore, None, candidate.project, {}, intent,
                                        decided=decision)
        if intent.alarm_id:
            # What `apply` does on success; the spent grant stops being re-collected too
            pstore.update_alarm(intent.alarm_id, status="acked", decided_at=db.now())
            pstore.add_event(intent.carrier_id or intent.subject_id, "remedy_applied", {
                "alarm_id": intent.alarm_id, "remedy": intent.remedy,
                "steward_decision_id": int(row["id"]), "result": result})
    finally:
        pstore.close()
    fix = re.search(r"\bwo-[0-9a-f]+", result)
    return fix.group(0) if fix else ""


# -- §4's six: the acts a rule may name, each WRAPPING an existing call


def _project_name(pstore: Any) -> str:
    """The project this store belongs to, by its registered name"""
    from . import ops

    path = Path(str(getattr(pstore, "project_path", "") or ""))
    for name, registered in ops.registered_project_paths().items():
        if Path(registered) == path:
            return name
    return path.name


def _last_known_head(pstore: Any, wo_id: str) -> str:
    """The newest head of this pull request THE OS ITSELF RECORDED, or `""`"""
    from . import db, invariants, ops

    newest_ts, newest = 0.0, ""
    for kind, field_name in ((invariants.PR_BASE_UPDATED_EVENT, "head_after"),
                             (ops.CARRY_REFUSED_EVENT, "head_sha"),
                             (invariants.REJUDGE_DECLINED_EVENT, "head_sha")):
        for event in pstore.events_of_kind(wo_id, kind):
            payload = db.from_json(event.get("payload"), {}) or {}
            sha = str(payload.get(field_name) or "")
            ts = float(event.get("ts") or 0.0)
            if sha and ts >= newest_ts:
                newest_ts, newest = ts, sha
    return newest


def _in_flight(pstore: Any, wo_id: str) -> str:
    """Why the head of this work order's branch must not move right now, or `""`"""
    from . import worker_session

    if worker_session.busy(pstore, wo_id):
        return (f"{wo_id} has a turn in flight, and moving the head under a worker "
                f"mid-turn rewrites the tree it is editing")
    if pstore.queued_messages(wo_id):
        return (f"{wo_id} has a message queued that the worker has not seen yet — it "
                f"may push in response to it, so the head is not settled")
    if pstore.validation_round_open(wo_id):
        return (f"a validation round is open on {wo_id}, and the seats are reading the "
                f"commit this would replace")
    return ""


def _can_update_branch(pstore: Any, subject: dict[str, Any],
                       params: dict[str, Any]) -> str:
    from . import ops

    wo_id = str(subject.get("id") or "")
    if not str(subject.get("pr_url") or ""):
        return (f"{wo_id} carries no pull request, so there is no branch to merge a base "
                f"into")
    flight = _in_flight(pstore, wo_id)
    if flight:
        return flight
    attempts = ops.catch_up_attempts(pstore, wo_id)
    if attempts >= ops.CATCH_UP_MAX:
        return (f"the OS has already caught {wo_id} up with its base "
                f"{ops.CATCH_UP_MAX} times, which is the bound `ops.CATCH_UP_MAX` sets — "
                f"a base moving faster than this one is caught up is a person's problem, "
                f"not another merge")
    return ""


def _apply_update_branch(pstore: Any, central: Any, project: str,
                         subject: dict[str, Any], intent: Intent, *,
                         params: dict[str, Any] | None = None) -> str:
    """Merge the base into this pull request's branch"""
    from . import ci, github, ops
    from .project_store import ProjectStore

    wo_id = str(subject["id"])
    pr_url = str(subject.get("pr_url") or "")
    if not pr_url:
        raise RemedyRefused(f"{wo_id} carries no pull request — nothing was done")
    # `can_apply` is a PREDICATE and authorises nothing: a grant is REVIEWED and then APPLIED later
    flight = _in_flight(pstore, wo_id)
    if flight:
        raise RemedyRefused(flight)
    attempts = ops.catch_up_attempts(pstore, wo_id)
    if attempts >= ops.CATCH_UP_MAX:
        raise RemedyRefused(
            f"the OS has already caught {wo_id} up with its base "
            f"{ops.CATCH_UP_MAX} times, which is the bound `ops.CATCH_UP_MAX` sets — "
            f"a base moving faster than this one is caught up is a person's problem, "
            f"not another merge")
    path = Path(str(pstore.project_path))
    try:
        fresh = github.pr_view(pr_url, cwd=path)
    except github.GitHubError as exc:
        raise RemedyRefused(
            f"the state of {pr_url} could not be read ({exc.reason}), so the OS does not "
            f"know which commit it would be merging into") from exc
    judged = ProjectStore.validated_head(
        pstore.latest_validation_round(wo_id=wo_id)) or ""
    head_before = str(getattr(fresh, "head_oid", "") or "")
    if not head_before or head_before != judged:
        raise RemedyRefused(
            f"the head of {pr_url} is {head_before[:10] or 'unreadable'} and the panel "
            f"judged {judged[:10] or 'nothing'} — the OS only updates a branch whose head "
            f"a round has read, so that the carry afterwards rests on something true")
    base = str(getattr(fresh, "base_ref", "") or "")
    base_oid = str(getattr(fresh, "base_oid", "") or "")
    try:
        ci.update_branch(pr_url, cwd=path)
    except github.GitHubError as exc:
        # The attempt is SPENT on the record (`base_heal_spent`
        ops.record_base_update_failed(pstore, subject, base=base, base_sha=base_oid,
                                      reason=exc.reason, cause=ops.BASE_UPDATE_BEHIND)
        raise RemedyRefused(
            f"GitHub refused to update {pr_url} against {base} ({exc.reason})") from exc
    try:
        fresh = github.pr_view(pr_url, cwd=path)
    except github.GitHubError as exc:
        # The update LANDED; only the read back failed
        log.info("caught %s up but could not re-read it: %s", pr_url, exc.reason)
    ops.record_base_update(pstore, subject, base=base, base_sha=base_oid,
                           head_before=head_before,
                           head_after=str(getattr(fresh, "head_oid", "") or ""),
                           checks=tuple(getattr(fresh, "failing", ()) or ()),
                           cause=ops.BASE_UPDATE_BEHIND)
    return (f"merged {base} into the branch of {pr_url}: {head_before[:10]} is now "
            f"{str(getattr(fresh, 'head_oid', '') or '')[:10]}, and its checks re-run "
            f"against the base as it is now")


def _can_carry_verdict(pstore: Any, subject: dict[str, Any],
                       params: dict[str, Any]) -> str:
    from .project_store import ProjectStore

    wo_id = str(subject.get("id") or "")
    if not str(subject.get("pr_url") or ""):
        return f"{wo_id} carries no pull request, so there is no head to carry a verdict onto"
    judged = ProjectStore.validated_head(
        pstore.latest_validation_round(wo_id=wo_id)) or ""
    if not judged:
        return (f"no round on {wo_id} has passed on a commit, so there is no verdict to "
                f"carry — a round has to be spent before one can be saved")
    head = _last_known_head(pstore, wo_id)
    if not head or head == judged:
        return (f"the newest head the OS has recorded for {wo_id} is the one round "
                f"{judged[:10]} already covers, so carrying it would record a fact that "
                f"is already on the record")
    return ""


def _apply_carry_verdict(pstore: Any, central: Any, project: str,
                         subject: dict[str, Any], intent: Intent, *,
                         params: dict[str, Any] | None = None) -> str:
    """Carry the panel's verdict onto a head that added nothing unjudged"""
    from . import branchproof, ci, github, ops
    from .project_store import ProjectStore

    wo_id = str(subject["id"])
    pr_url = str(subject.get("pr_url") or "")
    if not pr_url:
        raise RemedyRefused(f"{wo_id} carries no pull request — nothing was done")
    path = Path(str(pstore.project_path))
    latest = pstore.latest_validation_round(wo_id=wo_id)
    judged = ProjectStore.validated_head(latest) or ""
    if not judged:
        raise RemedyRefused(
            f"no round on {wo_id} has passed on a commit — there is no verdict to carry")
    try:
        pr = github.pr_view(pr_url, cwd=path)
    except github.GitHubError as exc:
        raise RemedyRefused(
            f"the state of {pr_url} could not be read ({exc.reason})") from exc
    head = str(getattr(pr, "head_oid", "") or "")
    base = str(getattr(pr, "base_ref", "") or "")
    if not head or not base or head == judged:
        raise RemedyRefused(
            f"the verdict on {wo_id} already covers {head[:10] or 'its head'}")

    def refuse(proof: str, detail: str, chain: tuple[str, ...] = ()) -> RemedyRefused:
        ops.record_carry_refusal(pstore, subject, judged=judged, head_sha=head,
                                 proof=proof, detail=detail, chain=chain)
        return RemedyRefused(detail)

    pull_ref = f"refs/pull/{pr_url.rsplit('/', 1)[-1]}/head"
    if not branchproof.fetch(path, base, pull_ref):
        raise refuse(ops.PROOF_FETCH,
                     f"`git fetch origin {base} {pull_ref}` failed, so the pull "
                     f"request's own diff cannot be compared locally")
    before = branchproof.diff_fingerprint(path, base, judged)
    after = branchproof.diff_fingerprint(path, base, head)
    if not before or not after:
        raise refuse(ops.PROOF_FETCH,
                     "the diff this branch adds on top of its merge base could not be "
                     "computed locally")
    if before != after:
        raise refuse(ops.PROOF_PATCH_ID,
                     f"the diff on {head[:10]} is not the diff the panel read, so the "
                     f"merge resolved content nobody judged")
    try:
        chain = ci.base_merge_chain(pr_url, judged, head, base_ref=base, cwd=path)
    except github.GitHubError as exc:
        raise refuse(ops.PROOF_READ,
                     f"GitHub would not say what {head[:10]} merged ({exc.reason})")
    if not chain:
        raise refuse(ops.PROOF_CHAIN,
                     f"{head[:10]} is not {judged[:10]} plus merges of {base} or of "
                     f"commits {judged[:10]} already contained")
    # The NEWEST BASE commit walked, and `""` when none was
    bases = [merged for _sha, merged, is_base in chain if is_base]
    carried = ops.carry_merge_chain(pstore, subject, judged=judged, head=head,
                                    chain=chain, base=base,
                                    base_sha=bases[-1] if bases else "",
                                    fingerprints=(before, after))
    if carried is None:
        raise refuse(ops.PROOF_CHAIN,
                     "the round this would have been carried from is no longer the "
                     "verdict on the judged commit")
    return (f"round {carried['round']} passed on {judged[:10]} and now covers "
            f"{head[:10]} — {len(chain)} merge(s), {len(bases)} of {base}, no round spent")


def _can_force_rejudge(pstore: Any, subject: dict[str, Any],
                       params: dict[str, Any]) -> str:
    """`ops.force_validation_refusal` VERBATIM, and that is the entire body on purpose"""
    from . import ops

    project = _project_name(pstore)
    return ops.force_validation_refusal(pstore, subject, project=project,
                                        cfg=ops.validation_config(project)) or ""


def _apply_force_rejudge(pstore: Any, central: Any, project: str,
                         subject: dict[str, Any], intent: Intent, *,
                         params: dict[str, Any] | None = None) -> str:
    """Open a fresh round that reads the CURRENT pull request"""
    from . import ops

    reason = str((params or {}).get("reason") or "").strip()
    if not reason:
        raise RemedyRefused(
            "`force_rejudge` was given no reason, and the reason is stored on the round "
            "so that a forced re-judgement never reads afterwards like a worker's own "
            "re-delivery — nothing was done")
    try:
        result = ops.force_validation(str(subject["id"]), reason=reason,
                                      project_name=project)
    except ops.OpsError as exc:
        raise RemedyRefused(str(exc)) from exc
    return (f"opened round {result['round']} on {subject['id']}, reading the pull "
            f"request as it is now: {reason}")


def _can_lower_attention(pstore: Any, subject: dict[str, Any],
                         params: dict[str, Any]) -> str:
    from . import invariants

    wo_id = str(subject.get("id") or "")
    if not subject.get("needs_attention"):
        return f"the attention flag on {wo_id} is already down"
    blockers = invariants.true_blockers(pstore, subject)
    if blockers:
        return (f"{wo_id} still needs the user: {'; '.join(blockers)}. Lowering the flag "
                f"would take a question out of their list that nothing else asks again")
    return ""


def _apply_lower_attention(pstore: Any, central: Any, project: str,
                           subject: dict[str, Any], intent: Intent, *,
                           params: dict[str, Any] | None = None) -> str:
    """Clear a flag whose reason `true_blockers` no longer re-derives"""
    from . import invariants

    wo_id = str(subject["id"])
    fresh = pstore.get_work_order(wo_id)
    if not fresh.get("needs_attention"):
        raise RemedyRefused(f"the attention flag on {wo_id} is already down")
    blockers = invariants.true_blockers(pstore, fresh)
    if blockers:
        raise RemedyRefused(
            f"{wo_id} still needs the user ({'; '.join(blockers)}) — the flag stays up")
    pstore.clear_attention(wo_id)
    return (f"took the attention flag down on {wo_id}: nothing in its state derives a "
            f"reason to ask the user for anything")


def _cause_refusal(cause: str) -> str:
    """Why `drop_hold` refuses every cause but the gate, NAMING the one asked for"""
    from . import holds

    named = cause or "(no cause given)"
    return (f"`drop_hold` does not end a {named} hold, and in v1 it ends only a "
            f"`{holds.GATE}` one (Neo 908) — the causes the OS derives are "
            f"{', '.join(sorted(holds.HOLD_CAUSES))}, none of the others has an end-it-now "
            f"call behind it, and no closer event may be fabricated for them because such "
            f"an event asserts something that never happened")


def _open_gate_episode(pstore: Any, wo_id: str) -> dict[str, Any] | None:
    """The approval whose `gate_requested` opened the newest gate hold still running"""
    from . import db

    open_ids = {int(a["id"]): a for a in pstore.open_approvals(wo_id)}
    newest: dict[str, Any] | None = None
    for event in pstore.events_of_kind(wo_id, "gate_requested"):
        payload = db.from_json(event.get("payload"), {}) or {}
        approval = open_ids.get(int(payload.get("approval_id") or 0))
        if approval is not None:
            newest = approval
    return newest


def _can_drop_hold(pstore: Any, subject: dict[str, Any],
                   params: dict[str, Any]) -> str:
    from . import holds

    cause = str((params or {}).get("cause") or "")
    if cause != holds.GATE:
        return _cause_refusal(cause)
    wo_id = str(subject.get("id") or "")
    approval = _open_gate_episode(pstore, wo_id)
    if approval is None:
        return (f"no gate request on {wo_id} is still open, so no gate hold episode is "
                f"running and there is nothing to end")
    if approval["status"] != "awaiting_case":
        return (f"gate request {approval['id']} on {wo_id} is {approval['status']}: a "
                f"reviewer is being shown it, and a request awaiting a verdict is ended "
                f"by deciding it (`jarvis gate approve|deny|dismiss {approval['id']}`) "
                f"rather than by abandoning it behind their back")
    return ""


def _apply_drop_hold(pstore: Any, central: Any, project: str, subject: dict[str, Any],
                     intent: Intent, *,
                     params: dict[str, Any] | None = None) -> str:
    """End a gate hold episode whose stated cause no longer holds"""
    from . import holds

    given = params or {}
    cause = str(given.get("cause") or "")
    reason = str(given.get("reason") or "").strip()
    if cause != holds.GATE:
        raise RemedyRefused(_cause_refusal(cause))
    if not reason:
        raise RemedyRefused(
            "`drop_hold` was given no reason, and the reason is the whole record of why "
            "a request nobody decided was closed — nothing was done")
    wo_id = str(subject["id"])
    approval = _open_gate_episode(pstore, wo_id)
    if approval is None:
        raise RemedyRefused(
            f"no gate request on {wo_id} is still open — there is no hold to end")
    if approval["status"] != "awaiting_case":
        raise RemedyRefused(
            f"gate request {approval['id']} on {wo_id} is {approval['status']} and is in "
            f"front of a reviewer — it is ended by a verdict, not by abandoning it")
    pstore.abandon_approval(int(approval["id"]), reason)
    return (f"closed gate request {approval['id']} on {wo_id} as abandoned, which ends "
            f"the gate hold: {reason}. The command it named is still blocked")


def _can_raise_attention(pstore: Any, subject: dict[str, Any],
                         params: dict[str, Any]) -> str:
    try:
        reason = render_attention(str((params or {}).get("template") or ""), params)
    except RemedyRefused as refused:
        # The same refusal `apply` would raise
        return str(refused)
    wo_id = str(subject.get("id") or "")
    if subject.get("needs_attention") and str(subject.get("attention_reason") or "") == reason:
        return (f"{wo_id} is already flagged with that exact reason — the user has it in "
                f"their list")
    return ""


def _apply_raise_attention(pstore: Any, central: Any, project: str,
                           subject: dict[str, Any], intent: Intent, *,
                           params: dict[str, Any] | None = None) -> str:
    """Flag attention with a reason RENDERED from the closed template table, and only so"""
    reason = render_attention(str((params or {}).get("template") or ""), params)
    wo_id = str(subject["id"])
    pstore.flag_attention(wo_id, reason)
    return f"flagged {wo_id} for the user: {reason}"


REMEDIES: dict[str, Remedy] = {
    "nudge": Remedy(
        id="nudge",
        headline="ask the session to say where it is, in one short message",
        blast="reaches a RUNNING session and costs one delivered turn, which re-sends "
              "the whole conversation at the cache-write rate. It changes no state and "
              "gives no instruction, but a message cannot be unsent and the spend "
              "cannot be undone.",
        subjects=("work_order", "feature_order"),
        # `prompt` is the ONE answer `waiting_on` reports as `stalled`
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
        # `pending` is an order held behind dependency edges
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
        # EMPTY ON PURPOSE, and it is a decision rather than an omission: this writes code and clears no...
        covers=(),
        apply=_apply_file_work_order,
    ),
    "update_branch": Remedy(
        id="update_branch",
        headline="merge the pull request's base branch into it, so its checks run "
                 "against the base as it is now",
        blast="moves the HEAD of a pull request on GitHub, which cannot be taken back "
              "without a force-push nobody here may make. It adds no authored content — "
              "the merge is GitHub's own, server-side — but it restarts CI, and the "
              "commit the panel judged is no longer the head. The verdict does not "
              "follow it automatically: `carry_verdict` proves the new head added "
              "nothing unjudged, and until it does the order waits.",
        subjects=("work_order",),
        params=(),
        can_apply=_can_update_branch,
        apply=_apply_update_branch,
    ),
    "carry_verdict": Remedy(
        id="carry_verdict",
        headline="bind the panel's existing verdict to the current head, when that head "
                 "is the judged commit plus base merges that added nothing",
        blast="writes `carried_head_sha` on a round that already passed and NEVER "
              "touches the verdict itself. Nothing is merged and nothing is sent. What "
              "it changes is what the OS believes has been judged, so a wrong carry "
              "would let a commit no seat read reach `main` — which is why the two "
              "proofs behind it are not re-implemented here and a failed one refuses on "
              "the record.",
        subjects=("work_order",),
        params=(),
        can_apply=_can_carry_verdict,
        apply=_apply_carry_verdict,
    ),
    "force_rejudge": Remedy(
        id="force_rejudge",
        headline="open a fresh validation round that reads the pull request as it is "
                 "now, with no worker and no `finished` event",
        blast="SPENDS A ROUND NUMBER like any other and runs the whole panel, which "
              "costs a model call per seat. A rejection at or past `max_rounds` comes to "
              "the user rather than to a worker, so a forced round can be the one that "
              "takes an order out of the automatic path. It reaches no session and "
              "changes no code.",
        subjects=("work_order",),
        params=(Param("reason", "str", True,
                      "why this is being re-judged, stored on the round and rendered "
                      "beside the verdict it causes — so a forced round never reads "
                      "afterwards like a worker's own re-delivery"),),
        can_apply=_can_force_rejudge,
        apply=_apply_force_rejudge,
    ),
    "lower_attention": Remedy(
        id="lower_attention",
        headline="take down an attention flag whose reason `true_blockers` no longer "
                 "derives from the work order's state",
        blast="TAKES SOMETHING OUT OF THE USER'S LIST, which is the one blast radius "
              "here that is measured in their attention rather than in tokens. It keeps "
              "their earlier dismissals (`clear_attention`, never `ack_attention`) and "
              "it refuses whenever any blocker is still derivable, so the flag it lowers "
              "is one the next reconcile tick would have lowered anyway.",
        subjects=("work_order",),
        params=(),
        can_apply=_can_lower_attention,
        apply=_apply_lower_attention,
    ),
    "drop_hold": Remedy(
        id="drop_hold",
        headline="end a GATE hold episode by closing the request nobody ever argued, so "
                 "the work order stops being held by it",
        blast="closes one gate request as ABANDONED — never approved, never denied — so "
              "it authorises nothing and the command it named stays blocked. A request "
              "that is genuinely in front of a reviewer is refused rather than closed. "
              "Only the gate cause is accepted: ending any other hold would mean writing "
              "a closing event that asserts something that never happened.",
        subjects=("work_order",),
        params=(Param("cause", "str", True,
                      "which hold to end. Only `gate` in v1 (Neo 908) — every other "
                      "cause is derived from timeline events the OS must not fabricate"),
                Param("reason", "str", True,
                      "why the request is being closed undecided; it is the whole record "
                      "of the decision and is shown wherever the gate is")),
        can_apply=_can_drop_hold,
        apply=_apply_drop_hold,
    ),
    "raise_attention": Remedy(
        id="raise_attention",
        headline="put a work order in front of the user with one of the reasons the "
                 "reconciler itself derives",
        blast="SPENDS THE USER'S ATTENTION, the scarcest thing the OS allocates, and a "
              "flag raised wrongly is read before it can be taken back. It writes no "
              "other state, reaches no session and costs no tokens. The rule names a KEY "
              "and the sentence is built in code from the closed table "
              "`invariants.true_blockers` re-derives: nothing the rule wrote reaches the "
              "user's list, and the flag still says the same thing after the next tick.",
        subjects=("work_order",),
        params=(Param("template", "str", True,
                      "WHICH attention sentence to render, as a key of `remedies."
                      "ATTENTION_TEMPLATES` — not the sentence itself. The wording is "
                      "built in code from the key, so a reason cannot be written here; "
                      "every key renders one of the sentences `invariants.true_blockers` "
                      "re-derives, and anything else is rewritten within a tick",
                      choices=tuple(ATTENTION_TEMPLATES)),),
        can_apply=_can_raise_attention,
        apply=_apply_raise_attention,
    ),
}

#: Asserted equal to `tuple(REMEDIES)`
SHIPPED_REMEDIES: tuple[str, ...] = ("nudge", "unblock", "file_work_order",
                                     "update_branch", "carry_verdict", "force_rejudge",
                                     "lower_attention", "drop_hold", "raise_attention")


def get(remedy_id: str) -> Remedy:
    """The remedy, or `KeyError`"""
    return REMEDIES[remedy_id]


@dataclass(frozen=True)
class RemedyMatch:
    """What `resolve` answers for a claimed slug: a primitive, and its parameters"""

    #: The primitive's id — always a key of `REMEDIES` (`resolve` raises otherwise)
    remedy: str
    #: The primitive's parameters
    params: Mapping[str, str]
    #: Where the row came from
    source: str


def resolve(slug: str) -> RemedyMatch | None:
    """THE SINGLE RESOLUTION POINT for an `ops.waiting_on` slug"""
    found = [r.id for r in REMEDIES.values() if slug in r.covers]
    if len(found) > 1:
        raise ValueError(f"{slug} is covered by more than one remedy: "
                         f"{', '.join(sorted(found))}")
    if not found:
        return None
    remedy_id = found[0]
    if remedy_id not in REMEDIES:
        raise ValueError(f"{slug} resolves to {remedy_id!r}, which is not a primitive "
                         f"this OS has ({', '.join(sorted(REMEDIES))})")
    return RemedyMatch(remedy=remedy_id, params={}, source="code")


def render_catalogue(allowed: tuple[str, ...]) -> str:
    """The armed remedies, as the judge and the reviewer are both shown them"""
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


# -- proposing


def _config_refusal(remedy_id: str, kind: str, cfg: Any) -> str | None:
    """Everything the CATALOG refuses, whoever asked — §11 shares this verbatim"""
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
    """Why this ALARM's proposal may not be filed, or None"""
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
    """What the reviewer reads"""
    # §11 changes WHO ASKED and nothing else about the request
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
    # Decision #11: the three fields the OS did not write are tagged and bounded
    author = "the user" if by_user else "the supervisor"
    parts = [
        f"SELF-HEAL REQUEST — gate `{GATE_KIND}`",
        lead,
        f"# The remedy\n"
        f"What it does: {remedy.headline}\n"
        f"What it touches, and what it cannot undo: {remedy.blast}\n"
        f"{actor}:",
    ]
    parts += borrowed_sections((
        Borrowed(label=actor[0].lower() + actor[1:], whose=author, text=argument,
                 limit=DESCRIPTION_LIMIT, absent="(nothing given)"),))
    parts.append(wants)
    parts += borrowed_sections((
        Borrowed(label=wants.lstrip("# ")[0].lower() + wants.lstrip("# ")[1:],
                 whose=author, text=reason, limit=DESCRIPTION_LIMIT,
                 absent="(no reason given)"),))
    # An empty evidence packet is a FACT about an alarm's proposal and worth saying
    if evidence.strip():
        parts += borrowed_sections((
            Borrowed(label="the alarm's evidence packet",
                     whose="the OS's detectors, quoting the worker's own session",
                     text=evidence, limit=EVIDENCE_LIMIT),))
    elif not by_user:
        parts.append("(the evidence packet was empty)")
    parts.append(tail)
    return "\n\n".join(parts)


def propose(pstore: Any, neo: Any, project: str, subject: dict[str, Any],
            alarm: dict[str, Any], remedy_id: str, argument: str, cfg: Any,
            *, evidence: str = "", reason: str = "", note: str = "") -> dict[str, Any]:
    """File a `self_heal` gate request for one remedy, or refuse and say why"""
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
        # No recogniser fired: this request was filed by the OS, not matched out of a command line
        matched="",
        justification=reason, evidence=evidence, max_uses=GRANT_USES,
    )
    question = neo.ask(
        project, carrier,
        _request_question(project, subject_id, remedy, argument, evidence, reason),
        # Decision #11: both halves of the context are borrowed, and both are bounded
        context=borrowed_context((
            Borrowed(label="the subject's title", whose="whoever filed it",
                     text=subject.get("title") or "", limit=TITLE_LIMIT),
            Borrowed(label="the alarm's reason", whose="the OS detector that raised it",
                     text=alarm.get("reason") or "", limit=CONTEXT_LIMIT))),
        # THE EXISTING KIND: `neo_store.Q_KINDS` already carries approval and `Daemon._deliver_gate_verdict`...
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
    """`propose` for a subject with NO ALARM (docs/superpowers/specs/2026-09-24-order-observability.md)"""
    refused = _config_refusal(remedy_id, subject_kind_of(subject), cfg)
    subject_id = str(subject.get("id") or "")
    carrier = None if refused else _carrier_for_subject(pstore, subject)
    command = user_intent(subject_id, remedy_id, argument)
    if refused is None and carrier is None:
        refused = (f"{subject_id} has no work order to carry the request — nothing has "
                   f"been planned for it yet")
    if refused is None:
        # A DUPLICATE IS REFUSED BY NUMBER
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
        # Decision #11, `propose`'s bounds — here the second half is the user's reason
        context=borrowed_context((
            Borrowed(label="the subject's title", whose="whoever filed it",
                     text=subject.get("title") or "", limit=TITLE_LIMIT),
            Borrowed(label="why the user asked for it", whose="the user",
                     text=reason, limit=CONTEXT_LIMIT))),
        kind="approval",   # the existing kind, for `propose`'s reasons
    )
    pstore.link_neo_question(approval["id"], question["id"])
    pstore.add_event(carrier, "remedy_proposed", {
        # NO `alarm_id` KEY, and `timeline._describe` forks on its absence: an entry that said "the...
        "remedy": remedy_id, "approval_id": approval["id"],
        "neo_question_id": question["id"], "subject_id": subject_id,
        "argument": (argument or "").strip(), "reason": reason,
        "at_user_request": True})
    log.info("user fix %s proposed for %s as gate request %s", remedy_id, subject_id,
             approval["id"])
    return {"proposed": True, "reason": reason, "approval": approval,
            "question": question}


def user_grants(pstore: Any) -> list[dict[str, Any]]:
    """The approved, unexpired, unspent §11 grants no alarm claims"""
    out: list[dict[str, Any]] = []
    # SCOPED TO THE KIND IN SQL
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
    """The work order or feature order one §11 grant is about, or None if it is gone"""
    parsed = parse_user_intent(approval["command"])
    if parsed is None:
        return None
    sid = parsed["subject_id"]
    try:
        return (pstore.get_work_order(sid) if sid.startswith("wo-")
                else pstore.get_feature_order(sid))
    except KeyError:
        return None


# -- the verdict, and applying


def record_verdict(pstore: Any, approval: dict[str, Any], verdict: str, reason: str,
                   decided_by: str, central: Any = None, project: str = "") -> None:
    """What a `self_heal` verdict does INSTEAD of messaging the worker"""
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
            # §11: an alarm-less grant is the USER's own request
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

        # Denied or dismissed
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
    """The verdict on a §11 fix: an inbox row either way, and NEVER a queued message"""
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
    """Put the unresolved alarm back in front of the user"""
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
    """Run one approved remedy, once, and record what it did"""
    from . import db, gates, ops

    # `alarm=None` is §11: the remedy and the subject come from the GRANT
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
    # Neo 904's forward-compatible params
    if alarm is not None:
        params = db.from_json(alarm.get("remedy_params"), {}) or {}
    else:
        params = {}
    result = remedy.apply(pstore, central, project, subject, intent, params=params)
    wo_id = approval["wo_id"]
    if alarm is not None:
        pstore.update_alarm(alarm["id"], status="acked", decided_at=db.now())
    pstore.add_event(wo_id, "remedy_applied", {
        # `alarm_id` only when one exists: `timeline._describe` forks on its absence
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

    # `supervisor._apply`'s ack path exactly: `ops.ack_os_flag` takes down the flag the alarm raised and...
    try:
        ops.ack_os_flag(wo_id)
    except ops.OpsError as exc:
        log.info("remedy applied on %s; attention left up: %s", wo_id, exc)
    log.info("remedy %s applied for %s: %s", remedy.id,
             intent.alarm_id or intent.subject_id, result)
    return result
