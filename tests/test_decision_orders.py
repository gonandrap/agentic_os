"""Decision orders: the kind, its states and the user-only close (docs/superpowers/specs/2026-10-31-decision-orders.md §2)"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jarvis import cli, invariants, ops, project_store, timeline
from jarvis.project_store import (FO_STATUSES, FO_TERMINAL_STATUSES, WORKER_CHILDREN,
                                  DecisionOrderGuard, ProjectStore, decision_report_sha)

REPORT = {
    "summary": "B is 30% cheaper",
    "findings": [{"key": "f1", "title": "cost", "detail": "d", "evidence": ["x"]}],
    "options": [{"key": "a", "title": "keep A", "pros": [], "cons": [], "cost": ""},
                {"key": "b", "title": "move to B", "pros": [], "cons": [], "cost": ""}],
    "recommendation": {"option": "b", "why": "cheaper"},
    "proposed_orders": [{"key": "p1", "as": "wo", "project": "", "title": "migrate",
                         "description": "move to B"}],
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


def _decision(store, *, researcher: str | None = "running", report: bool = False):
    do = ops.create_decision_order("proj_a", "which cache", "compare the two caches")
    if researcher:
        wo = store.create_work_order("research it", kind="researcher", parent_id=do["id"])
        store.set_status(wo["id"], researcher)
        store.update_feature_order(do["id"], plan_wo_id=wo["id"])
        store.set_feature_status(do["id"], "planning")
    if report:
        store.update_feature_order(do["id"], plan=project_store.db.to_json(REPORT))
    return store.get_feature_order(do["id"])


# -- the record


def test_a_decision_order_is_a_do_row_with_its_own_vocabulary(started, store):
    do = _decision(store, researcher=None)
    assert do["id"].startswith("do-") and do["kind"] == "decision"
    assert project_store.is_feature_order_id(do["id"])
    assert project_store.FAMILY_CHILD_KINDS["decision"] == ("researcher",)
    assert project_store.decide_command("decision", do["id"]) == f"jarvis do show {do['id']}"
    label = project_store.feature_status_label
    assert label("decision", "planning") == "researching"
    assert label("decision", "awaiting_decision") == "awaiting your decision"
    assert label("decision", "completed") == "decided"
    assert "awaiting_decision" in project_store.FO_OPEN_STATUSES
    contract = project_store.WO_KIND_CONTRACTS["researcher"]
    assert contract.route == project_store.SETTLE_BY_FINISH and not contract.lands_code
    assert contract.artifact == "report.json"


def test_every_feature_status_has_render_vocabulary():
    from jarvis.ui.app import FO_STATUS_META
    for status in FO_STATUSES:
        assert status in FO_STATUS_META and status in cli.FO_ICON, status


def test_the_report_sha_ignores_filed_marks_and_key_order():
    assert decision_report_sha(None) == "" and decision_report_sha("") == ""
    marked = {**REPORT, "proposed_orders": [{**REPORT["proposed_orders"][0],
                                             "filed": "wo-12345678"}]}
    assert decision_report_sha(marked) == decision_report_sha(REPORT)
    assert decision_report_sha(project_store.db.to_json(REPORT)) == decision_report_sha(REPORT)
    assert decision_report_sha({**REPORT, "summary": "A wins"}) != decision_report_sha(REPORT)
    assert "filed" in marked["proposed_orders"][0], "the caller's dict is never mutated"


# -- the guard


@pytest.mark.parametrize("status", FO_TERMINAL_STATUSES)
def test_no_writer_settles_a_decision_order_without_the_user(started, store, status):
    do = _decision(store)
    with pytest.raises(DecisionOrderGuard, match=do["id"]):
        store.set_feature_status(do["id"], status)
    with pytest.raises(DecisionOrderGuard):
        store.update_feature_order(do["id"], status=status)
    assert store.get_feature_order(do["id"])["status"] == "planning"
    store.set_feature_status(do["id"], status, decided_by_user=True)
    assert store.get_feature_order(do["id"])["status"] == status


@pytest.mark.parametrize("kind", ["feature", "improvement", "investigation"])
def test_the_guard_leaves_every_other_kind_alone(started, store, kind):
    fo = store.create_feature_order(title="t", description="d", kind=kind)
    store.set_feature_status(fo["id"], "completed")
    assert store.get_feature_order(fo["id"])["status"] == "completed"


def test_only_the_users_verb_passes_decided_by_user():
    src = Path(ops.__file__).parent
    hits = [(p.name, n) for p in src.rglob("*.py")
            for n, line in enumerate(p.read_text().splitlines(), 1)
            if re.search(r"decided_by_user\s*=\s*True", line)]
    assert [name for name, _ in hits] == ["ops.py"], hits
    import inspect
    assert "decided_by_user=True" in inspect.getsource(ops._user_closes_decision)


# -- the user's close


def test_done_closes_it_stops_the_researcher_and_records_who(started, store):
    do = _decision(store, report=True)
    store.set_feature_status(do["id"], "awaiting_decision")
    store.flag_feature_attention(do["id"], "report ready")

    out = ops.complete_decision_order(do["id"], note="going with B")

    row = store.get_feature_order(do["id"])
    assert row["status"] == "completed" and not row["needs_attention"]
    assert out["cancelled_work_orders"] == [do["plan_wo_id"]]
    assert store.get_work_order(do["plan_wo_id"])["status"] == "cancelled"
    ev = store.events_of_kind(do["plan_wo_id"], "decision_closed_by_user")
    assert project_store.db.from_json(ev[0]["payload"]) == {
        "do_id": do["id"], "status": "completed", "note": "going with B"}


def test_cancel_with_no_researcher_records_on_the_order_itself(started, store):
    do = _decision(store, researcher=None)
    ops.cancel_decision_order(do["id"])
    assert store.get_feature_order(do["id"])["status"] == "cancelled"
    last = store.state_spans(do["id"])[-1]
    assert (last["to_status"], last["trigger"]) == ("cancelled", "decision_closed_by_user")
    title, _ = timeline._describe("decision_closed_by_user",
                                  {"do_id": do["id"], "status": "cancelled"})
    assert title == f"You cancelled {do['id']}"


def test_done_without_a_report_points_at_cancel(started, store):
    do = _decision(store)
    with pytest.raises(ops.OpsError, match=f"jarvis do cancel {do['id']}"):
        ops.complete_decision_order(do["id"])
    # …but a spent budget is the user's to close either way
    store.set_feature_status(do["id"], "budget_exhausted")
    assert ops.complete_decision_order(do["id"])["status"] == "completed"


@pytest.mark.parametrize("verb", ["complete", "cancel"])
def test_an_agent_session_can_never_close_one(started, store, monkeypatch, verb):
    do = _decision(store, report=True)
    monkeypatch.setenv("JARVIS_WO_ID", "wo-11111111")
    fn = ops.complete_decision_order if verb == "complete" else ops.cancel_decision_order
    with pytest.raises(ops.OpsError, match="the user's verb"):
        fn(do["id"])
    assert store.get_feature_order(do["id"])["status"] == "planning"


def test_the_generic_verbs_refuse_a_decision_order_and_name_the_do_verb(started, store):
    do = _decision(store)
    with pytest.raises(ops.OpsError, match=f"jarvis do cancel {do['id']}"):
        ops.cancel_feature_order(do["id"])
    with pytest.raises(ops.OpsError, match="jarvis do show"):
        ops.resume_feature_order(do["id"])
    with pytest.raises(ops.OpsError, match="jarvis do show"):
        ops.review_plan(do["id"])
    with pytest.raises(ops.OpsError, match="decision order"):
        ops.cancel_investigation_order(do["id"])
    fo = store.create_feature_order(title="t", description="d")
    with pytest.raises(ops.OpsError, match="jarvis fo"):
        ops.complete_decision_order(fo["id"])


def test_a_work_order_cannot_be_filed_under_a_decision_order(started, store):
    do = _decision(store)
    with pytest.raises(ops.OpsError, match="without `--parent`"):
        ops.create_work_order("proj_a", "sneak", parent_id=do["id"])


def test_create_needs_a_description(started):
    with pytest.raises(ops.OpsError, match="researcher"):
        ops.create_decision_order("proj_a", "which cache", "  ")


def test_the_false_failure_repair_never_sees_the_researcher(started, store):
    do = _decision(store)
    assert store.feature_children(do["id"], kinds=WORKER_CHILDREN) == []
    assert not list(invariants.check_feature_failures_are_real(store))


# -- the CLI


def test_the_cli_drives_the_whole_surface(started, store, capsys):
    assert cli.main(["do", "create", "proj_a", "which cache", "-d", "compare them",
                     "--json"]) == 0
    do_id = re.search(r'"created": "(do-[0-9a-f]+)"', capsys.readouterr().out).group(1)
    assert cli.main(["do", "list"]) == 0
    assert do_id in capsys.readouterr().out
    store.update_feature_order(do_id, plan=project_store.db.to_json(REPORT))
    assert cli.main(["do", "show", do_id]) == 0
    out = capsys.readouterr().out
    assert "B is 30% cheaper" in out and "Recommendation: b" in out and "p1. (wo) migrate" in out
    assert cli.main(["do", "budget", do_id, "5"]) == 0
    assert cli.main(["do", "done", do_id, "--note", "B"]) == 0
    assert store.get_feature_order(do_id)["status"] == "completed"
    assert cli.main(["do", "list", "--all"]) == 0
    assert "decided" in capsys.readouterr().out
