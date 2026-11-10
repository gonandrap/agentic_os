"""`jarvis do forecast`: parse, compute, record, settle, flag (docs/superpowers/specs/2026-11-10-decision-order-experiments.md §5)"""

from __future__ import annotations

import json
import re

import pytest

from jarvis import cli, decision_report, experiment_forecast, hooks, ops
from jarvis.project_store import DECISION_FORECAST_EVENT, ProjectStore

DOC = {"runs": 24, "model": "claude-sonnet-4-5",
       "per_run": {"input_tokens": 40000, "output_tokens": 3000, "minutes": 2.5},
       "notes": "4 fixtures x 2 engines x 3 repeats"}
METER = {"used_pct": 30.0, "resets_at": 1_000.0, "dollars_per_point": 0.5}


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def _experiment(store, **entry):
    """A do reopened into `planning` with a running researcher and an open experiment"""
    do = ops.create_decision_order("proj_a", "which cache", "compare the two caches")
    wo = store.create_work_order("research it", kind="researcher", parent_id=do["id"])
    store.set_status(wo["id"], "running")
    store.update_feature_order(do["id"], plan_wo_id=wo["id"])
    store.set_feature_status(do["id"], "planning")
    store.append_experiment(do["id"], {"key": "p1", "title": "t", "brief": "b",
                                       "researcher_id": wo["id"], "started_at": 1.0,
                                       "forecast": None, "go_at": None,
                                       "attachments": [], "reported_at": None, **entry})
    return do["id"], wo["id"]


def _notes(store, do_id):
    return [n for n in store.conn.execute("SELECT title FROM notifications").fetchall()
            if n["title"].startswith(do_id)]


# -- the pure module


def test_the_schema_text_example_parses():
    example = decision_report.FORECAST_SCHEMA_TEXT[
        decision_report.FORECAST_SCHEMA_TEXT.index("{"):]
    got = experiment_forecast.parse_forecast(json.loads(example))
    assert got["runs"] == 24 and got["per_run"]["minutes"] == 2.5


def test_every_parse_problem_is_listed_together():
    bad = {"runs": 0, "model": " ", "per_run": {"input_tokens": -1, "output_tokens": 1.5,
                                                "minutes": -2}, "notes": "x" * 1001}
    with pytest.raises(experiment_forecast.ForecastError) as e:
        experiment_forecast.parse_forecast(bad)
    assert len(e.value.problems) == 6
    with pytest.raises(experiment_forecast.ForecastError, match="1 to 10000"):
        experiment_forecast.parse_forecast({**DOC, "runs": 10001})


def test_compute_with_a_meter_sample():
    got = experiment_forecast.compute(experiment_forecast.parse_forecast(DOC), METER)
    one = experiment_forecast.run_usd(DOC)
    assert got["total_tokens"] == 24 * 43000 and got["minutes_total"] == 60
    assert got["est_usd"] == round(24 * one, 4) and got["meter"] == "available"
    assert got["points_per_run"] == round(one / 0.5, 4)
    assert got["window_share_pct"] == round(24 * one / 0.5, 2)
    assert got["window_used_pct"] == 30.0 and got["resets_at"] == 1_000.0
    assert got["max_safe_runs_now"] == int((100 - 30 - 10) / (one / 0.5))
    assert got["fits_now"] == (24 <= got["max_safe_runs_now"])
    full = experiment_forecast.compute(DOC, {**METER, "used_pct": 95.0})
    assert full["max_safe_runs_now"] == 0 and full["fits_now"] is False


def test_compute_with_the_meter_unavailable():
    got = experiment_forecast.compute(DOC, None)
    assert got["meter"] == "unavailable" and got["est_usd"] > 0
    assert all(got[k] is None for k in experiment_forecast.MEASURED_FIELDS)
    assert "unavailable" in experiment_forecast.render(got)


def test_a_zero_priced_run_has_an_unknown_fit():
    free = {**DOC, "per_run": {"input_tokens": 0, "output_tokens": 0, "minutes": 1}}
    got = experiment_forecast.compute(free, METER)
    assert got["max_safe_runs_now"] is None and got["fits_now"] is None
    text = experiment_forecast.render(got)
    assert "unknown" in text and "None" not in text


# -- ops.forecast_meter reads the real usage_meter surface


def _meter(monkeypatch, *, token=lambda: "tok", sample=None, rate=None):
    from jarvis import usage_meter

    monkeypatch.setattr(usage_meter, "token", token)
    monkeypatch.setattr(usage_meter, "latest_sample", lambda **_: sample)
    monkeypatch.setattr(usage_meter, "dollars_per_point", lambda **_: rate)


def _rate(value=0.5):
    return {"value": value, "low": value, "high": value, "spread_low": None,
            "spread_high": None, "spread_spans": 0, "spread_widened": False,
            "source": "measured", "basis_spans": 3, "basis_points": 9.0, "basis_usd": 4.5}


