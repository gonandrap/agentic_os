"""A due retry fires wherever the work order is parked (issue #259).

Measured on wo-f35e603e: it finished behind a pull request, took a message, and that
turn was refused for the usage limit — so it sat in `waiting_pr_merge` holding a pause
that was resumable, due, and reported as due by `jarvis wo resume-auto`. Nothing ran it.
`Daemon.retry_paused_turns` swept `ACTIVE_STATUSES` while `invariants.stuck_message`
called the silence a defect across three statuses that tuple does not contain, so the OS
diagnosed the stall perfectly and had no loop that could ever repair it. Three messages
queued behind the lost turn; the only remedy on offer was to abandon the work order.

The durable half of this suite is the LAST section: the sweep's set and the set
`stuck_message` calls stuck are reconciled here, so the next status added to one cannot
go quietly missing from the other. The three statuses above are today's instance of that,
not the property.
"""

from __future__ import annotations

import time

import pytest

from jarvis import fleet, invariants, worker_session
from jarvis.catalog import load_catalog
from jarvis.daemon import PR_POLL_STATUSES, Daemon
from jarvis.invariants import MESSAGE_STUCK_STATUSES, stuck_message
from jarvis.project_store import (
    ACTIVE_STATUSES,
    NOT_RETRIED,
    RETRY_SWEEP_STATUSES,
    WO_STATUSES,
    ProjectStore,
)

#: A reset moment long past, so the pause is due the moment it is derived. The daemon
#: re-reads the error every pass, so this is the code path a real reopening takes.
WINDOW_REOPENED = "Claude AI usage limit reached|1000000000"

#: What issue #259 added: the statuses the sweep reaches that the ACTIVE set does not.
#: Derived rather than listed, so a fourth one inherits the behavioural tests below and
#: not only the partition test — the whole point of the reconciliation being a property.
#: Ordered by `RETRY_SWEEP_STATUSES` rather than by set arithmetic, so the parametrised
#: ids are stable between runs.
NEWLY_SWEPT = tuple(s for s in RETRY_SWEEP_STATUSES if s not in ACTIVE_STATUSES)


@pytest.fixture()
def sweep(fake_claude, catalog_file, project, monkeypatch):
    """The retry pass, callable directly, with the retry floor out of the way.

    The floor is a real guard against retrying into the same refusal and is not what
    these tests are about. Driving the pass rather than a whole tick keeps the seam that
    broke — which statuses it walks — the only thing under test.
    """
    monkeypatch.setattr(worker_session, "RATE_LIMIT_MIN_DELAY", 0)
    catalog = load_catalog(catalog_file)
    daemon, spec = Daemon(catalog), catalog.projects[0]
    store = ProjectStore(project)

    def run():
        daemon.retry_paused_turns(spec, store)

    def settle(wo_id):
        """The reconciler's half — where the relaunched turn leaves the work order."""
        daemon.settle_work_order(spec, store, store.get_work_order(wo_id))

    yield store, run, settle
    store.close()


def _refused(store: ProjectStore, status: str, *, times: int = 1,
             error: str = WINDOW_REOPENED) -> dict:
    """A work order parked in `status` whose last turn was refused for the usage limit.

    `times` > 1 builds the streak `TurnPause.exhausted` counts: consecutive refusals of
    the same kind, which is the state where the OS has run out of patience.
    """
    wo = store.create_work_order("ship the thing", origin="jarvis")
    store.update_work_order(wo["id"], session_id=worker_session.new_session_id(),
                            worktree=wo["id"])
    for _ in range(times):
        turn = store.create_turn(wo["id"], kind="message", prompt="carry on")
        store.finish_turn(turn["id"], "failed", error=error)
    store.set_status(wo["id"], status)
    return store.get_work_order(wo["id"])


def _window_shut(hours: float = 1) -> str:
    """A refusal whose window has NOT reopened yet — the retry is booked, not due."""
    return f"Claude AI usage limit reached|{int(time.time() + hours * 3600)}"


