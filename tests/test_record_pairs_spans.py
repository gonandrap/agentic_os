"""The record and the spans say the same thing (io-df4fa1e3 `record-omits-the-transitions`)"""

from __future__ import annotations

import pytest

from jarvis import catalog, invariants, supervisor
from jarvis.invariants import check_project
from jarvis.project_store import ProjectStore

CFG = catalog.SupervisorConfig()
HOUR = 3600.0


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(tmp_path / "proj")
    yield s
    s.close()


def _record_behind(store) -> list:
    return [v for v in check_project(store, repair=False)
            if v.invariant == invariants.RECORD_BEHIND]


# -- the reader: a long-lived order's newest transitions are the ones it must show


def test_list_events_keeps_the_newest_when_it_caps(store):
    """wo-9f00e3b5 had 401 events and every reader saw the OLDEST 200 — its last week was invisible"""
    wo = store.create_work_order("t")
    for i in range(10):
        store.add_event(wo["id"], "note", {"i": i}, ts=wo["created_at"] + 10 + i)

    capped = store.list_events(wo["id"], limit=3)

    # Newest three, still oldest first
    assert [e["payload"] for e in capped] == ['{"i": 7}', '{"i": 8}', '{"i": 9}']


# -- the writers


def test_the_atomic_claim_puts_its_transition_on_the_timeline(store):
    """The claim wrote a span and no event: 268 orders in the live store had no `dispatching` line"""
    wo = store.create_work_order("t")

    store.claim_next_pending()

    span = store.state_spans(wo["id"])[-1]
    (event,) = store.events_of_kind(wo["id"], "status")
    assert span["to_status"] == "dispatching"
    assert '"dispatching"' in event["payload"]
    assert event["ts"] == span["ts"]
    assert _record_behind(store) == []


def test_a_stranded_round_closed_by_the_doctor_says_so_on_the_timeline(store):
    """INV-VALIDATION-STRANDED closed a round `failed` and wrote no outcome event"""
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "validating")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="f")
    store.conn.execute("UPDATE validation_rounds SET ts=ts-? WHERE id=?",
                       (30 * 24 * HOUR, rnd["id"]))
    store.conn.commit()

    found = [v for v in check_project(store)
             if v.invariant == "INV-VALIDATION-STRANDED"]

    assert [v.wo_id for v in found] == [wo["id"]]
    (event,) = store.events_of_kind(wo["id"], "validation_failed")
    assert '"round": 1' in event["payload"]
    assert _record_behind(store) == []


# -- the invariant


def test_a_clean_order_has_nothing_behind(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="f")
    store.close_validation_round(rnd["id"], "passed")
    store.add_event(wo["id"], "validation_passed", {"round": 1, "round_id": rnd["id"]})

    assert _record_behind(store) == []


def test_a_span_with_no_status_event_is_reported_and_not_backfilled(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")
    # A writer that recorded the span and nothing else
    store._record_span(wo["id"], "wo", "running", "validating")
    store.conn.execute("UPDATE work_orders SET status='validating' WHERE id=?",
                       (wo["id"],))
    store.conn.commit()
    status_before = len(store.events_of_kind(wo["id"], "status"))

    (found,) = [v for v in check_project(store) if v.invariant == invariants.RECORD_BEHIND]

    assert found.wo_id == wo["id"]
    assert "validating" in found.detail
    assert not found.repaired
    # REPORT, NEVER BACKFILL: a retrospective event with an invented moment is worse
    assert len(store.events_of_kind(wo["id"], "status")) == status_before


def test_a_settled_round_with_no_outcome_event_is_reported(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "validating")
    store.add_event(wo["id"], "validation_passed", {"round": 1})
    first = store.open_validation_round(wo_id=wo["id"], fingerprint="a")
    store.close_validation_round(first["id"], "passed")
    second = store.open_validation_round(wo_id=wo["id"], fingerprint="b")
    store.close_validation_round(second["id"], "passed")

    (found,) = _record_behind(store)

    assert found.wo_id == wo["id"]
    assert "round 2" in found.detail and "passed" in found.detail


def test_an_approximate_repair_span_is_not_a_transition_the_record_owes(store):
    """INV-SPAN-BEHIND's own repair stands for a write whose event is already on the timeline"""
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")
    store.conn.execute("UPDATE work_orders SET status='needs_review' WHERE id=?",
                       (wo["id"],))
    store.conn.commit()

    found = check_project(store)

    assert "INV-SPAN-BEHIND" in [v.invariant for v in found if v.wo_id == wo["id"]]
    assert _record_behind(store) == []


def test_the_pairing_rides_the_span_check_rather_than_beside_it():
    """Extended, not duplicated: one pass over `state_spans` answers both questions"""
    assert invariants.check_spans_reach_the_status in invariants.INVARIANTS
    assert not hasattr(invariants, "check_record_pairs_spans")


# -- the alarm packet reads the spans `state_durations` reads


def test_the_alarm_packet_carries_the_status_history_and_the_last_verdict(store):
    """Neo cleared wo-12b4e416 as 'parked in validating' — the packet never said the round passed days ago"""
    wo = store.create_work_order("ship the sweep")
    store.set_status(wo["id"], "running")
    store.set_status(wo["id"], "validating")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="f")
    store.close_validation_round(rnd["id"], "passed")
    for i in range(300):
        store.add_event(wo["id"], "note", {"i": i})

    packet = supervisor.build_evidence(
        store, {"kind": "work_order", "row": store.get_work_order(wo["id"])}, None,
        CFG, catalog.InspectConfig())

    assert "# Where it has been" in packet
    section = packet.split("# Where it has been")[1].split("\n\n")[0]
    assert "validating" in section and "running" in section
    assert "round 1: passed" in section
    # Ahead of the session read, so a capped packet drops the transcript first
    assert packet.index("# Where it has been") < packet.index("# The session")


def test_the_feature_packet_carries_its_status_history(store):
    fo = store.create_feature_order("a feature", description="d")
    store.set_feature_status(fo["id"], "planning")

    packet = supervisor.build_evidence(
        store, {"kind": "feature_order", "row": store.get_feature_order(fo["id"])},
        None, CFG, catalog.InspectConfig())

    section = packet.split("# Where it has been")[1].split("\n\n")[0]
    assert "planning" in section
