"""A message the user sent that the worker will never see — GitHub issue 43.

The issue's root cause is gone: `Daemon.deliver_messages` skipped any session that was
not `is_finished`, and `blocked` is neither, so a worker waiting on a gate verdict never
received one. PR 46 replaced supervised background sessions with the headless turn
runtime and that guard with `worker_session.busy`, so no state can be permanently
undeliverable any more.

What the issue asked for and nothing built is the second half: the SILENCE. The row stays
`queued`, which from every surface looks exactly like one about to go out, and
`ops.waiting_on` answered `queued_message` — a member of `IN_FLIGHT_WAITS` — so the fleet
reported healthy while fifteen messages across four work orders rotted, a gate verdict
among them. These tests are that half: the flag, the diagnosis, and the four waits that
must stay silent because something else already owns them.
"""

from __future__ import annotations

import pytest

from jarvis import db, ops
from jarvis.catalog import DEFAULT_MESSAGING_STUCK_MINUTES, parse_catalog
from jarvis.invariants import (
    MESSAGE_STUCK_BLOCKER,
    check_project,
    parked_reason,
    stuck_message,
    true_blockers,
)
from jarvis.project_store import ProjectStore

#: Comfortably past the shipped 60-minute default, in seconds.
LONG_ENOUGH = 90 * 60


def _sent_and_never_delivered(store: ProjectStore, status: str = "running") -> dict:
    """A work order whose turn is reaped and whose message never went out."""
    wo = store.create_work_order("the one that was sent feedback")
    store.update_work_order(wo["id"], session_id="sess-1")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "done", result="waiting on the gate")
    store.set_status(wo["id"], status)
    store.queue_message(wo["id"], "the gate is open, carry on")
    return store.get_work_order(wo["id"])


def _now(store: ProjectStore, wo_id: str) -> float:
    """Long enough after the message was queued for the threshold to bite."""
    return float(store.queued_messages(wo_id)[0]["ts"]) + LONG_ENOUGH


# -- what it catches -------------------------------------------------------------------


def test_a_message_nobody_delivered_becomes_a_blocker(project):
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)

    assert true_blockers(store, wo, now=float(wo["updated_at"])) == []
    assert true_blockers(store, wo, now=_now(store, wo["id"])) == [MESSAGE_STUCK_BLOCKER]


def test_the_blocker_carries_no_elapsed_time(project):
    """INV-ATTENTION-REASON compares it against `attention_reason` and `ack_attention`
    stores it verbatim, so a reason that ticked could never be acknowledged."""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)

    early = true_blockers(store, wo, now=_now(store, wo["id"]))
    late = true_blockers(store, wo, now=_now(store, wo["id"]) + 10 * LONG_ENOUGH)

    assert early == late == [MESSAGE_STUCK_BLOCKER]


def test_the_invariant_flags_it_and_names_the_message(project):
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    msg_id = store.queued_messages(wo["id"])[0]["id"]
    _age_the_message(store, wo["id"])

    found = [v for v in check_project(store) if v.invariant == "INV-MESSAGE-STUCK"]

    assert len(found) == 1
    assert found[0].context["msg_id"] == msg_id
    assert found[0].context["hold"] == "the delivery pass has not attempted it"
    assert found[0].repaired
    assert store.get_work_order(wo["id"])["attention_reason"] == MESSAGE_STUCK_BLOCKER


def test_doctor_describes_the_flag_instead_of_raising_it(project):
    """`jarvis doctor` without --repair is a pure read (`invariants._ReadOnly`)."""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    _age_the_message(store, wo["id"])

    found = [v for v in check_project(store, repair=False)
             if v.invariant == "INV-MESSAGE-STUCK"]

    assert found and found[0].repair.startswith("would flag: ")
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_it_never_overwrites_an_older_decision_the_user_owes(project):
    """kn-78346a2d's rule: a work order can owe an assumption review AND be missing a
    message. The flag keeps the most actionable reason; the message is the second line."""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store, status="needs_review")
    store.add_assumption(wo["id"], "one call I made")
    _age_the_message(store, wo["id"])

    check_project(store)

    assert store.get_work_order(wo["id"])["attention_reason"] == \
        "1 assumption pending your review"
    assert MESSAGE_STUCK_BLOCKER in true_blockers(store, store.get_work_order(wo["id"]))


