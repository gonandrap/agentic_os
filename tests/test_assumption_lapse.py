"""A pending assumption lapses when its order ends, and lapsing is no verdict (io-d5e62d4d)"""

from __future__ import annotations

import pytest

from jarvis import autoreview, ops
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.invariants import check_project, true_blockers
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore


def _seed_pending_on_ended_order(store: ProjectStore, status: str) -> dict:
    """The shape on disk before this rule: an ended order still holding a pending row"""
    wo = store.create_work_order("ended before anyone ruled")
    store.set_status(wo["id"], status)
    aid = store.add_assumption(wo["id"], "a call nobody ruled on")
    return {"wo": store.get_work_order(wo["id"]), "aid": aid}


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_reaching_an_end_lapses_the_row_with_no_verdict(project, status):
    store = ProjectStore(project)
    wo = store.create_work_order("ends")
    aid = store.add_assumption(wo["id"], "a call")

    store.set_status(wo["id"], status)

    row = store.get_assumption(aid)
    assert row["status"] == "lapsed"
    assert not row["decided_by"]
    assert status in row["decided_reason"]
    assert store.pending_assumptions(wo["id"]) == []
    assert store.events_of_kind(wo["id"], "assumptions_lapsed")


def test_a_failed_orders_row_stays_pending_because_retry_can_resume_it(project):
    store = ProjectStore(project)
    wo = store.create_work_order("dies")
    aid = store.add_assumption(wo["id"], "a call")

    store.set_status(wo["id"], "failed")
    check_project(store, repair=True)

    assert store.get_assumption(aid)["status"] == "pending"


def test_a_ruled_row_is_not_touched(project):
    store = ProjectStore(project)
    wo = store.create_work_order("ruled")
    aid = store.add_assumption(wo["id"], "a call")
    store.review_assumption(aid, "accepted", decided_by="user")

    store.set_status(wo["id"], "completed")

    assert store.get_assumption(aid)["status"] == "accepted"
    assert store.events_of_kind(wo["id"], "assumptions_lapsed") == []


def test_closing_a_manager_with_pending_rows_leaves_no_flag(project, monkeypatch):
    store = ProjectStore(project)
    fo = store.create_feature_order("a feature")
    manager = store.create_work_order("manage", kind="manager", parent_id=fo["id"])
    store.set_status(manager["id"], "running")
    aid = store.add_assumption(manager["id"], "a triage call")
    monkeypatch.setattr(store, "manager_work_order", lambda _fo: store.get_work_order(manager["id"]))

    Daemon._close_feature_manager(object.__new__(Daemon), store, fo["id"])
    check_project(store, repair=True)

    fresh = store.get_work_order(manager["id"])
    assert fresh["status"] == "completed"
    assert not fresh["needs_attention"]
    assert store.get_assumption(aid)["status"] == "lapsed"
    assert true_blockers(store, fresh) == []


def test_the_invariant_repairs_a_seeded_row_and_its_flag(project):
    store = ProjectStore(project)
    seeded = _seed_pending_on_ended_order(store, "completed")
    wo_id = seeded["wo"]["id"]
    store.flag_attention(wo_id, "1 assumption pending your review")

    found = [v.invariant for v in check_project(store, repair=True)]

    assert found == ["INV-ASSUMPTION-LAPSED", "INV-ATTENTION-PHANTOM"]
    assert store.get_assumption(seeded["aid"])["status"] == "lapsed"
    assert not store.get_work_order(wo_id)["needs_attention"]
    # Settled: the next sweep has nothing to say
    assert check_project(store, repair=True) == []


def test_investigator_rows_lapse_too(project):
    store = ProjectStore(project)
    wo = store.create_work_order("diagnose", kind="investigator")
    aid = store.add_assumption(wo["id"], "a call on record")

    store.set_status(wo["id"], "completed")

    assert store.get_assumption(aid)["status"] == "lapsed"
    assert store.all_assumptions(wo["id"])[0]["content"] == "a call on record"


def test_show_renders_it_and_stops_offering_a_ruling(project):
    store = ProjectStore(project)
    wo = store.create_work_order("ended")
    store.add_assumption(wo["id"], "a call")
    store.set_status(wo["id"], "completed")

    line = ops.assumption_line(store.all_assumptions(wo["id"])[0])

    assert line.startswith("#1 lapsed by nobody")
    assert "reached completed" in line
    assert ops.review_state(store, store.get_work_order(wo["id"])) is None


def test_no_ruling_is_offered_on_a_row_the_sweep_has_not_reached(project):
    store = ProjectStore(project)
    seeded = _seed_pending_on_ended_order(store, "cancelled")

    assert ops.review_state(store, seeded["wo"]) is None


def test_review_of_an_ended_order_lapses_and_teaches_nothing(project, jarvis_home):
    central = CentralStore()
    central.upsert_project("proj_a", str(project))
    central.close()
    store = ProjectStore(project)
    seeded = _seed_pending_on_ended_order(store, "completed")
    wo_id = seeded["wo"]["id"]

    out = ops.review_work_order(wo_id, accept=True, feedback="looks fine",
                                false_alarm=True)

    assert out["reviewed"] == 0
    assert "learning_id" not in out and "false_alarms" not in out
    assert store.get_assumption(seeded["aid"])["status"] == "lapsed"
    neo = NeoStore()
    try:
        assert not [r for r in neo.learnings("proj_a", limit=500, include_retired=True)
                    if r.get("wo_id") == wo_id]
    finally:
        neo.close()


def test_stats_keep_a_lapsed_row_out_of_every_bucket_and_rate():
    facts = {"rows": [{"id": 1, "status": "lapsed", "decided_by": "", "ts": 0.0,
                       "neo_question_id": 7},
                      {"id": 2, "status": "accepted", "decided_by": "neo", "ts": 0.0}],
             "asked": {1}, "last_hold": {}, "high_stakes": set(), "false_alarms": set(),
             "dropped": {"total": 0, "re_asked": 0}}

    out = ops.assumption_row_outcomes([facts])

    assert out["rows"] == 2
    assert out["lapsed"] == 1
    assert out["settled"] == out["neo_decided"] == 1
    assert out["neo_escalated"] == out["never_reached_neo"] == 0
    assert out["unsettled"]["count"] == 0


def test_a_lapsed_sibling_is_not_shown_to_neo_as_the_users_ruling():
    row = {"n": 1, "status": "lapsed", "decided_by": "",
           "decided_reason": "its order reached completed before anyone ruled"}

    assert autoreview._user_ruling_item(row) == ""
