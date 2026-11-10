"""Experiments under a decision order: start, go, results gate, delivery (docs/superpowers/specs/2026-11-10-decision-order-experiments.md §4)"""

from __future__ import annotations

import pytest

from jarvis import cli, decision_report, invariants, ops
from jarvis.project_store import db
from tests.test_decision_orders import (REPORT, _daemon, _decision, _decision_notes,
                                        _deliver, started, store)  # noqa: F401

RESULTS = {**REPORT, "summary": "B is 41% cheaper, measured over 24 runs"}
FORECAST = {"runs": 24, "model": "m", "per_run": {"input_tokens": 1, "output_tokens": 1}}


def _awaiting(store, catalog_file, *, worktree: str | None = None, make_dir=True):
    do = _decision(store, researcher="completed", report=True)
    if worktree:
        store.update_work_order(do["plan_wo_id"], worktree=worktree)
        if make_dir:
            (store.project_path / ".claude" / "worktrees" / worktree).mkdir(parents=True)
    _deliver(catalog_file, store)
    return store.get_feature_order(do["id"])


def _completed(store, catalog_file):
    do = _awaiting(store, catalog_file)
    ops.complete_decision_order(do["id"], note="B")
    return store.get_feature_order(do["id"])


def _forecast_and_attach(store, do_id, *, attach=True):
    n = store.open_experiment(do_id)["n"]
    store.update_experiment(do_id, n, forecast=FORECAST)
    ops.approve_decision_experiment(do_id)
    if attach:
        store.add_experiment_attachment(do_id, n, {"name": "raw.json", "size": 2,
                                                   "sha256": "x", "path": "p", "at": 0.0})
    return n


def _run_to_delivery(store, catalog_file, do):
    out = ops.start_decision_experiment(do["id"], "p1")
    row = store.get_feature_order(do["id"])
    assert row["status"] == "planning" and not row["needs_attention"]
    assert row["plan_wo_id"] == out["researcher_id"]
    _forecast_and_attach(store, do["id"])
    ops.submit_decision_report(do["id"], RESULTS)
    assert store.experiments_of(do["id"])[0]["reported_at"] is not None
    store.set_status(out["researcher_id"], "completed")
    before = len(_decision_notes(store, do["id"]))
    for _ in range(3):
        _deliver(catalog_file, store)
    row = store.get_feature_order(do["id"])
    assert row["status"] == "awaiting_decision" and row["needs_attention"]
    notes = _decision_notes(store, do["id"])
    assert len(notes) == before + 1
    assert notes[-1]["title"] == f"{do['id']} report revised: which cache"
    return out


# -- start, go, results, delivery


def test_an_experiment_from_a_decided_order_comes_back_as_a_revision(started, store,
                                                                    catalog_file):
    do = _completed(store, catalog_file)
    old = do["plan_wo_id"]
    out = _run_to_delivery(store, catalog_file, do)
    assert not out["revived"] and out["researcher_id"] != old, "the old one was cancelled"
    entry = store.experiments_of(do["id"])[0]
    assert (entry["key"], entry["title"], entry["reopened_from"]) == ("p1", "migrate",
                                                                     "completed")
    child = store.get_work_order(out["researcher_id"])
    assert child["kind"] == "researcher" and child["parent_id"] == do["id"]
    assert child["description"].startswith("compare the two caches")
    assert "jarvis do forecast" in child["description"]
    started_ev = store.events_of_kind(out["researcher_id"], "decision_experiment_started")
    assert db.from_json(started_ev[0]["payload"]) == {
        "do_id": do["id"], "n": 1, "key": "p1", "reopened_from": "completed"}
    assert store.events_of_kind(out["researcher_id"], "decision_experiment_go")


def test_an_experiment_from_an_awaiting_order_revives_its_researcher(started, store,
                                                                    catalog_file):
    do = _awaiting(store, catalog_file, worktree="wo-research")
    out = _run_to_delivery(store, catalog_file, do)
    assert out["revived"] and out["researcher_id"] == do["plan_wo_id"]
    queued = [m["content"] for m in store.list_messages(do["plan_wo_id"])]
    assert any("experiment 1 of" in m for m in queued)
    assert any(m.startswith("go: run experiment 1") for m in queued)


