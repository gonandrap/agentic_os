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


# -- delivery and doctor (§5)


def _daemon(catalog_file):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    return Daemon(load_catalog(catalog_file))


def _deliver(catalog_file, store):
    d = _daemon(catalog_file)
    d.deliver_decisions(d.catalog.project("proj_a"), store)


def _decision_notes(store, do_id):
    return [dict(n) for n in store.conn.execute("SELECT * FROM notifications").fetchall()
            if n["title"].startswith(do_id)]


def _delivered(store, catalog_file):
    do = _decision(store, researcher="completed", report=True)
    _deliver(catalog_file, store)
    return store.get_feature_order(do["id"])


def test_a_validated_report_is_delivered_once(started, store, catalog_file):
    do = _delivered(store, catalog_file)
    assert do["status"] == "awaiting_decision" and do["needs_attention"]
    assert do["attention_reason"].startswith("decision awaiting you: ")
    assert do["attention_reason"].endswith(f"jarvis do show {do['id']}")
    for _ in range(3):
        _deliver(catalog_file, store)
    notes = _decision_notes(store, do["id"])
    assert len(notes) == 1 and notes[0]["level"] == "warning"
    assert notes[0]["title"] == f"{do['id']} report ready: which cache"
    assert "B is 30% cheaper" in notes[0]["body"] and "Recommendation: b" in notes[0]["body"]
    ev = store.events_of_kind(do["plan_wo_id"], "decision_delivered")
    assert len(ev) == 1
    assert project_store.db.from_json(ev[0]["payload"]) == {
        "do_id": do["id"], "sha": decision_report_sha(REPORT), "revised": False}
    title, _ = timeline._describe("decision_delivered", {"do_id": do["id"], "revised": True})
    assert title == f"Decision report revision delivered to you for {do['id']}"


def test_a_running_researcher_is_not_delivered(started, store, catalog_file):
    do = _decision(store, researcher="running", report=True)
    _deliver(catalog_file, store)
    row = store.get_feature_order(do["id"])
    assert row["status"] == "planning" and not row["needs_attention"]


def test_a_revision_notifies_once_and_filing_is_not_a_revision(started, store, catalog_file):
    do = _delivered(store, catalog_file)
    marked = {**REPORT, "proposed_orders": [{**REPORT["proposed_orders"][0],
                                             "filed": [{"id": "wo-1"}]}]}
    store.update_feature_order(do["id"], plan=project_store.db.to_json(marked))
    _deliver(catalog_file, store)
    assert len(_decision_notes(store, do["id"])) == 1
    store.update_feature_order(do["id"], plan=project_store.db.to_json(
        {**REPORT, "summary": "A wins after all"}))
    for _ in range(2):
        _deliver(catalog_file, store)
    notes = _decision_notes(store, do["id"])
    assert sorted(n["title"] for n in notes) == [f"{do['id']} report ready: which cache",
                                                f"{do['id']} report revised: which cache"]
    assert store.get_feature_order(do["id"])["status"] == "awaiting_decision"


def test_delivery_reaches_the_users_sinks(started, store, catalog_file, monkeypatch):
    from jarvis import notify
    from jarvis.central_store import CentralStore

    do = _delivered(store, catalog_file)
    d = _daemon(catalog_file)
    d.route_outbox(d.catalog.project("proj_a"), store)
    seen = []
    monkeypatch.setitem(notify.SINKS, "log", lambda item, catalog: seen.append(item) or "ok")
    central = CentralStore()
    try:
        notify.route_new_inbox(central, d.catalog)
    finally:
        central.close()
    mine = [i for i in seen if i["title"].startswith(do["id"])]
    assert [i["title"] for i in mine] == [f"{do['id']} report ready: which cache"]
    # §7: the deep link opens the order's own page, not a work-order page that 404s
    assert mine[0]["wo_link_problem"] == ""
    assert mine[0]["wo_link"].endswith(f"/do/proj_a/{do['id']}")


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed"])
def test_a_dead_researcher_leaves_the_order_open_and_flagged_once(started, store,
                                                                  catalog_file, status):
    do = _decision(store, researcher=status)
    for _ in range(3):
        _deliver(catalog_file, store)
    row = store.get_feature_order(do["id"])
    assert row["status"] == "planning" and row["needs_attention"]
    assert row["attention_reason"].startswith(f"researcher {do['plan_wo_id']} ")
    assert f'jarvis do send {do["id"]} "retry"' in row["attention_reason"]
    assert len(_decision_notes(store, do["id"])) == 1


