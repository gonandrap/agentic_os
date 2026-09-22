"""Post-conditions — the OS checking that its own state still means what it says.

Every action the OS takes has an intended effect. An event log does not prove the effect
survived: `attention {"reason": "assumptions pending review"}` records that we set a
reason, not that the reason is still true ninety seconds later when an unrelated hook
overwrites it. That exact sequence shipped two work orders to the dashboard labelled
"waiting for your input" when what they actually needed was an assumption review — every
component behaved correctly and the resulting state was a lie.

So invariants here are **steady-state predicates**, not write-time assertions. They are
re-evaluated on every reconcile tick against the database as it currently is, which is
the only way to catch state that was correct when written and went wrong afterwards.

Three rules for this module:

1. **No LLM, ever.** These are cheap SQL-level predicates. Determinism is the point:
   the checker must be more trustworthy than the thing it checks.
2. **Repair only what is unambiguous.** A violation with exactly one correct resolution
   derivable from state (a stale reason, a phantom flag) is repaired automatically and
   the repair is recorded. Anything else is reported and left alone.
3. **Report once, not every tick.** Callers dedupe on `Violation.key`; a standing
   violation must not spam the timeline or the inbox.

Adding an invariant: write a `_check_*` generator yielding `Violation`s and register it
in `INVARIANTS`. Give it a stable id — ids appear in work order timelines and in
`jarvis doctor` output, so renaming one rewrites history.

REGISTER IT IN `OS_INVARIANTS` INSTEAD when what it checks is a fact about the OS rather
than about one project — the dashboard, the production checkout, the fleet's cache
configuration. Those take no store, run once per `jarvis doctor` rather than once per
project, and never run on the daemon's reconcile tick. Putting a fleet-wide check in
`INVARIANTS` is not a smaller mistake than the reverse: it reports one decision once per
project, which is how a finding becomes noise.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

from . import budget, db, worker_session
# A leaf module (logging, subprocess, dataclasses): the import cannot cycle back here.
from .automerge import HELD_SHA_MOVED
from .catalog import DEFAULT_VALIDATION_MAX_ROUNDS, DEFAULT_VALIDATION_TIMEOUT
from .neo_store import USER_HELD_Q_STATUSES
from .project_store import (
    ACTIVE_STATUSES,
    DEPENDENCY_DEAD_STATUSES,
    FO_OPEN_STATUSES,
    OPEN_STATUSES,
    RETRY_SWEEP_STATUSES,
    RUNNABLE_VALIDATION_OUTCOMES,
    SLOT_STATUSES,
    UNGOVERNED_ORIGINS,
    validation_hold_until,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .fleet import Fleet
    from .project_store import ProjectStore

# Statuses where the user is the one holding the work up. A `running` worker is not
# blocked on anything the user can see. `pending` used to be excluded on the same
# reasoning — it hasn't started, so nobody is waiting on a decision — and dependency
# edges broke that: a pending work order whose dependency was cancelled will never
# start, and only the user can choose between cancelling it and cutting the edge.
# This tuple must cover every status `true_blockers` can return a blocker for, or the
# blocker is derived correctly and then never surfaced.
BLOCKED_STATUSES = ("waiting_input", "needs_review", "failed", "pending",
                    # `waiting_pr_merge` is silent in the ordinary case and returns no
                    # blockers below — it is here for the one state that does, the
                    # conflict the worker could not resolve (spec §5).
                    "waiting_pr_merge",
                    # ...and the two the OS calls work-in-progress, here for the one
                    # state THEY have: a turn that ended without finishing the work and
                    # nothing in flight behind it (`parked_reason`). Silent otherwise —
                    # a running worker is still not blocked on anything the user can see.
                    "running", "dispatching",
                    # `idle` is here for exactly one blocker and derives no other: a
                    # message the user or the bus sent a manager that it will never see
                    # (MESSAGE_STUCK_BLOCKER). Being idle asks nothing of anyone — that
                    # is the whole of issue #264 — but a feature routes everything
                    # through its manager, so a message rotting there strands the
                    # feature silently, which is issue 43 one level up.
                    "idle",
                    # An order that spent its budget. Only the user can decide whether to
                    # fund another turn, so nothing else will ever move it.
                    "budget_exhausted")
# Statuses where nothing can possibly be pending: the work order is over.
TERMINAL_STATUSES = ("completed", "cancelled")

#: What a work order says when the pull request it was parked behind was closed without
#: being merged. Lives here rather than at the call site because `true_blockers` is the
#: only source of attention reasons — INV-ATTENTION-REASON rewrites any flag it cannot
#: derive, so a reason raised by `Daemon.poll_pull_requests` and not repeated here would
#: silently become the generic IDLE_NO_FINISH_BLOCKER below on the next reconcile tick.
#: The fallback when an order is parked in `budget_exhausted` but the accounting no
#: longer says so — the user raised the budget and the reconcile tick that would move the
#: status has not run yet. Says the status and nothing it cannot stand behind.
BUDGET_SPENT_BLOCKER = ("budget spent — raise it with `jarvis wo budget <id> <amount>` "
                        "or close the order")


def budget_blocker(store: ProjectStore, wo: dict[str, Any]) -> str:
    """The attention line for a work order parked in `budget_exhausted`.

    Opens its own `CentralStore` for the Jarvis half of the bill and closes it again.
    That cost is paid only by an order ALREADY in this status — a handful fleet-wide —
    because the caller checks the status first; `true_blockers` runs against every work
    order on every tick and must not open a second database for the ones with no budget.

    A store that will not open DEGRADES rather than raises: the reason is then derived
    from the worker's turns alone, which understates the spend rather than inventing one,
    and an unopenable database must not take the whole attention list down with it.
    """
    from .central_store import CentralStore

    central = None
    try:
        central = CentralStore()
    except Exception:  # noqa: BLE001 — see the docstring
        pass
    try:
        out = budget.exhaustion(store, central, wo)
    finally:
        if central is not None:
            central.close()
    # None means the accounting no longer says it is spent: the user raised the budget
    # and the tick that moves the status has not run yet.
    return out.reason if out else BUDGET_SPENT_BLOCKER


PR_CLOSED_BLOCKER = "pull request closed without merging — the work was not accepted"

#: What a work order says when settling it would have stranded the code it wrote
#: (`ops.park_unlanded`, GitHub issue #232). Phrased as the decision the user owes rather
#: than as a fault: landing it and dropping it are both fine endings, and only silence is
#: not. Here rather than at the call site under PR_CLOSED_BLOCKER's obligation above.
UNLANDED_BLOCKER = ("work not landed — merge its pull request, or record that it is "
                    "being abandoned")

#: How many times the OS asks a worker to repair its own pull request before it asks the
#: user instead. ONE cap for both repairs — the conflict and the red build — because
#: they are the same mechanism with a different message; see §4 of
#: docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md and §3
#: of 2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md. Shared VALUE, separate
#: BUDGETS: a branch that conflicted twice last week still gets three tries at a red
#: build, because they are different problems and `ops.PrRepair` counts them apart.
PR_REPAIR_MAX_ATTEMPTS = 3

#: The statuses in which a pull request can SIT WITH NOBODY MOVING IT — so the statuses
#: the poll asks GitHub about (`Daemon.PR_POLL_STATUSES` is this tuple) and the only ones
#: in which either repair blocker below may be derived. ONE home for the set, because the
#: two have to agree: a status the poll nudges in but `true_blockers` does not derive for
#: raises a give-up flag nothing can re-derive, and INV-ATTENTION-REASON relabels it on
#: the next tick; a status derived for but never polled asserts a repair that cannot be
#: happening. Issue #224 widened the poll and this is what keeps the pair honest.
#:
#: Every member is in BLOCKED_STATUSES, or INV-ATTENTION-MISSING would derive the give-up
#: correctly and then never surface it.
#:
#: The in-flight statuses are deliberately absent — see `Daemon.PR_POLL_STATUSES` — and so
#: are the terminal ones, which is not merely tidiness: a hand-merged RED pull request
#: ends in `ops.complete_merged`, never in `clear_pr_repair`, so its episode stays open
#: for ever and an ungated derivation would leave a finished work order saying "do not
#: merge it as it stands".
PR_REPAIR_STATUSES = ("waiting_pr_merge", "needs_review", "waiting_input", "failed")

#: The NAME of each repair, which is also the name of its timeline events
#: (`pr_<name>_nudged`/`_cleared`/`_unresolved`), its message source (`pr-<name>`) and
#: the episode `ProjectStore.pr_repair_attempts` counts. They live here, beside the
#: status set, for exactly the reason that set does: `ops.PrRepair` is constructed FROM
#: these, and `true_blockers` below derives FROM these, so the two cannot be renamed
#: apart. Spelling one as a literal at either site is the poll/derivation disagreement
#: PR_REPAIR_STATUSES exists to prevent, one level down — a rename would silently stop
#: the blocker deriving, and nothing would fail loudly.
#:
#: Here rather than in `ops` because `ops` imports this module; the direction cannot be
#: reversed without a cycle.
PR_CONFLICT_REPAIR = "conflict"
PR_CHECKS_REPAIR = "checks"

#: THE THREE EVENTS OF THE INHERITED-FAILURE HEAL, named here for the reason the repair
#: names above are: `ops` writes them, `status_label` below derives from them, and `ops`
#: imports this module so the dependency cannot go the other way. A literal spelled at
#: either site is a rename that silently stops the status line deriving.
#:
#: `pr_base_health` is written ONLY ON A TRANSITION — the base going red, or going green
#: again. That is what lets `base_red_note` answer in ONE indexed read: the newest row is
#: the current state, so nothing has to be compared against a second event kind.
PR_BASE_HEALTH_EVENT = "pr_base_health"
#: The heal itself: the OS merged the base into this branch so CI rebuilds against a base
#: that works. Spec §4 — the recovery has to be a fact on the record, not a mystery.
PR_BASE_UPDATED_EVENT = "pr_base_updated"
#: GitHub refused the update. Spends the attempt for that base sha exactly as a success
#: does, so a repeatedly-refusing pull request falls through to the worker once and stays
#: there rather than being retried every two minutes.
PR_BASE_UPDATE_FAILED_EVENT = "pr_base_update_failed"

#: What a work order says while its base is broken. NOT an attention reason and NOT a
#: blocker: nobody owes a decision, the OS is waiting for a build that is already
#: running, and `true_blockers` deliberately does not derive it. It exists because the
#: alternative is silence — a work order sitting in `waiting_pr_merge` with a red pull
#: request and no nudge being sent looks identical to one the OS has forgotten.
#:
#: FREE OF ANY ELAPSED TIME, on PARKED_BLOCKER's rule, and free of the base's sha as
#: well: this string is rendered on every listing and a label that ticked would make two
#: consecutive renders of one unchanged work order disagree.
BASE_RED_NOTE = ("waiting for `{base}` to go green — the base's own build is red, so "
                 "nothing this branch pushes can pass")

#: What a work order says when its pull request conflicts and the worker could not fix
#: it in PR_REPAIR_MAX_ATTEMPTS attempts — one of the two things that make a
#: `waiting_pr_merge` work order an attention item (spec §4). Re-derived below from the
#: work order's own timeline, under the same obligation as PR_CLOSED_BLOCKER (spec §5).
PR_CONFLICT_BLOCKER = ("merge conflicts the worker could not resolve — the pull request "
                       "needs you")

#: The other one: CI is red on the pull request and the worker could not get it green.
#: Under exactly the same obligation, and it reaches a work order in ANY status that
#: carries a pull request — a red build in `needs_review` is the user being asked to
#: merge something broken, which is the case issue #224 was filed about.
PR_CHECKS_BLOCKER = ("the pull request's checks are failing and the worker could not fix "
                     "them — do not merge it as it stands")

#: THE TWO GIVE-UPS, PAIRED AND ORDERED, because `true_blockers` derives them from this
#: tuple at ONE site. They were derived at two — the red build above the `needs_review`
#: triage and the conflict below it — and after issue #224 widened both onto
#: PR_REPAIR_STATUSES that asymmetry had teeth: in the statuses it added, a conflict
#: give-up was derived and then outranked by the panel-gave-up line, so it was computed
#: and never read. `attention_reason` is one column fed from `blockers[0]` (kn-d4d5a967),
#: which makes "derived below something else" and "not derived" the same thing to the
#: user, and being derived-but-unread is the silence this whole work order exists to
#: close. One tuple, one ranking, one comment.
#:
#: Conflict first: a pull request that will not merge at all is not waiting on its
#: checks, and if a worker somehow spent both budgets that is the one to act on.
PR_REPAIR_BLOCKERS = ((PR_CONFLICT_REPAIR, PR_CONFLICT_BLOCKER),
                      (PR_CHECKS_REPAIR, PR_CHECKS_BLOCKER))

#: The event `ops.rejudge_moved_head` writes when it will NOT re-judge a moved head: the
#: round it would open is the one that reaches `validation.max_rounds`, and that last
#: round belongs to the user (spec
#: docs/superpowers/specs/2026-09-19-a-moved-head-re-judges-itself.md section 4).
REJUDGE_DECLINED_EVENT = "validation_rejudge_declined"

#: What a work order says when its pull request is green and mergeable, its verdict names
#: an older commit, and the OS has run out of rounds to bind a new one with. THE ONLY
#: `sha_moved` STALL THAT REACHES THE USER: every other one the OS now re-judges itself,
#: silently, which is why a held auto-merge is still deliberately not an attention item
#: (`Daemon._note_automerge_held`).
#:
#: Re-derived below from the timeline rather than raised where the decline is written —
#: kn-089de524: a flag written on a reconciler's path re-raises itself every tick and
#: overwrites `jarvis wo ack`.
SHA_MOVED_BLOCKER = ("the panel's verdict names an older commit and no rounds are left "
                     "to bind a new one — merge it yourself, re-judge it "
                     "(`jarvis validation force`), or give it another round "
                     "(`validation.max_rounds`) and the OS re-judges it itself")

#: What a work order says when the validation panel gave up on it: it kept resubmitting
#: and the panel kept rejecting, until the round budget ran out. Nothing automatic is
#: left to try, and only the user can say whether the work is good enough or the
#: reviewer is wrong. Subject to exactly the obligation PR_CLOSED_BLOCKER describes
#: above — INV-ATTENTION-REASON rewrites any attention reason `true_blockers` cannot
#: re-derive, so repeating it here is mandatory rather than cosmetic.
#:
#: A round that is merely OPEN raises nothing at all: `validating` is the OS working,
#: not a decision anyone owes, which is why that status is absent from BLOCKED_STATUSES
#: and has no branch below.
VALIDATION_STUCK_BLOCKER = ("the review could not be satisfied — the work needs your "
                            "judgement")

#: What a work order says when its turn died because Claude Code could not authenticate
#: (`worker_session.PAUSE_AUTH`). Nothing here is wrong with the work and nothing is wrong
#: with the API: the account cannot answer until a human signs in, and then it can. So
#: this asks for the ONE action that helps and promises the rest — `Daemon._park_on_signin`
#: holds the order in `waiting_input` and `retry_paused_turns` relaunches it fleet-wide
#: within a tick of the sign-in.
#:
#: Under the same obligation as PR_CLOSED_BLOCKER above: `_park_on_signin` raises it and
#: this module must be able to re-derive it, or INV-ATTENTION-REASON relabels it on the
#: next tick with a sentence that sends the user hunting for a bug in the work.
AUTH_BLOCKER = ("Claude Code could not authenticate — sign in again and it resumes "
                "by itself")

#: What a work order says when its worker went quiet without `jarvis wo finish`. Says
#: what HAPPENED, not merely that the turn ended: the worker stopped mid-task, and — the
#: half a user cannot see and reliably guesses wrong — nothing it may have left running
#: survived, because a turn is one `claude -p` process and no event wakes a worker when a
#: background job finishes. wo-2df8828c signed off on "I'll be re-invoked when it
#: finishes" and read as merely mislabelled; the eval had in fact died with the turn.
#:
#: `Daemon.settle_work_order` flags it and `true_blockers` re-derives it, from here, for
#: the reason PR_CLOSED_BLOCKER gives above. They said two different sentences for one
#: state until this constant existed.
IDLE_NO_FINISH_BLOCKER = ("the worker stopped mid-task without `jarvis wo finish` — "
                          "nothing it started is still running; review the session")

SECONDS_PER_MINUTE = 60  # a unit, not a setting
SECONDS_PER_HOUR = 3600  # ditto

#: What a work order says when its turn has ended, nothing is in flight, and the OS is
#: still calling it work in progress. FREE OF ANY ELAPSED TIME, deliberately: this string
#: is compared against `attention_reason` by INV-ATTENTION-REASON and stored verbatim by
#: `ProjectStore.ack_attention`, so a reason that ticked would be rewritten every
#: reconcile and could never be acknowledged.
PARKED_BLOCKER = ("the worker parked mid-task — nothing is in flight to move it; read "
                  "its last message, then `jarvis wo send` or `jarvis wo done`")

#: The other shape of the same fact, and the one that cost wo-a4bd6958 seven hours: the
#: work order HAS a recorded finish, was sent back to work afterwards, and stopped again
#: without finishing. `Daemon.settle_work_order` reads `result_summary` as a lifetime
#: fact, so no later turn can reach its IDLE_NO_FINISH branch — the order settles under
#: whatever the earlier finish left behind and says nothing about the turn that stopped.
STALE_FINISH_BLOCKER = ("the worker went back to work after finishing and stopped again "
                        "without finishing — its recorded summary is older than its last "
                        "turn; read that turn, then `jarvis wo send`")

#: Where "nothing is in flight" is worth saying. `validating` is excluded because the
#: round machine owns that work order and INV-VALIDATION-STRANDED already watches it;
#: `pending` has no turn to be parked after; `waiting_pr_merge` is waiting on
#: `Daemon.poll_pull_requests`, which is in flight by definition.
#:
#: `idle` is absent because "nothing is in flight" is the DEFINITION of that status
#: rather than news about it. That absence is what replaced `parked_reason`'s
#: `kind == "manager"` carve-out: the status now carries the fact the carve-out was
#: asserting, so a manager in any OTHER status is judged like anything else.
PARKABLE_STATUSES = ("dispatching", "running", "waiting_input", "needs_review")

#: What a work order says when something the user sent it is still sitting in the queue
#: and the worker has never seen it. GitHub issue 43: the delivery loop held messages for
#: ever with no timeline event, no retry counter and no invariant, so Neo's gate verdict,
#: two `jarvis wo send` messages and the documented cure all failed silently and the
#: fleet went on reporting healthy.
#:
#: FREE OF ANY ELAPSED TIME, under the rule PARKED_BLOCKER states: INV-ATTENTION-REASON
#: compares this against `attention_reason` and `ProjectStore.ack_attention` stores it
#: verbatim, so a reason that ticked could never be acknowledged. The minutes and the
#: hold go in the violation's detail, which nothing compares.
MESSAGE_STUCK_BLOCKER = ("a message you sent is still queued and the worker has not seen "
                         "it — `jarvis wo resume-auto` for what is holding it")

#: Where an undelivered message is a defect rather than a wait. `pending` is absent
#: because an undispatched order's message goes out as its second turn and a
#: dependency-blocked order is never an attention item just for waiting (its
#: unrecoverable case is DEAD_DEPENDENCY_BLOCKER's); `validating` for the reason it is
#: absent from BLOCKED_STATUSES; the terminal pair because INV-ATTENTION-PHANTOM clears
#: any flag raised there, so the two checks would fight every tick.
#:
#: `idle` IS here, and it is the one thing an idle manager can still owe the user. It
#: inherited the coverage rather than gaining it — a manager used to sit in
#: `waiting_input` — and dropping it while moving the status would have made every stuck
#: message to a feature's only addressee invisible.
MESSAGE_STUCK_STATUSES = ("dispatching", "running", "idle", "waiting_input",
                          "needs_review", "failed", "waiting_pr_merge")

#: The `ops.waiting_on` answers that mean the OS itself will do the next thing, with
#: nobody typing. A list of what IS coming rather than a second guess at what is not:
#: everything else that function can answer means the work order has stopped.
IN_FLIGHT_WAITS = ("turn_running", "queued_message", "neo_question", "gate_with_neo",
                   "retry_pending", "signin", "pending")

#: What `parked_reason` stays silent about. `message_stuck` is the one answer that means
#: the work order HAS stopped and is still not this check's to report: `true_blockers`
#: raises MESSAGE_STUCK_BLOCKER for it directly, and that sentence names what the user is
#: missing where PARKED_BLOCKER would only say the worker went quiet.
SPOKEN_FOR_WAITS = IN_FLIGHT_WAITS + ("message_stuck",)


@dataclass
class Violation:
    """One invariant found false, with whatever the checker did about it."""

    invariant: str
    detail: str
    wo_id: str | None = None
    repaired: bool = False
    repair: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str | None]:
        """Identity for dedupe: the same invariant failing on the same work order is
        one standing problem, however many ticks it survives."""
        return (self.invariant, self.wo_id)

    def __str__(self) -> str:
        where = f"{self.wo_id}: " if self.wo_id else ""
        fixed = f" [repaired: {self.repair}]" if self.repaired else ""
        return f"{self.invariant} — {where}{self.detail}{fixed}"


#: What a FEATURE order says when one of its children ended badly. Two reasons rather
#: than one because they ask the user for different things: a failed child is a problem
#: to diagnose, a cancelled one is a decision already taken whose consequences for the
#: rest of the feature have not been. Both are formatted with the child's id.
#:
#: Unlike the two constants around them these are NOT re-derived by `true_blockers` —
#: that function answers "what does this WORK ORDER need from me", and a feature order is
#: not a work order, so INV-ATTENTION-REASON never sees these and cannot relabel them.
#: They live here anyway so that every reason the OS puts in front of the user is written
#: in one file. If a feature-order invariant is ever added, it inherits the obligation:
#: whatever derives the flag has to be able to produce these strings.
FEATURE_CHILD_FAILED = "{id} failed — this feature cannot finish without it"
FEATURE_CHILD_CANCELLED = ("{id} was cancelled — this feature will not deliver what the "
                           "plan promised")


def dead_feature_children(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The children that fail their feature: ended badly, and not answered for.

    ONE function because `Daemon.settle_features` and `check_feature_failures_are_real`
    must agree exactly. A settler that fails a feature on a child the invariant then
    un-fails it on is an infinite loop, and the user watches it flap on every tick.
    """
    return [c for c in children
            if c["status"] in ("failed", "cancelled") and not c.get("superseded")]

