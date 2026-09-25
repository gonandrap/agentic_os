"""`ops.submit_findings` — the analyst's terminal action.

Section 4.1 of docs/superpowers/specs/2026-09-23-improvement-orders.md. The ORDER of the
steps is the design, and each of the three properties that order buys has a test here:

* validation happens BEFORE any write, so a bad report costs one revision and nothing
  else — nothing stored, no attention spent, no state to unwind;
* a resubmission replaces the stored report wholesale, and the status refusal is what
  keeps that safe;
* the analyst is settled exactly ONCE, because settling an already-settled work order is
  not an idempotent no-op in this codebase.
"""

from __future__ import annotations

import json as _json

import pytest

from jarvis import cli, db, findings, ops, project_store
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import a_finding, a_report

REFS = ("wo-11111111", "#42")
OBSERVATION = ("Three work orders in a row spent their first turn re-reading the same "
               "module because nothing told them where the dispatch path lives.")


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def analysing(started, store, improvement_order):
    """An improvement order in `planning` with an open analyst wired to it."""
    # `kind='analyst'` lands with the dispatch piece; `submit_findings` never reads the
    # kind, so the analyst is created here as a planner.
    analyst = store.create_work_order("analyse it", description=OBSERVATION,
                                      kind="planner", status="running")
    store.update_feature_order(improvement_order["id"], plan_wo_id=analyst["id"],
                               status="planning")
    return improvement_order, analyst


def stored_report(store, io_id):
    return db.from_json(store.get_feature_order(io_id)["plan"], None)


def test_an_invalid_report_is_refused_before_anything_is_written(analysing, store):
    io, analyst = analysing

    with pytest.raises(ops.OpsError) as e:
        ops.submit_findings(io["id"], a_report(
            summary="",
            findings=[a_finding("a", why_insufficient="no", evidence=[])]))

    message = str(e.value)
    assert "`summary` is required" in message
    assert "`why_insufficient` is" in message
    assert "`evidence` must be a non-empty list" in message

    row = store.get_feature_order(io["id"])
    assert row["plan"] is None
    assert row["status"] == "planning"
    assert not row["needs_attention"]
    assert store.get_work_order(analyst["id"])["status"] == "running"


def test_a_good_report_is_stored_whole_with_every_finding_pending(analysing, store):
    io, _ = analysing

    out = ops.submit_findings(io["id"], a_report())

    assert out["findings"] == 2
    report = stored_report(store, io["id"])
    assert report["summary"] == a_report()["summary"]
    assert [f["key"] for f in report["findings"]] == ["first-turn-reads",
                                                      "stale-code-map"]
    assert [f["status"] for f in report["findings"]] == ["pending", "pending"]


def test_it_parks_the_order_for_review_with_exactly_one_attention_item(analysing, store):
    io, _ = analysing

    out = ops.submit_findings(io["id"], a_report())

    assert out["status"] == "plan_review"
    row = store.get_feature_order(io["id"])
    assert row["status"] == "plan_review"
    assert row["needs_attention"]
    assert row["attention_reason"] == findings.review_headline(
        row, stored_report(store, io["id"]))
    assert row["attention_reason"].startswith("2 findings on ")
    assert f"jarvis io review {io['id']}" in row["attention_reason"]


def test_the_analyst_is_settled_once_and_a_resubmission_leaves_it_alone(analysing,
                                                                       store):
    io, analyst = analysing

    first = ops.submit_findings(io["id"], a_report())
    assert first["analyst"]["wo_id"] == analyst["id"]
    settled = store.get_work_order(analyst["id"])
    assert settled["status"] not in project_store.OPEN_STATUSES

    second = ops.submit_findings(io["id"], a_report(summary="A second reading."))

    assert "analyst" not in second
    again = store.get_work_order(analyst["id"])
    assert again["status"] == settled["status"]
    assert again["result_summary"] == settled["result_summary"]


