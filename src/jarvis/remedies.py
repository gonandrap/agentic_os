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
from dataclasses import dataclass, field
from pathlib import Path
from functools import lru_cache
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

#: The ship order's title and brief, and these words are the OS's rather than the judge's
#: on purpose: "ship it" is the same job every time, and a judge writing its own release
#: instructions each time is a judge inventing a procedure the `shipit` skill already owns.
SHIP_TITLE = "Ship the fix for {alarm_id} to production"
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


#: The types a remedy parameter may have, as STRINGS keying Python types.
#:
#: docs/specs/2026-09-27-self-evolution.md §4 addition 1. A string rather than a bare
#: `type` object because the sibling section validates a remedy row's parameters as JSON
#: read back out of a database column: the schema has to survive a round trip through
#: `json.dumps`, and `int` does not. Closed for the reason the registry itself is closed
#: (kn-6c252734) — a schema that accepts any type name accepts a typo, and a typo in a
#: schema is a validation that silently passes.
PARAM_TYPES: dict[str, type] = {"str": str, "int": int, "bool": bool}

#: The closed TEMPLATE table `raise_attention` renders from: our own short key -> the
#: `invariants` constant that spells the sentence, and the id slots that sentence takes.
#:
#: §4 of docs/specs/2026-09-27-self-evolution.md as corrected by spec commit 2920441:
#: **`raise_attention` renders its reason; it never relays one.** The parameter a rule
#: writes is a KEY, and the sentence the user reads is built in code from that key — no
#: free-text parameter, and no parameter interpolated into the command the reason tells
#: them to type. The distinction is not pedantry about a table that was already closed:
#: a rule row is data an INVESTIGATION wrote, an investigation reads a worker's prose,
#: and a remedy that passed a row's string through to the attention list would be an
#: unbroken path from that prose to a command the user is being told to run. Passing a
#: key breaks the path at the type level, because a key is compared and never printed.
#:
#: THE KEYS ARE OUR OWN LITERALS AND THAT IS THE LAYERING. This module has no `jarvis`
#: import at module scope — `ops`, `db`, `gates`, `invariants` and `supervisor` are all
#: imported inside function bodies — because the sibling rules section is specified as a
#: leaf that may import it beside stdlib, `db` and `catalog`. A table keyed on the
#: constants themselves would drag `project_store`, `neo_store`, `budget`,
#: `worker_session`, `catalog` and `automerge` into every importer, measured at 0.127s of
#: eager import. So the table holds the constant's NAME and `render_attention` fetches
#: the object lazily by `getattr`: the prose is never re-spelled here, and a constant
#: somebody renames fails loudly with `AttributeError` rather than quietly dropping a
#: reason out of the closed set — a silent widening in the direction that matters.
#:
#: The sentences belong to `invariants.true_blockers`, which owns
#: `work_orders.attention_reason`: INV-ATTENTION-REASON re-derives that column every
#: reconcile tick and REWRITES any reason it cannot derive. A flag outside this table is
#: therefore not a flag that says the wrong thing — it is a flag that says something
#: DIFFERENT two minutes later, with no record of what it first said.
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
}

#: The ONLY shape a value may have to reach a rendered sentence: a record id, or an issue
#: number. Deliberately narrower than "a short string with no spaces" — the point of the
#: correction is that the user's attention list can never carry a span an investigation
#: chose, and a permissive validator is a free-text parameter with extra steps.
_ID_PATTERN = re.compile(r"\A(?:(?:wo|fo|io|al)-[0-9a-f]{4,}|#[0-9]+)\Z")


def _is_id(value: Any) -> bool:
    """True for `wo-`/`fo-`/`io-`/`al-` plus hex, or `#<digits>`. Nothing else."""
    return isinstance(value, str) and bool(_ID_PATTERN.match(value))


@lru_cache(maxsize=None)
def _attention_sentence(name: str) -> str:
    """The `invariants` constant, fetched lazily and cached. See `ATTENTION_TEMPLATES`
    for why this is a function call rather than a module-level reference."""
    from . import invariants

    return getattr(invariants, name)


