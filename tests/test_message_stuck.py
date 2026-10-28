"""A message the user sent that the worker will never see — GitHub issue 43"""

from __future__ import annotations

import pytest

from jarvis import db, ops, worker_session
from jarvis.catalog import DEFAULT_MESSAGING_STUCK_MINUTES, parse_catalog
from jarvis.invariants import (
    MESSAGE_STUCK_BLOCKER,
    PARKED_BLOCKER,
    check_messages_are_delivered,
    check_project,
    parked_reason,
    stuck_message,
    true_blockers,
)
from jarvis.project_store import ProjectStore

#: Comfortably past the shipped 60-minute default, in seconds
LONG_ENOUGH = 90 * 60


def _sent_and_never_delivered(store: ProjectStore, status: str = "running") -> dict:
    """A work order whose turn is reaped and whose message never went out"""
    wo = store.create_work_order("the one that was sent feedback")
    store.update_work_order(wo["id"], session_id="sess-1")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "done", result="waiting on the gate")
    store.set_status(wo["id"], status)
    store.queue_message(wo["id"], "the gate is open, carry on")
    return store.get_work_order(wo["id"])


def _now(store: ProjectStore, wo_id: str) -> float:
    """Long enough after the message was queued for the threshold to bite"""
    return float(store.queued_messages(wo_id)[0]["ts"]) + LONG_ENOUGH


# -- what it catches


def test_a_message_nobody_delivered_becomes_a_blocker(project):
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)

    assert true_blockers(store, wo, now=float(wo["updated_at"])) == []
    assert true_blockers(store, wo, now=_now(store, wo["id"])) == [MESSAGE_STUCK_BLOCKER]


def test_the_blocker_carries_no_elapsed_time(project):
    """INV-ATTENTION-REASON compares it against `attention_reason` and `ack_attention` stores it verbatim"""
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
    """`jarvis doctor` without --repair is a pure read (`invariants._ReadOnly`)"""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    _age_the_message(store, wo["id"])

    found = [v for v in check_project(store, repair=False)
             if v.invariant == "INV-MESSAGE-STUCK"]

    assert found and found[0].repair.startswith("would flag: ")
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_it_never_overwrites_an_older_decision_the_user_owes(project):
    """kn-78346a2d's rule: a work order can owe an assumption review AND be missing a message"""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store, status="needs_review")
    store.add_assumption(wo["id"], "one call I made")
    _age_the_message(store, wo["id"])

    check_project(store)

    assert store.get_work_order(wo["id"])["attention_reason"] == \
        "1 assumption pending your review"
    assert MESSAGE_STUCK_BLOCKER in true_blockers(store, store.get_work_order(wo["id"]))


def test_resume_auto_says_what_is_holding_it_and_does_not_send_another(project):
    """MESSAGE_STUCK_BLOCKER sends the user here, so this command has to be able to answer"""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    assert ops.waiting_on(store, wo)["what"] == "queued_message"
    msg_id = store.queued_messages(wo["id"])[0]["id"]
    _age_the_message(store, wo["id"])

    answer = ops.waiting_on(store, wo)

    assert answer["what"] == "message_stuck"
    assert answer["stalled"] is False
    assert f"message {msg_id}" in answer["detail"]


def test_waiting_on_answers_from_the_instant_it_is_given(project):
    """The passed instant alone flips it; the message row stays current (spec §6.5)"""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)

    assert ops.waiting_on(store, wo)["what"] == "queued_message"
    assert ops.waiting_on(store, wo, now=_now(store, wo["id"]))["what"] == "message_stuck"


def test_the_invariant_answers_from_the_instant_it_is_given(project):
    """`check_project` forwards no instant, so the invariant is called direct (spec §6.5)"""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    msg_id = store.queued_messages(wo["id"])[0]["id"]

    assert list(check_messages_are_delivered(store)) == []

    found = list(check_messages_are_delivered(store, now=_now(store, wo["id"])))

    assert [v.context["msg_id"] for v in found] == [msg_id]


