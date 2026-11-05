"""A pending assumption lapses when its order ends, and lapsing is no verdict (io-d5e62d4d)"""

from __future__ import annotations

import pytest

from jarvis import autoreview, invariants, ops
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.invariants import check_project, true_blockers
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore
from jarvis.testing import a_report


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


@pytest.fixture()
def held_analyst(user_decides_findings, jarvis_home, fake_claude, catalog_file, project,
                 improvement_order):
    """wo-46f75767's shape: analyst delivered, one row pending, its question escalated"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    io_id = improvement_order["id"]
    analyst = store.create_work_order("analyse it", kind="analyst", status="running",
                                      parent_id=io_id, origin="os")
    store.update_feature_order(io_id, plan_wo_id=analyst["id"], status="planning")
    ops.submit_findings(io_id, a_report())
    aid = store.add_assumption(analyst["id"], "the same recommendation again")
    neo = NeoStore()
    qid = neo.ask("proj_a", analyst["id"], "ASSUMPTION REVIEW", kind="assumption")["id"]
    neo.mark(qid, "escalated", reason="yours")
    neo.close()
    store.link_assumption_question(aid, qid)
    store.set_status(analyst["id"], "needs_review")
    store.flag_attention(analyst["id"], "assumptions pending review")
    yield {"store": store, "io": io_id, "wo_id": analyst["id"], "aid": aid, "qid": qid}
    store.close()


def _question(qid: int) -> dict:
    neo = NeoStore()
    try:
        return neo.get(qid)
    finally:
        neo.close()


def _lapse_violations(store: ProjectStore, repair: bool = True) -> list:
    return [v for v in check_project(store, repair=repair)
            if v.invariant == "INV-ASSUMPTION-LAPSED"]


def test_accepting_a_finding_lapses_the_analysts_copy_and_lands_it(held_analyst):
    store, wo_id = held_analyst["store"], held_analyst["wo_id"]
    # Nothing decided yet: the row is still a live ask
    assert _lapse_violations(store) == []
    assert store.get_assumption(held_analyst["aid"])["status"] == "pending"

    ops.review_findings(held_analyst["io"], accept=["first-turn-reads"], feedback="yes")

    assert _lapse_violations(store)
    row = store.get_assumption(held_analyst["aid"])
    assert row["status"] == "lapsed" and not row["decided_by"]
    assert "first-turn-reads" in row["decided_reason"]
    assert held_analyst["io"] in row["decided_reason"]
    question = _question(held_analyst["qid"])
    assert (question["status"], question["answer"]) == ("lapsed", None)
    assert "first-turn-reads" in question["lapsed_reason"]
    fresh = store.get_work_order(wo_id)
    assert fresh["status"] == "completed" and not fresh["needs_attention"]
    assert true_blockers(store, fresh) == []
    assert _lapse_violations(store) == []


def test_a_settled_parent_lapses_the_row_it_can_no_longer_decide(held_analyst):
    store = held_analyst["store"]
    store.set_feature_status(held_analyst["io"], "cancelled")

    check_project(store, repair=True)

    row = store.get_assumption(held_analyst["aid"])
    assert row["status"] == "lapsed"
    assert "cancelled" in row["decided_reason"]
    assert _question(held_analyst["qid"])["status"] == "lapsed"
    assert store.get_work_order(held_analyst["wo_id"])["status"] == "completed"


def test_a_failed_parent_can_resume_so_its_childs_row_stays_pending(held_analyst):
    store = held_analyst["store"]
    store.set_feature_status(held_analyst["io"], "failed")

    assert _lapse_violations(store) == []
    assert store.get_assumption(held_analyst["aid"])["status"] == "pending"


def test_the_lapse_teaches_neo_nothing(held_analyst):
    store, wo_id = held_analyst["store"], held_analyst["wo_id"]
    ops.review_findings(held_analyst["io"], accept=["first-turn-reads"], feedback="yes")

    _lapse_violations(store)

    neo = NeoStore()
    try:
        taught = [r for r in neo.learnings("proj_a", limit=500, include_retired=True)
                  if r.get("wo_id") == wo_id and r.get("origin_kind") == "assumption"]
    finally:
        neo.close()
    assert taught == []


def test_another_blocker_keeps_the_row_pending(held_analyst, monkeypatch):
    store = held_analyst["store"]
    ops.review_findings(held_analyst["io"], accept=["first-turn-reads"], feedback="yes")
    real = invariants.true_blockers
    monkeypatch.setattr(invariants, "true_blockers",
                        lambda s, w, now=None: [*real(s, w, now), "gate approval needed"])

    assert _lapse_violations(store) == []
    assert store.get_assumption(held_analyst["aid"])["status"] == "pending"
    assert store.get_work_order(held_analyst["wo_id"])["status"] == "needs_review"


def test_doctor_reports_without_touching_it(held_analyst):
    store = held_analyst["store"]
    ops.review_findings(held_analyst["io"], accept=["first-turn-reads"], feedback="yes")

    assert _lapse_violations(store, repair=False)
    assert store.get_assumption(held_analyst["aid"])["status"] == "pending"
    assert _question(held_analyst["qid"])["status"] == "escalated"