# -- the sweep -------------------------------------------------------------------------


@pytest.mark.parametrize("status", RETRY_SWEEP_STATUSES)
def test_a_due_retry_fires_wherever_the_work_order_is_parked(sweep, status):
    store, run, _settle = sweep
    wo = _refused(store, status)
    pause = worker_session.turn_pause(store, wo["id"])
    assert pause is not None and pause.resumable and pause.due()

    run()

    assert store.latest_turn(wo["id"])["state"] != "failed", (
        f"the pause was resumable and due in `{status}` and nothing relaunched it")
    assert "turn_resumed" in [e["kind"] for e in store.list_events(wo["id"])]


@pytest.mark.parametrize("status", NEWLY_SWEPT)
def test_the_relaunched_work_order_stops_saying_somebody_else_has_it(sweep, status):
    """The turn is out, so the record must not go on saying the work order is settled.

    Required rather than tidy, and for two different reasons depending on the status.

    Every status here that carries a PULL REQUEST is in `PR_POLL_STATUSES`, which leaves
    the in-flight ones out precisely so `complete_merged` cannot end a work order out
    from under a worker still writing to it. Leaving a relaunched order where it was
    would put a live turn back in front of that poll.

    `idle` is the exception and has its own reason (issue #264): it means "this manager
    has nothing to act on", which `ops.waiting_on` reports verbatim and `resume-auto`
    refuses to nudge on. A live turn reading `idle` would be the OS giving a confident
    wrong answer about a state it had just created — the whole of the bug that status
    exists to fix.
    """
    store, run, _settle = sweep
    wo = _refused(store, status)
    assert status in PR_POLL_STATUSES or status == "idle", (
        "the argument above depends on this")

    run()

    assert store.get_work_order(wo["id"])["status"] == "running"


@pytest.mark.parametrize("status", NEWLY_SWEPT)
def test_the_relaunch_clears_only_the_flag_it_actually_answered(sweep, status):
    """A usage window reopening does not answer a review the user owes. The flag was
    cleared unconditionally here because, before #259, the only work order that reached
    this branch was one `_park_on_signin` had flagged itself (kn-b6977de3)."""
    store, run, _settle = sweep
    wo = _refused(store, status)
    store.flag_attention(wo["id"], "assumptions pending review")

    run()

    row = store.get_work_order(wo["id"])
    assert row["needs_attention"], "the user's item survives a retry that did not answer it"
    assert row["attention_reason"] == "assumptions pending review"


def test_the_signin_flag_is_still_cleared_because_nothing_re_derives_it(sweep):
    """The other half, and why the clear cannot simply be deleted: `true_blockers`
    re-derives AUTH_BLOCKER from `waiting_input`, so once this row is `running` the only
    thing that can take it down is this branch."""
    store, run, _settle = sweep
    wo = _refused(store, "waiting_input")
    store.flag_attention(wo["id"], invariants.AUTH_BLOCKER)

    run()

    assert not store.get_work_order(wo["id"])["needs_attention"]


@pytest.mark.parametrize(("status", "lands"), [
    # A decision the user still owes — a red build, a refused round, a pull request
    # closed unmerged. Pending assumptions never reach this branch; they are settled one
    # `if` earlier and return `needs_review` on their own.
    ("needs_review", "needs_review"),
    # Not a decision anyone owes: the OS failed this one and the relaunch is the
    # recovery, so it lands where a finished work order with an open PR belongs.
    ("failed", "waiting_pr_merge"),
    ("waiting_pr_merge", "waiting_pr_merge"),
])
def test_where_a_relaunched_work_order_lands_once_its_turn_finishes(sweep, fake_claude,
                                                                   settle_turns,
                                                                   status, lands):
    """`running` is a transit status, so the relaunch is only half an answer — this is
    the other half. `ops.resumed_from` is what keeps the `needs_review` row off the merge
    queue, the same rule `pr_repair_origin` already applies to a repair turn."""
    store, run, _settle = sweep
    wo = _refused(store, status)
    store.update_work_order(wo["id"], pr_url="https://github.com/x/y/pull/243",
                            result_summary="shipped it")
    fake_claude.turns_recover()

    run()
    assert settle_turns(store), "the relaunched turn never ran"
    _settle(wo["id"])

    assert store.get_work_order(wo["id"])["status"] == lands