def test_resume_auto_says_what_is_holding_it_and_does_not_send_another(project):
    """MESSAGE_STUCK_BLOCKER sends the user here, so this command has to be able to
    answer — and `stalled` must stay False, because a nudge is another message into the
    same stalled queue.
    """
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    assert ops.waiting_on(store, wo)["what"] == "queued_message"
    msg_id = store.queued_messages(wo["id"])[0]["id"]
    _age_the_message(store, wo["id"])

    answer = ops.waiting_on(store, wo)

    assert answer["what"] == "message_stuck"
    assert answer["stalled"] is False
    assert f"message {msg_id}" in answer["detail"]


def test_a_stuck_message_speaks_instead_of_the_parked_line(project):
    """One stopped worker, one sentence: PARKED_BLOCKER would only say it went quiet."""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)

    assert parked_reason(store, wo, now=_now(store, wo["id"])) is None


# -- the noise rule: every wait something else already owns -----------------------------


def test_a_fresh_message_is_not_stuck(project):
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)

    assert stuck_message(store, wo, now=float(wo["updated_at"])) is None


def test_a_message_behind_a_turn_in_flight_is_not_stuck(project):
    """Delivery waits for the turn by design; a turn that runs too long is
    `inspect.alarm_turn_minutes`' subject."""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    store.create_turn(wo["id"], "message", "an earlier message")

    assert stuck_message(store, wo, now=_now(store, wo["id"])) is None


def test_a_message_behind_a_booked_retry_is_not_stuck(project):
    """A usage limit legitimately holds a message until the reset. INV-PAUSE-OVERDUE is
    what fires if the relaunch then does not happen."""
    store = ProjectStore(project)
    wo = store.create_work_order("hit the usage limit")
    store.update_work_order(wo["id"], session_id="sess-1")
    store.set_status(wo["id"], "running")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "failed",
                      error=f"Claude usage limit reached|{int(db.now()) + 4 * 3600}",
                      terminal_reason="usage_limit")
    store.queue_message(wo["id"], "carry on")
    wo = store.get_work_order(wo["id"])

    assert stuck_message(store, wo, now=_now(store, wo["id"])) is None


def test_a_message_on_an_undispatched_order_is_not_stuck(project):
    """It goes out as the order's second turn, and a dependency-blocked order is never an
    attention item just for waiting."""
    store = ProjectStore(project)
    first = store.create_work_order("the one it waits for")
    wo = store.create_work_order("the waiter", depends_on=[first["id"]])
    store.queue_message(wo["id"], "one more thing")
    wo = store.get_work_order(wo["id"])

    assert wo["status"] == "pending"
    assert stuck_message(store, wo, now=_now(store, wo["id"])) is None


def test_an_acknowledged_stuck_message_stays_reported_but_stops_flagging(project):
    """`jarvis wo ack` answers the user's flag; it does not make the fleet healthy."""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    _age_the_message(store, wo["id"])
    check_project(store)
    store.ack_attention(wo["id"], [MESSAGE_STUCK_BLOCKER])

    found = [v for v in check_project(store) if v.invariant == "INV-MESSAGE-STUCK"]

    assert found and not found[0].repaired
    assert not store.get_work_order(wo["id"])["needs_attention"]


# -- the threshold is a setting, not a constant (kn-67cdb54b) ---------------------------


def test_the_threshold_is_per_project(project):
    catalog = parse_catalog({
        "os": {"messaging": {"stuck_minutes": 30}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "messaging": {"stuck_minutes": 5}},
                     {"name": "proj_b", "path": str(project)}],
    })

    assert catalog.os.messaging.stuck_minutes == 30
    assert catalog.project("proj_a").messaging.stuck_minutes == 5
    assert catalog.project("proj_b").messaging.stuck_minutes == 30  # inherited


def test_a_zero_threshold_is_refused_rather_than_clamped():
    """Zero would call every message the fleet has just queued stuck, and it arrives by
    a typo in a `jarvis config set` that the parser is the last place able to name."""
    from jarvis.catalog import CatalogError

    with pytest.raises(CatalogError, match="messaging.stuck_minutes"):
        parse_catalog({"os": {"messaging": {"stuck_minutes": 0}}, "projects": []})


def test_the_shipped_default_is_an_hour():
    assert DEFAULT_MESSAGING_STUCK_MINUTES == 60


# -- helpers ---------------------------------------------------------------------------


def _age_the_message(store: ProjectStore, wo_id: str) -> None:
    """Age the queued message past the threshold in the ROW rather than on the clock.

    `check_project` and `ops.waiting_on` read `time.time()` themselves and take no `now`,
    so the fixture is what has to move — and moving `db.now` instead would make every
    other row in the fixture move with it.
    """
    store.conn.execute("UPDATE wo_messages SET ts=ts-? WHERE wo_id=?",
                       (LONG_ENOUGH, wo_id))
