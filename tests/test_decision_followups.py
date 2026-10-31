"""Follow-ups filed from a decision report's proposals (docs/superpowers/specs/2026-10-31-decision-orders.md §6)"""

from __future__ import annotations

import json

import pytest

from jarvis import cli, ops, timeline
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore, db, decision_report_sha

REPORT = {
    "summary": "B is 30% cheaper",
    "findings": [{"key": "f1", "title": "cost", "detail": "d", "evidence": ["x"]}],
    "options": [],
    "recommendation": {"option": "b", "why": "cheaper"},
    "proposed_orders": [
        {"key": "p1", "as": "wo", "project": "", "title": "migrate", "description": "move to B"},
        {"key": "p2", "as": "backlog", "project": "", "title": "later", "description": "tidy"},
    ],
}


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def _decision(store, status="awaiting_decision", report=REPORT):
    do = ops.create_decision_order("proj_a", "which cache", "compare the two caches")
    wo = store.create_work_order("research it", kind="researcher", parent_id=do["id"])
    store.update_feature_order(do["id"], plan_wo_id=wo["id"], plan=db.to_json(report))
    store.set_feature_status(do["id"], status)
    return store.get_feature_order(do["id"])


def test_a_proposal_is_filed_as_a_linked_order_and_the_decision_stays_open(started, store):
    do = _decision(store)
    sha = decision_report_sha(do["plan"])
    out = ops.file_decision_followups(do["id"], ["p1"])
    [f] = out["filed"]
    assert f["key"] == "p1" and f["as"] == "wo" and not out["errors"]
    wo = store.get_work_order(f["id"])
    assert wo["parent_id"] is None
    assert wo["description"].splitlines()[0] == (
        f"Filed from decision order {do['id']} — run `jarvis do show {do['id']}` "
        f"for the data behind it.")
    assert ops.decision_origin(wo) == do["id"]
    after = store.get_feature_order(do["id"])
    assert after["status"] == "awaiting_decision"
    assert decision_report_sha(after["plan"]) == sha  # §5 never sees a revision
    assert json.loads(after["plan"])["proposed_orders"][0]["filed"][0]["id"] == f["id"]
    shown = ops.show_decision_order(do["id"])
    assert [(x["key"], x["id"]) for x in shown["followups"]] == [("p1", f["id"])]
    [ev] = [e for e in store.list_events(do["plan_wo_id"])
            if e["kind"] == ops.DECISION_FOLLOWUP_EVENT]
    payload = db.from_json(ev["payload"], {})
    assert payload == {"do_id": do["id"], "key": "p1", "order_id": f["id"]}
    assert timeline._describe(ev["kind"], payload)[0].startswith(f"Filed {f['id']}")


def test_filing_is_refused_before_a_report_is_delivered(started, store):
    do = _decision(store, status="planning")
    with pytest.raises(ops.OpsError, match="delivered report"):
        ops.file_decision_followups(do["id"], ["p1"])


def test_a_filed_key_is_refused_unless_again_and_all_skips_it(started, store):
    do = _decision(store)
    ops.file_decision_followups(do["id"], ["p1"])
    with pytest.raises(ops.OpsError, match="--again"):
        ops.file_decision_followups(do["id"], ["p1"])
    out = ops.file_decision_followups(do["id"], all_=True)
    assert [f["key"] for f in out["filed"]] == ["p2"] and out["skipped"] == ["p1"]
    again = ops.file_decision_followups(do["id"], ["p1"], again=True)
    assert len(again["filed"]) == 1
    marks = json.loads(store.get_feature_order(do["id"])["plan"])["proposed_orders"][0]["filed"]
    assert len(marks) == 2


def test_as_overrides_and_filing_after_the_user_decided_keeps_it_decided(started, store):
    do = _decision(store)
    ops.complete_decision_order(do["id"], note="B")
    out = ops.file_decision_followups(do["id"], ["p1", "p2"], as_="backlog")
    assert {f["as"] for f in out["filed"]} == {"backlog"}
    central = CentralStore()
    try:
        item = central.get_backlog(out["filed"][0]["id"])
    finally:
        central.close()
    assert item["origin_fo_id"] == do["id"]
    assert store.get_feature_order(do["id"])["status"] == "completed"


def test_a_failed_filing_is_reported_and_the_rest_still_file(started, store):
    report = {**REPORT, "proposed_orders": [
        {**REPORT["proposed_orders"][0], "project": "nowhere"}, REPORT["proposed_orders"][1]]}
    do = _decision(store, report=report)
    out = ops.file_decision_followups(do["id"], all_=True)
    assert [e["key"] for e in out["errors"]] == ["p1"]
    assert [f["key"] for f in out["filed"]] == ["p2"]
    stored = json.loads(store.get_feature_order(do["id"])["plan"])["proposed_orders"]
    assert "filed" not in stored[0]
    assert store.get_feature_order(do["id"])["status"] == "awaiting_decision"


def test_the_cli_wrapper_files_by_key(started, store, capsys):
    do = _decision(store)
    assert cli.main(["do", "follow-up", do["id"], "p1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["filed"][0]["key"] == "p1"
