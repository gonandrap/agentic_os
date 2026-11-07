"""`jarvis wo overrule`: the user reverses Neo's ruling on one assumption (docs/superpowers/specs/2026-11-07-neo-owns-os-origin-assumption-review.md §5)"""

from __future__ import annotations

import pytest

from jarvis import autoreview, db, ops, timeline
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

pytestmark = pytest.mark.usefixtures("neo_projects")

FEEDBACK = "CSV is wrong here; the consumer reads JSON only"


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def neo_accepted(project, status: str) -> tuple[dict, int]:
    """An order whose assumption #1 Neo accepted, moved to `status`; #2 stays pending"""
    wo = ops.create_work_order("proj_a", "build the exporter")
    store = ProjectStore(project)
    neo = NeoStore()
    try:
        store.add_assumption(wo["id"], "export defaults to CSV\nJSON behind a flag")
        store.add_assumption(wo["id"], "keeps the order from settling")
        q = neo.ask("proj_a", wo["id"], "rule on assumption #1", kind="assumption")
        neo.record_answer(q["id"], "approve")
        a = store.all_assumptions(wo["id"])[0]
        ops.accept_assumption(store, project, wo, a, reason="routine format choice",
                              model="m", question_id=int(q["id"]),
                              cfg=ops.validation_config("proj_a"))
        store.set_status(wo["id"], status)
        return store.get_work_order(wo["id"]), int(q["id"])
    finally:
        store.close()
        neo.close()


def overruled_event(project, wo_id):
    store = ProjectStore(project)
    try:
        (e,) = store.events_of_kind(wo_id, "assumption_overruled")
        return db.from_json(e["payload"], {})
    finally:
        store.close()


def test_open_order_worker_told_row_rejected_neo_taught(started, project):
    wo, qid = neo_accepted(project, "needs_review")
    out = ops.overrule_assumption(wo["id"], 1, FEEDBACK)

    assert out["question_id"] == qid and out["learning_id"]
    assert "delivered" in out and "fix_wo_id" not in out
    store = ProjectStore(project)
    try:
        a = store.all_assumptions(wo["id"])[0]
        assert (a["status"], a["decided_by"], a["decided_reason"]) == (
            "rejected", "user", FEEDBACK)
        contents = [m["content"] for m in store.list_messages(wo["id"])]
        assert any(c.startswith(f"The user overruled Neo on assumption #1: {FEEDBACK}")
                   for c in contents)
    finally:
        store.close()
    p = overruled_event(project, wo["id"])
    assert p["prior_decided_by"] == autoreview.DECIDER
    assert (p["n"], p["neo_question_id"], p["feedback"], p["fix_wo_id"]) == (
        1, qid, FEEDBACK, None)
    assert p["learning_id"] == out["learning_id"]
    neo = NeoStore()
    try:
        assert neo.get(qid)["review_status"] == "corrected"
        (row,) = [r for r in neo.learnings("proj_a", limit=20) if FEEDBACK in r["content"]]
        assert row["origin_kind"] == "assumption"
    finally:
        neo.close()
    label, _ = timeline._describe("assumption_overruled", p)
    assert "overruled Neo on assumption #1" in label


def test_merged_order_gets_a_fix_order_the_user_asked_for(started, project):
    wo, qid = neo_accepted(project, "completed")
    out = ops.overrule_assumption(wo["id"], 1, FEEDBACK)

    assert "delivered" not in out
    _, _, fix = ops.find_work_order(out["fix_wo_id"])
    # No parent, no trigger: `ops.requestor` reads it as the user's (spec §2)
    assert not fix.get("parent_id") and not fix.get("trigger_wo_id")
    assert fix["origin"] == "jarvis"
    assert fix["title"] == "Overrule: export defaults to CSV"
    assert db.from_json(fix["metadata"], {})["overrules"] == {
        "wo_id": wo["id"], "n": 1, "question_id": qid}
    assert FEEDBACK in fix["description"] and "routine format choice" in fix["description"]
    assert overruled_event(project, wo["id"])["fix_wo_id"] == out["fix_wo_id"]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_ended_order_records_only_and_says_so(started, project, status):
    wo, _ = neo_accepted(project, status)
    out = ops.overrule_assumption(wo["id"], 1, FEEDBACK)

    assert "delivered" not in out and "fix_wo_id" not in out
    assert status in out["note"] and "no worker told" in out["note"]
    assert out["learning_id"]
    assert overruled_event(project, wo["id"])["fix_wo_id"] is None
    store = ProjectStore(project)
    try:
        assert store.all_assumptions(wo["id"])[0]["status"] == "rejected"
        assert store.list_messages(wo["id"]) == []
    finally:
        store.close()


def test_a_user_decided_row_is_refused_and_names_wo_review(started, project):
    wo, _ = neo_accepted(project, "needs_review")
    with pytest.raises(ops.OpsError, match="jarvis wo review"):
        ops.overrule_assumption(wo["id"], 2, FEEDBACK)     # still pending
    ops.overrule_assumption(wo["id"], 1, FEEDBACK)
    with pytest.raises(ops.OpsError, match="nothing of Neo's to overrule"):
        ops.overrule_assumption(wo["id"], 1, FEEDBACK)     # now the user's


def test_no_such_assumption_and_empty_feedback_are_refused(started, project):
    wo, _ = neo_accepted(project, "needs_review")
    with pytest.raises(ops.OpsError, match="no assumption #9"):
        ops.overrule_assumption(wo["id"], 9, FEEDBACK)
    with pytest.raises(ops.OpsError, match="needs feedback"):
        ops.overrule_assumption(wo["id"], 1, "  ")


def test_a_worker_session_may_not_overrule(started, project, monkeypatch):
    wo, _ = neo_accepted(project, "needs_review")
    monkeypatch.setenv("JARVIS_WO_ID", "wo-deadbeef")
    with pytest.raises(ops.OpsError, match="worker session"):
        ops.overrule_assumption(wo["id"], 1, FEEDBACK)
    store = ProjectStore(project)
    try:
        assert store.all_assumptions(wo["id"])[0]["decided_by"] == autoreview.DECIDER
    finally:
        store.close()
