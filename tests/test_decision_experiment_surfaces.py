"""Experiments on do show, the timeline, and the verbs composed (docs/superpowers/specs/2026-11-10-decision-order-experiments.md §7)"""

from __future__ import annotations

import json

import pytest

from jarvis import cli, ops, timeline
from tests.test_decision_experiments import RESULTS, _completed
from tests.test_decision_orders import (_daemon, _decision_notes, started,  # noqa: F401
                                        store)

FORECAST = {"runs": 24, "model": "m", "notes": "4 x 2 x 3",
            "per_run": {"input_tokens": 1000, "output_tokens": 100, "minutes": 2}}


def _show(capsys, do_id: str, *, as_json: bool = False):
    capsys.readouterr()
    cli.main(["do", "show", do_id] + (["--json"] if as_json else []))
    out = capsys.readouterr().out
    return json.loads(out) if as_json else out


def _researching(store, catalog_file):
    do = _completed(store, catalog_file)
    out = ops.start_decision_experiment(do["id"], "p1")
    store.set_status(out["researcher_id"], "running")
    return do, out["researcher_id"]


# -- do show


def test_show_with_no_experiment(started, store, catalog_file, capsys):
    do = _completed(store, catalog_file)
    assert _show(capsys, do["id"], as_json=True)["experiments"] == []
    assert "Experiment" not in _show(capsys, do["id"])


def test_show_a_forecast_awaiting_go_names_the_go_command(started, store, catalog_file,
                                                          capsys):
    do, _ = _researching(store, catalog_file)
    ops.submit_decision_forecast(do["id"], FORECAST)
    data = _show(capsys, do["id"], as_json=True)
    [e] = data["experiments"]
    assert (e["n"], e["phase"], e["forecast"]["runs"]) == (1, "awaiting_go", 24)
    text = _show(capsys, do["id"])
    assert "Experiment 1: migrate (awaiting your go)" in text
    assert "24 runs of m" in text
    assert "usage meter unavailable" in text or "5h window: ~" in text
    go = f"jarvis do experiment {do['id']} --go"
    assert f"go: {go}" in text
    # The named command is the one that is accepted
    cli.main(go.split()[1:])
    assert ops.show_decision_order(do["id"])["experiments"][0]["phase"] == "running"
    assert "--go" not in _show(capsys, do["id"])


def test_show_a_reported_experiment_lists_absolute_attachment_paths(started, store,
                                                                    catalog_file, capsys,
                                                                    tmp_path):
    do, _ = _researching(store, catalog_file)
    ops.submit_decision_forecast(do["id"], FORECAST)
    ops.approve_decision_experiment(do["id"])
    raw = tmp_path / "raw.json"
    raw.write_text("[1, 2, 3]")
    ops.attach_decision_outputs(do["id"], [raw])
    ops.submit_decision_report(do["id"], RESULTS)
    [e] = _show(capsys, do["id"], as_json=True)["experiments"]
    [a] = e["attachments"]
    assert e["phase"] == "reported"
    assert a["abs_path"] == str(store.project_path / a["path"])
    text = _show(capsys, do["id"])
    assert "(reported)" in text and f"- {a['abs_path']} (9 bytes)" in text
    assert "--go" not in text


# -- timeline


@pytest.mark.parametrize("kind,payload,label,detail", [
    ("decision_experiment_started",
     {"do_id": "do-1", "n": 1, "key": "p1", "reopened_from": "completed"},
     "Experiment 1 started for do-1", "proposal p1; reopened from completed"),
    ("decision_experiment_started",
     {"do_id": "do-1", "n": 2, "key": "", "reopened_from": "awaiting_decision"},
     "Experiment 2 started for do-1", "an ad-hoc brief; reopened from awaiting_decision"),
    ("decision_forecast_submitted",
     {"do_id": "do-1", "n": 1, "runs": 24, "est_usd": 3.5, "window_share_pct": 12.0,
      "meter": "available", "replaced": False},
     "Experiment 1 forecast: 24 runs", "~$3.50, ~12% of the 5h window"),
    ("decision_forecast_submitted",
     {"do_id": "do-1", "n": 1, "runs": 24, "est_usd": 3.5, "window_share_pct": None,
      "meter": "unavailable", "replaced": True},
     "Experiment 1 re-forecast: 24 runs", "~$3.50, usage meter unavailable"),
    ("decision_experiment_go", {"do_id": "do-1", "n": 1},
     "You said go on experiment 1", "do-1"),
    ("decision_attached",
     {"do_id": "do-1", "n": 1, "name": "raw.json", "size": 2048, "sha256": "ab" * 32},
     "Attached raw.json to experiment 1", "2,048 bytes, sha256 abababababab"),
])
def test_timeline_labels_each_experiment_event(kind, payload, label, detail):
    assert timeline._describe(kind, payload) == (label, detail)


# -- the sections composed, through the real verbs


def test_the_real_verbs_compose_to_one_revision(started, store, catalog_file, tmp_path):
    do, rid = _researching(store, catalog_file)
    before = len(_decision_notes(store, do["id"]))
    ops.submit_decision_forecast(do["id"], FORECAST)
    assert store.get_work_order(rid)["status"] == "completed"
    ops.approve_decision_experiment(do["id"])
    store.set_status(rid, "running")  # the go message revives it; fake_claude cannot run its turn
    raw = tmp_path / "raw.json"
    raw.write_text("{}")
    ops.attach_decision_outputs(do["id"], [raw])
    ops.submit_decision_report(do["id"], RESULTS)
    d = _daemon(catalog_file)
    for _ in range(3):
        d.tick()
    statuses = [json.loads(e["payload"]).get("status")
                for e in store.events_of_kind(rid, "status")]
    assert "needs_review" not in statuses, statuses
    assert store.get_work_order(rid)["status"] == "completed"
    row = store.get_feature_order(do["id"])
    assert row["status"] == "awaiting_decision" and row["needs_attention"]
    notes = _decision_notes(store, do["id"])[before:]
    revised = [n for n in notes if "report revised" in n["title"]]
    assert len(revised) == 1, [n["title"] for n in notes]
