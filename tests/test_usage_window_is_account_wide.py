"""A spent usage window is a fact about the ACCOUNT, and every order it holds says so (#714)"""

from __future__ import annotations

import time

import pytest

from jarvis import db, fleet as fleet_mod
from jarvis.invariants import (CI_HOLD_NOTE, FLEET_HELD_STATUSES, fleet_hold_note,
                               parallel_round_note, status_label, validation_hold_note)
from jarvis.project_store import (VALIDATION_CI_CAUSE, VALIDATION_HELD_CAUSE,
                                  validation_hold)
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish, passed)

REFUSAL = "You've hit your session limit · resets 11:50pm (America/Los_Angeles)"


def outage(in_seconds: float, wo_id: str = "wo-1") -> fleet_mod.Outage:
    return fleet_mod.Outage(project="proj_a", wo_id=wo_id,
                            reopens_at=db.now() + in_seconds, message=REFUSAL)


def shut(in_seconds: float = 3600, wo_id: str = "wo-1") -> fleet_mod.Fleet:
    return fleet_mod.Fleet(3, 0, outage(in_seconds, wo_id))


def shut_then_reopened() -> fleet_mod.Fleet:
    """A reading taken DURING a window that has since reopened"""
    return fleet_mod.Fleet(3, 0, outage(-1), at=db.now() - 3600)


def held_events(store, wo_id: str) -> list[dict]:
    return [e for e in store.events_of_kind(wo_id, "validation_failed")
            if VALIDATION_HELD_CAUSE in (e["payload"] or "")]


def parked(fleet, assume: bool = False) -> dict:
    """A work order delivered, with round one open beside it"""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    return wo


# -- 1. the tick that skips a round must say that it skipped it


def test_a_round_the_shut_window_skips_records_the_hold(fleet):
    """Defect 2, stated as a test"""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    wo = parked(fleet)
    state = shut()

    store = fleet.store()
    try:
        fleet.daemon.validation_tick(fleet.spec, store, state)

        assert validator.calls == [], "a seat was called into a closed window"
        assert [(r["round"], r["outcome"]) for r in
                store.validation_rounds(wo_id=wo["id"])] == [(1, "pending")], (
            "the hold spent the round it was supposed to protect")
        events = held_events(store, wo["id"])
        assert len(events) == 1
        until, cause = validation_hold(store.events_of_kind(wo["id"],
                                                            "validation_failed"), 1)
        assert cause == VALIDATION_HELD_CAUSE
        assert until == pytest.approx(state.outage.reopens_at)
    finally:
        store.close()


def test_the_hold_is_written_once_however_many_ticks_the_window_spans(fleet):
    """At a 5s tick the outage in #714 covered ~2,100 of them"""
    fleet.daemon.validator = Validator(passed())
    wo = parked(fleet)
    state = shut()

    store = fleet.store()
    try:
        for _ in range(5):
            fleet.daemon.validation_tick(fleet.spec, store, state)
        assert len(held_events(store, wo["id"])) == 1
    finally:
        store.close()


def test_a_second_window_after_the_first_reopened_is_recorded_again(fleet):
    """The dedupe is on the MOMENT, not on "has this round ever been held": a window that reopened and..."""
    fleet.daemon.validator = Validator(passed())
    wo = parked(fleet)

    store = fleet.store()
    try:
        fleet.daemon.validation_tick(fleet.spec, store, shut(60))
        later = shut(7200)
        fleet.daemon.validation_tick(fleet.spec, store, later)

        assert len(held_events(store, wo["id"])) == 2
        until, _ = validation_hold(
            store.events_of_kind(wo["id"], "validation_failed"), 1)
        assert until == pytest.approx(later.outage.reopens_at), "the newest must win"
    finally:
        store.close()


def test_the_recorded_hold_does_not_outlast_the_window(fleet):
    """THE DEFINITION OF DONE: no human touch anywhere"""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    wo = parked(fleet)

    store = fleet.store()
    try:
        fleet.daemon.validation_tick(fleet.spec, store, shut_then_reopened())
        assert held_events(store, wo["id"]), "the hold was never recorded"
    finally:
        store.close()

    fleet.drain()  # the window it named has already reopened

    store = fleet.store()
    try:
        assert len(validator.calls) == 1
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "passed"
    finally:
        store.close()


def test_a_round_already_in_flight_is_left_alone(fleet):
    """It was submitted before the window shut and the pool thread owns its outcome"""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    validator.block()
    wo = parked(fleet)

    store = fleet.store()
    try:
        fleet.daemon.validation_tick(fleet.spec, store)  # no outage: it goes
        assert validator.entered.wait(timeout=15)
        fleet.daemon.validation_tick(fleet.spec, store, shut())

        assert held_events(store, wo["id"]) == []
    finally:
        validator.release.set()
        deadline = time.monotonic() + 15
        while fleet.daemon.validating and time.monotonic() < deadline:
            time.sleep(0.01)
        store.close()