#: What a work order says when something it depends on can never complete. A dependency
#: that is `cancelled` or `failed` will not come back, so the dependent would sit in
#: `pending` for ever with nothing on its face to say why — the stranded row the
#: feature-order design warns about. Same rule as PR_CLOSED_BLOCKER above: an attention
#: reason must be re-derivable by `true_blockers` or the next tick relabels it.
DEAD_DEPENDENCY_BLOCKER = "blocked by a dependency that can never complete"


# -- the derivation everything else is checked against ------------------------------


def rejudge_exhausted(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Is this order parked on a head the OS declined to re-judge, and still on it?

    TWO FACTS, and the second is what keeps this from sticking: the OS recorded a decline
    for some commit, and the newest automatic-merge hold is still `sha_moved` on THAT
    commit. A branch that moves again clears the flag by itself — the next tick either
    re-judges the new head (the budget is only spent for the round it counted) or
    declines afresh and raises this again against the new commit.

    Derived, never stored. `ops.rejudge_moved_head` writes the decline;
    INV-ATTENTION-MISSING puts the flag up from here, which is the path that honours
    `acknowledged_blockers` (kn-089de524).
    """
    declined = store.events_of_kind(wo["id"], REJUDGE_DECLINED_EVENT)
    if not declined:
        return False
    held = store.events_of_kind(wo["id"], "automerge_held")
    if not held:
        return False
    newest = db.from_json(held[-1]["payload"], {})
    if str(newest.get("code") or "") != HELD_SHA_MOVED:
        return False
    head = str(newest.get("head_sha") or "")
    if not head or not any(
            str(db.from_json(e["payload"], {}).get("head_sha") or "") == head
            for e in declined):
        return False
    # ...AND NOTHING HAS BOUND A VERDICT TO THAT COMMIT SINCE. A hold is only written
    # when the merge is declined, so an armed one writes nothing and the newest hold
    # goes on describing a stall that is over — the user forces the round the machine
    # left them, it passes, and this would still be flagging them about it.
    return store.validated_head(
        store.latest_validation_round(wo_id=wo["id"])) != head


def true_blockers(store: ProjectStore, wo: dict[str, Any],
                  now: float | None = None) -> list[str]:
    """The reasons this work order genuinely needs the user, derived from state alone.

    Ordered most-actionable first, so `[0]` is the canonical attention reason. This is
    the single source of truth for "what does this work order want from me" — the UI
    banner, the attention list and the invariants below all have to agree with it, and
    any of them disagreeing is a bug in that surface, not here.
    """
    blockers: list[str] = []
    pending = store.pending_assumptions(wo["id"])
    if pending:
        n = len(pending)
        blockers.append(f"{n} assumption{'s' if n != 1 else ''} pending your review")
    # A privileged action Neo declined to decide on. Nobody else can open that gate, and
    # the work order cannot proceed past it.
    for approval in store.escalated_approvals(wo["id"]):
        blockers.append(
            f"gate approval needed: {approval['kind']} (request {approval['id']})"
        )
    # Two of the blockers below only exist because Jarvis dispatched the worker and
    # briefed it on the contract (`jarvis wo finish`, worktree, OPERATION.md). A session
    # the user started and injected got none of that — no JARVIS_WO_ID, no briefing — so
    # it cannot signal completion and its session ending is not a failure. Holding it to
    # the contract anyway made every such session a guaranteed attention item.
    # See Daemon.retire_ungoverned.
    governed = wo.get("origin") not in UNGOVERNED_ORIGINS
    # OUT OF MONEY, and the reason is REBUILT rather than read off `attention_reason`:
    # this function is the only source of attention reasons — INV-ATTENTION-REASON
    # rewrites any flag it cannot derive — so a line `budget.escalate` wrote and this did
    # not repeat would be replaced by a generic one on the next reconcile tick. Rebuilt
    # from live accounting, so the figures in it stay current as the panel spends, and it
    # states the feature's unreserved remainder for a child whose slice ran out.
    #
    # ABOVE `failed`, and above every wait below: an order in this status has nothing
    # else it can be waiting for, and the question the user is being asked is about
    # money rather than about the work.
    if wo["status"] == budget.EXHAUSTED:
        blockers.append(budget_blocker(store, wo))
    if governed and wo["status"] == "failed":
        blockers.append("worker failed — review and retry")
    # Parked on the user's Claude Code sign-in (`Daemon._park_on_signin`). Before the
    # `waiting_input` branch below, whose generic "waiting on your input" would send the
    # user looking for a session to type into — the thing to do is `/login`, and once
    # they have, nothing else is asked of them. The query sits behind the status check so
    # no other work order pays for it.
    auth_parked = False
    if governed and wo["status"] == "waiting_input":
        pause = worker_session.turn_pause(store, wo["id"])
        auth_parked = pause is not None and pause.reason == worker_session.PAUSE_AUTH
        if auth_parked:
            blockers.append(AUTH_BLOCKER)
    # Blocked on a prompt is real whoever started the session: nobody else can unstick it.
    # A worker waiting on the DELEGATE is the exception, and there are two ways to be
    # waiting on it — a gate still with Neo, and a question still with Neo. Routing both
    # away from the user is the entire point of having a delegate, so each stays silent
    # until Neo either decides or hands it back.
    #
    # The question half is what GitHub issue 100 was: `jarvis wo ask` parks the work
    # order here without flagging attention, and this line put the flag straight back on
    # the next tick — "worker is waiting on your input" about a worker waiting on Neo,
    # steering the user at `jarvis wo resume-auto`, which cannot help. Three of five
    # sampled `jarvis_os` work orders had it.
    #
    # A MANAGER used to be the second exception, on the grounds that it sits in
    # `waiting_input` for its feature's entire life. It has its own status now (`idle`,
    # issue #264) and the exception is gone — which fixes a bug the exception was
    # hiding. A manager reaches `waiting_input` only by asking, so the one case it USED
    # to suppress was the case that most needed the user: Neo handing its question back.
    if wo["status"] == "waiting_input" and not auth_parked:
        question = awaiting_neo(wo["id"])
        if question is not None and question["status"] in USER_HELD_Q_STATUSES:
            # Neo handed the decision back. That IS the user's, and it gets a reason
            # naming the question rather than the generic one below, which would send
            # them looking for a session to type into.
            blockers.append(neo_question_blocker(question))
        elif (question is None and not _waiting_on_neo_gate(store, wo)
                and not store.queued_messages(wo["id"])):
            # A queued message is the reply already on its way: `ops.send_message` clears
            # the flag the moment the user writes it, and this line put it back on the
            # next tick — for the seconds between queueing and delivery, the user was
            # asked again for something they had just done. A delivery that FAILS raises
            # its own, more specific flag (`Daemon._deliver`), so nothing is lost.
            blockers.append("worker is waiting on your input")
    # A dependency that will never complete. Ordinary blocking is silent on purpose —
    # `pending — blocked by …` is a listing label, not a decision anyone owes — but a
    # dependency that is cancelled or failed cannot clear itself, so the work order will
    # wait for ever and only the user can choose between cancelling it and cutting the
    # edge (`jarvis wo unblock`). That is the difference between waiting and stranded.
    if wo["status"] == "pending" and dead_dependencies(store, wo):
        blockers.append(DEAD_DEPENDENCY_BLOCKER)
    # A PULL REQUEST THE OS TRIED TO REPAIR AND COULD NOT — conflicts, a red build, or
    # both. Derived at ONE site from PR_REPAIR_BLOCKERS, in that tuple's order; see its
    # note for what being derived at two sites cost.
    #
    # ABOVE the `needs_review` triage below, on the same precedent that ranks the closed
    # pull request first there: each is a fact about the outside world, and each changes
    # what the user does next — they were about to merge. The status still says
    # `needs_review`, which is the rest of the story.
    #
    # BELOW PR_CLOSED_BLOCKER IS THE ONE RANKING THIS DOES NOT HAVE TO MAKE, and that is
    # by construction rather than by luck: `ops.record_pr_closed` — the only writer of
    # `pr_state='CLOSED'` — closes both episodes before it flags anything, so a refused
    # pull request has no give-up left to outrank its refusal. Without that a red pull
    # request later closed unmerged would say "do not merge it as it stands" over the
    # news that nobody is going to, which is kn-b6977de3's shape exactly: a true line
    # hiding a truer one.
    #
    # UNGUARDED BY `pending`, for the reason case 2 below is: a pull request can be red
    # while an assumption is still undecided, and those are two independent things owed.
    #
    # Gated on PR_REPAIR_STATUSES, not on `pr_url` alone: an episode is only ever closed
    # by a poll, and a terminal work order is not polled. A hand-merged red pull request
    # ends in `complete_merged` with its episode still open, and an ungated derivation
    # would leave a finished work order saying "do not merge it as it stands" for ever.
    # The status check also keeps both queries off every work order that cannot be in one.
    if wo["status"] in PR_REPAIR_STATUSES:
        for repair, blocker in PR_REPAIR_BLOCKERS:
            if store.pr_repair_attempts(wo["id"], repair) >= PR_REPAIR_MAX_ATTEMPTS:
                blockers.append(blocker)
    # A PULL REQUEST THE OS CANNOT RE-JUDGE. The head moved past the verdict and the
    # round budget is spent, so the one remedy left is a person's — the spec's section 4,
    # and the ONLY `sha_moved` case that reaches this list. Gated on the status so no
    # other work order pays for the two reads.
    if wo["status"] == "waiting_pr_merge" and rejudge_exhausted(store, wo):
        blockers.append(SHA_MOVED_BLOCKER)
    if governed and wo["status"] == "needs_review":
        # THREE WAYS TO ARRIVE AT `needs_review`, ranked, and each asking the user for
        # something different. The `not pending` guards are PER LINE and not on the
        # branch head, which is the whole subtlety here: only ONE of the three can be
        # true at the same time as an undecided assumption, and hoisting the guard back
        # up — where it sat until issue 212 — silently drops that one.
        #
        # 1. A closed pull request: the work WAS delivered and then refused, so there is
        #    nothing to review in the session — the question is what to do about the
        #    refusal. Ranked first among the three: a fact about the outside world
        #    supersedes whatever the panel thought.
        #
        #    NOW UNGUARDED, and it used to carry `not pending` on the argument that a
        #    work order holding an undecided assumption had by construction never reached
        #    `waiting_pr_merge`, the only status the poll looked at — so `pr_state` could
        #    never say CLOSED here. Issue #224 widened the poll to `needs_review`, which
        #    is exactly where such a work order sits, so the co-occurrence is reachable
        #    and the guard had quietly become a way to hide a refusal behind a pending
        #    decision. Same reasoning as case 2 below: two independent things owed, and
        #    the assumptions line is appended ABOVE this one anyway, so nothing is
        #    relabelled by letting both through.
        if wo.get("pr_state") == "CLOSED":
            blockers.append(PR_CLOSED_BLOCKER)
        # 2. The panel ran its rounds and could not be satisfied. UNGUARDED, and that is
        #    the thing to preserve: since issue 212 a round runs while the user decides
        #    (`ops.land_when_cleared`), so the panel can give up on a work order whose
        #    assumption is still outstanding. They are two independent things owed, and
        #    dropping the give-up because a decision is also open is the silent
        #    relabelling kn-78346a2d names — dropping it FOR GOOD, because accepting the
        #    assumption lands the work order and nothing re-derives it afterwards.
        elif _validation_escalated(store, wo):
            blockers.append(VALIDATION_STUCK_BLOCKER)
        # 3. The landing refused to complete it over code that is on nothing but its own
        #    branch (`ops.park_unlanded`, GitHub issue #232). Above the idle line and
        #    not merged into it, because they are opposite facts about the same status:
        #    that one says the worker produced nothing anyone can see, this one says it
        #    produced something nobody can see. Guarded for the reason below, and
        #    unreachable with an assumption pending anyway — the landing that parks it
        #    only runs once the user's gate has cleared.
        elif not pending and store.work_unlanded_open(wo["id"]):
            blockers.append(UNLANDED_BLOCKER)
        # 4. Nothing more specific: the worker stopped without finishing. Guarded,
        #    because a `needs_review` holding a pending assumption is doing exactly what
        #    that status is for, and this line would call it a worker that gave up.
        elif not pending:
            blockers.append(IDLE_NO_FINISH_BLOCKER)
    # A message the user sent that the worker will never see (GitHub issue 43). Derived
    # here rather than flagged at the delivery site because `deliver_messages` never runs
    # for these — the hold is the absence of an attempt, so there is no call site to
    # raise it from — and because INV-ATTENTION-REASON rewrites any reason this function
    # cannot re-derive. Above the parked line on purpose: both say the worker has
    # stopped, and this one also says what the user is missing.
    if stuck_message(store, wo, now=now) is not None:
        blockers.append(MESSAGE_STUCK_BLOCKER)
    # LAST, and never INSTEAD of anything: a work order can be parked and also owe the
    # user a decision, and overwriting the decision with the parking is exactly the
    # silent relabelling kn-78346a2d describes. It is appended only when nothing already
    # here has told the user the worker has stopped — an assumption review is the one
    # blocker that says who owes what without saying anything about the session behind
    # it, which is how wo-a4bd6958 sat for 7h13m under a four-hour-old assumptions line.
    if all(_mentions_assumptions(b) for b in blockers):
        parked = parked_reason(store, wo, now=now)
        if parked:
            blockers.append(parked)
    # What the user has already looked at and dismissed stops being a blocker — but only
    # exactly that. Anything new still gets through (a pending assumption never can be
    # acknowledged away; `jarvis wo ack` refuses).
    acked = db.from_json(wo.get("acknowledged_blockers"), []) or []
    return [b for b in blockers if b not in acked]


def stuck_message(store: ProjectStore, wo: dict[str, Any],
                  now: float | None = None) -> tuple[dict[str, Any], str] | None:
    """The message this work order was sent and will never receive, and what holds it.

    `None` while delivery is still accounted for. Four waits are excused, and each is
    excused because something else already owns it — a second check saying the same thing
    later is how one problem becomes two lines on the attention list:

    * a status outside MESSAGE_STUCK_STATUSES, whose own note says who owns each one;
    * anything younger than `messaging.stuck_minutes`;
    * and the two `worker_session.delivery_hold` marks `accounted` — a turn in flight,
      and a retry the OS has booked and not yet missed.

    THE HOLD IS NOT RE-DERIVED HERE. `delivery_hold` is the same call
    `Daemon.deliver_messages` skips on, so "the delivery pass declined this" and "this is
    why" cannot drift; asking the same questions again independently is how they would.
    What is left over — no hold at all, or one nothing owns — is the shape GitHub issue
    43 measured: nothing is coming, and no surface says so. The reason names the hold
    rather than the symptom, because the blocker string cannot (MESSAGE_STUCK_BLOCKER).
    """
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
    hold = worker_session.delivery_hold(store, wo, now=now)
    if hold is not None and hold.accounted:
        return None
    # No hold at all is its own finding: the pass was free to send and the message is
    # still here, which is a daemon that is not turning the queue.
    return oldest, hold.reason if hold else "the delivery pass has not attempted it"


def _parked_minutes(store: ProjectStore) -> tuple[bool, int]:
    """Whether the parked check is armed for THIS project, and after how long.

    Per project like every other `inspect.alarm_*` number, and behind the same
    `inspect.enabled` switch — that flag is `InspectConfig`'s single "raise nothing here",
    and a second way to turn one thing off is a second way to be surprised by it.
    """
    from .ops import inspect_config_at

    cfg = inspect_config_at(store.project_path)
    return bool(cfg.enabled), int(cfg.alarm_parked_minutes)


def parked_reason(store: ProjectStore, wo: dict[str, Any],
                  now: float | None = None) -> str | None:
    """Why nothing is going to happen to this work order — or None if something is.

    THE PREDICATE IS `ops.waiting_on`, NOT A SECOND OPINION. That function already
    answers "what is this order actually waiting for" for `jarvis wo resume-auto`, and
    its answers divide cleanly into things the OS will do next (`IN_FLIGHT_WAITS`) and
    everything else. Deriving the same split again here is how the attention list and
    `resume-auto` come to disagree about one work order.

    Two shapes, and the second is why this is not merely the first. An order the OS still
    calls `running` with a finished turn behind it is INVISIBLY stopped — no status in
    `BLOCKED_STATUSES` reached it before this. An order that finished, was sent back to
    work and stopped again is VISIBLY stopped and flagged for the wrong thing, which is
    what `STALE_FINISH_BLOCKER` says.

    Ordered cheapest first, and that ordering is load-bearing rather than tidy: this runs
    from `true_blockers`, which runs for every work order on every reconcile tick, and
    the catalog read and `waiting_on`'s cross-database question about Neo both sit behind
    the two free row checks. No model, no transcript.
    """
    if wo.get("origin") in UNGOVERNED_ORIGINS:
        # An injected session was never briefed on `jarvis wo finish`, so a turn of its
        # that ends without one is not a worker parking mid-task. A manager used to be
        # excused here too; `idle` is not in PARKABLE_STATUSES, so the status says it
        # now and the kind does not have to (issue #264).
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
    # ...and how much of that silence was the OS itself (`holds`, user ruling
    # 2026-09-18). BELOW the wall-clock test and never above it: this reads the whole
    # timeline, and the ordering note on this function is load-bearing — every work
    # order pays for every line here on every reconcile tick, and only the handful that
    # have already been quiet for an hour pay for this one.
    from . import holds as holds_mod

    spans = holds_mod.held(store, wo["id"], now=now)
    if (now - ended) - sum(h.overlap(ended, now, now) for h in spans) \
            < minutes * SECONDS_PER_MINUTE:
        return None
    from .ops import waiting_on

    if waiting_on(store, wo)["what"] in SPOKEN_FOR_WAITS:
        return None
    finished = store.events_of_kind(wo["id"], "finished")
    if finished and float(finished[-1]["ts"]) < float(turn["started_at"]):
        return STALE_FINISH_BLOCKER
    # A `needs_review` order with no stale finish is doing exactly what that status says,
    # and is already flagged for it. A second line on every review the user has not got
    # to yet is the noise this check exists to avoid producing.
    return None if wo["status"] == "needs_review" else PARKED_BLOCKER


def _validation_escalated(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Did this work order's most recent validation round give up and ask for a human?

    Only the LATEST round is consulted. An escalation ends the loop, so an older
    escalated round with a newer one after it is not something the user still owes a
    decision on — and a round that is still `pending` owes them nothing yet.
    """
    latest = store.latest_validation_round(wo_id=wo["id"])
    return bool(latest and latest["outcome"] == "escalated")


def dead_dependencies(store: ProjectStore, wo: dict[str, Any]) -> list[dict[str, Any]]:
    """Dependencies of this work order that can never be satisfied.

    Cancelled, failed, or deleted out from under it. Anything else is merely not done
    yet, which is the normal condition of a dependency and asks nothing of anyone.
    """
    return [dep for dep in store.unfinished_dependencies(wo["id"])
            if dep["status"] in DEPENDENCY_DEAD_STATUSES or dep["status"] == "missing"]


def usage_hold_note(until: float) -> str:
    """"the Claude usage window is spent, …" — the PANEL's twin of `pause_note` above.

    Worded to match the worker-side sentence deliberately: one window refuses a worker
    turn and a validation seat alike, and a user reading a quiet fleet at midnight should
    not have to learn that these are two different things (GitHub issue #235).

    Takes the MOMENT rather than a store because three surfaces need it off three
    different carriers — the held round's own `reason`, `status_label`, and
    `parallel_round_note` — and a helper that fetched for itself would have to be written
    three times to reach them.
    """
    return ("the Claude usage window is spent, the review resumes by itself at "
            f"{clock(until)}")


def validation_hold_note(store: ProjectStore, wo_id: str, round_no: int) -> str:
    """`usage_hold_note` for a round that is actually held, or "" for one that is not."""
    until = validation_hold_until(store.events_of_kind(wo_id, "validation_failed"),
                                  round_no)
    return usage_hold_note(until) if until > time.time() else ""


def parallel_round_note(store: ProjectStore, wo_id: str) -> str:
    """" — review round N is running in parallel", or "" when none is.

    ONE HOME, because two surfaces say it and a reword that landed in only one of them
    would be two surfaces disagreeing about the same work order — the drift PR 65 is the
    standing example of. `status_label` appends it to a status; `ops.waiting_on` appends
    it to a diagnosis, and both are read by a user asking "is anything happening".

    `RUNNABLE_VALIDATION_OUTCOMES` and not the wider open set: a `rejected` round is
    waiting on the SUBMITTER, and saying the panel is still reading would send the user
    looking for something to wait for.
    """
    latest = store.latest_validation_round(wo_id=wo_id)
    if latest and latest["outcome"] in RUNNABLE_VALIDATION_OUTCOMES:
        held = validation_hold_note(store, wo_id, int(latest["round"]))
        if held:
            return f" — review round {latest['round']} is held: {held}"
        return f" — review round {latest['round']} is running in parallel"
    return ""


def base_red_note(store: ProjectStore, wo_id: str) -> str:
    """"waiting for `main` to go green", or "" when the base is not the problem.

    ONE INDEXED READ, and the write side is what buys that: `pr_base_health` is recorded
    only when the base CHANGES state, so the newest row IS the current answer and no
    second kind has to be read to know whether it is stale. An unconditional pair of
    reads here would cost every listing in the fleet a query per work order, every time,
    to notice a change that almost never happened.

    **Derived, never fetched.** Whether the base is red is a question for GitHub, and
    this module may not ask it — `poll_pull_requests` is where the network lives, and an
    invariant that shelled out to `gh` would put a subprocess behind `jarvis wo list`.
    So the poll writes down what it learned and this reads it back, which is the same
    division `ops.automerge_state` makes and for the same reason.
    """
    from . import db

    rows = store.events_of_kind(wo_id, PR_BASE_HEALTH_EVENT)
    if not rows:
        return ""
    payload = db.from_json(rows[-1].get("payload"), {}) or {}
    if not payload.get("red"):
        return ""
    return BASE_RED_NOTE.format(base=payload.get("base") or "its base")


def status_label(store: ProjectStore, wo: dict[str, Any],
                 fleet: Fleet | None = None) -> str:
    """How this work order's status should read to a human.

    `pending` alone promises "will start as soon as a slot frees", which is a lie for a
    row that is waiting on another work order — possibly for days. Every surface that
    prints a status renders it through here, so the CLI listing, `jarvis status` and the
    dashboard cannot drift apart on the answer. (They have before: a dashboard listing
    and its own header disagreed about what needed the user because each derived it
    separately — see the FEATURED_STATUSES fix in PR 65.)
    """
    # FIRST, and deliberately so. `validating` is in ACTIVE_STATUSES (it holds a slot),
    # and everything past the two early returns below is unreachable for any status but
    # `pending` — so a validating label written anywhere further down this function is
    # dead code, and nothing else in this file would catch that.
    if wo["status"] == "validating":
        latest = store.latest_validation_round(wo_id=wo["id"])
        round_no = latest["round"] if latest else 1
        # The budget is the shipped default rather than this project's configured
        # `os.validation.max_rounds`: every surface renders a status through here, and
        # this function is handed a store and a row — no catalog is in reach, and
        # threading one through every caller to print a number is not worth it.
        label = f"validating — review round {round_no} of {DEFAULT_VALIDATION_MAX_ROUNDS}"
        # Why nothing is happening, when the answer is a spent window rather than a slow
        # reviewer. Without it the label promises a review that is in fact waiting hours.
        held = validation_hold_note(store, wo["id"], int(round_no))
        return f"{label} — {held}" if held else label
    if wo["status"] in ACTIVE_STATUSES:
        note = pause_note(store, wo) or neo_wait_note(wo)
        return f"{wo['status']} — {note}" if note else wo["status"]
    # OUT OF MONEY, and ABOVE the pause note: a work order can hold a booked retry AND
    # be over its budget, and only one of the two is going to happen. `NOT_RETRIED` is
    # what makes that true — the sweep never relaunches this status — so a label naming
    # the retry would promise a turn that is never coming.
    #
    # Rendered with the figures rather than the bare word, because this is the string
    # every listing prints and "budget_exhausted" alone sends the reader to a second
    # command to learn the one thing they need: how much more it would take.
    if wo["status"] == budget.EXHAUSTED:
        cap = budget.ceiling(store, None, wo)
        if cap is None:
            # The budget was cleared and the tick that moves the status has not run.
            return "budget spent — cleared, resuming shortly"
        return (f"budget spent — ${cap.spent_usd:.2f} of ${cap.cap_usd:.2f}"
                + (" (its feature's slice)" if cap.source == "feature" else ""))
    # A booked retry on one of the settled-looking statuses the sweep reaches and the
    # branch above does not (issue #259). Ranked over the round note below it: a turn the
    # transport dropped is why nothing is moving, and it names the moment that changes.
    parked = pause_note(store, wo)
    if parked:
        return f"{wo['status']} — {parked}"
    # Below the pause note above, which names a moment this one cannot: a manager whose
    # turn was refused IS coming back, and "idle" alone would read as the steady state.
    if wo["status"] == "idle":
        return "idle — waiting for a message from its feature"
    # `needs_review` no longer means the panel is waiting for the user: since issue 212
    # the round runs in parallel with the assumption review, and a status that said only
    # "needs_review" would hide the half of the work that is still moving.
    if wo["status"] == "needs_review":
        note = parallel_round_note(store, wo["id"])
        if note:
            return f"{wo['status']}{note}"
    # WHY NOTHING IS BEING NUDGED. Below the round note, which names a thing actually in
    # flight, and gated on carrying a pull request in a status the poll reaches — so the
    # extra indexed read is paid by parked work orders and by nothing else. Without it a
    # work order whose base is broken is indistinguishable from one the OS forgot: the
    # poll deliberately spends no repair attempt in that window (spec §3), so there is no
    # nudge, no attention item and, until this, nothing said at all.
    if wo["status"] in PR_REPAIR_STATUSES and wo.get("pr_url"):
        note = base_red_note(store, wo["id"])
        if note:
            return f"{wo['status']} — {note}"
    if wo["status"] != "pending":
        return wo["status"]
    blockers = store.unfinished_dependencies(wo["id"])
    if blockers:
        return f"pending — blocked by {', '.join(dep['id'] for dep in blockers)}"
    # Ranked below the dependency label deliberately: a work order waiting on a sibling's
    # merge is not going to start when a slot frees, so naming the slot would be the less
    # true of the two answers. Neither raises attention — a slot always frees, so this is
    # the system working, not a decision anyone owes (the same rule that keeps a merely
    # unfinished dependency silent).
    cap = _slot_cap(store, wo)
    if cap:
        parent, limit, active = cap
        return (f"pending — waiting for a slot in {parent} "
                f"({active}/{limit} children running)")
    # LAST, and only for a caller that HAS the fleet's state: this one is a fact about
    # the account rather than about the work order, so it is the least specific of the
    # three answers — and a caller holding one project's store cannot know it. `None`
    # renders the bare "pending" the surface would have printed anyway, rather than a
    # guess. Like the two above, it raises no attention: the window reopens and the slot
    # frees, neither of which is a decision anyone owes (`fleet.blocked`).
    held = fleet.blocked() if fleet is not None else ""
    if held:
        return f"pending — {held}"
    return "pending"


#: How long past its own deadline a paused turn may sit before the OS says so.
#: `Daemon.retry_paused_turns` runs every `RETRY_EVERY_TICKS` ticks — about ten seconds
#: — so this is two orders of magnitude of slack: long enough that a busy tick or a
#: momentarily unreachable CLI never cries wolf, short enough that nobody loses an
#: evening to it. See `check_paused_turns_resume`.
PAUSE_OVERDUE_GRACE = 15 * 60


def clock(ts: float) -> str:
    """A moment as the reader's own clock reads it, WITH THE DATE unless it is today.

    A bare "%H:%M" is only unambiguous within the day it is written, and the pause note
    is read exactly where that breaks. Through the twelve hours wo-b4f207ad was stuck the
    dashboard promised "retrying by itself at 12:00" while meaning the NEXT day's noon,
    which is why the user believed the retry was overdue rather than mis-scheduled (PR
    129). The parse that caused that is fixed, but the ambiguity is not the parse: a
    7-day window legitimately resets days out ("resets Aug 29, 9:50am") and would still
    render as a bare "09:50" tomorrow.

    Local wall-clock, not the timezone the CLI quoted: the reader is at this machine, and
    a time they have to convert is a time they will misread.

    "Today" is read through `time.time()` rather than by calling `time.localtime()` with
    no argument. Same answer in production, and NOT the same thing: the no-argument form
    reads the C clock directly, so it is a SECOND clock this function cannot be told
    about — a caller that pins one of them moves half of the comparison and gets an
    answer belonging to neither. That is the same two-clocks-for-one-question mistake as
    the bug this whole change exists to fix, so there is one clock here.
    """
    when = time.localtime(ts)
    if when[:3] == time.localtime(time.time())[:3]:
        return time.strftime("%H:%M", when)
    return time.strftime("%H:%M on %a %d %b", when)


def _span(seconds: float) -> str:
    """A duration in the coarsest unit that still says something — "8h", "12m"."""
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}h"
    return f"{max(seconds, 60) / 60:.0f}m"