@pytest.mark.parametrize("case", ["reclaimed", "cancelled"])
def test_a_reclaimed_or_cancelled_researcher_gets_a_fresh_one(started, store, catalog_file,
                                                             case):
    do = _awaiting(store, catalog_file, worktree="wo-gone", make_dir=False)
    if case == "cancelled":
        store.set_status(do["plan_wo_id"], "cancelled")
    out = ops.start_decision_experiment(do["id"], brief="time both caches\nmore detail")
    assert not out["revived"]
    assert store.get_feature_order(do["id"])["plan_wo_id"] == out["researcher_id"]
    entry = store.experiments_of(do["id"])[0]
    assert (entry["key"], entry["title"]) == ("", "time both caches")
    assert entry["researcher_id"] == out["researcher_id"]


# -- refusals, each naming the command that fixes it


def test_an_agent_session_cannot_start_or_go(started, store, catalog_file, monkeypatch):
    do = _awaiting(store, catalog_file)
    monkeypatch.setenv("JARVIS_WO_ID", "wo-11111111")
    with pytest.raises(ops.OpsError, match=f"`jarvis do experiment {do['id']} p1`"):
        ops.start_decision_experiment(do["id"], "p1")
    with pytest.raises(ops.OpsError, match=f"`jarvis do experiment {do['id']} --go`"):
        ops.approve_decision_experiment(do["id"])
    monkeypatch.delenv("JARVIS_WO_ID")
    assert ops.start_decision_experiment(do["id"], "p1")["status"] == "planning"


@pytest.mark.parametrize("status,needle", [
    ("planning", "already running — jarvis do show"),
    ("cancelled", "jarvis do create proj_a"),
    ("budget_exhausted", "jarvis do budget {do} <usd>"),
])
def test_a_wrong_status_is_refused(started, store, status, needle):
    do = _decision(store, researcher="completed", report=True)
    store.update_feature_order(do["id"], status=status, decided_by_user=status == "cancelled")
    with pytest.raises(ops.OpsError, match=needle.format(do=do["id"]).replace("(", r"\(")):
        ops.start_decision_experiment(do["id"], "p1")


def test_a_budget_exhausted_order_runs_once_the_budget_is_raised(started, store):
    do = _decision(store, researcher="completed", report=True)
    store.set_feature_status(do["id"], "budget_exhausted")
    with pytest.raises(ops.OpsError, match=f"jarvis do budget {do['id']} <usd>"):
        ops.start_decision_experiment(do["id"], "p1")
    ops.set_decision_budget(do["id"], 50.0)
    store.set_feature_status(do["id"], "awaiting_decision")
    assert ops.start_decision_experiment(do["id"], "p1")["status"] == "planning"


def test_one_experiment_at_a_time_and_a_live_researcher_is_refused(started, store,
                                                                  catalog_file):
    do = _awaiting(store, catalog_file)
    store.append_experiment(do["id"], {"key": "", "reported_at": None})
    with pytest.raises(ops.OpsError, match="one experiment at a time.*jarvis do show"):
        ops.start_decision_experiment(do["id"], "p1")
    other = _awaiting(store, catalog_file)
    store.set_status(other["plan_wo_id"], "running")
    with pytest.raises(ops.OpsError, match=f"jarvis do show {other['id']}"):
        ops.start_decision_experiment(other["id"], "p1")


def test_go_needs_a_forecast(started, store, catalog_file):
    do = _awaiting(store, catalog_file)
    ops.start_decision_experiment(do["id"], "p1")
    with pytest.raises(ops.OpsError, match="forecast_due.*jarvis do show"):
        ops.approve_decision_experiment(do["id"])


