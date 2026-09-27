"""Time in each state: the spans the two chokepoints write, and the one reader over them.

Built against a real `ProjectStore` in the style of `tests/test_active_time.py`'s
`Record` — the store stamps `db.now()` and there is no back-dating API, so a fixture that
needs a transition to have happened thirteen hours ago says so afterwards with SQL.
`now` is passed explicitly everywhere; nothing sleeps.

Spec: docs/superpowers/specs/2026-09-27-time-in-each-state.md.
"""

from __future__ import annotations

import pytest

from jarvis import landing, ops, timeline
from jarvis.project_store import (
    FO_STATUSES,
    WO_STATUSES,
    ProjectStore,
    feature_status_label,
)

MINUTE = 60.0
HOUR = 3600.0
#: An arbitrary epoch the fixtures hang off. Big enough that nothing reads as "unset".
T0 = 1_700_000_000.0
NOW = T0 + 24 * HOUR


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(tmp_path / "proj")
    yield s
    s.close()


def backdate(store: ProjectStore, order_id: str, *stamps: float) -> None:
    """Put an order's spans, and its creation, on the clock — oldest first."""
    ids = [r["id"] for r in store.conn.execute(
        "SELECT id FROM wo_state_spans WHERE order_id=? ORDER BY id", (order_id,))]
    assert len(ids) == len(stamps), (ids, stamps)
    for span_id, ts in zip(ids, stamps):
        store.conn.execute("UPDATE wo_state_spans SET ts=? WHERE id=?", (ts, span_id))
    store.conn.execute("UPDATE work_orders SET created_at=? WHERE id=?",
                       (stamps[0], order_id))
    store.conn.execute("UPDATE feature_orders SET created_at=? WHERE id=?",
                       (stamps[0], order_id))
    store.conn.commit()


def event_at(store: ProjectStore, wo_id: str, kind: str, ts: float) -> None:
    store.add_event(wo_id, kind)
    store.conn.execute(
        "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events WHERE wo_id=?)",
        (ts, wo_id))
    store.conn.commit()


def turn_at(store: ProjectStore, wo_id: str, started: float, ended: float) -> None:
    row = store.create_turn(wo_id, kind="message", prompt="go")
    store.finish_turn(row["id"], state="done")
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (started, ended, row["id"]))
    store.conn.commit()


def totals_by_status(payload: dict) -> dict[str, dict]:
    return {t["status"]: t for t in payload["totals"]}


# 1 -------------------------------------------------------------------------


def test_spans_sum_to_lifetime(store):
    wo = store.create_work_order("t")
    for status in ("running", "needs_review", "waiting_pr_merge"):
        store.set_status(wo["id"], status)
    backdate(store, wo["id"], T0, T0 + HOUR, T0 + 3 * HOUR, T0 + 4 * HOUR)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert round(sum(s["seconds"] for s in payload["spans"]), 2) == \
        payload["lifetime_seconds"]
    assert payload["lifetime_seconds"] == NOW - T0
    assert [s["status"] for s in payload["spans"]] == [
        "pending", "running", "needs_review", "waiting_pr_merge"]
    assert payload["spans"][-1]["open"] is True