def test_a_stuck_message_speaks_instead_of_the_parked_line(project):
    """One stopped worker, one sentence: PARKED_BLOCKER would only say it went quiet (docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md)"""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store, status="waiting_pr_merge")
    _age_the_message(store, wo["id"])
    _age_the_turn(store, wo["id"])

    assert ops.waiting_on(store, wo)["what"] == "message_stuck"
    assert true_blockers(store, wo) == [MESSAGE_STUCK_BLOCKER]

    store.mark_message(store.queued_messages(wo["id"])[0]["id"], "delivered")

    assert true_blockers(store, wo) == [PARKED_BLOCKER]
    assert parked_reason(store, wo) == PARKED_BLOCKER


# -- the hold it names, one test per branch


def test_it_names_a_missing_session(project):
    """`deliver_messages` skips an order with no `session_id`"""
    store = ProjectStore(project)
    wo = store.create_work_order("dispatched nowhere")
    store.set_status(wo["id"], "running")
    store.queue_message(wo["id"], "carry on")
    _age_the_message(store, wo["id"])
    wo = store.get_work_order(wo["id"])

    assert stuck_message(store, wo)[1] == "it has no session to resume"


def test_it_names_a_retry_that_came_due_and_never_happened(project):
    """The pause is resumable and its moment has passed: INV-PAUSE-OVERDUE's subject"""
    store = ProjectStore(project)
    wo = _paused(store, f"Claude usage limit reached|{int(db.now()) - 3600}",
                 "usage_limit")

    assert stuck_message(store, wo)[1] == \
        "its usage-limit retry came due and has not happened"


def test_a_pause_that_will_never_retry_does_not_hold_delivery(project):
    """An auth pause whose sign-in has not changed since the turn died: `retry_at` is `NEVER`"""
    store = ProjectStore(project)
    wo = _paused(store, "Invalid API key · Please run /login", "auth_error")

    assert worker_session.delivery_hold(store, wo) is None
    assert stuck_message(store, wo)[1] == "the delivery pass has not attempted it"


def test_the_delivery_pass_and_the_diagnosis_read_the_same_decision(project):
    """The seam review round 2 asked for"""
    store = ProjectStore(project)
    free = _sent_and_never_delivered(store)
    _age_the_message(store, free["id"])
    held = _sent_and_never_delivered(store)
    store.create_turn(held["id"], "message", "an earlier message")
    _age_the_message(store, held["id"])

    assert worker_session.delivery_hold(store, free) is None
    assert stuck_message(store, free)[1] == "the delivery pass has not attempted it"

    hold = worker_session.delivery_hold(store, held)

    assert hold.kind == worker_session.HOLD_TURN_IN_FLIGHT and hold.accounted
    assert stuck_message(store, held) is None


# -- every reader of the new `waiting_on` answer