def render_attention(key: str, params: dict[str, Any] | None = None) -> str:
    """The sentence a `raise_attention` flag carries, BUILT IN CODE from a template key.

    Refuses an unknown key, and interpolates ONLY the slots the template declares, only
    after each value passes `_is_id`. **EVERY OTHER PARAM IS IGNORED** — that is the
    whole of the correction in spec commit 2920441, and it is asserted as such: there
    must be no route by which a value reaches this sentence except a declared, validated
    slot, so the params mapping is read by slot name and never iterated.

    In v1 every template declares `slots=()` and nothing is interpolated at all; the
    validator and the slot machinery exist because the next template will name an id, and
    `tests/test_remedies.py` grades them on a synthetic template today so that the day it
    lands is not the day the rule is written.
    """
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
        # The constant ITSELF, untouched: no `.format` call, so a brace that turns up in
        # one of these sentences can never become an interpolation site by accident.
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
    """Nothing was done. Raised by `apply` for every reason a remedy must not run, so a
    caller cannot mistake a refusal for a no-op that succeeded."""


@dataclass(frozen=True)
class Param:
    """One parameter a remedy takes, and everything the rule that names it must satisfy.

    `help` is not decoration: the rule author is a model, and the schema is the only
    place the OS gets to say what a parameter MEANS before the rule is written. A
    parameter with no help is a free-text field with a name.
    """

    name: str
    type: str              # a key of PARAM_TYPES, never a Python type — see that table
    required: bool = True
    help: str = ""

    #: The closed set of values this parameter may take; empty means unconstrained.
    #:
    #: Spec commit 2920441, §4: it is what makes "an unknown key is refused ON INSERT"
    #: true without this module knowing the rule registry exists. The sibling validates a
    #: remedy row against `Remedy.params` before it is stored, so a `raise_attention`
    #: template key outside this tuple never reaches a database row — the refusal is one
    #: `check_params` call the sibling already makes, rather than a second validator that
    #: would have to be kept in step with this one.
    choices: tuple[str, ...] = ()


def _always_possible(pstore: Any, subject: dict[str, Any],
                     params: dict[str, Any]) -> str:
    """The default `can_apply`: this act has no precondition a local read can decide.

    The three shipped remedies keep it, and that is a statement rather than an omission —
    their refusals were written into `apply` and were reviewed there. Moving one up here
    would change WHEN it is decided (the mechanical gate, a tick before the act) without
    anybody reviewing whether the fact is still true at the moment of acting.
    """
    return ""


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

    #: What a rule must supply to name this act. Empty for a primitive that takes none,
    #: declared explicitly so "no parameters" and "nobody wrote the schema" are the same
    #: readable fact. Spec §4 addition 1.
    params: tuple[Param, ...] = ()

    #: `can_apply(pstore, subject, params) -> ""` when the act is CURRENTLY possible, or
    #: a sentence saying why not. Spec §4 addition 2.
    #:
    #: READ-ONLY, and the contract is stronger than "does not write": no network call and
    #: no subprocess either. It runs on every candidate a rule matches, a tick before
    #: anything is proposed, so a predicate that reached `gh` would put the fleet's poll
    #: rate through GitHub's rate limiter — and the facts that genuinely need `gh` are
    #: checked inside `apply`, where they are re-read at the moment of acting anyway.
    #:
    #: IT AUTHORISES NOTHING. `""` means possible, not permitted: the catalog allow-list,
    #: the `self_heal` grant and the AST walk over this file each still refuse on their
    #: own, and any one of them is enough.
    can_apply: Callable[[Any, dict[str, Any], dict[str, Any]], str] = field(
        default=_always_possible)

    def check_params(self, params: dict[str, Any] | None) -> str:
        """`""` when this mapping satisfies the schema, else ONE sentence naming EVERY
        problem at once.

        Every problem, following `plans.parse_plan` and `findings.parse_report`: the
        author of a rule is a model, and a parser that reports the first fault makes it
        fix one thing per round trip — each of which is a whole session. The four kinds
        are reported together because they are usually one mistake (a renamed parameter
        is a missing name AND an unknown one).

        The fourth kind is a value outside `Param.choices`, added by spec commit 2920441:
        it is the insert-time half of "`raise_attention` renders its reason and never
        relays one", since the sibling grades a remedy row here before storing it.

        A BOOL IS NOT AN INT HERE, whatever `isinstance` says. `True` where a count
        belongs is a mistake worth naming, and Python's own subclassing is the reason it
        would otherwise pass.
        """
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
# file's AST for `send_message`, `queue_message`, `unblock_work_order`,
# `create_work_order`, `cancel`, `cancel_work_order` and `set_status` — and, since §4 of
# docs/specs/2026-09-27-self-evolution.md widened the registry, `update_branch`,
# `abandon_approval`, `flag_attention`, `clear_attention`, `force_validation`,
# `carry_merge_chain`, `record_base_update` and `record_base_update_failed` — as an
# attribute or a bare name, and requires the nearest enclosing function to be one of
# `REMEDIES[*].apply`. Reachability is not decidable from an AST; enclosure is, and it is
# the property that actually matters.
#
# EVERY HANDLER TAKES A TRAILING KEYWORD-ONLY `params`, defaulting to empty (Neo 904).
# The three below ignore it and say so: a handler that takes no parameters still has to
# accept the keyword, because `apply` passes one signature to all of them and a schema
# that is empty today is the shape a later rule widens.