def test_a_pause_that_is_not_due_is_left_alone(sweep):
    """The widening is about WHERE a retry fires, not WHEN. A booked retry still waits."""
    store, run, _settle = sweep
    wo = store.create_work_order("ship the thing", origin="jarvis")
    store.update_work_order(wo["id"], session_id=worker_session.new_session_id())
    turn = store.create_turn(wo["id"], kind="message", prompt="carry on")
    store.finish_turn(turn["id"], "failed",
                      error="You've hit your session limit · resets 11:50pm (UTC)")
    store.set_status(wo["id"], "waiting_pr_merge")

    run()

    assert store.latest_turn(wo["id"])["seq"] == turn["seq"], "it was not due yet"


@pytest.mark.parametrize("status", NEWLY_SWEPT)
def test_an_exhausted_pause_is_not_retried_and_is_surfaced_instead(sweep, status):
    """The negative half. Out of retries is the one state where 'the OS will do it'
    stops being true, so nothing here may quietly keep trying — and `delivery_hold`
    releases the queued message rather than holding it behind a turn that is never
    going out, which is the escape hatch the stranded work order never had."""
    store, run, _settle = sweep
    wo = _refused(store, status, times=worker_session.MAX_RATE_LIMIT_RETRIES + 1)
    pause = worker_session.turn_pause(store, wo["id"])
    assert pause is not None and pause.exhausted and not pause.resumable

    run()

    assert store.latest_turn(wo["id"])["state"] == "failed", "nothing may retry this one"
    assert "turn_resumed" not in [e["kind"] for e in store.list_events(wo["id"])]
    # Not held for ever behind the dead turn: a message sent now goes out, and
    # `stuck_message` stays silent because the queue is moving.
    assert worker_session.delivery_hold(store, store.get_work_order(wo["id"])) is None


@pytest.mark.parametrize("status", NOT_RETRIED)
def test_the_statuses_the_sweep_leaves_alone_stay_alone(sweep, status):
    """The other side of the partition, tested rather than asserted from the tuple.
    `completed` and `cancelled` were ended by a PERSON and relaunching a turn under one
    would reopen work its owner closed; `pending` has no conversation to relaunch."""
    store, run, _settle = sweep
    wo = _refused(store, status)

    run()

    assert store.latest_turn(wo["id"])["state"] == "failed"
    assert store.get_work_order(wo["id"])["status"] == status


# -- the two tuples cannot drift apart -------------------------------------------------


def test_every_status_is_on_one_side_or_the_other():
    """THE DURABLE FIX. A status added to `WO_STATUSES` fails here until somebody decides
    whether a paused turn may be relaunched in it — which is the decision nobody was
    asked to make when `waiting_pr_merge` was added."""
    assert set(RETRY_SWEEP_STATUSES) | set(NOT_RETRIED) == set(WO_STATUSES)
    assert not set(RETRY_SWEEP_STATUSES) & set(NOT_RETRIED)


def test_the_sweep_covers_everything_stuck_message_calls_stuck():
    """The gap issue #259 was. `stuck_message` names an undelivered message in these
    statuses a defect; the only repair for one held behind a lost turn is the relaunch,
    so a status it reports and the sweep does not reach is a diagnosis with nothing
    behind it. Superset, not equality: `validating` is swept and is deliberately not
    stuck-checked (see `MESSAGE_STUCK_STATUSES`)."""
    assert set(MESSAGE_STUCK_STATUSES) <= set(RETRY_SWEEP_STATUSES)
    assert set(ACTIVE_STATUSES) <= set(RETRY_SWEEP_STATUSES)