def pause_note(store: ProjectStore, wo: dict[str, Any]) -> str:
    """Why this work order is not moving and when it will move again — or "" normally.

    `running` alone promises "a worker is working on this", which is a lie for a
    conversation the transport dropped — the usage window spent, or the API itself
    failing. It keeps its status and its slot on purpose (see `worker_session`, and
    `Daemon.retry_paused_turns` relaunches it) — but the user looking at a dashboard at
    midnight is owed the reason nothing is happening, and the time it will happen again.

    Every surface renders this one string (the CLI through `status_label`, the dashboard
    through `ops.os_status`) so they cannot disagree about the answer. `clock` carries
    the date whenever the retry is not today, which a usage window over 24h out and every
    7-day reset genuinely is.

    The transient line names the attempt as well as the clock, because unlike a usage
    window — which reopens once, at a stated time — a backoff can be on its fourth of
    five, and "retrying at 14:07" without that reads as a promise it might not keep.

    SCOPED TO `RETRY_SWEEP_STATUSES`, which is the set this sentence is a promise about:
    it says the OS will relaunch the turn, so it must be readable exactly where the
    sweep will. Since issue #259 that includes the three settled-looking statuses a
    refused turn can land on, where the note was blank and the work order looked simply
    finished.
    """
    if wo["status"] not in RETRY_SWEEP_STATUSES:
        return ""
    pause = worker_session.turn_pause(store, wo["id"])
    if pause is None or pause.exhausted:
        return ""
    if pause.reason == worker_session.PAUSE_AUTH:
        # The one pause with no moment to name — it resumes on an ACTION. `clock` would
        # be asked to render `NEVER`, and any time it could print would be a guess.
        return "Claude Code sign-in expired, resuming once you sign in again"
    when = clock(pause.retry_at)
    if pause.reason == worker_session.PAUSE_USAGE_LIMIT:
        return f"Claude usage limit reached, retrying by itself at {when}"
    what = f"Claude API error {pause.status}" if pause.status else "Claude API error"
    return (f"{what}, retrying by itself at {when} "
            f"(attempt {pause.attempts} of {pause.max_attempts})")