def _apply_nudge(pstore: Any, central: Any, project: str, subject: dict[str, Any],
                 alarm: dict[str, Any], *,
                 params: dict[str, Any] | None = None) -> str:
    """Ask the session where it is. One message, and nothing else.

    NOT `ops.send_message`, and this is not a style choice: that function ends with
    `clear_attention`, which sets `acknowledged_blockers` to NULL and so discards the
    user's own earlier dismissals — before `apply` ever reaches `ops.ack_os_flag`.
    `ops.nudge_pr_repair` is the precedent for an OS-authored message and does the two
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
                   alarm: dict[str, Any], *,
                   params: dict[str, Any] | None = None) -> str:
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
                           subject: dict[str, Any], alarm: dict[str, Any], *,
                           params: dict[str, Any] | None = None) -> str:
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

    argument = (alarm.get("remedy_argument") or "").strip()
    if not argument:
        raise RemedyRefused(
            f"{alarm['id']} proposed `file_work_order` with no argument — there is "
            f"nothing to brief a work order with")
    reason = (alarm.get("reason") or "").strip()
    try:
        fix = ops.create_work_order(
            project, _title_from(argument),
            description=FIX_BRIEF.format(alarm_id=alarm["id"], reason=reason,
                                         argument=argument))
    except ops.OpsError as exc:
        raise RemedyRefused(str(exc)) from exc
    try:
        ship = ops.create_work_order(
            project, SHIP_TITLE.format(alarm_id=alarm["id"]),
            description=SHIP_BRIEF.format(fix_id=fix["id"], reason=reason),
            depends_on=[fix["id"]])
    except ops.OpsError as exc:
        log.warning("filed %s for %s but could not file its ship order: %s",
                    fix["id"], alarm["id"], exc)
        return (f"filed {fix['id']} to fix the root cause; its ship order could NOT be "
                f"filed ({exc}) — file one by hand with `jarvis wo create {project} "
                f"\"…\" --depends-on {fix['id']}`")
    return (f"filed {fix['id']} to fix the root cause, and {ship['id']} to ship it "
            f"(blocked until {fix['id']} completes; the release itself is still gated)")


# -- §4's six: the acts a rule may name, each WRAPPING an existing call ----------------
#
# docs/specs/2026-09-27-self-evolution.md §4. Every one of these is a thin wrapper around
# a call the daemon already makes, in the daemon's own order, and NOT a second
# implementation of it. The catch-up proof is the case that matters (kn-907c9a61):
# parentage read from GitHub plus content hashed locally with `--full-index --no-ext-diff
# --no-textconv`, never `patch-id`. A second implementation of that proof is a fail-open,
# and a reviewed one at that — so `_apply_update_branch` and `_apply_carry_verdict` call
# `branchproof`, `ci` and `ops` in exactly the sequence `Daemon._catch_up_with_base` and
# `Daemon._carry_catch_up` call them in.
#
# `daemon` is NOT imported here and must not be: the layering runs daemon -> remedies,
# and a remedy reaching back up it would make the acting module depend on the loop that
# schedules it.


def _project_name(pstore: Any) -> str:
    """The project this store belongs to, by its registered name.

    A LOCAL READ and no network: `registered_project_paths` opens the central store and
    nothing else, which keeps `can_apply`'s read-only contract. The name matters because
    `ops.force_validation_refusal` writes it into the sentence it refuses with, and a
    refusal naming the wrong project is a refusal a user cannot act on. The directory
    name is the fallback for a project the central store has never seen — a test store,
    or a checkout registered under another name — and it is better than an empty string
    in the one place it shows up.
    """
    from . import ops

    path = Path(str(getattr(pstore, "project_path", "") or ""))
    for name, registered in ops.registered_project_paths().items():
        if Path(registered) == path:
            return name
    return path.name


def _last_known_head(pstore: Any, wo_id: str) -> str:
    """The newest head of this pull request THE OS ITSELF RECORDED, or `""`.

    `can_apply` may not ask GitHub, so this is the only head a precondition can compare a
    verdict against. Three event kinds carry one, and all three are read because they are
    written by different paths over the same commit: the OS's own base update, a carry it
    refused, and a re-judge it declined. Newest by timestamp wins.

    It is DELIBERATELY not authoritative — `apply` re-reads the real head from GitHub
    before it touches anything. What this answers is the cheaper question the precondition
    actually asks: is there any reason to think the head has moved past the verdict?
    """
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
    """Why the head of this work order's branch must not move right now, or `""`.

    `Daemon._catch_up_with_base`'s guards 1 and 2, verbatim in effect: never move the head
    under a worker mid-turn or with a message it has not seen, and never move it beneath
    an open round (Neo question 283 — the seats are reading the commit).
    """
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
                         subject: dict[str, Any], alarm: dict[str, Any], *,
                         params: dict[str, Any] | None = None) -> str:
    """Merge the base into this pull request's branch. `Daemon._catch_up_with_base`'s act.

    THE HEAD IS RE-READ IMMEDIATELY BEFORE THE UPDATE and the act is refused unless it is
    still the commit the panel judged. `gh pr update-branch` has no `--match-head-commit`,
    so the window between reading and updating cannot be closed here — it is NARROWED
    here and closed by the carry's proof (a) afterwards. Without the re-read the OS would
    merge a base into a commit no round has read and then carry a verdict onto the result.
    """
    from . import ci, github, ops
    from .project_store import ProjectStore

    wo_id = str(subject["id"])
    pr_url = str(subject.get("pr_url") or "")
    if not pr_url:
        raise RemedyRefused(f"{wo_id} carries no pull request — nothing was done")
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
        # The attempt is SPENT on the record (`base_heal_spent`, `catch_up_attempts`), and
        # then refused: a failed update leaves the pull request exactly where it was, so
        # `RemedyRefused` is the honest verdict — nothing was done to the branch.
        ops.record_base_update_failed(pstore, subject, base=base, base_sha=base_oid,
                                      reason=exc.reason, cause=ops.BASE_UPDATE_BEHIND)
        raise RemedyRefused(
            f"GitHub refused to update {pr_url} against {base} ({exc.reason})") from exc
    try:
        fresh = github.pr_view(pr_url, cwd=path)
    except github.GitHubError as exc:
        # The update LANDED; only the read back failed. Recorded with the head unmoved,
        # `heal_inherited_failure`'s direction: the next poll reads the real one, and a
        # missing record of an update that happened is the worse of the two errors.
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
                         subject: dict[str, Any], alarm: dict[str, Any], *,
                         params: dict[str, Any] | None = None) -> str:
    """Carry the panel's verdict onto a head that added nothing unjudged.

    `Daemon._carry_catch_up`'s body, call for call. THE PROOF IS NOT RE-IMPLEMENTED
    (kn-907c9a61): `branchproof.fetch` first because both halves need it,
    `diff_fingerprint` on the judged commit and on the live head for proof (b), and
    `ci.base_merge_chain` for proof (a). The rule stays in `ops.carry_merge_chain`, which
    reads no config and no round budget, and is why an order with its rounds spent
    recovers with nothing typed.

    EVERY REFUSAL IS RECORDED WITH THE PROOF THAT FAILED. A bare "not carried" cannot tell
    "someone resolved a conflict" from "GitHub would not answer", and those want opposite
    responses from the reader.
    """
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
    # The NEWEST BASE commit walked, and `""` when none was — Neo question 806: a walked
    # merge may have brought in the branch's own lineage rather than the base's.
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
    """`ops.force_validation_refusal` VERBATIM, and that is the entire body on purpose.

    That function already returns exactly the sentence this field wants, and it is THE
    home of the rule: `jarvis validation force` raises it and the dashboard renders its
    control disabled beside it. Two carefully written copies of a predicate pass every
    behavioural test and drift anyway (kn-4ea33fe6), so the sharing has to be structural.
    """
    from . import ops

    project = _project_name(pstore)
    return ops.force_validation_refusal(pstore, subject, project=project,
                                        cfg=ops.validation_config(project)) or ""


def _apply_force_rejudge(pstore: Any, central: Any, project: str,
                         subject: dict[str, Any], alarm: dict[str, Any], *,
                         params: dict[str, Any] | None = None) -> str:
    """Open a fresh round that reads the CURRENT pull request. `jarvis validation force`.

    The reason is REQUIRED and is stored on the round, for the command's own reason: a
    forced round that read as organic afterwards is the defect the command was written to
    remove — and a round the OS forced ITSELF, unattributed, would be worse still.
    """
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
                           subject: dict[str, Any], alarm: dict[str, Any], *,
                           params: dict[str, Any] | None = None) -> str:
    """Clear a flag whose reason `true_blockers` no longer re-derives.

    THE ROW IS RE-READ rather than trusting `subject`, because the caller's copy was read
    at the top of a tick and a worker turn can settle in between. `true_blockers` is asked
    of the FRESH row for the same reason: this is the one remedy that takes something away
    from the user, so the fact it rests on has to be the current one.
    """
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
    """Why `drop_hold` refuses every cause but the gate, NAMING the one asked for.

    NEO 908, and the reason is structural rather than cautious: a hold has no row. Every
    episode in `holds.py` is derived from a PAIR of timeline events, so ending one means
    writing its closing event — and for a question, a round, a budget or a transport
    pause, that event asserts something that did not happen (a `neo_answered` nobody
    answered, a `validation_passed` no panel returned). The gate is the one cause with a
    real end-it-now call behind it, `ProjectStore.abandon_approval`, which is honest about
    what it does: it closes the request as never decided and authorises nothing.
    """
    from . import holds

    named = cause or "(no cause given)"
    return (f"`drop_hold` does not end a {named} hold, and in v1 it ends only a "
            f"`{holds.GATE}` one (Neo 908) — the causes the OS derives are "
            f"{', '.join(sorted(holds.HOLD_CAUSES))}, none of the others has an end-it-now "
            f"call behind it, and no closer event may be fabricated for them because such "
            f"an event asserts something that never happened")


def _open_gate_episode(pstore: Any, wo_id: str) -> dict[str, Any] | None:
    """The approval whose `gate_requested` opened the newest gate hold still running.

    `holds.Hold` carries three fields and no key — deliberately, and it is a security
    boundary rather than minimalism (see its docstring) — so the episode cannot hand back
    its own approval id. It is resolved the other way round instead: walk the
    `gate_requested` events, newest last, and keep the one whose approval the store still
    counts as open. That is the same pairing `holds._episodes` does, over the same rows.
    """
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
                     alarm: dict[str, Any], *,
                     params: dict[str, Any] | None = None) -> str:
    """End a gate hold episode whose stated cause no longer holds.

    `abandon_approval` and NEVER a verdict: the row lands in `expired`/`abandoned`, which
    is the one status that already means "never decided, and can no longer be". Writing
    `denied` here would assert that a reviewer refused a privileged action, on evidence
    the recogniser is usually wrong about. It authorises nothing — the command string
    stays blocked, so a retry gets a real review.
    """
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
        # The same refusal `apply` would raise, as a sentence: `can_apply` answers with
        # prose and never raises, and re-deriving the wording here would let the two
        # doors drift.
        return str(refused)
    wo_id = str(subject.get("id") or "")
    if subject.get("needs_attention") and str(subject.get("attention_reason") or "") == reason:
        return (f"{wo_id} is already flagged with that exact reason — the user has it in "
                f"their list")
    return ""


def _apply_raise_attention(pstore: Any, central: Any, project: str,
                           subject: dict[str, Any], alarm: dict[str, Any], *,
                           params: dict[str, Any] | None = None) -> str:
    """Flag attention with a reason RENDERED from the closed template table, and only so.

    The rule names a key; the sentence is built here by `render_attention` and is one of
    the sentences `invariants.true_blockers` itself re-derives (see `ATTENTION_TEMPLATES`
    for why both halves matter). The reconciler owns this column, so a reason outside the
    table is not a flag that says the wrong thing — it is a flag that says something
    DIFFERENT two minutes later with no record of what it first said. And per spec commit
    2920441 nothing the rule wrote reaches the sentence: an unknown key is refused here,
    and was already refused on insert by `check_params` against `Param.choices`.
    """
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

#: Asserted equal to `tuple(REMEDIES)`. The registry is closed BY A TEST rather than by
#: a convention, so widening what the OS may do fails a suite and is read by a human.
#:
#: NINE, and the spec's §4 table lists TEN: `retry_neo_question` is DROPPED on Neo 908 and
#: filed as a backlog item instead — a primitive a rule can name must have an act behind
#: it, and requeuing a failed question has none that does not double-spend an attempt.
SHIPPED_REMEDIES: tuple[str, ...] = ("nudge", "unblock", "file_work_order",
                                     "update_branch", "carry_verdict", "force_rejudge",
                                     "lower_attention", "drop_hold", "raise_attention")


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
    # THE COLUMN DOES NOT EXIST YET, and `.get` on an absent key is exactly why this is
    # forward-compatible rather than broken (Neo 904). Every alarm row today yields `{}`
    # here and every handler ignores it; the section that adds `wo_alarms.remedy_params`
    # lands the column alone, with no edit to this call and no migration of this module.
    params = db.from_json(alarm.get("remedy_params"), {}) or {}
    result = remedy.apply(pstore, central, project, subject, alarm, params=params)
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

    # `supervisor._apply`'s ack path exactly: `ops.ack_os_flag` takes down the flag the
    # alarm raised and re-raises whatever else is blocking. Never `ack_attention` (that
    # is the user's blanket dismissal and would bury blockers they never saw — issue
    # 573), never `clear_attention` (that discards their earlier dismissals).
    try:
        ops.ack_os_flag(wo_id)
    except ops.OpsError as exc:
        log.info("remedy applied on %s; attention left up: %s", alarm["id"], exc)
    log.info("remedy %s applied for %s: %s", remedy.id, alarm["id"], result)
    return result
