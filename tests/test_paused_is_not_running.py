"""A running order whose turn the usage limit refused reads as paused, as fields (issue #247)"""

from __future__ import annotations

import pytest

from jarvis import db, invariants, ops, supervisor, worker_session
from jarvis.project_store import ProjectStore


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def _refused(store: ProjectStore) -> dict:
    """A `running` order whose last turn the usage limit refused, window still shut"""
    wo = store.create_work_order("ship the thing", origin="jarvis")
    turn = store.create_turn(wo["id"], kind="message", prompt="carry on")
    store.finish_turn(turn["id"], "failed",
                      error=f"Claude AI usage limit reached|{int(db.now() + 3600)}")
    store.set_status(wo["id"], "running")
    return store.get_work_order(wo["id"])


def _working(store: ProjectStore) -> dict:
    wo = store.create_work_order("other thing", origin="jarvis")
    store.create_turn(wo["id"], kind="message", prompt="go")
    store.set_status(wo["id"], "running")
    return store.get_work_order(wo["id"])


def test_the_pause_is_a_field_with_its_moment(store):
    wo = _refused(store)

    state = invariants.pause_state(store, wo)

    assert state["reason"] == worker_session.PAUSE_USAGE_LIMIT
    assert state["until"] == worker_session.turn_pause(store, wo["id"]).retry_at
    assert "usage limit" in state["note"]


def test_a_working_order_has_no_pause(store):
    assert invariants.pause_state(store, _working(store)) is None


def test_the_status_counts_move_a_paused_order_out_of_running(store):
    wos = [_refused(store), _working(store)]
    summary = store.summary()
    assert summary["by_status"] == {"running": 2}

    ops._count_paused(summary, wos, {w["id"]: invariants.pause_state(store, w) for w in wos})

    assert summary["by_status"] == {"running": 1, "paused": 1}


def test_the_health_sweep_is_told_the_quiet_has_an_owner(store):
    wo = _refused(store)

    packet = supervisor.build_evidence(store, {"kind": "work_order", "row": wo}, None)

    assert "paused (usage_limit)" in packet
    assert "this is not a stall" in packet