def test_the_results_gate_names_each_missing_step(started, store, catalog_file):
    do = _awaiting(store, catalog_file)
    ops.start_decision_experiment(do["id"], "p1")
    with pytest.raises(ops.OpsError, match=f"jarvis do forecast {do['id']}"):
        ops.submit_decision_report(do["id"], RESULTS)
    n = store.open_experiment(do["id"])["n"]
    store.update_experiment(do["id"], n, forecast=FORECAST)
    with pytest.raises(ops.OpsError, match=f"jarvis do experiment {do['id']} --go"):
        ops.submit_decision_report(do["id"], RESULTS)
    ops.approve_decision_experiment(do["id"])
    with pytest.raises(ops.OpsError, match=f"jarvis do attach {do['id']}"):
        ops.submit_decision_report(do["id"], RESULTS)
    store.add_experiment_attachment(do["id"], n, {"name": "raw", "size": 1, "sha256": "x",
                                                  "path": "p", "at": 0.0})
    with pytest.raises(ops.OpsError, match="identical"):
        ops.submit_decision_report(do["id"], REPORT)
    assert decision_report.experiment_phase(store.open_experiment(do["id"])) == "running"
    assert ops.decision_report(store.get_feature_order(do["id"]))["summary"] == REPORT["summary"]
    ops.submit_decision_report(do["id"], RESULTS)
    assert store.open_experiment(do["id"]) is None


# -- follow-up, the brief, the user's close


def test_follow_up_as_experiment_starts_rather_than_files(started, store, catalog_file):
    do = _awaiting(store, catalog_file)
    plan = {**REPORT, "proposed_orders": [
        *REPORT["proposed_orders"],
        {"key": "p2", "as": "experiment", "project": "", "title": "time it",
         "description": "24 runs"},
        {"key": "p3", "as": "experiment", "project": "", "title": "again",
         "description": "x"}]}
    store.update_feature_order(do["id"], plan=db.to_json(plan))
    out = ops.file_decision_followups(do["id"], all_=True)
    assert [s["key"] for s in out["started"]] == ["p2"] and out["status"] == "planning"
    assert out["errors"] == [{"key": "p3", "error": "one experiment at a time"}]
    assert [f["key"] for f in out["filed"]] == ["p1"]
    stored = ops.decision_report(store.get_feature_order(do["id"]))["proposed_orders"]
    assert not any(p.get("filed") for p in stored if p["key"] != "p1")
    assert store.experiments_of(do["id"])[0]["key"] == "p2"


def test_cli_follow_up_as_experiment(started, store, catalog_file):
    do = _awaiting(store, catalog_file)
    assert cli.main(["do", "follow-up", do["id"], "p1", "--as", "experiment", "--json"]) == 0
    assert store.experiments_of(do["id"])[0]["key"] == "p1"


def test_the_cli_starts_and_goes(started, store, catalog_file):
    do = _awaiting(store, catalog_file)
    assert cli.main(["do", "experiment", do["id"], "-d", "time both", "--json"]) == 0
    store.update_experiment(do["id"], 1, forecast=FORECAST)
    assert cli.main(["do", "experiment", do["id"], "--go", "--json"]) == 0
    assert store.open_experiment(do["id"])["go_at"] is not None


def test_the_brief_forecasts_before_any_run():
    fo = {"id": "do-1", "title": "which cache"}
    text = decision_report.experiment_brief(fo, {"n": 2, "brief": ""},
                                            {"description": "time both caches"})
    assert text.index("jarvis do forecast do-1") < text.index("On go: run it")
    assert "Do NOT start the batch" in text and "time both caches" in text
    assert decision_report.FORECAST_SCHEMA_TEXT in text
    assert "experiment 2 of decision order do-1" in text
    adhoc = decision_report.experiment_brief(fo, {"n": 1, "brief": "ad hoc"}, None)
    assert "ad hoc" in adhoc


def test_done_mid_experiment_closes_and_ticks_never_settle(started, store, catalog_file):
    do = _awaiting(store, catalog_file)
    ops.start_decision_experiment(do["id"], "p1")
    store.update_experiment(do["id"], 1, forecast=FORECAST)
    d = _daemon(catalog_file)
    for _ in range(6):
        d.tick_count = 0
        d.tick()
        d.stuck_tick(None)
        d.health_tick(None)
        invariants.check_project(store, repair=True, slow=True)
        assert store.get_feature_order(do["id"])["status"] == "planning"
    ops.complete_decision_order(do["id"], note="enough")
    assert store.get_feature_order(do["id"])["status"] == "completed"
