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

from jarvis import db, ops, worker_session
from jarvis.catalog import DEFAULT_MESSAGING_STUCK_MINUTES, parse_catalog
from jarvis.invariants import (
    MESSAGE_STUCK_BLOCKER,
    PARKED_BLOCKER,
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
    """One stopped worker, one sentence: PARKED_BLOCKER would only say it went quiet.

    THE FIXTURE HAS TO BE OLD IN THE ROWS, not in a `now` argument. `parked_reason` takes
    one and `ops.waiting_on`, which it consults, does not — it reads `time.time()` itself
    — so passing `now=` ages only half the question and the `None` comes back from
    `queued_message`'s pre-existing IN_FLIGHT_WAITS membership, which is exactly what
    this test must not be able to pass on. The second assertion is the negative control:
    with the message delivered, the same fixture IS parked, which is what proves the
    check was armed and makes reverting `SPOKEN_FOR_WAITS` fail here.
    """
    store = ProjectStore(project)
    wo = _sent_and_never_delivered(store)
    _age_the_message(store, wo["id"])
    _age_the_turn(store, wo["id"])

    assert ops.waiting_on(store, wo)["what"] == "message_stuck"
    assert parked_reason(store, wo) is None

    store.mark_message(store.queued_messages(wo["id"])[0]["id"], "delivered")

    assert parked_reason(store, wo) == PARKED_BLOCKER


# -- the hold it names, one test per branch ---------------------------------------------
# `stuck_message`'s reason is read by a doctor invariant AND by `ops.waiting_on`, so the
# `worker_session.PAUSE_NOUN` lookup in two of these runs in front of the user.


def test_it_names_a_missing_session(project):
    """`deliver_messages` skips an order with no `session_id`, and nothing carries the
    message into a dispatch prompt — so one that never dispatches holds it for ever."""
    store = ProjectStore(project)
    wo = store.create_work_order("dispatched nowhere")
    store.set_status(wo["id"], "running")
    store.queue_message(wo["id"], "carry on")
    _age_the_message(store, wo["id"])
    wo = store.get_work_order(wo["id"])

    assert stuck_message(store, wo)[1] == "it has no session to resume"


def test_it_names_a_retry_that_came_due_and_never_happened(project):
    """The pause is resumable and its moment has passed: INV-PAUSE-OVERDUE's subject,
    with the user's message waiting behind it."""
    store = ProjectStore(project)
    wo = _paused(store, f"Claude usage limit reached|{int(db.now()) - 3600}",
                 "usage_limit")

    assert stuck_message(store, wo)[1] == \
        "its usage-limit retry came due and has not happened"


def test_a_pause_that_will_never_retry_does_not_hold_delivery(project):
    """An auth pause whose sign-in has not changed since the turn died: `retry_at` is
    `NEVER`, so nothing will relaunch that turn and the message is the only thing that
    can restart the conversation. `delivery_hold` therefore returns None and the message
    is diagnosed as one the pass was FREE to send — which is the finding, because if it
    really cannot be sent `Daemon._deliver` marks it `failed` and flags.
    """
    store = ProjectStore(project)
    wo = _paused(store, "Invalid API key · Please run /login", "auth_error")

    assert worker_session.delivery_hold(store, wo) is None
    assert stuck_message(store, wo)[1] == "the delivery pass has not attempted it"


def test_the_delivery_pass_and_the_diagnosis_read_the_same_decision(project):
    """The seam review round 2 asked for. A fourth skip added to `deliver_messages`
    used to leave the diagnosis saying "the delivery pass has not attempted it" about a
    message the pass had just declined; both now go through `delivery_hold`, so a hold
    it reports is a hold the loop honours and vice versa."""
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


def _paused(store: ProjectStore, error: str, terminal_reason: str) -> dict:
    """A work order whose last turn died in a pause, with an aged message behind it."""
    wo = store.create_work_order("parked on the way through")
    store.update_work_order(wo["id"], session_id="sess-1")
    store.set_status(wo["id"], "running")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "failed", error=error, terminal_reason=terminal_reason)
    store.queue_message(wo["id"], "carry on")
    _age_the_message(store, wo["id"])
    # The turn too: a usage-limit pause is anchored to it (`retry_at` is at least
    # `ended + RATE_LIMIT_MIN_DELAY`), so a turn that died a moment ago is never overdue
    # however far back the refusal's own reset moment is set.
    _age_the_turn(store, wo["id"])
    return store.get_work_order(wo["id"])


def _age_the_turn(store: ProjectStore, wo_id: str) -> None:
    """Age the settled turn past `inspect.alarm_parked_minutes`, so `parked_reason` gets
    as far as asking `ops.waiting_on` at all."""
    store.conn.execute(
        "UPDATE wo_turns SET started_at=started_at-?, ended_at=ended_at-? WHERE wo_id=?",
        (LONG_ENOUGH, LONG_ENOUGH, wo_id))


def _age_the_message(store: ProjectStore, wo_id: str) -> None:
    """Age the queued message past the threshold in the ROW rather than on the clock.

    `check_project` and `ops.waiting_on` read `time.time()` themselves and take no `now`,
    so the fixture is what has to move — and moving `db.now` instead would make every
    other row in the fixture move with it.
    """
    store.conn.execute("UPDATE wo_messages SET ts=ts-? WHERE wo_id=?",
                       (LONG_ENOUGH, wo_id))
