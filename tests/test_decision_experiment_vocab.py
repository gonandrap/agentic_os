"""Decision-order experiments: the shared vocabulary (docs/superpowers/specs/2026-11-10-decision-order-experiments.md §2)"""

from __future__ import annotations

import pytest

from jarvis import decision_report, specs
from jarvis.project_store import EXPERIMENTS_KEY, ProjectStore

REPORT = {
    "summary": "B is 30% cheaper",
    "findings": [{"key": "f1", "title": "cost", "detail": "d", "evidence": ["x"]}],
    "recommendation": {"option": "", "why": "cheaper"},
    "proposed_orders": [{"key": "p1", "as": "experiment", "project": "",
                         "title": "measure it", "description": "run the batch"}],
}


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def _entry(**over):
    return {"key": "p1", "title": "t", "brief": "b", "researcher_id": "wo-x",
            "started_at": 1.0, "reopened_from": "awaiting_decision", "forecast": None,
            "go_at": None, "attachments": [], "reported_at": None, **over}


def test_helpers_round_trip_in_metadata_not_plan(store):
    do = store.create_feature_order("which cache", kind="decision",
                                    metadata={"other": 1})
    assert store.experiments_of(do["id"]) == []
    assert store.open_experiment(do["id"]) is None
    assert store.append_experiment(do["id"], _entry()) == 1
    store.update_experiment(do["id"], 1, go_at=5.0)
    mark = {"name": "out.jsonl", "size": 3, "sha256": "ab", "path": "p", "at": 6.0}
    store.add_experiment_attachment(do["id"], 1, mark)
    [got] = store.experiments_of(do["id"])
    assert got == {**_entry(), "n": 1, "go_at": 5.0, "attachments": [mark]}
    row = store.get_feature_order(do["id"])
    assert not row.get("plan")
    assert '"other": 1' in row["metadata"] and EXPERIMENTS_KEY in row["metadata"]


def test_open_experiment_is_the_last_unreported(store):
    do = store.create_feature_order("which cache", kind="decision")["id"]
    store.append_experiment(do, _entry(key="p1"))
    assert store.append_experiment(do, _entry(key="")) == 2
    assert store.open_experiment(do)["n"] == 2
    store.update_experiment(do, 2, reported_at=9.0)
    assert store.open_experiment(do)["n"] == 1
    store.update_experiment(do, 1, reported_at=9.0)
    assert store.open_experiment(do) is None


def test_update_of_a_missing_experiment_raises(store):
    do = store.create_feature_order("which cache", kind="decision")["id"]
    with pytest.raises(KeyError):
        store.update_experiment(do, 1, go_at=1.0)


@pytest.mark.parametrize("stamps, phase", [
    ({}, "forecast_due"),
    ({"forecast": {"runs": 1}}, "awaiting_go"),
    ({"forecast": {"runs": 1}, "go_at": 2.0}, "running"),
    ({"forecast": {"runs": 1}, "go_at": 2.0, "reported_at": 3.0}, "reported"),
])
def test_experiment_phase(stamps, phase):
    assert decision_report.experiment_phase(_entry(**stamps)) == phase


def test_parse_report_accepts_an_experiment_proposal():
    parsed = decision_report.parse_report(REPORT)
    assert parsed["proposed_orders"][0]["as"] == "experiment"


def test_forecast_schema_text_carries_a_json_example():
    import json
    text = decision_report.FORECAST_SCHEMA_TEXT
    example = json.loads(text[text.index("{"):])
    assert example["runs"] >= 1 and example["per_run"]["input_tokens"] >= 0


def test_experiment_dir_and_a_terminal_status_keeps_it(store, project):
    path = specs.experiment_dir(project, "do-1", 2, "")
    assert path == specs.feature_dir(project, "do-1") / "experiments" / "2-adhoc"
    assert specs.experiment_dir(project, "do-1", 1, "p1").name == "1-p1"
    assert not path.exists()
    fo = store.create_feature_order("f")["id"]
    out = specs.experiment_dir(project, fo, 1, "p1")
    out.mkdir(parents=True)
    specs.agent_root(project, fo).mkdir(parents=True)
    store.set_feature_status(fo, "cancelled")
    assert not specs.agent_root(project, fo).exists()
    assert out.exists()