def neo_wait_note(wo: dict[str, Any]) -> str:
    """"with Neo …" for a work order parked on a question — or "" for anything else.

    `waiting_input` renders as "Waiting on you" everywhere (`timeline.STATUS_LABEL`), and
    for the whole time Neo holds a question that label is false: it can be minutes, and
    the user reads it as a demand. The note says who actually has it. Display only — what
    costs attention is `true_blockers`, and nothing here changes that.
    """
    if wo["status"] != "waiting_input":
        return ""
    question = awaiting_neo(wo["id"])
    if question is None:
        return ""
    if question["status"] in USER_HELD_Q_STATUSES:
        return f"Neo handed question {question['id']} back to you"
    return f"with Neo — it is answering question {question['id']}"


def _slot_cap(store: ProjectStore, wo: dict[str, Any]) -> tuple[str, int, int] | None:
    """`(feature id, max_parallel, active children)` when this work order's feature order
    is already running as many children as it allows, else None.

    A parentless work order — nearly all of them — is answered from the row it was handed
    and costs no query, the same shape `ops.blocked_by` uses for `depends_on`.
    """
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


def _waiting_on_neo_gate(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """True when this work order is parked on a privileged-action request the user does
    not hold. Not a user blocker either way — but for two different reasons.

    A `pending` request is with Neo. An `awaiting_case` one is with the WORKER, which
    still asks the user for nothing: it is held precisely because nobody has argued it
    yet, and the OS closes it as abandoned on a timer if nobody ever does
    (`gates.sweep_unargued`).
    Omitting the second reads the park as the generic "waiting on your input" — the
    false flag GitHub issue 100 was, arrived at down a different road.
    """
    return (any(not a["escalated"] for a in store.pending_approvals(wo["id"]))
            or bool(store.held_approvals(wo["id"])))


def awaiting_neo(wo_id: str) -> dict[str, Any] | None:
    """The question this work order is parked on, or None — read from Neo's own DB.

    THE ONE PREDICATE for "is this work order waiting on the delegate rather than on the
    user", and the answer to the same false page arriving from three directions at once
    (GitHub issue 100): `true_blockers` below, the idle-`Notification` branch of
    `hooks.handle`, and `Daemon.settle_work_order` each decided independently what an
    idle `waiting_input` worker meant, and each decided "the user". `ops.ask_question`
    flips the status to `waiting_input` WITHOUT flagging attention precisely so that it
    does not — and all three undid that within a tick of the worker ending its turn.

    Read `q["status"]` to know who is holding it: `neo_store.NEO_HELD_Q_STATUSES` is
    Neo's and costs the user nothing, `USER_HELD_Q_STATUSES` is the user's.

    Cross-DB and deliberately so — a question's state lives in `neo.db` and mirroring it
    into a column here would drift the first time a daemon dies between the two writes.
    The cost is bounded by the callers: every one of them asks only about a work order
    already sitting in `waiting_input`, which is a handful of rows fleet-wide.

    Best-effort, and it fails TOWARDS the user: an unreadable `neo.db` returns None, so
    the generic blocker surfaces and the work order is visible-but-mislabelled rather
    than silently stalled. That is the direction `check_blocked_work_is_surfaced` calls
    the dangerous one.
    """
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
    """Is this work order parked on somebody, rather than merely having nothing to do?

    ONE RESOLVER, and kn-4ea33fe6 is why it is a function rather than a rule written
    twice: `end_wait_if_nothing_is_out` asks it to decide whether a wait has ENDED, and
    `Daemon.settle_work_order`'s manager branch asks it to decide whether a manager is
    free to be re-statused `idle`. Those two disagreeing means a manager parked on a gate
    is quietly relabelled "nothing to act on" — muted, out of the dashboard's needs-me
    strip, and refused a nudge — which is the bug issue #264 exists to remove, recreated.

    `held_approvals` counts as out, and it is the clause a caller inheriting this from
    `settle_work_order`'s `pending_approvals` check would drop. Nobody is REVIEWING a
    held request, so it is not "with a reviewer" — but the work order is not free either:
    `gates.file_request` parks it in `waiting_input` down BOTH its roads, the OS refuses
    it on the `gates.case_ttl_seconds` timer, and until then the worker is waiting for a
    verdict exactly as it would be for an argued one.
    """
    return bool(store.pending_approvals(wo_id) or store.held_approvals(wo_id)
                or awaiting_neo(wo_id))


def end_wait_if_nothing_is_out(store: ProjectStore, wo_id: str) -> bool:
    """Take a work order out of `waiting_input` once nothing is holding it there.

    Every wait in the OS parks the work order here — `gates.request`, `ops.ask_question`
    — and every one of them has to end it again, because a status that outlives its wait
    is read as a USER blocker by everything downstream: `jarvis status`, the dashboard,
    and `true_blockers` above, which renders it as "worker is waiting on your input".

    One function for all of them, because the condition is not per-wait: what ends a wait
    is that NOTHING is out, and a gate verdict delivered while a question is still with
    Neo has not ended anything. Three call sites had three copies of a two-thirds-right
    version of this (`gates.apply_decision`, `Daemon._neo_drain`, and `neo_answer_escalated`,
    which had none at all and left the flag to come straight back on the next tick).

    Narrow on the other side too: only FROM `waiting_input`, so a work order cancelled or
    settled while it waited keeps where it got to. Returns whether it moved.
    """
    if store.get_work_order(wo_id)["status"] != "waiting_input":
        return False
    if something_is_out(store, wo_id):
        return False
    store.set_status(wo_id, "running")
    return True


def neo_question_blocker(question: dict[str, Any]) -> str:
    """The attention reason for a question Neo has handed back to the user.

    One function so the reason `Daemon._neo_drain` writes at escalation time and the one
    `true_blockers` re-derives on every tick afterwards are the same string — kn-78346a2d:
    a flag whose reason `true_blockers` cannot re-derive is relabelled behind the user's
    back, and the relabelling is silent.
    """
    if question["status"] == "failed":
        return (f"Neo could not answer question {question['id']} — "
                f"answer it: `jarvis neo answer {question['id']} \"…\"`")
    return (f"Neo escalated question {question['id']} to you — "
            f"answer it: `jarvis neo answer {question['id']} \"…\"`")


# -- invariants ---------------------------------------------------------------------


def check_attention_reason_is_true(store: ProjectStore) -> Iterator[Violation]:
    """INV-ATTENTION-REASON — a flagged work order's reason must name the real blocker.

    The observed failure: a work order settles into `needs_review` with
    "assumptions pending review", then Claude Code's routine idle Notification fires a
    minute later and the hook stamps "Claude is waiting for your input" over it. The
    user is sent looking for a question that does not exist while the actual action —
    approve or reject the assumptions — sits unlabelled below it.

    Repairable: the correct reason is derivable from state.
    """
    for wo in store.list_work_orders(include_hidden=True):
        if not wo["needs_attention"]:
            continue
        blockers = true_blockers(store, wo)
        if not blockers:
            continue
        reason = wo.get("attention_reason") or ""
        # Only assumptions are enforced strictly. For other blockers a hook-supplied
        # reason ("needs permission to run X") is more specific than anything we can
        # derive, and clobbering it would repeat the very bug this invariant exists for.
        if not _mentions_assumptions(blockers[0]):
            continue
        if _mentions_assumptions(reason):
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


def check_adhoc_not_governed(store: ProjectStore) -> Iterator[Violation]:
    """INV-ADHOC-NOT-GOVERNED — an injected session must not be judged as a worker.

    A session the user started and handed over with `jarvis wo inject` is a *mirror*,
    not a dispatch: it never received the worker contract and has no way to call
    `jarvis wo finish`. Judging it against that contract parked it in `needs_review`
    (IDLE_NO_FINISH_BLOCKER) the moment it ended a turn, and in
    `failed` ("worker session disappeared") the moment the user cleaned it up — one live
    fleet accumulated fifteen of these, one of which was the session the user was
    talking to. (Back then Jarvis adopted these sessions on its own; it no longer does,
    which is why `adhoc` rows still on disk get the same treatment as `injected` ones.)

    `true_blockers` stops *new* ones. This retires the records already on disk, so the
    fix reaches a running fleet on the next reconcile tick instead of waiting for the
    user to hand-clear a dashboard.

    Repairable: with no contract there is no verdict to make. The record, its timeline
    and whatever reply was captured all stay; only the demand for the user goes away.
    Anything the session genuinely left pending (an assumption it filed itself) is left
    exactly where it is.
    """
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
    """INV-ADHOC-LEGACY-RETIRED — a session Jarvis adopted on its own is let go.

    Jarvis used to adopt every Claude session running under a registered project path
    into an `origin="adhoc"` work order, and then track it. It no longer does: adoption
    is now the user's explicit act (`jarvis wo inject`), so nothing follows those rows
    any more.

    That is why leaving them alone is not the neutral option. A row parked in
    `waiting_input` is a genuine user blocker — "worker is waiting on your input" is
    true whoever started the session, so `true_blockers` does not suppress it — and with
    nothing left to refresh it, it would ask for the user forever, about a session that
    may have ended weeks ago. This closes them once, at the point of upgrade.

    Repairable: the record, its timeline, its captured replies and any assumptions all
    stay exactly as they are; only the demand for the user goes away, which is the same
    treatment `Daemon.retire_ungoverned` gives a session that ends normally. A row with
    assumptions still pending is left alone: the user owes it an answer, and that is a
    real blocker, not adoption noise.

    Idempotent: a retired row is `completed`, so a re-run finds nothing. Only `adhoc`
    rows are touched — `injected` ones were handed over deliberately and are still
    tracked.
    """
    # Adoption only ever produced these two statuses, and no path moves an adhoc row to
    # `pending`/`dispatching`. `failed`/`needs_review` are INV-ADHOC-NOT-GOVERNED's.
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
    """INV-GATE-ORPHAN — a gate request must not outlive the work order that filed it.

    A gate is a control on something about to happen. Once its work order is completed,
    cancelled or failed there is no worker left to run the command, so an approval could
    permit nothing and a denial could stop nothing — and yet the request keeps sitting in
    `jarvis gate list --pending`, and if Neo escalated it, in the user's attention list
    and inbox, asking them to authorise an action that has either already happened or
    never will.

    That is the state wo-52a6164d ended in: `completed`, the release shipped, and a
    pending gate still demanding permission to ship it. Read from the outside, the gate
    looks like it failed to hold — which is worse than a missing control, because it
    teaches the operator that the ones still standing mean nothing either.

    Repairable: close the request as superseded. It is not a verdict and authorises
    nothing (see `ProjectStore.supersede_approval`); the command string stays blocked.

    Held requests count. One is further from a verdict than a pending one, not closer:
    no question exists for Neo to answer, and the only thing that would ever have closed
    it is a worker that is gone.
    """
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
    """INV-ATTENTION-PHANTOM — a work order with nothing pending must not ask for you.

    Covers the "I acked it and it is still in my face" case: once a work order is
    completed or cancelled there is nothing the user can act on, so a lingering flag is
    pure noise on the dashboard and in the attention list.

    Repairable: clear the flag.
    """
    for wo in store.list_work_orders(statuses=TERMINAL_STATUSES, include_hidden=True):
        if not wo["needs_attention"]:
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


def check_blocked_work_is_surfaced(store: ProjectStore) -> Iterator[Violation]:
    """INV-ATTENTION-MISSING — work that needs the user must actually say so.

    The mirror of the two above, and the more dangerous direction: a work order stuck
    with pending assumptions but no attention flag never appears in `jarvis status`, the
    attention list or the dashboard strip. It is invisibly stalled, which is how a fleet
    quietly stops moving.

    Repairable: raise the flag with the derived reason.
    """
    for wo in store.list_work_orders(statuses=BLOCKED_STATUSES, include_hidden=True):
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
    """INV-MESSAGE-STUCK — a message queued for a worker must not sit undelivered for ever.

    GitHub issue 43, and the paragraph below is the state of the OS BEFORE this check
    existed — present tense would read as a description of how it still works and invite
    the next reader to undo it (kn-88615da4).

    `Daemon.deliver_messages` held a message on four paths and every one of them WAS
    silent: the row stayed `queued`, which from the outside is indistinguishable from one
    about to go out, and `ops.waiting_on` answered `queued_message` for it — a member of
    IN_FLIGHT_WAITS, so `parked_reason` read the hold as the OS being about to act and
    every surface reported the work order healthy. Fifteen messages across four work
    orders were rotting when the issue was filed, including a gate verdict and the user's
    own two follow-ups. Today `waiting_on` answers `message_stuck` instead, SPOKEN_FOR_WAITS
    catches it, and the blocker below is what the user reads.

    The predicate and its four excused waits are `stuck_message`'s. What is added here
    is the DIAGNOSIS, which is the reason this is its own invariant rather than a line of
    INV-ATTENTION-MISSING: that check can only say "needs the user", and the useful fact
    is which message, for how long, and what is holding it.

    The repair is the flag, raised through `true_blockers` and never with a reason of its
    own — a work order can owe the user an assumption review AND be missing a message,
    and overwriting the first with the second is the silent relabelling kn-78346a2d
    describes. Runs immediately before INV-ATTENTION-MISSING, which skips anything
    already flagged, so the flag goes up exactly once whichever of the two sees it first.
    A blocker the user has acknowledged leaves `true_blockers` and the flag stays down;
    the violation is still reported, because `jarvis doctor` answers "is this healthy",
    not "have you been told".
    """
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
    """INV-ASSUMPTION-PERSISTED — every recorded assumption must exist as a row.

    The timeline saying "Assumption recorded" is a claim about a write. This checks the
    write: an `assumption` event whose content has no matching row is an assumption the
    worker believes it filed, the user will never be asked about, and no other surface
    can show — the review queue reads rows, not events.

    Repairable: the event payload carries the full content, so the row is reconstructed
    from it. Matching is by exact content, so a re-run never duplicates.
    """
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
    """INV-ATTENTION-BLANK — a flagged work order must say what it wants.

    "Needs you" with an empty reason is indistinguishable from noise, and it is the
    fastest way to teach an operator to ignore the attention strip.

    Repairable when a blocker is derivable; reported otherwise.
    """
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
    """INV-ENVELOPE-STUCK — an envelope must not sit in the queue for ever.

    The message bus (src/jarvis/bus.py) is the pipe every cross-entity message in the
    validation loop travels down, and a `queued` envelope is a message nobody will ever
    receive. From the outside it looks exactly like one that was delivered: the row is
    there, the payload is there, and nothing says the recipient never saw it. The bus has
    to own that liveness itself rather than wait for a later feature to notice.

    Predicate: still `queued` after `bus.DELIVERY_ATTEMPT_CEILING` routing attempts. The
    daemon turns the queue every tick, so an envelope only accumulates attempts by
    failing — a delivery that raised and rolled back, over and over.

    Repairable, and unambiguously so, which is rare for this module: `bus.deliver` is
    transactional and therefore safe to run again, so the repair is to attempt delivery
    once more. If that attempt leaves it `queued` too, THAT was the final attempt and the
    envelope is marked `undeliverable` with the reason in `note` — which is the whole
    point, because an undeliverable message is a fact the record must carry, while a
    queued one is a lie of omission.

    Reported once by construction: every path out of here leaves the envelope in a state
    that is no longer `queued`, so the next tick finds nothing to say.
    """
    from .bus import DELIVERY_ATTEMPT_CEILING, deliver

    stuck = [e for e in store.queued_envelopes()
             if int(e["attempts"] or 0) >= DELIVERY_ATTEMPT_CEILING]
    if not stuck:
        return
    if getattr(store, "readonly", False):
        # `jarvis doctor` without --repair. The retry would write to the CENTRAL store
        # too (a deferral with no manager is filed as a backlog item), which no proxy
        # over the project store can intercept — so the retry is described, not run.
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
    """INV-NEO-ESCALATION-STALE — a question held by the user must still be answerable.

    `ops._neo_attention` lists every `escalated` or `failed` question of every kind, so
    one whose decision was taken elsewhere goes on asking the user for a ruling nobody
    can give. Three were doing that in production, the oldest for a fortnight, and none
    was of kind `question`: an `approval` question is closed only through
    `approvals.neo_question_id`, a `plan` question only through
    `feature_orders.plan_question_id` and an `alarm` question only through
    `wo_alarms.neo_question_id` — and every one of those subjects can move that pointer
    or retire without closing what it pointed at.

    The point fixes (`ops.submit_plan`, `ops.cancel_feature_order`, `gates.open_gate`,
    `ops.review_alarm`) close them as the pointer moves, which is the only moment that
    can name what replaced it. This derives the same fact from the subject's current
    state instead, so a site that forgets costs one tick rather than for ever — and it is
    what clears the rows already stranded.

    LIVE means the subject still has this decision open: a `pending` approval, a feature
    order in `plan_review`, or an `escalated` alarm — each still pointing at this very
    question. Everything else is moot, which is the same "only the LATEST round counts"
    reading `_validation_escalated` uses. A question whose subject this project does not
    know is left alone — that is how another project's rows are skipped, since the checks
    run per project while Neo's database is OS-wide.

    Repairs on the daemon tick rather than behind `--repair` as INV-MANAGER-MISSING does:
    closing a question the OS can prove is moot creates nothing, authorises nothing and
    overwrites no verdict (`NeoStore.supersede` is guarded on the open statuses).
    """
    from .neo_store import USER_HELD_Q_STATUSES, NeoStore
    from .ops import registered_project_paths

    readonly = getattr(store, "readonly", False)
    neo = NeoStore()
    try:
        held = [q for q in neo.list_questions(statuses=USER_HELD_Q_STATUSES)
                if q["kind"] in ("approval", "plan", "alarm", "triage",
                                 "assumption")]
        # `triage` is the one kind whose subject is not a row in THIS database — it is a
        # central backlog item, and the question carries no work order at all (issue
        # #240). So ownership cannot be read off the project store the way the other
        # three read it, and without this the same question would be reported by every
        # project on the same tick. Resolved once, and only when there is one to resolve.
        #
        # `assumption` needs none of this: its subject IS a row in this database, reached
        # by `assumption_for_question`, so a question belonging to another project reads
        # as a missing row and is left alone — the rule all the non-`triage` kinds share.
        mine = (registered_project_paths()
                if any(q["kind"] == "triage" for q in held) else {})
        for q in held:
            if q["kind"] == "triage" and mine.get(q["project"]) != store.project_path:
                continue
            moot = {"approval": _stale_approval_question,
                    "plan": _stale_plan_question,
                    "alarm": _stale_alarm_question,
                    "triage": _stale_triage_question,
                    "assumption": _stale_assumption_question}[q["kind"]](store, q)
            if moot is None:
                continue
            answer, why = moot
            # Neo's store is OS-wide, so the `_ReadOnly` proxy over the project store
            # cannot intercept this write — the checker has to skip it itself.
            if not readonly:
                neo.supersede(q["id"], answer, why)
            yield Violation(
                invariant="INV-NEO-ESCALATION-STALE",
                # A plan question names the FEATURE order when its planner is gone
                # (`ops.submit_plan`), and the daemon writes this id onto the work
                # order's timeline — an FK the events table enforces. Report it
                # unattached rather than crash the reporting loop.
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
    """(answer, why) if this approval question is moot, else None."""
    approval = store.approval_for_question(q["id"])
    if approval is None or approval["status"] == "pending":
        return None
    by = f" by {approval['decided_by']}" if approval["decided_by"] else ""
    return (f"SUPERSEDED — approval {approval['id']} is {approval['status']}",
            f"approval {approval['id']} was already {approval['status']}{by}")


def _stale_plan_question(store: ProjectStore,
                         q: dict[str, Any]) -> tuple[str, str] | None:
    """(answer, why) if this plan question is moot, else None."""
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
    """(answer, why) if this alarm question is moot, else None.

    A MISSING ROW IS LEFT ALONE, as in both siblings: the checks run per project against
    an OS-wide `neo.db`, so "no such alarm here" is how another project's rows are
    skipped and cannot be told apart from a subject that has gone. The one way it could
    genuinely go — deleting the work order, which cascades `wo_alarms` — already erases
    the question itself (`ops.delete_work_order` → `NeoStore.purge_work_order`), so
    nothing is leaked by declining to guess.
    """
    alarm = store.alarm_for_question(q["id"])
    if alarm is None or alarm["status"] == "escalated":
        return None
    return (f"SUPERSEDED — alarm {alarm['id']} is {alarm['status']}",
            f"alarm {alarm['id']} was already {alarm['status']}")


def _stale_assumption_question(store: ProjectStore,
                               q: dict[str, Any]) -> tuple[str, str] | None:
    """(answer, why) if this assumption question is moot, else None.

    An assumption question Neo escalated is held by the user, and the way they answer it
    is `jarvis wo review` — which settles the assumption and never touches the question.
    So the row would go on asking for a ruling that has already been given, which is
    exactly the shape this invariant exists to catch.

    A MISSING ROW IS LEFT ALONE, as in all three siblings: the checks run per project
    against an OS-wide `neo.db`, so "no such assumption here" is how another project's
    rows are skipped and cannot be told apart from a subject that has gone.
    """
    assumption = store.assumption_for_question(q["id"])
    if assumption is None or assumption["status"] == "pending":
        return None
    return (f"SUPERSEDED — assumption {assumption['id']} is {assumption['status']}",
            f"assumption {assumption['id']} was already {assumption['status']}")


def _stale_triage_question(store: ProjectStore,
                           q: dict[str, Any]) -> tuple[str, str] | None:
    """(answer, why) if this priority re-assessment is moot, else None. Issue #240.

    The subject of a `triage` question is the BACKLOG ITEM the filing left behind, not a
    work order — this is the only kind with no work order behind it at all. So "still
    live" means that item is still waiting: once the user has promoted it, or dismissed
    it, or it has gone, nobody can act on the rating any more and the escalation is
    asking for a ruling that would change nothing.

    A missing item is decisive here, unlike `_stale_alarm_question`, and for the reason
    that check spells out: the backlog is CENTRAL, so absence means gone rather than
    "belongs to another project" — the caller has already established this project owns
    the question before calling.
    """
    from . import issues
    from .central_store import CentralStore

    payload = issues.triage_payload(q)
    item_id = (payload or {}).get("backlog_id") or ""
    if not item_id:
        # A question whose context nobody can parse. `settle_triage` already refuses to
        # act on one, and closing it here would guess at what it was about.
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
    """INV-REMEDY-PROPOSAL-STALE — a proposal nobody can answer must not sit for ever.

    An alarm at `proposed` is holding the attention flag up on behalf of a gate request
    that a reviewer is expected to decide. When that request has been closed some other
    way — superseded by `gates.open_gate`, expired by the sweep, or gone with a deleted
    row — nothing else moves the alarm, and it goes on saying "the OS is waiting for
    permission" about a request that no longer exists.

    ABSENCE IS DECISIVE HERE, WHICH IS THE OPPOSITE OF `_stale_alarm_question`'s RULE,
    and the difference is which database the subject lives in. That check runs per
    project against an OS-WIDE `neo.db`, so "no such alarm here" is how another project's
    rows are skipped and cannot be told apart from a subject that has gone. An approval
    is in THIS project's database beside the alarm that points at it, and the pointer is
    an id this project wrote — so a missing row means missing, not "somebody else's".

    `approved` is left alone: `Daemon.remedy_tick` is about to apply it, and closing it
    here would race the application it is waiting for.
    """
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
    """INV-PAUSE-OVERDUE — a paused turn whose wait is over must actually be relaunched.

    THE LIVENESS GUARANTEE `Daemon.retry_paused_turns` OWES, and the reason it is owed is
    that the pass failing to fire is INVISIBLE. A work order paused for the usage limit
    keeps its status and its slot on purpose (see `worker_session`), so from every
    surface it looks like one that is being handled: `running`, no attention flag, and a
    note promising it will retry by itself. `TurnPause.attempts` only rises when a retry
    is ATTEMPTED, so a pass that never fires leaves it at 1 of 8 for ever — `exhausted`
    is unreachable, and the path that hands an out-of-retries work order to the user can
    never be taken. Nothing else in the OS is watching.

    That is not hypothetical. wo-b4f207ad and four siblings sat silently stuck for twelve
    hours because the reset moment was re-resolved against the asking clock and ran away
    each time it arrived (PR 129); the parse is fixed, but a clock skew, a catalog
    omission, or an exception inside the pass would all reproduce the same silent day.
    This checks the OUTCOME instead of any one cause, so it survives the next one.

    Predicate: a governed work order in `RETRY_SWEEP_STATUSES` whose pause came due more
    than `PAUSE_OVERDUE_GRACE` ago. THE SAME TUPLE THE PASS WALKS, and it has to be: a
    check scoped narrower than the loop it audits is blind in exactly the rows the loop
    never reaches, which is how issue #259 went unreported while `stuck_message` named it
    on demand. The pass runs every `RETRY_EVERY_TICKS` ticks — about ten
    seconds — so the grace is two orders of magnitude of slack, and anything reported
    here is stuck rather than merely waiting its turn.

    `resumable`, not `exhausted`, because an auth pause is neither due nor exhausted
    while the sign-in has not changed: its `retry_at` is `NEVER`, and there is nothing
    overdue about a wait for an action the user has not taken yet. It becomes ordinary
    the moment they sign in — `retry_at` lands in the recent past, and this holds the
    relaunch to the same grace as the other two.

    REPORT-ONLY, and deliberately not repairable. The repair is to relaunch the turn,
    which is `retry_paused_turns`'s whole job; doing it here too would be a second
    relaunch path to keep in step with the first, which is the exact duplication
    `turn_pause` exists to prevent. An unrepaired violation raises a notification, and
    being told once that the OS's self-healing is not healing is worth more than a quiet
    second mechanism that hides it.

    Self-clearing: a relaunch makes the new turn the latest, so `turn_pause` returns None
    and there is nothing left to report. Exhausted pauses are skipped because the retry
    pass skips them too — those already reach the user through the attention flag.
    """
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
    """INV-PAUSE-DRIFT — a pause's deadline must still be the one it was given.

    THE COMPANION TO INV-PAUSE-OVERDUE, AND THE ONE THAT WOULD ACTUALLY HAVE CAUGHT
    wo-b4f207ad. Overdue-ness cannot see a runaway deadline: while the reset moment was
    being re-resolved against the asking clock it was ALWAYS in the future, so
    `now > retry_at` was never true and an overdue check would have stayed silent for
    the entire twelve hours. The two predicates cover the two ways self-healing fails —
    the pass not running, and the pass being told the wrong moment — and neither sees
    the other's.

    Predicate: the reset `worker_session._diagnose` derives now must still match the one
    `worker_session.settle_turn` recorded on the `turn_paused` event when the turn died.
    That event is written at the one moment nobody has to reason about — the refusal is
    in hand and the clock IS the turn's clock — so it is the closest thing to a
    measurement the OS has. Under the bug the two diverge by exactly a day the instant
    the stated reset passes, which is within a reconcile tick of when it matters.

    This costs the derivation nothing extra: the pause is already computed for
    INV-PAUSE-OVERDUE's sake on the same tick, and the event read is kind-filtered.

    Matched by turn `seq`, not just by work order: a conversation refused twice has two
    `turn_paused` events and comparing the newest against an older turn's deadline would
    invent a violation. Skipped when the payload predates this field, or when the message
    named no readable moment (`reset_at` is None) — there is nothing to disagree with.

    Report-only. A disagreement means the derivation is wrong, and which of the two
    numbers to believe is exactly the judgement an invariant must not make on its own.
    """
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
        # The reset is a clock time rounded to the minute, so a sub-minute difference is
        # the rounding and not a drift. A real one is a whole day.
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
    """INV-ENVELOPE-LOST — a message that reached nobody must not pass for one that did.

    `check_envelopes_move` above is about liveness: an envelope still `queued` is one
    the bus has not given up on. This one is about the ending it gives up INTO.
    `undeliverable` means the router found no work order filling the role — review
    feedback for a work order that was cancelled, or a feature whose manager is gone —
    and from the outside that is indistinguishable from delivered: the row is there, the
    payload is there, and the unit is simply waiting for a reply that will never come.

    Reported, never repaired. There is nothing to derive here — the recipient does not
    exist — so the only honest action is to tell someone, which is exactly the shape of
    the OS-level checks.

    **Scoped to units that are still open.** An undeliverable envelope is permanent, and
    a check that reported every one of them for ever would mean a project's `jarvis
    doctor` never printed a clean bill again — and a signal that can only ever say
    "something is wrong" is one an operator learns to skip. Once the subject is settled
    or cancelled the lost message can no longer cost anything, and it stays on the
    unit's own page and in `jarvis validation show` for the permanent record.
    """
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
            # Deleted out from under the envelope. Nothing is waiting on it any more,
            # which is the same conclusion as a settled subject.
            still_open = False
        if not still_open:
            continue
        yield Violation(
            invariant="INV-ENVELOPE-LOST",
            wo_id=env["subject_wo_id"],
            detail=(f"envelope {env['id']} ({env['kind']} to role {env['to_role']}) "
                    f"about {subject} reached nobody: {env['note'] or 'undeliverable'}"),
            context={"envelope_id": env["id"], "kind": env["kind"],
                     "to_role": env["to_role"], "state": env["state"],
                     "subject_fo_id": env["subject_fo_id"]},
        )


def check_validation_progresses(store: ProjectStore) -> Iterator[Violation]:
    """INV-VALIDATION-STRANDED — a unit under review must not sit on an open round for ever.

    Nothing outside the daemon moves a unit whose round is open: it raises no attention
    flag and `settle_work_order` returns early for it. Both are right while a round is in
    flight, and together they mean a daemon that dies mid-round leaves the unit invisibly
    stalled with nothing left in the OS that will ever look at it again.

    Predicate: latest round still `pending`, opened longer than TWICE
    `os.validation.timeout` ago — one timeout is what a round is allowed to take, so a
    round at 1.2x its budget is late rather than abandoned.

    **The work-order half looks for the ROUND, not for `status='validating'`** (GitHub
    issue 212, spec docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §3): a
    work order with pending assumptions holds its round while parked in `needs_review`,
    and the status query this replaced would have let exactly that one sit for ever. It
    is bounded to `OPEN_STATUSES` instead — a cancelled work order's abandoned round must
    not be handed back to a machine that would judge and then land the work the user
    stopped.

    Repaired by closing the round `failed`, not `escalated`: `counted_validation_rounds`
    ignores `failed`, so the interruption costs the submitter no round, and
    `Daemon.validation_tick` picks up `pending` AND `failed` — closing it is what hands
    the unit back. Under `jarvis doctor` without `--repair`, `_ReadOnly` blocks the write.

    Covers FEATURE orders too. Nothing sets one to `validating` yet (a sibling work order
    adds that loop), and an invariant covering half the units would look like one
    covering all of them.
    """
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


def check_feature_failures_are_real(store: ProjectStore) -> Iterator[Violation]:
    """INV-FEATURE-FALSE-FAILURE — a failed feature whose children all recovered goes back.

    `Daemon.settle_features` only ever looks at `executing` features. That is what makes
    "flag once, at feature level" true by construction — and it is also what makes
    `failed` TERMINAL. A child that recovers after the feature settled (a retry, a
    `jarvis wo done` on a wrongly-flagged failure, a late merge) leaves the feature failed
    for ever, carrying a stale reason that names a work order which is now `completed`.
    That is how fo-e353491c sat with 12/12 children done and nothing in the OS that would
    ever look again.

    Predicate: `failed`, has children, and `dead_feature_children` finds none — the exact
    function the settler fails on, so the two cannot disagree.

    Repair: back to `executing`, flag cleared. It DECIDES NOTHING. `settle_features` gets
    the feature on the next tick and completes it, or opens a validation round, exactly as
    it would have the first time.

    Repaired on the daemon tick rather than behind `--repair`, unlike INV-MANAGER-MISSING:
    the state admits one reading, and the failure mode is silence — a settled feature
    raises nothing, ever again, so nobody comes looking.
    """
    for row in store.conn.execute(
            "SELECT id, attention_reason FROM feature_orders WHERE status='failed'"
    ).fetchall():
        fo_id = row["id"]
        children = store.feature_children(fo_id)
        if not children or dead_feature_children(children):
            continue
        store.set_feature_status(fo_id, "executing")
        store.clear_feature_attention(fo_id)
        yield Violation(
            invariant="INV-FEATURE-FALSE-FAILURE",
            detail=(f"feature order {fo_id} is `failed` — \"{row['attention_reason']}\" "
                    f"— but not one of its {len(children)} children is failed or "
                    f"cancelled any more. The child recovered after the feature settled, "
                    f"and nothing re-derives a settled feature."),
            repaired=True,
            repair="back to `executing` — the next tick settles it on the real state",
            context={"fo_id": fo_id, "children": len(children),
                     "stale_reason": row["attention_reason"]},
        )


def _validation_timeout() -> int:
    """How long one validation round is allowed to take, per the LIVE catalog.

    From the catalog rather than `DEFAULT_VALIDATION_TIMEOUT` because this number decides
    a write: a project that raised the value would otherwise have its healthy long rounds
    closed out from under it. (`status_label` makes the opposite call — it only prints a
    number.) Falls back to the default when no catalog is readable; an invariant must
    never be the thing that raises.
    """
    from .ops import validation_config

    timeout = getattr(validation_config(), "timeout", None)
    return int(timeout) if timeout else DEFAULT_VALIDATION_TIMEOUT


#: How many consecutive failed sweeps mean the sweep itself is broken rather than
#: unlucky. A transport blip fails one or two; a prompt that can never satisfy its own
#: validator fails every one, for ever. Ten is comfortably past noise and is reached in
#: a few hours at the shipped cadence — against the 2215 that went unreported.
HEALTH_SWEEP_FAILURE_RUN = 10

#: How recent the newest failure must be for the run to still be happening. The canary
#: claims the sweep IS SPENDING model calls, which is a statement about now: a run with
#: nothing in it for two hours has stopped — fixed, disabled, or the daemon is down —
#: and reporting it alarms on history. Four missed sweeps at the default 30-minute floor.
HEALTH_SWEEP_FAILURE_WINDOW_MINUTES = 120


def check_health_sweep_produces_judgements(store: ProjectStore) -> Iterator[Violation]:
    """INV-HEALTH-SWEEP-MUTE — a sweep that never judges anything must not bill silently.

    THIS CHECK IS THE POINT OF ISSUE #216, more than either line of the fix it shipped
    with. The sweep could never satisfy its own validator and ran 2215 times anyway, for
    $101.78 and no finding — with no error, no flag and no stuck work order to show for
    it. A failure mode of "works, costs money, produces nothing" needs a heartbeat rather
    than a comment, because the other two halves of the fix are exactly the kind an
    innocent edit undoes with every test still green. §4.1 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.

    Predicate: of the project's last `HEALTH_SWEEP_FAILURE_RUN` sweeps, every one failed,
    AND the newest of them is inside `HEALTH_SWEEP_FAILURE_WINDOW_MINUTES`. `outcome=
    'clear'` is a healthy sweep and the common one, so a working project clears this on
    its first tick and pays a single indexed read for it.

    THE WINDOW IS NOT TIDINESS. Without it this check reported the first deploy of its
    own fix, five seconds after the daemon booted, on the history the fix was written
    for — and a sweep that has been SWITCHED OFF would alarm for ever, because with no
    sweeps running no new row ever arrives to push the old ones out. A run still going
    is still reported however long ago it started; what ages out is a run with nothing
    recent in it.

    NOT repairable, and it must not try. The rows are honest — they record what really
    happened — and the defect is in the prompt or the transport, neither of which is
    derivable from state. The detail carries the most recent `health_reviews.detail`,
    which for the #216 shape is the model's reply itself and names the problem outright.

    Silent on a project that has never swept: the sweep ships disabled, and no rows is
    not a run of failures.
    """
    recent = store.recent_health_reviews(HEALTH_SWEEP_FAILURE_RUN)
    if len(recent) < HEALTH_SWEEP_FAILURE_RUN:
        return
    if any(r["outcome"] != "failed" for r in recent):
        return
    window = HEALTH_SWEEP_FAILURE_WINDOW_MINUTES * 60
    if db.now() - float(recent[0]["ts"] or 0.0) > window:
        return
    yield Violation(
        invariant="INV-HEALTH-SWEEP-MUTE",
        detail=(f"the last {HEALTH_SWEEP_FAILURE_RUN} health sweeps all failed, so the "
                f"sweep is spending model calls and producing no judgement. Most recent "
                f"reason: {str(recent[0]['detail'] or '(none recorded)')[:200]}"),
        context={"failures": HEALTH_SWEEP_FAILURE_RUN,
                 "last_detail": str(recent[0]["detail"] or "")[:500]},
    )


def check_work_lands(store: ProjectStore) -> Iterator[Violation]:
    """INV-WORK-LANDED — a completed work order's pull request must have merged.

    THE STANDING ANSWER TO "what has this fleet delivered that nobody merged". The audit
    behind GitHub issue #232 was a one-off that found six stranded orders across 209, two
    of them pull requests open for seven weeks carrying ~3,100 lines, and it should not
    have to be repeated by hand.

    **IT JUDGES THE PULL REQUEST, NOT THE DIFF** — a deliberate narrowing, ruled by the
    user on 2026-09-18 after this check reported seven completed `jarvis_os` orders as
    unlanded and five were false positives. `src/jarvis/landing.py`'s docstring carries
    the evidence and the ruling verbatim; the short version is that the content test it
    used to run survives a squash merge and does not survive a refactor, so work that
    merged months ago and was rewritten since read as absent.

    Four states, and only the middle two are violations:

    * merged -> satisfied. No arithmetic. Merged is the answer.
    * open -> delivered and WAITING ON A MERGE. The remedy says so: this is not stranded
      work, it is work nobody has merged, and telling the user to "rescue" it would send
      them looking for something that is sitting in a pull request.
    * closed unmerged -> delivered and REFUSED. Somebody decided, on GitHub, where the
      work order could not see it, and the order is still claiming `completed`.
    * NO PULL REQUEST -> out of scope, silent. See the next paragraph: that is another
      invariant's question, and this one would answer it wrong.

    **THIS CHECK IS HALF A CHAIN, AND THE OTHER HALF IS INV-PR-RECORDED** (work order
    `wo-2005a89b`). Read them together or neither of them is safe.

    * INV-PR-RECORDED holds the front: an order that CHANGED CODE may not settle with an
      empty `pr_url`. It is what makes the column trustworthy, and it is the reason the
      filter below is allowed to be `if not wo["pr_url"]: continue` — a skip that would
      otherwise be a silent exemption for exactly the orders this exists for. `pr_url` is
      NULL on the live `wo-5eedc84d`, which merged pull request #42, and that order is
      INV-PR-RECORDED's to report, not this one's.
    * THIS check holds the back: once the order is completed, the pull request it recorded
      must have merged.
    * The `wo-cd73c537` shape — a `pr_url` naming a MERGED #81 while #116 carries the rest
      of the work and is open — reads as `landed` here and is silent. Said plainly rather
      than worked around: this check judges the pull request the order RECORDED. A
      `pr_url` that names the wrong pull request is a recording defect, which is the front
      half of the chain, per the user's ruling of 2026-09-18: *"If there is any code
      change made in an order, then pr_url must not be empty, and the invariant should
      rely on that pr_url to check the change lands on main once the order gets
      completed."*

    Scoped to `completed`, and only that. `waiting_pr_merge` is a merge queue the poll
    already watches; `cancelled` and `failed` never claimed the work was done. `completed`
    is the status that makes the claim, which is the one worth checking. HIDDEN ORDERS
    ARE INCLUDED, on `poll_pull_requests`' reasoning: hiding drops a record from listings,
    it does not mean the record may go on saying something untrue.

    **IT NEVER ASKS GITHUB.** That is what keeps it cheap enough for the daemon's sweep —
    it is a pure timeline read with no subprocess at all. `Daemon.refresh_landings` makes
    the round trip, reading the order's own `pr_url`, and writes what GitHub said to the
    timeline as a `landing_seen` event; this reads the latest one.

    **AN ORDER NOTHING HAS READ YET IS SILENT ABOUT ITS PULL REQUEST, AND SAYS SO OUT
    LOUD** — `INV-LANDING-AUDIT-FRESH`, the second violation this function yields, and it
    is here because review round 1 caught the alternative being a lie. The refresh is the
    daemon's, so this check has no data until the first sweep; without that second
    violation `jarvis doctor` printed "✓ all OS invariants hold" on a project with
    genuinely unmerged work, which is an audit with NO DATA reading identically to a clean
    bill of health. That is the single worst thing a checker can do, and it is worse than
    the false positives this rewrite removed, because nothing shows it happening.

    So: one project-level line, no `wo_id`, naming how many orders in the population have
    never been read or were last read more than `landing.FRESH_FOR_SECONDS` ago. It shrinks
    as the sweep fills in and disappears in steady state; it stands for ever on a project
    whose daemon is not running, which is exactly what is true. The freshness window is
    `landing`'s and not the daemon's precisely so the two halves cannot disagree — a
    second copy of that number would let the audit go quiet at the moment it stopped
    knowing anything.

    `repair=False` changes nothing about either answer: there is no write on this path.

    **AN ORDER THE USER CLOSED BY HAND IS EXCLUDED.** `jarvis wo done` over unlanded
    work is the one landing that records instead of refusing (`ops.mark_done`), and its
    `work_unlanded` event carries `closed_by: marked_done`. That event IS the decision
    this sweep asks for, so it excuses the order exactly as `abandoned` does — otherwise
    the sweep names it every hour with a remedy telling the user to run a different
    command on an order they already closed, which is how a checker gets switched off.
    Both exclusions lapse the same way: the episode arithmetic in `work_unlanded_open`
    and `work_abandoned` retires a decision as soon as the work is delivered again.

    Not repairable, and it must not try. What to do with an unmerged pull request — merge
    it, re-open it, drop it — is exactly the decision `--abandon` exists to record, and
    deriving one is not the OS's to make.
    """
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
            # No current answer about this pull request. COUNTED, never guessed at, and
            # reported once below rather than per order — see the docstring.
            unchecked += 1
            continue
        found = landing.from_record(wo_id, db.from_json(seen[-1]["payload"], {}))
        if not found.unsettled:
            continue
        # The two remedies differ in their FIRST clause, because the two states differ in
        # what the reader has to go and do: an open pull request is one click, a closed
        # one is a decision somebody already made and has to be reversed or ratified.
        remedy = ("Merge it" if found.verdict == landing.AWAITING_MERGE
                  else "Re-open and merge it")
        yield Violation(
            invariant="INV-WORK-LANDED",
            wo_id=wo_id,
            detail=(f"completed, but its pull request has not merged: {found.detail}. "
                    f"{remedy}, or record the decision to drop it with `jarvis wo "
                    f"finish {wo_id} --summary \"...\" --abandon \"<why>\"`."),
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
    """INV-PR-RECORDED — a settled work order that wrote code must carry its pull request.

    THE GUARANTEE INV-WORK-LANDED RESTS ON. That check judges the recorded pull request
    and nothing else, which is only safe if something else holds `pr_url` populated for
    every order that produced code; this is that something. The two read as a chain: this
    one says the identifier exists, that one says the work behind it reached the default
    branch. Ruled by the user on 2026-09-18 — "if there is any code change made in an
    order, then pr_url must not be empty, and the invariant should rely on that pr_url"
    (docs/superpowers/specs/2026-09-18-an-order-that-wrote-code-carries-its-pull-request.md).

    **THE POPULATION COMES OFF THE TIMELINE, NEVER OFF THE REPOSITORY** (Neo question
    429). Every settlement route now writes what the order authored onto the event it
    already writes — `ops.finish`, `ops.park_unlanded`, `finish --abandon`,
    `Daemon._close_feature_manager` — because `landing.authored` is exact only while the
    worktree exists, and a worktree is gone long before anyone audits. So this is a pure
    SQL-and-JSON read, cheap enough for `INVARIANTS` rather than `SLOW_INVARIANTS`, and
    it never asks git or `gh` anything.

    The price is that it is STRUCTURALLY SILENT on anything settled before it shipped:
    `wo-5a6b2d6d` — a planner that completed over a WIP commit on `rescue/wo-5a6b2d6d`
    with no pull request, ever — carries no such record and is reported by nothing. The
    user accepted that explicitly; those orders are being closed by hand, and this exists
    to stop the next one. `landing.authored_in` keeps "nobody looked" distinct from
    "produced nothing" so the silence is a known gap rather than an exoneration.

    Three exemptions, each a decision somebody WROTE DOWN, and all three lapse the same
    way — `work_abandoned`'s episode arithmetic retires a decision the moment the work is
    delivered again:

    * a recorded `pr_url`, which is the whole point;
    * `finish --abandon`, the worker saying this is deliberately not being landed;
    * `jarvis wo done`, whose `work_unlanded {closed_by: marked_done}` is the user's own
      decision and the documented exit for a pull request that will never merge (Neo
      question 430; `ops.mark_done` records rather than refusing, and always has).

    **THE STALE `pr_url` IS OUT OF SCOPE**, and deliberately: `wo-cd73c537` records #81,
    which merged, while #116 carries the rest of its work and is open. This predicate
    asks whether an identifier is PRESENT, which is a fact the record holds; asking
    whether it is CURRENT is a `gh` round trip per settled order and belongs with the
    daemon polls that already have a network — and NEITHER OF THEM DOES IT TODAY:
    `Daemon.poll_pull_requests` asks about the pull request an order is parked behind,
    `Daemon.refresh_landings` about the one it recorded, and both read `pr_url` rather
    than looking for a branch's live pull request. Filed as `bl-2aabaee8`.

    Not repairable. An empty `pr_url` has two resolutions — find the pull request that
    exists, or record that none ever will — and nothing in the database distinguishes
    them. Discovery is what closes the gap once it can write back, and it is the daemon's.
    """
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
    """INV-MANAGER-SLOTS — a project manager order must not spend a concurrency slot.

    `ProjectStore.count_active` is what `Daemon.dispatch_pending` compares against
    `max_concurrent`, and it excludes `kind='manager'` on purpose: a manager runs a turn
    every time its feature reports and is idle in between, for the whole life of that
    feature, because coordinating is what it is FOR. Counted, two features in flight
    would spend a `max_concurrent: 2` project's whole budget on bookkeeping and the
    project would stop claiming work altogether.

    That failure is why this check exists rather than a comment. It degrades into "the
    project has gone quiet", which nobody reports as a bug and no other surface shows —
    there is no error, no flag and no stuck work order, just a queue that stops moving.
    Written as a canary for the same reason `check_gate_canaries` is: the protection can
    be regressed by an innocent-looking edit years from now, so it is re-derived from live
    state instead of trusted.

    Predicate: `count_active()` equals the number of work orders in SLOT_STATUSES whose
    kind is not `manager` — the cap's set since issue #134, NOT `ACTIVE_STATUSES`. Both
    sides are computed here, one through the method under test and one directly in SQL,
    so the check cannot pass by sharing the bug — which is also why it does not count
    `list_work_orders`, whose `limit` would quietly under-report on exactly the busy
    project where a lost slot hurts most.

    NOT repairable, and it must not try: a code regression is not derivable from state,
    and there is nothing in the database to fix — writing to rows here would corrupt
    healthy data in response to a bug in a query. The detail names `count_active` so
    whoever reads `jarvis doctor` knows where to look.

    Silent on healthy state, including the overwhelmingly common state of no manager at
    all: with none, both sides count the same rows and the difference is zero.
    """
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


# -- configuration checks (catalog, not database state) ------------------------------


def check_gate_deny_conflict(spec: Any) -> Iterator[Violation]:
    """INV-GATE-DENY-CONFLICT — an enabled gate must not also be denied outright.

    A gate and a `deny` rule for the same command are not belt-and-braces, they are a
    contradiction that resolves against the gate. Claude Code evaluates deny rules
    regardless of what a PreToolUse hook returned, so the approved retry is blocked too
    — and every surface reports success along the way: the request is filed, Neo reviews
    it, Neo approves it, the timeline says `gate_opened`. Only the command silently never
    runs, which reads to the user as the worker being incompetent rather than the
    catalog being wrong.

    Not repairable: the fix is an edit to the user's catalog, which is theirs to make.
    Reported with the exact rule to delete.
    """
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
    """Config-level post-conditions over the catalog itself.

    Separate from the store checks below because the subject is different: these predict
    that the OS *cannot* do something it has been configured to do, which no amount of
    database state will reveal.
    """
    found: list[Violation] = []
    for spec in catalog.projects:
        if project and spec.name != project:
            continue
        found.extend(check_gate_deny_conflict(spec))
    return found


# -- OS-level checks ($JARVIS_HOME state, not any project's database) -----------------

#: How long a pending-release marker may sit in flight before it is a fault. The whole
#: staged flow is minutes end to end (worker settles → restart → boot verification), so
#: an hour means a hand-off was dropped: the daemon never ran, the restart never landed,
#: or the boot check never fired.
RELEASE_MARKER_STALE_AFTER = 3600.0


def check_gate_canaries() -> Iterator[Violation]:
    """INV-GATE-CANARY — every command that must gate still gates.

    The gate recognisers are no longer constants; they are rows the OS writes itself when
    a reviewer dismisses a false positive (see gate_rules.py). That is the point — it is
    how the classifier stops re-asking a question already answered — and it is also the
    one change that could quietly disarm a gate. A learned exemption is canary-tested
    before it is admitted, but "tested once, at birth" is not the property worth having:
    rules accumulate, a later one can interact with an earlier one, and a user may retract
    a recogniser without seeing what it was holding up.

    So the check is re-derived from the LIVE table, and it asks the only question that
    matters about a self-modifying classifier: does a real `shipit`, a real `gh pr merge`,
    a real `systemctl restart` still get stopped? Failure here means something can now
    ship unreviewed, which is the failure this whole subsystem exists to prevent, and it
    is invisible from every other surface — nothing looks wrong when a gate simply never
    fires.

    WHERE IT RUNS, precisely, because the answer is narrower than "continuously" and the
    difference matters to anyone relying on it. This is an `OS_INVARIANTS` member, and
    `check_os()` is called by `jarvis doctor` — NOT by the daemon's reconcile tick, which
    runs the per-project `INVARIANTS` only. So this is a periodic audit the user (or a
    cron) triggers, not a live guard.

    That is sufficient because it is the third line of defence, not the first two. A rule
    is canary-tested before it is ever admitted (`gate_rules.propose_exemption`), and
    `ops.retract_gate_rule` re-runs the whole set the moment a recogniser is retracted and
    hands the failures straight back to whoever retracted it. What is left for this check
    is slow drift — rules accumulating until two of them interact — which is exactly the
    kind of fault an audit catches and a per-tick check would only find sooner.

    Not repairable automatically: which rule to retract is a judgement about what the user
    meant, and the wrong choice re-breaks the thing the exemption was fixing.
    """
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
    """INV-RELEASE-MARKER-STALE — a staged release must not sit unapplied for an hour.

    The marker (`$JARVIS_HOME/run/pending_release.json`) is a hand-off between the
    deploy script's `--stage` mode and the daemon (src/jarvis/release.py): `staged`
    waits for the shipping worker to settle, `restarting` waits for the new daemon to
    verify on boot. Both are meant to clear within minutes, so either state an hour on
    is a release the fleet believes is in flight and nothing is advancing.

    `failed_verification` is deliberately NOT flagged: that state already raised
    attention on the work order and an inbox warning when it was written, and it stays
    on disk until a human resolves it — reporting it hourly would say the same thing
    twice. An unreadable marker IS flagged, whatever it says: no state can clear it.

    OS-level, not per-project (it reads $JARVIS_HOME, not a project store), so it is
    not in `INVARIANTS`; `ops.run_doctor` calls it directly.

    Not repairable: which half of the hand-off died is not derivable from the file.
    """
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


# -- OS-level invariants --------------------------------------------------------------
#
# The project checks above are predicates over one project's store. These are predicates
# over the OS itself, so they take no store and run once per `jarvis doctor`, not once
# per project. They are never repairable: nothing here has a resolution derivable from
# state — a crashed dashboard needs a person.
#
# `check_release_marker` above is the same class of check but predates this registry and
# is still called directly by `ops.run_doctor`; it takes a `now` override the registry
# has no way to pass.


def check_ui_healthy() -> Iterator[Violation]:
    """INV-UI-HEALTHY — the OS's own dashboard must not be throwing 500s.

    The dashboard used to fail in a blind spot: uvicorn printed the traceback to the
    systemd journal and nothing else recorded it, so an HTTP 500 on the work-order page
    was invisible to `jarvis status`, to `jarvis doctor`, to the inbox and to every
    agent that can read `$JARVIS_HOME` but not `journalctl`. A user who was not already
    tailing a log had no way to learn the web UI was broken. `uilog` now keeps the
    errors next to the databases; this is the check that reads them back.

    Not repairable: reported only. The window is `uilog.ERROR_WINDOW_SECONDS`, so this
    clears itself once the dashboard has been quiet for a day.
    """
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


def check_config_drift() -> Iterator[Violation]:
    """INV-CONFIG-DRIFT — the catalog on disk must be the version the ledger calls head.

    The ledger is only evidence if it describes the file the fleet is actually running,
    and `jarvis config` is not the only way that file changes: a hand edit, an `scp`, a
    restore of a backup copy all move it behind the record's back. Content addressing
    makes the check one hash (spec §3, §6).

    A `jarvis doctor` check ONLY — an `OS_INVARIANTS` member, and `check_os()`'s single
    caller is `ops.run_doctor`. Deliberately not on the daemon's reconcile tick: a hand
    edit is legitimate, `Daemon.reload_catalog` has already applied it, and a fleet that
    filed an inbox item every time someone opened their editor would teach the user to
    ignore the one that matters.

    Not repairable, and the two repairs are opposites: `adopt` keeps the file and moves
    the record, `restore` keeps the record and moves the file. Nothing in the state says
    which of them the user meant.
    """
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
    # The DOCUMENTS, not the ids: a release-rebase row is addressed by document AND
    # build (§6.1), so an id comparison would report permanent drift after an upgrade
    # that moved a default — on a file nobody has touched.
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
    """INV-SERVICE-PATH — the installed units' PATH must reach `gh`.

    A unit's `Environment=PATH=` is the daemon's PATH and therefore every worker's
    (`claude_cli.spawn_turn` copies `os.environ`), so a `gh` missing from it means
    `gh pr create` dies at the end of a work order, `jarvis bug report` fails fleet-wide
    and PR-merge polling is silently off — issues #41 and #90.

    Those were fixed in `scripts/install_prod_service.sh`, and the fix then sat on disk
    unapplied for a release and a half, because installing the units is a one-time
    manual step and nothing ever compared what the script renders with what is
    installed. This is that comparison, narrowed to the part that has actually broken.

    Reads the FILE, not `systemctl show`: what is on disk is what the next start uses.
    `bugreport.heal_path` repairs the running process, so the live daemon can be healthy
    while the unit that starts it is not — checking `os.environ` would report nothing.

    A `jarvis doctor` check only (see `check_config_drift` on why `OS_INVARIANTS` stays
    off the reconcile tick). Not repairable: rewriting a systemd unit and reloading the
    manager is a privileged action, not something a read-only check may do.
    """
    from . import bugreport, release

    gh = bugreport.gh_bin()
    if os.sep not in gh:
        return  # `gh` is nowhere on this machine: a real problem, but not this one's,
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
    """INV-PROD-CLEAN — the production checkout must be byte-identical to its tag.

    "Reproduce in prod, fix it in dev, ship it" is only trustworthy while prod is
    exactly what the tag says. Drift there is invisible: nothing errors, `jarvis
    --version` merely gains a `-dirty` suffix, and the next deploy's `git checkout -f`
    erases the evidence. Issue #202 went unnoticed for nine releases because the only
    symptom was a version string nobody read.

    Reports tracked modifications only (`-uno`): untracked files are not drift — `.venv/`
    and `.jarvis/` live in that checkout by design, and the deploy never removed them.
    No exemption list, unlike `scripts/shipit.sh`'s clean-tree precondition: the OS's
    managed-artifact writes land in the DEV checkout registered in the catalog, never in
    the production one, so anything modified here is genuinely unexplained.

    The ref the remedy names comes from git (`release.production_status`), never from the
    checkout's `pyproject.toml` — that file is one of the things drift can touch, and a
    remedy built from a drifted version names a tag nobody cut.

    A `jarvis doctor` check only (see `check_config_drift` on why `OS_INVARIANTS` stays
    off the reconcile tick). Not repairable: discarding files in a checkout is
    destructive, and the drift is the one thing worth looking at before it is thrown
    away.
    """
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


# -- cache health ----------------------------------------------------------------------
#
# Two post-conditions on the fleet's cache configuration, from findings 2 and 4 of
# docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md. Both are OS-level
# and not per-project: each decides something there is exactly one of, and a per-project
# copy would report one fleet decision once per project.
#
# NEITHER WALKS A TRANSCRIPT. They read `bill.cache_writes_since`, which is an indexed
# query and a JSON parse per sealed order — the objection finding 2 raised against doing
# this at all was the cost of the scan, and on this machine a full walk is ~5 minutes
# over 4,642 transcripts. The price is that both see only bills sealed at
# `bill.PAYLOAD_VERSION` 3 or later, which each violation says out loud.


def _cache_health() -> tuple[Any, Any] | None:
    """The fleet's cache-write cohort, and the config that judges it. None if no catalog.

    Summed across every active project, because both readers are asking about the fleet.
    A project whose path has gone is skipped rather than reported: that is
    `ops.run_doctor`'s own finding to make, and it makes it per project.

    Recomputed by each check rather than shared between them. Two indexed queries per
    project per `jarvis doctor` run is not worth a memo that outlives the run it was
    taken in — the whole point of a post-condition is that it reads the state as it
    currently is.
    """
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
    """THE ANSWER TO "it cries wolf on a quiet day", and it is a silence, not a caveat.

    Below either floor both checks yield nothing at all rather than a hedged finding: a
    fleet that settled three orders yesterday HAS a ratio, and reporting it with a note
    about the volume still puts a number in front of a reader who will act on it. The
    two floors answer different questions — too few orders is one order's shape wearing
    the fleet's name, too few boundaries is one 300k write's — so either alone lets the
    other case through (`catalog.DEFAULT_CACHE_HEALTH_MIN_ORDERS`).
    """
    return (writes.orders < cfg.cache_health_min_orders
            or writes.boundaries < cfg.cache_health_min_boundaries)


def _cohort_note(cfg: Any, writes: Any) -> str:
    """What was actually measured. Said on both violations, because the number they
    report is over Jarvis's own worker sessions and the command they send the reader to
    is over every transcript on the machine — two populations, one decision."""
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
    """INV-CACHE-TTL-TRIGGER — the 1-hour cache write has started paying, and nobody
    would have noticed.

    Finding 2 kept the 5-minute write and its action was "re-measure monthly"; its own
    stated con was that the trigger needs a person to remember, and for two weeks nobody
    did. This is the reminder, and it is a post-condition rather than a reminder because
    the crossing is a fact about the fleet's own bill.

    THE COMPARISON IS THE ENTIRE HAZARD OF THIS CHECK. `usage.TTL_BREAK_EVEN` is a share
    of ALL CACHE WRITES, because the 1-hour premium is charged on every written token.
    The TTL's share of the RE-WRITE TAX is a different ratio over a much smaller
    denominator, it runs at about double, and comparing THAT against 39.5% says "switch
    now" when the answer is "keep the 5-minute write" — the error PR #160's own draft
    made (kn-1449447a (4)). `bill.CacheWrites.ttl_share_of_tax` exists only to be printed
    beside the deciding ratio here, never to be compared with anything, and
    `tests/test_cache_health.py` pins that a cohort straddling the two stays quiet.

    Reports rather than decides: switching the TTL is a fleet-wide config change and
    `scripts/cache_ttl_cohort.py` is what prices it against the wider population.
    """
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
    """Which of the suspects the SessionStart hook actually watched move — "" if none.

    `hooks.note_prefix` fingerprints the inputs to the prompt prefix once per turn and
    records a `prefix_drift` event when one moves. It cannot see a cache boundary, so it
    never adjudicates — but this check has the opposite problem: it knows the prefix got
    worse and has only the suspect list below to offer about WHY. Reading the hook's
    events here is what turns "check these three things" into "the Claude Code version
    changed on 12 Sep", and it is the only place the two signals meet.

    Empty rather than hedged when it saw nothing, so the suspect list it narrows is
    printed either way: the hook is young, most of the fleet's settled orders predate it,
    and an absence here is not evidence that nothing moved. One indexed query per project.
    """
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

    # A dead ingredient is reported even when nothing moved, and BEFORE the movers: the
    # reader's next step turns on which suspects were actually being watched, and the
    # suspect list below would otherwise be read as four checks when it was three.
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
    """"12 Sep", in local wall-clock for the reason `clock` gives at length: the reader is
    at this machine, and a time they have to convert is a time they will misread."""
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


OS_INVARIANTS: tuple[Callable[[], Iterator[Violation]], ...] = (
    check_ui_healthy,
    check_gate_canaries,
    check_config_drift,
    check_service_path,
    check_production_clean,
    check_cache_ttl_trigger,
    check_prefix_stable,
)


def check_os() -> list[Violation]:
    """Run the OS-level invariants. Read-only by construction."""
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
    """INV-SCHEDULE-HELD — a recurring job that has wanted to fire for days.

    The scheduler holds SILENTLY by design: a job whose previous order has not settled
    does not fire, does not queue, and does not flag anything, because a stack of
    identical daily orders is the alarm nobody reads (`schedule.decide`). The cost of
    that choice is that a scheduler which has quietly stopped looks exactly like one that
    is politely waiting, and the difference is invisible on every surface. This is the
    difference, and it is why `scheduled_jobs` persists the hold rather than recomputing
    it: what matters is how long, and only the row remembers.

    `held_alarm_intervals` whole intervals, three by default — a doctor order the user
    has not reviewed by lunchtime is a normal Tuesday, and reporting that would put the
    scheduler on the attention list for working correctly.

    NOT REPAIRABLE, and it must not try: the remedy is to settle the order that is in the
    way (or to decide it never will be), which is a judgement about that work, not a
    derivation. Silent on a project whose scheduler is OFF, or which does not run the
    job any more — `check_health_sweep_produces_judgements`' lesson, where a switched-off
    mechanism with rows still on disk would otherwise alarm for ever.
    """
    from .ops import schedule_config_at
    from .schedule import held_seconds

    cfg = schedule_config_at(store.project_path)
    if not cfg.enabled:
        return
    # `db.now`, not `time.time`: `held_since` was written from that clock by
    # `record_schedule_hold`, and judging a stored moment against a different clock is
    # how a check comes to disagree with the thing it is checking (kn-e6373014).
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
    """INV-BUDGET-OVERSPENT — an order past its ceiling that is still able to spend.

    The post-condition behind the whole feature, stated as the OS's own claim rather than
    as a test's: a work order with a budget is either inside it or parked in
    `budget_exhausted`. Anything else means the next dispatch, message delivery or retry
    will hand `claude` a negative remainder — or, if a caller ever skips
    `worker_session._launch`, no cap at all.

    THE REPAIR IS THE SETTLER'S OWN. `Daemon.settle_work_order` parks a spent order every
    tick, so a violation here is not "nobody has parked it yet" — it is that something is
    keeping it out of that branch, and the one thing that can is a status the settler
    returns early on. Reporting rather than repairing keeps this a check on the settler
    instead of a second implementation of it.

    EXCEPT WHERE THE SETTLER DECLINES ON PURPOSE. An order whose validation round is
    still runnable is deliberately left unparked so the panel can finish judging work
    that is already delivered, and an invariant that flagged that window would report the
    OS's own design as a defect on every tick — noise that teaches the reader to ignore
    the line. The exemption is narrow by construction: it is the same predicate the
    settler branches on, so if that branch is ever widened this check widens with it, and
    a round that is not runnable is judged like anything else. Nothing escapes for long —
    a panel round cannot outlive `validation.timeout`.

    Silent for every order with no budget, which is the fleet as it stands: `ceiling`
    returns None on a row with neither a budget nor a reservation, so this costs one
    indexed read per open order and yields nothing.
    """
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
            # The window the settler declines on purpose. Asked AFTER the cap, so the
            # query is paid for only by an order that is actually over its ceiling.
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


INVARIANTS: tuple[Callable[[ProjectStore], Iterator[Violation]], ...] = (
    check_assumptions_persisted,   # rows first: the others read pending_assumptions
    check_no_orphan_gate_requests,  # ...and gates before the flag checks: an orphan
                                    # request is a blocker they would otherwise believe
    check_attention_reason_is_true,
    check_adhoc_not_governed,      # retire before the flag checks judge the leftovers
    check_legacy_adhoc_retired,    # ...and before them, let go of what nothing tracks
    check_no_phantom_attention,
    check_messages_are_delivered,  # before INV-ATTENTION-MISSING, which skips anything
                                   # already flagged: whichever sees it first raises the
                                   # same `true_blockers[0]`, so the flag goes up once
    check_blocked_work_is_surfaced,
    check_attention_has_reason,
    check_pull_request_recorded,   # order-free: a pure timeline read that repairs
                                   # nothing and touches no flag. NOT a SLOW_INVARIANT —
                                   # it asks the record, never the repository
    check_manager_slots,           # a canary, not a state check: it repairs nothing and
                                   # is unaffected by the order it runs in
    check_health_sweep_produces_judgements,  # ditto: a pure read of the sweep ledger,
                                   # repairing nothing and read by nothing else
    check_paused_turns_resume,     # ditto: a pure read of what the retry pass did or
                                   # did not do, with nothing to repair
    check_pause_deadline_stable,   # ...and its companion: the pass can also be failing
                                   # because the moment it was given keeps moving
    check_schedule_progresses,     # ditto, one mechanism over: a pure read of the
                                   # scheduler's clock, repairing nothing
    check_budgets_are_enforced,    # ditto again: it reports what the settler did not do
                                   # and repairs nothing, so nothing depends on where it
                                   # sits
    check_validation_progresses,   # after the flag checks: its repair touches no flag,
                                   # and a `validating` row is invisible to all of them
    check_feature_failures_are_real,  # order-free: it reads and writes feature orders
                                   # only, which no work-order check looks at
    check_neo_escalations_are_live,  # order-free: it writes to Neo's store only, and
                                   # touches no flag any other check reads
    check_proposed_remedies_are_live,  # after the flag checks: it RAISES a flag, and one
                                   # raised before them is read as phantom attention on
                                   # an alarm `true_blockers` cannot re-derive
    check_envelopes_move,          # last: it delivers, and delivery changes work orders
    check_no_lost_feedback,        # ...and after it, because that delivery is what
                                   # marks an envelope undeliverable in the first place
)


#: Invariants whose cost scales with the SETTLED BACKLOG rather than with live work.
#: `check_work_lands` walks every completed order a project has ever had, for ever, where
#: every other check here is bounded by what is currently in flight — so a mature project
#: would pay its whole history twice a minute on the reconcile cadence to answer a
#: question about work that stopped moving months ago. Hence OFF by default and their own
#: cadence, as `Daemon.PR_POLL_EVERY_TICKS` does for a related reason.
#:
#: THIS USED TO SAY "invariants that shell out", and that is no longer what they are:
#: since the landing check began judging the pull request instead of the diff it runs no
#: subprocess at all (`Daemon.refresh_landings` is where the round trip went). The
#: cadence is unchanged because the reason for it was always the population, not the
#: `git` calls. `jarvis doctor` always runs them: a human who typed the command is
#: waiting for the answer, and the answer is the point of the command.
SLOW_INVARIANTS: tuple[Callable[[ProjectStore], Iterator[Violation]], ...] = (
    check_work_lands,
)


def check_project(store: ProjectStore, repair: bool = True,
                  slow: bool = False) -> list[Violation]:
    """Run every invariant over one project. Returns the violations found.

    With `repair=False` the checks run against a read-only view of the store, so
    reporting never mutates state — that is what `jarvis doctor` uses before the user
    has decided whether to let it touch anything.

    `slow` adds `SLOW_INVARIANTS`, which read the repository rather than the database.
    """
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
    """Store proxy that swallows repairs, so `check_project(repair=False)` is a pure read.

    Only the handful of mutators the invariants above call are intercepted; everything
    else passes straight through. A `Violation` produced against this proxy still claims
    `repaired=True` — it describes the repair that *would* be applied — so callers that
    ask for no repair must present it as proposed, not done (`jarvis doctor` does).
    """

    _BLOCKED = ("flag_attention", "clear_attention", "add_assumption", "add_event",
                "set_status", "update_work_order", "supersede_approval",
                # The bus. `queue_message` is here because delivering an envelope IS a
                # queued message, and a read-only doctor run must not send one.
                "mark_envelope", "bump_envelope_attempt", "deliver_envelope",
                "queue_message", "flag_feature_attention",
                # INV-VALIDATION-STRANDED's repair. Reporting a stranded round must not
                # be the thing that ends it.
                "close_validation_round",
                # INV-FEATURE-FALSE-FAILURE's. Ditto: reporting that a feature is wrongly
                # failed must not be the thing that un-fails it.
                "set_feature_status", "clear_feature_attention")

    #: How a checker asks "am I allowed to change anything?". Needed by
    #: `check_envelopes_move`, whose repair also writes to the CENTRAL store — a proxy
    #: over the project store cannot intercept that, so the checker has to skip the work
    #: rather than have it swallowed. Absent on a real ProjectStore, so `getattr(store,
    #: "readonly", False)` is the test.
    readonly = True

    def __init__(self, store: ProjectStore):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        if name in self._BLOCKED:
            return lambda *a, **k: None
        return getattr(self._store, name)