def test_terminal_lifetime_stops_at_the_ending(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")
    store.set_status(wo["id"], "completed")
    backdate(store, wo["id"], T0, T0 + HOUR, T0 + 5 * HOUR)

    reading = ops.state_durations(store, wo_id=wo["id"], now=NOW)
    payload = reading.as_dict(NOW)
    assert payload["lifetime_seconds"] == 5 * HOUR
    assert round(sum(s["seconds"] for s in payload["spans"]), 2) == 5 * HOUR
    later = reading.as_dict(NOW + 10 * HOUR)
    assert later["lifetime_seconds"] == 5 * HOUR


# 2 -------------------------------------------------------------------------


def test_re_entry_accumulates(store):
    wo = store.create_work_order("t")
    for status in ("needs_review", "running", "needs_review"):
        store.set_status(wo["id"], status)
    backdate(store, wo["id"], T0, T0 + HOUR, T0 + 2 * HOUR, T0 + 3 * HOUR)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    entry = totals_by_status(payload)["needs_review"]
    assert entry["entries"] == 2
    assert entry["seconds"] == HOUR + (NOW - (T0 + 3 * HOUR))


# 3 -------------------------------------------------------------------------


def test_current_status_age_and_observer_events(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")
    store.set_status(wo["id"], "waiting_pr_merge")
    backdate(store, wo["id"], NOW - 20 * HOUR, NOW - 14 * HOUR, NOW - 13 * HOUR)
    turn_at(store, wo["id"], NOW - 14 * HOUR, NOW - 13 * HOUR)
    event_at(store, wo["id"], "turn_ended", NOW - 13 * HOUR)
    event_at(store, wo["id"], "health_finding", NOW - MINUTE)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert payload["current_status"] == "waiting_pr_merge"
    assert payload["current_status_age"] == 13 * HOUR
    assert payload["last_activity_age"] == 13 * HOUR

    event_at(store, wo["id"], "landing_seen", NOW - 10 * MINUTE)
    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert payload["last_activity_age"] == 10 * MINUTE
    assert payload["last_activity_kind"] == "landing_seen"


# 4 -------------------------------------------------------------------------


def test_settled_order_has_no_current_span(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "completed")
    backdate(store, wo["id"], T0, T0 + HOUR)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert payload["current_status"] == ""
    assert payload["current_status_since"] is None
    assert payload["current_status_age"] is None
    assert payload["current_status_age_human"] is None
    assert not [s for s in payload["spans"] if s["open"]]


# 5 -------------------------------------------------------------------------


def test_brand_new_order(store):
    wo = store.create_work_order("t")
    backdate(store, wo["id"], NOW - MINUTE)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert [s["status"] for s in payload["spans"]] == ["pending"]
    assert totals_by_status(payload)["pending"]["entries"] == 1
    assert payload["last_activity_ts"] is None
    assert payload["last_activity_age"] is None
    assert ops.NO_ACTIVITY_NOTE in payload["notes"]


# 6 -------------------------------------------------------------------------


def test_no_change_guard(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "needs_review")
    store.set_status(wo["id"], "needs_review", pr_url="https://x/1")

    spans = store.state_spans(wo["id"])
    assert [s["to_status"] for s in spans] == ["pending", "needs_review"]
    assert len(store.events_of_kind(wo["id"], "status")) == 1
    assert store.get_work_order(wo["id"])["pr_url"] == "https://x/1"
    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert totals_by_status(payload)["needs_review"]["entries"] == 1


def test_park_unlanded_twice_writes_one_span(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")
    work = landing.Authored(branch="wo/t", base="main", commits=1)
    ops.park_unlanded(store, store.get_work_order(wo["id"]), work)
    ops.park_unlanded(store, store.get_work_order(wo["id"]), work)

    assert [s["to_status"] for s in store.state_spans(wo["id"])] == [
        "pending", "running", "needs_review"]
    assert len(store.events_of_kind(wo["id"], "status")) == 2
    assert len(store.events_of_kind(wo["id"], "work_unlanded")) == 1


# 7 -------------------------------------------------------------------------


def test_feature_order_spans_are_exact(store):
    fo = store.create_feature_order("f")
    for status in ("planning", "executing", "completed"):
        store.set_feature_status(fo["id"], status)
    backdate(store, fo["id"], T0, T0 + HOUR, T0 + 2 * HOUR, T0 + 3 * HOUR)

    reading = ops.state_durations(store, fo_id=fo["id"], now=NOW)
    payload = reading.as_dict(NOW)
    assert reading.order_kind == "fo"
    assert reading.approximate is False
    assert payload["lifetime_seconds"] == 3 * HOUR
    assert round(sum(s["seconds"] for s in payload["spans"]), 2) == 3 * HOUR
    assert payload["current_status"] == ""


# 8 -------------------------------------------------------------------------


def test_feature_activity_is_the_familys(store):
    fo = store.create_feature_order("f")
    manager = store.create_work_order("m", parent_id=fo["id"], kind="manager")
    child = store.create_work_order("c", parent_id=fo["id"])
    store.set_feature_status(fo["id"], "executing")
    turn_at(store, child["id"], NOW - 20 * MINUTE, NOW - 5 * MINUTE)
    assert manager["id"]  # idle: no turn, no message

    payload = ops.state_durations(store, fo_id=fo["id"], now=NOW).as_dict(NOW)
    assert payload["last_activity_age"] == 5 * MINUTE


# 9 -------------------------------------------------------------------------


def test_every_status_has_a_label():
    for status in WO_STATUSES:
        label = timeline.STATUS_LABEL.get(status, "")
        assert label and label != status, status
    for kind in ("feature", "improvement"):
        for status in FO_STATUSES:
            assert feature_status_label(kind, status), (kind, status)


# the reader's own contract ------------------------------------------------


def test_one_subject_only(store):
    wo = store.create_work_order("t")
    with pytest.raises(ops.OpsError):
        ops.state_durations(store, now=NOW)
    with pytest.raises(ops.OpsError):
        ops.state_durations(store, wo_id=wo["id"], fo_id="fo-1", now=NOW)