# -- 2. and the surfaces must read it


def test_the_parked_work_order_names_the_window_it_is_waiting_on(fleet):
    """The live failure: `needs_review` with a round open beside it"""
    fleet.daemon.validator = Validator(passed())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    from jarvis import ops
    ops.assume(wo["id"], "assumed the exporter writes UTF-8")
    assert finish(fleet, wo["id"])["status"] == "needs_review"

    store = fleet.store()
    try:
        fresh = store.get_work_order(wo["id"])
        assert parallel_round_note(store, wo["id"]) == \
            " — review round 1 is running in parallel", "the fixture is not the case"

        fleet.daemon.validation_tick(fleet.spec, store, shut())

        note = parallel_round_note(store, wo["id"])
        assert "is held" in note and "usage window is spent" in note
        assert "usage window is spent" in status_label(store, fresh)
    finally:
        store.close()


def test_a_validating_order_says_it_before_the_daemon_has_recorded_anything(fleet):
    """The window can shut between two ticks"""
    fleet.daemon.validator = Validator(passed())
    wo = parked(fleet)

    store = fleet.store()
    try:
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "validating"
        assert "usage window" not in status_label(store, fresh), "no outage, no line"
        assert "usage window is spent" in status_label(store, fresh, shut())
    finally:
        store.close()


def test_a_running_order_between_turns_says_it_too(fleet):
    """Defect 1"""
    wo = fleet.dispatch()

    store = fleet.store()
    try:
        store.set_status(wo["id"], "running")
        fresh = store.get_work_order(wo["id"])
        from jarvis.invariants import pause_note
        assert pause_note(store, fresh) == "", "the fixture staged a refused turn"
        assert "usage window is spent" in status_label(store, fresh, shut())
    finally:
        store.close()


# -- 3. ...and only the orders the window actually holds


@pytest.mark.parametrize("status", FLEET_HELD_STATUSES)
def test_every_status_the_os_would_start_something_for_carries_the_line(status):
    assert "usage window is spent" in fleet_hold_note({"status": status}, shut())


@pytest.mark.parametrize("status", ["needs_review", "waiting_pr_merge", "waiting_input",
                                    "idle", "completed", "budget_exhausted"])
def test_the_line_stays_off_the_orders_the_window_is_not_what_holds(status):
    """Neo, question 563"""
    assert fleet_hold_note({"status": status}, shut()) == ""


def test_an_open_account_says_nothing_at_all():
    """Including the slot cap, which holds a worker turn and never a work order's explanation of itself"""
    assert fleet_hold_note({"status": "running"}, None) == ""
    assert fleet_hold_note({"status": "running"}, fleet_mod.Fleet(3, 0, None)) == ""
    assert fleet_hold_note({"status": "running"}, shut(-1)) == "", "a reopened window"


# -- 4. the inverse defect: CI is not a usage window


class _Events:
    """A store, as much of one as `validation_hold_note` reads"""

    def __init__(self, *payloads: str):
        self.rows = [{"payload": p} for p in payloads]

    def events_of_kind(self, wo_id, kind):
        return self.rows


def _payload(cause: str, reopens_at: float) -> str:
    return '{"round": 1, "cause": "%s", "reopens_at": %f}' % (cause, reopens_at)


def test_a_round_waiting_on_ci_is_never_described_as_a_spent_window():
    """The live text: "the Claude usage window is spent"""
    note = validation_hold_note(_Events(_payload(VALIDATION_CI_CAUSE,
                                                 db.now() + 60)), "wo-1", 1)
    assert note == CI_HOLD_NOTE
    assert "usage window" not in note


def test_a_round_held_by_the_window_still_names_the_window_and_the_moment():
    note = validation_hold_note(_Events(_payload(VALIDATION_HELD_CAUSE,
                                                 db.now() + 3600)), "wo-1", 1)
    assert "usage window is spent" in note and "resumes by itself at" in note


def test_the_cause_that_wins_is_the_one_the_tick_is_honouring():
    """A window that shut while CI was still running holds for the LATER moment"""
    now = db.now()
    both = _Events(_payload(VALIDATION_CI_CAUSE, now + 60),
                   _payload(VALIDATION_HELD_CAUSE, now + 3600))
    assert "usage window is spent" in validation_hold_note(both, "wo-1", 1)

    ci_last = _Events(_payload(VALIDATION_HELD_CAUSE, now + 60),
                      _payload(VALIDATION_CI_CAUSE, now + 3600))
    assert validation_hold_note(ci_last, "wo-1", 1) == CI_HOLD_NOTE


def test_a_lifted_hold_says_nothing():
    assert validation_hold_note(_Events(_payload(VALIDATION_HELD_CAUSE,
                                                 db.now() - 1)), "wo-1", 1) == ""
    assert validation_hold_note(_Events(), "wo-1", 1) == ""
