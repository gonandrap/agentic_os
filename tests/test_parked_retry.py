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
from jarvis.daemon import Daemon
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

    yield store, run
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
    store, run = sweep
    wo = _refused(store, status)
    pause = worker_session.turn_pause(store, wo["id"])
    assert pause is not None and pause.resumable and pause.due()

    run()

    assert store.latest_turn(wo["id"])["state"] != "failed", (
        f"the pause was resumable and due in `{status}` and nothing relaunched it")
    assert "turn_resumed" in [e["kind"] for e in store.list_events(wo["id"])]


@pytest.mark.parametrize("status", ("waiting_pr_merge", "needs_review", "failed"))
def test_the_relaunched_work_order_stops_saying_somebody_else_has_it(sweep, status):
    """The turn is out, so the record must not go on saying the work order is settled —
    and `running` is the only status `PR_POLL_STATUSES` is allowed to skip, which is what
    stops `complete_merged` ending a work order out from under the worker it just woke.
    `settle_work_order` puts it back on the merge queue when this turn is done."""
    store, run = sweep
    wo = _refused(store, status)

    run()

    assert store.get_work_order(wo["id"])["status"] == "running"


def test_a_pause_that_is_not_due_is_left_alone(sweep):
    """The widening is about WHERE a retry fires, not WHEN. A booked retry still waits."""
    store, run = sweep
    wo = store.create_work_order("ship the thing", origin="jarvis")
    store.update_work_order(wo["id"], session_id=worker_session.new_session_id())
    turn = store.create_turn(wo["id"], kind="message", prompt="carry on")
    store.finish_turn(turn["id"], "failed",
                      error="You've hit your session limit · resets 11:50pm (UTC)")
    store.set_status(wo["id"], "waiting_pr_merge")

    run()

    assert store.latest_turn(wo["id"])["seq"] == turn["seq"], "it was not due yet"


@pytest.mark.parametrize("status", ("waiting_pr_merge", "needs_review", "failed"))
def test_an_exhausted_pause_is_not_retried_and_is_surfaced_instead(sweep, status):
    """The negative half. Out of retries is the one state where 'the OS will do it'
    stops being true, so nothing here may quietly keep trying — and `delivery_hold`
    releases the queued message rather than holding it behind a turn that is never
    going out, which is the escape hatch the stranded work order never had."""
    store, run = sweep
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
    store, run = sweep
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
    store, _ = sweep
    wo = _refused(store, status)
    monkeypatch.setattr(
        "jarvis.invariants.time.time",
        lambda: worker_session.turn_pause(store, wo["id"]).retry_at
        + invariants.PAUSE_OVERDUE_GRACE + 60)

    reported = [v.wo_id for v in invariants.check_paused_turns_resume(store)]

    assert reported == [wo["id"]]


# -- what the widened sweep owes the surfaces that report it ---------------------------


@pytest.mark.parametrize("status", ("waiting_pr_merge", "needs_review", "failed"))
def test_a_booked_retry_is_visible_where_the_work_order_is_parked(sweep, status):
    """Neo, question 312: a relaunch the OS will perform has to be readable where it
    will happen, or #259 is fixed in the sweep and still blind on the page. The note is
    the one string every surface renders (the CLI through `status_label`, the dashboard
    through `ops.os_status`), so widening it covers all of them at once."""
    store, _ = sweep
    wo = _refused(store, status, error=_window_shut())

    assert "retrying by itself at" in invariants.pause_note(store, wo)
    assert invariants.status_label(store, wo).startswith(f"{status} — Claude usage limit")


def test_the_parked_note_does_not_displace_the_round_note(sweep):
    """The new branch sits between the two and fires only on a pause, so a `needs_review`
    order with a round open and no pause reads exactly as it did before."""
    store, _ = sweep
    wo = store.create_work_order("ship the thing", origin="jarvis")
    store.set_status(wo["id"], "needs_review")

    assert invariants.status_label(store, store.get_work_order(wo["id"])) == "needs_review"


def test_the_outage_is_seen_when_a_parked_order_is_its_only_evidence(sweep):
    """The window is shut for the ACCOUNT, whatever status the work order that was told
    happens to be in. Read through `ACTIVE_STATUSES` this came back empty, and the OS
    went on dispatching fresh work into a closed window."""
    store, _ = sweep
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
    store, run = sweep
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
