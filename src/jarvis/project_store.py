"""Per-project store: <project>/.jarvis/jarvis.db

Authoritative record of a project's work orders, their event timeline, the user⇄agent
message queue, the notification outbox, assumptions pending review, and the feature
orders that own work orders in sets.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import db
from .paths import project_db_path

# Work order lifecycle.
WO_STATUSES = (
    "pending",       # created, waiting for the project orchestrator to pick it up
    "dispatching",   # claimed by the daemon, worker being spawned
    "running",       # worker session active
    # A `kind='manager'` order between its feature's messages. Idle-until-messaged is
    # its designed steady state, not a question, and it used to be parked in
    # `waiting_input` — which every surface renders "Waiting on you" and which
    # `ops.waiting_on` could only explain as an unanswered permission prompt. GitHub
    # issue #264: the user cleared it twice, each nudge bought another turn saying
    # nothing was needed. A status that lies is the defect; the two `kind == 'manager'`
    # carve-outs that were paying for it are gone. See
    # docs/superpowers/specs/2026-09-16-an-idle-manager-is-not-waiting-on-you.md.
    "idle",
    "waiting_input", # worker asked something / is blocked on the user
    # The worker has claimed the job done and an independent panel is judging the
    # claim (see the validation-panel design). Ordered here rather than appended
    # because this tuple IS the order the dashboard renders status counts in, and a
    # step that happens between "waiting on the user" and "needs your review" reads
    # wrong anywhere else. Raises NO attention on its own: nobody is waiting on the
    # user while a round is open.
    "validating",
    "needs_review",  # finished but has pending assumptions or attention items
    # Worker done, PR open, waiting for the user to merge it. Deliberately NOT an
    # attention item (see invariants.true_blockers): it is a merge queue the user works
    # through in the dashboard, not a decision blocking the OS, and putting every
    # finished work order in the "NEEDS YOU" strip is how that strip stops being read.
    "waiting_pr_merge",
    # The order spent its dollar budget (`budget.EXHAUSTED`). A status of its own rather
    # than a reuse of one above, because the two candidates both say something false:
    # `failed` says the work went wrong, `needs_review` says a judgement is wanted, and
    # neither is true of an order that was doing fine and ran out of money. Ordered here
    # — after the merge queue, before the terminal three — because this tuple IS the
    # order the dashboard renders status counts in, and running out of money belongs
    # with the things that have stopped rather than with the things in flight.
    #
    # OPEN, NOT TERMINAL, and that is the whole reason the state is worth having: the
    # user raises the budget and the work carries on in the same session
    # (`ops.set_work_order_budget`). Making it terminal would paint it into the corner
    # the work order that asked for it explicitly warned against — and would also make
    # it a DEPENDENCY_DEAD_STATUS, stranding every dependent and failing the parent
    # feature over a number the user can change in one command.
    "budget_exhausted",
    "completed",
    "failed",
    "cancelled",
)
OPEN_STATUSES = ("pending", "dispatching", "running", "idle", "waiting_input",
                 "validating", "needs_review", "waiting_pr_merge", "budget_exhausted")
# Settled: nothing more will happen to these on their own. They are the bulk of an old
# project's history, so listings collapse them behind a count rather than printing them.
TERMINAL_STATUSES = ("completed", "cancelled", "failed")
# Where a PERSON may force a fresh validation round (`ops.force_validation`). AN
# ALLOWLIST, not a blocklist, and the two are not the same statement here: the question is
# not "has this settled" but "has this work order DELIVERED, and is nobody typing". An
# open round OWNS the worker's session (kn-01a4ab27) — `Daemon._reject` posts the panel's
# feedback to whatever fills the `implementor` role — so a round opened over a live worker
# gives that session two writers and moves the branch head under the seats mid-round.
# `running`, `dispatching` and `waiting_input` are live sessions; `pending` has not begun.
# Neither has anything to re-judge, and both are refused by NOT being here rather than by
# a rule that has to be kept in step with `WO_STATUSES` as it grows.
#
# `validating` is absent for a different reason and is not a silent omission: its round is
# open by definition, which is its own refusal with its own sentence.
FORCEABLE_STATUSES = ("waiting_pr_merge", "needs_review")

# The seat names a validation panel may be rostered with. This is the VOCABULARY, not
# the set whose markdown ships in a given build: a catalog may name a seat whose
# definition arrives in a later release, and that must still parse (the seat records a
# `failed` opinion at run time instead of refusing to boot the fleet). Mirrors
# `neo_store.SEATS`, which `catalog.py` already imports for exactly the same job, and
# lives beside the statuses rather than in `catalog.py` so the vocabulary sits with the
# store that records what the seats say.
VALIDATOR_SEATS = ("tester", "security", "architect", "maintainer", "chair")

# How a round ended. `pending` is a round still open; `failed` is the panel itself
# breaking (no seat answered), which is not the same as the work being `rejected`.
# `void` is a round there was nothing for a REVIEWER to judge — a staged release, whose
# every effect the OS verifies itself — and it is DERIVED from the packet before any seat
# is called, never returned by one (spec
# docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §3). It joins this
# tuple and NO OTHER below, deliberately: not `COUNTED` because nobody judged, so the
# submitter spent no round; not `RUNNABLE` or `OPEN` because it is terminal and the panel
# has finished with the unit.
VALIDATION_OUTCOMES = ("pending", "passed", "rejected", "escalated", "failed", "void")
# The outcomes that mean a round was JUDGED, and so that the submitter spent one of its
# `max_rounds`. `pending` and `failed` are deliberately absent: see
# `counted_validation_rounds`, which is the only thing that may count a round.
COUNTED_VALIDATION_OUTCOMES = ("passed", "rejected", "escalated")
# The outcomes the ROUND MACHINE still owns: `pending` is in flight, `failed` is an
# outage it retries. What `work_orders_awaiting_validation` looks for.
RUNNABLE_VALIDATION_OUTCOMES = ("pending", "failed")
# ...and the wider set that means the panel has not finished with this unit: `rejected`
# adds the wait for the submitter to come back. `escalated` is absent — the panel gave
# up and the USER holds it — and so is `passed`, which is the one that clears.
# See docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §2.
OPEN_VALIDATION_OUTCOMES = ("pending", "failed", "rejected")

# Why a `failed` round failed, on its `validation_failed` event. THREE causes and they
# want three different responses, which is the whole reason the field exists: `transport`
# retries on the next tick and gives up after `VALIDATION_OUTAGE_LIMIT`, `no_validator`
# settles the unit where it settles with the panel off, and `usage_limit` WAITS — the
# account's window is spent and the refusal named the moment it reopens.
VALIDATION_HELD_CAUSE = "usage_limit"

# ...and this one is a round waiting for GITHUB to finish running the checks. A separate
# cause because the two are told apart on every surface that reads the timeline: a usage
# window is the account being out of budget, and this is the ordinary, expected pause
# between a worker pushing and CI reporting. Same MECHANISM, and that sharing is the
# point — see `validation_hold_until`.
VALIDATION_CI_CAUSE = "ci_pending"

#: Every cause that means "this round is not failed, it is WAITING". A round closed with
#: one of these is `RUNNABLE` and uncounted, so the submitter spends no round number on
#: it and the next tick owns the same round again. ONE set, because `validation_hold_until`
#: is the only reader and a cause missing from here is a round that holds once and then
#: spins every tick for ever.
VALIDATION_HOLDING_CAUSES = frozenset({VALIDATION_HELD_CAUSE, VALIDATION_CI_CAUSE})

#: HOW A ROUND READS TO A PERSON: one word, one tone, one icon, for every
#: (outcome, hold_cause) pair. THE POINT IS THAT THERE IS ONE OF THESE — three surfaces
#: render a round (`ops.round_line`, `ui/templates/_validation.html`, and the auto-merge
#: hold sentence in `automerge.decide`) and a rule written out three times is a rule that
#: drifts in two of them (kn-432d0f19: "the display key is a function, not a ternary").
#:
#: `failed` is the entry this exists for. It is the storage word for three different
#: facts — the reviewer was unreachable, no reviewer was configured, and nothing is wrong
#: at all and the round is merely WAITING — and only `hold_cause` tells them apart. A
#: waiting round is toned `active`, not `bad`: it is the system working, and painting it
#: red is what made a green, mergeable pull request read as a failed review for the whole
#: CI window (GitHub issue #581).
#:
#: The tones are the dashboard's own vocabulary (`tone-ok`, `tone-warn`, …); the CLI and
#: the auto-merge sentence take the word alone.
VALIDATION_STANDINGS: dict[tuple[str, str | None], tuple[str, str, str]] = {
    ("passed", None): ("passed", "ok", "✓"),
    ("rejected", None): ("rejected", "warn", "◭"),
    ("escalated", None): ("escalated", "warn", "🙋"),
    # Nothing here was for a reviewer, so nobody judged and nobody is owed a decision.
    ("void", None): ("voided", "muted", "∅"),
    ("failed", None): ("failed", "bad", "✗"),
    ("failed", VALIDATION_CI_CAUSE): ("waiting for CI", "active", "◑"),
    ("failed", VALIDATION_HELD_CAUSE): ("held for the usage window", "active", "◑"),
}


def validation_standing(round_row: Any) -> tuple[str, str, str]:
    """The word, tone and icon for one round row. Never raises, never returns nothing.

    An unknown outcome falls through to the outcome itself, toned `active` — `pending`
    is the ordinary member of that branch, and a value this table has not learned yet
    must render as itself rather than as a failure.

    A `hold_cause` against any outcome but `failed` is ignored rather than trusted: every
    non-hold close clears the column, so the pair cannot occur, and rendering off a stale
    one would be this same defect one layer down.
    """
    outcome = str((round_row or {}).get("outcome") or "")
    cause = (round_row or {}).get("hold_cause") or None
    if cause not in VALIDATION_HOLDING_CAUSES:
        cause = None
    hit = (VALIDATION_STANDINGS.get((outcome, cause))
           or VALIDATION_STANDINGS.get((outcome, None)))
    return hit or (outcome, "active", "◑")


def validation_hold_until(events: Iterable[Any], round_no: int) -> float:
    """The moment this round may go again, or 0 when nothing is holding it back.

    Derived from the events and from nothing else: no column, no status, no flag — the
    rule this module states for `waiting_pr_merge` ("that earned a status because nothing
    derived it; this does not") and that `worker_session.turn_pause` already follows for
    the worker-side twin of exactly this hold.

    NEWEST WINS, which is not the same as first: a window that reopened, was retried and
    shut again writes a second event for the same round number, and taking the earlier
    moment would send the round straight back into a closed window every tick.

    TWO CAUSES HOLD, and the caller is told apart from neither: a spent usage window
    (`VALIDATION_HELD_CAUSE`) and GitHub still running the checks
    (`VALIDATION_CI_CAUSE`). They are different facts about why nobody is judging yet and
    identical in what the tick must do about it, so the vocabulary is
    `VALIDATION_HOLDING_CAUSES` and the behaviour is this one function. A round holding
    for both — a window that shut while CI was still running — takes the later moment,
    which is the same "newest wins" rule and the right one: going again before either has
    lifted is a refusal either way.

    Takes ROWS rather than a store because the same rule has to answer for a feature
    order, whose events live on its manager's timeline and come back through
    `ops.feature_events_of_kind`. One home, two carriers (GitHub issue #235).
    """
    held = 0.0
    for e in events:
        payload = db.from_json(e["payload"], {})
        if (payload.get("round") == round_no
                and payload.get("cause") in VALIDATION_HOLDING_CAUSES):
            held = max(held, float(payload.get("reopens_at") or 0.0))
    return held

# What one seat proposed. "" is a seat that offered none — it ran, but said nothing the
# arbiter can count.
VALIDATION_VERDICTS = ("pass", "reject", "")
# ...and whether that seat's opinion is usable at all.
VALIDATION_OPINION_STATUSES = ("ok", "abstained", "failed")

# How the work order entered the system. jarvis/ui follow the framework; manual is a
# direct DB insert; injected is a session the user started and then handed to Jarvis
# with `jarvis wo inject`; adhoc is the legacy marker for a session the reconciler
# adopted on its own, which it no longer does (GitHub issue 47); neo is one Neo filed
# itself (a ledger cleanup), which nobody asked for by hand — worth telling apart from
# `jarvis` in listings for exactly that reason; `schedule` is one a RECURRING JOB filed
# (src/jarvis/schedule.py), and it is here for `neo`'s reason turned up a notch — it is
# the only origin where not even a model decided to file this, a clock did, so "why am I
# paying for this work order" is unanswerable without it.
WO_ORIGINS = ("jarvis", "ui", "manual", "adhoc", "injected", "neo", "schedule")

# Origins whose session Jarvis did not dispatch: it belongs to the user, never received
# the worker briefing or `JARVIS_WO_ID`, and therefore cannot satisfy the worker contract
# (no `jarvis wo finish`, and its ending is not a failure). Holding one to that contract
# is what made every such record a permanent attention item — see INV-ADHOC-NOT-GOVERNED.
# `neo` is deliberately NOT here: Neo files the record, but the daemon dispatches it like
# any other work order, briefing and all.
UNGOVERNED_ORIGINS = ("adhoc", "injected")

# What a work order IS to the OS, which is not the same question as what it is about.
# `worker` is every work order that has ever existed: one session, one job, one pull
# request. `planner` is the session a feature order opens to decompose itself — same
# transport, same worktree, same contract, but a different briefing and a structured
# terminal action (`jarvis fo plan`) instead of a prose one.
#
# A column rather than a derivation, even though `feature_orders.plan_wo_id` already
# names the planner: `parent_id` cannot tell the two apart (a feature order's CHILDREN
# carry it too), and the briefing has to know which it is composing without querying
# back up into a second table on every dispatch.
# `manager` is a long-lived coordinator session that owns one feature order's
# follow-through: it stays open for the whole feature, receives what its children
# report and decides what happens next, instead of finishing a job and exiting.
WO_KINDS = ("worker", "planner", "manager")

# A work order with a LIVE SESSION: dispatched, running, or parked mid-conversation on
# somebody else. The per-feature cap (`claim_next_pending`, spent by
# `feature_orders.max_parallel`) and every "a turn may be in flight" reader mean this.
#
# NOT what the project-wide `max_concurrent` counts any more — see `SLOT_STATUSES` below
# and issue #134. The two were one constant until then, on the reasoning that its readers
# must agree; they are two because the readers turned out to be asking different
# questions, and the split is deliberate rather than drift.
#
# NOT what the retry sweep walks either, since issue #259 — see `RETRY_SWEEP_STATUSES`.
#
# `idle` is absent on the same reading that keeps `waiting_input` out of `SLOT_STATUSES`:
# a manager between messages has no turn in flight and draws nothing. It is in
# RETRY_SWEEP_STATUSES below regardless, because those two tuples answer different
# questions and issue #259 is the standing example of assuming they do not.
ACTIVE_STATUSES = ("dispatching", "running", "waiting_input", "validating")

# Where a paused turn may be relaunched: every status a work order can be sitting in when
# `worker_session.turn_pause` says a retry is booked (`Daemon.retry_paused_turns`, and the
# two invariants that watch it).
#
# WIDER THAN `ACTIVE_STATUSES`, and issue #259 is why. A work order that finishes behind
# a pull request, takes a message, and has that turn refused for the usage limit lands in
# `waiting_pr_merge` HOLDING A RESUMABLE, DUE PAUSE — and the sweep, scoped to the active
# set, could never see it. `invariants.MESSAGE_STUCK_STATUSES` already called that a
# defect, so the OS diagnosed the stall perfectly (`jarvis wo resume-auto`: "its retry
# came due and has not happened") and then had no loop that would ever run it. Measured
# on wo-f35e603e: three messages queued behind one lost turn, none delivered, and the
# only remedy on offer was `jarvis wo done` — abandoning the work.
#
# The two tuples below PARTITION `WO_STATUSES`, asserted by a test rather than derived
# from each other: a status added to `WO_STATUSES` fails that test until somebody decides
# which side it belongs on. That is the opposite shape from kn-32434cef's allowlist rule
# because the failure direction is inverted — there, a status silently allowed through is
# the danger; here, a status silently left OUT is a work order nothing will ever resume.
RETRY_SWEEP_STATUSES = ("dispatching", "running", "idle", "waiting_input", "validating",
                        "needs_review", "failed", "waiting_pr_merge")

# The rest of `WO_STATUSES`, and why a due retry is not run there. `pending` has no turn
# to relaunch (it has never been dispatched, so `turn_pause` reads nothing); `completed`
# and `cancelled` were ENDED BY A PERSON, and relaunching a turn under one would reopen
# work its owner closed. `failed` is not with them: the OS failed that one, the user did
# not, and recovering it is the point.
#
# `idle` is NOT here, and the direction of the mistake is why: a manager whose turn was
# refused for the usage limit settles back to `idle` HOLDING A RESUMABLE, DUE PAUSE
# (`Daemon.settle_work_order` leaves a paused turn's status alone), so a sweep that
# skipped it would strand the one work order a feature routes all its messages through.
#
# `budget_exhausted` IS here, and it is the one entry whose reason is money rather than
# ownership: relaunching that turn would spend dollars the user has not authorised, and
# the sweep cannot ask them. The relaunch is `ops.set_work_order_budget`'s, and it
# happens only once a person has raised the number.
NOT_RETRIED = ("pending", "completed", "cancelled", "budget_exhausted")

# What spends one of a project's `max_concurrent` slots: a work order whose turn is
# actually executing, plus the claim that is about to launch one (issue #134).
#
# `idle`, `waiting_input` and `validating` are OUT. All three park a work order — a
# Neo question, a gate, the panel — with no turn in flight and no tokens moving, and
# counting them meant a project capped at 5 could have one turn running and refuse to
# claim a sixth order. `dispatching` is IN even though it is transient
# (`dispatch_work_order` reaches `running` in the same call): a crash between the claim
# and the launch must not read as a free slot.
#
# Narrowing this needs the cap enforced where a parked order RESUMES as well as where one
# is claimed, or six rejected rounds all restart at once and blow past it — see
# `Daemon.deliver_messages` and `Daemon.retry_paused_turns`, which is where that is done.
SLOT_STATUSES = ("dispatching", "running")

#: What a `wo_turns` row can be. `dispatch` opens the conversation, `message` carries
#: something to the worker, and `compact` carries nothing to it at all: it is the OS
#: spending a turn on the CONVERSATION rather than on the work, summarising it before a
#: prompt re-sends a history whose cache has expired (`worker_session.compact`). A kind
#: rather than a flag on `message` because every reader that counts turns, prices them
#: or shows them has to be able to tell the three apart.
TURN_KINDS = ("dispatch", "message", "compact")

#: The one whose reply belongs to nobody. A compact turn's `result` is the CLI's, not
#: the worker's, so it is never recorded as an agent reply and never shown as one.
COMPACT_TURN = "compact"

#: Where a turn's `cost_usd` came from — see the `wo_turns.cost_source` comment. The
#: two are not the same currency: one is the CLI's own figure, the other a list-price
#: floor read off the transcript, and a surface that adds them owes the reader the
#: distinction (kn-e6bb1166's ruling).
COST_FROM_ENVELOPE = "envelope"
COST_FROM_TRANSCRIPT = "transcript"


def resume_spends_slot(wo: Mapping[str, Any]) -> bool:
    """Would starting a turn on this work order NOW take a `max_concurrent` slot it is
    not already holding?

    `count_active`'s predicate asked of one row, and negated — the question the two
    places that enforce the cap on a RESUME have (`Daemon.deliver_messages` and
    `Daemon.retry_paused_turns`), where counting the table answers the wrong thing.

    Both clauses are load-bearing and both are easy to drop. A manager is exempt from
    the count, so holding its message against a cap it does not contribute to would
    strand a feature's coordinator behind its own children. A work order already in
    `SLOT_STATUSES` is holding the slot its next turn runs in — most of all the one
    parked on a usage limit, which stays `running` with no turn in flight — so charging
    it again would refuse to resume the very order the cap is counting.
    """
    return wo.get("kind") != "manager" and wo["status"] not in SLOT_STATUSES

# -- the message bus (see bus.py, and the validation-panel design doc) ----------------
#
# Nothing addresses anything directly: a cross-entity message is an envelope posted to a
# ROLE, and the router works out who fills it. These three tuples are that vocabulary.
#
# Module tuples, NOT SQL CHECK constraints, and deliberately so. No status column in this
# codebase carries a CHECK; every one of them is a tuple here plus an `assert` at the
# write site. More to the point, ENVELOPE_ROLES and ENVELOPE_KINDS are DESIGNED TO GROW —
# the whole justification for a bus is that a new participant costs a routing rule — and a
# CHECK on `to_role` would turn adding a role into a schema migration, which is exactly
# the cost this design exists to avoid.
ENVELOPE_ROLES = ("reviewer", "implementor", "manager", "reconciler")
ENVELOPE_KINDS = ("review_feedback", "deferral_request", "children_landed")
# queued -> delivered (a work order filled the role and was sent the message)
#        -> handled_by_router (nobody filled it and the router acted itself)
#        -> undeliverable (nobody filled it and nobody could act — see bus.deliver)
ENVELOPE_STATES = ("queued", "delivered", "handled_by_router", "undeliverable")

# Feature order lifecycle. Deliberately NOT a copy of WO_STATUSES: a feature order never
# runs a session of its own, so most of a work order's states are meaningless for it.
FO_STATUSES = (
    "pending",      # created; the planner has not been dispatched
    "planning",     # the plan work order is running
    "plan_review",  # a plan was submitted; Neo is reviewing it, or it is escalated
    "executing",    # children dispatching / running
    "validating",   # every child is done and the panel is judging the feature as a whole
    "completed",    # every child settled successfully
    # The FAMILY budget is gone — planner, manager and children together have spent
    # `feature_orders.budget_usd`. Open, for the same reason the work-order status is:
    # `jarvis fo budget <id> <amount>` puts the feature back to work.
    #
    # AFTER `completed` AND NOT BEFORE IT, which is the one place it differs from the
    # work-order tuple above. This tuple is also a render order, and its happy path —
    # `executing`, `validating`, `completed` — is CONTIGUOUS and asserted as such
    # (tests/test_stores.py). Running out of money is a stop rather than a stage of that
    # pipeline, so it sits with the other endings instead of interrupting them.
    "budget_exhausted",
    "failed",       # a child failed or was cancelled
    "cancelled",    # the user stopped it
)
FO_OPEN_STATUSES = ("pending", "planning", "plan_review", "executing", "validating",
                    "budget_exhausted")
FO_TERMINAL_STATUSES = ("completed", "failed", "cancelled")

# Work-order metadata key: this work order was authorised by whoever filed it, so the
# worker must not spend a round trip asking whether it may do the thing it was sent to
# do. Value: {"by": "neo", "scope": "<what is pre-approved, in words>", ...}.
PRE_APPROVED_KEY = "pre_approved"

# Feature-order metadata key: children whose failure the user has already answered for
# with `jarvis fo resume`. Value: [{"wo_id": str, "ts": float, "note": str}] — the flat
# list of ids AND the record of why, in one key, because the two must never disagree.
#
# In `feature_orders.metadata` rather than on a work order or a feature event. The event
# path was the obvious home and cannot carry it: `ops.feature_event` returns False when a
# feature has no project manager order, which is every feature planned while
# `os.validation.enabled` was false. See docs/superpowers/specs/2026-08-29-feature-order-resume.md.
SUPERSEDED_CHILDREN_KEY = "superseded_children"

# -- the alarm's vocabulary ---------------------------------------------------------
#
# In the STORE because four surfaces have to agree on it and none of them may depend on
# another: the daemon raises, the supervisor judges, Neo answers and the timeline
# renders. §1 of docs/superpowers/specs/2026-08-31-the-supervisor.md freezes all four.
#
# Every value later sections write is declared HERE, in one pass. §1 of
# docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md: two sections
# editing the same tuple is a conflict for no reason; one section declaring them all is
# free.

ALARM_STATUSES = (
    "raised",     # on the supervisor's queue, awaiting a look
    "reviewing",  # claimed by a supervisor tick
    "acked",      # judged and answered with a note to the user
    "escalated",  # judged and handed to Neo
    "proposed",   # judged, and a remedy is waiting on a gate grant (§5, health spec)
    "skipped",    # never offered to the supervisor — backfilled history, or declined
    "failed",     # the review could not be completed; the alarm stays unresolved
)
# The supervisor reads, reports, and — from §5 — may ASK to act. It still never acts:
# `propose` requests a gate grant, and applying the remedy is `remedies.py`'s alone.
ALARM_VERDICTS = ("ack", "escalate", "propose")
ALARM_REVIEW_STATUSES = ("unreviewed", "approved", "corrected")

# What an alarm is ABOUT, and what noticed it. Both columns default, so every row
# written before the health spec reads back as exactly what it was — a cost alarm about
# a work order — without a backfill.
ALARM_SUBJECTS = ("work_order", "feature_order")
ALARM_SOURCES = ("cost", "health")

# How one health sweep ended (`health_reviews.outcome`, §4). `clear` and `failed` are
# not the same answer and the difference is the whole fail-safe: `clear` is the model
# saying it found nothing, `failed` is nobody having judged at all — which is why a
# `failed` sweep does not record its fingerprint as reviewed and the next tick retries.
HEALTH_OUTCOMES = ("clear", "findings", "failed")

# How long an alarm held back by a transport failure waits before it may be claimed
# again, indexed by attempts already spent. `neo_store.RETRY_BACKOFF_SECONDS`' reasoning:
# an un-delayed re-queue is not a retry, it is the same failure three times in a row.
ALARM_RETRY_BACKOFF_SECONDS = (60.0, 300.0, 900.0)

# How long a queued message held back by a delivery failure waits, same ladder and same
# reason. `Daemon.deliver_messages` runs every tick.
MESSAGE_RETRY_BACKOFF_SECONDS = ALARM_RETRY_BACKOFF_SECONDS

# How many times a message may fail to reach its worker before the user is told it is
# not getting through. GitHub issue 43: a message that vanishes must not look delivered.
MAX_MESSAGE_DELIVERY_ATTEMPTS = 3

# How many times launching a work order's first turn may fail on the transport before
# the order is given up on, and how long it waits between tries.
MAX_DISPATCH_ATTEMPTS = 3
DISPATCH_RETRY_BACKOFF_SECONDS = ALARM_RETRY_BACKOFF_SECONDS

# A subject-level finding judges the unit rather than a turn, so it has no `seq` to
# carry — and `wo_alarms.seq` is NOT NULL, which `ALTER TABLE ADD COLUMN` cannot relax.
# This is the sentinel that fills it, and every surface printing a turn number renders
# it as "no turn": `turn -1` reaching the user is the failure this constant names.
NO_TURN = -1

# The `wo_events` kinds that carry an alarm's life, and their payloads:
#
#   cost_alarm       {kind, seq, reason, alarm_id}   the raise (daemon)
#   alarm_reviewed   {alarm_id, verdict, reason, note}
#   alarm_escalated  {alarm_id, neo_question_id}
#   alarm_advice     {alarm_id, neo_question_id, answer}
#   health_finding   {alarm_id, probe, subject_kind, subject_id, reason}      (§4)
#   health_reviewed  {subject_kind, subject_id, trigger, findings}            (§4)
#   remedy_proposed  {alarm_id, approval_id, remedy, argument}                (§5)
#   remedy_applied   {alarm_id, approval_id, remedy, result}                  (§5)
#   remedy_refused   {alarm_id, approval_id, remedy, reason}                  (§5)
#
# Duplicated by `timeline.ALARM_KINDS`, which is a leaf and may not import a store; a
# test asserts the two are equal, because a kind in only one of them means every deep
# link on §6's page stops resolving with no error anywhere.
#
# `cost_alarm`'s first three keys are UNCHANGED and load-bearing: they are the dedupe
# memory that makes it one alarm per turn per kind (see `Daemon.check_burning_turns`).
ALARM_EVENT_KINDS = ("cost_alarm", "alarm_reviewed", "alarm_escalated", "alarm_advice",
                     "health_finding", "health_reviewed",
                     "remedy_proposed", "remedy_applied", "remedy_refused")

# HOW A WORK ORDER CAN BE LINKED TO A TRACKER ISSUE, weakest first — the order IS the
# precedence, and `link_issue` never demotes. The admitting set is deliberately small
# (Neo, question 409): a count that admits passing mentions is not a priority signal.
#
#   cited     — the order's brief names the issue, by URL or by `#N` on the project's own
#               repository. Someone wrote it down on purpose.
#   assigned  — the order exists to FIX the issue (`work_orders.issue_url`).
#   raised    — the validation panel filed the issue out of this order's own review.
ISSUE_LINK_KINDS = ("cited", "assigned", "raised")

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    origin TEXT NOT NULL DEFAULT 'manual',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    model TEXT,
    effort TEXT,
    permission_mode TEXT,
    append_system_prompt TEXT,
    session_id TEXT,
    bg_id TEXT,
    -- LEGACY (background-session transport), no longer written; see wo_turns. A
    -- non-NULL job_id is now only a marker that this work order predates headless
    -- turns, which is what tells worker_session to release its background agent
    -- before the next turn resumes.
    job_id TEXT,
    reply_job_id TEXT,
    worktree TEXT,
    branch TEXT,
    needs_attention INTEGER NOT NULL DEFAULT 0,
    attention_reason TEXT,
    result_summary TEXT,
    backlog_id TEXT,
    metadata TEXT
);
-- A planned unit of work above the work order: the coarse ask the user actually has,
-- which the project plans into a dependency-ordered set of ordinary work orders before
-- any of them runs. Its children are `work_orders` rows carrying `parent_id`.
--
-- Per-project rather than central (where the backlog lives) because a feature order is
-- scoped to one project by construction, and keeping the parent in the same database as
-- its children buys one transaction, real foreign keys, and one query for a listing.
CREATE TABLE IF NOT EXISTS feature_orders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    origin TEXT NOT NULL DEFAULT 'jarvis',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    plan_wo_id TEXT REFERENCES work_orders(id),   -- the planner
    plan TEXT,                              -- the submitted plan, as JSON
    -- The Neo question reviewing the submitted plan. The back-link lives here rather
    -- than a `fo_id` on the question, mirroring `approvals.neo_question_id`: Neo's
    -- database is OS-wide and knows nothing about a project's tables.
    plan_question_id INTEGER,
    max_parallel INTEGER,                   -- slot cap for this feature's children (Phase 3)
    -- The commit the feature's work is measured against: what the repository looked
    -- like before any child ran, so a whole-feature diff can be taken later. Nullable,
    -- and nothing writes it yet.
    base_sha TEXT,
    needs_attention INTEGER NOT NULL DEFAULT 0,
    attention_reason TEXT,
    backlog_id TEXT,
    metadata TEXT
);
CREATE TABLE IF NOT EXISTS wo_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT
);
-- One cost alarm, with an identity. The `cost_alarm` event is still written and is
-- still the raise's dedupe memory; this is the object a supervisor claims, a verdict
-- attaches to and a URL can point at, none of which an event row can carry.
--
-- IN THIS DATABASE AND NOT `neo.db`, where Neo's questions live: an alarm is unreadable
-- without its work order's title, status, hidden and attention flags, and those are
-- `work_orders` columns here. `questions.wo_id` is a loose string with no foreign key,
-- so the fleet-wide read would keep its per-project fan-out AND gain a second database
-- — and the cascade below would become hand-maintained cleanup, which `neo_store` is
-- the standing evidence this OS gets wrong.
--
-- Everything past `reason` is written by later sections of
-- docs/superpowers/specs/2026-08-31-the-supervisor.md and is NULL/default until then.
CREATE TABLE IF NOT EXISTS wo_alarms (
    id TEXT PRIMARY KEY,                    -- 'al-' + db.new_id, like wo-/fo-
    wo_id TEXT NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,                     -- inspection's alarm kinds
    seq INTEGER NOT NULL,                   -- the turn it judged
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'raised',  -- ALARM_STATUSES
    claimed_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    verdict TEXT,                           -- ALARM_VERDICTS
    verdict_reason TEXT,
    note TEXT,                              -- what the user is told, in words
    decided_at REAL,
    neo_question_id INTEGER,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',  -- ALARM_REVIEW_STATUSES
    review_feedback TEXT,
    reviewed_at REAL,
    -- The remedy the supervisor proposed, its argument, and the `self_heal` approval
    -- that must be granted before `remedies.apply` may run it. Also in ADDED_COLUMNS:
    -- this table already ships, so a live database gets them only there.
    remedy TEXT,                            -- a key of remedies.REMEDIES
    remedy_argument TEXT,                   -- what to say, or why, in the judge's words
    remedy_approval_id INTEGER
);
-- THE LEDGER OF LOOKING, and it is deliberately NOT `wo_alarms` (§4 of
-- docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md). A sweep that
-- found nothing is the common case; writing it as an alarm would fill `alarms_across`,
-- `list_cost_alarms`, /alarms and `jarvis wo show`'s alarm line with rows that say
-- nothing, and every surface would then need the same filter.
--
-- No foreign key: the subject is either a work order or a feature order and neither can
-- carry a cascade for both, so `delete_work_order` deletes these rows itself. No
-- migration either — the table is new, and `CREATE TABLE IF NOT EXISTS` is what every
-- open already runs.
--
-- `fingerprint` is the DEDUPE MEMORY as well as the trigger's input: a finding is not
-- re-raised for a (subject, probe) pair already reported at the same fingerprint. That
-- is why `detail` carries the probe ids a sweep REPORTED (comma-separated) when
-- `outcome='findings'`, and the failure's reason when `outcome='failed'`.
CREATE TABLE IF NOT EXISTS health_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    subject_kind TEXT NOT NULL,         -- ALARM_SUBJECTS
    subject_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,          -- health.fingerprint
    trigger TEXT NOT NULL,              -- health.TRIGGERS
    outcome TEXT NOT NULL,              -- HEALTH_OUTCOMES
    findings INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT ''
);
-- THE SCHEDULER'S CLOCK, one row per job this project runs
-- (docs/superpowers/specs/2026-09-14-the-scheduler.md §3). In the PROJECT store rather
-- than `os_state`, because what a firing produces is a project work order and two
-- projects sharing one clock would mean the first to fire silenced the rest.
--
-- `last_fired_at` IS THE ANCHOR AND IT IS NEVER NULL: seeded to the moment the job was
-- first seen enabled, so neither enabling a job nor restarting the daemon can make one
-- due. `last_wo_id IS NULL` is what tells a seeded job apart from one that has fired.
--
-- The held pair is the other half of Neo's ruling on holding silently: a job parked
-- behind its own unsettled order raises no attention, but the park is RECORDED, so
-- `jarvis doctor` can tell a scheduler that is waiting from one that is dead
-- (`invariants.check_schedule_progresses`). New table, so no migration — the
-- `CREATE TABLE IF NOT EXISTS` every open already runs is the whole upgrade.
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,            -- schedule.JOB_IDS
    created_at REAL NOT NULL,
    last_fired_at REAL NOT NULL,
    last_wo_id TEXT,                    -- no FK: the order may be deleted, the clock stays
    held_since REAL,
    held_reason TEXT
);
CREATE TABLE IF NOT EXISTS wo_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    direction TEXT NOT NULL,            -- user_to_agent | agent_to_user
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'jarvis',  -- jarvis | ui | direct
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | delivered | failed
    delivered_at REAL,
    authored_by TEXT NOT NULL DEFAULT ''    -- MESSAGE_AUTHOR_USER, or '' for unknown
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL DEFAULT 'info', -- info | warning | critical
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    wo_id TEXT,
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new'  -- new | routed
);
-- One invariant violation the OS has already announced, and the only thing that makes
-- "report once, not every tick" (invariants.py rule 3) survive a restart. The dedupe
-- used to be a set on the Daemon object, so every release re-announced the whole
-- standing unrepaired set to Telegram — noise that scaled with release cadence (wo-31bb26ff).
CREATE TABLE IF NOT EXISTS violation_reports (
    invariant TEXT NOT NULL,
    -- '' rather than NULL for a violation that carries no work order (this is the
    -- INV-HEALTH-SWEEP-MUTE case): NULLs are DISTINCT under a SQLite primary key, so a
    -- nullable column here would open a fresh row — and fire afresh — every tick.
    wo_id TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    seen INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (invariant, wo_id)
);
CREATE TABLE IF NOT EXISTS assumptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' -- pending | accepted | rejected
    -- WHO settled it, why, under which model and configuration, and the Neo question
    -- that ruled. All in ADDED_COLUMNS, where the reasoning is — this table already
    -- ships, so a live database gets them only there. So are the PROVISIONAL verdict
    -- and the OBJECTION columns (§4.2 of the spec named in ADDED_COLUMNS), for the
    -- same reason.
);
-- One judging round over one working unit — a work order or a feature order — by the
-- validation panel. ONE table for both, not two: the two loops record identical facts,
-- and two tables would mean two of every reader, two renderers, and two chances to
-- disagree about what a round is.
--
-- TWO NULLABLE FOREIGN KEYS rather than one polymorphic `subject_id`: a single id column
-- cannot carry ON DELETE CASCADE, so deleting a work order would strand its rounds. The
-- CHECK is what keeps exactly one of them set.
CREATE TABLE IF NOT EXISTS validation_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT REFERENCES work_orders(id) ON DELETE CASCADE,
    fo_id TEXT REFERENCES feature_orders(id) ON DELETE CASCADE,
    round INTEGER NOT NULL,             -- 1-based, per subject
    ts REAL NOT NULL,
    fingerprint TEXT NOT NULL,          -- what was judged; a repeat means no new evidence
    summary TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    pr_url TEXT,
    -- pending | passed | rejected | escalated | failed
    outcome TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',    -- what was sent back
    -- Which COMMIT was judged (the PR's headRefOid), '' when nothing binds this verdict
    -- to one. Also in ADDED_COLUMNS, where the reasoning is — this table already ships,
    -- so a live database gets it only there.
    head_sha TEXT NOT NULL DEFAULT '',
    -- Which configuration judged it (`os_config_versions.id`). Also in ADDED_COLUMNS —
    -- this table already ships, so a live database gets it only there.
    config_version TEXT,
    -- Why a PERSON opened this round by hand (`jarvis validation force`). '' is the
    -- ordinary round, opened by a submission. Also in ADDED_COLUMNS, where the
    -- reasoning is.
    forced_reason TEXT NOT NULL DEFAULT '',
    -- WHY this round is `failed` when nothing failed: a `VALIDATION_HOLDING_CAUSES`
    -- token, NULL on every other close. Also in ADDED_COLUMNS, where the reasoning is.
    hold_cause TEXT,
    -- The commit this verdict has been CARRIED FORWARD to, and why, when the OS merged
    -- the base into the branch and moved the head itself. Separate from `head_sha`,
    -- which keeps meaning "what the seats read". Also in ADDED_COLUMNS, where the
    -- reasoning is — this table already ships, so a live database gets it only there.
    carried_head_sha TEXT NOT NULL DEFAULT '',
    carried_reason TEXT NOT NULL DEFAULT '',
    -- Per-file digests of the diff this round judged, as JSON. Also in ADDED_COLUMNS,
    -- where the reasoning is.
    file_shas TEXT NOT NULL DEFAULT '',
    CHECK ((wo_id IS NULL) <> (fo_id IS NULL))
);
-- PARTIAL unique indexes, NOT `UNIQUE (wo_id, fo_id, round)`. SQLite treats NULLs as
-- distinct in a UNIQUE constraint, and every row here has a NULL in one of the two id
-- columns — so the three-column form would look perfectly correct and enforce NOTHING
-- AT ALL on either loop, letting duplicate rounds insert on both.
CREATE UNIQUE INDEX IF NOT EXISTS validation_rounds_wo
    ON validation_rounds(wo_id, round) WHERE wo_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS validation_rounds_fo
    ON validation_rounds(fo_id, round) WHERE fo_id IS NOT NULL;
-- What one seat said in one round. Keyed on the ROUND, not on the subject: the nearest
-- precedent (`neo_store.panel_opinions`) keys on the question because a Neo question has
-- exactly one round of deliberation, whereas a validation has up to `max_rounds` — so
-- `UNIQUE (subject, seat)` here would silently overwrite round one's opinions with round
-- two's and leave no trace of either.
CREATE TABLE IF NOT EXISTS validation_opinions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES validation_rounds(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    seat TEXT NOT NULL,                 -- VALIDATOR_SEATS
    reply TEXT NOT NULL DEFAULT '',     -- the seat's raw reply, verbatim
    verdict TEXT NOT NULL DEFAULT '',   -- pass | reject | '' (none offered)
    status TEXT NOT NULL DEFAULT 'ok',  -- ok | abstained | failed
    model TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    UNIQUE (round_id, seat)
);
-- Requests to perform a privileged action (merge a PR, cut a release). Filed by the
-- PreToolUse gate when a worker attempts one, reviewed by Neo, and consumed by the
-- retry. A row is a receipt for one command, not a capability: see gates.py.
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL,                     -- gates.KIND_NAMES
    command TEXT NOT NULL,                  -- the exact string the grant authorises
    matched TEXT NOT NULL DEFAULT '',       -- the recogniser that fired
    justification TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    -- awaiting_case | pending | approved | denied | dismissed | expired.
    -- `awaiting_case` is filed-but-unargued: the worker ran the command instead of
    -- asking, so no Neo question exists yet and nothing in the OS acts on the row until
    -- the worker makes its case. It is the ONLY status no reviewer can see, which is why
    -- it is also the only one with a TTL that abandons it — see gates.AWAITING_CASE.
    -- `dismissed` is not a verdict on a privileged action, it is a verdict on the
    -- CLASSIFIER: the command never performed one and the gate matched it by mistake.
    -- It clears the command like an approval does but records no authorisation, and it
    -- is the only decided status that never expires — see gates.py.
    status TEXT NOT NULL DEFAULT 'pending',
    -- Neo declined to decide, so the request is still pending but it is now the USER
    -- who holds it. This is the bit that decides whether a gate costs the user any
    -- attention: pending-with-Neo must stay silent, pending-with-user must not.
    escalated INTEGER NOT NULL DEFAULT 0,
    escalation_reason TEXT,
    neo_question_id INTEGER,
    decided_by TEXT,                        -- neo | user
    decision_reason TEXT,
    decided_at REAL,
    expires_at REAL,
    uses INTEGER NOT NULL DEFAULT 0,
    max_uses INTEGER NOT NULL DEFAULT 3
);
-- One turn of a worker's conversation: a `claude -p` process Jarvis started, and what
-- it said back. The work order's conversation IS this table in order of `seq`.
--
-- Replaces the old job_id/reply_job_id pair, which tracked a background job through the
-- Claude supervisor's private state file. Owning the record outright is what makes reply
-- capture a field read instead of a retry loop over someone else's internals.
CREATE TABLE IF NOT EXISTS wo_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    seq INTEGER NOT NULL,                   -- 1-based position in the conversation
    kind TEXT NOT NULL,                     -- dispatch | message
    msg_id INTEGER,                         -- the wo_messages row that triggered it
    prompt TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'running',  -- running | done | failed
    pid INTEGER,
    -- The transient systemd unit the turn runs in, when it got one (systemd_units).
    -- NULL is the direct-Popen transport: a dev checkout, `start --foreground`, a host
    -- without systemd, or a spawn that fell back. Recorded rather than re-derived from
    -- the (wo, seq) naming convention, because `cancel` has to stop the unit that
    -- actually exists.
    unit TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    exit_code INTEGER,
    result TEXT,                            -- the turn's final assistant message
    error TEXT,
    cost_usd REAL,
    -- WHICH READING WROTE `cost_usd`. 'envelope' is the CLI's own `total_cost_usd`;
    -- 'transcript' is `usage.cost_between`, the floor derived from the session
    -- transcript for a turn whose envelope never arrived (issue #471). NULL alongside
    -- a cost is a row written before this column existed, and reads as 'envelope' —
    -- the only source there was. NULL with no cost is a turn that spent nothing on
    -- record.
    cost_source TEXT,
    num_turns INTEGER,
    -- The turn's exact accounting, compacted from the result JSON the `claude` CLI
    -- wrote to `outfile` (see claude_cli.derive_turn_usage): cost, tokens by class
    -- with the ephemeral 1h/5m split, per-API-call context peak, context window.
    -- The outfile stays the source of truth; this is the copy that outlives it.
    -- NULL means "not recorded" — a turn reaped before this column existed (readers
    -- lazily backfill it from the outfile while that survives) — never zero spend.
    usage_json TEXT,
    outfile TEXT NOT NULL DEFAULT '',
    errfile TEXT NOT NULL DEFAULT ''
);
-- THE MESSAGE BUS. One row is one message posted to a ROLE about a SUBJECT (a work
-- order or a feature order), delivered by the router in bus.py. The sender never names
-- a recipient and never learns who read it.
--
-- `delivered_wo_id` is written by the ROUTER and never by the sender. It is the only
-- record of who read an envelope, and a sender able to set it would be a sender coupled
-- to its recipient — the coupling this whole design exists to prevent.
--
-- The one CHECK is different in kind from the vocabulary tuples above: exactly-one-of-two
-- parents is a structural invariant that never changes and that no Python writer can be
-- trusted to re-derive.
CREATE TABLE IF NOT EXISTS envelopes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    subject_wo_id TEXT REFERENCES work_orders(id) ON DELETE CASCADE,
    subject_fo_id TEXT REFERENCES feature_orders(id) ON DELETE CASCADE,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'queued',
    delivered_wo_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    -- WHICH `wo_messages` ROW THIS ENVELOPE BECAME. In ADDED_COLUMNS, where the
    -- reasoning is — this table already ships.
    CHECK ((subject_wo_id IS NULL) <> (subject_fo_id IS NULL))
);
-- A TRACKER ISSUE THIS PROJECT IS LINKED TO, and what the OS last saw of it. Cached
-- rather than fetched, for `ops.filed_follow_ups`'s reason: a surface that asked GitHub
-- what state an issue is in would make `jarvis wo show` fail when the tracker is
-- unreachable. `state` is "" until the sweep has looked once.
CREATE TABLE IF NOT EXISTS tracked_issues (
    issue_url TEXT PRIMARY KEY,
    number INTEGER NOT NULL DEFAULT 0,
    repo TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    checked_at REAL,
    -- The count last written to the issue as its `referenced: N` label. -1 means never,
    -- which is not the same claim as 0 and is why the default is not 0.
    refs_labelled INTEGER NOT NULL DEFAULT -1
);
-- THE RELATION THE ISSUE BODY USED TO CARRY IN PROSE ONLY. One row per (issue, unit),
-- so the reference COUNT is a count of distinct work orders — which is the signal the
-- user asked for — and an order that both raised and cites an issue counts once.
CREATE TABLE IF NOT EXISTS issue_links (
    issue_url TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 0,
    seat TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    -- When the comment naming this reference landed on the issue. NULL until the sweep
    -- has said it out loud; the `raised` link is stamped at creation because the filing
    -- already writes that sentence into the body.
    announced_at REAL,
    PRIMARY KEY (issue_url, unit_id)
);
-- A FOLLOW-UP THE TRACKER MAY NOT CARRY. When the OS cannot establish the project's
-- repository is private, a seat's words are not published (spec §9) — and an issue with
-- the text withheld carries nothing a reader can act on, so none is opened at all. The
-- finding lives here instead, in full, and the surfaces mark it internal-only.
-- UNIQUE on (digest, unit_id): the same dedupe key the tracker half uses, so a repeat
-- settle files nothing twice.
CREATE TABLE IF NOT EXISTS internal_follow_ups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 0,
    seat TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    failure TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE (digest, unit_id)
);
CREATE INDEX IF NOT EXISTS idx_internal_follow_ups_unit ON internal_follow_ups(unit_id);
CREATE INDEX IF NOT EXISTS idx_issue_links_unit ON issue_links(unit_id);
CREATE INDEX IF NOT EXISTS idx_turns_wo ON wo_turns(wo_id, seq);
CREATE INDEX IF NOT EXISTS idx_turns_state ON wo_turns(state);
CREATE INDEX IF NOT EXISTS idx_wo_status ON work_orders(status);
CREATE INDEX IF NOT EXISTS idx_events_wo ON wo_events(wo_id);
-- Both readers of this index run per project: `events_across` on every alarm surface,
-- and the alarm backfill's guard on EVERY ProjectStore open, which is every CLI
-- invocation. Neither had one before and both were full scans of the busiest table.
CREATE INDEX IF NOT EXISTS idx_events_kind ON wo_events(kind);
CREATE INDEX IF NOT EXISTS idx_health_subject ON health_reviews(subject_kind, subject_id, ts);
CREATE INDEX IF NOT EXISTS idx_alarms_wo ON wo_alarms(wo_id, ts);
CREATE INDEX IF NOT EXISTS idx_alarms_status ON wo_alarms(status);
CREATE INDEX IF NOT EXISTS idx_msgs_status ON wo_messages(status);
CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_approvals_wo ON approvals(wo_id, status);
CREATE INDEX IF NOT EXISTS idx_fo_status ON feature_orders(status);
CREATE INDEX IF NOT EXISTS idx_validation_opinions ON validation_opinions(round_id);
CREATE INDEX IF NOT EXISTS idx_envelopes_state ON envelopes(state, id);
CREATE INDEX IF NOT EXISTS idx_envelopes_subject ON envelopes(subject_wo_id, subject_fo_id);
"""

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a no-op on an
# existing database, so new columns must be ALTERed in on open.
ADDED_COLUMNS = {
    "work_orders": {
        # How many times launching this order's first turn has failed on the transport,
        # and when it may next be claimed. A dispatch that could not reach `claude` used
        # to set `failed` at attempts=0, which is a terminal state derived from a blip —
        # spec docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §5. NULL /
        # 0 on every pre-existing row, which reads as "never failed, claimable now".
        "dispatch_attempts": "INTEGER NOT NULL DEFAULT 0",
        "retry_after": "REAL",
        # THE DOLLAR CEILING THE USER SET on this one order, or NULL for no ceiling —
        # which is what every row written before this existed says, and what the OS does
        # by default. See src/jarvis/budget.py for what the number governs (the order's
        # whole bill, worker turns plus Jarvis's own calls on it) and how it is enforced.
        #
        # Stamped at creation from the catalog default when there is one, rather than
        # re-read from the catalog on every turn the way `autocompact_window` is. The
        # opposite choice on purpose: a budget is a contract about ONE order, `jarvis wo
        # show` has to be able to state it, and lowering a fleet default must not
        # silently strand work the user already authorised at the old number.
        "budget_usd": "REAL",
        # ...and the slice this child's FEATURE reserved for it at dispatch — see the
        # reserve-on-dispatch note in budget.py. NULL for every standalone work order and
        # for a child whose feature has no budget. The two columns are separate, and the
        # tighter of them wins (`budget.ceiling`), because "what the user typed" and
        # "what the family lent it" are different claims and an escalation has to name
        # which one ran out.
        "budget_reserved_usd": "REAL",
        "job_id": "TEXT",
        "reply_job_id": "TEXT",
        # Hidden orders stay on the record but stop competing for the user's attention:
        # out of listings, out of the summary, and never dispatched.
        "hidden": "INTEGER NOT NULL DEFAULT 0",
        # Blockers the user has explicitly seen and dismissed (JSON list). Attention is
        # re-derived from state on every reconcile tick, so clearing the flag alone does
        # not stick — the tick puts it straight back. This is what makes an ack hold:
        # `true_blockers` subtracts these, and only these, so a *new* blocker still
        # surfaces. Cleared whenever the flag legitimately drops (the ack is spent).
        "acknowledged_blockers": "TEXT",
        # LEGACY, no longer written. Under the old background-session transport every
        # delivered turn forked a fresh session id, so a work order accumulated a trail
        # of spent ones and needed this to stop its binding walking backwards. Headless
        # turns reuse one Jarvis-minted id for the work order's whole life, so there is
        # no trail to keep. Retained because old rows still carry their history.
        "prior_sessions": "TEXT",
        # The pull request this work order is waiting on, as reported by the worker via
        # `jarvis wo finish --pr`. Its presence is what puts the work order in
        # `waiting_pr_merge` rather than `completed`, and it is the link the user
        # follows from the dashboard to go and merge.
        "pr_url": "TEXT",
        # The last state the daemon read back from GitHub for `pr_url` (OPEN, MERGED or
        # CLOSED), written by `Daemon.poll_pull_requests`. CLOSED is the load-bearing
        # one: it is what tells `invariants.true_blockers` that a `needs_review` work
        # order is there because the pull request was shut without merging, rather than
        # because a worker went idle. Absent means "never polled".
        "pr_state": "TEXT",
        # Work orders that must finish before this one may be claimed (JSON list of
        # work-order ids). Deliberately NOT a `blocked` status: this codebase's statuses
        # are load-bearing — OPEN_STATUSES, TERMINAL_STATUSES, true_blockers and the
        # settle path all switch on them — and "blocked" is fully derivable from this
        # column plus the dependencies' statuses, so storing it would only invite drift.
        # `waiting_pr_merge` earned a status because nothing derived it; this does not.
        # Shape matches `backlog.depends_on` exactly, so the two read the same way.
        "depends_on": "TEXT NOT NULL DEFAULT '[]'",
        # The feature order this work order belongs to — its planner or one of its
        # children. NULL for a standalone work order, which is nearly all of them, and
        # the reason this whole migration is invisible to a project that never creates a
        # feature order. `ALTER TABLE ADD COLUMN` may carry a REFERENCES clause only
        # while the column defaults to NULL, which it does.
        "parent_id": "TEXT REFERENCES feature_orders(id)",
        # `worker` or `planner` — see WO_KINDS.
        "kind": "TEXT NOT NULL DEFAULT 'worker'",
        # Which section of the parent feature's spec this child implements, as the plan
        # named it (a heading number or its text). NULL for every standalone work order
        # and for the planner and manager, which own the whole feature rather than a
        # piece of it. Three readers, which is why it is a column and not re-derived from
        # the plan at each of them: the worker's prompt, the section file materialised
        # beside it, and the validation panel's evidence packet. See §1.2 of
        # docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md.
        "spec_section": "TEXT",
        # THE SEALED BILL (`bill.build`), written once the order reaches a terminal
        # status and never recomputed. The sources a bill is built from all expire:
        # Claude Code prunes session transcripts and result JSONs on its own schedule,
        # so an order costed on demand quietly SHRINKS as its evidence ages. Sealing it
        # at completion is what makes "what did this cost" answerable a year later.
        # NULL means not sealed yet — an open order, or one that completed before this
        # column existed — and those are costed live, with the shortfall named.
        "bill_json": "TEXT",
        "bill_sealed_at": "REAL",
        # The configuration in force when this work order was DISPATCHED — the id of a
        # row in `os_config_versions`. Frozen there beside model/effort/permission_mode
        # and for the same reason (dispatch.py), and NULL carries the same honesty as
        # `pr_state`: "ran before the console existed", never version 1. See
        # docs/superpowers/specs/2026-08-27-the-config-console.md §5.
        "config_version": "TEXT",
        # The tracker issue this work order exists to fix, when the OS filed it itself
        # (`jarvis bug report`). NULL for every work order a human or a planner created,
        # which is nearly all of them, and the reason `Daemon.sync_issues` is a single
        # indexed query that usually returns nothing.
        "issue_url": "TEXT",
        # The priority the bug was settled at — one of `issues.PRIORITIES`. Only ever
        # `critical` or `blocker` on a work order, because those are the only two that
        # become one. Here rather than re-read from the tracker because the thing that
        # needs it is the release trigger, which runs offline and must not depend on a
        # label a human may have edited.
        "issue_priority": "TEXT",
        # The tracker state the OS last SUCCESSFULLY applied to `issue_url` — one of
        # `issues.IN_PROGRESS`, `issues.RELEASED`, `issues.CLOSED`. The pair is
        # `pr_url`/`pr_state`'s shape with the arrow reversed: `pr_state` caches what
        # GitHub told us, this caches what we told GitHub. Comparing it against
        # `issues.desired_state` is what makes the sweep free while they agree and a
        # retry when `gh` was unreachable — see the `issues` module docstring.
        "issue_state": "TEXT",
    },
    "feature_orders": {
        # Same, one level up: a feature's bill is its children's, and children can be
        # deleted. See the work_orders comment.
        "bill_json": "TEXT",
        "bill_sealed_at": "REAL",
        # See the CREATE TABLE comment. A live database already has `feature_orders`,
        # so the column only reaches it through here.
        "base_sha": "TEXT",
        # A FAMILY BUDGET: the ceiling on this feature's whole rollup — its planner, its
        # manager and every child — the same dollars `jarvis cost <fo-id>` adds up. It is
        # not a per-child number and is never divided up front; a child takes its slice
        # out of the unreserved remainder at the moment it is dispatched
        # (`budget.reserve`). NULL is no ceiling, exactly as before this shipped.
        "budget_usd": "REAL",
    },
    "wo_turns": {
        # See the CREATE TABLE comment. Live databases already have `wo_turns`, so the
        # column only reaches them through here.
        "usage_json": "TEXT",
        # WHY THE FAILURE DIAGNOSIS IS STORED AND THE PAUSE IS NOT. A pause is a verdict
        # — re-derived from the latest turn every time it is asked for, so it cannot go
        # stale (see worker_session's note, and Neo's ruling on question 83). These two
        # are the EVIDENCE that verdict is read from, and evidence has to be kept: they
        # come off the CLI's result JSON, which Claude Code prunes on its own schedule,
        # so a turn diagnosed from the file today is undiagnosable next week. Same rule
        # that earned `usage_json` its column.
        #
        # `terminal_reason` is why the CLI's query loop stopped, verbatim; api_error,
        # aborted_streaming, prompt_too_long, max_turns, completed and the rest of a
        # closed set it defines. `api_error_status` is the HTTP status when the failure
        # was an API error — 500+ is the transport and retriable, 429 is the usage
        # window. NULL for both means "not recorded", which is a turn reaped before
        # these columns existed, and never "nothing went wrong".
        "terminal_reason": "TEXT",
        "api_error_status": "INTEGER",
        # See the CREATE TABLE comment. NULL on every row written before turns moved into
        # their own units, which reads correctly as "the direct transport".
        "unit": "TEXT",
        # See the CREATE TABLE comment. NULL on every row written before the transcript
        # fallback existed, which reads correctly as "the CLI's own figure".
        "cost_source": "TEXT",
    },
    "validation_rounds": {
        # WHICH CONFIGURATION JUDGED THIS ROUND — a different question from the work
        # order's stamp, because one unit can be judged three times under three
        # configurations. The same idea as this row's `fingerprint`, about the other
        # input. Load-bearing rather than decorative: `Daemon._validate_work_order`
        # resolves its `ValidationConfig` from this id, so settling follows the version
        # the round was OPENED under and not a catalog that has since moved (§4.1). NULL
        # falls back to the live catalog. `validation_rounds` already ships, so the
        # column reaches a live database only through here.
        "config_version": "TEXT",
        # WHICH COMMIT THE PANEL JUDGED — `evidence.judged_head`, the pull request's
        # `headRefOid` at the moment the packet was collected. A verdict is a judgement
        # about one diff, and this is the only thing in the OS that says which one, so it
        # is what `validated_head` hands the auto-merge decision
        # (docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md §5.2).
        #
        # DEFAULT '' AND THAT IS THE FAIL-CLOSED VALUE, not a placeholder: every round
        # written before this column existed migrates to it, as does every round judged
        # from a worktree, and `validated_head` reads '' as "not recorded" — which never
        # auto-merges. NOT NULL so there is one spelling of "nothing" rather than two.
        "head_sha": "TEXT NOT NULL DEFAULT ''",
        # WHY A PERSON FORCED THIS ROUND — `jarvis validation force --reason`, verbatim.
        # The population this exists for is every round judged before `head_sha` shipped:
        # they all carry '' and can never auto-merge, so an operator has to open a fresh
        # round by hand, and a re-judgement that looked organic afterwards would be
        # indistinguishable from a worker re-delivering (spec
        # docs/superpowers/specs/2026-09-15-forcing-a-validation-round.md §3).
        #
        # DEFAULT '' MEANS "NOT FORCED", which is the honest reading of every round
        # written by a submission and of every round written before this column existed.
        # NOT NULL so there is one spelling of "nobody forced this" rather than two.
        "forced_reason": "TEXT NOT NULL DEFAULT ''",
        # WHY A `failed` ROUND IS NOT A FAILURE — `VALIDATION_CI_CAUSE` while GitHub is
        # still running the checks, `VALIDATION_HELD_CAUSE` while the account's usage
        # window is spent. The cause was already on the `validation_failed` event, which
        # `validation_hold_until` reads for the SCHEDULING decision; it is here because
        # three RENDERERS need it and none of them can reach that event — a feature
        # round's lives on its manager's timeline, not on the round's own subject. See
        # `validation_standing`.
        #
        # NULLABLE, and NULL is "nothing is holding this round": the honest reading of
        # every non-hold close and of every row written before this column existed. NO
        # BACKFILL, and the reader is what justifies that — a round still genuinely held
        # is re-closed in place on its next recheck tick and writes the column itself
        # within `CI_HOLD_RECHECK_SECONDS`, which is the whole population anybody can
        # misread. A row that keeps NULL is a round that settled, and a settled round is
        # not waiting for anything.
        "hold_cause": "TEXT",
        # THE COMMIT THIS VERDICT HAS BEEN CARRIED FORWARD TO, and why. A SECOND column
        # rather than an overwrite of `head_sha`, and that is the whole point: `head_sha`
        # means "the commit the seats read" and must stay true on `jarvis validation
        # show` for ever. Writing the new head over it would make that surface say five
        # judges examined a commit none of them ever saw.
        #
        # Written only by `ops.carry_validated_head`, only when the OS itself moved the
        # head by merging the base in and nothing else did — see that function for the
        # three facts it checks. `validated_head` prefers it, so this is what an
        # automatic merge binds to; the gate the merge still files quotes both.
        #
        # DEFAULT '' MEANS "NEVER CARRIED", the honest reading of every round written by
        # a submission and of every round written before this column existed. NOT NULL
        # so there is one spelling of "not carried" rather than two.
        "carried_head_sha": "TEXT NOT NULL DEFAULT ''",
        "carried_reason": "TEXT NOT NULL DEFAULT ''",
        # WHAT EACH FILE LOOKED LIKE IN THE DIFF THIS ROUND JUDGED — `evidence.
        # file_digests` as a JSON object, written beside `head_sha` and for the same
        # reason: it is a fact about the packet, not about the verdict. The next
        # submission diffs its own map against this one to learn which files the
        # submitter actually moved, which no other column can say — every one of them is
        # cumulative against the base (spec
        # docs/superpowers/specs/2026-09-22-a-round-must-answer-the-list.md §3).
        #
        # DEFAULT '' MEANS "NOT RECORDED" and it FAILS OPEN: `unanswered_submission`
        # reads an empty map as "I cannot tell what moved" and lets the panel judge, so
        # every round written before this column existed costs a submitter nothing.
        "file_shas": "TEXT NOT NULL DEFAULT ''",
    },
    "approvals": {
        # Which SEAT attempted the command, when a subagent did. NULL means the session's
        # lead ran it directly, which is every gate a plain worker ever trips.
        #
        # Needed because `JARVIS_WO_ID` is per-session, not per-agent: a gate a subagent
        # trips files its request against the work order that owns the turn. That is the
        # right owner — the lead is answerable for what its team did — but without this
        # column the audit trail would say the planner attempted what its architect did.
        # `PreToolUse` carries `agent_type` for a subagent's call and omits the key
        # entirely for the lead's, so the payload can always tell the two apart.
        "agent_type": "TEXT",
        # `jarvis gate contest`: a different claim from every other row, and the column is
        # what makes "dismissed or denied, never approved" enforceable — spec §2.
        "contested": "INTEGER NOT NULL DEFAULT 0",
        # WHY a row landed in `expired`: 'spent', 'lapsed', 'superseded' or 'abandoned'
        # — only the last is worth counting. Spec 2026-09-12 §4, §5; 2026-09-19 §1.
        "closed_as": "TEXT NOT NULL DEFAULT ''",
        # WHEN THIS REQUEST STARTED REFUSING THE WORKER'S COMMANDS — the moment it went
        # `pending`, which `ts` does not record for one filed `awaiting_case` and `status`
        # cannot recover once it is decided. NULL means never: a request nobody ever
        # argued blocked nothing (`hooks.pending_turn_block` reads `pending` and only
        # `pending`). The one reader is `gate_open_at`; §4 of
        # docs/superpowers/specs/2026-09-19-an-attempt-the-worker-could-not-make.md.
        "pending_at": "REAL",
    },
    # An alarm can name a FEATURE ORDER as its subject and a health probe as its source.
    # All four are additive with defaults and no CHECK: `_migrate` runs inside
    # `ProjectStore.__init__` — every CLI invocation and every reconcile of every
    # project, over live production databases — and a twelve-step table rebuild there is
    # not an option. So `wo_id` stays `NOT NULL` and means the CARRIER (see
    # `carrier_for_feature`), and `fo_id` carries no foreign key.
    #
    # The pairing the schema cannot express — `subject_kind == 'feature_order'` iff
    # `fo_id` — is enforced in `add_finding`, because a constraint neither the database
    # nor Python enforces is one that fails as a wrong page three weeks later.
    "wo_alarms": {
        "subject_kind": "TEXT NOT NULL DEFAULT 'work_order'",   # ALARM_SUBJECTS
        "fo_id": "TEXT",                                        # set iff feature_order
        "source": "TEXT NOT NULL DEFAULT 'cost'",               # ALARM_SOURCES
        "probe": "TEXT",                                        # the probe id (§2)
        # The proposed remedy and the gate grant that must open before it runs (§5).
        # Nullable with no default: a row raised before this shipped proposed nothing,
        # which is what NULL already says.
        "remedy": "TEXT",
        "remedy_argument": "TEXT",
        "remedy_approval_id": "INTEGER",
        # When this alarm may next be claimed, after a review call that never happened —
        # see `release_alarm_claim`. NULL reads as "claimable now", which is every row
        # that predates this and every alarm that has never failed.
        "retry_after": "REAL",
    },
    # WHO WROTE THIS MESSAGE, as opposed to `source`, which is which surface filed it.
    # Additive with an EMPTY default on purpose: every row written before this column
    # existed reads as "we cannot say", which is the honest answer and deliberately not
    # the same claim as "the user did not write it". See §2 of
    # docs/superpowers/specs/2026-09-11-a-gate-request-carries-the-users-words.md.
    "wo_messages": {
        "authored_by": "TEXT NOT NULL DEFAULT ''",
        # Delivery retries. A message whose turn could not be launched used to go
        # straight to `failed` — the user's words, silently discarded, because `claude`
        # was unreachable for a second (GitHub issue 43's shape; spec
        # docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §6). It now
        # stays `queued` behind `retry_after` until the attempts are spent.
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "retry_after": "REAL",
        # Why the last delivery attempt failed, for the surface that has to say so.
        "last_error": "TEXT NOT NULL DEFAULT ''",
    },
    # WHO DECIDED AN ASSUMPTION, AND ON WHAT BASIS. Until auto-review shipped there was
    # exactly one answer — the user, through `jarvis wo review` — so a row said only
    # `accepted` and every surface read that as the user's act. Now the OS can settle one
    # itself (docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md §4), and a
    # record that cannot tell the two apart is a record that credits a machine decision
    # to a person.
    #
    # EMPTY IS "THE USER", not "unknown", and that reads correctly on every row written
    # before this existed: the user was the only thing that could have written a verdict
    # into them. `ops.review_work_order` stamps `ASSUMPTION_DECIDER_USER` explicitly all
    # the same, so the claim is asserted going forward rather than inferred from a
    # default.
    "assumptions": {
        "decided_by": "TEXT NOT NULL DEFAULT ''",        # '' | user | neo
        "decided_reason": "TEXT NOT NULL DEFAULT ''",    # the reviewer's one line
        # The MODEL that ruled, and the configuration it ruled under — the two facts that
        # let a reader judge a machine verdict months later, when both have moved on.
        # Empty on every user verdict, which is the honest answer: a person is not a
        # model, and a config version stamped on a human decision would say nothing.
        "decided_model": "TEXT NOT NULL DEFAULT ''",
        "decided_config_version": "TEXT",
        # The Neo question that ruled on this one assumption. One question per assumption
        # — never a batch verdict over a list (§3) — and the back-link `NeoStore` cannot
        # hold, since it is OS-wide and knows nothing about a project's tables. Read by
        # `invariants.check_neo_escalations_are_live` to tell a live escalation from a
        # moot one.
        "neo_question_id": "INTEGER",
        # THE VERDICT FORMED WHILE THE WORKER WAS STILL TYPING, and it settles NOTHING.
        # `status` stays `pending` until the confirmation pass at delivery rules again —
        # docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-
        # runs.md §4.1, which is explicit that a `provisional` STATUS value would read as
        # "not pending" at the eight-plus call sites that gate the user's review on it,
        # and so would silently open that gate. Provisional state lives in columns
        # instead: a surface that has not been taught about them is merely unaware,
        # never wrong.
        #
        # EMPTY IS "NEVER JUDGED EARLY" — every row written before this existed, and
        # every row in a project with early review off, which is most of the fleet. Those
        # rows must render exactly as they do today.
        "provisional_verdict": "TEXT NOT NULL DEFAULT ''",     # '' | accept | object
        "provisional_reason": "TEXT NOT NULL DEFAULT ''",      # Neo's one line
        # The model and the configuration behind the early verdict, for the reason
        # `decided_model` / `decided_config_version` carry above: a machine judgement
        # read months later is only judgeable if it names what produced it. The model is
        # whatever the TRANSPORT reported, never what was asked for — those differ when a
        # model is aliased or falls back, and the record wants the one that answered.
        "provisional_model": "TEXT NOT NULL DEFAULT ''",
        "provisional_stakes": "TEXT NOT NULL DEFAULT ''",      # or 'unclassified'
        "provisional_ts": "REAL",
        "provisional_config_version": "TEXT",
        # THE SECOND Neo question, asked at delivery (§7). Separate from
        # `neo_question_id` rather than overwriting it: that one points at the early
        # question, "one question per assumption per pass" is the invariant being
        # preserved, and reusing the column would erase what the OS thought while the
        # work was still running — which is half of what the user reads afterwards.
        "confirm_question_id": "INTEGER",
        # THE OBJECTION SENT TO A RUNNING WORKER, identified by the ENVELOPE it IS.
        # Not a `wo_messages` id: `bus.post` returns an envelope id, and the message row
        # does not exist until `Daemon.deliver_envelopes` resolves that envelope on a
        # later tick — so a message id could not be written at the moment the objection
        # is recorded, and §6.1 requires the record to be complete BEFORE anything
        # reaches a wire. `envelopes.delivered_msg_id` below closes the link forward.
        "objection_envelope_id": "INTEGER",
        "objection_transport": "TEXT NOT NULL DEFAULT ''",     # '' | queue | peer
        # THREE TIMESTAMPS BECAUSE THEY ARE THREE FACTS. One boolean cannot tell "sent,
        # still in flight" from "sent, never arrived" from "withdrawn, the order stopped
        # first"; the user asked to see how and when an objection was delivered and
        # whether it was, §8 renders each of those differently, and §9 raises attention
        # on exactly one of them.
        "objection_sent_ts": "REAL",
        "objection_delivered_ts": "REAL",
        "objection_withdrawn_ts": "REAL",
    },
    # WHICH MESSAGE AN ENVELOPE BECAME. An envelope records that it was delivered and to
    # which work order, but not which `wo_messages` row carries its words — so nothing
    # can follow the link forward to ask what happened to them. §8 (what the worker did
    # about an objection) and §9 (is this objection undeliverable) both have to, and so
    # will the next reader of the bus. NULL on every row written before this and on every
    # envelope still queued, which is the honest answer in both cases.
    "envelopes": {
        "delivered_msg_id": "INTEGER",
    },
}

#: `assumptions.decided_by` when the person reviewed it. See the migration note above:
#: `''` means the same thing on a historical row, and this is what says so on a new one.
ASSUMPTION_DECIDER_USER = "user"

#: `assumptions.decided_by` when the OS did — `autoreview`. The one value that must never
#: be mistaken for the user's, which is the whole reason the column exists.
ASSUMPTION_DECIDER_OS = "neo"

#: What an early verdict may say. NOT a `status` value and never written to `status` —
#: see the `provisional_verdict` note in ADDED_COLUMNS. `object` is guidance to a running
#: worker, not a machine rejection: the settlement verdict space is still ACCEPT or
#: ESCALATE (§2 of both specs).
PROVISIONAL_VERDICTS = ("accept", "object")

#: How an objection reached the worker. `queue` always exists and is the fallback;
#: `peer` is a mid-turn delivery. Recorded because "it was sent" and "it was sent in a
#: way the worker could act on before its next turn boundary" are different claims.
OBJECTION_TRANSPORTS = ("queue", "peer")

#: `wo_messages.authored_by` when the OS could prove the human typed it. The ONLY value
#: this column ever takes: everything else is unattributed (`''`), because a stamp that
#: names a machine author would invite a later reader to treat the set of stamps as a
#: closed world and ask "which of these is trusted", when the only question that matters
#: is "is this one the user's". Written by `ops.send_message` alone (§2 of
#: docs/superpowers/specs/2026-09-11-a-gate-request-carries-the-users-words.md).
MESSAGE_AUTHOR_USER = "user"

# A dependency is satisfied only when it reaches `completed` — the strict rule of the
# feature-order design. It is affordable because the merge poller landed first: the user
# merges the pull request they were going to merge anyway and the work order completes
# itself within a tick or two, so an edge costs no extra human step.
#
# `waiting_pr_merge` deliberately does NOT satisfy: the dependent's worktree is cut from
# the main working tree's HEAD, which does not yet contain an unmerged dependency's code,
# so releasing it early gives it a tree without the thing it was told to build on.
DEPENDENCY_SATISFIED_STATUS = "completed"

# ...and these are the statuses from which it can never get there. A dependency parked in
# one of them strands its dependents forever, which is the one case that has to speak up
# rather than sit quietly in `pending` (see invariants.true_blockers).
DEPENDENCY_DEAD_STATUSES = ("cancelled", "failed")


class ProjectStore:
    def __init__(self, project_path: str | Path):
        self.project_path = Path(project_path)
        self.db_path = project_db_path(self.project_path)
        self.conn = db.connect(self.db_path)
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, columns in ADDED_COLUMNS.items():
            have = {
                r["name"]
                for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self._backfill_alarms()
        self._backfill_abandoned_gates()
        self._backfill_spent_gates()
        self._backfill_pending_at()

    def _backfill_pending_at(self) -> None:
        """Give historical rows the best `pending_at` the record can support. Spec §4.

        `ts` is the filing time, so it is exact for every row filed straight into
        `pending` (the default, and every gate a worker trips) and early by the arguing
        time for one that was held first. Two shapes are left NULL because they provably
        never refused anyone: a request still `awaiting_case`, and one abandoned unargued.
        Idempotent — it only ever fills a NULL.
        """
        self.conn.execute(
            """UPDATE approvals SET pending_at = ts
               WHERE pending_at IS NULL AND status != 'awaiting_case'
                 AND closed_as != 'abandoned'"""
        )
        self.conn.commit()

    #: The reason `gates.sweep_unargued` wrote while the TTL still recorded a DENIAL.
    #: A prefix because the minute count varies per project; nothing else ever wrote it,
    #: which is what makes the backfill below safe.
    _TTL_DENIAL_PREFIX = "no case was made for it within "

    def _backfill_abandoned_gates(self) -> None:
        """Re-file the TTL's old denials as what they were: abandonments. Spec §4, §5.

        NARROW, and that is the whole safety argument: `decided_by='os'` plus the sweep's
        own reason prefix, so a verdict any REVIEWER reached is never touched. Idempotent.
        """
        self.conn.execute(
            """UPDATE approvals SET status='expired', closed_as='abandoned'
               WHERE status='denied' AND decided_by='os' AND closed_as=''
                 AND decision_reason LIKE ?""",
            (self._TTL_DENIAL_PREFIX + "%",),
        )

    def _backfill_spent_gates(self) -> None:
        """Re-file the grants that were USED as spent, not lapsed. Spec 2026-09-19 §1.

        Without this, every auto-merge the fleet has ever run keeps reading as a grant
        Neo let time out — 23 of the 26 `auto_merge` gates in production on 2026-09-19.
        NARROW and idempotent: `uses > 0` is what the two sweeps disagreed about, and
        nothing else writes `closed_as='lapsed'`.
        """
        self.conn.execute(
            """UPDATE approvals SET closed_as='spent'
               WHERE status='expired' AND closed_as='lapsed'
                 AND uses > 0 AND uses >= max_uses"""
        )

    def _backfill_alarms(self) -> None:
        """Give every alarm raised before `wo_alarms` existed a row of its own.

        A backfill rather than a permanent union read ("rows, plus events with no row")
        in `alarms_across`: that union would be in the one function every alarm surface
        is built on, for ever, to serve the two events the production fleet holds today.

        'ONCE' IS NOT FREE HERE. This runs inside `__init__` — every CLI invocation and
        every reconcile of every project, not once per release — so the guard is the
        `(wo_id, kind, seq)` set rebuilt from the table on each pass, not a flag. The
        count comparison above it is only a fast path off the hot road; correctness is
        the set. See §1 of docs/superpowers/specs/2026-08-31-the-supervisor.md.

        `skipped`, never `raised`: `raised` is the supervisor's work queue, and history
        landing in it would spend one model call per legacy alarm, fleet-wide, on turns
        that finished weeks ago.
        """
        legacy = self.conn.execute(
            "SELECT COUNT(*) c FROM wo_events WHERE kind='cost_alarm'").fetchone()["c"]
        have = self.conn.execute("SELECT COUNT(*) c FROM wo_alarms").fetchone()["c"]
        if legacy <= have:
            return
        known = {(r["wo_id"], r["kind"], r["seq"]) for r in
                 self.conn.execute("SELECT wo_id, kind, seq FROM wo_alarms")}
        rows = self.conn.execute(
            "SELECT * FROM wo_events WHERE kind='cost_alarm' ORDER BY ts").fetchall()
        for event in rows:
            payload = db.from_json(event["payload"], {}) or {}
            key = (event["wo_id"], str(payload.get("kind") or "unknown"),
                   int(payload.get("seq") or 0))
            if key in known:
                continue
            known.add(key)
            self.conn.execute(
                """INSERT INTO wo_alarms (id, wo_id, ts, kind, seq, reason, status)
                   VALUES (?,?,?,?,?,?,'skipped')""",
                (db.new_id("al"), key[0], event["ts"], key[1], key[2],
                 str(payload.get("reason") or "")),
            )

    def close(self) -> None:
        self.conn.close()

    # -- work orders -------------------------------------------------------

    def create_work_order(
        self,
        title: str,
        description: str = "",
        origin: str = "jarvis",
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
        append_system_prompt: str | None = None,
        backlog_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        wo_id: str | None = None,
        status: str = "pending",
        session_id: str | None = None,
        depends_on: list[str] | None = None,
        parent_id: str | None = None,
        kind: str = "worker",
        spec_section: str | None = None,
        issue_url: str | None = None,
        issue_priority: str | None = None,
        budget_usd: float | None = None,
    ) -> dict[str, Any]:
        """Create a work order. `status` and `session_id` are set in the same INSERT
        rather than afterwards, because the row is visible to the daemon the instant it
        lands: a record that is `pending` for even a moment can be claimed and dispatched
        (`claim_next_pending`), which for an injected session would launch a worker into
        the user's own conversation.

        `depends_on` names work orders that must reach `completed` first. Every id is
        checked here, at the only moment a dependency edge is ever written — which is
        also why there is no cycle check: an edge may only point at a row that already
        exists, so the graph is acyclic by construction. Anything that lets an existing
        work order acquire an edge later loses that property and owes one.
        """
        assert origin in WO_ORIGINS, origin
        assert status in WO_STATUSES, status
        assert kind in WO_KINDS, kind
        wo_id = wo_id or db.new_id("wo")
        deps = list(depends_on or [])
        if wo_id in deps:
            raise ValueError(f"work order {wo_id!r} cannot depend on itself")
        for dep in deps:
            self.get_work_order(dep)  # KeyError names the id that does not exist
        if parent_id:
            self.get_feature_order(parent_id)  # same: KeyError names it
        ts = db.now()
        self.conn.execute(
            """INSERT INTO work_orders (id, title, description, status, origin,
                   created_at, updated_at, model, effort, permission_mode,
                   append_system_prompt, backlog_id, metadata, session_id, depends_on,
                   parent_id, kind, spec_section, issue_url, issue_priority,
                   budget_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                wo_id, title, description, status, origin, ts, ts, model, effort,
                permission_mode, append_system_prompt, backlog_id,
                db.to_json(metadata or {}), session_id, db.to_json(deps),
                parent_id, kind, spec_section or None, issue_url or None,
                issue_priority or None, budget_usd,
            ),
        )
        self.add_event(wo_id, "created", {"origin": origin, "depends_on": deps,
                                          **({"parent_id": parent_id} if parent_id else {}),
                                          **({"kind": kind} if kind != "worker" else {}),
                                          **({"budget_usd": budget_usd} if budget_usd else {})})
        return self.get_work_order(wo_id)

    def get_work_order(self, wo_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM work_orders WHERE id=?", (wo_id,)).fetchone()
        if row is None:
            raise KeyError(f"work order {wo_id!r} not found in {self.db_path}")
        return dict(row)

    def work_orders_for_issue(self, issue_url: str) -> list[dict[str, Any]]:
        """Every work order filed against this tracker issue, newest first.

        The idempotency question in one query (issue #240 D): a second `jarvis bug
        report` for an issue that already has an OPEN work order must not file another,
        and a REOPENED issue whose old work order is terminal must be free to get one.
        Hidden rows are included — hiding stops a record being listed, it does not stop
        it being the work order that already exists.
        """
        rows = self.conn.execute(
            "SELECT * FROM work_orders WHERE issue_url=? ORDER BY created_at DESC",
            (issue_url,)).fetchall()
        return db.rows_to_dicts(rows)

    # -- the issue <-> work order relation ------------------------------------------
    #
    # Both directions answerable WITHOUT A NETWORK CALL, which is the whole of why the
    # relation exists: the origin used to live only in the issue body's prose, so "which
    # issues did this order raise" could only be answered by scraping GitHub and
    # `jarvis wo show` could not answer it at all.

    def link_issue(self, issue_url: str, unit_id: str, kind: str, *,
                   round_no: int = 0, seat: str = "", announced: bool = False) -> bool:
        """Record that `unit_id` raised, was assigned, or cites `issue_url`.

        Returns True only when this is a link the project did not have — the caller's
        signal that something new happened, and what keeps the sweep from re-announcing
        a reference it has already commented on.

        ONE ROW PER (issue, unit) AND THE KIND ONLY EVER STRENGTHENS (`ISSUE_LINK_KINDS`
        is the order). An order whose brief cites the very issue it was dispatched to fix
        must not count twice, and re-recording the weaker kind afterwards must not
        overwrite the stronger one — either would corrupt the count the user reads as a
        priority signal.
        """
        if kind not in ISSUE_LINK_KINDS:
            raise ValueError(f"{kind!r} is not one of {ISSUE_LINK_KINDS}")
        row = self.conn.execute(
            "SELECT kind FROM issue_links WHERE issue_url=? AND unit_id=?",
            (issue_url, unit_id)).fetchone()
        with db.write_transaction(self.conn):
            if row is None:
                self.conn.execute(
                    "INSERT INTO issue_links (issue_url, unit_id, kind, round, seat, "
                    "created_at, announced_at) VALUES (?,?,?,?,?,?,?)",
                    (issue_url, unit_id, kind, round_no, seat, db.now(),
                     db.now() if announced else None))
                return True
            if ISSUE_LINK_KINDS.index(kind) > ISSUE_LINK_KINDS.index(row["kind"]):
                self.conn.execute(
                    "UPDATE issue_links SET kind=?, round=?, seat=? "
                    "WHERE issue_url=? AND unit_id=?",
                    (kind, round_no, seat, issue_url, unit_id))
        return False

    def record_issue(self, issue_url: str, *, number: int = 0, repo: str = "",
                     title: str = "", state: str | None = None) -> None:
        """Remember what the OS knows about one issue. Upsert; never unlearns.

        `state=None` leaves the cached state alone, so the filing path can record an
        issue's identity without claiming to have read its state — and a title already
        read off GitHub is not overwritten by a blank.
        """
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tracked_issues (issue_url, number, repo, title, state, "
                "checked_at) VALUES (?,?,?,?,?,?) ON CONFLICT(issue_url) DO NOTHING",
                (issue_url, number, repo, title, state or "",
                 db.now() if state else None))
            if number:
                self.conn.execute("UPDATE tracked_issues SET number=? WHERE issue_url=?",
                                  (number, issue_url))
            if repo:
                self.conn.execute("UPDATE tracked_issues SET repo=? WHERE issue_url=?",
                                  (repo, issue_url))
            if title:
                self.conn.execute("UPDATE tracked_issues SET title=? WHERE issue_url=?",
                                  (title, issue_url))
            if state:
                self.conn.execute(
                    "UPDATE tracked_issues SET state=?, checked_at=? WHERE issue_url=?",
                    (state, db.now(), issue_url))

    def record_internal_follow_up(self, digest: str, unit_id: str, *, round: int = 0,
                                  seat: str = "", title: str = "", detail: str = "",
                                  file: str = "", symbol: str = "",
                                  failure: str = "") -> bool:
        """Keep a follow-up whose text may not be published. True if this one is new.

        The tracker half of the same decision is `ops.file_validation_follow_ups`; this
        is where a finding goes when the repository is not established private (spec §9)
        — the whole finding, because the internal record is where it is read from.
        """
        with db.write_transaction(self.conn):
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO internal_follow_ups "
                "(digest, unit_id, round, seat, title, detail, file, symbol, failure, "
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (digest, unit_id, round, seat, title, detail, file, symbol, failure,
                 db.now()))
            return cur.rowcount > 0

    def internal_follow_ups(self, unit_id: str | None = None) -> list[dict[str, Any]]:
        """Withheld follow-ups — one unit's, or the whole project's, oldest first."""
        sql = "SELECT * FROM internal_follow_ups"
        args: tuple[Any, ...] = ()
        if unit_id:
            sql += " WHERE unit_id=?"
            args = (unit_id,)
        return db.rows_to_dicts(self.conn.execute(sql + " ORDER BY id", args).fetchall())

    def record_issue_label(self, issue_url: str, count: int) -> None:
        """The `referenced: N` the OS last managed to put on the issue."""
        with db.write_transaction(self.conn):
            self.conn.execute(
                "UPDATE tracked_issues SET refs_labelled=? WHERE issue_url=?",
                (count, issue_url))

    def mark_issue_announced(self, issue_url: str, unit_id: str) -> None:
        with db.write_transaction(self.conn):
            self.conn.execute(
                "UPDATE issue_links SET announced_at=? WHERE issue_url=? AND unit_id=?",
                (db.now(), issue_url, unit_id))

    def issue_links_of(self, unit_id: str) -> list[dict[str, Any]]:
        """Every issue this one work order or feature order is linked to.

        Joined onto the cached facts so a caller gets the issue's number, title and last
        known state in the same read — the projection behind the consolidated list on
        `jarvis wo show` and the work-order page.
        """
        rows = self.conn.execute(
            "SELECT l.*, i.number, i.repo, i.title, i.state, i.checked_at "
            "FROM issue_links l LEFT JOIN tracked_issues i USING (issue_url) "
            "WHERE l.unit_id=? ORDER BY l.round, i.number", (unit_id,)).fetchall()
        return db.rows_to_dicts(rows)

    def issue_board(self) -> list[dict[str, Any]]:
        """Every tracked issue with its reference count, most-referenced first.

        The ranking the user reads when choosing what to work on next. `refs` counts
        DISTINCT work orders by construction — one row per (issue, unit) — so the number
        means what the user asked it to mean.
        """
        rows = self.conn.execute(
            "SELECT i.*, COUNT(l.unit_id) AS refs FROM tracked_issues i "
            "JOIN issue_links l USING (issue_url) GROUP BY i.issue_url "
            "ORDER BY refs DESC, i.number DESC").fetchall()
        out = db.rows_to_dicts(rows)
        for row in out:
            row["units"] = [dict(r) for r in self.conn.execute(
                "SELECT unit_id, kind, round, seat FROM issue_links WHERE issue_url=? "
                "ORDER BY created_at", (row["issue_url"],)).fetchall()]
        return out

    def issues_to_sync(self) -> list[dict[str, Any]]:
        """Every linked issue, with its reference count and what is still unsaid.

        The sweep's whole input, in one query: `refs` is what the label should say,
        `refs_labelled` what it does say, and `unannounced` how many references have not
        been commented onto the issue yet. Nothing is sent while all three agree.
        """
        rows = self.conn.execute(
            "SELECT i.*, COUNT(l.unit_id) AS refs, "
            "SUM(CASE WHEN l.announced_at IS NULL THEN 1 ELSE 0 END) AS unannounced "
            "FROM tracked_issues i JOIN issue_links l USING (issue_url) "
            "GROUP BY i.issue_url ORDER BY i.checked_at IS NOT NULL, i.checked_at"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def unannounced_links(self, issue_url: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM issue_links WHERE issue_url=? AND announced_at IS NULL "
            "ORDER BY created_at", (issue_url,)).fetchall()
        return db.rows_to_dicts(rows)

    def work_orders_tracking_issues(self) -> list[dict[str, Any]]:
        """Every work order that owns a tracker issue. The sweep's whole input.

        Usually empty, and always small: only `jarvis bug report` writes `issue_url`.
        Hidden rows included for `record_pr_closed`'s reason — a hidden work order's
        issue may not go on saying something untrue.
        """
        rows = self.conn.execute(
            "SELECT * FROM work_orders WHERE issue_url IS NOT NULL AND issue_url != ''"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def find_by_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM work_orders WHERE session_id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_work_orders(
        self, statuses: tuple[str, ...] | None = None, limit: int = 200,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        conds, params = [], []
        if statuses:
            conds.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if not include_hidden:
            conds.append("hidden=0")
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        rows = self.conn.execute(
            f"SELECT * FROM work_orders{where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def search_work_orders(self, words: Sequence[str],
                           limit: int = 50) -> list[dict[str, Any]]:
        """Work orders matching `words`, most relevant first — SETTLED ONES INCLUDED.

        The listing verbs default to open and unhidden because they answer "what wants
        me now"; search answers "where is that thing", and the completed order the user
        is trying to find again is the case it exists for (wo-edf5c425).
        """
        expr, params = db.score_sql(words, {
            "id": 3, "title": 3, "description": 1, "result_summary": 1,
            "attention_reason": 1, "branch": 1,
        })
        rows = self.conn.execute(
            f"SELECT *, {expr} AS _score FROM work_orders "
            "WHERE _score > 0 ORDER BY _score DESC, created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def search_feature_orders(self, words: Sequence[str],
                              limit: int = 50) -> list[dict[str, Any]]:
        expr, params = db.score_sql(words, {
            "id": 3, "title": 3, "description": 1, "attention_reason": 1,
        })
        rows = self.conn.execute(
            f"SELECT *, {expr} AS _score FROM feature_orders "
            "WHERE _score > 0 ORDER BY _score DESC, created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def search_alarms(self, words: Sequence[str],
                      limit: int = 50) -> list[dict[str, Any]]:
        """Alarms carry their work order's title, as `alarms_across` does: an alarm read
        without the order it fired on says almost nothing."""
        expr, params = db.score_sql(words, {
            "a.id": 3, "a.kind": 2, "a.reason": 1, "a.note": 1, "a.verdict_reason": 1,
            "w.title": 1,
        })
        rows = self.conn.execute(
            f"SELECT a.*, w.title AS wo_title, {expr} AS _score FROM wo_alarms a "
            "JOIN work_orders w ON w.id = a.wo_id "
            "WHERE _score > 0 ORDER BY _score DESC, a.ts DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def search_gates(self, words: Sequence[str],
                     limit: int = 50) -> list[dict[str, Any]]:
        expr, params = db.score_sql(words, {
            "a.command": 3, "a.kind": 2, "a.wo_id": 2, "a.justification": 1,
            "a.evidence": 1, "a.decision_reason": 1, "w.title": 1,
        })
        rows = self.conn.execute(
            f"SELECT a.*, w.title AS wo_title, {expr} AS _score FROM approvals a "
            "JOIN work_orders w ON w.id = a.wo_id "
            "WHERE _score > 0 ORDER BY _score DESC, a.ts DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def status_counts(self, include_hidden: bool = False) -> dict[str, int]:
        """How many work orders sit in each status. Counted in SQL, not by listing.

        `list_work_orders` is capped at `limit`, so counting its result would quietly
        under-report exactly where it matters most — a project with more history than
        one page of it.
        """
        where = "" if include_hidden else " WHERE hidden=0"
        rows = self.conn.execute(
            f"SELECT status, COUNT(*) AS n FROM work_orders{where} GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def dependencies(self, wo: dict[str, Any]) -> list[str]:
        """The ids this work order waits on. Decoded at the use site, not on the row.

        `depends_on` stays raw JSON on the dict, like `acknowledged_blockers` and unlike
        `backlog.depends_on` — the two stores differ here and this follows the local one.
        Work-order rows are handed around widely (`{**wo, ...}` in worker_session, the
        dashboard, the hooks), so decoding a column in place would change the shape of a
        dict a lot of code already reads.
        """
        return db.from_json(wo.get("depends_on"), []) or []

    def unfinished_dependencies(self, wo_id: str) -> list[dict[str, Any]]:
        """The dependencies still standing between this work order and dispatch.

        A dependency that has been deleted counts as unfinished rather than satisfied,
        and says so — the alternative is releasing a work order because the thing it was
        told to build on vanished, which is the worse of the two failures.
        """
        blockers = []
        for dep_id in self.dependencies(self.get_work_order(wo_id)):
            try:
                dep = self.get_work_order(dep_id)
            except KeyError:
                blockers.append({"id": dep_id, "status": "missing", "title": "?"})
                continue
            if dep["status"] != DEPENDENCY_SATISFIED_STATUS:
                blockers.append({k: dep[k] for k in ("id", "status", "title")})
        return blockers

    def drop_dependencies(self, wo_id: str, dep_ids: list[str]) -> list[str]:
        """Cut these edges. Returns the edges that remain.

        The only way an edge is ever removed, and it is always a deliberate act by the
        user: a dependency that can never complete strands its dependent, and cutting
        the edge is the alternative to cancelling work that is still wanted. Removing
        edges cannot create a cycle, so the acyclic-by-construction property that
        `create_work_order` relies on survives this.
        """
        remaining = [d for d in self.dependencies(self.get_work_order(wo_id))
                     if d not in dep_ids]
        self.update_work_order(wo_id, depends_on=db.to_json(remaining))
        self.add_event(wo_id, "dependencies_dropped",
                       {"dropped": dep_ids, "remaining": remaining})
        return remaining

    def release_dispatch_claim(self, wo_id: str, error: str,
                               max_attempts: int = MAX_DISPATCH_ATTEMPTS) -> str:
        """Hand a claimed work order back after a turn that could never be launched.

        `NeoStore.release_claim`'s shape once more. Returns `"pending"` (retryable, held
        by `retry_after`) or `"failed"` (the launches are spent, and now it really is the
        user's problem). Spec
        docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §5.
        """
        row = self.conn.execute(
            "SELECT dispatch_attempts FROM work_orders WHERE id=?", (wo_id,)).fetchone()
        if row is None:
            return "failed"
        attempts = int(row["dispatch_attempts"] or 0) + 1
        if attempts >= max_attempts:
            self.conn.execute(
                "UPDATE work_orders SET status='failed', dispatch_attempts=?, "
                "retry_after=NULL, updated_at=? WHERE id=?",
                (attempts, db.now(), wo_id))
            return "failed"
        delay = DISPATCH_RETRY_BACKOFF_SECONDS[
            min(attempts - 1, len(DISPATCH_RETRY_BACKOFF_SECONDS) - 1)]
        self.conn.execute(
            "UPDATE work_orders SET status='pending', dispatch_attempts=?, "
            "retry_after=?, updated_at=? WHERE id=?",
            (attempts, db.now() + delay, db.now(), wo_id))
        return "pending"

    def clear_dispatch_attempts(self, wo_id: str) -> None:
        """A launch that worked spends the record of the ones that did not."""
        self.conn.execute(
            "UPDATE work_orders SET dispatch_attempts=0, retry_after=NULL WHERE id=?",
            (wo_id,))

    def claim_next_pending(self) -> dict[str, Any] | None:
        """Atomically claim the oldest claimable pending order (pending -> dispatching).

        Two things can make a pending work order unclaimable, and neither writes anything
        when it fires: the order is passed over and stays `pending`, because nothing about
        it has changed.

        1. **A dependency has not completed** (Phase 1). Note that this does not hold up
           the queue behind it — the filter is in the row selection, so a younger
           unblocked order is claimed while an older blocked one waits.
        2. **Its feature order is already running `max_parallel` children** (Phase 3).
           A per-feature slot cap, spent alongside the project-wide `max_concurrent`
           rather than instead of it: whichever is tighter binds. It applies only to a
           feature's `worker` children — the planner is the feature order deciding what
           its children are, not one of them, so capping it against its own children
           would be capping a feature against itself.

        3. **A dispatch that could not reach `claude` is backing off** — `retry_after`,
           written by `release_dispatch_claim`. Without it the caller's `while` loop
           re-claims the order it has just failed to launch, on the next iteration, and
           spends the whole ceiling inside one tick.

        For the overwhelming majority of work orders — no dependencies, no parent, never
        a failed launch — all three subqueries are vacuously true and this is the query it
        always was.
        """
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        cur = self.conn.execute(
            f"""UPDATE work_orders SET status='dispatching', updated_at=?
               WHERE id = (SELECT w.id FROM work_orders w
                           WHERE w.status='pending' AND w.hidden=0
                             AND COALESCE(w.retry_after, 0) <= ?
                             AND NOT EXISTS (
                                 SELECT 1 FROM json_each(w.depends_on) dep
                                 WHERE NOT EXISTS (
                                     SELECT 1 FROM work_orders d
                                     WHERE d.id = dep.value AND d.status = ?
                                 )
                             )
                             AND NOT EXISTS (
                                 SELECT 1 FROM feature_orders f
                                 WHERE f.id = w.parent_id AND w.kind = 'worker'
                                   AND f.max_parallel IS NOT NULL
                                   AND f.max_parallel <= (
                                       SELECT COUNT(*) FROM work_orders s
                                       WHERE s.parent_id = f.id AND s.kind = 'worker'
                                         AND s.status IN ({marks})
                                   )
                             )
                           ORDER BY w.created_at LIMIT 1)
               RETURNING *"""
            , (db.now(), db.now(), DEPENDENCY_SATISFIED_STATUS, *ACTIVE_STATUSES),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def count_active(self) -> int:
        """How many work orders are spending one of this project's `max_concurrent` slots.

        `SLOT_STATUSES`, not `ACTIVE_STATUSES`: the slot is for a turn that is executing,
        not for a record that is waiting on somebody (issue #134).

        **Managers are exempt, and the exemption is load-bearing.** A project manager
        order is idle between messages for the entire life of its feature, because that
        is what it is FOR. A coordinator is not a piece of the work — the same reasoning
        that already exempts the planner from a feature's `max_parallel`, applied to the
        project-wide cap, and it still bites now that parking is free: a manager runs a
        turn every time its feature reports, and two features would otherwise spend a
        `max_concurrent: 2` project's whole budget on bookkeeping.

        INV-MANAGER-SLOTS re-derives this from live state, because a regression here is
        invisible from every other surface: nothing looks wrong when a project simply
        stops claiming.
        """
        marks = ",".join("?" for _ in SLOT_STATUSES)
        row = self.conn.execute(
            f"SELECT COUNT(*) c FROM work_orders "
            f"WHERE status IN ({marks}) AND kind != 'manager'",
            SLOT_STATUSES,
        ).fetchone()
        return row["c"]

    def count_active_children(self, fo_id: str) -> int:
        """How many of this feature order's children are occupying a slot right now.

        The number `max_parallel` is compared against, exposed so a listing can say why a
        child is waiting without re-deriving the claim query's arithmetic differently.
        """
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.conn.execute(
            f"""SELECT COUNT(*) c FROM work_orders
                WHERE parent_id=? AND kind='worker' AND status IN ({marks})""",
            (fo_id, *ACTIVE_STATUSES),
        ).fetchone()
        return row["c"]

    def update_work_order(self, wo_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = db.now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE work_orders SET {cols} WHERE id=?", (*fields.values(), wo_id)
        )

    def set_status(self, wo_id: str, status: str, **extra: Any) -> None:
        assert status in WO_STATUSES, status
        self.update_work_order(wo_id, status=status, **extra)
        self.add_event(wo_id, "status", {"status": status})

    def seal_bill(self, order_id: str, payload_json: str, *, feature: bool = False,
                  at: float | None = None) -> None:
        """Freeze one order's bill.

        Written once at settle, and re-written only by `bill._upgrade_seal` — when the
        stored payload predates a field the module now computes AND recomputing today
        still sees at least as many tokens as the seal holds. Pass `at` to preserve the
        original seal time through such an upgrade: WHEN the order settled has not
        changed just because the payload was re-derived.
        """
        fields = {"bill_json": payload_json, "bill_sealed_at": at or db.now()}
        if feature:
            self.update_feature_order(order_id, **fields)
        else:
            self.update_work_order(order_id, **fields)

    def unsealed_terminal_orders(self, limit: int = 5) -> list[dict[str, Any]]:
        """Settled orders with no bill on record yet — oldest first, so a backlog drains.

        Bounded because the first tick after this ships meets every order the project
        has ever completed, and each bill reads transcripts off disk. A few per tick
        costs nothing and clears a hundred orders in an hour.
        """
        marks = ", ".join("?" for _ in TERMINAL_STATUSES)
        rows = self.conn.execute(
            f"SELECT * FROM work_orders WHERE status IN ({marks}) AND bill_json IS NULL"
            " ORDER BY updated_at LIMIT ?", (*TERMINAL_STATUSES, limit)).fetchall()
        return [dict(r) for r in rows]

    def sealed_bills_since(self, since: float) -> list[dict[str, Any]]:
        """Every work order whose bill was frozen at or after `since`, newest first.

        THE CHEAP AGGREGATE, and it is what makes an alarm about a project's SPEND
        affordable on the reconcile cadence: a bill is frozen JSON on the row already, so
        a window's worth is one indexed read and a JSON parse per order. Walking the
        transcripts instead is the objection finding 2 of
        docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md raised against
        doing this at all, and the reason nobody did.

        Sealed bills only, which is also the right population: an OPEN order's cost is
        still moving, and a share of a denominator that has not stopped changing would
        report a different number every tick for the same facts.
        """
        rows = self.conn.execute(
            "SELECT id, title, status, hidden, bill_sealed_at, bill_json"
            " FROM work_orders WHERE bill_json IS NOT NULL AND bill_sealed_at >= ?"
            " ORDER BY bill_sealed_at DESC", (since,)).fetchall()
        return db.rows_to_dicts(rows)

    def last_alarm_of_kind(self, kind: str) -> dict[str, Any] | None:
        """The newest alarm of one kind in this project, whatever became of it.

        THE DEDUPE MEMORY FOR AN ALARM ABOUT A STANDING CONDITION.
        `Daemon.check_burning_turns` matches on `(kind, seq)` because a burning turn has
        a turn to key on; a project's re-write tax has none, is still true on the next
        tick, and would re-raise for ever. `Daemon.check_rewrite_tax` keys on this
        instead: one alarm per kind per cohort window.

        WHATEVER BECAME OF IT is the whole point — judged, escalated, skipped or still
        open all mean the same thing here, that this window has already been reported.
        Filtering to the unsettled ones would re-raise the moment the supervisor acked.
        """
        row = self.conn.execute(
            "SELECT * FROM wo_alarms WHERE kind=? ORDER BY ts DESC LIMIT 1",
            (kind,)).fetchone()
        return dict(row) if row else None

    def latest_settled_order(self) -> dict[str, Any] | None:
        """The most recently settled work order, as a CARRIER and nothing more.

        `wo_alarms.wo_id` is a real foreign key, so a finding about something that is not
        a work order still needs one. `Daemon.check_rewrite_tax` can use the biggest
        contributor, because its subject IS a work order; `Daemon.check_cache_ttl`'s
        subject is a session the OS never dispatched, and no order is the number's
        exemplar — so this picks the one a reader following the link will find least
        confusing to be shown, and the alarm's own text says it stands for nothing.

        SETTLED and not merely existing: a running order's page is about a turn in
        flight, and hanging a standing finding there would put a fleet-wide claim beside
        live work it has nothing to do with.
        """
        row = self.conn.execute(
            f"""SELECT * FROM work_orders
                WHERE status IN ({','.join('?' * len(TERMINAL_STATUSES))})
                ORDER BY updated_at DESC LIMIT 1""",
            TERMINAL_STATUSES).fetchone()
        return dict(row) if row else None

    def unsealed_terminal_features(self, limit: int = 2) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in FO_TERMINAL_STATUSES)
        rows = self.conn.execute(
            f"SELECT * FROM feature_orders WHERE status IN ({marks}) AND bill_json IS"
            " NULL ORDER BY updated_at LIMIT ?", (*FO_TERMINAL_STATUSES, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def flag_attention(self, wo_id: str, reason: str) -> None:
        self.update_work_order(wo_id, needs_attention=1, attention_reason=reason)
        self.add_event(wo_id, "attention", {"reason": reason})

    def clear_attention(self, wo_id: str) -> None:
        # The blocker is gone, so any ack against it is spent: if the same reason comes
        # back later it is a new event and must be shown again.
        self.update_work_order(wo_id, needs_attention=0, attention_reason=None,
                               acknowledged_blockers=None)

    def ack_attention(self, wo_id: str, blockers: list[str], by: str = "you") -> None:
        """Record that the user has seen these blockers, and put the flag down.

        Deliberately dumb: the caller derives `blockers` (via `invariants.true_blockers`)
        so the store stays free of policy. Unlike `clear_attention` this remembers what
        was dismissed, which is the only reason the flag stays down across reconcile
        ticks — see `acknowledged_blockers` in ADDED_COLUMNS.

        `by` NAMES WHO DISMISSED THEM, because this is a claim about a person and the
        record used to make it on their behalf (issue 573). Rows written before it
        existed carry no `by` and the timeline attributes them to nobody — an OS ack and
        a user's ack are indistinguishable in those, and guessing would repeat the bug.
        THE ONLY CALLER IS THE USER'S OWN `ops.ack_attention`: a flag the OS raised for
        itself goes down through `lower_attention`, which acknowledges nothing.
        """
        self.update_work_order(wo_id, needs_attention=0, attention_reason=None,
                               acknowledged_blockers=db.to_json(blockers))
        self.add_event(wo_id, "acknowledged", {"blockers": blockers, "by": by})

    def lower_attention(self, wo_id: str) -> None:
        """Put the flag down, dismissing nothing and forgetting nothing.

        The third door beside `clear_attention` (the blocker is gone, so spend the acks)
        and `ack_attention` (the user has seen these, so remember them). This one is for
        a flag the OS raised for its OWN purposes and has now settled: it must not spend
        the user's acks and must not forge new ones. Silent on the timeline on purpose —
        `ops.ack_os_flag`'s callers all write an event of their own saying what they
        settled, and a second row per alarm would say it twice.
        """
        self.update_work_order(wo_id, needs_attention=0, attention_reason=None)

    def set_hidden(self, wo_id: str, hidden: bool = True) -> None:
        """Hide (or unhide) a work order.

        Hiding is non-destructive: the record and its whole history stay, they just
        stop showing up in listings, summaries and the attention list, and a hidden
        pending order is never dispatched.
        """
        self.get_work_order(wo_id)  # KeyError if it doesn't exist
        self.update_work_order(wo_id, hidden=1 if hidden else 0)
        self.add_event(wo_id, "hidden", {"hidden": bool(hidden)})

    # -- the scheduler's clock (`scheduled_jobs`) -------------------------------------
    #
    # Dumb on purpose, like `ack_attention`: the store keeps the row, `schedule.decide`
    # holds the policy. Nothing here consults a catalog or a clock it was not handed.

    def seed_schedule(self, job_id: str, now: float | None = None) -> dict[str, Any]:
        """This job's clock, created on first sight if it has never been seen.

        `last_fired_at = now` AT SEED TIME is what makes enabling a job mean "from the
        next interval" rather than "right now", and it is the same row a restart reads
        back, so neither event can fire an order. See `schedule.decide`.
        """
        now = db.now() if now is None else now
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO scheduled_jobs (job_id, created_at, last_fired_at)"
                " VALUES (?,?,?)", (job_id, now, now))
        row = self.conn.execute("SELECT * FROM scheduled_jobs WHERE job_id=?",
                                (job_id,)).fetchone()
        return dict(row)

    def schedule_state(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM scheduled_jobs WHERE job_id=?",
                                (job_id,)).fetchone()
        return dict(row) if row else None

    def list_schedule_states(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM scheduled_jobs ORDER BY job_id")]

    def record_schedule_fire(self, job_id: str, wo_id: str,
                             now: float | None = None) -> None:
        """A job fired: advance its clock to NOW and clear any hold.

        `now`, never `last_fired_at + interval` — that is the whole of "at most one
        catch-up, never a backfill" (`schedule.decide`), and moving it here rather than
        in the caller is what stops a second caller getting it wrong.
        """
        now = db.now() if now is None else now
        with self.conn:
            self.conn.execute(
                "UPDATE scheduled_jobs SET last_fired_at=?, last_wo_id=?, held_since=NULL,"
                " held_reason=NULL WHERE job_id=?", (now, wo_id, job_id))

    def record_schedule_hold(self, job_id: str, reason: str,
                             now: float | None = None) -> None:
        """A job wanted to fire and could not. Idempotent in `held_since`.

        The FIRST such tick is the one that matters — how long this has been parked is
        the number `check_schedule_progresses` judges by — so a held job re-held on the
        next tick keeps its original timestamp and only refreshes the words.
        """
        now = db.now() if now is None else now
        with self.conn:
            self.conn.execute(
                "UPDATE scheduled_jobs SET held_since=COALESCE(held_since, ?),"
                " held_reason=? WHERE job_id=?", (now, reason, job_id))

    def delete_work_order(self, wo_id: str) -> dict[str, int]:
        """Erase a work order and everything hanging off it. Returns the row counts.

        Foreign keys are enforced (see db.connect), so children go first. The whole
        cascade runs in one transaction: a half-deleted work order is worse than none.
        """
        self.get_work_order(wo_id)  # KeyError if it doesn't exist
        deleted: dict[str, int] = {}
        with db.write_transaction(self.conn):
            # Foreign keys are on, so a feature order still pointing at this work order
            # as its planner would refuse the delete outright. Releasing the link is the
            # right move rather than cascading: the feature order is not the thing being
            # deleted, and losing the planner is a fact about it worth surviving.
            self.conn.execute(
                "UPDATE feature_orders SET plan_wo_id=NULL WHERE plan_wo_id=?", (wo_id,)
            )
            for key, table in (("events", "wo_events"), ("messages", "wo_messages"),
                               ("turns", "wo_turns"),
                               ("assumptions", "assumptions"),
                               ("approvals", "approvals"),
                               ("notifications", "notifications")):
                cur = self.conn.execute(f"DELETE FROM {table} WHERE wo_id=?", (wo_id,))
                deleted[key] = cur.rowcount
            # `health_reviews` has no foreign key — its subject may be either kind, and
            # neither can carry a cascade for both — so it is deleted by hand. NOT a
            # seventh key in `deleted`: that dict is asserted with `==` over exactly six
            # (tests/test_wo_hide_delete.py::test_delete_work_order_cascades), and §4 of
            # docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md keeps it
            # at six on purpose.
            self.conn.execute(
                "DELETE FROM health_reviews WHERE subject_kind='work_order' "
                "AND subject_id=?", (wo_id,))
            self.conn.execute("DELETE FROM work_orders WHERE id=?", (wo_id,))
        return deleted

    # -- feature orders ----------------------------------------------------------
    #
    # A feature order has no session, no turns and no messages of its own — everything
    # it does, it does through work orders. So it has no timeline table either: its
    # history is written into the timeline of whichever work order carried the step
    # (`plan_submitted` on the planner, `created` on each child), which is where anyone
    # investigating it is already looking.

    def create_feature_order(self, title: str, description: str = "",
                             origin: str = "jarvis", backlog_id: str | None = None,
                             metadata: dict[str, Any] | None = None,
                             fo_id: str | None = None,
                             max_parallel: int | None = None,
                             budget_usd: float | None = None) -> dict[str, Any]:
        assert origin in WO_ORIGINS, origin
        assert max_parallel is None or max_parallel >= 1, max_parallel
        fo_id = fo_id or db.new_id("fo")
        ts = db.now()
        self.conn.execute(
            """INSERT INTO feature_orders (id, title, description, status, origin,
                   created_at, updated_at, backlog_id, metadata, max_parallel,
                   budget_usd)
               VALUES (?,?,?,'pending',?,?,?,?,?,?,?)""",
            (fo_id, title, description, origin, ts, ts, backlog_id,
             db.to_json(metadata or {}), max_parallel, budget_usd),
        )
        return self.get_feature_order(fo_id)

    def get_feature_order(self, fo_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM feature_orders WHERE id=?", (fo_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"feature order {fo_id!r} not found in {self.db_path}")
        return dict(row)

    def list_feature_orders(self, statuses: tuple[str, ...] | None = None,
                            limit: int = 200) -> list[dict[str, Any]]:
        where = f" WHERE status IN ({','.join('?' for _ in statuses)})" if statuses else ""
        rows = self.conn.execute(
            f"SELECT * FROM feature_orders{where} ORDER BY created_at DESC LIMIT ?",
            (*(statuses or ()), limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def feature_status_counts(self) -> dict[str, int]:
        """How many feature orders sit in each status. Counted in SQL, like
        `status_counts` and for the same reason: `list_feature_orders` is capped."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM feature_orders GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def flagged_feature_orders(self) -> list[dict[str, Any]]:
        """Every feature order asking for the user, whatever its status.

        Deliberately not filtered by `FO_OPEN_STATUSES`: `failed` is a SETTLED status and
        it is also the one a feature order raises its flag in — `settle_features` marks
        both in the same call. Listing only the open ones would drop the flag on the floor
        at the exact moment it means the most.
        """
        rows = self.conn.execute(
            "SELECT * FROM feature_orders WHERE needs_attention=1 ORDER BY created_at DESC"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def update_feature_order(self, fo_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = db.now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE feature_orders SET {cols} WHERE id=?", (*fields.values(), fo_id)
        )

    def set_feature_status(self, fo_id: str, status: str, **extra: Any) -> None:
        """Move a feature order, and retire its agent type when it settles.

        The deletion lives HERE, not at the four callers that settle a feature (the
        daemon's completed and failed branches, the merge poller, `fo cancel`), because
        it is the one line every one of them passes through. A generated agent left
        behind after its feature is over is a persona a later, unrelated work order in
        the same project could be given.

        `remove_agent` never raises and the spec snapshot stays in the stored plan, so
        `jarvis fo agent <fo-id>` rebuilds it — §3 of
        docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md.
        """
        from . import specs

        assert status in FO_STATUSES, status
        self.update_feature_order(fo_id, status=status, **extra)
        if status in FO_TERMINAL_STATUSES:
            specs.remove_agent(self.project_path, fo_id)

    def flag_feature_attention(self, fo_id: str, reason: str) -> None:
        self.update_feature_order(fo_id, needs_attention=1, attention_reason=reason)

    def clear_feature_attention(self, fo_id: str) -> None:
        self.update_feature_order(fo_id, needs_attention=0, attention_reason=None)

    def feature_children(self, fo_id: str) -> list[dict[str, Any]]:
        """The feature order's child work orders, oldest first.

        Oldest first, unlike every other listing here: children are created in
        dependency order (`plans.creation_order`), so creation order IS the plan's
        order, and printing it newest-first would show the graph upside down.

        The planner is deliberately excluded — it carries `parent_id` too, because it
        belongs to the feature order as much as any child does, but it is the session
        that produced the plan rather than a piece of the work. `plan_wo_id` is how you
        reach it.

        Every child carries `superseded`: True when the user answered for its failure
        with `jarvis fo resume`. ANNOTATED, NEVER FILTERED OUT — billing, cancellation
        and the child tree all still want the row, and a superseded child that vanished
        from the tree would look like one that never existed. Only the settle rule reads
        the flag (`Daemon.settle_features`).
        """
        rows = self.conn.execute(
            "SELECT * FROM work_orders WHERE parent_id=? AND kind='worker' "
            "ORDER BY created_at",
            (fo_id,),
        ).fetchall()
        answered = {s["wo_id"] for s in self.superseded_children(fo_id)}
        return [{**c, "superseded": c["id"] in answered}
                for c in db.rows_to_dicts(rows)]

    def superseded_children(self, fo_id: str) -> list[dict[str, Any]]:
        """Which of this feature's children the user has answered for, and why.

        Read straight off `feature_orders.metadata` rather than through
        `get_feature_order`, so a call for a feature that no longer exists is an empty
        list rather than a KeyError: `feature_children` is on every settle tick and every
        listing, and it has always been tolerant of an unknown id.
        """
        row = self.conn.execute(
            "SELECT metadata FROM feature_orders WHERE id=?", (fo_id,)
        ).fetchone()
        meta = db.from_json(row["metadata"], {}) if row else {}
        return list((meta or {}).get(SUPERSEDED_CHILDREN_KEY) or [])

    def supersede_children(self, fo_id: str, wo_ids: Iterable[str],
                           note: str = "") -> list[dict[str, Any]]:
        """Record that the user has answered for these children's failure.

        Idempotent on `wo_id`: resuming a feature twice must not double the record, and
        the FIRST note is the one kept — it is the one that was true when the decision
        was taken.
        """
        current = self.superseded_children(fo_id)
        known = {s["wo_id"] for s in current}
        for wo_id in wo_ids:
            if wo_id not in known:
                current.append({"wo_id": wo_id, "ts": db.now(), "note": note})
                known.add(wo_id)
        meta = db.from_json(self.get_feature_order(fo_id).get("metadata"), {}) or {}
        meta[SUPERSEDED_CHILDREN_KEY] = current
        self.update_feature_order(fo_id, metadata=db.to_json(meta))
        return current

    def feature_order_for_question(self, question_id: int) -> dict[str, Any] | None:
        """The feature order whose plan this Neo question is reviewing, if any.

        The mirror of `approval_for_question`, and it exists for the same reason: Neo's
        database is OS-wide and knows nothing about a project's tables, so the back-link
        has to be resolved from this side.
        """
        row = self.conn.execute(
            "SELECT * FROM feature_orders WHERE plan_question_id=?", (question_id,)
        ).fetchone()
        return dict(row) if row else None

    def feature_order_for_planner(self, wo_id: str) -> dict[str, Any] | None:
        """The feature order a plan question hangs off, found the way the question
        names it rather than the way the feature order points back.

        `feature_order_for_question` follows `plan_question_id`, so it goes blind the
        moment a resubmission moves that pointer — which is exactly the case
        `invariants.check_neo_escalations_are_live` has to judge. A plan question's
        `wo_id` is `plan_wo_id`, or the feature order itself when the planner is gone
        (`ops.submit_plan`), so both are matched here.
        """
        row = self.conn.execute(
            "SELECT * FROM feature_orders WHERE id=? OR plan_wo_id=?", (wo_id, wo_id)
        ).fetchone()
        return dict(row) if row else None

    def create_plan_children(self, fo_id: str,
                             ordered: list[dict[str, Any]],
                             manager: bool = False) -> list[dict[str, Any]]:
        """Turn an approved plan into work orders, in one transaction.

        `ordered` must already be in dependency order (`plans.creation_order`): each
        child's `needs` are plan-local keys, and they are resolved to real work-order ids
        as the children are created, which only works if every child follows the ones it
        needs. That constraint is not tidiness — `create_work_order` refuses an edge
        pointing at a row that does not exist yet, and that refusal is exactly what keeps
        the live dependency graph acyclic by construction. Resolving the keys as we go
        means a plan's edges are written by the same guarded path as a hand-typed
        `--depends-on`, rather than being stamped onto rows afterwards.

        All-or-nothing: a feature order holding three of its six children is worse than
        one holding none, because the three would start running against a plan that was
        never fully created.

        `manager` adds this feature's PROJECT MANAGER ORDER to the same transaction: one
        `kind='manager'` work order that owns the feature's follow-through and is the
        addressee for anything the feature needs a human-shaped decision about — a panel
        rejection, a deferral. It is created HERE rather than by the caller afterwards for
        exactly the reason the children are created together: a feature holding children
        but no manager is a feature whose rejections have nowhere to go. It is not in the
        returned list, because that list is the plan the user reviewed and the manager is
        not a piece of the work; `manager_work_order(fo_id)` is how you reach it.

        The flag is off by default and its caller passes `os.validation.enabled`, so with
        validation disabled this is the method it always was.
        """
        by_key: dict[str, str] = {}
        created: list[str] = []
        with db.write_transaction(self.conn):
            for child in ordered:
                wo = self.create_work_order(
                    title=child["title"],
                    description=child["description"],
                    origin="jarvis",
                    depends_on=[by_key[k] for k in child["needs"] if k in by_key],
                    parent_id=fo_id,
                    spec_section=child.get("spec_section"),
                )
                by_key[child["key"]] = wo["id"]
                created.append(wo["id"])
            if manager:
                self.create_manager_order(fo_id)
        return [self.get_work_order(wo_id) for wo_id in created]

    def create_manager_order(self, fo_id: str) -> dict[str, Any]:
        """The feature's project manager order. Opens no transaction of its own.

        Called from inside `create_plan_children`'s transaction, so it must not open one
        of its own: an all-or-nothing release is the whole point of creating it here.

        The description is what a listing and `jarvis wo show` display. The manager's own
        briefing is composed at dispatch by `dispatch.build_worker_prompt`, which reads
        the feature's ask and the LIVE list of children rather than a snapshot taken now —
        a manager files further children as the feature runs, so a snapshot would be wrong
        by its second turn.
        """
        fo = self.get_feature_order(fo_id)
        return self.create_work_order(
            title=f"Manage {fo['title']}",
            description=(
                f"Own the follow-through for feature order {fo_id} ({fo['title']}).\n\n"
                f"This work order writes no product code and opens no pull request. It "
                f"receives what the feature needs decided, acts on each message, and "
                f"files ordinary work orders under the feature when something has to "
                f"change. Between messages it is idle, and that is correct."
            ),
            origin="jarvis",
            parent_id=fo_id,
            kind="manager",
        )

    def feature_summary(self) -> dict[str, Any]:
        """How many feature orders sit in each status, and how many want the user."""
        by_status = {
            r["status"]: int(r["n"]) for r in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM feature_orders GROUP BY status"
            ).fetchall()
        }
        attention = self.conn.execute(
            "SELECT COUNT(*) c FROM feature_orders WHERE needs_attention=1"
        ).fetchone()["c"]
        return {"by_status": by_status, "needs_attention": int(attention)}

    # -- events --------------------------------------------------------------

    def add_event(self, wo_id: str, kind: str, payload: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO wo_events (wo_id, ts, kind, payload) VALUES (?,?,?,?)",
            (wo_id, db.now(), kind, db.to_json(payload or {})),
        )

    def events_of_kind(self, wo_id: str, kind: str) -> list[dict[str, Any]]:
        """Every event of ONE kind on this work order, oldest first and UNCAPPED.

        `list_events` takes the oldest `limit` rows, which is right for a timeline and
        wrong for counting: a chatty work order would push the rows a counter cares
        about off the end and quietly report zero. The kind filter is what makes an
        uncapped read safe here.
        """
        rows = self.conn.execute(
            "SELECT * FROM wo_events WHERE wo_id=? AND kind=? ORDER BY ts",
            (wo_id, kind)).fetchall()
        return db.rows_to_dicts(rows)

    def events_across(self, kind: str, limit: int = 200) -> list[dict[str, Any]]:
        """Every event of ONE kind in the project, NEWEST first, with its work order.

        `events_of_kind` answers "what happened to this order". A fleet-wide review
        surface asks the opposite question, and without this it would have to read
        every work order to find the handful that carry the event at all.

        The work-order columns come along because the row is unreadable without them:
        an alarm is about a title and a status, not about an id. Hidden orders are
        included and marked — hiding takes an order out of the listings that compete
        for attention, and this page is the record of what it cost, not a listing.
        """
        rows = self.conn.execute(
            "SELECT e.ts AS ts, e.kind AS kind, e.payload AS payload,"
            " w.id AS wo_id, w.title AS title, w.status AS status,"
            " w.hidden AS hidden, w.needs_attention AS needs_attention,"
            " w.attention_reason AS attention_reason"
            " FROM wo_events e JOIN work_orders w ON w.id = e.wo_id"
            " WHERE e.kind=? ORDER BY e.ts DESC LIMIT ?", (kind, limit)).fetchall()
        return db.rows_to_dicts(rows)

    # -- cost alarms ---------------------------------------------------------

    def add_alarm(self, wo_id: str, kind: str, seq: int, reason: str) -> dict[str, Any]:
        """Record one raised alarm and return it. The caller still writes the event.

        Both, not one: the row is the identity everything downstream hangs off, and the
        `cost_alarm` event remains the raise's dedupe memory and the work order's
        timeline entry. See ALARM_EVENT_KINDS for the payloads of all four kinds.
        """
        alarm_id = db.new_id("al")
        self.conn.execute(
            """INSERT INTO wo_alarms (id, wo_id, ts, kind, seq, reason)
               VALUES (?,?,?,?,?,?)""",
            (alarm_id, wo_id, db.now(), kind, int(seq), reason),
        )
        return self.get_alarm(alarm_id)

    def add_finding(self, wo_id: str, *, kind: str, reason: str, seq: int = NO_TURN,
                    source: str = "cost", probe: str | None = None,
                    subject_kind: str = "work_order",
                    fo_id: str | None = None) -> dict[str, Any]:
        """Record one finding — the general raise, of which `add_alarm` is the cost case.

        A second entry point rather than a widened `add_alarm`, because `add_alarm`'s one
        call site sits inside `Daemon.check_burning_turns`' `(kind, seq)` dedupe, which
        §1 of docs/superpowers/specs/2026-08-31-the-supervisor.md spent a section
        protecting: moved or re-signatured, every cost alarm re-raises on every reconcile
        tick for the life of the turn.

        `wo_id` is always the CARRIER. For a feature-order subject that is
        `carrier_for_feature(fo_id)`, and the pairing below is the whole of the
        constraint the schema could not carry.
        """
        assert subject_kind in ALARM_SUBJECTS, subject_kind
        assert source in ALARM_SOURCES, source
        if (subject_kind == "feature_order") != bool(fo_id):
            raise ValueError(
                f"subject_kind={subject_kind!r} and fo_id={fo_id!r} disagree: "
                "a feature_order finding needs an fo_id, and a work_order one has none")
        alarm_id = db.new_id("al")
        self.conn.execute(
            """INSERT INTO wo_alarms (id, wo_id, ts, kind, seq, reason,
                                      subject_kind, fo_id, source, probe)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (alarm_id, wo_id, db.now(), kind, int(seq), reason,
             subject_kind, fo_id, source, probe),
        )
        return self.get_alarm(alarm_id)

    def get_alarm(self, alarm_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM wo_alarms WHERE id=?", (alarm_id,)).fetchone()
        if row is None:
            raise KeyError(alarm_id)
        return dict(row)

    def alarm_for_question(self, question_id: int) -> dict[str, Any] | None:
        """The alarm this Neo question was escalated from, if any.

        The third of `approval_for_question`'s family and it exists for the same reason:
        Neo's database is OS-wide and knows nothing about a project's tables, so the
        back-link is resolved from this side.
        """
        row = self.conn.execute(
            "SELECT * FROM wo_alarms WHERE neo_question_id=?", (question_id,)).fetchone()
        return dict(row) if row else None

    def alarm_for_remedy_approval(self, approval_id: int) -> dict[str, Any] | None:
        """The alarm whose proposed remedy this `self_heal` approval authorises, if any.

        `alarm_for_question`'s sibling, resolved from this side for the same reason:
        `gates.apply_decision` is handed an approval and has no other way back to the
        alarm the verdict is really about (§5).
        """
        row = self.conn.execute(
            "SELECT * FROM wo_alarms WHERE remedy_approval_id=?",
            (approval_id,)).fetchone()
        return dict(row) if row else None

    def alarms_of(self, wo_id: str) -> list[dict[str, Any]]:
        """Every alarm on one work order, oldest first.

        By CARRIER, so a feature finding appears on the order that carried it — which is
        where its record belongs and where `jarvis wo show` reads it back.
        """
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM wo_alarms WHERE wo_id=? ORDER BY ts", (wo_id,)).fetchall())

    def alarms_for_feature(self, fo_id: str) -> list[dict[str, Any]]:
        """Every finding ABOUT one feature order, oldest first, whatever carried it.

        The counterpart of `alarms_of`: a feature reached through two carriers has its
        findings in two places, and this is the read that puts them back together.
        """
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM wo_alarms WHERE fo_id=? ORDER BY ts", (fo_id,)).fetchall())

    def alarms_across(self, limit: int = 200, statuses: tuple[str, ...] | None = None,
                      wo_id: str | None = None, fo_id: str | None = None,
                      sources: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Every alarm in the project, NEWEST first, with its work order.

        The work-order columns are the same ones `events_across` brings along and for
        the same reason — an alarm is about a title and a status, not about an id — and
        `ops.list_cost_alarms` builds its dict straight from them, so the two reads must
        not diverge. Hidden orders are included and marked: this is the record of what
        the fleet spent, not a listing competing for attention.

        `statuses` is what makes this the supervisor's work queue as well as the review
        surface's read.

        `status` IS THE WORK ORDER'S and `alarm_status` is the row's own, matching what
        `ops.list_cost_alarms` has published since PR 159. The two tables both have a
        `status`, and `SELECT a.*, w.*` would have silently handed one of them to every
        caller depending on column order, so both are spelled out.

        THE FEATURE JOIN IS UNCONDITIONAL AND `fo_id` IS A `WHERE` FILTER, NOT A JOIN
        SWITCH. `ops._find_alarm` and `ops.list_cost_alarms`' unfiltered path — which
        together feed the alarm page, the badge, `/alarms/{project}/{alarm_id}` and
        `jarvis alarms show` — never pass `fo_id`, so joining only when it is supplied
        would leave every one of those surfaces rendering the CARRIER's title.
        """
        where = ["1=1"]
        args: list[Any] = []
        if statuses:
            where.append(f"a.status IN ({','.join('?' for _ in statuses)})")
            args.extend(statuses)
        if wo_id:
            # Filtered here rather than by the caller after the fact: `limit` is applied
            # by SQLite, so a post-filter over a busy project's newest 200 could return
            # nothing for an order that has alarms.
            where.append("a.wo_id=?")
            args.append(wo_id)
        if fo_id:
            where.append("a.fo_id=?")
            args.append(fo_id)
        if sources:
            where.append(f"a.source IN ({','.join('?' for _ in sources)})")
            args.extend(sources)
        rows = self.conn.execute(
            "SELECT a.id AS id, a.wo_id AS wo_id, a.ts AS ts, a.kind AS kind,"
            " a.seq AS seq, a.reason AS reason, a.status AS alarm_status,"
            " a.claimed_at AS claimed_at, a.attempts AS attempts,"
            " a.verdict AS verdict, a.verdict_reason AS verdict_reason,"
            " a.note AS note, a.decided_at AS decided_at,"
            " a.neo_question_id AS neo_question_id,"
            " a.review_status AS review_status, a.review_feedback AS review_feedback,"
            " a.reviewed_at AS reviewed_at,"
            " a.subject_kind AS subject_kind, a.fo_id AS fo_id,"
            " a.source AS source, a.probe AS probe,"
            " a.remedy AS remedy, a.remedy_argument AS remedy_argument,"
            " a.remedy_approval_id AS remedy_approval_id,"
            " w.title AS title, w.status AS status, w.hidden AS hidden,"
            " w.needs_attention AS needs_attention,"
            " w.attention_reason AS attention_reason,"
            " f.title AS fo_title, f.status AS fo_status"
            " FROM wo_alarms a JOIN work_orders w ON w.id = a.wo_id"
            " LEFT JOIN feature_orders f ON f.id = a.fo_id"
            f" WHERE {' AND '.join(where)} ORDER BY a.ts DESC LIMIT ?",
            (*args, limit)).fetchall()
        return db.rows_to_dicts(rows)

    def claim_next_alarm(self) -> dict[str, Any] | None:
        """Atomically claim the OLDEST alarm still `raised`, or None.

        FIFO keeps the supervisor's byte-stable prompt prefix inside the cache TTL, and
        the oldest alarm is also the one closest to expiring unjudged.
        """
        now = db.now()
        cur = self.conn.execute(
            """UPDATE wo_alarms SET status='reviewing', claimed_at=?
               WHERE id = (SELECT id FROM wo_alarms WHERE status='raised'
                             AND COALESCE(retry_after, 0) <= ?
                           ORDER BY ts LIMIT 1)
               RETURNING *""",
            (now, now),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def release_alarm_claim(self, alarm_id: str, reason: str,
                            max_attempts: int = 3) -> str:
        """Hand a claimed alarm back after a review call that never happened.

        `NeoStore.release_claim`'s twin, and deliberately the same shape — the two queues
        fail the same way and must recover the same way. Returns `"raised"` (retryable,
        `attempts` incremented, held by `retry_after`) or `"failed"` (spent, and now the
        user's).

        `attempts` COUNTS RETRIES GRANTED, not calls made — `reclaim_stale_alarms`'
        reading of the same column, and the two must agree or one of them silently
        shortens the other's ladder.

        THE HOLD IS WHAT MAKES IT A RETRY. `Daemon._drain_project_alarms` claims in a
        `while True`, so an alarm returned to `raised` with no delay is re-claimed on the
        next iteration and burns the whole ceiling inside one tick.
        """
        row = self.conn.execute("SELECT attempts FROM wo_alarms WHERE id=?",
                                (alarm_id,)).fetchone()
        if row is None:
            return "failed"
        attempts = int(row["attempts"] or 0)
        if attempts >= max_attempts:
            self.update_alarm(
                alarm_id, status="failed", claimed_at=None, decided_at=db.now(),
                verdict_reason=f"{reason} (after {attempts} retries — nobody has "
                               f"judged this)")
            return "failed"
        self.conn.execute(
            "UPDATE wo_alarms SET status='raised', claimed_at=NULL, "
            "attempts=attempts+1, retry_after=?, verdict_reason=? WHERE id=?",
            (db.now() + ALARM_RETRY_BACKOFF_SECONDS[
                min(attempts, len(ALARM_RETRY_BACKOFF_SECONDS) - 1)],
             reason, alarm_id))
        return "raised"

    def reclaim_stale_alarms(self, older_than: float,
                             max_attempts: int) -> dict[str, list[str]]:
        """Unstick alarms parked in `reviewing` by a drain that never finished.

        SHIPPED WITH `claim_next_alarm`, NOT AFTER IT: `NeoStore.claim_next` went out
        without its counterpart and a daemon restart mid-drain parked a question for ever
        (bl-3f5f1464). Past `older_than` a row goes back to `raised` with `attempts`
        incremented; at `max_attempts` it is `failed` instead — out of the queue, not
        looping, and still flagged.

        Both bounds come from `catalog.SupervisorConfig`. Returns
        {"requeued": [alarm id, ...], "failed": [...]}.
        """
        cutoff = db.now() - older_than
        # Give up FIRST, then re-queue — `NeoStore.reclaim_stale`'s ordering and its
        # reason: the other way round increments a row to the ceiling and then fails it
        # in the same call, spending an attempt the alarm never got to use.
        failed = [
            str(r["id"])
            for r in self.conn.execute(
                """UPDATE wo_alarms
                      SET status='failed',
                          verdict_reason='the supervisor never finished: stranded in '
                                         || 'reviewing after ' || attempts
                                         || ' reclaim attempt(s)'
                    WHERE status='reviewing' AND attempts >= ?
                      AND COALESCE(claimed_at, ts) < ?
                RETURNING id""",
                (max_attempts, cutoff),
            ).fetchall()
        ]
        requeued = [
            str(r["id"])
            for r in self.conn.execute(
                """UPDATE wo_alarms SET status='raised', claimed_at=NULL,
                                        attempts=attempts + 1
                    WHERE status='reviewing' AND COALESCE(claimed_at, ts) < ?
                RETURNING id""",
                (cutoff,),
            ).fetchall()
        ]
        return {"requeued": requeued, "failed": failed}

    def update_alarm(self, alarm_id: str, **fields: Any) -> None:
        for column, vocabulary in (("status", ALARM_STATUSES),
                                   ("verdict", ALARM_VERDICTS),
                                   ("review_status", ALARM_REVIEW_STATUSES)):
            value = fields.get(column)
            if value is not None:
                assert value in vocabulary, (column, value)
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE wo_alarms SET {cols} WHERE id=?", (*fields.values(), alarm_id))

    # -- health_reviews: the ledger of LOOKING, not of finding (§4) ------------------

    def record_health_review(self, subject_kind: str, subject_id: str, *,
                             fingerprint: str, trigger: str, outcome: str,
                             findings: int = 0, detail: str = "") -> int:
        """One sweep, whatever it concluded. Returns the row id."""
        assert subject_kind in ALARM_SUBJECTS, subject_kind
        assert outcome in HEALTH_OUTCOMES, outcome
        cur = self.conn.execute(
            """INSERT INTO health_reviews (ts, subject_kind, subject_id, fingerprint,
                                           trigger, outcome, findings, detail)
               VALUES (?,?,?,?,?,?,?,?)""",
            (db.now(), subject_kind, subject_id, fingerprint, trigger, outcome,
             int(findings), detail),
        )
        return int(cur.lastrowid or 0)

    def last_health_review(self, subject_kind: str,
                           subject_id: str) -> dict[str, Any] | None:
        """The most recent sweep of one unit, or None if it has never been looked at.

        A `failed` sweep is EXCLUDED. `health.due` compares the recorded fingerprint
        against the live one to decide whether to look again, and a failure recorded a
        fingerprint nobody judged: counted as a look, an unreadable reply would suppress
        the retry §4 requires.
        """
        row = self.conn.execute(
            """SELECT * FROM health_reviews
                WHERE subject_kind=? AND subject_id=? AND outcome!='failed'
             ORDER BY ts DESC, id DESC LIMIT 1""",
            (subject_kind, subject_id),
        ).fetchone()
        return dict(row) if row else None

    def last_health_attempt_ts(self, subject_kind: str,
                               subject_id: str) -> float | None:
        """When this unit was last SWEPT, whatever came of it — or None if never.

        The sibling of `last_health_review` and deliberately not a widening of it: that
        one answers "what was the last judgement", which must skip a `failed` row, and
        this answers "when did we last spend a call", which must not. `health.due` needs
        both and they disagree exactly when the sweep is broken — which is when the
        difference is worth money (issue #216).
        """
        row = self.conn.execute(
            """SELECT ts FROM health_reviews
                WHERE subject_kind=? AND subject_id=?
             ORDER BY ts DESC, id DESC LIMIT 1""",
            (subject_kind, subject_id),
        ).fetchone()
        return float(row["ts"]) if row else None

    def health_reviews_of(self, subject_kind: str,
                          subject_id: str) -> list[dict[str, Any]]:
        """Every sweep of one unit, oldest first."""
        return db.rows_to_dicts(self.conn.execute(
            """SELECT * FROM health_reviews WHERE subject_kind=? AND subject_id=?
             ORDER BY ts, id""", (subject_kind, subject_id)).fetchall())

    def recent_health_reviews(self, limit: int) -> list[dict[str, Any]]:
        """The project's last `limit` sweeps, newest first, of every unit and outcome.

        Across subjects on purpose: a sweep that cannot produce a judgement is broken
        for the PROJECT, not for one work order, and asking per unit would need a run
        long enough on a single order — which is exactly the unit that then settles and
        takes the evidence with it.
        """
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM health_reviews ORDER BY ts DESC, id DESC LIMIT ?",
            (int(limit),)).fetchall())

    def probes_reported_at(self, subject_kind: str, subject_id: str,
                           fingerprint: str) -> set[str]:
        """Which probes a sweep has already reported about this unit AT THIS FINGERPRINT.

        THE DEDUPE PREDICATE, and it deliberately says nothing about an alarm's STATUS.
        Keying on "no open alarm for this probe" would re-raise the moment the supervisor
        acks one — which is every time the supervisor works correctly, and is how the
        attention flag comes back the instant the user puts it down (§4). A health
        finding has no turn to key on the way a cost alarm does; an unchanged fingerprint
        is what says nothing has happened since.
        """
        found: set[str] = set()
        for row in self.conn.execute(
                """SELECT detail FROM health_reviews
                    WHERE subject_kind=? AND subject_id=? AND fingerprint=?
                      AND outcome='findings'""",
                (subject_kind, subject_id, fingerprint)).fetchall():
            found.update(p for p in str(row["detail"] or "").split(",") if p)
        return found

    def _this_episode(self, wo_id: str, repair: str, kind: str) -> list[dict[str, Any]]:
        """Events of `kind` since this EPISODE began — the last clear or re-arm.

        Repair state is derived from the timeline rather than kept in columns; the clear
        is the budget reset. See §4 of
        docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md.

        `repair` is `ops.PrRepair.name` — "conflict" or "checks" — passed as a string
        because a store may not import `ops`. The two episodes are counted APART on
        purpose: a branch that conflicted twice last week has spent nothing of the red
        build's budget, and they are different problems with different fixes.

        TWO EVENTS OPEN A FRESH EPISODE, not one. `pr_<repair>_rearmed` is the budget
        given back because the attempts were never made — the worker was refused by a
        pending gate before it could read the conflict (issue #469, §4 and §6 of
        docs/superpowers/specs/2026-09-19-an-attempt-the-worker-could-not-make.md). It
        moves the boundary here rather than at each caller so that the attempt count,
        the give-up and the origin all reset together and cannot disagree about which
        episode is current.

        The second read happens only once there is something to count: a pull request
        that has never been nudged returns above it, which is what keeps the green case
        at the cost `test_a_green_pull_request_costs_one_call_three_reads_and_no_write`
        pins.
        """
        rows = self.events_of_kind(wo_id, kind)
        if not rows:
            return []
        opened = (self.events_of_kind(wo_id, f"pr_{repair}_cleared")
                  + self.events_of_kind(wo_id, f"pr_{repair}_rearmed"))
        since = max((e["ts"] for e in opened), default=0.0)
        return [r for r in rows if r["ts"] > since]

    def pr_repair_nudges(self, wo_id: str, repair: str) -> list[dict[str, Any]]:
        """Every ask of this episode, oldest first — the attempts themselves.

        `pr_repair_attempts` is the count; this is what `ops.rearm_pr_repair` needs
        instead, because WHEN each one went out is what says whether the worker was
        allowed to act on it (issue #469).
        """
        return self._this_episode(wo_id, repair, f"pr_{repair}_nudged")

    def pr_repair_attempts(self, wo_id: str, repair: str) -> int:
        """How many times the OS has asked this worker to fix the SAME thing."""
        return len(self.pr_repair_nudges(wo_id, repair))

    def pr_repair_deferred(self, wo_id: str, repair: str, approval_id: int) -> bool:
        """Has this episode already recorded that THIS gate is holding the repair up?

        The dedupe behind `ops.defer_pr_repair`: the poll asks every tick for as long as
        the review takes, and the timeline must carry the fact once, not once per tick.
        """
        for event in self._this_episode(wo_id, repair, f"pr_{repair}_deferred"):
            if (db.from_json(event["payload"], {}) or {}).get("approval_id") == approval_id:
                return True
        return False

    def pr_repair_gave_up(self, wo_id: str, repair: str) -> bool:
        """Has the OS already stopped trying on this episode and said so?

        Not "are the attempts spent": this is what keeps the give-up event to one per
        episode on a work order that may be flagged for something else entirely.
        """
        return bool(self._this_episode(wo_id, repair, f"pr_{repair}_unresolved"))

    def pr_closure_told(self, wo_id: str) -> bool:
        """Has the user already been told about THIS closure of the pull request?

        Episode arithmetic again, and for the reason `_this_episode` gives: a
        `pr_closed` newer than the newest `pr_reopened`. NOT `pr_state == 'CLOSED'`,
        which kn-dbc4971d records as stale by construction — nothing ever cleared it, so
        a pull request closed, reopened and closed again still reads CLOSED and the
        second refusal would never reach the user. That is issue #224's silence with a
        different cause, which is a poor thing to reintroduce while fixing it.

        `Daemon.poll_pull_requests` is the only writer of both events, and it now also
        clears the column when it writes `pr_reopened`, so the stale reader this
        replaces has one fewer way to be wrong too.
        """
        closed = self.events_of_kind(wo_id, "pr_closed")
        if not closed:
            return False
        reopened = self.events_of_kind(wo_id, "pr_reopened")
        return not reopened or closed[-1]["ts"] > reopened[-1]["ts"]

    def pr_repair_origin(self, wo_id: str, repairs: tuple[str, ...]) -> str | None:
        """The status the newest OPEN repair episode took this work order away from.

        `Daemon.settle_work_order` parks any done turn carrying a summary and a pull
        request into `waiting_pr_merge`. That is right for a work order that WAS parked,
        and wrong for one the OS pulled out of `needs_review` to fix a red build: the
        repair would end by silently downgrading a review item into a merge-queue entry
        (Neo question 275, spec §4).

        Only an OPEN episode answers — a cleared one is a problem that is over, and its
        origin must not outlive it. Derived from the timeline for the reason the attempt
        count is: there is no column to drift from what the user reads.
        """
        newest: dict[str, Any] | None = None
        for repair in repairs:
            rows = self._this_episode(wo_id, repair, f"pr_{repair}_nudged")
            if rows and (newest is None or rows[-1]["ts"] > newest["ts"]):
                newest = rows[-1]
        if newest is None:
            return None
        return (db.from_json(newest["payload"], {}) or {}).get("was") or None


    def work_abandoned(self, wo_id: str) -> bool:
        """Has the worker declared this work deliberately dropped, and not since landed?

        THE NEWEST OF THE TWO EVENTS WINS, which is the whole rule. `ops.finish` writes
        `finished` on every route and `abandoned` only when asked, in that order — so an
        abandonment stands until an ordinary finish supersedes it, and a work order that
        was abandoned and later delivered properly is judged again. Same episode
        arithmetic as `pr_conflict_gave_up` above and the same reason: a decision is
        current only until the thing it was about happens again.

        On the store because `ops` (which excuses a finish) and `invariants` (which
        excuses a sweep) both need it and `invariants` cannot import `ops`. Issue #232.
        """
        dropped = self.events_of_kind(wo_id, "abandoned")
        if not dropped:
            return False
        delivered = self.events_of_kind(wo_id, "finished")
        return not delivered or float(dropped[-1]["ts"]) >= float(delivered[-1]["ts"])

    def work_unlanded_open(self, wo_id: str, closed_by: str = "") -> bool:
        """Did a landing refuse this work order, with nothing since answering it?

        The episode arithmetic `work_abandoned` uses, over a wider set: the refusal
        stands until the work order is DELIVERED again (`finished`, which every route
        through `ops.finish` writes), dropped on purpose (`abandoned`) or landed by a
        merge (`pr_merged`).

        `invariants.true_blockers` needs this, and needs it derived from the record
        rather than from the repository. A flag that function cannot re-derive is
        relabelled by INV-ATTENTION-REASON on the next tick — kn-eafe383a is that exact
        bug, one level over — and re-reading git for every open work order on every tick
        is the cost `landing`'s two-speed split exists to avoid.

        `closed_by` narrows it to the landings written by ONE route — in practice
        `marked_done`, which is `ops.mark_done` recording that the user closed an order
        over work that never landed. That is a decision, exactly as `abandoned` is, and
        `invariants.check_work_lands` has to read it as one or the sweep re-reports an
        order the user already closed, every hour, for ever (issue #232). It shares this
        method rather than copying the arithmetic, because "and nothing since answered
        it" is the half that would rot in a second copy.
        """
        parked = [e for e in self.events_of_kind(wo_id, "work_unlanded")
                  if not closed_by
                  or (db.from_json(e["payload"], {}) or {}).get("closed_by") == closed_by]
        if not parked:
            return False
        since = float(parked[-1]["ts"])
        return not any(
            float(e["ts"]) > since
            for kind in ("finished", "abandoned", "pr_merged")
            for e in self.events_of_kind(wo_id, kind))

    def count_events(self, wo_id: str, exclude: tuple[str, ...] = ()) -> int:
        """How many events this work order has, unbounded, minus the kinds named.

        `list_events` caps at 200, which is fine for a page and wrong for
        `health.fingerprint`: a busy order's count would freeze at the cap and the unit
        would read as motionless for ever (§4). `exclude` is there for the same caller —
        see `health.fingerprint` for why it must not count the OS looking.
        """
        holes = ",".join("?" for _ in exclude)
        clause = f" AND kind NOT IN ({holes})" if exclude else ""
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM wo_events WHERE wo_id=?{clause}",
            (wo_id, *exclude)).fetchone()[0])

    def list_events(self, wo_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_events WHERE wo_id=? ORDER BY ts LIMIT ?", (wo_id, limit)
        ).fetchall()
        return db.rows_to_dicts(rows)

    # -- messages (user feedback queue) ---------------------------------------

    def queue_message(self, wo_id: str, content: str, source: str = "jarvis",
                      direction: str = "user_to_agent", status: str = "queued",
                      authored_by: str = "") -> int:
        """Put one message on the work order's queue. Returns its row id.

        `authored_by` defaults to unattributed and every machine caller leaves it there.
        It is NOT a claim a caller gets to make freely: `ops.send_message` is the only
        thing that ever passes `MESSAGE_AUTHOR_USER`, and only after proving the calling
        process is not a dispatched worker session — see its docstring, and §2 of
        docs/superpowers/specs/2026-09-11-a-gate-request-carries-the-users-words.md.
        """
        cur = self.conn.execute(
            "INSERT INTO wo_messages (wo_id, ts, direction, content, source, status, "
            "authored_by) VALUES (?,?,?,?,?,?,?)",
            (wo_id, db.now(), direction, content, source, status, authored_by),
        )
        return int(cur.lastrowid)

    def record_agent_reply(self, wo_id: str, content: str, source: str = "worker") -> int:
        """Persist a worker's final assistant message into the work order record.

        The work order is the representation of the worker's conversation: the user and
        Neo decide from it and never open the session, so the full reply is stored, not
        just the `wo finish --summary` headline.
        """
        return self.queue_message(wo_id, content, source=source,
                                  direction="agent_to_user", status="delivered")

    def agent_replies(self, wo_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_messages WHERE wo_id=? AND direction='agent_to_user' ORDER BY ts",
            (wo_id,),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def queued_messages(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        if wo_id:
            rows = self.conn.execute(
                "SELECT * FROM wo_messages WHERE status='queued' AND direction='user_to_agent' AND wo_id=? ORDER BY ts",
                (wo_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM wo_messages WHERE status='queued' AND direction='user_to_agent' ORDER BY ts"
            ).fetchall()
        return db.rows_to_dicts(rows)

    def mark_message(self, msg_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE wo_messages SET status=?, delivered_at=? WHERE id=?",
            (status, db.now() if status == "delivered" else None, msg_id),
        )

    def deliverable_messages(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """`queued_messages` minus the ones held by a delivery backoff.

        A SEPARATE READER rather than a filter inside `queued_messages`, because the two
        questions are different and one of them is an invariant's. `invariants.stuck_message`
        asks what is waiting and must still see a held message — that is precisely the
        message it exists to notice — while the daemon asks what it may send NOW.
        """
        now = db.now()
        return [m for m in self.queued_messages(wo_id)
                if float(m.get("retry_after") or 0.0) <= now]

    def record_delivery_failure(
            self, msg_id: int, error: str,
            max_attempts: int = MAX_MESSAGE_DELIVERY_ATTEMPTS) -> str:
        """One failed attempt to put a queued message into its worker's conversation.

        Returns `"queued"` (held by `retry_after`, still the worker's to receive) or
        `"failed"` (the attempts are spent and the message will not be delivered).

        THE MESSAGE IS NOT THE OS'S TO DISCARD. What this replaces marked it `failed` on
        the first `ClaudeCliError`, so a blip in the transport silently swallowed whatever
        the user had just typed at a worker and nothing ever re-sent it.
        """
        row = self.conn.execute("SELECT attempts FROM wo_messages WHERE id=?",
                                (msg_id,)).fetchone()
        if row is None:
            return "failed"
        attempts = int(row["attempts"] or 0) + 1
        if attempts >= max_attempts:
            self.conn.execute(
                "UPDATE wo_messages SET status='failed', attempts=?, last_error=? "
                "WHERE id=?", (attempts, error, msg_id))
            return "failed"
        delay = MESSAGE_RETRY_BACKOFF_SECONDS[
            min(attempts - 1, len(MESSAGE_RETRY_BACKOFF_SECONDS) - 1)]
        self.conn.execute(
            "UPDATE wo_messages SET attempts=?, retry_after=?, last_error=? WHERE id=?",
            (attempts, db.now() + delay, error, msg_id))
        return "queued"

    def list_messages(self, wo_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_messages WHERE wo_id=? ORDER BY ts LIMIT ?", (wo_id, limit)
        ).fetchall()
        return db.rows_to_dicts(rows)

    def user_messages(self, wo_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Inbound messages this work order can PROVE the user wrote, newest first.

        The narrow half of `list_messages`, and the narrowness is the whole point: what
        reads this renders the user's own words into a privileged-action request, so a
        row a worker could have written must not be in it. `authored_by` is matched
        exactly, so an unstamped legacy row is excluded rather than grandfathered in.
        """
        rows = self.conn.execute(
            "SELECT * FROM wo_messages WHERE wo_id=? AND direction='user_to_agent' "
            "AND authored_by=? ORDER BY ts DESC, id DESC LIMIT ?",
            (wo_id, MESSAGE_AUTHOR_USER, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    # -- envelopes (the message bus) --------------------------------------------

    def post_envelope(self, *, from_role: str, to_role: str, kind: str,
                      payload: dict[str, Any] | None = None,
                      subject_wo_id: str | None = None,
                      subject_fo_id: str | None = None) -> int:
        """Queue one envelope. Returns its id.

        Never resolves anything: who fills `to_role` is the router's question and it is
        asked at delivery, not here (see bus.post). `delivered_wo_id` is left NULL for
        the router to write, and there is deliberately no parameter for it.
        """
        assert from_role in ENVELOPE_ROLES, from_role
        assert to_role in ENVELOPE_ROLES, to_role
        assert kind in ENVELOPE_KINDS, kind
        if bool(subject_wo_id) == bool(subject_fo_id):
            raise ValueError("an envelope has exactly one subject: a work order or a "
                             "feature order, never both and never neither")
        cur = self.conn.execute(
            """INSERT INTO envelopes (ts, subject_wo_id, subject_fo_id, from_role,
                                      to_role, kind, payload)
               VALUES (?,?,?,?,?,?,?)""",
            (db.now(), subject_wo_id, subject_fo_id, from_role, to_role, kind,
             db.to_json(payload or {})),
        )
        return int(cur.lastrowid)

    def queued_envelopes(self, limit: int = 200) -> list[dict[str, Any]]:
        """Undelivered envelopes, oldest first.

        Ordered by `(ts, id)` rather than `ts` alone: two envelopes posted in the same
        transaction can share a timestamp to the microsecond, and the whole promise of
        this queue is that a subject's messages arrive in the order they were posted.
        """
        rows = self.conn.execute(
            "SELECT * FROM envelopes WHERE state='queued' ORDER BY ts, id LIMIT ?",
            (limit,),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def mark_envelope(self, env_id: int, state: str, *,
                      delivered_wo_id: str | None = None, note: str = "") -> None:
        assert state in ENVELOPE_STATES, state
        fields: dict[str, Any] = {"state": state, "note": note}
        if delivered_wo_id is not None:
            fields["delivered_wo_id"] = delivered_wo_id
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE envelopes SET {cols} WHERE id=?",
                          (*fields.values(), env_id))

    def bump_envelope_attempt(self, env_id: int) -> None:
        """Count one routing attempt, successful or not.

        Committed on its own, deliberately OUTSIDE the delivery transaction below: an
        attempt that fails rolls the delivery back, and if the count went with it a
        permanently unroutable envelope would retry for ever and INV-ENVELOPE-STUCK
        would never see it.
        """
        self.conn.execute(
            "UPDATE envelopes SET attempts = attempts + 1 WHERE id=?", (env_id,))

    def deliver_envelope(self, env_id: int, wo_id: str, content: str,
                         source: str = "bus", note: str = "") -> int:
        """Hand one envelope to a work order's message queue. ONE transaction.

        The `wo_messages` insert and the state change commit together or not at all. A
        daemon that dies between them would otherwise redeliver an envelope the worker
        has already been sent; dying before either redelivers the whole envelope, which
        is correct and is what the queue already guarantees for `jarvis wo send`.
        """
        with db.write_transaction(self.conn):
            msg_id = self.queue_message(wo_id, content, source=source)
            self.conn.execute(
                "UPDATE envelopes SET state='delivered', delivered_wo_id=?, note=?, "
                "delivered_msg_id=? WHERE id=?", (wo_id, note, msg_id, env_id))
        return msg_id

    def envelopes(self, subject_wo_id: str | None = None,
                  subject_fo_id: str | None = None,
                  limit: int = 200) -> list[dict[str, Any]]:
        """One subject's envelopes, oldest first. No subject means all of them."""
        conds, params = [], []
        if subject_wo_id:
            conds.append("subject_wo_id=?"); params.append(subject_wo_id)
        if subject_fo_id:
            conds.append("subject_fo_id=?"); params.append(subject_fo_id)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        rows = self.conn.execute(
            f"SELECT * FROM envelopes{where} ORDER BY ts, id LIMIT ?", (*params, limit)
        ).fetchall()
        return db.rows_to_dicts(rows)

    def manager_work_order(self, fo_id: str) -> dict[str, Any] | None:
        """The work order that owns this feature's follow-through, whatever its status.

        One is created with the children when a plan is released and
        `os.validation.enabled` is on (`create_plan_children`), so a feature planned with
        validation off has none and the router treats that as an unfilled role — which is
        every feature today. The status is NOT filtered here:
        the router has to tell "no manager was ever created" apart from "the manager was
        cancelled while its feature is still open", and those two are different verdicts.
        """
        row = self.conn.execute(
            "SELECT * FROM work_orders WHERE parent_id=? AND kind='manager' "
            "ORDER BY created_at LIMIT 1", (fo_id,)
        ).fetchone()
        return dict(row) if row else None

    def carrier_for_feature(self, fo_id: str) -> dict[str, Any] | None:
        """The work order that carries a record ABOUT this feature: manager, planner,
        newest child, or None.

        A feature order has no timeline and no alarms of its own — `wo_events.wo_id` and
        `wo_alarms.wo_id` are real foreign keys into `work_orders` — so anything said
        about a feature is recorded on whichever work order carried it. This is the
        general rule; `ops.feature_event`'s manager-only carrier is the narrow case of
        it, kept narrow because the validation loop addresses the manager specifically.
        Two rules that disagree would put a feature's record in two places.

        The order of the ladder is longest-lived first: the manager exists for the whole
        feature, the planner for its planning, a child only for its own piece. None means
        the feature has no session at all — a `pending` feature nobody has planned — and
        there is nothing to observe. §1 of
        docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.
        """
        manager = self.manager_work_order(fo_id)
        if manager:
            return manager
        row = self.conn.execute(
            "SELECT w.* FROM feature_orders f JOIN work_orders w ON w.id = f.plan_wo_id"
            " WHERE f.id=?", (fo_id,)).fetchone()
        if row:
            return dict(row)
        # Newest, not oldest: `feature_children` is ordered by the plan's dependency
        # order, and the newest child is the one whose session is likeliest still live.
        row = self.conn.execute(
            "SELECT * FROM work_orders WHERE parent_id=? AND kind='worker'"
            " ORDER BY created_at DESC LIMIT 1", (fo_id,)).fetchone()
        return dict(row) if row else None

    # -- turns (the worker's conversation) --------------------------------------

    def create_turn(self, wo_id: str, kind: str, prompt: str,
                    msg_id: int | None = None, outfile: str = "",
                    errfile: str = "") -> dict[str, Any]:
        """Open a turn row. Written BEFORE the process is spawned, so a turn can never
        be running with nothing on record to reap it."""
        assert kind in TURN_KINDS, kind
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM wo_turns WHERE wo_id=?", (wo_id,)
        ).fetchone()["n"]
        cur = self.conn.execute(
            """INSERT INTO wo_turns (wo_id, seq, kind, msg_id, prompt, started_at,
                                     outfile, errfile)
               VALUES (?,?,?,?,?,?,?,?)""",
            (wo_id, seq, kind, msg_id, prompt, db.now(), outfile, errfile),
        )
        return self.get_turn(int(cur.lastrowid))  # type: ignore[arg-type,return-value]

    def get_turn(self, turn_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM wo_turns WHERE id=?", (turn_id,)).fetchone()
        return dict(row) if row else None

    def set_turn_pid(self, turn_id: int, pid: int | None,
                     unit: str | None = None) -> None:
        """Record how to reach the turn's process. Both halves land together: the unit
        is useless without the row and the pid alone cannot stop a cgroup."""
        self.conn.execute("UPDATE wo_turns SET pid=?, unit=? WHERE id=?",
                          (pid, unit, turn_id))

    def finish_turn(self, turn_id: int, state: str, result: str | None = None,
                    error: str | None = None, cost_usd: float | None = None,
                    num_turns: int | None = None,
                    usage_json: str | None = None,
                    terminal_reason: str | None = None,
                    api_error_status: int | None = None,
                    cost_source: str | None = None) -> dict[str, Any]:
        """Settle one turn. EVERY CALLER OWES A `cost_usd`, including the failures.

        This SETs the column rather than coalescing into it, so a caller that omits the
        cost writes NULL over whatever was there and `budget.spent` — which is
        `SUM(cost_usd)` — bills the turn nothing for ever (issue #471). A turn that ran
        long enough to be killed spent real money; the caller with nothing from the CLI
        falls back to the transcript (`worker_session.lost_turn_cost`) rather than
        leaving the argument off.
        """
        assert state in ("done", "failed"), state
        assert cost_source in (None, COST_FROM_ENVELOPE, COST_FROM_TRANSCRIPT), \
            cost_source
        self.conn.execute(
            """UPDATE wo_turns SET state=?, ended_at=?, result=?, error=?, cost_usd=?,
                                   cost_source=?, num_turns=?, usage_json=?,
                                   terminal_reason=?, api_error_status=? WHERE id=?""",
            (state, db.now(), result, error, cost_usd,
             (cost_source or COST_FROM_ENVELOPE) if cost_usd is not None else None,
             num_turns, usage_json, terminal_reason or None, api_error_status, turn_id),
        )
        return self.get_turn(turn_id)  # type: ignore[return-value]

    def set_turn_usage(self, turn_id: int, usage_json: str,
                       cost_usd: float | None = None) -> None:
        """Backfill a settled turn's recorded usage (parsed late from its outfile).

        THE COST RIDES WITH IT. The envelope this re-reads carries `total_cost_usd`, and
        repairing `usage_json` alone left rows that knew what they cost and were still
        summed as $0.00 by the one query the enforcement runs (issue #471). Coalesced
        rather than set: a row whose cost is already known must not be nulled by an
        envelope that lost the field.

        `cost_usd` overrides that reading for a caller that has already worked out this
        turn's SHARE of it — `ops._turn_usage` re-deriving a row whose envelope holds the
        resumed session's running total (issue #470). Both paths agree once the row is
        current; they disagree exactly while it is being repaired.
        """
        cost = cost_usd
        if cost is None:
            cost = (db.from_json(usage_json, None) or {}).get("total_cost_usd")
        self.conn.execute(
            "UPDATE wo_turns SET usage_json=?, cost_usd=COALESCE(?, cost_usd), "
            "cost_source=CASE WHEN ? IS NULL THEN cost_source ELSE ? END WHERE id=?",
            (usage_json, cost, cost, COST_FROM_ENVELOPE, turn_id))

    def latest_turn(self, wo_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM wo_turns WHERE wo_id=? ORDER BY seq DESC LIMIT 1", (wo_id,)
        ).fetchone()
        return dict(row) if row else None

    def turn_usage_before(self, wo_id: str, seq: int) -> dict[str, Any] | None:
        """The usage envelope of the last turn recorded before `seq`, or None.

        The baseline a turn's own spend is measured against: from CLI 2.1.277 a result
        envelope reports the whole resumed session's usage, so the turn before it is
        what makes the newest one a per-turn figure (`claude_cli.derive_turn_usage`).
        Any KIND of turn counts — a `/compact` spends inside the same session and the
        running total contains it.
        """
        row = self.conn.execute(
            "SELECT usage_json FROM wo_turns WHERE wo_id=? AND seq<? AND "
            "usage_json IS NOT NULL ORDER BY seq DESC LIMIT 1", (wo_id, seq)
        ).fetchone()
        envelope = db.from_json(row["usage_json"], None) if row else None
        return envelope if isinstance(envelope, dict) else None

    def running_turns(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_turns WHERE state='running' ORDER BY started_at"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def count_running_turns(self) -> int:
        """How many of this project's turns are in flight — see src/jarvis/fleet.py.

        Deliberately NOT `count_active`: that counts work-order STATUSES, and a work
        order parked in `waiting_input` on a Neo question holds a slot while spending no
        tokens. The fleet cap rations the account, so it counts the thing that draws on
        it (kn-5c32dde8 records the same distinction biting the project-wide cap).
        """
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM wo_turns WHERE state='running'").fetchone()[0])

    def list_turns(self, wo_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_turns WHERE wo_id=? ORDER BY seq LIMIT ?", (wo_id, limit)
        ).fetchall()
        return db.rows_to_dicts(rows)

    def recent_turns(self, wo_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """The conversation's most recent turns, newest first.

        Not `list_turns(...)[-n:]`: that one's LIMIT applies to the ascending scan, so on
        a long conversation it returns the FIRST hundred turns and the tail is exactly
        what is missing. The only reader that wants the tail is the pause streak
        counter (`worker_session.pause_streak`), and it wants it cheap.
        """
        rows = self.conn.execute(
            "SELECT * FROM wo_turns WHERE wo_id=? ORDER BY seq DESC LIMIT ?",
            (wo_id, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    # -- notifications outbox --------------------------------------------------

    def add_notification(self, title: str, body: str = "", level: str = "info",
                         wo_id: str | None = None, source: str = "") -> int:
        assert level in ("info", "warning", "critical"), level
        cur = self.conn.execute(
            "INSERT INTO notifications (ts, level, title, body, wo_id, source) VALUES (?,?,?,?,?,?)",
            (db.now(), level, title, body, wo_id, source),
        )
        return int(cur.lastrowid)

    def unrouted_notifications(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM notifications WHERE status='new' ORDER BY ts"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def mark_notification_routed(self, notif_id: int) -> None:
        self.conn.execute("UPDATE notifications SET status='routed' WHERE id=?", (notif_id,))

    # -- invariant violation reports -------------------------------------------

    def open_violation_report(self, invariant: str, wo_id: str | None = None) -> bool:
        """Note that this violation is standing. True the FIRST time it is seen.

        The caller announces on True and says nothing on False — `invariants.py` rule 3,
        made durable. `seen` and `last_seen` are the audit trail that a per-tick inbox
        row would otherwise have been.
        """
        key = (invariant, wo_id or "")
        now = db.now()
        row = self.conn.execute(
            "SELECT seen FROM violation_reports WHERE invariant=? AND wo_id=?", key
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO violation_reports (invariant, wo_id, first_seen, last_seen)"
                " VALUES (?,?,?,?)", (*key, now, now))
            return True
        self.conn.execute(
            "UPDATE violation_reports SET last_seen=?, seen=seen+1"
            " WHERE invariant=? AND wo_id=?", (now, *key))
        return False

    def close_violation_reports(
            self, standing: Iterable[tuple[str, str | None]]) -> list[tuple[str, str]]:
        """Forget every report whose violation is not in `standing`, so that one which
        comes back is announced again.

        ONLY SOUND WHERE EVERY CHECK RAN — absence otherwise means "not looked at".
        `Daemon.check_invariants` is the one caller and holds that reasoning.
        """
        keys = {(inv, wo or "") for inv, wo in standing}
        gone = [(r["invariant"], r["wo_id"])
                for r in self.conn.execute(
                    "SELECT invariant, wo_id FROM violation_reports").fetchall()
                if (r["invariant"], r["wo_id"]) not in keys]
        for key in gone:
            self.conn.execute(
                "DELETE FROM violation_reports WHERE invariant=? AND wo_id=?", key)
        return gone

    def violation_reports(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM violation_reports ORDER BY first_seen").fetchall()
        return db.rows_to_dicts(rows)

    # -- assumptions -----------------------------------------------------------

    def add_assumption(self, wo_id: str, content: str) -> int:
        n = 1 + int(self.conn.execute(
            "SELECT COUNT(*) FROM assumptions WHERE wo_id=?", (wo_id,)
        ).fetchone()[0])
        cur = self.conn.execute(
            "INSERT INTO assumptions (wo_id, ts, content) VALUES (?,?,?)",
            (wo_id, db.now(), content),
        )
        self.add_event(wo_id, "assumption", {"content": content, "n": n})
        return int(cur.lastrowid)

    def pending_assumptions(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        if wo_id:
            return [a for a in self.all_assumptions(wo_id) if a["status"] == "pending"]
        # Fleet-wide view: assumptions of hidden work orders aren't asking for review.
        # No `n` here — a number only means anything beside its own work order's list.
        rows = self.conn.execute(
            """SELECT a.* FROM assumptions a JOIN work_orders w ON w.id = a.wo_id
               WHERE a.status='pending' AND w.hidden=0 ORDER BY a.ts"""
        ).fetchall()
        return db.rows_to_dicts(rows)

    def all_assumptions(self, wo_id: str) -> list[dict[str, Any]]:
        """Every assumption of a work order, reviewed or not, each numbered from 1.

        `pending_assumptions` answers "what does the user still owe a decision on";
        this answers "what was ever recorded", which is what the persistence invariant
        has to compare the timeline against.

        `n` is a position, never the row id, and is derived here rather than stored so
        that rows written before it existed still have one. See §4 of
        docs/superpowers/specs/2026-08-23-the-work-order-record.md.

        `SELECT *`, and that is load-bearing rather than lazy: both surfaces in §8 of the
        early-review spec read their rows from here, so every column added to the table
        reaches them without a second edit — and a column left out of a projection here
        would render as "never happened" on the page.
        """
        rows = self.conn.execute(
            "SELECT * FROM assumptions WHERE wo_id=? ORDER BY ts, id", (wo_id,)
        ).fetchall()
        return [{**a, "n": i} for i, a in enumerate(db.rows_to_dicts(rows), start=1)]

    def review_assumption(self, assumption_id: int, status: str, *,
                          decided_by: str = "", reason: str = "", model: str = "",
                          config_version: str | None = None) -> None:
        """Settle one assumption, recording WHO settled it and on what basis.

        The attribution is keyword-only and defaults to `''` — "the user", the only
        decider that existed before auto-review — so no caller is silently re-attributed
        by this signature growing. `ops` passes it explicitly on both routes.
        """
        assert status in ("accepted", "rejected"), status
        self.conn.execute(
            """UPDATE assumptions SET status=?, decided_by=?, decided_reason=?,
                   decided_model=?, decided_config_version=? WHERE id=?""",
            (status, decided_by, reason, model, config_version, assumption_id))

    def get_assumption(self, assumption_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM assumptions WHERE id=?",
                                (assumption_id,)).fetchone()
        return dict(row) if row else None

    def link_assumption_question(self, assumption_id: int, question_id: int) -> None:
        self.conn.execute("UPDATE assumptions SET neo_question_id=? WHERE id=?",
                          (question_id, assumption_id))

    def assumption_for_question(self, question_id: int) -> dict[str, Any] | None:
        """The assumption this Neo question is ruling on, if any.

        The mirror of `approval_for_question` and `feature_order_for_question`, and it
        exists for their reason: Neo's database is OS-wide and knows nothing about a
        project's tables, so the back-link is resolved from this side.

        BOTH QUESTION COLUMNS, not just the early one. An assumption can be asked about
        twice — once while the worker ran, once at delivery to confirm (§7) — and the
        drain that resolves a verdict back to its subject asks this one question. Reading
        `neo_question_id` alone would resolve the confirmation question to nothing, and
        the verdict would be dropped on the floor with the assumption left pending.
        """
        row = self.conn.execute(
            "SELECT * FROM assumptions WHERE neo_question_id=? OR confirm_question_id=?",
            (question_id, question_id)).fetchone()
        return dict(row) if row else None

    # -- the early (provisional) verdict and the objection -----------------------
    #
    # docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md
    # §4.3. These verbs exist beside `review_assumption` rather than inside it because
    # they record something of a DIFFERENT KIND: `review_assumption` settles, and every
    # one of these leaves `status='pending'` untouched on purpose (§4.1).

    def record_provisional(self, assumption_id: int, *, verdict: str, reason: str = "",
                           model: str = "", stakes: str = "",
                           config_version: str | None = None) -> None:
        """Stamp the verdict formed while the worker was still typing. Settles nothing.

        `status` is not an argument here and must not become one: an early verdict is an
        opinion about an intention, and the only thing allowed to settle an assumption is
        the confirmation pass reading the diff (§7).
        """
        assert verdict in PROVISIONAL_VERDICTS, verdict
        self.conn.execute(
            """UPDATE assumptions SET provisional_verdict=?, provisional_reason=?,
                   provisional_model=?, provisional_stakes=?, provisional_ts=?,
                   provisional_config_version=? WHERE id=?""",
            (verdict, reason, model, stakes or "unclassified", db.now(),
             config_version, assumption_id))

    def link_assumption_confirmation(self, assumption_id: int,
                                     question_id: int) -> None:
        """Link the SECOND question. `link_assumption_question` keeps the first."""
        self.conn.execute("UPDATE assumptions SET confirm_question_id=? WHERE id=?",
                          (question_id, assumption_id))

    def record_objection(self, assumption_id: int, *, envelope_id: int,
                         transport: str, sent_ts: float | None = None) -> None:
        """Record that an objection was handed to a transport. Called BEFORE the send.

        `sent_ts` is a parameter rather than `db.now()` so the caller can record the
        moment it actually handed the message over; §6.1 is why it is recorded at all,
        and it is the order of operations that matters — an objection that exists only on
        a wire is one the record cannot explain.
        """
        assert transport in OBJECTION_TRANSPORTS, transport
        self.conn.execute(
            """UPDATE assumptions SET objection_envelope_id=?, objection_transport=?,
                   objection_sent_ts=? WHERE id=?""",
            (envelope_id, transport, db.now() if sent_ts is None else sent_ts,
             assumption_id))

    def mark_objection_delivered(self, assumption_id: int,
                                 ts: float | None = None) -> None:
        """The worker ACTUALLY received it — not that the OS sent it."""
        self.conn.execute(
            "UPDATE assumptions SET objection_delivered_ts=? WHERE id=?",
            (db.now() if ts is None else ts, assumption_id))

    def withdraw_objection(self, assumption_id: int, ts: float | None = None) -> None:
        """The order stopped before the objection could be delivered (§6.6).

        Never erases the objection: withdrawal stops a delivery, it does not unsay what
        Neo said, and the user still reads it in full beside the assumption.
        """
        self.conn.execute(
            "UPDATE assumptions SET objection_withdrawn_ts=? WHERE id=?",
            (db.now() if ts is None else ts, assumption_id))

    def outstanding_objections(self, wo_id: str) -> list[dict[str, Any]]:
        """Objections that are neither delivered nor withdrawn — ONE query, two readers.

        §6.6 withdraws from this list and §7 refuses to run while it is non-empty. Two
        spellings of "outstanding" is two chances for one pass to settle an assumption
        while the other still has a message in flight to the worker about it, which is
        the contradiction that ordering exists to remove.

        Filtered off `all_assumptions` rather than queried, so every row carries its `n`
        — the number the withdrawal event and every surface name the assumption by.
        """
        return [a for a in self.all_assumptions(wo_id)
                if a.get("objection_envelope_id") is not None
                and a.get("objection_delivered_ts") is None
                and a.get("objection_withdrawn_ts") is None]

    # -- validation rounds (see the validation-panel design) ----------------------
    #
    # A round hangs off EITHER a work order or a feature order, never both and never
    # neither, so every method that names a subject takes the two as keyword-only
    # arguments and refuses anything but exactly one of them. That refusal is Python's
    # job as well as the CHECK constraint's: the constraint catches a bad INSERT, this
    # catches a bad SELECT, which would otherwise quietly return every round in the
    # project.

    @staticmethod
    def _subject(wo_id: str | None, fo_id: str | None) -> tuple[str, str]:
        """(column, id) for the one subject given. Raises if that is not exactly one."""
        if (wo_id is None) == (fo_id is None):
            raise ValueError("pass exactly one of wo_id / fo_id")
        return ("wo_id", wo_id) if wo_id is not None else ("fo_id", fo_id)  # type: ignore[return-value]

    def open_validation_round(self, *, wo_id: str | None = None,
                              fo_id: str | None = None, fingerprint: str,
                              summary: str = "", evidence: str = "",
                              pr_url: str | None = None,
                              round: int | None = None,
                              config_version: str | None = None,
                              forced_reason: str = "") -> dict[str, Any]:
        """Start a round on one subject, or return the one that already holds its number.

        1-based and per subject. Left to itself the number is derived from what is
        already stored, so two rounds can never disagree about which came first; a caller
        that COUNTS rounds by outcome — the round machine does, because a `failed`
        transport round must not consume one — passes the number it counted instead.

        **Idempotent per (subject, round).** A `finish` retried while its round is still
        open, or an envelope redelivered, must not consume a second round. The
        enforcement is the partial unique index and the `IntegrityError` it raises, not a
        SELECT before the INSERT: that check-then-insert is a race with no lock, and the
        losing side would silently open the round the index exists to forbid.

        `config_version` stamps the round with the configuration it is being judged
        under; None means the ledger holds nothing yet, and reads as "not recorded".

        `forced_reason` is set only by `ops.force_validation`, and its emptiness is what
        every other surface reads as "a submission opened this".
        """
        col, subject_id = self._subject(wo_id, fo_id)
        if round is None:
            row = self.conn.execute(
                f"SELECT MAX(round) AS n FROM validation_rounds WHERE {col}=?",
                (subject_id,)).fetchone()
            round = int(row["n"] or 0) + 1
        try:
            cur = self.conn.execute(
                f"""INSERT INTO validation_rounds ({col}, round, ts, fingerprint,
                                                   summary, evidence, pr_url,
                                                   config_version, forced_reason)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                (subject_id, round, db.now(), fingerprint, summary, evidence, pr_url,
                 config_version, forced_reason),
            )
        except sqlite3.IntegrityError:
            existing = self.conn.execute(
                f"SELECT * FROM validation_rounds WHERE {col}=? AND round=?",
                (subject_id, round)).fetchone()
            if existing is None:  # pragma: no cover - some other constraint
                raise
            return dict(existing)
        return self.get_validation_round(int(cur.lastrowid))  # type: ignore[arg-type]

    def counted_validation_rounds(self, *, wo_id: str | None = None,
                                  fo_id: str | None = None) -> int:
        """How many rounds this subject has actually SPENT.

        Rounds are counted, never inferred from the row count: only a round that reached
        a verdict — `COUNTED_VALIDATION_OUTCOMES` — is one the submitter used up. A
        `failed` round is a transport outage rather than a judgement and must stay
        invisible here, or three bad nights on the network would give up on a unit
        nothing ever judged; a `pending` round is the one in flight, and counting it
        would make a retried submission consume a second.
        """
        col, subject_id = self._subject(wo_id, fo_id)
        marks = ",".join("?" * len(COUNTED_VALIDATION_OUTCOMES))
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM validation_rounds "
            f"WHERE {col}=? AND outcome IN ({marks})",
            (subject_id, *COUNTED_VALIDATION_OUTCOMES),
        ).fetchone()
        return int(row["n"] or 0)

    def get_validation_round(self, round_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM validation_rounds WHERE id=?", (round_id,)).fetchone()
        return dict(row) if row else None

    def close_validation_round(self, round_id: int, outcome: str,
                               reason: str = "",
                               hold_cause: str | None = None) -> None:
        """Close a round. `hold_cause` says why a `failed` one is WAITING, not broken.

        WRITTEN ON EVERY CLOSE, including the ones that pass None. A held round is
        re-closed in place each time the tick picks it up again, so a cause left behind
        by an earlier hold would outlive the hold and make a genuine outage read as
        "waiting for CI". Clearing it is the same statement as setting it.
        """
        assert outcome in VALIDATION_OUTCOMES, outcome
        assert hold_cause is None or hold_cause in VALIDATION_HOLDING_CAUSES, hold_cause
        self.conn.execute(
            "UPDATE validation_rounds SET outcome=?, reason=?, hold_cause=? WHERE id=?",
            (outcome, reason, hold_cause, round_id),
        )

    def set_validation_head(self, round_id: int, head_sha: str) -> None:
        """Record which commit this round is judging. Called once per round.

        WHATEVER THE OUTCOME, and before one is known: a rejection that recorded nothing
        would leave the record unable to say what it was rejecting, and the value is a
        fact about the packet rather than about the verdict. `''` is written for a
        worktree packet, which is the same "not recorded" every pre-migration row carries
        (spec 2026-09-14 §5.2).
        """
        self.conn.execute("UPDATE validation_rounds SET head_sha=? WHERE id=?",
                          (head_sha, round_id))

    def set_validation_file_shas(self, round_id: int,
                                 file_shas: Iterable[tuple[str, str]]) -> None:
        """Record what each file looked like in the diff this round judged.

        Written where `set_validation_head` is written and whatever the outcome, for that
        method's reason: the next round has to be able to ask what moved since this one,
        and a round that recorded nothing can only answer "I cannot tell".
        """
        self.conn.execute("UPDATE validation_rounds SET file_shas=? WHERE id=?",
                          (db.to_json(dict(file_shas)), round_id))

    @staticmethod
    def validation_file_shas(round_row: Mapping[str, Any] | None) -> dict[str, str]:
        """One round's per-file digests, `{}` when it recorded none.

        A staticmethod over the ROW for `validated_head`'s reason (kn-08f2ff9b): the
        caller already holds the row, and a re-fetch could straddle a new round.
        """
        raw = db.from_json(str((round_row or {}).get("file_shas") or ""), {})
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def last_judged_round(self, *, wo_id: str | None = None,
                          fo_id: str | None = None) -> dict[str, Any] | None:
        """The most recent round a PANEL actually settled, or None.

        `latest_validation_round` is the wrong question for a submitter check: the latest
        row can be a `failed` transport round or a `void`, neither of which asked the
        submitter for anything. Only `COUNTED_VALIDATION_OUTCOMES` told it something.
        """
        for row in reversed(self.validation_rounds(wo_id=wo_id, fo_id=fo_id)):
            if str(row["outcome"] or "") in COUNTED_VALIDATION_OUTCOMES:
                return row
        return None

    @staticmethod
    def validated_head(round_row: dict[str, Any] | None) -> str | None:
        """The commit the panel ACCEPTED, given its LATEST round, or None. THE predicate.

        Non-None means all three of: the latest round has settled `passed`, that round
        recorded which commit it judged, and therefore an automatic merge has something
        to be bound to. Everything else — never validated, a round still pending, a round
        that failed on transport, a pass superseded by a later rejection, a pass with no
        commit recorded — is None.

        **The LATEST round, never "some round passed".** A work order that passed round 1,
        was sent back and is sitting on a pending round 2 is not validated, and reading
        it as validated is how a panel that died silently on a usage limit (issue #235)
        would come to merge code: `pending` and `failed` are not `passed`, and the
        question this asks is "was it accepted", not "was it never refused".

        **`carried_head_sha` WINS OVER `head_sha` WHEN IT IS SET**, and that is the one
        weakening of "nothing merges a commit no round judged". It is written only by
        `ops.carry_validated_head`, only when the OS itself merged the base into the
        branch and provably nothing else moved the head — so the commit it names differs
        from the judged one by a base merge and no authored content. The alternative was
        a fresh panel round per healed pull request, which spends a round number and so
        manufactures an attention item out of a heal nobody asked for
        (docs/superpowers/specs/2026-09-18-a-red-base-heals-itself.md §5).

        Nothing else relaxes: CI must still be green on the carried commit, and the merge
        still files its gate. Reading the two columns HERE rather than at the automerge
        site is the same one-home rule the paragraph below states.

        One home for the rule, for `arbitrate`'s reason — a rule spread across three call
        sites is a rule that holds by luck (spec 2026-09-14 §5.2).

        **TAKES THE ROW, DOES NOT FETCH IT — that is why it is static.** The validator
        runs on another thread and opens rounds while the pull-request poll is reading:
        a caller that fetched `latest_validation_round` for the wording and let this
        fetch it again for the predicate could be handed two DIFFERENT rounds a
        microsecond apart. The failure is not a missed merge, which the next tick
        repairs, but a permanent one — the pair (passed round N, no accepted head) is
        the `HELD_SHA_UNRECORDED` wording, and that hold is deduped for ever on a reason
        that was never true. One read, one row, both answers off it.
        """
        if round_row is None or round_row["outcome"] != "passed":
            return None
        judged = str(round_row["head_sha"] or "")
        if not judged:
            # A carry can never manufacture a binding where the seats bound none: the
            # whole licence for carrying is "this differs from what was judged by a base
            # merge", and there is nothing to differ from. Belt to `carry_validated_head`'s
            # braces, which refuses to write one.
            return None
        # `.get`, unlike the two lookups above: this column arrived after the rule did,
        # and the callers that build a round row by hand (the decision table in
        # `tests/test_automerge.py`) must keep meaning "never carried" rather than
        # raising. A real row always has it — `_migrate` sees to that.
        return str(round_row.get("carried_head_sha") or "") or judged

    def validation_rounds(self, *, wo_id: str | None = None,
                          fo_id: str | None = None) -> list[dict[str, Any]]:
        """Every round on one subject, oldest first — the order they were judged in."""
        col, subject_id = self._subject(wo_id, fo_id)
        return db.rows_to_dicts(self.conn.execute(
            f"SELECT * FROM validation_rounds WHERE {col}=? ORDER BY round",
            (subject_id,),
        ).fetchall())

    def work_orders_awaiting_validation(self) -> list[dict[str, Any]]:
        """Every OPEN work order whose latest round is the round machine's to run.

        Keyed off the ROUND, never off `status='validating'`: a work order with pending
        assumptions parks in `needs_review` with its round open beside it, and the query
        this replaced could not see one (spec
        docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §3).

        `OPEN_STATUSES` is the bound that matters. A cancelled work order can hold a
        round nobody closed, and handing that to the machine would judge — and then land
        — work the user stopped. Hidden ones ARE included: hiding is "stop showing me
        this", not "stop this".
        """
        marks = ",".join("?" * len(RUNNABLE_VALIDATION_OUTCOMES))
        statuses = ",".join("?" * len(OPEN_STATUSES))
        rows = self.conn.execute(
            f"""SELECT w.* FROM work_orders w
                  JOIN validation_rounds r ON r.wo_id = w.id
                 WHERE r.outcome IN ({marks})
                   AND w.status IN ({statuses})
                   AND r.round = (SELECT MAX(round) FROM validation_rounds
                                   WHERE wo_id = w.id)
                 ORDER BY w.created_at""",
            (*RUNNABLE_VALIDATION_OUTCOMES, *OPEN_STATUSES),
        ).fetchall()
        return db.rows_to_dicts(rows)

    @staticmethod
    def round_machine_owns(round_row: dict[str, Any] | None) -> bool:
        """Does the round machine still own the work order this row is the latest round
        of? THE ONE DEFINITION, and it takes the ROW for `validated_head`'s reason.

        A caller that needs the predicate AND the wording — "round 2 is pending, wait for
        it" — must derive both from ONE read, or the panel opening a round on its own
        thread between the two hands it a predicate and a sentence taken a microsecond
        apart (kn-08f2ff9b, the `HELD_SHA_UNRECORDED` bug). A staticmethod over the row
        cannot re-fetch, which is what makes that impossible rather than merely unlikely.

        None — the unit has never been judged — is False: nothing owns it.
        """
        return str((round_row or {}).get("outcome") or "") in RUNNABLE_VALIDATION_OUTCOMES

    def validation_round_open(self, wo_id: str) -> bool:
        """Is the round machine going to act on this work order?

        `work_orders_awaiting_validation` asked about ONE work order, off the same
        `RUNNABLE_VALIDATION_OUTCOMES` and the same latest-round rule, because the two
        must answer alike: this is the question "does something else own this worker's
        session right now", and the round machine is the thing that would be lying to.
        The status bound is left to the caller, which has its own (`Daemon.heal_pull_request`
        only ever asks about a work order the pull-request poll selected).

        `rejected` is deliberately NOT runnable here, which means this says False while
        the panel waits for the submitter to come back. That window belongs to
        `heal_pull_request`'s other guards — a turn in flight, or a nudge already
        queued — and a red build the worker is about to push over is worth telling it
        about. See §4.1 of
        docs/superpowers/specs/2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md.

        It fetches the latest round and hands it to `round_machine_owns` rather than
        asking SQL the same question a second way: a caller that needs the predicate AND
        the round it is about (`ops.force_validation`) can then take one read and derive
        both, which is the only shape that cannot straddle a round opening on the panel's
        thread.
        """
        return self.round_machine_owns(self.latest_validation_round(wo_id=wo_id))

    def latest_validation_round(self, *, wo_id: str | None = None,
                                fo_id: str | None = None) -> dict[str, Any] | None:
        """The most recent round on one subject, or None if it has never been judged."""
        col, subject_id = self._subject(wo_id, fo_id)
        row = self.conn.execute(
            f"SELECT * FROM validation_rounds WHERE {col}=? ORDER BY round DESC LIMIT 1",
            (subject_id,),
        ).fetchone()
        return dict(row) if row else None

    def carry_round_head(self, round_id: int, head_sha: str, reason: str) -> None:
        """Bind a passed round's verdict to a commit the OS itself produced.

        Writes `carried_head_sha`/`carried_reason` and NEVER touches `head_sha` — see those
        columns. The guard that decides whether this may be called at all is
        `ops.carry_validated_head`; this is only the write, kept dumb for the reason
        every other writer here is: a rule enforced in two places is enforced in one and
        copied in the other.
        """
        self.conn.execute(
            "UPDATE validation_rounds SET carried_head_sha=?, carried_reason=? WHERE id=?",
            (head_sha, reason, round_id))

    def record_validation_opinion(self, round_id: int, seat: str, *, reply: str = "",
                                  verdict: str = "", status: str = "ok",
                                  model: str = "", latency_ms: int = 0) -> None:
        """What one seat said. Re-recording a seat REPLACES its opinion.

        A seat that is re-run — a retry after a timeout — must leave one row, not two:
        the arbiter counts verdicts, and a doubled seat would vote twice.
        """
        assert status in VALIDATION_OPINION_STATUSES, status
        assert verdict in VALIDATION_VERDICTS, verdict
        self.conn.execute(
            """INSERT INTO validation_opinions
                   (round_id, ts, seat, reply, verdict, status, model, latency_ms)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(round_id, seat) DO UPDATE SET
                   ts=excluded.ts, reply=excluded.reply, verdict=excluded.verdict,
                   status=excluded.status, model=excluded.model,
                   latency_ms=excluded.latency_ms""",
            (round_id, db.now(), seat, reply, verdict, status, model, latency_ms),
        )

    def validation_opinions(self, round_id: int) -> list[dict[str, Any]]:
        """One round's opinions, in the order the seats first reported.

        Ordered by `id` rather than `ts` so a partial re-run cannot reshuffle rows a
        reader has already seen — the same rule `neo_store.opinions` follows.
        """
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM validation_opinions WHERE round_id=? ORDER BY id",
            (round_id,),
        ).fetchall())

    # -- approvals (privileged-action gates; see gates.py) ------------------------

    def add_approval(self, wo_id: str, kind: str, command: str, matched: str = "",
                     justification: str = "", evidence: str = "",
                     max_uses: int = 3,
                     agent_type: str | None = None,
                     status: str = "pending",
                     contested: bool = False) -> dict[str, Any]:
        now = db.now()
        cur = self.conn.execute(
            """INSERT INTO approvals (wo_id, ts, kind, command, matched, justification,
                                      evidence, max_uses, agent_type, status, contested,
                                      pending_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            # `pending_at` iff it is pending from birth: an `awaiting_case` row gets one
            # in `start_review` or never at all.
            (wo_id, now, kind, command, matched, justification, evidence, max_uses,
             agent_type, status, int(contested), now if status == "pending" else None),
        )
        approval_id = int(cur.lastrowid)  # type: ignore[arg-type]
        self.add_event(wo_id, "gate_requested", {
            "approval_id": approval_id, "kind": kind, "command": command,
            # Whether a reviewer can see it yet. The timeline is read on its own, and
            # "asked permission" is the wrong thing to say about a request that is still
            # waiting for the worker to argue it — see gates.AWAITING_CASE.
            "held": status == "awaiting_case",
            # A CONTEST, not a request: the worker is disputing the match, so "asked
            # permission to cut a release" is the wrong thing for the timeline to say.
            **({"contested": True} if contested else {}),
            # In the payload as well as the column: the timeline is read on its own, and
            # "the planner ran this" is exactly the wrong thing for it to imply.
            **({"agent_type": agent_type} if agent_type else {}),
        })
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def start_review(self, approval_id: int, question_id: int) -> dict[str, Any]:
        """Link the reviewer's question and open the request to review. See
        `gates.queue_for_review` — the one transition out of `awaiting_case`."""
        self.conn.execute(
            """UPDATE approvals SET neo_question_id=?, status='pending',
                                    pending_at=COALESCE(pending_at, ?) WHERE id=?""",
            (question_id, db.now(), approval_id),
        )
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def get_approval(self, approval_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def latest_approval_for(self, wo_id: str, kind: str, command: str
                            ) -> dict[str, Any] | None:
        """The most recent request this work order filed for this exact command.

        Exact-match on the command is the whole security model: a grant is a receipt for
        one string. A worker that "tidies up" the command on retry gets a fresh gate,
        which is the correct outcome — the reviewer approved what it read.
        """
        row = self.conn.execute(
            """SELECT * FROM approvals WHERE wo_id=? AND kind=? AND command=?
               ORDER BY ts DESC, id DESC LIMIT 1""",
            (wo_id, kind, command),
        ).fetchone()
        return dict(row) if row else None

    def amend_approval(self, approval_id: int, justification: str,
                       evidence: str) -> dict[str, Any]:
        """Write a fuller case onto a request that is still pending. See `gates.amend_request`.

        Only the two case fields move. The command, the recogniser and the status are the
        request's identity — a worker that wants a different command files a different
        request, which is the whole point of exact-string matching.
        """
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        self.conn.execute(
            "UPDATE approvals SET justification=?, evidence=? WHERE id=?",
            (justification, evidence, approval_id),
        )
        self.add_event(approval["wo_id"], "gate_amended", {
            "approval_id": approval_id, "kind": approval["kind"],
            "command": approval["command"],
        })
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def link_neo_question(self, approval_id: int, question_id: int) -> None:
        self.conn.execute("UPDATE approvals SET neo_question_id=? WHERE id=?",
                          (question_id, approval_id))

    def approval_for_question(self, question_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM approvals WHERE neo_question_id=?",
                                (question_id,)).fetchone()
        return dict(row) if row else None

    def mark_approval_escalated(self, approval_id: int, reason: str) -> None:
        """Neo declined to decide: the request stays open, now against the user."""
        self.conn.execute(
            "UPDATE approvals SET escalated=1, escalation_reason=? WHERE id=?",
            (reason, approval_id),
        )

    def escalated_approvals(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """Requests waiting on the user specifically — the only gates that are allowed
        to consume attention."""
        return [a for a in self.pending_approvals(wo_id) if a["escalated"]]

    def decide_approval(self, approval_id: int, verdict: str, reason: str,
                        decided_by: str, ttl_seconds: int = 3600) -> dict[str, Any]:
        """Record a verdict — `approved`, `denied` or `dismissed`.

        Only an approval gets an expiry. It starts its clock now, not when the request
        was filed: the window exists to bound the gap between "yes" and the act.

        A dismissal deliberately gets none. It does not say "you may do this for the next
        hour", it says "this command performs no privileged action" — a fact about the
        command string, which does not lapse. Giving it a TTL would model it as a
        permission, and would make one classifier bug cost a second review an hour later.
        """
        if verdict not in ("approved", "denied", "dismissed"):
            raise ValueError(f"unknown verdict {verdict!r}")
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        now = db.now()
        self.conn.execute(
            """UPDATE approvals SET status=?, decided_by=?, decision_reason=?,
                                    decided_at=?, expires_at=? WHERE id=?""",
            (verdict, decided_by, reason, now,
             now + ttl_seconds if verdict == "approved" else None, approval_id),
        )
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def supersede_approval(self, approval_id: int, reason: str) -> dict[str, Any]:
        """Close a pending request that no longer has an answer worth giving.

        Each of the three verdicts says something about the REQUEST: approved and denied
        permit or refuse it, dismissed calls the classifier wrong about it. This says
        something about the world around it instead — the action already ran under a
        separate approval, or the work order that filed this is over — so none of the
        three would be true, and writing one of them anyway is how a record ends up
        claiming a deploy was authorised twice.

        It lands in `expired`, the one existing status that already means "never decided,
        and can no longer be", and it authorises NOTHING: the command string stays
        blocked, so a worker that retries files a fresh request and gets a real review.
        That is the property that makes superseding safe to do automatically.

        A no-op on anything already decided — a verdict is never overwritten.
        """
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        # `awaiting_case` too: a request nobody ever argued is no more answerable than a
        # pending one once its work order is over, and it has even less claim on the
        # record — see gates.AWAITING_CASE.
        if approval["status"] not in ("pending", "awaiting_case"):
            return approval
        self.conn.execute(
            """UPDATE approvals SET status='expired', closed_as='superseded',
                                    decided_by='os', decision_reason=?, decided_at=?,
                                    expires_at=NULL
               WHERE id=?""",
            (reason, db.now(), approval_id),
        )
        self.add_event(approval["wo_id"], "gate_superseded", {
            "approval_id": approval_id,
            "kind": approval["kind"],
            "command": approval["command"],
            "reason": reason,
        })
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def usable_grant(self, wo_id: str, kind: str, command: str) -> dict[str, Any] | None:
        """The decided request that lets this exact command through right now, or None.

        Two statuses clear a command, and they are not the same thing — callers must read
        `status` to tell them apart, because only one of them is an authorisation:

        * `approved` — a privileged action was reviewed and permitted. Bounded: expiry
          and use-count are re-checked here rather than trusted from the row, so a grant
          cannot outlive its window just because nothing swept the table.
        * `dismissed` — the gate matched a command that performs no privileged action.
          Unbounded on purpose (see `decide_approval`); the scope that keeps it safe is
          the exact-string match on one work order, not a clock.
        """
        approval = self.latest_approval_for(wo_id, kind, command)
        if approval is None or approval["status"] not in ("approved", "dismissed"):
            return None
        if approval["status"] == "dismissed":
            return approval
        if approval["uses"] >= approval["max_uses"]:
            return None
        if approval["expires_at"] is not None and db.now() > approval["expires_at"]:
            return None
        return approval

    def consume_grant(self, approval_id: int) -> dict[str, Any]:
        """Spend one use of a grant. Called only when the gate actually opens, so the
        count reflects attempts that ran, not attempts that were merely considered.

        A dismissed row counts its uses too, though nothing enforces the limit for it:
        the number is how often one classifier bug actually cost a worker something.
        """
        self.conn.execute("UPDATE approvals SET uses = uses + 1 WHERE id=?", (approval_id,))
        approval = self.get_approval(approval_id)
        assert approval is not None
        self.add_event(approval["wo_id"], "gate_opened", {
            "approval_id": approval_id,
            "kind": approval["kind"],
            "use": approval["uses"],
            "of": approval["max_uses"],
            # Which of the two clearing statuses opened it. The timeline must not report
            # a dismissed false positive as "ran the approved command".
            "clearance": approval["status"],
        })
        return approval

    def list_approvals(self, wo_id: str | None = None,
                       statuses: tuple[str, ...] | None = None,
                       limit: int = 200) -> list[dict[str, Any]]:
        q = "SELECT * FROM approvals"
        conds: list[str] = []
        params: list[Any] = []
        if wo_id:
            conds.append("wo_id=?")
            params.append(wo_id)
        if statuses:
            conds.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        return db.rows_to_dicts(self.conn.execute(q, params).fetchall())

    def pending_approvals(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """Requests still awaiting a verdict — from Neo or, once escalated, the user.

        `awaiting_case` is deliberately NOT here: nobody is deciding one, so every caller
        that asks "what is a reviewer holding?" would be told something false. Ask
        `held_approvals` for those.
        """
        return self.list_approvals(wo_id, statuses=("pending",))

    def held_approvals(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """Requests recorded but not yet argued, so not yet in front of anyone."""
        return self.list_approvals(wo_id, statuses=("awaiting_case",))

    def open_approvals(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """Every request still on its way to a verdict, held or pending.

        The two above answer "who is holding this"; this one answers "is anything still
        unresolved", which is the question every caller that must not settle over a gate
        is really asking (`ops.finish`, `invariants.check_no_orphan_gate_requests`).
        Named once so a seventh status cannot reach one of them and not the other.
        """
        return self.pending_approvals(wo_id) + self.held_approvals(wo_id)

    def gate_open_at(self, wo_id: str, ts: float) -> bool:
        """Was a privileged-action gate REFUSING THIS WORKER'S COMMANDS at `ts`?

        The historical form of `pending_approvals`, and it exists for one caller:
        `ops.rearm_pr_repair` asking whether a repair attempt already spent was an
        attempt the worker was ever allowed to make (issue #469).

        SAME PREDICATE AS THE GUARD, and that matters more than any other property here:
        `hooks.pending_turn_block` refuses a session's commands while a request is
        `pending` and at no other time, `Daemon.heal_pull_request` defers on exactly
        that, and this asks it of a past moment. A request still `awaiting_case` refuses
        only the END of a turn, so it is no excuse for a nudge that failed — if this
        counted one, a work order carrying a single held request nobody ever argued
        would be refunded its whole budget every episode, for ever, which is the burn
        issue #469 is about. §4 of
        docs/superpowers/specs/2026-09-19-an-attempt-the-worker-could-not-make.md.
        """
        for approval in self.list_approvals(wo_id):
            pending_at = approval["pending_at"]
            if pending_at is None or pending_at > ts:
                continue
            decided_at = approval["decided_at"]
            if decided_at is None or decided_at > ts:
                return True
        return False

    def expire_approvals(self) -> int:
        """Move spent or timed-out grants to `expired` so listings tell the truth.

        Cosmetic for enforcement — `usable_grant` already refuses them — but a dashboard
        showing a month-old "approved" release gate reads as standing permission, which
        is exactly the wrong impression to leave lying around.

        TWO SWEEPS, because the two ways out of `approved` are opposite outcomes and one
        word for both is a lie in the majority case: a grant whose uses ran out was
        approved AND USED — the merge ran — while one whose clock ran out was approved
        and never used by anyone. Spent goes first so a grant that is both reads as what
        actually happened to it. Spec 2026-09-19 §1; same argument as `abandoned`.

        `status='approved'` in the WHERE clauses is doing two jobs, and the second one is
        load-bearing: it EXCLUDES `dismissed`. A dismissal never expires, and sweeping it
        into `expired` would also erase the false-positive count that
        `dismissed_count()` exists to report — the whole reason the verdict is separate.
        """
        spent = self.conn.execute(
            """UPDATE approvals SET status='expired', closed_as='spent'
               WHERE status='approved' AND uses > 0 AND uses >= max_uses""",
        )
        lapsed = self.conn.execute(
            """UPDATE approvals SET status='expired', closed_as='lapsed'
               WHERE status='approved'
                 AND (uses >= max_uses OR (expires_at IS NOT NULL AND expires_at < ?))""",
            (db.now(),),
        )
        return spent.rowcount + lapsed.rowcount

    def abandon_approval(self, approval_id: int, reason: str) -> dict[str, Any]:
        """Close a held request whose case never came. NOT a verdict — see
        `gates.sweep_unargued` and spec 2026-09-12 §4.

        The worker ran a command, was blocked, and never came back to argue it or to
        contest the match. Nobody ever reviewed it, so nobody can rule on it: writing
        `denied` here would assert that a reviewer refused a privileged action, on
        evidence that the OS's own recogniser is usually wrong about (`abandoned_count`).

        Lands in `expired` like `supersede_approval`, for the same reason — it is the one
        status that already means "never decided, and can no longer be" — and authorises
        nothing: the command string stays blocked, so a retry gets a real review.
        """
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        if approval["status"] != "awaiting_case":
            return approval
        self.conn.execute(
            """UPDATE approvals SET status='expired', closed_as='abandoned',
                                    decided_by='os', decision_reason=?, decided_at=?,
                                    expires_at=NULL
               WHERE id=?""",
            (reason, db.now(), approval_id),
        )
        self.add_event(approval["wo_id"], "gate_abandoned", {
            "approval_id": approval_id,
            "kind": approval["kind"],
            "command": approval["command"],
            "matched": approval["matched"],
            "reason": reason,
        })
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def mark_contested(self, approval_id: int) -> dict[str, Any]:
        """Record that the worker disputes the MATCH rather than asking permission.

        One-way: a row that has carried the claim "this performs no privileged action"
        can never be turned back into a request to perform one, because the case a
        reviewer read would no longer be the case it is ruling on.
        """
        self.conn.execute("UPDATE approvals SET contested=1 WHERE id=?", (approval_id,))
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def abandoned_count(self, wo_id: str | None = None) -> int:
        """How many held requests timed out with no case and no contest.

        Counted beside `dismissed_count` and never folded into it: an abandonment is
        EVIDENCE about the classifier, not a verdict on it. A worker that walks away from
        a block is usually routing around a false positive, and until this existed that
        signal was discarded — so the measured false-positive rate was understated by
        every one of them.
        """
        q = ("SELECT COUNT(*) c FROM approvals "
             "WHERE status='expired' AND closed_as='abandoned'")
        params: list[Any] = []
        if wo_id:
            q += " AND wo_id=?"
            params.append(wo_id)
        return int(self.conn.execute(q, params).fetchone()["c"])

    def dismissed_count(self, wo_id: str | None = None) -> int:
        """How many gate requests turned out not to be gated actions at all.

        The OS's classifier false-positive rate, in one number. It is the signal for
        whether the recognisers in `gates.KINDS` are getting better or worse, so it is
        counted from the rows rather than derived from anything a reviewer wrote.
        """
        q = "SELECT COUNT(*) c FROM approvals WHERE status='dismissed'"
        params: list[Any] = []
        if wo_id:
            q += " AND wo_id=?"
            params.append(wo_id)
        return int(self.conn.execute(q, params).fetchone()["c"])

    # -- summary ----------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        by_status = {
            r["status"]: r["c"]
            for r in self.conn.execute(
                "SELECT status, COUNT(*) c FROM work_orders WHERE hidden=0 GROUP BY status"
            ).fetchall()
        }
        attention = self.conn.execute(
            "SELECT COUNT(*) c FROM work_orders WHERE needs_attention=1 AND hidden=0"
        ).fetchone()["c"]
        pending_assumptions = len(self.pending_assumptions())
        return {
            "by_status": by_status,
            "needs_attention": attention,
            "pending_assumptions": pending_assumptions,
        }