def test_resume_auto_refuses_to_nudge_even_where_a_worker_could_prompt(project,
                                                                      jarvis_home,
                                                                      catalog_file):
    """The reader review round 3 named"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    store.update_work_order(wo["id"], permission_mode="default")  # CAN prompt
    _age_the_message(store, wo["id"])
    before = len(store.queued_messages(wo["id"]))
    store.close()

    out = ops.resume_in_auto(wo["id"], project_name="proj_a")

    assert out["waiting_on"] == "message_stuck"
    assert out["nudged"] is False
    assert "a nudge cannot help" in out["note"]
    store = ProjectStore(project)
    assert len(store.queued_messages(wo["id"])) == before  # nothing was added


def test_the_work_order_page_says_what_is_holding_the_message(project, jarvis_home,
                                                              catalog_file):
    """The UI reader"""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store, status="waiting_input")
    msg_id = store.queued_messages(wo["id"])[0]["id"]
    _age_the_message(store, wo["id"])
    store.close()

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo['id']}").text

    assert f"message {msg_id} is queued undelivered" in page
    assert "Resume in auto" not in page


def test_the_parked_check_is_the_third_reader(project):
    """`invariants.SPOKEN_FOR_WAITS`"""
    from jarvis.invariants import IN_FLIGHT_WAITS, SPOKEN_FOR_WAITS

    assert "message_stuck" in SPOKEN_FOR_WAITS
    assert "message_stuck" not in IN_FLIGHT_WAITS  # nothing is in flight for it


# -- the noise rule: every wait something else already owns


def test_a_fresh_message_is_not_stuck(project):
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)

    assert stuck_message(store, wo, now=float(wo["updated_at"])) is None


def test_a_message_behind_a_turn_in_flight_is_not_stuck(project):
    """Delivery waits for the turn by design"""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    store.create_turn(wo["id"], "message", "an earlier message")

    assert stuck_message(store, wo, now=_now(store, wo["id"])) is None


def test_a_message_behind_a_booked_retry_is_not_stuck(project):
    """A usage limit legitimately holds a message until the reset"""
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
    """It goes out as the order's second turn"""
    store = ProjectStore(project)
    first = store.create_work_order("the one it waits for")
    wo = store.create_work_order("the waiter", depends_on=[first["id"]])
    store.queue_message(wo["id"], "one more thing")
    wo = store.get_work_order(wo["id"])

    assert wo["status"] == "pending"
    assert stuck_message(store, wo, now=_now(store, wo["id"])) is None


def test_an_acknowledged_stuck_message_stays_reported_but_stops_flagging(project):
    """`jarvis wo ack` answers the user's flag; it does not make the fleet healthy"""
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    _age_the_message(store, wo["id"])
    check_project(store)
    store.ack_attention(wo["id"], [MESSAGE_STUCK_BLOCKER])

    found = [v for v in check_project(store) if v.invariant == "INV-MESSAGE-STUCK"]

    assert found and not found[0].repaired
    assert not store.get_work_order(wo["id"])["needs_attention"]


# -- the threshold is a setting, not a constant (kn-67cdb54b)


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
    """Zero would call every message the fleet has just queued stuck"""
    from jarvis.catalog import CatalogError

    with pytest.raises(CatalogError, match="messaging.stuck_minutes"):
        parse_catalog({"os": {"messaging": {"stuck_minutes": 0}}, "projects": []})


def test_the_shipped_default_is_an_hour():
    assert DEFAULT_MESSAGING_STUCK_MINUTES == 60


# -- helpers


def _paused(store: ProjectStore, error: str, terminal_reason: str) -> dict:
    """A work order whose last turn died in a pause, with an aged message behind it"""
    wo = store.create_work_order("parked on the way through")
    store.update_work_order(wo["id"], session_id="sess-1")
    store.set_status(wo["id"], "running")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "failed", error=error, terminal_reason=terminal_reason)
    store.queue_message(wo["id"], "carry on")
    _age_the_message(store, wo["id"])
    # The turn too: a usage-limit pause is anchored to it (`retry_at` is at least `ended +...
    _age_the_turn(store, wo["id"])
    return store.get_work_order(wo["id"])


def _age_the_turn(store: ProjectStore, wo_id: str) -> None:
    """Age the settled turn past `inspect.alarm_parked_minutes`"""
    store.conn.execute(
        "UPDATE wo_turns SET started_at=started_at-?, ended_at=ended_at-? WHERE wo_id=?",
        (LONG_ENOUGH, LONG_ENOUGH, wo_id))


def _age_the_message(store: ProjectStore, wo_id: str) -> None:
    """Age the queued message past the threshold in the ROW rather than on the clock"""
    store.conn.execute("UPDATE wo_messages SET ts=ts-? WHERE wo_id=?",
                       (LONG_ENOUGH, wo_id))