def test_send_reaches_the_researcher_and_leaves_the_order(started, store, catalog_file):
    do = _delivered(store, catalog_file)
    out = ops.send_decision_message(do["id"], "dig into option a")
    assert out["wo_id"] == do["plan_wo_id"] and out["status"] == "awaiting_decision"
    row = store.get_feature_order(do["id"])
    assert row["status"] == "awaiting_decision" and row["needs_attention"]
    assert cli.main(["do", "send", do["id"], "and option b", "--json"]) == 0
    ops.cancel_decision_order(do["id"])
    with pytest.raises(ops.OpsError, match="jarvis do create"):
        ops.send_decision_message(do["id"], "again")


def test_the_doctor_detects_a_bypassed_guard_and_restores_the_flag(started, store,
                                                                   catalog_file):
    do = _delivered(store, catalog_file)
    store.clear_feature_attention(do["id"])
    found = [v for v in invariants.check_project(store) if v.subject_id == do["id"]]
    assert [(v.invariant, v.repaired) for v in found] == [
        ("INV-DECISION-AWAITING-FLAGGED", True)]
    row = store.get_feature_order(do["id"])
    assert row["needs_attention"] and row["attention_reason"].startswith("decision awaiting")
    assert not [v for v in invariants.check_project(store) if v.subject_id == do["id"]]
    # The user's close carries its record; a bypass does not
    ops.complete_decision_order(do["id"])
    assert not list(invariants.check_decisions_closed_by_user(store))
    other = _decision(store, researcher=None)
    store.update_feature_order(other["id"], status="cancelled", decided_by_user=True)
    found = list(invariants.check_decisions_closed_by_user(store))
    assert [(v.invariant, v.subject_id, v.level) for v in found] == [
        ("INV-DECISION-USER-CLOSED", other["id"], "critical")]


def test_status_attention_lists_an_awaiting_order_without_feature_progress(
        started, store, catalog_file):
    do = _delivered(store, catalog_file)
    for _ in range(2):
        invariants.check_project(store)
        items = [a for a in ops.os_status()["attention"] if a.get("fo_id") == do["id"]]
        assert len(items) == 1
        assert items[0]["reason"] == store.get_feature_order(do["id"])["attention_reason"]
        assert items[0]["decide"] == f"jarvis do show {do['id']}"


def test_nothing_but_the_user_ever_completes_a_decision_order(started, store, catalog_file):
    do = _decision(store, researcher="completed", report=True)
    ops.set_decision_budget(do["id"], 0.01)
    d = _daemon(catalog_file)
    for _ in range(12):
        # Tick 1 runs every `% N == 1` cadence: PR poll, reconcile, landing sweep, schedule
        d.tick_count = 0
        d.tick()
        d.stuck_tick(None)
        d.health_tick(None)
        invariants.check_project(store, repair=True, slow=True)
    row = store.get_feature_order(do["id"])
    assert row["status"] == "awaiting_decision" and row["needs_attention"]
    assert len(_decision_notes(store, do["id"])) == 1
    ops.complete_decision_order(do["id"], note="B")
    row = store.get_feature_order(do["id"])
    assert row["status"] == "completed" and not row["needs_attention"]
    assert store.events_of_kind(do["plan_wo_id"], "decision_closed_by_user")


@pytest.mark.parametrize("route", ["wo_done", "close_out", "budget_raise", "budget_clear",
                                   "hide", "delete"])
def test_the_non_daemon_routes_leave_the_order_open(started, store, catalog_file, route):
    do = _decision(store, researcher="running")
    wo_id = do["plan_wo_id"]
    if route == "wo_done":
        ops.mark_done(wo_id)
    elif route == "close_out":
        ops.close_out(store, store.get_work_order(wo_id), "closed_by_test", why="test")
    elif route == "budget_raise":
        ops.set_decision_budget(do["id"], 50.0)
    elif route == "budget_clear":
        ops.set_decision_budget(do["id"], None)
    elif route == "hide":
        store.set_status(wo_id, "failed")
        ops.hide_work_order(wo_id)
    else:
        ops.delete_work_order(wo_id)
    for _ in range(2):
        _deliver(catalog_file, store)
    row = store.get_feature_order(do["id"])
    assert row["status"] == "planning"
    if route.startswith("budget"):
        assert not row["needs_attention"]  # the researcher is still running
    else:
        assert row["needs_attention"] and len(_decision_notes(store, do["id"])) == 1


# -- §7: the surfaces that read the kind


def test_cost_and_inspect_bill_the_order_plus_its_researcher(started, store):
    do = _decision(store, researcher="running")
    for report in (ops.cost_report(target=do["id"]), ops.inspect_report(do["id"])):
        assert report["scope"] == do["id"]
        assert [u.get("wo_id") or u.get("id") for u in report["units"]] == [
            do["plan_wo_id"]]
    # A mistyped do- id is refused, never read as a project name
    with pytest.raises(ops.OpsError):
        ops._resolve_report_target("do-00000000", None, project_fallback=True)