def test_a_resubmission_replaces_the_report_and_every_decision_on_it(analysing, store):
    io, _ = analysing
    ops.submit_findings(io["id"], a_report())

    # A decision recorded by hand, exactly as `jarvis io review` will record one.
    report = stored_report(store, io["id"])
    report["findings"][0]["status"] = "accepted"
    store.update_feature_order(io["id"], plan=db.to_json(report))

    ops.submit_findings(io["id"], a_report(
        summary="A different reading of the same order.",
        findings=[a_finding("queue-starvation")]))

    fresh = stored_report(store, io["id"])
    assert [f["key"] for f in fresh["findings"]] == ["queue-starvation"]
    assert [f["status"] for f in fresh["findings"]] == ["pending"]
    assert fresh["summary"] == "A different reading of the same order."


@pytest.mark.parametrize("status", ["completed", "cancelled", "executing", "pending"])
def test_every_other_status_is_refused_with_the_status_named(analysing, store, status):
    io, _ = analysing
    store.update_feature_order(io["id"], status=status)

    with pytest.raises(ops.OpsError) as e:
        ops.submit_findings(io["id"], a_report())

    assert status in str(e.value)
    assert store.get_feature_order(io["id"])["plan"] is None


def test_a_feature_order_is_refused_and_named_for_the_verb_that_works(started, store):
    fo = store.create_feature_order("CSV export", description="the whole ask")
    store.update_feature_order(fo["id"], status="planning")

    with pytest.raises(ops.OpsError) as e:
        ops.submit_findings(fo["id"], a_report())

    assert "jarvis fo plan" in str(e.value)


def test_it_submits_with_no_analyst_at_all(started, store, improvement_order):
    store.update_feature_order(improvement_order["id"], status="planning")

    out = ops.submit_findings(improvement_order["id"], a_report())

    assert "analyst" not in out
    assert out["status"] == "plan_review"
    assert stored_report(store, improvement_order["id"])["findings"][0]["status"] == \
        "pending"


def test_a_tick_never_re_raises_the_attention_the_user_will_lower(started, analysing,
                                                                  store):
    """§6.2 and kn-089de524: nothing re-derives improvement-order attention on a tick.

    The flag is raised ONCE, at the transition in `submit_findings`. A flag written on a
    path that re-derives every tick re-raises itself and overwrites the user's ack, so
    the proof is by construction: reconcile several times and the flag, its reason and
    the status must all be byte-for-byte what the submission wrote.
    """
    io, _ = analysing
    ops.submit_findings(io["id"], a_report())
    before = store.get_feature_order(io["id"])

    for _ in range(3):
        started.tick()

    after = store.get_feature_order(io["id"])
    assert after["needs_attention"] == before["needs_attention"]
    assert after["attention_reason"] == before["attention_reason"]
    assert after["status"] == "plan_review"


def test_cli_io_report_reads_the_file_and_parks_the_order(analysing, store, tmp_path,
                                                          capsys):
    io, _ = analysing
    path = tmp_path / "report.json"
    path.write_text(_json.dumps(a_report()))

    assert cli.main(["io", "report", io["id"], "--from-file", str(path), "--json"]) == 0

    out = _json.loads(capsys.readouterr().out)
    assert out["status"] == "plan_review"
    assert out["findings"] == 2
    row = store.get_feature_order(io["id"])
    assert row["status"] == "plan_review"
    assert stored_report(store, io["id"])["summary"] == a_report()["summary"]


def test_cli_io_report_refuses_a_missing_file(analysing, store, tmp_path, capsys):
    io, _ = analysing
    missing = tmp_path / "nope.json"

    assert cli.main(["io", "report", io["id"], "--from-file", str(missing)]) == 1

    assert f"no such report file: {missing}" in capsys.readouterr().err
    assert store.get_feature_order(io["id"])["plan"] is None


def test_cli_io_report_refuses_invalid_json(analysing, store, tmp_path, capsys):
    io, _ = analysing
    path = tmp_path / "report.json"
    path.write_text("{not json")

    assert cli.main(["io", "report", io["id"], "--from-file", str(path)]) == 1

    assert "is not valid JSON" in capsys.readouterr().err
    assert store.get_feature_order(io["id"])["plan"] is None