def test_forecast_meter_returns_the_reading(jarvis_home, monkeypatch):
    from jarvis.usage_meter import Sample

    _meter(monkeypatch, sample=Sample(ts=1.0, five_hour_pct=30.0,
                                      five_hour_resets_at=1_000.0), rate=_rate())
    assert ops.forecast_meter() == METER


def _raise():
    from jarvis.usage_meter import MeterError

    raise MeterError("credentials file has no claudeAiOauth.accessToken")


@pytest.mark.parametrize("case", ["no_token", "no_sample", "no_pct", "bad_rate"])
def test_forecast_meter_is_unavailable_never_a_refusal(started, store, monkeypatch, case):
    from jarvis.usage_meter import Sample

    sample = Sample(ts=1.0, five_hour_pct=None if case == "no_pct" else 30.0,
                    five_hour_resets_at=1_000.0)
    _meter(monkeypatch, token=_raise if case == "no_token" else (lambda: "tok"),
           sample=None if case == "no_sample" else sample,
           rate={} if case == "bad_rate" else _rate())
    assert ops.forecast_meter() is None
    do_id, _ = _experiment(store)
    out = ops.submit_decision_forecast(do_id, DOC)
    assert out["forecast"]["meter"] == "unavailable" and out["flagged"]


# -- ops.submit_decision_forecast


def test_submit_settles_the_researcher_without_a_round_and_flags_once(
        started, store, monkeypatch):
    monkeypatch.setattr(ops, "forecast_meter", lambda now=None: METER)
    do_id, wo_id = _experiment(store)
    out = ops.submit_decision_forecast(do_id, DOC)
    assert out["flagged"] and out["researcher"]["status"] == "completed"
    assert store.get_work_order(wo_id)["status"] == "completed"
    assert store.conn.execute("SELECT COUNT(*) FROM validation_rounds").fetchone()[0] == 0
    [entry] = store.experiments_of(do_id)
    assert entry["forecast"]["runs"] == 24
    assert decision_report.experiment_phase(entry) == "awaiting_go"
    do = store.get_feature_order(do_id)
    assert do["status"] == "planning" and do["needs_attention"]
    assert do["attention_reason"].startswith("experiment 1 forecast: 24 runs, ~")
    assert do["attention_reason"].endswith(f"jarvis do experiment {do_id} --go")
    assert len(_notes(store, do_id)) == 1
    events = [e for e in store.list_events(wo_id) if e["kind"] == DECISION_FORECAST_EVENT]
    assert len(events) == 1


def test_a_reforecast_while_awaiting_go_replaces_the_stored_one(
        started, store, monkeypatch):
    monkeypatch.setattr(ops, "forecast_meter", lambda now=None: None)
    do_id, _ = _experiment(store)
    ops.submit_decision_forecast(do_id, DOC)
    ops.submit_decision_forecast(do_id, {**DOC, "runs": 6})
    [entry] = store.experiments_of(do_id)
    assert entry["forecast"]["runs"] == 6 and entry["forecast"]["meter"] == "unavailable"
    assert "usage meter unavailable" in store.get_feature_order(do_id)["attention_reason"]


def test_submit_is_refused_while_running_and_names_the_report_verb(started, store):
    do_id, _ = _experiment(store, forecast={"runs": 1}, go_at=5.0)
    with pytest.raises(ops.OpsError) as e:
        ops.submit_decision_forecast(do_id, DOC)
    command = re.search(r"`(jarvis do report [^`]+)`", str(e.value)).group(1)
    assert cli.build_parser().parse_args(command.split()[1:]).do_cmd == "report"
    assert store.experiments_of(do_id)[0]["forecast"] == {"runs": 1}


def test_a_bad_forecast_stores_nothing_and_names_the_resubmit(started, store):
    do_id, wo_id = _experiment(store)
    with pytest.raises(ops.OpsError) as e:
        ops.submit_decision_forecast(do_id, {**DOC, "runs": 0, "model": ""})
    assert str(e.value).count("\n  - ") == 2
    command = re.search(r"`(jarvis do forecast [^`]+)`", str(e.value)).group(1)
    assert cli.build_parser().parse_args(command.split()[1:]).do_cmd == "forecast"
    assert store.experiments_of(do_id)[0]["forecast"] is None
    assert store.get_work_order(wo_id)["status"] == "running"


def test_the_cli_reads_the_file(started, store, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ops, "forecast_meter", lambda now=None: None)
    do_id, _ = _experiment(store)
    path = tmp_path / "forecast.json"
    path.write_text(json.dumps(DOC))
    assert cli.main(["do", "forecast", do_id, "--from-file", str(path), "--json"]) == 0
    assert store.experiments_of(do_id)[0]["forecast"]["runs"] == 24


# -- the researcher's hooks let it through


def test_the_researcher_hooks_allow_do_forecast():
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "jarvis do forecast do-1234abcd --from-file "
                                         "forecast.json"}}
    env = {hooks.WO_KIND_ENV: "researcher", "JARVIS_WO_ID": "wo-1234abcd"}
    assert hooks.researcher_bash_decision(payload, env) is None
    assert hooks.decision_close_decision(payload, env) is None