@pytest.mark.parametrize("status", RETRY_SWEEP_STATUSES)
def test_the_liveness_check_watches_exactly_what_the_sweep_walks(sweep, status,
                                                                 monkeypatch):
    """INV-PAUSE-OVERDUE is the only thing that reports the sweep failing to fire, so a
    check scoped narrower than the loop it audits is blind in precisely the rows the loop
    never reaches — which is how this went unreported for as long as it did."""
    store, _run, _settle = sweep
    wo = _refused(store, status)
    monkeypatch.setattr(
        "jarvis.invariants.time.time",
        lambda: worker_session.turn_pause(store, wo["id"]).retry_at
        + invariants.PAUSE_OVERDUE_GRACE + 60)

    reported = [v.wo_id for v in invariants.check_paused_turns_resume(store)]

    assert reported == [wo["id"]]


# -- what the widened sweep owes the surfaces that report it ---------------------------


@pytest.mark.parametrize("status", NEWLY_SWEPT)
def test_a_booked_retry_is_visible_where_the_work_order_is_parked(sweep, status):
    """Neo, question 312: a relaunch the OS will perform has to be readable where it
    will happen, or #259 is fixed in the sweep and still blind on the page. The note is
    the one string every surface renders (the CLI through `status_label`, the dashboard
    through `ops.os_status`), so widening it covers all of them at once."""
    store, _run, _settle = sweep
    wo = _refused(store, status, error=_window_shut())

    assert "retrying by itself at" in invariants.pause_note(store, wo)
    assert invariants.status_label(store, wo).startswith(f"{status} — Claude usage limit")


def test_the_round_note_is_untouched_when_nothing_is_paused(sweep):
    """The new branch fires only on a pause, so a `needs_review` order with a round
    running in parallel reads exactly as it did before."""
    store, _run, _settle = sweep
    wo = store.create_work_order("ship the thing", origin="jarvis")
    store.set_status(wo["id"], "needs_review")
    store.open_validation_round(wo_id=wo["id"], fingerprint="fp")

    label = invariants.status_label(store, store.get_work_order(wo["id"]))

    assert label == "needs_review — review round 1 is running in parallel"


def test_the_parked_note_outranks_the_round_note(sweep):
    """The ranking the new branch introduces, which is the only thing it decides. Both
    notes are true of a `needs_review` order whose turn the window ate; the pause is the
    one that says why nothing is moving and names the moment that changes."""
    store, _run, _settle = sweep
    wo = _refused(store, "needs_review", error=_window_shut())
    store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    assert invariants.parallel_round_note(store, wo["id"]), "the round note is available"

    label = invariants.status_label(store, store.get_work_order(wo["id"]))

    assert label.startswith("needs_review — Claude usage limit")
    assert "running in parallel" not in label


def test_the_outage_is_seen_when_a_parked_order_is_its_only_evidence(sweep):
    """The window is shut for the ACCOUNT, whatever status the work order that was told
    happens to be in. Read through `ACTIVE_STATUSES` this came back empty, and the OS
    went on dispatching fresh work into a closed window."""
    store, _run, _settle = sweep
    wo = _refused(store, "waiting_pr_merge", error=_window_shut())

    state = fleet.read(cap=5, stores={"proj_a": store})

    assert state.outage is not None and state.outage.wo_id == wo["id"]
    assert state.blocked()


# -- the work order that is already stranded -------------------------------------------


