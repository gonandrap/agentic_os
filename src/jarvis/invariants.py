"""Post-conditions — the OS checking that its own state still means what it says"""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping

from . import budget, db, worker_session
# A leaf module (logging, subprocess, dataclasses): the import cannot cycle back here
from .automerge import HELD_NOT_PASSED, HELD_SHA_MOVED, HELD_SHA_UNRECORDED
# Same leaf argument: `autoreview` imports only `navigation`
from .autoreview import REFUSAL_UNANSWERED_REASON
from .catalog import DEFAULT_VALIDATION_MAX_ROUNDS, DEFAULT_VALIDATION_TIMEOUT
from .neo_store import USER_HELD_Q_STATUSES
from .project_store import (
    ACTIVE_STATUSES,
    DEPENDENCY_DEAD_STATUSES,
    FO_OPEN_STATUSES,
    FO_TERMINAL_STATUSES,
    OPEN_STATUSES,
    RETRY_SWEEP_STATUSES,
    RUNNABLE_VALIDATION_OUTCOMES,
    SLOT_STATUSES,
    UNGOVERNED_ORIGINS,
    VALIDATION_AUTH_CAUSE,
    VALIDATION_HELD_CAUSE,
    validation_hold,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .fleet import Fleet
    from .project_store import ProjectStore

#: Statuses a work order can still reach `needs_review` from under its own steam (#711)
PRE_DELIVERY_STATUSES = ("pending", "dispatching", "running", "idle", "waiting_input",
                         "validating")

# Statuses where nothing can possibly be pending: the work order is over
TERMINAL_STATUSES = ("completed", "cancelled")

#: What a work order says when the pull request it was parked behind was closed without being merged
BUDGET_SPENT_BLOCKER = ("budget spent — raise it with `jarvis wo budget <id> <amount>` "
                        "or close the order")


@contextmanager
def _billing_store() -> Iterator[Any]:
    """A `CentralStore` for the Jarvis half of a bill, or None if it will not open (#692)"""
    from .central_store import CentralStore

    central = None
    try:
        central = CentralStore()
    except Exception:  # noqa: BLE001 — see the docstring
        pass
    try:
        yield central
    finally:
        if central is not None:
            central.close()


def budget_blocker(store: ProjectStore, wo: dict[str, Any]) -> str:
    """The attention line for a work order parked in `budget_exhausted`"""
    with _billing_store() as central:
        out = budget.exhaustion(store, central, wo)
    # None means the accounting no longer says it is spent: the user raised the budget and the tick that...
    return out.reason if out else BUDGET_SPENT_BLOCKER


PR_CLOSED_BLOCKER = "pull request closed without merging — the work was not accepted"

#: What a work order says when settling it would have stranded the code it wrote (`ops.park_unlanded` (#232)
UNLANDED_BLOCKER = ("work not landed — merge its pull request, or record that it is "
                    "being abandoned")

#: How many times the OS asks a worker to repair its own pull request before it asks the user instead (docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md)
PR_REPAIR_MAX_ATTEMPTS = 3

#: The statuses in which a pull request can SIT WITH NOBODY MOVING IT AND A WORKER CAN BE ASKED TO FIX... (#224)
PR_REPAIR_STATUSES = ("waiting_pr_merge", "needs_review", "waiting_input", "failed")

#: The NAME of each repair, which is also the name of its timeline events...
PR_CONFLICT_REPAIR = "conflict"
PR_CHECKS_REPAIR = "checks"
#: The third: the worker pushed past a refusal and never declared it (docs/superpowers/specs/2026-10-07-an-undeclared-delivery-must-heal-itself.md §2.1.)
PR_UNDECLARED_REPAIR = "undeclared"

#: `ops.PrRepair.source` for all three repairs — the `wo_messages.source` a nudge is queued under
PR_REPAIR_SOURCES = (f"pr-{PR_CONFLICT_REPAIR}", f"pr-{PR_CHECKS_REPAIR}",
                     f"pr-{PR_UNDECLARED_REPAIR}")

#: THE THREE EVENTS OF THE INHERITED-FAILURE HEAL
PR_BASE_HEALTH_EVENT = "pr_base_health"
#: The heal itself: the OS merged the base into this branch so CI rebuilds against a base that works
PR_BASE_UPDATED_EVENT = "pr_base_updated"
#: GitHub refused the update
PR_BASE_UPDATE_FAILED_EVENT = "pr_base_update_failed"

#: The finding when a project's DEFAULT BRANCH is red (docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §3)
INV_BASE_RED = "INV-BASE-BRANCH-RED"

#: The fix work order for that red branch is SETTLED and the branch is still red (docs/superpowers/specs/2026-10-14-a-red-main-dispatches-its-own-fix.md §5)
INV_BASE_FIX_DEAD = "INV-BASE-FIX-DEAD"

#: A completed work order whose recorded pull request has not merged (docs/superpowers/specs/2026-10-09-an-unrepairable-invariant-owes-a-decision.md §8)
INV_WORK_LANDED = "INV-WORK-LANDED"

#: What a work order says while its base is broken
BASE_RED_NOTE = ("waiting for `{base}` to go green — the base's own build is red, so "
                 "nothing this branch pushes can pass")

#: What a work order says when its pull request conflicts and the worker could not fix it in...
PR_CONFLICT_BLOCKER = ("merge conflicts the worker could not resolve — the pull request "
                       "needs you")

#: The other one: CI is red on the pull request and the worker could not get it green (#224)
PR_CHECKS_BLOCKER = ("the pull request's checks are failing and the worker could not fix "
                     "them — do not merge it as it stands")

#: The third: the worker answered a refusal with commits and never declared them
PR_UNDECLARED_BLOCKER = ("the worker pushed commits answering your refusal and would not "
                         "declare them — nothing has been judged, so decide what to do "
                         "with the branch as it stands")

#: THE TWO GIVE-UPS, PAIRED AND ORDERED (docs/superpowers/specs/2026-10-07-an-undeclared-delivery-must-heal-itself.md §2.1)
PR_REPAIR_BLOCKERS = ((PR_CONFLICT_REPAIR, PR_CONFLICT_BLOCKER),
                      (PR_CHECKS_REPAIR, PR_CHECKS_BLOCKER),
                      (PR_UNDECLARED_REPAIR, PR_UNDECLARED_BLOCKER))

#: The event `ops.rejudge_moved_head` writes when it will NOT re-judge a moved head: the round it... (docs/superpowers/specs/2026-09-19-a-moved-head-re-judges-itself.md)
REJUDGE_DECLINED_EVENT = "validation_rejudge_declined"

#: What a work order says when its pull request is green and mergeable
SHA_MOVED_BLOCKER = ("the panel's verdict names an older commit and no rounds are left "
                     "to bind a new one — merge it yourself, re-judge it "
                     "(`jarvis validation force`), or give it another round "
                     "(`validation.max_rounds`) and the OS re-judges it itself")

#: The OTHER `sha_moved` stall that reaches the user (docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md §4.5)
REBIND_EXHAUSTED_BLOCKER = ("the OS re-judged the merge it asked for twice and the "
                            "panel still refuses it — read the review and decide: "
                            "merge it yourself, or send the worker back")

#: THE STALL WITH NO COMMIT AT ALL (docs/superpowers/specs/2026-10-16-a-verdict-bound-to-no-commit-re-judges-itself.md §4)
SHA_UNRECORDED_BLOCKER = ("the panel passed this without recording which commit it "
                          "judged — it read a worktree, not the pull request — and no "
                          "rounds are left to bind one: merge it yourself, re-judge it "
                          "(`jarvis validation force`), or give it another round "
                          "(`validation.max_rounds`) and the OS re-judges it itself")

#: THE SAME STALL WITH NO DECLINE BEHIND IT (docs/superpowers/specs/2026-10-16-a-verdict-bound-to-no-commit-re-judges-itself.md §4b)
SHA_UNRECORDED_SPENT_BLOCKER = ("the panel passed this without recording which commit it "
                                "judged, the OS re-judged it itself and that round could "
                                "not read the pull request either — so no round will bind "
                                "one: merge it yourself, or re-judge it "
                                "(`jarvis validation force`) once GitHub is reachable")

#: THE THIRD SENTENCE IN THIS FAMILY
USER_REWORK_REFUSED_BLOCKER = ("the rework you asked for was judged and the panel "
                               "refused it, and no rounds are left — read the review "
                               "and decide: accept it, send the worker back with "
                               "another round (`validation.max_rounds`), or close it")

#: THE SAME ROUND WITH NOBODY'S OPINION ON IT (docs/superpowers/specs/2026-10-14-an-escalated-round-says-why-and-what-to-run-next.md §1)
USER_REWORK_UNREVIEWED_BLOCKER = (
    "the rework you asked for was never reviewed — the submission gave the panel nothing "
    "new to judge, and no counted round was spent, so another round changes nothing: "
    "send the worker back with "
    '`jarvis wo review --reject --feedback "<what to change>"`, or land it without a '
    'panel verdict with `jarvis wo review --feedback "<why>"`')

#: What a work order says when the reviewer REFUSED the automatic merge of the commit the panel...
AUTOMERGE_DENIED_BLOCKER = ("the automatic merge of the commit the panel accepted was "
                            "refused, so nothing will land this pull request by itself "
                            "— merge it by hand, push a fix and re-judge it "
                            "(`jarvis validation force`), or close it with "
                            "`jarvis wo done`")

#: What a work order says when the validation panel gave up on it: it kept resubmitting and the panel...
VALIDATION_STUCK_BLOCKER = ("the review could not be satisfied — the work needs your "
                            "judgement")

#: THE SAME GIVE-UP, ONE STATUS LATER
PARKED_GIVE_UP_BLOCKER = ("the panel gave up on this and no commit was ever accepted, so "
                          "the automatic merge will never arm — merge it yourself, "
                          "re-judge it (`jarvis validation force`), or give it another "
                          "round (`validation.max_rounds`)")

#: What a work order says when its turn died because Claude Code could not authenticate...
AUTH_BLOCKER = ("Claude Code could not authenticate — sign in again and it resumes "
                "by itself")

#: What a work order says when its worker went quiet without `jarvis wo finish`
IDLE_NO_FINISH_BLOCKER = ("the worker stopped mid-task without `jarvis wo finish` — "
                          "nothing it started is still running; read its last message, "
                          "then `jarvis wo send` or `jarvis wo done`")

#: WHAT THE IDLE LINE ABOVE SAID ABOUT A HOLD IT DOES NOT DESCRIBE: the worker DID deliver (docs/superpowers/specs/2026-10-14-an-unanswered-refusal-must-name-itself.md)
REFUSAL_UNANSWERED_BLOCKER = (
    f"{REFUSAL_UNANSWERED_REASON} — read your refusal, then `jarvis wo send` telling it "
    f"what to change; it re-delivers with `jarvis wo finish`")
#: The same hold on a kind whose own submission verb REPLACES the stored document
REFUSAL_UNANSWERED_DOC_BLOCKER = (
    f"{REFUSAL_UNANSWERED_REASON} — it delivered a document, not code, and resubmitting "
    f"that document would replace it and discard the decisions you have already "
    f"recorded on it; `jarvis wo send` telling it what to change, and it re-delivers "
    f"with `jarvis wo finish`, which records the delivery and does not touch the "
    f"document")

#: The kinds whose FIRST delivery used a verb of their own (`jarvis io report`
DOCUMENT_DELIVERY_KINDS = ("planner", "analyst")

#: Both sentences above contain the word "assumption"
REFUSAL_HELD_BLOCKERS = frozenset({REFUSAL_UNANSWERED_BLOCKER,
                                   REFUSAL_UNANSWERED_DOC_BLOCKER})

#: What a `failed` work order says when there is nothing more specific to say
WORKER_FAILED_BLOCKER = "worker failed — review and retry"

#: What a `failed` work order says when its turn died AFTER it had delivered (#839)
TURN_DIED_AFTER_DELIVERY_BLOCKER = (
    "its worker's session died after the work was delivered — the delivery is on record, "
    "and the order is `failed` because the turn never finished, not because the work did; "
    "`jarvis wo retry` resumes the session where it died, `jarvis wo done` closes it, and "
    "`jarvis wo review` decides any assumption still pending")

#: `kind` of the event the settler writes on that path, so the record never reads as a clean delivery
TURN_DIED_AFTER_DELIVERY_EVENT = "turn_died_after_delivery"

SECONDS_PER_MINUTE = 60  # a unit, not a setting
SECONDS_PER_HOUR = 3600  # ditto

#: What a work order says when its turn has ended
PARKED_BLOCKER = ("the worker parked mid-task — nothing is in flight to move it; read "
                  "its last message, then `jarvis wo send` or `jarvis wo done`")

#: The other shape of the same fact
STALE_FINISH_BLOCKER = ("the worker went back to work after finishing and stopped again "
                        "without finishing — its recorded summary is older than its last "
                        "turn; read that turn, then `jarvis wo send`")

#: Where "nothing is in flight" is worth saying
PARKABLE_STATUSES = ("dispatching", "running", "waiting_input", "needs_review")

#: What a work order says when something the user sent it is still sitting in the queue and the worker...
MESSAGE_STUCK_BLOCKER = ("a message you sent is still queued and the worker has not seen "
                         "it — `jarvis wo resume-auto` for what is holding it")

#: Where an undelivered message is a defect rather than a wait
MESSAGE_STUCK_STATUSES = ("dispatching", "running", "idle", "waiting_input",
                          "needs_review", "failed", "waiting_pr_merge")

#: The `ops.waiting_on` answers that mean the OS itself will do the next thing, with nobody typing
IN_FLIGHT_WAITS = ("turn_running", "queued_message", "neo_question", "gate_with_neo",
                   "retry_pending", "signin", "pending")

#: What `parked_reason` stays silent about
SPOKEN_FOR_WAITS = IN_FLIGHT_WAITS + ("message_stuck",)


@dataclass
class Violation:
    """One invariant found false, with whatever the checker did about it"""

    invariant: str
    detail: str
    wo_id: str | None = None
    repaired: bool = False
    repair: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    #: How loudly the notification arrives (docs/superpowers/specs/2026-09-28-a-usage-limit-is-not-a-failed-sweep.md §5)
    level: str = "warning"
    #: NOT REPAIRABLE, AND A DECISION IS OWED BY THE USER (docs/superpowers/specs/2026-10-09-an-unrepairable-invariant-owes-a-decision.md §1)
    owed: str = ""
    #: DEDUPE IDENTITY for a violation that is not about a work order — a feature order, say (docs/superpowers/specs/2026-10-13-an-assumption-review-payload-carries-its-spec-section.md §2.3)
    subject_id: str | None = None
    #: What the NOTIFICATION says, when `detail` holds text that must not leave the record (docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md §2.5)
    notice: str = ""

    @property
    def key(self) -> tuple[str, str | None]:
        """Identity for dedupe: the same invariant failing on the same SUBJECT is one standing problem"""
        return (self.invariant, self.subject_id or self.wo_id)

    def __str__(self) -> str:
        where = f"{self.wo_id}: " if self.wo_id else ""
        fixed = f" [repaired: {self.repair}]" if self.repaired else ""
        return f"{self.invariant} — {where}{self.detail}{fixed}"


#: What a FEATURE order says when one of its children ended badly
FEATURE_CHILD_FAILED = "{id} failed — this feature cannot finish without it"
FEATURE_CHILD_CANCELLED = ("{id} was cancelled — this feature will not deliver what the "
                           "plan promised")

#: What a FEATURE order says when a merged child's commit never reached the head a round would judge (docs/superpowers/specs/2026-10-07-a-feature-round-must-judge-a-head-that-contains-its-children.md)
FEATURE_CHILD_NOT_INTEGRATED = (
    "{id}'s merge commit {commit} is not in {head}, the commit on the default branch a "
    "review would judge — so a panel would report its work as missing. Nothing has been "
    "judged.")


def dead_feature_children(store: ProjectStore,
                          children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The children that fail their feature: ended badly, and not answered for (#839)"""
    dead = [c for c in children
            if c["status"] in ("failed", "cancelled")
            and not c.get("superseded")]
    return [c for c in dead
            if c["status"] != "failed"
            or not store.events_of_kind(c["id"], TURN_DIED_AFTER_DELIVERY_EVENT)]


def outstanding_feature_children(store: ProjectStore,
                                 fo_id: str) -> list[dict[str, Any]]:
    """The children this feature has not finished with: non-settled, non-superseded"""
    return _outstanding(store.feature_children(fo_id))


def _outstanding(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rule itself, over rows already read"""
    from .project_store import TERMINAL_STATUSES as SETTLED_STATUSES

    return [c for c in children
            if c["status"] not in SETTLED_STATUSES and not c.get("superseded")]


#: How many outstanding children the note names before it stops listing them
OUTSTANDING_CHILDREN_NAMED = 3


def outstanding_children_note(store: ProjectStore, wo: dict[str, Any]) -> str:
    """Why an idle manager is idle, as one phrase — `""` when it is not that case"""
    if wo.get("kind") != "manager" or wo["status"] != "idle" or not wo.get("parent_id"):
        return ""
    fo_id = str(wo["parent_id"])
    children = store.feature_children(fo_id)
    outstanding = _outstanding(children)
    if not outstanding:
        return ""
    named = ", ".join(c["id"] for c in outstanding[:OUTSTANDING_CHILDREN_NAMED])
    more = len(outstanding) - OUTSTANDING_CHILDREN_NAMED
    return (f"waiting on {len(outstanding)} of {len(children)} children in {fo_id} "
            f"({named}{f', +{more} more' if more > 0 else ''})")


def has_delivered(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Has this work order produced something a person has to decide about?"""
    return bool(wo.get("pr_url")) or bool(store.all_assumptions(wo["id"]))


#: What a live child says when the feature it belongs to has settled under it
SETTLED_FEATURE_BLOCKER = ("its feature order {fo_id} is {status} — nothing will "
                           "coordinate this work order until the feature is live "
                           "again; {remedy}")

#: One remedy per settled status, because the way out genuinely differs: `jarvis fo resume` refuses...
SETTLED_FEATURE_REMEDY = {
    "failed": "`jarvis fo resume {fo_id}`",
    "cancelled": "`jarvis wo cancel {wo_id}` if this work order is not wanted either",
    "completed": "`jarvis wo done {wo_id}`, or `jarvis wo cancel {wo_id}`",
}

#: What a work order says when something it depends on can never complete
DEAD_DEPENDENCY_BLOCKER = "blocked by a dependency that can never complete"

#: What a release order says when it stopped waiting for a red base and asked the user (2026-09-29...
RELEASE_BASE_RED_BLOCKER = ("`{base}` has been red for {hours}h — `{workflow}` failed at "
                            "{sha} ({run_url}). The release is waiting on that build, "
                            "not on anything about the release.")


#: What a work order says when an OS-built prompt for it was past the ceiling (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
OS_PROMPT_REFUSED_BLOCKER = (
    "the OS built a {call} prompt of {total} chars for this work order ({prompt} + "
    "{system} system), over the {ceiling}-char ceiling (os.max_os_prompt_chars), so "
    "the call was never made and nobody has judged the work. Raise the ceiling or "
    "shrink what that call carries.")


def os_prompt_refused_blocker(said: dict[str, Any]) -> str:
    """OS_PROMPT_REFUSED_BLOCKER filled from the event the refusing call site wrote"""
    return OS_PROMPT_REFUSED_BLOCKER.format(
        call=said.get("call") or "model", total=said.get("total") or 0,
        prompt=said.get("prompt_chars") or 0,
        system=said.get("system_prompt_chars") or 0,
        ceiling=said.get("ceiling") or 0)


def release_base_red_blocker(said: dict[str, Any]) -> str:
    """RELEASE_BASE_RED_BLOCKER filled from the park event `ops.defer_red_release` wrote"""
    from .daemon import Daemon

    return RELEASE_BASE_RED_BLOCKER.format(
        base=said.get("base") or "main",
        hours=Daemon.RED_PARK_AFTER_SECONDS // SECONDS_PER_HOUR,
        workflow=said.get("workflow") or "ci", sha=str(said.get("head_sha") or "")[:10],
        run_url=said.get("run_url") or "")


#: §9: the objection the OS sent about an assumption died on the way to the worker
OBJECTION_UNDELIVERABLE_BLOCKER = ("the OS objected to an assumption and the objection "
                                   "never reached the worker — decide it yourself")


# -- the derivation everything else is checked against


def rejudge_exhausted(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Is this order parked on a head the OS declined to re-judge, and still on it?"""
    return _parked_on_a_decline(store, wo, rebind=False, code=HELD_SHA_MOVED)


def rebind_exhausted(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """The same question about the OTHER decline: the OS re-judged the merge it asked for `ops.REBIND_MAX`..."""
    return _parked_on_a_decline(store, wo, rebind=True, code=HELD_SHA_MOVED)


def sha_unrecorded_exhausted(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """The same question about the stall with NO commit bound: the round passed on a worktree"""
    return _parked_on_a_decline(store, wo, rebind=False, code=HELD_SHA_UNRECORDED)


def sha_unrecorded_rejudge_spent(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """The stall with no commit bound AND NO DECLINE: the OS spent its one round on this very head and..."""
    from . import ops as ops_mod

    held = store.events_of_kind(wo["id"], "automerge_held")
    if not held:
        return False
    newest = db.from_json(held[-1]["payload"], {})
    if str(newest.get("code") or "") != HELD_SHA_UNRECORDED:
        return False
    head = str(newest.get("head_sha") or "")
    if not head or head not in ops_mod.rejudged_heads(store, str(wo["id"])):
        return False
    return store.validated_head(
        store.latest_validation_round(wo_id=wo["id"])) != head


def _parked_on_a_decline(store: ProjectStore, wo: dict[str, Any], *,
                         rebind: bool, code: str) -> bool:
    """The three facts the derivations above share, read for one `cause` and one hold `code` only"""
    from . import ops as ops_mod

    declined = [e for e in store.events_of_kind(wo["id"], REJUDGE_DECLINED_EVENT)
                if (str(db.from_json(e["payload"], {}).get("cause") or "")
                    == ops_mod.REBIND_EXHAUSTED) is rebind]
    if not declined:
        return False
    held = store.events_of_kind(wo["id"], "automerge_held")
    if not held:
        return False
    newest = db.from_json(held[-1]["payload"], {})
    if str(newest.get("code") or "") != code:
        return False
    head = str(newest.get("head_sha") or "")
    if not head or not any(
            str(db.from_json(e["payload"], {}).get("head_sha") or "") == head
            for e in declined):
        return False
    # ...AND NOTHING HAS BOUND A VERDICT TO THAT COMMIT SINCE
    return store.validated_head(
        store.latest_validation_round(wo_id=wo["id"])) != head


def automerge_denied(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Was this order's automatic merge REFUSED, on the commit the panel still names?"""
    from .automerge import decided_sha

    decided = store.events_of_kind(wo["id"], "automerge_decided")
    if not decided:
        return False
    verdict = db.from_json(decided[-1]["payload"], {})
    if str(verdict.get("decision") or "") not in ("denied", "dismissed"):
        return False
    sha = decided_sha(verdict)
    # A row carrying neither `head_sha` nor a command to parse one from is not a verdict anyone can act...
    if not sha or store.validated_head(
            store.latest_validation_round(wo_id=wo["id"])) != sha:
        return False
    held = store.events_of_kind(wo["id"], "automerge_held")
    if held and float(held[-1]["ts"]) > float(decided[-1]["ts"]):
        newest = db.from_json(held[-1]["payload"], {})
        if (str(newest.get("code") or "") == HELD_SHA_MOVED
                and str(newest.get("head_sha") or "") != sha):
            return False
    return True


def parked_on_a_give_up(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Is the panel's give-up the thing holding this parked pull request? (docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md §A.)"""
    if not validation_escalated(store, wo):
        return False
    held = store.events_of_kind(wo["id"], "automerge_held")
    if not held:
        return False
    newest = db.from_json(held[-1]["payload"], {})
    return str(newest.get("code") or "") == HELD_NOT_PASSED


def neo_reviews_later(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Are this order's pending assumptions Neo's to decide on delivery, not the user's? (#711)"""
    if wo["status"] not in PRE_DELIVERY_STATUSES:
        return False
    from .ops import auto_review_at

    return auto_review_at(store.project_path)


def objection_undeliverable(store: ProjectStore, a: dict[str, Any]) -> bool:
    """Did the OS's objection about this assumption die on the way to the worker? (§9)"""
    env_id = a.get("objection_envelope_id")
    if env_id is None:
        return False
    if a.get("objection_delivered_ts") or a.get("objection_withdrawn_ts"):
        return False
    env = store.get_envelope(int(env_id))
    if env is None:
        return False
    msg_id = env.get("delivered_msg_id")
    if msg_id is not None:
        msg = store.get_message(int(msg_id))
        if msg is not None and msg["status"] == "failed":
            return True
    # Terminal and not delivered: nobody filled the role, or nobody could act
    return str(env.get("state") or "") in ("undeliverable", "handled_by_router")


def _confirmation_is_open(question_id: int) -> bool:
    """Is Neo still holding this confirmation question?"""
    from .neo_store import NEO_HELD_Q_STATUSES, NeoStore

    try:
        neo = NeoStore()
        try:
            q = neo.get(question_id)
        finally:
            neo.close()
    except Exception:  # noqa: BLE001 — see docstring: never take a caller down with us
        return False
    return bool(q) and str(q["status"] or "") in NEO_HELD_Q_STATUSES


def _os_is_confirming(a: dict[str, Any]) -> bool:
    """Is the OS's own confirmation pass (§7) holding this assumption, not the user? (#833)"""
    verdict = str(a.get("provisional_verdict") or "")
    if verdict == "accept":
        qid = int(a.get("confirm_question_id") or 0)
        return not qid or _confirmation_is_open(qid)
    return verdict == "object" and bool(a.get("objection_delivered_ts"))


def true_blockers(store: ProjectStore, wo: dict[str, Any],
                  now: float | None = None) -> list[str]:
    """The reasons this work order genuinely needs the user, derived from state alone"""
    blockers: list[str] = []
    pending = store.pending_assumptions(wo["id"])
    # §9: FIRST, because it is the more specific fact about the same assumption
    if any(objection_undeliverable(store, a) for a in pending):
        blockers.append(OBJECTION_UNDELIVERABLE_BLOCKER)
    owed = pending
    if pending and wo["status"] == "needs_review":
        # `neo_reviews_later` covers the PRE-DELIVERY statuses (#711) and is False here
        from .ops import auto_review_at
        if auto_review_at(store.project_path):
            owed = [a for a in pending if not _os_is_confirming(a)]
    # ONE COMPOUND LINE when the order is also out of money
    compound = False
    if owed and not neo_reviews_later(store, wo):
        n = len(owed)
        compound = wo["status"] == budget.EXHAUSTED
        clause = (f"{n} assumption{'s' if n != 1 else ''} "
                  f"also await{'s' if n == 1 else ''} you")
        blockers.append(f"{budget_blocker(store, wo)}; {clause}" if compound
                        else f"{n} assumption{'s' if n != 1 else ''} pending your review")
    # A privileged action Neo declined to decide on
    for approval in store.escalated_approvals(wo["id"]):
        blockers.append(
            f"gate approval needed: {approval['kind']} (request {approval['id']})"
        )
    # Two of the blockers below only exist because Jarvis dispatched the worker and briefed it on the...
    governed = wo.get("origin") not in UNGOVERNED_ORIGINS
    # OUT OF MONEY, and the reason is REBUILT rather than read off `attention_reason`: this function is...
    if wo["status"] == budget.EXHAUSTED and not compound:
        blockers.append(budget_blocker(store, wo))
    if governed and wo["status"] == "failed":
        # TWO SENTENCES, and the second is the one the generic line got wrong: a session that died AFTER the...
        died = store.events_of_kind(wo["id"], TURN_DIED_AFTER_DELIVERY_EVENT)
        blockers.append(TURN_DIED_AFTER_DELIVERY_BLOCKER if died
                        else WORKER_FAILED_BLOCKER)
    # AN INVARIANT THE OS CANNOT REPAIR, WHICH OWES THE USER A DECISION (docs/superpowers/specs/2026-10-09-an-unrepairable-invariant-owes-a-decision.md §3)
    for report in store.owed_violations(wo["id"]):
        blockers.append(report["owed"])
    # AN OS-BUILT PROMPT WAS REFUSED AS TOO LARGE (spec §4 (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
    if wo["status"] in OPEN_STATUSES:
        refusal = store.os_prompt_refusal_open(wo["id"])
        if refusal is not None:
            blockers.append(os_prompt_refused_blocker(refusal))
    # Parked on the user's Claude Code sign-in (`Daemon._park_on_signin`)
    auth_parked = False
    if governed and wo["status"] == "waiting_input":
        pause = worker_session.turn_pause(store, wo["id"])
        auth_parked = pause is not None and pause.reason == worker_session.PAUSE_AUTH
        if auth_parked:
            blockers.append(AUTH_BLOCKER)
    # Blocked on a prompt is real whoever started the session: nobody else can unstick it (#264)
    if wo["status"] == "waiting_input" and not auth_parked:
        question = awaiting_neo(wo["id"])
        if question is not None and question["status"] in USER_HELD_Q_STATUSES:
            # Neo handed the decision back
            blockers.append(neo_question_blocker(question))
        elif (question is None and not waiting_on_neo_gate(store, wo)
                and not store.queued_messages(wo["id"])):
            # A queued message is the reply already on its way: `ops.send_message` clears the flag the moment the...
            blockers.append("worker is waiting on your input")
    # A dependency that will never complete
    if wo["status"] == "pending" and dead_dependencies(store, wo):
        blockers.append(DEAD_DEPENDENCY_BLOCKER)
    # THE FEATURE THIS CHILD BELONGS TO HAS SETTLED UNDER IT
    if (wo["status"] in OPEN_STATUSES and wo.get("parent_id")
            and wo.get("kind") != "manager"):
        try:
            parent = store.get_feature_order(wo["parent_id"])
        except KeyError:
            parent = None  # deleted out from under the child: nothing left to name
        if parent is not None and parent["status"] in FO_TERMINAL_STATUSES:
            remedy = SETTLED_FEATURE_REMEDY.get(parent["status"], "")
            blockers.append(SETTLED_FEATURE_BLOCKER.format(
                fo_id=parent["id"], status=parent["status"],
                remedy=remedy.format(fo_id=parent["id"], wo_id=wo["id"])))
    # A RELEASE THAT WAITED OUT THE THRESHOLD ON A RED BASE (2026-09-29 spec §3)
    if wo["status"] == "needs_review":
        park = store.release_red_park_open(wo["id"])
        if park is not None:
            blockers.append(release_base_red_blocker(park))
    # A PULL REQUEST THE OS TRIED TO REPAIR AND COULD NOT — conflicts, a red build, or both
    if wo["status"] in PR_REPAIR_STATUSES:
        for repair, blocker in PR_REPAIR_BLOCKERS:
            if store.pr_repair_attempts(wo["id"], repair) >= PR_REPAIR_MAX_ATTEMPTS:
                blockers.append(blocker)
    # CANCELLED CI THE OS'S ONE RE-RUN DID NOT CLEAR — the column keeps the green path free of reads (#1123)
    ci_stuck = ci_checks_stuck(store, wo)
    if ci_stuck:
        blockers.append(ci_stuck)
    # A PULL REQUEST THE OS CANNOT RE-JUDGE
    if wo["status"] == "waiting_pr_merge" and rejudge_exhausted(store, wo):
        blockers.append(SHA_MOVED_BLOCKER)
    # THE SAME STALL WITH THE OTHER CAUSE
    if wo["status"] == "waiting_pr_merge" and rebind_exhausted(store, wo):
        blockers.append(REBIND_EXHAUSTED_BLOCKER)
    # AND THE STALL WITH NO COMMIT BOUND AT ALL (spec 2026-10-16 §4)
    if wo["status"] == "waiting_pr_merge" and sha_unrecorded_exhausted(store, wo):
        blockers.append(SHA_UNRECORDED_BLOCKER)
    # ...AND THE SAME STALL THE OS ALREADY SPENT ITS ROUND ON
    if wo["status"] == "waiting_pr_merge" and sha_unrecorded_rejudge_spent(store, wo):
        blockers.append(SHA_UNRECORDED_SPENT_BLOCKER)
    # A MERGE THE REVIEWER REFUSED
    if wo["status"] == "waiting_pr_merge" and automerge_denied(store, wo):
        blockers.append(AUTOMERGE_DENIED_BLOCKER)
    # THE GIVE-UP, SURVIVING INTO THE STATUS THAT DROPPED IT (issue 902)
    if wo["status"] == "waiting_pr_merge" and parked_on_a_give_up(store, wo):
        blockers.append(PARKED_GIVE_UP_BLOCKER)
    if governed and wo["status"] == "needs_review":
        # In the body, the pattern `auto_review_at` above already uses: `ops` imports this module
        from .ops import refusal_answered

        # THREE WAYS TO ARRIVE AT `needs_review`, ranked, and each asking the user for something different (#224)
        if wo.get("pr_state") == "CLOSED":
            blockers.append(PR_CLOSED_BLOCKER)
        # 2
        elif validation_escalated(store, wo):
            if user_rework_unreviewed(store, wo):
                blockers.append(USER_REWORK_UNREVIEWED_BLOCKER)
            elif user_rework_refused(store, wo):
                blockers.append(USER_REWORK_REFUSED_BLOCKER)
            else:
                blockers.append(VALIDATION_STUCK_BLOCKER)
        # 3 (#232)
        elif not pending and store.work_unlanded_open(wo["id"]):
            blockers.append(UNLANDED_BLOCKER)
        # 3b (docs/superpowers/specs/2026-10-14-an-unanswered-refusal-must-name-itself.md)
        elif (not pending and PR_UNDECLARED_BLOCKER not in blockers
                and not refusal_answered(store, wo["id"])):
            blockers.append(REFUSAL_UNANSWERED_DOC_BLOCKER
                            if wo.get("kind") in DOCUMENT_DELIVERY_KINDS
                            else REFUSAL_UNANSWERED_BLOCKER)
        # 4
        elif not pending:
            blockers.append(IDLE_NO_FINISH_BLOCKER)
    # A message the user sent that the worker will never see (GitHub issue 43)
    if stuck_message(store, wo, now=now) is not None:
        blockers.append(MESSAGE_STUCK_BLOCKER)
    # LAST, and never INSTEAD of anything: a work order can be parked and also owe the user a decision (docs/superpowers/specs/2026-10-14-an-unanswered-refusal-must-name-itself.md)
    if all(_mentions_assumptions(b) and b not in REFUSAL_HELD_BLOCKERS
           for b in blockers):
        parked = parked_reason(store, wo, now=now)
        if parked:
            blockers.append(parked)
    # What the user has already looked at and dismissed stops being a blocker — but only exactly that
    return [b for b in blockers if b not in acknowledged(wo)]


def ci_checks_stuck(store: ProjectStore, wo: dict[str, Any]) -> str:
    """The give-up line for cancelled CI on the head the poll last saw, or "" (#1123)"""
    from . import ci_rerun

    stuck = str(wo.get("pr_checks_stuck_head") or "")
    if (not stuck or wo["status"] not in ci_rerun.CI_WAIT_STATUSES
            or stuck != str(wo.get("pr_head_oid") or "")):
        return ""
    found = ci_rerun.episode(store, wo["id"], stuck).stuck
    return str((found or {}).get("blocker") or "")


def acknowledged(wo: dict[str, Any]) -> list[str]:
    """What has already been dismissed on this work order, decoded from the column"""
    return db.from_json(wo.get("acknowledged_blockers"), []) or []


def stuck_message(store: ProjectStore, wo: dict[str, Any],
                  now: float | None = None) -> tuple[dict[str, Any], str] | None:
    """The message this work order was sent and will never receive, and what holds it"""
    from .ops import messaging_config_at

    if wo["status"] not in MESSAGE_STUCK_STATUSES:
        return None
    queued = store.queued_messages(wo["id"])
    if not queued:
        return None
    oldest = queued[0]  # `queued_messages` is chronological
    now = time.time() if now is None else now
    minutes = int(messaging_config_at(store.project_path).stuck_minutes)
    if now - float(oldest["ts"]) < minutes * SECONDS_PER_MINUTE:
        return None
    hold = worker_session.message_hold(store, wo, oldest, now=now)
    if hold is not None and hold.accounted:
        return None
    # No hold at all is its own finding: the pass was free to send and the message is still here
    return oldest, hold.reason if hold else "the delivery pass has not attempted it"


def _parked_minutes(store: ProjectStore) -> tuple[bool, int]:
    """Whether the parked check is armed for THIS project, and after how long"""
    from .ops import inspect_config_at

    cfg = inspect_config_at(store.project_path)
    return bool(cfg.enabled), int(cfg.alarm_parked_minutes)


def judged_heads(store: ProjectStore, wo_id: str) -> set[str]:
    """Every commit of this work order the OS has already judged, or seen land"""
    from .project_store import ProjectStore as _Store

    merged = store.events_of_kind(wo_id, "pr_merged")
    heads = {
        _Store.validated_head(store.latest_validation_round(wo_id=wo_id)) or "",
        str((db.from_json(merged[-1]["payload"], {}) or {}).get("head_oid") or "")
        if merged else "",
    }
    return heads - {""}


def _head_already_judged(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Is the head the poll last saw one of `judged_heads`?"""
    current = str(wo.get("pr_head_oid") or "")
    return bool(current) and current in judged_heads(store, wo["id"])


#: `wo_alarms.kind` for §2c's detector, and the statuses it is asked about
UNDECLARED_DELIVERY_KIND = "undeclared_delivery"
UNDECLARED_DELIVERY_STATUSES = ("needs_review", "waiting_pr_merge", "waiting_input")

#: What the OS asks the worker to do about an undeclared delivery
UNDECLARED_DELIVERY_REASON = (
    "the user refused an assumption on this work order and the worker has pushed "
    "commits since without running `jarvis wo finish`, so nothing has declared them and "
    "the panel has nothing it may judge")


def undeclared_delivery(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Has this worker pushed past a refusal without declaring it? (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)"""
    from .ops import refusal_answered

    current = str(wo.get("pr_head_oid") or "")
    if not current or refusal_answered(store, wo["id"]):
        return False
    judged = judged_heads(store, wo["id"])
    return bool(judged) and current not in judged


def check_undeclared_delivery(store: ProjectStore) -> Iterator[Violation]:
    """INV-UNDECLARED-DELIVERY — raise the finding; the supervisor decides what to do"""
    # `add_finding` is not one of the proxy's blocked mutators
    readonly = getattr(store, "readonly", False)
    for wo in store.list_work_orders(statuses=UNDECLARED_DELIVERY_STATUSES):
        if not undeclared_delivery(store, wo):
            continue
        # THE OS IS ON IT: the nudge is the response, and the give-up is what reaches the user (docs/superpowers/specs/2026-10-07-an-undeclared-delivery-must-heal-itself.md §2.4.)
        if wo.get("session_id") and not store.pr_repair_gave_up(wo["id"],
                                                                PR_UNDECLARED_REPAIR):
            continue
        if any(a["kind"] == UNDECLARED_DELIVERY_KIND for a in store.alarms_of(wo["id"])):
            continue
        if not readonly:
            store.add_finding(wo["id"], kind=UNDECLARED_DELIVERY_KIND,
                              reason=UNDECLARED_DELIVERY_REASON, source="invariant")
        yield Violation(
            invariant="INV-UNDECLARED-DELIVERY",
            wo_id=wo["id"],
            detail=UNDECLARED_DELIVERY_REASON,
            repaired=not readonly,
            repair=("would raise " if readonly else "raised ")
                   + "a finding for the supervisor to judge",
        )


def check_spans_reach_the_status(store: ProjectStore) -> Iterator[Violation]:
    """INV-SPAN-BEHIND — a status write with no span is repaired where it is found (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)"""
    readonly = getattr(store, "readonly", False)
    # Hidden too: hiding takes an order out of the listings
    for wo in store.list_work_orders(include_hidden=True):
        spans = store.state_spans(wo["id"])
        tail = str(spans[-1]["to_status"]) if spans else ""
        status = str(wo["status"] or "")
        # No spans at all is `_backfill_wo_spans`' case, not this one
        if not spans or tail == status:
            continue
        if not readonly:
            # Never BEFORE the span it follows: `state_spans` orders by ts
            at = max(float(wo["updated_at"] or db.now()), float(spans[-1]["ts"]))
            store._record_span(wo["id"], "wo", tail, status, trigger="repair",
                               ts=at, approximate=True)
        yield Violation(
            invariant="INV-SPAN-BEHIND",
            wo_id=wo["id"],
            detail=(f"the recorded spans stop at {tail!r} while this order's status "
                    f"column says {status!r}"),
            repaired=not readonly,
            repair=("would append " if readonly else "appended ")
                   + "an approximate span at the order's last update",
            context={"tail": tail, "status": status},
        )


def parked_reason(store: ProjectStore, wo: dict[str, Any],
                  now: float | None = None) -> str | None:
    """Why nothing is going to happen to this work order — or None if something is"""
    if wo.get("origin") in UNGOVERNED_ORIGINS:
        # An injected session was never briefed on `jarvis wo finish` (#264)
        return None
    if wo["status"] not in PARKABLE_STATUSES:
        return None
    turn = store.latest_turn(wo["id"])
    if turn is None or turn["state"] != "done" or not turn["ended_at"]:
        return None
    enabled, minutes = _parked_minutes(store)
    if not enabled:
        return None
    now = time.time() if now is None else now
    ended = float(turn["ended_at"])
    if now - ended < minutes * SECONDS_PER_MINUTE:
        return None
    # ...and how much of that silence was the OS itself (`holds`, user ruling 2026-09-18)
    from . import holds as holds_mod

    spans = holds_mod.held(store, wo["id"], now=now)
    if (now - ended) - sum(h.overlap(ended, now, now) for h in spans) \
            < minutes * SECONDS_PER_MINUTE:
        return None
    from .ops import waiting_on

    if waiting_on(store, wo)["what"] in SPOKEN_FOR_WAITS:
        return None
    finished = store.events_of_kind(wo["id"], "finished")
    if (finished and float(finished[-1]["ts"]) < float(turn["started_at"])
            # ...UNLESS THE OS OPENED THAT TURN ITSELF (#705)
            and store.turn_opened_by(turn) not in PR_REPAIR_SOURCES
            # ...OR THE HEAD IS ONE THE OS HAS ALREADY JUDGED OR SEEN LAND (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
            and not _head_already_judged(store, wo)):
        return STALE_FINISH_BLOCKER
    # A `needs_review` order with no stale finish is doing exactly what that status says
    return None if wo["status"] == "needs_review" else PARKED_BLOCKER


def validation_escalated(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Did this work order's most recent validation round give up and ask for a human? (docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md §A.)"""
    judged = store.last_judged_round(wo_id=wo["id"])
    return bool(judged and judged["outcome"] == "escalated")


def user_rework_refused(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Was the give-up above the panel refusing the rework the USER asked for? (docs/superpowers/specs/2026-10-14-an-escalated-round-says-why-and-what-to-run-next.md §1)"""
    family = _escalated_user_rework_family(store, wo)
    return bool(family) and family != "not_reviewed"


def user_rework_unreviewed(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """The same round with NO seat's opinion on it — `USER_REWORK_UNREVIEWED_BLOCKER` (docs/superpowers/specs/2026-10-14-an-escalated-round-says-why-and-what-to-run-next.md §1)"""
    return _escalated_user_rework_family(store, wo) == "not_reviewed"


def _escalated_user_rework_family(store: ProjectStore, wo: dict[str, Any]) -> str:
    """The escalation family of the latest round IF a user rejection bought it, else ``""``"""
    from . import ops as ops_mod

    latest = store.latest_validation_round(wo_id=wo["id"])
    if not (latest and latest["outcome"] == "escalated"
            and str(latest["uncounted_cause"] or "") == ops_mod.USER_REWORK_CAUSE):
        return ""
    cause = ops_mod.escalation_cause(store, wo_id=wo["id"], round_id=int(latest["id"]))
    return ops_mod.ESCALATION_FAMILY.get(cause, "")


def dead_dependencies(store: ProjectStore, wo: dict[str, Any]) -> list[dict[str, Any]]:
    """Dependencies of this work order that can never be satisfied"""
    return [dep for dep in store.unfinished_dependencies(wo["id"])
            if dependency_is_dead(dep)]


def dependency_is_dead(dep: Mapping[str, Any]) -> bool:
    """One row of `ProjectStore.unfinished_dependencies`: can this edge NEVER clear? (docs/superpowers/specs/2026-10-09-a-designed-wait-is-not-a-stall.md)"""
    return dep["status"] in DEPENDENCY_DEAD_STATUSES or dep["status"] == "missing"


def usage_hold_note(until: float) -> str:
    """"the Claude usage window is spent, …" — the PANEL's twin of `pause_note` above (#235)"""
    return ("the Claude usage window is spent, the review resumes by itself at "
            f"{clock(until)}")


#: The OTHER hold, and it names no moment on purpose
CI_HOLD_NOTE = "waiting for GitHub to finish running the checks"

#: The THIRD hold, and it names no moment either (docs/superpowers/specs/2026-09-26-the-panel-must-not-mistake-an-auth-failure-for-a-verdict.md §7)
AUTH_HOLD_NOTE = "waiting for Claude Code authentication"


def validation_hold_note(store: ProjectStore, wo_id: str, round_no: int) -> str:
    """Why this round is not being judged, or "" for a round that is not held (#714)"""
    until, cause = validation_hold(store.events_of_kind(wo_id, "validation_failed"),
                                   round_no)
    if until <= time.time():
        return ""
    if cause == VALIDATION_HELD_CAUSE:
        return usage_hold_note(until)
    if cause == VALIDATION_AUTH_CAUSE:
        return AUTH_HOLD_NOTE
    return CI_HOLD_NOTE


def parallel_round_note(store: ProjectStore, wo_id: str) -> str:
    """" — review round N is running in parallel", or "" when none is"""
    latest = store.latest_validation_round(wo_id=wo_id)
    if latest and latest["outcome"] in RUNNABLE_VALIDATION_OUTCOMES:
        held = validation_hold_note(store, wo_id, int(latest["round"]))
        if held:
            return f" — review round {latest['round']} is held: {held}"
        return f" — review round {latest['round']} is running in parallel"
    return ""


def base_red_note(store: ProjectStore, wo_id: str) -> str:
    """"waiting for `main` to go green", or "" when the base is not the problem"""
    from . import db

    rows = store.events_of_kind(wo_id, PR_BASE_HEALTH_EVENT)
    if not rows:
        return ""
    payload = db.from_json(rows[-1].get("payload"), {}) or {}
    if not payload.get("red"):
        return ""
    return BASE_RED_NOTE.format(base=payload.get("base") or "its base")


#: Statuses an ACCOUNT-WIDE outage actually holds: the OS itself would start a turn or a validation... (#714)
FLEET_HELD_STATUSES = ("pending", "dispatching", "running", "validating")


def fleet_hold_note(wo: dict[str, Any], fleet: Fleet | None) -> str:
    """"the Claude usage window is spent, reopening at …" for an order the window holds (#714)"""
    if fleet is None or not fleet.shut():
        return ""
    if wo["status"] not in FLEET_HELD_STATUSES:
        return ""
    return fleet.blocked()


def plan_hold_note(hold: dict[str, Any] | None) -> str:
    """One sentence for a `ProjectStore.plan_hold` row, `""` for no hold"""
    if not hold:
        return ""
    n = hold["n"]
    return (f"blocked by {hold['fo_id']}'s plan ({n} "
            f"assumption{'' if n == 1 else 's'} await{'s' if n == 1 else ''} your "
            f"review — `jarvis wo review {hold['planner_id']}`)")


#: §5 of docs/superpowers/specs/2026-10-20-a-merge-held-for-running-checks-is-not-stuck.md
AUTOMERGE_CHECKS_NOTE = "waiting for GitHub's checks on {sha} — {reason}"

#: The STORED reason, clamped to one line for the sweep's blocker column, which renders it verbatim
AUTOMERGE_CHECKS_REASON_CHARS = 120


def automerge_checks_note(state: dict[str, Any] | None) -> str:
    """One sentence for a `checks_running` auto-merge hold, `""` for anything else"""
    from .automerge import HELD_CHECKS_RUNNING, hold_is_live

    if not hold_is_live(state) or state is None:
        return ""
    if str(state.get("code") or "") != HELD_CHECKS_RUNNING:
        return ""
    reason = " ".join(str(state.get("reason") or "").split())
    if len(reason) > AUTOMERGE_CHECKS_REASON_CHARS:
        reason = reason[:AUTOMERGE_CHECKS_REASON_CHARS - 1] + "…"
    return AUTOMERGE_CHECKS_NOTE.format(
        sha=str(state.get("head_sha") or "")[:10] or "the merge commit",
        reason=reason or "no reason recorded")


def status_label(store: ProjectStore, wo: dict[str, Any],
                 fleet: Fleet | None = None) -> str:
    """How this work order's status should read to a human"""
    # FIRST, and deliberately so
    if wo["status"] == "validating":
        latest = store.latest_validation_round(wo_id=wo["id"])
        round_no = latest["round"] if latest else 1
        # The budget is the shipped default rather than this project's configured `os.validation.max_rounds`:...
        label = f"validating — review round {round_no} of {DEFAULT_VALIDATION_MAX_ROUNDS}"
        # Why nothing is happening, when the answer is a spent window rather than a slow reviewer
        held = (validation_hold_note(store, wo["id"], int(round_no))
                or fleet_hold_note(wo, fleet))
        return f"{label} — {held}" if held else label
    if wo["status"] in ACTIVE_STATUSES:
        note = pause_note(store, wo) or neo_wait_note(wo) or fleet_hold_note(wo, fleet)
        return f"{wo['status']} — {note}" if note else wo["status"]
    # OUT OF MONEY, and ABOVE the pause note: a work order can hold a booked retry AND be over its...
    if wo["status"] == budget.EXHAUSTED:
        # A real billing store, like every other reader of this figure: the label and the Budget line on the...
        with _billing_store() as central:
            cap = budget.ceiling(store, central, wo)
        if cap is None:
            # The budget was cleared and the tick that moves the status has not run
            return "budget spent — cleared, resuming shortly"
        line = f"budget spent — ${cap.spent_usd:.2f} of ${cap.cap_usd:.2f}"
        if cap.source != "feature":
            return line
        # `source == 'feature'` is the ALLOCATOR's word for "the family's cap" and says nothing about the...
        from .ops import family_prose
        parent = None
        if wo.get("parent_id"):
            try:
                parent = store.get_feature_order(wo["parent_id"])
            except KeyError:
                parent = None
        return f"{line} (its {family_prose((parent or {}).get('kind'))[0]}'s slice)"
    # A booked retry on one of the settled-looking statuses the sweep reaches and the branch above does... (#259)
    parked = pause_note(store, wo)
    if parked:
        return f"{wo['status']} — {parked}"
    # Below the pause note above, which names a moment this one cannot: a manager whose turn was refused...
    if wo["status"] == "idle":
        # Naming the children when there are any (spec §5): "waiting for a message" is true of a manager...
        note = outstanding_children_note(store, wo)
        return f"idle — {note}" if note else \
            "idle — waiting for a message from its feature"
    # `needs_review` no longer means the panel is waiting for the user: since issue 212 the round runs in...
    if wo["status"] == "needs_review":
        note = parallel_round_note(store, wo["id"])
        if note:
            return f"{wo['status']}{note}"
    # WHY NOTHING IS BEING NUDGED
    if wo["status"] in PR_REPAIR_STATUSES and wo.get("pr_url"):
        note = base_red_note(store, wo["id"])
        if note:
            return f"{wo['status']} — {note}"
    if wo["status"] != "pending":
        return wo["status"]
    # FIRST of the four pending answers: of them it is the only one that may never clear without a... (docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md)
    held = store.open_preconditions(wo["id"])
    if held:
        refs = ", ".join(row["ref"] for row in held)
        notes = "; ".join(row["note"] for row in held if row["note"])
        label = (f"pending — held until {refs} clears "
                 f"(`jarvis wo unblock {wo['id']}`)")
        return f"{label} — {notes}" if notes else label
    # ABOVE the dependency and slot labels
    note = plan_hold_note(store.plan_hold(wo))
    if note:
        return f"pending — {note}"
    blockers = store.unfinished_dependencies(wo["id"])
    if blockers:
        return f"pending — blocked by {', '.join(dep['id'] for dep in blockers)}"
    # Ranked below the dependency label deliberately: a work order waiting on a sibling's merge is not...
    cap = _slot_cap(store, wo)
    if cap:
        parent, limit, active = cap
        return (f"pending — waiting for a slot in {parent} "
                f"({active}/{limit} children running)")
    # LAST, and only for a caller that HAS the fleet's state: this one is a fact about the account rather...
    held = fleet.blocked(wo["id"]) if fleet is not None else ""
    if held:
        return f"pending — {held}"
    return "pending"


#: How long past its own deadline a paused turn may sit before the OS says so
PAUSE_OVERDUE_GRACE = 15 * 60

#: What `Daemon.retry_paused_turns` writes when it looks at a due pause and defers the relaunch (docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md §1.)
RETRY_HELD_EVENT = "retry_held"

#: At most one `retry_held` per order per five minutes
RETRY_HELD_RESTATE = 5 * 60

#: How long a reader treats the newest hold as LIVE
RETRY_HELD_FRESH_FOR = 2 * RETRY_HELD_RESTATE


def clock(ts: float) -> str:
    """A moment as the reader's own clock reads it, WITH THE DATE unless it is today"""
    when = time.localtime(ts)
    if when[:3] == time.localtime(time.time())[:3]:
        return time.strftime("%H:%M", when)
    return time.strftime("%H:%M on %a %d %b", when)


def _span(seconds: float) -> str:
    """A duration in the coarsest unit that still says something — "8h", "12m\""""
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}h"
    return f"{max(seconds, 60) / 60:.0f}m"


def retry_hold(store: ProjectStore, wo: dict[str, Any], pause: Any,
               now: float | None = None) -> dict[str, Any] | None:
    """The `retry_held` payload explaining why this due pause has not relaunched"""
    from . import db

    if pause is None:
        return None
    row = store.last_event_of_kind(wo["id"], RETRY_HELD_EVENT)
    if row is None:
        return None
    payload = db.from_json(row.get("payload"), {}) or {}
    if payload.get("seq") != pause.turn["seq"]:
        return None
    at = time.time() if now is None else now
    if at - float(row.get("ts") or 0) > RETRY_HELD_FRESH_FOR:
        return None
    return payload


def _retry_hold_note(wo: dict[str, Any], hold: dict[str, Any]) -> str | None:
    """That hold in words — or None for a cause this OS does not write"""
    cause = hold.get("cause")
    if cause == "fleet_cap":
        return (f"waiting for a free in-flight slot ({hold.get('in_flight')} of "
                f"{hold.get('cap')} worker turns already in flight)")
    if cause == "project_cap":
        return (f"waiting for a free slot in this project ({hold.get('active')} of "
                f"{hold.get('max_concurrent')} running)")
    if cause == "fleet_paused":
        return ("held: the fleet is paused by the user — `jarvis resume "
                f"{wo['id']}` lets this order through")
    if cause == "fleet_ramp":
        return (f"waiting for the fleet to ramp back up after the usage window reopened "
                f"({hold.get('in_flight')} of {hold.get('cap')} worker turns in flight "
                f"until {clock(float(hold.get('until') or 0))})")
    if cause == "fleet_outage":
        # `fleet_hold_note` owns these words wherever it is gated to speak them
        if wo["status"] in FLEET_HELD_STATUSES:
            return ""
        return ("the Claude usage window is spent, reopening at "
                f"{clock(float(hold.get('reopens_at') or 0))}")
    return None


def pause_note(store: ProjectStore, wo: dict[str, Any]) -> str:
    """Why this work order is not moving and when it will move again — or "" normally (#259)"""
    if wo["status"] not in RETRY_SWEEP_STATUSES:
        return ""
    pause = worker_session.turn_pause(store, wo["id"])
    if pause is None or pause.exhausted:
        return ""
    # ABOVE all three branches below (docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md)
    hold = retry_hold(store, wo, pause)
    if hold is not None:
        note = _retry_hold_note(wo, hold)
        if note is not None:
            return note
    if pause.reason == worker_session.PAUSE_AUTH:
        # The one pause with no moment to name — it resumes on an ACTION
        return "Claude Code sign-in expired, resuming once you sign in again"
    when = clock(pause.retry_at)
    if pause.reason == worker_session.PAUSE_USAGE_LIMIT:
        return f"Claude usage limit reached, retrying by itself at {when}"
    what = f"Claude API error {pause.status}" if pause.status else "Claude API error"
    return (f"{what}, retrying by itself at {when} "
            f"(attempt {pause.attempts} of {pause.max_attempts})")


def neo_wait_note(wo: dict[str, Any]) -> str:
    """"with Neo …" for a work order parked on a question — or "" for anything else"""
    if wo["status"] != "waiting_input":
        return ""
    question = awaiting_neo(wo["id"])
    if question is None:
        return ""
    if question["status"] in USER_HELD_Q_STATUSES:
        return f"Neo handed question {question['id']} back to you"
    return f"with Neo — it is answering question {question['id']}"


def _slot_cap(store: ProjectStore, wo: dict[str, Any]) -> tuple[str, int, int] | None:
    """`(feature id, max_parallel, active children)` when this work order's feature order is already..."""
    parent = wo.get("parent_id")
    if not parent or wo.get("kind") != "worker":
        return None
    try:
        fo = store.get_feature_order(parent)
    except KeyError:
        return None
    limit = fo.get("max_parallel")
    if not limit:
        return None
    active = store.count_active_children(parent)
    return (parent, int(limit), active) if active >= limit else None


def _mentions_assumptions(reason: str | None) -> bool:
    return "assumption" in (reason or "").lower()


def waiting_on_neo_gate(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """True when this work order is parked on a privileged-action request the user does not hold"""
    return (any(not a["escalated"] for a in store.pending_approvals(wo["id"]))
            or bool(store.held_approvals(wo["id"])))


def awaiting_neo(wo_id: str) -> dict[str, Any] | None:
    """The question this work order is parked on, or None — read from Neo's own DB"""
    from .neo_store import NeoStore

    try:
        neo = NeoStore()
    except Exception:  # noqa: BLE001 — see docstring: never take a caller down with us
        return None
    try:
        open_questions = neo.open_questions(wo_id)
    except Exception:  # noqa: BLE001
        return None
    finally:
        neo.close()
    return open_questions[0] if open_questions else None


def something_is_out(store: ProjectStore, wo_id: str) -> bool:
    """Is this work order parked on somebody, rather than merely having nothing to do? (#264)"""
    return bool(store.pending_approvals(wo_id) or store.held_approvals(wo_id)
                or awaiting_neo(wo_id))


def user_facing_wait(store: ProjectStore, wo_id: str) -> bool:
    """Is somebody ELSE holding this work order — a reviewer, or Neo?"""
    return bool(store.pending_approvals(wo_id) or awaiting_neo(wo_id))


def end_wait_if_nothing_is_out(store: ProjectStore, wo_id: str) -> bool:
    """Take a work order out of `waiting_input` once nothing is holding it there"""
    if store.get_work_order(wo_id)["status"] != "waiting_input":
        return False
    if something_is_out(store, wo_id):
        return False
    store.set_status(wo_id, "running")
    return True


def neo_question_blocker(question: dict[str, Any]) -> str:
    """The attention reason for a question Neo has handed back to the user"""
    if question["status"] == "failed":
        return (f"Neo could not answer question {question['id']} — "
                f"answer it: `jarvis neo answer {question['id']} \"…\"`")
    return (f"Neo escalated question {question['id']} to you — "
            f"answer it: `jarvis neo answer {question['id']} \"…\"`")


# -- invariants


def check_attention_reason_is_true(store: ProjectStore) -> Iterator[Violation]:
    """INV-ATTENTION-REASON — a flagged work order's reason must name the real blocker"""
    for wo in store.list_work_orders(include_hidden=True):
        if not wo["needs_attention"]:
            continue
        blockers = true_blockers(store, wo)
        if not blockers:
            continue
        reason = wo.get("attention_reason") or ""
        # Only assumptions are enforced strictly (#705)
        if _mentions_assumptions(blockers[0]) == _mentions_assumptions(reason):
            continue
        store.flag_attention(wo["id"], blockers[0])
        yield Violation(
            invariant="INV-ATTENTION-REASON",
            wo_id=wo["id"],
            detail=f"attention reason {reason!r} does not name the real blocker",
            repaired=True,
            repair=f"reason set to {blockers[0]!r}",
            context={"was": reason, "now": blockers[0]},
        )


def check_repaired_work_rejoins_the_merge_queue(store: ProjectStore) -> Iterator[Violation]:
    """INV-REPAIR-RESETTLED — a repaired pull request goes back in the merge queue (#705)"""
    from . import ops as ops_mod

    for wo in store.list_work_orders(statuses=("needs_review",), include_hidden=True):
        was = wo["attention_reason"]
        if not ops_mod.resettle_after_repair(store, wo["id"]):
            continue
        yield Violation(
            invariant="INV-REPAIR-RESETTLED",
            wo_id=wo["id"],
            detail="repaired pull request left out of the merge queue by a stale "
                   "pre-repair status snapshot",
            repaired=True,
            repair="status set to waiting_pr_merge; attention cleared",
            context={"was": "needs_review", "reason": was,
                     "now": store.get_work_order(wo["id"])["attention_reason"]},
        )


def check_adhoc_not_governed(store: ProjectStore) -> Iterator[Violation]:
    """INV-ADHOC-NOT-GOVERNED — an injected session must not be judged as a worker"""
    for wo in store.list_work_orders(statuses=("failed", "needs_review"),
                                     include_hidden=True):
        if (wo["origin"] not in UNGOVERNED_ORIGINS
                or store.pending_assumptions(wo["id"])):
            continue
        store.set_status(wo["id"], "completed")
        store.clear_attention(wo["id"])
        yield Violation(
            invariant="INV-ADHOC-NOT-GOVERNED",
            wo_id=wo["id"],
            detail=f"{wo['origin']} session held to the worker contract "
                   f"({wo.get('attention_reason') or wo['status']})",
            repaired=True,
            repair="retired to completed; attention cleared",
            context={"was": wo["status"], "reason": wo.get("attention_reason")},
        )


def check_legacy_adhoc_retired(store: ProjectStore) -> Iterator[Violation]:
    """INV-ADHOC-LEGACY-RETIRED — a session Jarvis adopted on its own is let go"""
    # Adoption only ever produced these two statuses
    for wo in store.list_work_orders(statuses=("running", "waiting_input"),
                                     include_hidden=True):
        if wo["origin"] != "adhoc" or store.pending_assumptions(wo["id"]):
            continue
        why = ("Jarvis no longer adopts sessions on its own (GitHub issue 47); "
               "this record was closed by the upgrade, not by anything the session did")
        store.set_status(wo["id"], "completed")
        store.clear_attention(wo["id"])
        store.add_event(wo["id"], "session_retired", {"why": why, "was": wo["status"]})
        yield Violation(
            invariant="INV-ADHOC-LEGACY-RETIRED",
            wo_id=wo["id"],
            detail=f"auto-adopted session still open ({wo['status']}) with nothing "
                   f"left to track it",
            repaired=True,
            repair="retired to completed; attention cleared",
            context={"was": wo["status"], "reason": wo.get("attention_reason")},
        )


def check_no_orphan_gate_requests(store: ProjectStore) -> Iterator[Violation]:
    """INV-GATE-ORPHAN — a gate request must not outlive the work order that filed it"""
    for wo in store.list_work_orders(statuses=TERMINAL_STATUSES, include_hidden=True):
        for approval in store.open_approvals(wo["id"]):
            store.supersede_approval(approval["id"], (
                f"work order is {wo['status']} — no worker is left to run this command, "
                f"so there is nothing to authorise or refuse"
            ))
            yield Violation(
                invariant="INV-GATE-ORPHAN",
                wo_id=wo["id"],
                detail=f"{wo['status']} work order still had gate request "
                       f"{approval['id']} ({approval['kind']}) pending"
                       + (" and escalated to the user" if approval["escalated"] else ""),
                repaired=True,
                repair=f"request {approval['id']} closed as superseded",
                context={"approval_id": approval["id"], "kind": approval["kind"],
                         "escalated": bool(approval["escalated"])},
            )


def check_no_phantom_attention(store: ProjectStore) -> Iterator[Violation]:
    """INV-ATTENTION-PHANTOM — a work order with nothing pending must not ask for you (docs/superpowers/specs/2026-10-09-an-unrepairable-invariant-owes-a-decision.md §5)"""
    for wo in store.list_work_orders(statuses=TERMINAL_STATUSES, include_hidden=True):
        if not wo["needs_attention"]:
            continue
        if true_blockers(store, wo):
            continue
        store.clear_attention(wo["id"])
        yield Violation(
            invariant="INV-ATTENTION-PHANTOM",
            wo_id=wo["id"],
            detail=f"{wo['status']} work order still flagged "
                   f"({wo.get('attention_reason') or 'no reason'})",
            repaired=True,
            repair="attention cleared",
        )


def check_assumption_flags_are_owed(store: ProjectStore) -> Iterator[Violation]:
    """INV-ATTENTION-PREMATURE — an assumption flag Neo, not the user, is going to answer (#711)"""
    for wo in store.list_work_orders(statuses=PRE_DELIVERY_STATUSES, include_hidden=True):
        if not wo["needs_attention"]:
            continue
        if not _mentions_assumptions(wo.get("attention_reason")):
            continue
        if not (store.pending_assumptions(wo["id"]) and neo_reviews_later(store, wo)):
            continue
        # An undeliverable objection raises a real blocker on a RUNNING order
        if any(_mentions_assumptions(b) for b in true_blockers(store, wo)):
            continue
        store.clear_attention(wo["id"])
        yield Violation(
            invariant="INV-ATTENTION-PREMATURE",
            wo_id=wo["id"],
            detail=f"{wo['status']} work order asked for you over assumptions the OS "
                   f"reviews itself on delivery",
            repaired=True,
            repair="attention cleared",
        )


def check_blocked_work_is_surfaced(store: ProjectStore) -> Iterator[Violation]:
    """INV-ATTENTION-MISSING — work that needs the user must actually say so"""
    # Every status `true_blockers` can derive for (docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md §5c)
    statuses = OPEN_STATUSES + ("failed",)
    for wo in store.list_work_orders(statuses=statuses, include_hidden=True):
        if wo["needs_attention"]:
            continue
        blockers = true_blockers(store, wo)
        if not blockers:
            continue
        store.flag_attention(wo["id"], blockers[0])
        yield Violation(
            invariant="INV-ATTENTION-MISSING",
            wo_id=wo["id"],
            detail=f"{wo['status']} work order needs the user ({blockers[0]}) "
                   f"but was not flagged",
            repaired=True,
            repair=f"flagged: {blockers[0]!r}",
        )


def check_messages_are_delivered(store: ProjectStore) -> Iterator[Violation]:
    """INV-MESSAGE-STUCK — a message queued for a worker must not sit undelivered for ever"""
    readonly = getattr(store, "readonly", False)
    now = time.time()
    for wo in store.list_work_orders(statuses=MESSAGE_STUCK_STATUSES,
                                     include_hidden=True):
        found = stuck_message(store, wo, now=now)
        if found is None:
            continue
        msg, why = found
        waited = int((now - float(msg["ts"])) // SECONDS_PER_MINUTE)
        blockers = [] if wo["needs_attention"] else true_blockers(store, wo, now=now)
        if blockers and not readonly:
            store.flag_attention(wo["id"], blockers[0])
        yield Violation(
            invariant="INV-MESSAGE-STUCK",
            wo_id=wo["id"],
            detail=(f"message {msg['id']} has been queued {waited} minute(s) and the "
                    f"worker has not seen it: {why}"),
            repaired=bool(blockers),
            repair=(("would flag: " if readonly else "flagged: ") + repr(blockers[0]))
                   if blockers else "",
            context={"msg_id": msg["id"], "source": msg["source"],
                     "waited_minutes": waited, "hold": why},
        )


def check_assumptions_persisted(store: ProjectStore) -> Iterator[Violation]:
    """INV-ASSUMPTION-PERSISTED — every recorded assumption must exist as a row"""
    for wo in store.list_work_orders(include_hidden=True):
        events = [e for e in store.list_events(wo["id"], limit=1000)
                  if e.get("kind") == "assumption"]
        if not events:
            continue
        rows = store.all_assumptions(wo["id"])
        stored = {r["content"] for r in rows}
        for e in events:
            payload = e.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            content = (payload or {}).get("content")
            if not content or content in stored:
                continue
            store.add_assumption(wo["id"], content)
            stored.add(content)
            yield Violation(
                invariant="INV-ASSUMPTION-PERSISTED",
                wo_id=wo["id"],
                detail="assumption recorded in the timeline had no row in the "
                       "review queue",
                repaired=True,
                repair="row reconstructed from the event payload",
                context={"content": content[:200]},
            )


def check_attention_has_reason(store: ProjectStore) -> Iterator[Violation]:
    """INV-ATTENTION-BLANK — a flagged work order must say what it wants"""
    for wo in store.list_work_orders(include_hidden=True):
        if not wo["needs_attention"] or (wo.get("attention_reason") or "").strip():
            continue
        blockers = true_blockers(store, wo)
        if blockers:
            store.flag_attention(wo["id"], blockers[0])
            yield Violation(
                invariant="INV-ATTENTION-BLANK", wo_id=wo["id"],
                detail="flagged with no reason",
                repaired=True, repair=f"reason set to {blockers[0]!r}",
            )
        else:
            yield Violation(
                invariant="INV-ATTENTION-BLANK", wo_id=wo["id"],
                detail="flagged with no reason and no derivable blocker",
            )


def check_envelopes_move(store: ProjectStore) -> Iterator[Violation]:
    """INV-ENVELOPE-STUCK — an envelope must not sit in the queue for ever"""
    from .bus import DELIVERY_ATTEMPT_CEILING, deliver

    stuck = [e for e in store.queued_envelopes()
             if int(e["attempts"] or 0) >= DELIVERY_ATTEMPT_CEILING]
    if not stuck:
        return
    if getattr(store, "readonly", False):
        # `jarvis doctor` without --repair
        for env in stuck:
            yield _envelope_violation(env, repair="would retry delivery once more")
        return

    from .central_store import CentralStore

    central = CentralStore()
    try:
        for env in stuck:
            try:
                state = deliver(store, central, env)
            except Exception as e:  # noqa: BLE001 — a broken route is what we are here for
                state, why = "queued", f"delivery raised {e!r}"
            else:
                why = "delivery left it queued"
            if state == "queued":
                note = (f"undeliverable after {int(env['attempts'] or 0) + 1} attempts: "
                        f"{why}")
                store.mark_envelope(env["id"], "undeliverable", note=note)
                repair = f"marked undeliverable — {why}"
            else:
                repair = f"retried; now {state}"
            yield _envelope_violation(env, repair=repair)
    finally:
        central.close()


def check_neo_escalations_are_live(store: ProjectStore) -> Iterator[Violation]:
    """INV-NEO-ESCALATION-STALE — a question held by the user must still be answerable"""
    from .neo_store import USER_HELD_Q_STATUSES, NeoStore
    from .ops import registered_project_paths

    readonly = getattr(store, "readonly", False)
    neo = NeoStore()
    try:
        held = [q for q in neo.list_questions(statuses=USER_HELD_Q_STATUSES)
                if q["kind"] in ("approval", "plan", "alarm", "triage",
                                 "assumption", "question")]
        # `triage` is the one kind whose subject is not a row in THIS database (#240)
        mine = (registered_project_paths()
                if any(q["kind"] == "triage" for q in held) else {})
        for q in held:
            if q["kind"] == "triage" and mine.get(q["project"]) != store.project_path:
                continue
            moot = {"approval": _stale_approval_question,
                    "plan": _stale_plan_question,
                    "alarm": _stale_alarm_question,
                    "triage": _stale_triage_question,
                    "assumption": _stale_assumption_question,
                    "question": _stale_worker_question}[q["kind"]](store, q)
            if moot is None:
                continue
            answer, why = moot
            # Neo's store is OS-wide, so the `_ReadOnly` proxy over the project store cannot intercept this write...
            if not readonly:
                neo.supersede(q["id"], answer, why)
            yield Violation(
                invariant="INV-NEO-ESCALATION-STALE",
                # A plan question names the FEATURE order when its planner is gone (`ops.submit_plan`)
                wo_id=q["wo_id"] if _is_work_order(store, q["wo_id"]) else None,
                detail=(f"Neo question {q['id']} ({q['kind']}) was still {q['status']} "
                        f"to the user, but {why}"),
                repaired=True,
                repair=("would close it as " if readonly else "closed as ") + answer,
                context={"question_id": q["id"], "kind": q["kind"], "was": q["status"]},
            )
    finally:
        neo.close()


def _is_work_order(store: ProjectStore, wo_id: str) -> bool:
    try:
        store.get_work_order(wo_id)
    except KeyError:
        return False
    return True


def _stale_approval_question(store: ProjectStore,
                             q: dict[str, Any]) -> tuple[str, str] | None:
    """(answer, why) if this approval question is moot, else None"""
    approval = store.approval_for_question(q["id"])
    if approval is None or approval["status"] == "pending":
        return None
    by = f" by {approval['decided_by']}" if approval["decided_by"] else ""
    return (f"SUPERSEDED — approval {approval['id']} is {approval['status']}",
            f"approval {approval['id']} was already {approval['status']}{by}")


def _stale_plan_question(store: ProjectStore,
                         q: dict[str, Any]) -> tuple[str, str] | None:
    """(answer, why) if this plan question is moot, else None"""
    fo = store.feature_order_for_planner(q["wo_id"])
    if fo is None:
        return None
    if fo["plan_question_id"] != q["id"]:
        return (f"SUPERSEDED by question {fo['plan_question_id']}",
                f"{fo['id']} has since been replanned and its review is now question "
                f"{fo['plan_question_id']}")
    if fo["status"] == "plan_review":
        return None
    return (f"SUPERSEDED — {fo['id']} is {fo['status']}",
            f"{fo['id']} left plan review and is now {fo['status']}")


def _stale_alarm_question(store: ProjectStore,
                          q: dict[str, Any]) -> tuple[str, str] | None:
    """(answer, why) if this alarm question is moot, else None"""
    alarm = store.alarm_for_question(q["id"])
    if alarm is None or alarm["status"] == "escalated":
        return None
    return (f"SUPERSEDED — alarm {alarm['id']} is {alarm['status']}",
            f"alarm {alarm['id']} was already {alarm['status']}")


def _stale_assumption_question(store: ProjectStore,
                               q: dict[str, Any]) -> tuple[str, str] | None:
    """(answer, why) if this assumption question is moot, else None"""
    assumption = store.assumption_for_question(q["id"])
    if assumption is None or assumption["status"] == "pending":
        return None
    return (f"SUPERSEDED — assumption {assumption['id']} is {assumption['status']}",
            f"assumption {assumption['id']} was already {assumption['status']}")


def _stale_worker_question(store: ProjectStore,
                           q: dict[str, Any]) -> tuple[str, str] | None:
    """(answer, why) if this worker question is moot, else None (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)"""
    if not _is_work_order(store, q["wo_id"]):
        return None
    asked = float(q["ts"])
    reviews = [float(e["ts"]) for e in store.events_of_kind(q["wo_id"], "reviewed")
               if float(e["ts"]) > asked]
    if not reviews:
        return None
    delivered = [float(e["ts"]) for e in store.events_of_kind(q["wo_id"], "finished")
                 if asked < float(e["ts"]) < max(reviews)]
    if not delivered:
        return None
    return ("SUPERSEDED — the user reviewed a delivery made after this question was "
            "asked",
            f"the user reviewed work order {q['wo_id']} after it was asked, on a "
            f"delivery newer than the question")


def _stale_triage_question(store: ProjectStore,
                           q: dict[str, Any]) -> tuple[str, str] | None:
    """(answer, why) if this priority re-assessment is moot, else None (#240)"""
    from . import issues
    from .central_store import CentralStore
    from .project_store import TERMINAL_STATUSES

    payload = issues.triage_payload(q)
    url = (payload or {}).get("issue_url") or ""
    if not url:
        # A question whose context nobody can parse
        return None
    for wo in store.work_orders_for_issue(url):
        if wo["status"] not in TERMINAL_STATUSES:
            return (f"SUPERSEDED — {wo['id']} is already on this issue",
                    f"work order {wo['id']} is live on {url}, so the rating changes "
                    f"nothing")
    item_id = (payload or {}).get("backlog_id") or ""
    if not item_id:
        return None
    central = CentralStore()
    try:
        item = central.get_backlog(item_id)
    finally:
        central.close()
    if item is None:
        return ("SUPERSEDED — the backlog item this was about is gone",
                f"backlog item {item_id} no longer exists")
    if item["status"] == "open":
        return None
    return (f"SUPERSEDED — backlog item {item_id} is {item['status']}",
            f"backlog item {item_id} was already {item['status']}, so the rating "
            f"changes nothing")


def check_proposed_remedies_are_live(store: ProjectStore) -> Iterator[Violation]:
    """INV-REMEDY-PROPOSAL-STALE — a proposal nobody can answer must not sit for ever"""
    from .supervisor import ALARM_BLOCKER

    readonly = getattr(store, "readonly", False)
    for alarm in store.alarms_across(statuses=("proposed",)):
        approval_id = alarm.get("remedy_approval_id")
        approval = store.get_approval(int(approval_id)) if approval_id else None
        if approval is not None and approval["status"] in ("pending", "approved"):
            continue
        was = approval["status"] if approval else "gone"
        why = (f"its gate request is {was}, so nothing will decide it and the remedy "
               f"can never be applied")
        if not readonly:
            store.update_alarm(alarm["id"], status="escalated",
                               verdict_reason=f"the {alarm['remedy']} proposal was "
                                              f"abandoned: {why}")
            store.flag_attention(alarm["wo_id"], ALARM_BLOCKER.format(
                alarm_id=alarm["id"]))
        yield Violation(
            invariant="INV-REMEDY-PROPOSAL-STALE",
            wo_id=alarm["wo_id"],
            detail=(f"alarm {alarm['id']} was still proposing `{alarm['remedy']}`, but "
                    f"{why}"),
            repaired=True,
            repair=("would return it to " if readonly else "returned it to ")
                   + "escalated, with the user",
            context={"alarm_id": alarm["id"], "remedy": alarm["remedy"],
                     "approval_id": approval_id, "approval_status": was},
        )


def _envelope_violation(env: dict[str, Any], repair: str) -> Violation:
    return Violation(
        invariant="INV-ENVELOPE-STUCK",
        wo_id=env["subject_wo_id"],
        detail=(f"envelope {env['id']} ({env['kind']} to role {env['to_role']}) was "
                f"still queued after {env['attempts']} delivery attempts"),
        repaired=True,
        repair=repair,
        context={"envelope_id": env["id"], "kind": env["kind"],
                 "to_role": env["to_role"], "attempts": env["attempts"],
                 "subject_fo_id": env["subject_fo_id"]},
    )


def check_paused_turns_resume(store: ProjectStore) -> Iterator[Violation]:
    """INV-PAUSE-OVERDUE — a paused turn whose wait is over must actually be relaunched (#259)"""
    for wo in store.list_work_orders(statuses=RETRY_SWEEP_STATUSES):
        if wo["origin"] in UNGOVERNED_ORIGINS:
            continue  # the user's own session; Jarvis does not drive it
        try:
            pause = worker_session.turn_pause(store, wo["id"])
        except Exception as e:  # noqa: BLE001 — one work order must not stall the check
            yield Violation(invariant="INV-PAUSE-OVERDUE", wo_id=wo["id"],
                            detail=f"could not diagnose the pause: {e!r}")
            continue
        if pause is None or not pause.resumable:
            continue
        # The sweep looked at this one and deferred it; nothing is broken (docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md)
        if retry_hold(store, wo, pause) is not None:
            continue
        overdue = time.time() - pause.retry_at
        if overdue <= PAUSE_OVERDUE_GRACE:
            continue
        yield Violation(
            invariant="INV-PAUSE-OVERDUE", wo_id=wo["id"],
            detail=(f"paused for the {worker_session.PAUSE_NOUN.get(pause.reason, pause.reason)} since "
                    f"{clock(pause.turn.get('ended_at') or pause.turn['started_at'])}, "
                    f"due to retry at {clock(pause.retry_at)} and still not relaunched "
                    f"{_span(overdue)} later — the OS is not healing this by itself"),
            context={"reason": pause.reason, "retry_at": pause.retry_at,
                     "overdue_seconds": round(overdue),
                     "attempts": pause.attempts, "of": pause.max_attempts,
                     "turn_seq": pause.turn["seq"]},
        )


def check_pause_deadline_stable(store: ProjectStore) -> Iterator[Violation]:
    """INV-PAUSE-DRIFT — a pause's deadline must still be the one it was given"""
    for wo in store.list_work_orders(statuses=RETRY_SWEEP_STATUSES):
        if wo["origin"] in UNGOVERNED_ORIGINS:
            continue
        try:
            pause = worker_session.turn_pause(store, wo["id"])
        except Exception:  # noqa: BLE001 — INV-PAUSE-OVERDUE reports the broken diagnosis
            continue
        if (pause is None or pause.reason != worker_session.PAUSE_USAGE_LIMIT
                or pause.reset_at is None):
            continue
        events = store.events_of_kind(wo["id"], "turn_paused")
        if not events:
            continue
        payload = events[-1].get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        recorded = payload.get("reset_at")
        if recorded is None or payload.get("seq") != pause.turn["seq"]:
            continue
        # The reset is a clock time rounded to the minute
        if abs(float(recorded) - pause.reset_at) <= 60:
            continue
        yield Violation(
            invariant="INV-PAUSE-DRIFT", wo_id=wo["id"],
            detail=(f"the retry deadline moved: recorded {clock(float(recorded))} when "
                    f"the turn was refused, re-derived as {clock(pause.reset_at)} now — "
                    f"the pause is not a pure function of the turn, so the retry pass is "
                    f"chasing a moment that keeps moving"),
            context={"recorded_reset_at": float(recorded),
                     "derived_reset_at": pause.reset_at,
                     "drift_seconds": round(pause.reset_at - float(recorded)),
                     "turn_seq": pause.turn["seq"]},
        )


def check_no_lost_feedback(store: ProjectStore) -> Iterator[Violation]:
    """INV-ENVELOPE-LOST — a message that reached nobody must not pass for one that did"""
    for env in store.envelopes():
        if env["state"] != "undeliverable":
            continue
        subject, still_open = env["subject_wo_id"] or env["subject_fo_id"], False
        try:
            if env["subject_wo_id"]:
                still_open = (store.get_work_order(env["subject_wo_id"])["status"]
                              in OPEN_STATUSES)
            elif env["subject_fo_id"]:
                still_open = (store.get_feature_order(env["subject_fo_id"])["status"]
                              in FO_OPEN_STATUSES)
        except KeyError:
            # Deleted out from under the envelope
            still_open = False
        if not still_open:
            continue
        lost = (f"envelope {env['id']} ({env['kind']} to role {env['to_role']}) "
                f"about {subject} reached nobody: {env['note'] or 'undeliverable'}")
        # LEADS WITH WHAT IS SETTLED for a lost `manager` envelope: the user read the SUBJECT as the...
        manager, fo_id = None, None
        if env["to_role"] == "manager":
            from .bus import _feature_of

            fo_id = _feature_of(store, env)
            manager = store.manager_work_order(fo_id) if fo_id else None
        if manager is not None:
            detail = (f"the manager work order {manager['id']} is {manager['status']} "
                      f"under feature {fo_id}, so {lost}")
            if env["subject_wo_id"]:
                detail += (f". {env['subject_wo_id']} is held until the feature is live "
                           f"again")
        else:
            detail = lost
        yield Violation(
            invariant="INV-ENVELOPE-LOST",
            wo_id=env["subject_wo_id"],
            detail=detail,
            context={"envelope_id": env["id"], "kind": env["kind"],
                     "to_role": env["to_role"], "state": env["state"],
                     "subject_fo_id": env["subject_fo_id"],
                     "manager_wo_id": manager["id"] if manager else None,
                     "manager_status": manager["status"] if manager else None},
        )


def check_validation_progresses(store: ProjectStore) -> Iterator[Violation]:
    """INV-VALIDATION-STRANDED — a unit under review must not sit on an open round for ever (docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §3)"""
    per_round = _validation_timeout()
    threshold = 2 * per_round
    now = time.time()
    cutoff = now - threshold
    open_marks = ",".join("?" * len(OPEN_STATUSES))
    for kind, id_col, rows in (
        ("work order", "wo_id", store.conn.execute(
            f"""SELECT DISTINCT w.id AS id FROM work_orders w
                  JOIN validation_rounds r ON r.wo_id = w.id
                 WHERE r.outcome='pending' AND w.status IN ({open_marks})""",
            OPEN_STATUSES).fetchall()),
        ("feature order", "fo_id", store.conn.execute(
            "SELECT id FROM feature_orders WHERE status='validating'").fetchall()),
    ):
        for row in rows:
            unit_id = row["id"]
            latest = store.latest_validation_round(**{id_col: unit_id})
            if latest is None or latest["outcome"] != "pending":
                continue
            if float(latest["ts"] or 0) > cutoff:
                continue  # late, not abandoned
            age = int(now - float(latest["ts"] or 0))
            store.close_validation_round(
                int(latest["id"]), "failed",
                f"the review was interrupted — round {latest['round']} was left open "
                f"with nothing running it for {age}s")
            yield Violation(
                invariant="INV-VALIDATION-STRANDED",
                wo_id=unit_id if id_col == "wo_id" else None,
                detail=(
                    f"{kind} {unit_id} has been `validating` on a `pending` round for "
                    f"{age}s — over twice the {per_round}s a round is given. Nothing is "
                    f"judging it; the daemon almost certainly restarted mid-round."
                ),
                repaired=True,
                repair=(f"closed round {latest['round']} `failed` — the next tick "
                        f"picks it up and runs it again"),
                context={"round_id": latest["id"], "round": latest["round"],
                         "age_seconds": age, "threshold_seconds": threshold,
                         "unit": kind,
                         "fo_id": None if id_col == "wo_id" else unit_id},
            )
    yield from _passed_round_never_landed(store)
    yield from _give_up_parked_in_the_merge_queue(store)


def _passed_round_never_landed(store: ProjectStore) -> Iterator[Violation]:
    """INV-VALIDATION-STRANDED, second arm — a round that PASSED and never landed (docs/superpowers/specs/2026-10-09-a-passed-round-that-never-landed-is-stranded.md)"""
    from .daemon import VALIDATION_LANDED

    rows = store.conn.execute(
        "SELECT id FROM work_orders WHERE status='validating'").fetchall()
    readonly = getattr(store, "readonly", False)
    for row in rows:
        wo_id = row["id"]
        latest = store.latest_validation_round(wo_id=wo_id)
        if latest is None or latest["outcome"] != "passed":
            continue
        if worker_session.busy(store, wo_id) is not None:
            continue  # the deferral is correct there: a worker is typing
        landed = store.last_event_of_kind(wo_id, VALIDATION_LANDED)
        done = (db.from_json(landed.get("payload"), {}) or {}).get("round_id") \
            if landed else None
        if done is not None and int(done) == int(latest["id"]):
            continue  # spec §1: this round's landing is already recorded
        detail = (
            f"work order {wo_id} is `validating` on a round that PASSED — round "
            f"{latest['round']} is closed and no worker turn is in flight, so nothing in "
            f"the OS will ever land it. The landing was deferred and its delegate is "
            f"unreachable for this status."
        )
        context = {"round_id": latest["id"], "round": latest["round"],
                   "unit": "work order", "fo_id": None}
        if readonly:
            # `jarvis doctor` without --repair
            yield Violation(
                invariant="INV-VALIDATION-STRANDED", wo_id=wo_id, detail=detail,
                repaired=True,
                repair="would land the passed round — `ops.land_when_cleared`",
                context=context,
            )
            continue
        from . import ops as ops_mod

        fresh_wo = store.get_work_order(wo_id)
        status = ops_mod.land_when_cleared(store, fresh_wo)
        store.add_event(wo_id, VALIDATION_LANDED, {"round_id": int(latest["id"])})
        yield Violation(
            invariant="INV-VALIDATION-STRANDED", wo_id=wo_id, detail=detail,
            repaired=True,
            repair=f"landed the passed round — the work order is now `{status}`",
            context=context,
        )


#: The event the third arm writes when it puts a give-up back on the user's list
GIVE_UP_REPARKED = "give_up_reparked"


def _give_up_parked_in_the_merge_queue(store: ProjectStore) -> Iterator[Violation]:
    """INV-VALIDATION-STRANDED, third arm — a panel give-up parked in the merge queue (docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md §B.)"""
    rows = store.conn.execute(
        "SELECT id FROM work_orders WHERE status='waiting_pr_merge'").fetchall()
    readonly = getattr(store, "readonly", False)
    for row in rows:
        wo_id = row["id"]
        wo = store.get_work_order(wo_id)
        if not parked_on_a_give_up(store, wo):
            continue
        if store.pending_assumptions(wo_id):
            continue  # the `needs_review` arm's decision outranks this one
        if worker_session.busy(store, wo_id) is not None \
                or store.queued_messages(wo_id):
            continue  # no status is written under a live turn
        judged = store.last_judged_round(wo_id=wo_id)
        detail = (
            f"work order {wo_id} is `waiting_pr_merge` on a round that GAVE UP — round "
            f"{judged['round']} escalated and nobody has decided it, while the merge is "
            f"held on `{HELD_NOT_PASSED}`. The status carries no review control and "
            f"nothing re-derives it."
        )
        context = {"round_id": judged["id"], "round": judged["round"],
                   "unit": "work order", "fo_id": None}
        if readonly:
            yield Violation(
                invariant="INV-VALIDATION-STRANDED", wo_id=wo_id, detail=detail,
                repaired=True,
                repair="would set the status to `needs_review` — the give-up is owed",
                context=context,
            )
            continue
        store.set_status(wo_id, "needs_review", trigger=GIVE_UP_REPARKED)
        # `round` beside the id so `timeline` can name it without a lookup
        store.add_event(wo_id, GIVE_UP_REPARKED,
                        {"round_id": int(judged["id"]), "round": int(judged["round"]),
                         "was": "waiting_pr_merge"})
        yield Violation(
            invariant="INV-VALIDATION-STRANDED", wo_id=wo_id, detail=detail,
            repaired=True,
            repair="status set to needs_review; the give-up is owed again",
            context=context,
        )


def check_feature_failures_are_real(store: ProjectStore) -> Iterator[Violation]:
    """INV-FEATURE-FALSE-FAILURE — a failed feature whose children all recovered goes back"""
    for row in store.conn.execute(
            "SELECT id, attention_reason FROM feature_orders WHERE status='failed'"
    ).fetchall():
        fo_id = row["id"]
        children = store.feature_children(fo_id)
        if not children or dead_feature_children(store, children):
            continue
        store.set_feature_status(fo_id, "executing")
        store.clear_feature_attention(fo_id)
        # A reopened feature with a `completed` manager has no addressee for its next round's feedback
        from .ops import lower_settled_feature_holds, revive_feature_manager

        revived = revive_feature_manager(store, fo_id, why="INV-FEATURE-FALSE-FAILURE")
        # ...and the children's holds go down with it: SETTLED_FEATURE_BLOCKER is now false and nothing...
        lower_settled_feature_holds(store, children)
        yield Violation(
            invariant="INV-FEATURE-FALSE-FAILURE",
            detail=(f"feature order {fo_id} is `failed` — \"{row['attention_reason']}\" "
                    f"— but not one of its {len(children)} children is failed or "
                    f"cancelled any more. The child recovered after the feature settled, "
                    f"and nothing re-derives a settled feature."),
            repaired=True,
            repair=("back to `executing` — the next tick settles it on the real state"
                    + (", manager revived" if revived else "")),
            context={"fo_id": fo_id, "children": len(children),
                     "stale_reason": row["attention_reason"],
                     "manager_revived": revived["id"] if revived else None},
        )


def _validation_timeout() -> int:
    """How long one validation round is allowed to take, per the LIVE catalog"""
    from .ops import validation_config

    timeout = getattr(validation_config(), "timeout", None)
    return int(timeout) if timeout else DEFAULT_VALIDATION_TIMEOUT


#: How many consecutive failed sweeps mean the sweep itself is broken rather than unlucky
HEALTH_SWEEP_FAILURE_RUN = 10

#: How recent the newest failure must be for the run to still be happening
HEALTH_SWEEP_FAILURE_WINDOW_MINUTES = 120


def check_health_sweep_produces_judgements(store: ProjectStore) -> Iterator[Violation]:
    """INV-HEALTH-SWEEP-MUTE — a sweep that never judges anything must not bill silently (#216)"""
    if store.health_sweep_hold() is not None:
        # The check claims the sweep IS SPENDING model calls right now
        return
    # HELD ROWS ARE FILTERED OUT BEFORE THE LAST TEN ARE TAKEN, not skipped in the loop
    recent = store.recent_health_reviews(HEALTH_SWEEP_FAILURE_RUN, include_held=False)
    if len(recent) < HEALTH_SWEEP_FAILURE_RUN:
        return
    if any(r["outcome"] != "failed" for r in recent):
        return
    window = HEALTH_SWEEP_FAILURE_WINDOW_MINUTES * 60
    if db.now() - float(recent[0]["ts"] or 0.0) > window:
        return
    yield Violation(
        invariant="INV-HEALTH-SWEEP-MUTE",
        # FAILING, never held: the word "held" is reserved for the usage-limit rows
        detail=(f"the last {HEALTH_SWEEP_FAILURE_RUN} health sweeps all FAILED (the "
                f"account's usage window is a hold, not a failure), so the sweep is "
                f"spending model calls and producing no judgement. Most recent "
                f"reason: {str(recent[0]['detail'] or '(none recorded)')[:200]}"),
        context={"failures": HEALTH_SWEEP_FAILURE_RUN,
                 "last_detail": str(recent[0]["detail"] or "")[:500]},
    )


#: How long the OS's own sweep may produce no judgement before the user is told
OS_HEALTH_SWEEP_DARK_MINUTES = 180


def _live_catalog() -> Any:
    """The registered catalog, or None"""
    try:
        from .ops import resolve_catalog

        return resolve_catalog()
    except Exception:  # noqa: BLE001 — no catalog, an unreadable one, a moved path
        return None


def _os_owning_project(store: ProjectStore) -> Any:
    """This store's `ProjectSpec` if it is the project that runs the OS, else None"""
    from . import schedule

    catalog = _live_catalog()
    if catalog is None:
        return None
    try:
        owner = schedule.os_project((p.name, p.path) for p in catalog.projects)
        if owner is None:
            return None
        spec = catalog.project(owner)
        if Path(spec.path).resolve() != Path(store.project_path).resolve():
            return None
        return spec
    except Exception:  # noqa: BLE001 — same rule: never the thing that raises
        return None


def _health_hold_seconds(store: ProjectStore, since: float, until: float) -> float:
    """How much of `[since, until]` the account's usage window was shut for"""
    spans = []
    for row in store.conn.execute(
            "SELECT ts, reopens_at FROM health_reviews WHERE outcome='held' "
            "AND reopens_at > ? ORDER BY ts", (since,)).fetchall():
        start = max(float(row["ts"] or 0.0), since)
        end = min(float(row["reopens_at"] or 0.0), until)
        if end > start:
            spans.append((start, end))
    total, edge = 0.0, since
    for start, end in spans:
        start = max(start, edge)
        if end > start:
            total += end - start
            edge = end
    return total


def check_os_health_sweep_alive(store: ProjectStore) -> Iterator[Violation]:
    """INV-OS-HEALTH-SWEEP-DARK — the OS's own health sweep must never be dark (docs/superpowers/specs/2026-09-28-a-usage-limit-is-not-a-failed-sweep.md §5)"""
    spec = _os_owning_project(store)
    if spec is None:
        return
    name = spec.name
    supervisor = getattr(spec, "supervisor", None)
    for key, on in (("supervisor.enabled", getattr(supervisor, "enabled", False)),
                    ("supervisor.health_enabled",
                     getattr(supervisor, "health_enabled", False))):
        if not on:
            yield Violation(
                invariant="INV-OS-HEALTH-SWEEP-DARK",
                detail=(f"the OS's own health sweep is DISABLED ({key}=false) and must "
                        f"not be — {name} runs the OS itself, and its sweep must always "
                        f"be on and producing judgements (user rule, 2026-09-28)"),
                level="critical", context={"project": name, "cause": "disabled",
                                           "key": key},
            )
            return

    now = db.now()
    window = OS_HEALTH_SWEEP_DARK_MINUTES * 60
    judged = store.conn.execute(
        "SELECT ts FROM health_reviews WHERE outcome IN ('clear','findings') "
        "ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
    # With no judgement ever recorded the clock starts at the window's own edge: the check then fires as...
    since = float(judged["ts"]) if judged else now - window
    dark = (now - since) - _health_hold_seconds(store, since, now)
    if dark < window:
        return

    minutes = int(dark / 60)
    newest = store.recent_health_reviews(1, include_held=False)
    if newest and newest[0]["outcome"] == "failed":
        detail = (f"the OS's own health sweep has produced no judgement for {minutes}m; "
                  f"the newest sweep FAILED: "
                  f"{str(newest[0]['detail'] or '(none recorded)')[:200]}")
        cause = "failing"
    elif judged is None and not newest:
        detail = ("the OS's own health sweep is enabled but has never run — no sweep "
                  "has been scheduled")
        cause = "not-scheduled"
    else:
        detail = (f"the OS's own health sweep has produced no judgement for {minutes}m, "
                  f"and {name} runs the OS itself")
        cause = "silent"
    yield Violation(
        invariant="INV-OS-HEALTH-SWEEP-DARK", detail=detail, level="critical",
        context={"project": name, "cause": cause, "dark_minutes": minutes},
    )


def check_stuck_sweep_alive(store: ProjectStore) -> Iterator[Violation]:
    """INV-STUCK-SWEEP-DARK — the sweep must never be the thing that is stuck. §8"""
    from .central_store import CentralStore
    from .daemon import Daemon

    if _os_owning_project(store) is None:
        return
    catalog = _live_catalog()
    if catalog is None or not any(p.fleet_health.enabled for p in catalog.projects):
        return
    central = CentralStore()
    try:
        run = db.from_json(central.get_state(Daemon.STUCK_RUN_KEY), None)
    finally:
        central.close()
    # The FLEET number, off `catalog.os` the way `Daemon.stuck_tick` reads `max_per_day`: one fleet-wide...
    window = catalog.os.fleet_health.sweep_dark_minutes * 60
    if run and str(run.get("error") or ""):
        yield Violation(
            invariant="INV-STUCK-SWEEP-DARK",
            detail=(f"the stuck sweep's newest run FAILED, so no order is being judged "
                    f"for time in status: {str(run['error'])[:200]}"),
            level="critical", context={"cause": "failing",
                                       "error": str(run["error"])[:500]},
        )
        return
    if run is None:
        # NEVER SWEPT is only dark while the OS is UP
        from .daemon import daemon_running

        if not daemon_running():
            return
    dark = db.now() - float((run or {}).get("ts") or 0.0)
    if run is not None and dark < window:
        return
    minutes = int(dark / 60) if run else 0
    yield Violation(
        invariant="INV-STUCK-SWEEP-DARK",
        detail=(f"the stuck sweep has not run for {minutes}m" if run else
                "the stuck sweep is enabled but has never run — no sweep has been "
                "scheduled") + ", so an order that stopped moving is nobody's finding",
        level="critical", context={"cause": "dark", "dark_minutes": minutes},
    )


def check_work_lands(store: ProjectStore) -> Iterator[Violation]:
    """INV-WORK-LANDED — a completed work order's pull request must have merged (#232)"""
    from . import landing

    population = 0
    unchecked = 0
    for wo in store.list_work_orders(statuses=("completed",), include_hidden=True):
        wo_id = wo["id"]
        if store.work_abandoned(wo_id) or store.work_unlanded_open(
                wo_id, closed_by="marked_done"):
            continue  # the decision was taken and written down; that is the whole ask
        if not wo.get("pr_url"):
            continue  # INV-PR-RECORDED's question, not this one's — see the docstring
        population += 1
        seen = store.events_of_kind(wo_id, "landing_seen")
        if not seen or db.now() - float(seen[-1]["ts"] or 0.0) >= landing.FRESH_FOR_SECONDS:
            # No current answer about this pull request
            unchecked += 1
            continue
        found = landing.from_record(wo_id, db.from_json(seen[-1]["payload"], {}))
        if not found.unsettled:
            continue
        # The two remedies differ in their FIRST clause
        remedy = ("Merge it" if found.verdict == landing.AWAITING_MERGE
                  else "Re-open and merge it")
        # BUILT ONCE and assigned to both: it is the detail AND the owed decision (docs/superpowers/specs/2026-10-09-an-unrepairable-invariant-owes-a-decision.md §6)
        sentence = (f"completed, but its pull request has not merged: {found.detail}. "
                    f"{remedy}, or record the decision to drop it with `jarvis wo "
                    f"finish {wo_id} --summary \"...\" --abandon \"<why>\"`.")
        yield Violation(
            invariant=INV_WORK_LANDED,
            wo_id=wo_id,
            detail=sentence,
            owed=sentence,
            context={"verdict": found.verdict, "pr_url": found.pr_url,
                     "pr_state": found.pr_state},
        )

    if unchecked:
        days = int(landing.FRESH_FOR_SECONDS // 86400)
        yield Violation(
            invariant="INV-LANDING-AUDIT-FRESH",
            detail=(f"the landing audit has no current answer for {unchecked} of "
                    f"{population} completed work order(s) carrying a pull request, so "
                    f"INV-WORK-LANDED is silent about them — which is NOT the same as "
                    f"saying they landed. `Daemon.refresh_landings` fills this in on the "
                    f"hourly sweep ({landing.REFRESH_PER_SWEEP} per sweep, re-asked every "
                    f"{days} days); a count that does not shrink means the daemon is not "
                    f"running or `gh` cannot read this repository's pull requests."),
            context={"unchecked": unchecked, "population": population},
        )


def check_pull_request_recorded(store: ProjectStore) -> Iterator[Violation]:
    """INV-PR-RECORDED — a settled work order that wrote code must carry its pull request (docs/superpowers/specs/2026-09-18-an-order-that-wrote-code-carries-its-pull-request.md)"""
    from . import landing

    for wo in store.list_work_orders(statuses=("completed",), include_hidden=True):
        wo_id = wo["id"]
        if wo.get("pr_url"):
            continue
        if store.work_abandoned(wo_id) or store.work_unlanded_open(
                wo_id, closed_by="marked_done"):
            continue
        work = landing.latest_authorship(
            [(float(e["ts"]), db.from_json(e["payload"], {}))
             for kind in landing.SETTLEMENT_EVENTS
             for e in store.events_of_kind(wo_id, kind)])
        if work is None or not work.produced:
            continue  # nobody looked, or it wrote nothing — see the docstring on both
        yield Violation(
            invariant="INV-PR-RECORDED",
            wo_id=wo_id,
            detail=(f"completed over {work.describe()} with no pull request recorded, "
                    f"so nothing can say whether that work reached "
                    f"`{work.base or 'the default branch'}`. Record the pull request "
                    f"with `jarvis wo finish {wo_id} --summary \"...\" --pr <url>`, or "
                    f"the decision to drop it with `--abandon \"<why>\"`."),
            context={"branch": work.branch, "base": work.base,
                     "commits": work.commits, "dirty": list(work.dirty[:10])},
        )


def check_manager_slots(store: ProjectStore) -> Iterator[Violation]:
    """INV-MANAGER-SLOTS — a project manager order must not spend a concurrency slot (#134)"""
    counted = store.count_active()
    marks = ",".join("?" for _ in SLOT_STATUSES)
    row = store.conn.execute(
        f"SELECT COUNT(*) c FROM work_orders "
        f"WHERE status IN ({marks}) AND kind != 'manager'",
        SLOT_STATUSES,
    ).fetchone()
    expected = int(row["c"])
    if counted == expected:
        return
    yield Violation(
        invariant="INV-MANAGER-SLOTS",
        detail=(
            f"`ProjectStore.count_active` returned {counted} where {expected} work "
            f"orders hold a slot and are not managers: the manager exemption has "
            f"regressed. A project manager order runs a turn every time its feature "
            f"reports, for that feature's whole life, so counting one spends a "
            f"`max_concurrent` slot the work itself needs and the project dispatches "
            f"less and less. Restore the "
            f"`kind != 'manager'` filter in `count_active` (project_store.py); the "
            f"reasoning is in that method's docstring and in work order wo-9652be2f."
        ),
        repaired=False,
        context={"count_active": counted, "active_non_manager": expected},
    )


# -- configuration checks (catalog, not database state)


def check_gate_deny_conflict(spec: Any) -> Iterator[Violation]:
    """INV-GATE-DENY-CONFLICT — an enabled gate must not also be denied outright"""
    from .gates import deny_conflicts

    if not spec.gates:
        return
    deny_rules = (
        (spec.settings_overrides.get("permissions") or {}).get("deny") or []
    )
    for gate_name, rule in deny_conflicts(spec.gates, deny_rules):
        yield Violation(
            invariant="INV-GATE-DENY-CONFLICT",
            detail=(
                f"project {spec.name!r} enables the `{gate_name}` gate but also denies "
                f"{rule!r} in settings_overrides. A deny rule beats a hook's allow, so "
                f"approval can never take effect: remove {rule!r} from the catalog and "
                f"let the gate mediate it."
            ),
            context={"project": spec.name, "gate": gate_name, "rule": rule},
        )


def check_catalog(catalog: Any, project: str | None = None) -> list[Violation]:
    """Config-level post-conditions over the catalog itself"""
    found: list[Violation] = []
    for spec in catalog.projects:
        if project and spec.name != project:
            continue
        found.extend(check_gate_deny_conflict(spec))
    return found


# -- OS-level checks ($JARVIS_HOME state, not any project's database)

#: How long a pending-release marker may sit in flight before it is a fault
RELEASE_MARKER_STALE_AFTER = 3600.0


def check_gate_canaries() -> Iterator[Violation]:
    """INV-GATE-CANARY — every command that must gate still gates"""
    from .central_store import CentralStore
    from .gate_rules import RuleSet

    central = CentralStore()
    try:
        failures = RuleSet.load(central).check_canaries()
    finally:
        central.close()
    for f in failures:
        yield Violation(
            invariant="INV-GATE-CANARY",
            detail=(
                f"`{f['command'].splitlines()[0]}` must always trip the "
                f"`{f['kind']}` gate, and no longer does: {f['why']}. Inspect the rule "
                f"base with `jarvis gate rules` and retract what cleared it with "
                f"`jarvis gate rule-retract <id> --reason \"...\"`."
            ),
            context=f,
        )


def check_release_marker(now: float | None = None) -> list[Violation]:
    """INV-RELEASE-MARKER-STALE — a staged release must not sit unapplied for an hour"""
    from . import release

    marker = release.read_marker()
    if marker is None:
        if release.marker_path().exists():
            return [Violation(
                invariant="INV-RELEASE-MARKER-STALE",
                detail=f"{release.marker_path()} exists but is not readable JSON — "
                       f"no release state can act on it",
            )]
        return []
    state = marker.get("state")
    if state not in ("staged", "restarting"):
        return []
    now = now if now is not None else time.time()
    reference = float(marker.get("restart_at") or marker.get("staged_at") or 0)
    age = now - reference
    if age <= RELEASE_MARKER_STALE_AFTER:
        return []
    return [Violation(
        invariant="INV-RELEASE-MARKER-STALE",
        wo_id=marker.get("wo_id"),
        detail=(f"pending release {marker.get('tag')} has been {state!r} for "
                f"{int(age // 60)} minutes — the daemon should have applied and "
                f"verified it within minutes (marker: {release.marker_path()})"),
        context={"state": state, "tag": marker.get("tag"), "age_seconds": int(age)},
    )]


# -- OS-level invariants


def check_ui_healthy() -> Iterator[Violation]:
    """INV-UI-HEALTHY — the OS's own dashboard must not be throwing 500s"""
    from . import uilog

    recent, total = uilog.recent_errors()
    if not total:
        return
    hours = int(uilog.ERROR_WINDOW_SECONDS / 3600)
    yield Violation(
        invariant="INV-UI-HEALTHY",
        detail=f"the dashboard raised {total} unhandled error"
               f"{'s' if total != 1 else ''} in the last {hours}h "
               f"(latest: {recent[0].summary}) — see {uilog.ui_log_path()}",
        context={"count": total, "log": str(uilog.ui_log_path()),
                 "recent": [e.as_dict() for e in recent]},
    )


def check_ui_wedged() -> Iterator[Violation]:
    """INV-UI-WEDGED — the dashboard has stopped serving and said so about itself (docs/superpowers/specs/2026-10-08-the-dashboard-reports-and-heals-its-own-wedge.md)"""
    from . import release, uilog

    stamp = uilog.read_wedge()
    if not stamp:
        return
    try:
        last = float(stamp.get("at") or stamp.get("since") or 0)
        since = float(stamp.get("since") or 0)
    except (TypeError, ValueError):
        return
    # Window-based, exactly as `check_ui_healthy` is and for its reason: the restarted dashboard is a new...
    if not last or time.time() - last > uilog.ERROR_WINDOW_SECONDS:
        return
    age = max(0.0, time.time() - since) if since else 0.0
    restarts, cap, cap_spent = _ui_restart_counts()
    yield Violation(
        invariant="INV-UI-WEDGED",
        detail=(f"the dashboard reported its threadpool wedged "
                f"{int(age // 60)} minutes ago and dumped every thread's stack to "
                f"{stamp.get('dump') or uilog.stack_dump_path()} — the OS has "
                f"self-restarted {release.UI_UNIT} {restarts} time"
                f"{'s' if restarts != 1 else ''} in the last 24h"
                f"{f', which is the cap of {cap}' if cap_spent else ''} "
                f"(stamp: {uilog.wedge_stamp_path()})"),
        level="critical" if cap_spent else "warning",
        context={"since": since, "age_seconds": int(age),
                 "dump": stamp.get("dump"), "limiter": stamp.get("limiter"),
                 "version": stamp.get("version"), "restarts": restarts,
                 "cap": cap, "cap_spent": cap_spent},
    )


def _ui_restart_counts() -> tuple[int, int, bool]:
    """Self-restarts inside the rolling 24h, the cap, and whether it is spent"""
    from . import db, ops, uilog
    from .central_store import CentralStore

    cap = int(getattr(ops.ui_health_config(), "max_restarts_per_day", 0) or 0)
    try:
        central = CentralStore()
    except Exception:  # noqa: BLE001 — a check must not fail on an unopenable store
        return 0, cap, False
    try:
        rows = db.from_json(central.get_state(uilog.UI_WEDGE_RESTARTS), []) or []
    finally:
        central.close()
    cutoff = time.time() - 24 * 3600
    recent = 0
    for row in rows if isinstance(rows, list) else []:
        try:
            if float(row) >= cutoff:
                recent += 1
        except (TypeError, ValueError):
            continue
    return recent, cap, bool(cap) and recent >= cap


def check_config_drift() -> Iterator[Violation]:
    """INV-CONFIG-DRIFT — the catalog on disk must be the version the ledger calls head"""
    from . import config_version
    from .central_store import CentralStore

    central = CentralStore()
    try:
        head = central.head_config_version()
        stored = central.get_state("catalog_path")
    finally:
        central.close()
    if head is None or not stored:
        return  # no ledger, or no catalog registered: nothing to be behind
    path = Path(stored)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # unreadable or not JSON is a louder problem, and not this one's
    # The DOCUMENTS, not the ids: a release-rebase row is addressed by document AND build (§6.1)
    if config_version.canonicalise(document) == config_version.canonicalise(
            head["document"]):
        return
    on_disk = config_version.version_id(document)
    yield Violation(
        invariant="INV-CONFIG-DRIFT",
        detail=(f"{path} hashes to {on_disk}, but the ledger's head version is "
                f"{head['id']} — the fleet is running a configuration no row records. "
                f"Keep the file with `jarvis config adopt --reason \"...\"`, or put the "
                f"recorded version back with `jarvis config restore {head['id']} "
                f"--reason \"...\"`."),
        context={"catalog": str(path), "on_disk": on_disk, "head": head["id"]},
    )


def check_service_path() -> Iterator[Violation]:
    """INV-SERVICE-PATH — the installed units' PATH must reach `gh` (#41)"""
    from . import bugreport, release

    gh = bugreport.gh_bin()
    if os.sep not in gh:
        return  # `gh` is nowhere on this machine: a real problem, but not this one's
                # and `bugreport.gh_missing_message` already explains that one
    gh_dir = str(Path(gh).parent)
    stale = [unit for unit in (release.DAEMON_UNIT, release.UI_UNIT)
             # None = not installed, or carrying no PATH: nothing to be stale about
             if (value := release.unit_environment(unit, "PATH")) is not None
             and gh_dir not in value.split(os.pathsep)]
    if not stale:
        return
    yield Violation(
        invariant="INV-SERVICE-PATH",
        detail=(f"`gh` is at {gh}, but {' and '.join(stale)} carry a PATH without "
                f"{gh_dir} — every worker they spawn gets a shell where `gh` is not "
                f"found, so `gh pr create`, `jarvis bug report` and PR-merge polling "
                f"all fail. Re-render the units by re-running "
                f"scripts/install_prod_service.sh (it restarts the services)."),
        context={"gh": gh, "gh_dir": gh_dir, "units": stale,
                 "unit_dir": str(release.unit_dir())},
    )


def check_production_clean() -> Iterator[Violation]:
    """INV-PROD-CLEAN — the production checkout must be byte-identical to its tag (#202)"""
    from . import release
    from .paths import production_code_dir

    prod = production_code_dir()
    if not (prod / ".git").exists():
        return  # no production deployment on this machine
    status = release.production_status(prod)
    if status.dirty is None:
        yield Violation(
            invariant="INV-PROD-CLEAN",
            detail=(f"cannot tell whether the production checkout at {prod} still "
                    f"matches its tag — {status.error}. Unknown is not clean: this is "
                    f"the one checkout the invariant exists to watch, so it reports "
                    f"rather than assumes. Run `git -C {prod} status` as the user the "
                    f"daemon runs as to see what git is objecting to."),
            context={"checkout": str(prod), "paths": None, "error": status.error},
        )
        return
    if not status.dirty:
        return
    dirty = status.dirty
    yield Violation(
        invariant="INV-PROD-CLEAN",
        detail=(f"the production checkout at {prod} has {len(dirty)} tracked "
                f"file(s) modified since its tag was deployed "
                f"({', '.join(dirty[:5])}{', …' if len(dirty) > 5 else ''}) — "
                f"production is meant to be byte-identical to the tag, and every "
                f"version string it reports is suffixed `-dirty` until it is. Inspect "
                f"the diff, then discard it with `git -C {prod} checkout -f "
                f"{status.ref}` (which is what the next deploy does) — not "
                f"`checkout -- .`, which restores from the index and so cannot clear a "
                f"STAGED change, and staged changes are part of what is reported above."),
        context={"checkout": str(prod), "paths": dirty, "ref": status.ref},
    )


def check_prod_cli() -> Iterator[Violation]:
    """INV-PROD-CLI — the `jarvis` a human types must mean production"""
    from . import release
    from .paths import production_code_dir

    prod = production_code_dir()
    if not (prod / ".git").exists() or not os.access(prod / ".venv/bin/jarvis", os.X_OK):
        return  # no production deployment on this machine
    wrapper = release.cli_wrapper()
    expected_target = str(prod / ".venv/bin/jarvis")
    expected_home = f"{prod.parent}/state"
    facts = release.cli_wrapper_facts()
    if facts is None:
        yield Violation(
            invariant="INV-PROD-CLI",
            detail=(f"no production `jarvis` on PATH: {wrapper} does not exist, so "
                    f"`jarvis` in an interactive shell is either command-not-found or — "
                    f"worse — the venv binary with no `JARVIS_HOME`, which drives the "
                    f"DEV instance at ~/.jarvis silently (issue 757). Install it with "
                    f"scripts/install_prod_cli.sh (it starts and restarts nothing)."),
            context={"wrapper": str(wrapper), "target": None, "jarvis_home": None,
                     "expected_target": expected_target, "expected_home": expected_home,
                     "generated": False},
        )
        return
    target, home = facts["target"], facts["env"].get("JARVIS_HOME")
    context = {"wrapper": str(wrapper), "target": target, "jarvis_home": home,
               "expected_target": expected_target, "expected_home": expected_home,
               "generated": facts["generated"]}
    if not facts["generated"]:
        # Most likely a dev install pointing at ~/.jarvis — the defect's second half
        yield Violation(
            invariant="INV-PROD-CLI",
            detail=(f"{wrapper} is a `jarvis` this OS did not generate (no "
                    f"`{release.CLI_MARKER}` marker), so typing `jarvis` reaches "
                    f"something other than the fleet the units run — most likely a dev "
                    f"install writing to ~/.jarvis (issue 757). Replace it by running "
                    f"scripts/install_prod_cli.sh, or keep it deliberately."),
            context=context,
        )
        return
    if target != expected_target or home != expected_home:
        yield Violation(
            invariant="INV-PROD-CLI",
            detail=(f"{wrapper} execs {target} and exports JARVIS_HOME={home}, but "
                    f"production on this machine is {prod} with "
                    f"JARVIS_HOME={expected_home} — every `jarvis` typed in a shell is "
                    f"therefore driving a different fleet from the one the units run. "
                    f"Re-render it with scripts/install_prod_cli.sh."),
            context=context,
        )


# -- cache health


def _cache_health() -> tuple[Any, Any] | None:
    """The fleet's cache-write cohort, and the config that judges it"""
    from . import bill
    from .central_store import CentralStore
    from .ops import resolve_catalog
    from .project_store import ProjectStore as Store

    try:
        cfg = resolve_catalog().os
    except Exception:  # noqa: BLE001 — no catalog is not a cache-health finding
        return None
    central = CentralStore()
    try:
        rows = central.list_projects()
    finally:
        central.close()
    since = db.now() - cfg.cache_health_window_days * 86_400
    found = bill.CacheWrites()
    for row in rows:
        if row["status"] != "active" or not Path(row["path"]).is_dir():
            continue
        store = Store(Path(row["path"]))
        try:
            found = found + bill.cache_writes_since(store, since)
        finally:
            store.close()
    return cfg, found


def _thin_cohort(cfg: Any, writes: Any) -> bool:
    """THE ANSWER TO "it cries wolf on a quiet day", and it is a silence, not a caveat"""
    return (writes.orders < cfg.cache_health_min_orders
            or writes.boundaries < cfg.cache_health_min_boundaries)


def _cohort_note(cfg: Any, writes: Any) -> str:
    """What was actually measured"""
    from . import bill

    skipped = ""
    if writes.unmeasured_orders:
        skipped = (f", and {writes.unmeasured_orders} more whose bill was sealed before "
                   f"the OS recorded the split and cannot be re-read")
    return (f"Measured over {writes.orders} settled work orders in the last "
            f"{cfg.cache_health_window_days} days ({writes.boundaries} boundaries, "
            f"{writes.cache_write:,} cache-write tokens){skipped}. That population is "
            f"Jarvis's OWN dispatched workers, which is the population the fleet's cache "
            f"settings reach; `{bill.cohort_command(cfg.cache_health_window_days)}` "
            f"measures every transcript on this machine, including sessions opened by "
            f"hand, so the two figures differ by design.")


def check_cache_ttl_trigger() -> Iterator[Violation]:
    """INV-CACHE-TTL-TRIGGER — the 1-hour cache write has started paying, and nobody would have noticed (#160)"""
    from . import usage as usage_mod

    health = _cache_health()
    if health is None:
        return
    cfg, writes = health
    if _thin_cohort(cfg, writes):
        return
    deciding = writes.ttl_share_of_writes
    if deciding is None or deciding <= usage_mod.TTL_BREAK_EVEN:
        return
    of_tax = writes.ttl_share_of_tax
    yield Violation(
        invariant="INV-CACHE-TTL-TRIGGER",
        detail=(
            f"{deciding:.1%} of the fleet's cache writes are now re-writes the cache "
            f"entry EXPIRING caused, against the {usage_mod.TTL_BREAK_EVEN:.1%} "
            f"break-even where buying the 1-hour cache write starts paying — so the "
            f"fleet's 5-minute write (`claude_cli.PROMPT_CACHE_5M_ENV`) is now costing "
            f"money rather than saving it. Price it over the wider population before "
            f"changing anything. For contrast and NOT for comparison, TTL expiry is "
            f"{of_tax:.1%} of the re-write tax — a larger figure, because its "
            f"denominator is only the writes seen at a boundary rather than every "
            f"written token. The break-even does not apply to it, and reading it "
            f"against {usage_mod.TTL_BREAK_EVEN:.1%} is how a fleet talks itself into "
            f"switching when the arithmetic says do not. "
            f"{_cohort_note(cfg, writes)}"),
        context={"ttl_share_of_writes": deciding, "break_even": usage_mod.TTL_BREAK_EVEN,
                 "ttl_share_of_tax": of_tax, "orders": writes.orders,
                 "boundaries": writes.boundaries, "days": cfg.cache_health_window_days},
    )


def _prefix_witness(cfg: Any, limit: int = 200) -> str:
    """Which of the suspects the SessionStart hook actually watched move — "" if none"""
    from collections import Counter

    from . import hooks, timeline
    from .central_store import CentralStore
    from .project_store import ProjectStore as Store

    since = db.now() - cfg.cache_health_window_days * 86_400
    central = CentralStore()
    try:
        rows = central.list_projects()
    finally:
        central.close()

    seen: Counter[str] = Counter()
    latest: dict[str, float] = {}
    blind: set[str] = set()
    for row in rows:
        if row["status"] != "active" or not Path(row["path"]).is_dir():
            continue
        blind |= hooks.unreadable_ingredients(Path(row["path"]))
        store = Store(Path(row["path"]))
        try:
            events = store.events_across("prefix_drift", limit)
        except Exception:  # noqa: BLE001 — an unreadable project is not this finding
            continue
        finally:
            store.close()
        for event in events:
            if event["ts"] < since:
                break  # newest first
            for name in (db.from_json(event["payload"]) or {}).get("changed", []):
                seen[name] += 1
                latest[name] = max(latest.get(name, 0.0), event["ts"])

    # A dead ingredient is reported even when nothing moved
    dark = ""
    if blind:
        unwatched = ", ".join(sorted(timeline.PREFIX_INGREDIENT_LABEL.get(name, name)
                                     for name in blind))
        dark = (f"FIRST, WHAT WAS NOT BEING WATCHED: the SessionStart hook could not read "
                f"{unwatched} on this machine, so that suspect is neither confirmed nor "
                f"ruled out below and has to be checked by hand (`hooks.claude_cli_version` "
                f"for the version, whose install layout is the usual reason). ")
    if not seen:
        return dark
    named = ", ".join(
        f"{timeline.PREFIX_INGREDIENT_LABEL.get(name, name)} "
        f"({count}x, last {_date(latest[name])})"
        for name, count in seen.most_common(len(hooks.PREFIX_INGREDIENTS)))
    return (f"{dark}The SessionStart hook watched these move in the same window, commonest "
            f"first: {named}. That is an early warning and not this measurement — it "
            f"reports what went INTO the prompt, which is why it can name a cause and "
            f"this cannot. It is also blind to the MCP tool set, so an unexplained "
            f"crossing with nothing named above is the case to suspect a server in "
            f"(`hooks.note_prefix`). ")


def _date(ts: float) -> str:
    """"12 Sep", in local wall-clock for the reason `clock` gives at length: the reader is at this..."""
    return time.strftime("%d %b", time.localtime(ts))


def check_prefix_stable() -> Iterator[Violation]:
    """INV-PREFIX-DRIFT — the prompt prefix has started moving again.

    The fix is one line (`includeGitInstructions: false` in
    `dispatch._write_worker_settings`, plus `worker_brief.git_briefing`), it was verified
    once in a clean room and then trusted, and prefix invalidation is still the larger
    half of the re-write tax. A CLI upgrade, a new MCP server or an edit to the briefing
    would re-break it and nothing would say so — it would surface months later as a
    bigger bill (finding 4).

    THIS IS THE AUTHORITATIVE MEASUREMENT OF PREFIX STABILITY IN THIS TREE, because of
    where its number comes from: the cache accounting the API ITSELF reported, read back
    through the boundary classification in `usage._usage_of` and frozen onto each order's
    bill. Any other prefix-drift signal here is a proxy and defers to this one — whether
    it works by hashing the rendered system prompt at session start, by comparing against
    a recorded baseline, or by asserting in CI which region of the rendered prompt each
    piece of text lives in (`evals/test_prefix_drift.py`). All of those infer that the
    prefix moved; this reads what the cache actually did. When they disagree, this is
    right. They are worth having anyway, because they are the EARLY ones: they fail in
    the pull request that causes the drift, where this can only report it days later out
    of transcripts already paid for.

    THE RATIO IS OVER ALL CACHE WRITES, not over the tax and not over the boundaries.
    Prefix writes as a share of the tax FALLS when TTL expiry rises, so it would report
    the prefix improving on a day when only the cache got colder. The honest caveat,
    stated rather than engineered around: this ratio does move with the fleet's turn
    shape, since a fleet of shorter, choppier sessions crosses more boundaries per token.
    That is why the threshold is empirical and set above a measured normal rather than
    derived — `catalog.DEFAULT_CACHE_HEALTH_PREFIX_SHARE`.
    """
    health = _cache_health()
    if health is None:
        return
    cfg, writes = health
    if _thin_cohort(cfg, writes):
        return
    share = writes.prefix_share_of_writes
    if share is None or share < cfg.cache_health_prefix_share:
        return
    yield Violation(
        invariant="INV-PREFIX-DRIFT",
        detail=(
            f"{share:.1%} of the fleet's cache writes are being spent re-sending "
            f"conversations whose PROMPT PREFIX had moved, over the "
            f"{cfg.cache_health_prefix_share:.1%} ceiling this fleet is held to — a "
            f"ceiling set above the figure measured while the `includeGitInstructions` "
            f"fix was known to be working, so crossing it means the prefix has got "
            f"WORSE and not merely that it costs something. No cache TTL buys any of it "
            f"back: the cure is whatever is now changing the head of the prompt between "
            f"calls. {_prefix_witness(cfg)}The usual suspects, in the order they are worth "
            f"checking: a Claude CLI upgrade, an MCP server added or changed mid-session, "
            f"and an edit to `worker_brief.git_briefing` or to a project's CLAUDE.md. "
            f"`jarvis inspect <wo-id>` labels every re-write of one order by cause, and "
            f"`pytest evals/test_prefix_drift.py` says whether the part of the prompt "
            f"JARVIS renders still has its shared head where the code says — green there "
            f"with this red points at the CLI or an MCP server rather than at this tree. "
            f"{_cohort_note(cfg, writes)}"),
        context={"prefix_share_of_writes": share,
                 "threshold": cfg.cache_health_prefix_share,
                 "prefix_boundaries": writes.prefix_boundaries,
                 "orders": writes.orders, "boundaries": writes.boundaries,
                 "days": cfg.cache_health_window_days},
    )


def check_os_identity() -> Iterator[Violation]:
    """INV-OS-IDENTITY — the OS's own project must be identifiable"""
    from . import schedule

    catalog = _live_catalog()
    if catalog is None:
        return
    projects = [(p.name, p.path) for p in getattr(catalog, "projects", ())]
    candidates = schedule.os_candidates(projects)
    if not candidates or schedule.os_project(projects) is not None:
        return
    yield Violation(
        invariant="INV-OS-IDENTITY",
        detail=(f"the catalog holds {len(candidates)} projects that are checkouts of the "
                f"OS's own repository ({', '.join(candidates)}), so which one IS the OS "
                f"has no answer — and every caller of that question does nothing on no "
                f"answer. The release guards file duplicate release orders for fixes "
                f"already live, the OS's health sweep is never checked for liveness, and "
                f"`jarvis config set` stops refusing a write that switches that sweep "
                f"off. Leave one of them in the catalog."),
        level="critical", context={"candidates": candidates},
    )


def check_usage_meter_stale() -> Iterator[Violation]:
    """INV-USAGE-METER-STALE — the account's usage meter has not been readable for N minutes (docs/superpowers/specs/2026-10-08-usage-meter-samples-and-outside-spend.md)"""
    from . import usage_meter
    from .catalog import CostConfig
    from .central_store import CentralStore

    catalog = _live_catalog()
    cfg = getattr(getattr(catalog, "os", None), "cost", None) or CostConfig()
    central = CentralStore()
    try:
        streak = usage_meter.gap_streak(central)
    finally:
        central.close()
    if streak.gap_rows == 0:
        return
    since = streak.last_ok_ts if streak.last_ok_ts is not None else streak.first_ts
    stale_for = db.now() - float(since or db.now())
    if stale_for <= cfg.meter_stale_minutes * 60:
        return
    yield Violation(
        invariant="INV-USAGE-METER-STALE",
        detail=(f"the account's usage meter has not been readable for "
                f"{stale_for / 60:.0f} minutes ({streak.gap_rows} consecutive failed "
                f"reads, the first at {_stamp(streak.first_ts)}): {streak.reason}. Every "
                f"cost surface is back to a lower bound of unknown size until this is "
                f"fixed — there is no second, independent number to reconcile the "
                f"measured spend against."),
        context={"last_ok_ts": streak.last_ok_ts, "gap_rows": streak.gap_rows,
                 "reason": streak.reason},
    )


def _stamp(ts: float | None) -> str:
    from datetime import datetime, timezone

    if ts is None:
        return "unknown"
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


OS_INVARIANTS: tuple[Callable[[], Iterator[Violation]], ...] = (
    check_ui_healthy,
    check_ui_wedged,
    check_gate_canaries,
    check_config_drift,
    check_service_path,
    check_production_clean,
    check_prod_cli,
    check_cache_ttl_trigger,
    check_prefix_stable,
    check_os_identity,
    check_usage_meter_stale,
)


def check_os() -> list[Violation]:
    """Run the OS-level invariants"""
    found: list[Violation] = []
    for check in OS_INVARIANTS:
        try:
            found.extend(check())
        except Exception as e:  # noqa: BLE001 — one broken check must not hide the rest
            found.append(Violation(
                invariant=getattr(check, "__name__", "unknown"),
                detail=f"invariant raised {e!r}",
            ))
    return found


def check_schedule_progresses(store: ProjectStore) -> Iterator[Violation]:
    """INV-SCHEDULE-HELD — a recurring job that has wanted to fire for days"""
    from .ops import schedule_config_at
    from .schedule import held_seconds

    cfg = schedule_config_at(store.project_path)
    if not cfg.enabled:
        return
    # `db.now`, not `time.time`: `held_since` was written from that clock by `record_schedule_hold`
    now = db.now()
    limit = cfg.held_alarm_intervals * cfg.interval_seconds
    for state in store.list_schedule_states():
        if state["job_id"] not in cfg.jobs:
            continue
        held = held_seconds(state, now)
        if held <= limit:
            continue
        waiting_for = state["held_reason"] or "its previous order has not settled"
        yield Violation(
            invariant="INV-SCHEDULE-HELD",
            wo_id=state["last_wo_id"],
            detail=(f"scheduled job {state['job_id']!r} has been unable to fire for "
                    f"{held / SECONDS_PER_HOUR:.0f}h — longer than "
                    f"{cfg.held_alarm_intervals} intervals of {cfg.interval_hours}h. "
                    f"It is waiting because {waiting_for}. Settle that work order and "
                    f"the job files its next one on the following tick."),
            context={"job_id": state["job_id"], "held_seconds": held,
                     "held_reason": state["held_reason"] or ""},
        )


def check_budgets_are_enforced(store: ProjectStore) -> Iterator[Violation]:
    """INV-BUDGET-OVERSPENT — an order past its ceiling that is still able to spend"""
    from . import budget as budget_mod
    from .central_store import CentralStore

    central = None
    try:
        central = CentralStore()
    except Exception:  # noqa: BLE001 — a degraded reading beats no invariant at all
        pass
    try:
        for wo in store.list_work_orders(statuses=OPEN_STATUSES, include_hidden=True):
            if wo["status"] == budget_mod.EXHAUSTED:
                continue
            cap = budget_mod.ceiling(store, central, wo)
            if cap is None or not cap.exhausted:
                continue
            # The window the settler declines on purpose
            outcome = (store.latest_validation_round(wo_id=wo["id"]) or {}).get("outcome")
            if outcome in RUNNABLE_VALIDATION_OUTCOMES:
                continue
            yield Violation(
                invariant="INV-BUDGET-OVERSPENT",
                wo_id=wo["id"],
                detail=(f"{wo['id']} has spent ${cap.spent_usd:.2f} of its "
                        f"${cap.cap_usd:.2f} ceiling and is still {wo['status']} — the "
                        f"settler should have parked it in `budget_exhausted`. Until it "
                        f"does, every turn it starts is launched with no headroom."),
                context={"status": wo["status"], "cap_usd": cap.cap_usd,
                         "spent_usd": cap.spent_usd, "source": cap.source},
            )
    finally:
        if central is not None:
            central.close()


def check_default_branch_green(store: ProjectStore) -> Iterator[Violation]:
    """This project's default branch is red, and the OS says so ONCE, out loud (docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §3.)"""
    from .central_store import CentralStore

    central = None
    try:
        central = CentralStore()
        fact = central.base_health(central.project_name_for_path(store.project_path))
    except Exception:  # noqa: BLE001 — an unreadable central database is not a red base
        return
    finally:
        if central is not None:
            central.close()
    if not fact.get("red"):
        return
    base = str(fact.get("base") or "the default branch")
    workflow = str(fact.get("workflow") or "an unnamed workflow")
    url = str(fact.get("run_url") or "")
    sha = str(fact.get("head_sha") or "")
    wo_id = str(fact.get("wo_id") or "")
    blamed = f", merged by the OS for {wo_id}" if wo_id else ""
    # Spec 2026-10-14-a-red-main-dispatches-its-own-fix.md §5: the order producing that commit
    fix_wo_id = str(fact.get("fix_wo_id") or "")
    issue_url = str(fact.get("issue_url") or "")
    dead = _dead_base_fix(store, fix_wo_id)
    tail = _base_fix_decision(dead, fix_wo_id, issue_url)
    if dead:
        # §5's third branch: the detail STATES the state; the owed violation below TAKES the decision
        remedy = (f"The remedy is a commit on `{base}`; {fix_wo_id} was dispatched to "
                  f"produce it and is now `{dead}`, so the OS is out of moves — it "
                  f"dispatches no second fix order and never retries a dead turn "
                  f"itself. {tail}")
    elif fix_wo_id:
        remedy = f"The remedy is a commit on `{base}`, and {fix_wo_id} is producing it."
    else:
        remedy = (f"The remedy is a commit on `{base}`, and no work order is producing "
                  f"it yet.")
    yield Violation(
        invariant=INV_BASE_RED,
        wo_id=None,
        repaired=False,
        detail=(f"`{base}` is red — workflow `{workflow}`, run {url}, at "
                f"{sha[:10] or 'an unknown commit'}{blamed}. Nothing merges onto a "
                f"broken default branch, and every pull request built against it "
                f"inherits the failure. {remedy}"),
        context={"base": base, "workflow": workflow, "run_url": url, "head_sha": sha,
                 "wo_id": wo_id, "run_id": fact.get("run_id"),
                 "fix_wo_id": fix_wo_id, "issue_url": issue_url,
                 "checked_at": fact.get("checked_at")},
    )
    if not dead:
        return
    # NO elapsed time and nothing that changes tick to tick — `Violation.owed`'s rule
    owed = (f"{fix_wo_id}, the work order fixing the red default branch `{base}`, "
            f"settled `{dead}` without clearing it, and the OS will not open another. "
            f"{tail}")
    yield Violation(
        invariant=INV_BASE_FIX_DEAD,
        wo_id=fix_wo_id,            # `owed_violations` selects by it, so it must be set
        repaired=False,
        detail=owed,
        owed=owed,
        context={"base": base, "fix_wo_id": fix_wo_id, "issue_url": issue_url,
                 "status": dead},
    )


def _base_fix_decision(dead: str, fix_wo_id: str, issue_url: str) -> str:
    """THE DECISION a dead base-fix order leaves the user, naming only a command that can actually run"""
    if not dead:
        return ""
    if dead == "failed":
        return f"Run `jarvis wo retry {fix_wo_id}`, or order a different fix."
    gone = ("Its record is gone, so nothing can be resumed" if dead == "deleted"
            else "That order will not run again")
    if issue_url:
        return f"{gone} — a fix is a new order on the same issue: " \
               f"`jarvis issues start {issue_url}`."
    return f"{gone} — order a fix yourself."


def _dead_base_fix(store: ProjectStore, fix_wo_id: str) -> str:
    """The terminal status of the red base's fix order, `""` while it can still fix it (docs/superpowers/specs/2026-10-14-a-red-main-dispatches-its-own-fix.md §5.)"""
    from .project_store import TERMINAL_STATUSES as SETTLED_STATUSES

    if not fix_wo_id:
        return ""
    try:
        status = str(store.get_work_order(fix_wo_id)["status"])
    except KeyError:
        return "deleted"
    return status if status in SETTLED_STATUSES else ""


#: How many feature orders the pointer check reads
SPEC_POINTER_ROWS = 100000

INV_SPEC_POINTER_REAL = "INV-SPEC-POINTER-REAL"


def check_spec_pointer_resolves(store: ProjectStore) -> Iterator[Violation]:
    """INV-SPEC-POINTER-REAL — a plan's `design_doc` names a file that is really there (docs/superpowers/specs/2026-10-14-inv-spec-pointer-real-reads-the-integration-branch.md)"""
    from . import landing

    # One memo for the whole sweep: the remote is refreshed at most once — spec §2.2
    fetched: set[str] = set()
    for fo in store.list_feature_orders(statuses=None, kind=None,
                                        limit=SPEC_POINTER_ROWS):
        plan = db.from_json(fo.get("plan"), {}) or {}
        doc = str(plan.get("design_doc") or "").strip()
        if not doc:
            continue
        found = landing.committed_exists(store.project_path, _planner_row(store, fo),
                                         doc, fetched)
        if found is None or found.present:
            continue
        fo_id = fo["id"]
        matches = _tracked_with_basename(found.repo, found.ref, Path(doc).name)
        detail = (f"feature order {fo_id} stores its design document at `{doc}`, and no "
                  f"file is there. `jarvis fo show {fo_id}` and the work order pages "
                  f"under it render that path, and `jarvis spec section {doc} <n>` reads "
                  f"from disk — the plan's snapshot keeps every other reader working, "
                  f"which is why nothing noticed.")
        if len(matches) == 1:
            plan["design_doc"] = matches[0]
            store.update_feature_order(fo_id, plan=db.to_json(plan))
            yield Violation(
                invariant=INV_SPEC_POINTER_REAL,
                detail=detail,
                repaired=True,
                repair=f"repointed {fo_id}'s design_doc to {matches[0]}",
                subject_id=fo_id,       # per-feature dedupe identity — spec §2.3
                context={"fo_id": fo_id, "was": doc, "now": matches[0]},
            )
            continue
        # NO elapsed time and nothing that changes tick to tick, or the item can never be acked down
        where = (f"{len(matches)} tracked files carry that name: "
                 f"{', '.join(matches[:10])}" if matches
                 else "no tracked file carries that name")
        owed = (f"{detail} {where}, so which document this feature was planned against "
                f"is a decision: move or rename the file so the stored path opens, or "
                f"cancel the feature with `jarvis fo cancel {fo_id}`.")
        yield Violation(
            invariant=INV_SPEC_POINTER_REAL,
            detail=owed,
            owed=owed,
            subject_id=fo_id,           # per-feature dedupe identity — spec §2.3
            context={"fo_id": fo_id, "was": doc, "matches": matches[:10]},
        )


def _planner_row(store: ProjectStore, fo: dict[str, Any]) -> dict[str, Any] | None:
    """The feature's planner row, for the ladder's first rung"""
    if not fo.get("plan_wo_id"):
        return None
    return store.get_work_order(fo["plan_wo_id"])


def _tracked_with_basename(root: Path, ref: str, name: str) -> list[str]:
    """Paths called `name` ON `ref` — the same ref the existence question was answered from (docs/superpowers/specs/2026-10-14-inv-spec-pointer-real-reads-the-integration-branch.md)"""
    try:
        out = subprocess.run(["git", "ls-tree", "-r", "--name-only", "-z", ref],
                             cwd=str(root), capture_output=True, text=True, check=False)
    except OSError:
        return []
    if out.returncode != 0:
        return []
    return sorted(p for p in out.stdout.split("\0") if p and Path(p).name == name)


INV_PRECONDITION_UNWIRED = "INV-PRECONDITION-UNWIRED"


def check_precondition_is_wired(store: ProjectStore) -> Iterator[Violation]:
    """INV-PRECONDITION-UNWIRED — a brief that says "do not start" must be held by state (docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md)"""
    from . import preconditions

    for wo in store.list_work_orders(statuses=OPEN_STATUSES, include_hidden=True):
        matched = preconditions.states_hold(wo["title"], wo["description"] or "")
        if not matched:
            continue
        if store.dependencies(wo) or store.open_preconditions(wo["id"]):
            continue
        yield Violation(
            invariant=INV_PRECONDITION_UNWIRED,
            wo_id=wo["id"],
            detail=f"its brief states a stop condition — \"{matched}\" — and nothing "
                   f"holds it back, so the claim takes it: wire the hold "
                   f"(`--blocked-by`/`--depends-on` at creation) or delete the sentence",
            repaired=False,
            owed="",
            # The matched sentence is the USER's text: it stays on the record and never rides outward into a...
            notice=f"{wo['id']}: its brief states an unwired stop condition and nothing "
                   f"holds it back, so the claim takes it — wire the hold "
                   f"(`--blocked-by`/`--depends-on` at creation) or delete the sentence; "
                   f"`jarvis wo show {wo['id']}` quotes it",
            context={"matched": matched},
        )


INVARIANTS: tuple[Callable[[ProjectStore], Iterator[Violation]], ...] = (
    check_default_branch_green,    # first and order-free: a pure read of the central
                                   # reading, repairing nothing and touching no flag any other check derives
    check_assumptions_persisted,   # rows first: the others read pending_assumptions
    check_no_orphan_gate_requests,  # ...and gates before the flag checks: an orphan
                                    # request is a blocker they would otherwise believe
    check_repaired_work_rejoins_the_merge_queue,  # BEFORE the reason checks: it moves
                                   # the status they derive a reason from
    check_attention_reason_is_true,
    check_adhoc_not_governed,      # retire before the flag checks judge the leftovers
    check_legacy_adhoc_retired,    # ...and before them, let go of what nothing tracks
    check_no_phantom_attention,
    check_assumption_flags_are_owed,  # ...and its pre-delivery sibling
                                   # against INV-ATTENTION-MISSING, which derives the same `true_blockers` and so will not put back what...
    check_messages_are_delivered,  # before INV-ATTENTION-MISSING, which skips anything
                                   # already flagged: whichever sees it first raises the same `true_blockers[0]`
    check_blocked_work_is_surfaced,
    check_attention_has_reason,
    check_precondition_is_wired,   # order-free: a pure read that repairs nothing, raises
                                   # no flag and writes no status, so nothing else in the tuple reads its output
    check_pull_request_recorded,   # order-free: a pure timeline read that repairs
                                   # nothing and touches no flag
    check_manager_slots,           # a canary, not a state check: it repairs nothing and
                                   # is unaffected by the order it runs in
    check_health_sweep_produces_judgements,  # ditto: a pure read of the sweep ledger
                                   # repairing nothing and read by nothing else
    check_os_health_sweep_alive,   # NOT in SLOW_INVARIANTS: it shells out to nothing
                                   # and a liveness check that runs hourly is a liveness check with an hour of blind spot
    check_stuck_sweep_alive,       # ...and beside it for the same reason: a fleet-wide
                                   # fact short-circuited on `_os_owning_project`
    check_paused_turns_resume,     # ditto: a pure read of what the retry pass did or
                                   # did not do, with nothing to repair
    check_pause_deadline_stable,   # ...and its companion: the pass can also be failing
                                   # because the moment it was given keeps moving
    check_schedule_progresses,     # ditto, one mechanism over: a pure read of the
                                   # scheduler's clock, repairing nothing
    check_budgets_are_enforced,    # ditto again: it reports what the settler did not do
                                   # and repairs nothing, so nothing depends on where it sits
    check_validation_progresses,   # after the flag checks: its repair touches no flag
                                   # and a `validating` row is invisible to all of them
    check_feature_failures_are_real,  # order-free: it reads and writes feature orders
                                   # only, which no work-order check looks at
    check_neo_escalations_are_live,  # order-free: it writes to Neo's store only, and
                                   # touches no flag any other check reads
    check_proposed_remedies_are_live,  # after the flag checks: it RAISES a flag, and one
                                   # raised before them is read as phantom attention on an alarm `true_blockers` cannot re-derive
    check_undeclared_delivery,     # order-free: it raises a FINDING and touches no flag
                                   # and no status, so nothing else here reads its output
    check_spans_reach_the_status,  # order-free: it appends a span and changes no status
                                   # and nothing else here reads `wo_state_spans`
    check_envelopes_move,          # last: it delivers, and delivery changes work orders
    check_no_lost_feedback,        # ...and after it, because that delivery is what
                                   # marks an envelope undeliverable in the first place
)


#: Invariants whose cost scales with the SETTLED BACKLOG rather than with live work
SLOW_INVARIANTS: tuple[Callable[[ProjectStore], Iterator[Violation]], ...] = (
    check_work_lands,
    check_spec_pointer_resolves,  # both properties of this class and neither of the fast
                                  # one: its population is every feature order a project has ever had
)


def check_project(store: ProjectStore, repair: bool = True,
                  slow: bool = False) -> list[Violation]:
    """Run every invariant over one project"""
    target = store if repair else _ReadOnly(store)
    found: list[Violation] = []
    for check in (*INVARIANTS, *(SLOW_INVARIANTS if slow else ())):
        try:
            found.extend(check(target))  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001 — one broken check must not hide the rest
            found.append(Violation(
                invariant=getattr(check, "__name__", "unknown"),
                detail=f"invariant raised {e!r}",
            ))
    return found


class _ReadOnly:
    """Store proxy that swallows repairs, so `check_project(repair=False)` is a pure read"""

    _BLOCKED = ("flag_attention", "clear_attention", "add_assumption", "add_event",
                "set_status", "update_work_order", "supersede_approval",
                # The bus
                "mark_envelope", "bump_envelope_attempt", "deliver_envelope",
                "queue_message", "flag_feature_attention",
                # INV-VALIDATION-STRANDED's repair
                "close_validation_round",
                # INV-FEATURE-FALSE-FAILURE's
                "set_feature_status", "clear_feature_attention",
                # INV-SPEC-POINTER-REAL's
                "update_feature_order")

    #: How a checker asks "am I allowed to change anything?"
    readonly = True

    def __init__(self, store: ProjectStore):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        if name in self._BLOCKED:
            return lambda *a, **k: None
        return getattr(self._store, name)