def test_a_work_order_already_in_this_state_is_rescued_rather_than_abandoned(sweep):
    """wo-f35e603e's shape, rebuilt: parked behind a pull request, a refused turn holding
    a due retry, and three messages queued behind it that never arrived. The fix has to
    reach the ones already in it, not only prevent the next — before, the only remedy the
    OS offered was `jarvis wo done`, which throws the work away.

    The transition under test is the diagnosis going quiet for the right reason:
    `stuck_message` calls the silence a defect, and one pass of the sweep turns it into
    an ordinary `accounted` wait on a turn that is now in flight.
    """
    store, run, _settle = sweep
    wo = _refused(store, "waiting_pr_merge")
    store.update_work_order(wo["id"], pr_url="https://github.com/x/y/pull/243")
    for text in ("one", "two", "three"):
        store.queue_message(wo["id"], text)
    stale = time.time() + 24 * 3600  # older than any `messaging.stuck_minutes`
    assert stuck_message(store, store.get_work_order(wo["id"]), now=stale) is not None

    run()

    assert stuck_message(store, store.get_work_order(wo["id"]), now=stale) is None
    hold = worker_session.delivery_hold(store, store.get_work_order(wo["id"]))
    assert hold is not None and hold.accounted, "the queue is moving again"
    assert len(store.queued_messages(wo["id"])) == 3, (
        "the lost turn goes out first; nothing may jump the queue")


@pytest.mark.parametrize("status", NEWLY_SWEPT)
def test_the_drift_check_watches_the_newly_swept_rows_too(sweep, status):
    """INV-PAUSE-DRIFT, the half of the self-healing watch that INV-PAUSE-OVERDUE cannot
    see: a deadline that runs away is always in the FUTURE, so it is never overdue. It
    was widened with the sweep for the same reason and needs its own test to say so —
    a runaway reset on a parked order would otherwise be as silent as the missing
    relaunch was."""
    store, _run, _settle = sweep
    wo = _refused(store, status, error=_window_shut())
    pause = worker_session.turn_pause(store, wo["id"])
    # What `settle_turn` recorded when the turn died, a whole day off what `_diagnose`
    # derives now — the signature of the reset being re-resolved against the asking
    # clock rather than against the turn's.
    store.add_event(wo["id"], "turn_paused",
                    {"seq": pause.turn["seq"], "reset_at": pause.reset_at - 86400})

    reported = [(v.invariant, v.wo_id)
                for v in invariants.check_pause_deadline_stable(store)]

    assert reported == [("INV-PAUSE-DRIFT", wo["id"])]


def test_the_overdue_invariant_is_quiet_under_a_cap_hold_and_returns_after(sweep,
                                                                          monkeypatch):
    """A deferral the sweep took deliberately is not the sweep failing to fire — and
    absence of evidence still is. Suppression lasts exactly as long as the sweep keeps
    restating the hold, so a pass that dies mid-hold is reported within
    `RETRY_HELD_FRESH_FOR` of its last word."""
    store, _run, _settle = sweep
    wo = _refused(store, "running", error=_window_shut())
    pause = worker_session.turn_pause(store, wo["id"])
    # The moment the check would report this order: its wait is over and the whole grace
    # has run out on top.
    asking = pause.retry_at + invariants.PAUSE_OVERDUE_GRACE + 60
    monkeypatch.setattr("jarvis.invariants.time.time", lambda: asking)
    store.add_event(wo["id"], invariants.RETRY_HELD_EVENT,
                    {"cause": "fleet_cap", "seq": pause.turn["seq"],
                     "reason": pause.reason, "retry_at": pause.retry_at,
                     "in_flight": 2, "cap": 2})
    held = store.last_event_of_kind(wo["id"], invariants.RETRY_HELD_EVENT)

    def restated(ago: float) -> None:
        store.conn.execute("UPDATE wo_events SET ts=? WHERE id=?",
                           (asking - ago, held["id"]))

    restated(60)  # the sweep said so a minute ago: it is deferring, not failing
    assert [v.wo_id for v in invariants.check_paused_turns_resume(store)] == []

    # Nothing has restated it since — the pass died, or the hold ended and the relaunch
    # still did not happen. Absence of evidence is a violation again.
    restated(invariants.RETRY_HELD_FRESH_FOR + 60)
    assert [v.wo_id for v in invariants.check_paused_turns_resume(store)] == [wo["id"]]
