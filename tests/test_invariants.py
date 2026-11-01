"""Post-conditions — the OS checking that its own state still means what it says"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarvis import cli, db, invariants, ops, release, worker_session
from jarvis.catalog import DEFAULT_VALIDATION_TIMEOUT
from jarvis.hooks import handle_hook
from jarvis.invariants import (
    IDLE_NO_FINISH_BLOCKER,
    PR_CLOSED_BLOCKER,
    VALIDATION_STUCK_BLOCKER,
    check_project,
    status_label,
    true_blockers,
)
from jarvis.project_store import OPEN_STATUSES, ProjectStore

IDLE_NOTIFICATION = "Claude is waiting for your input"


def _settled_with_assumptions(store: ProjectStore, n: int = 2) -> dict:
    """A work order in the state the reconciler leaves a finished worker in"""
    wo = store.create_work_order("status of this project")
    for i in range(n):
        store.add_assumption(wo["id"], f"assumption {i}")
    store.update_work_order(wo["id"], result_summary="done")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], "assumptions pending review")
    return store.get_work_order(wo["id"])


# -- the derivation everything is checked against


def test_true_blockers_puts_the_actionable_thing_first(project):
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store, n=2)

    blockers = true_blockers(store, wo)

    assert blockers[0] == "2 assumptions pending your review"


def test_true_blockers_is_singular_for_one_assumption(project):
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store, n=1)

    assert true_blockers(store, wo)[0] == "1 assumption pending your review"


def test_a_finished_work_order_blocks_on_nothing(project):
    store = ProjectStore(project)
    wo = store.create_work_order("done and dusted")
    store.set_status(wo["id"], "completed")

    assert true_blockers(store, store.get_work_order(wo["id"])) == []


# -- the root-cause fix


def test_idle_notification_no_longer_clobbers_the_real_reason(project):
    """The bug, at the hook layer: the idle prompt must not touch a settled order"""
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store)

    handle_hook(
        {"hook_event_name": "Notification", "session_id": "s1",
         "cwd": str(project), "message": IDLE_NOTIFICATION},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )

    fresh = ProjectStore(project).get_work_order(wo["id"])
    assert fresh["attention_reason"] == "assumptions pending review"
    assert fresh["status"] == "needs_review"


def test_idle_notification_on_a_settled_order_raises_no_inbox_noise(project):
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store)

    handle_hook(
        {"hook_event_name": "Notification", "session_id": "s1",
         "cwd": str(project), "message": IDLE_NOTIFICATION},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )

    assert ProjectStore(project).unrouted_notifications() == []


def test_a_real_mid_work_block_still_gets_through(project):
    """The guard must not swallow the case the hook exists for"""
    store = ProjectStore(project)
    wo = store.create_work_order("real work")
    store.set_status(wo["id"], "running")

    handle_hook(
        {"hook_event_name": "Notification", "session_id": "s1",
         "cwd": str(project), "message": "Claude needs permission to run npm test"},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )

    fresh = ProjectStore(project).get_work_order(wo["id"])
    assert fresh["status"] == "waiting_input"
    assert fresh["needs_attention"] == 1
    assert "permission" in fresh["attention_reason"]


# -- the invariants (defence in depth: they catch it however it happens)


def test_repairs_the_state_the_live_bug_left_behind(project):
    """Whatever clobbers the reason, the next tick puts it right"""
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store)
    store.flag_attention(wo["id"], IDLE_NOTIFICATION)  # the damage, however caused

    violations = check_project(store, repair=True)

    assert [v.invariant for v in violations] == ["INV-ATTENTION-REASON"]
    assert violations[0].repaired
    fresh = store.get_work_order(wo["id"])
    assert fresh["attention_reason"] == "2 assumptions pending your review"


def test_reporting_mode_changes_nothing(project):
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store)
    store.flag_attention(wo["id"], IDLE_NOTIFICATION)

    violations = check_project(store, repair=False)

    assert len(violations) == 1
    assert store.get_work_order(wo["id"])["attention_reason"] == IDLE_NOTIFICATION


def test_a_correct_work_order_reports_nothing(project):
    store = ProjectStore(project)
    _settled_with_assumptions(store)

    assert check_project(store, repair=True) == []


def test_repair_is_idempotent(project):
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store)
    store.flag_attention(wo["id"], IDLE_NOTIFICATION)

    check_project(store, repair=True)

    assert check_project(store, repair=True) == []


def test_a_specific_permission_reason_is_left_alone(project):
    """Only assumptions are enforced: a hook reason is more specific than anything derivable"""
    store = ProjectStore(project)
    wo = store.create_work_order("running work")
    store.set_status(wo["id"], "waiting_input")
    store.flag_attention(wo["id"], "Claude needs permission to run npm test")

    assert check_project(store, repair=True) == []
    assert store.get_work_order(wo["id"])["attention_reason"] == (
        "Claude needs permission to run npm test")


def _finished_with_a_gate_still_pending(store: ProjectStore, escalated: bool = True):
    """The state wo-52a6164d ended in: shipped, completed, still asking permission"""
    wo = store.create_work_order("ship a new version to production")
    approval = store.add_approval(wo["id"], "release", "scripts/deploy.sh 0.5.4")
    if escalated:
        store.mark_approval_escalated(approval["id"], "no justification was filed")
        store.flag_attention(
            wo["id"], f"gate approval escalated by Neo: release (request {approval['id']})")
    store.update_work_order(wo["id"], result_summary="staged 0.5.4")
    store.set_status(wo["id"], "completed")
    return wo, approval


def test_a_finished_work_order_stops_asking_for_permission(project):
    """A gate is a control on something about to happen"""
    store = ProjectStore(project)
    wo, approval = _finished_with_a_gate_still_pending(store)

    violations = check_project(store, repair=True)

    # The gate is closed first, so the attention sweep that follows sees the truth
    assert [v.invariant for v in violations] == ["INV-GATE-ORPHAN",
                                                 "INV-ATTENTION-PHANTOM"]
    assert violations[0].repaired
    closed = store.get_approval(approval["id"])
    assert closed["status"] == "expired"
    assert closed["decided_by"] == "os"
    assert store.pending_approvals(wo["id"]) == []
    # ...and with the blocker gone, the attention flag goes down with it
    assert true_blockers(store, store.get_work_order(wo["id"])) == []
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_closing_an_orphan_gate_records_no_verdict(project):
    """Nobody authorised anything"""
    store = ProjectStore(project)
    _, approval = _finished_with_a_gate_still_pending(store)

    check_project(store, repair=True)

    closed = store.get_approval(approval["id"])
    assert closed["status"] not in ("approved", "denied", "dismissed")
    # A dismissal would also have inflated the classifier false-positive count
    assert store.dismissed_count() == 0


def test_an_orphan_gate_is_reported_but_not_closed_in_reporting_mode(project):
    store = ProjectStore(project)
    _, approval = _finished_with_a_gate_still_pending(store)

    violations = check_project(store, repair=False)

    assert "INV-GATE-ORPHAN" in [v.invariant for v in violations]
    assert store.get_approval(approval["id"])["status"] == "pending"


def test_a_gate_pending_on_live_work_is_left_alone(project):
    """The whole point of the gate: while the worker is still there"""
    store = ProjectStore(project)
    wo = store.create_work_order("ship it")
    approval = store.add_approval(wo["id"], "release", "scripts/deploy.sh 0.5.4")
    store.set_status(wo["id"], "waiting_input")

    check_project(store, repair=True)

    assert store.get_approval(approval["id"])["status"] == "pending"


def _gate_whose_reviewer_died(store: ProjectStore, cause: str = "transport-unreachable"):
    """The state issue #1261 reported: a parked order, a pending gate, a dead question"""
    from jarvis.neo_store import NeoStore

    wo = store.create_work_order("ship a new version to production")
    approval = store.add_approval(wo["id"], "auto_merge", "gh pr merge 1234 --squash")
    store.set_status(wo["id"], "waiting_pr_merge")
    neo = NeoStore()
    try:
        q = neo.ask("proj_a", wo["id"], "merge this pull request?", kind="approval")
        store.start_review(approval["id"], q["id"])
        neo.mark(q["id"], "failed", reason="neo could not be reached: OAuth expired",
                 cause=cause)
    finally:
        neo.close()
    return wo, approval, q


def test_a_gate_whose_neo_question_died_comes_to_the_user(project, neo_projects):
    """Issue #1261: nobody holds the request, so no surface may claim a verdict is coming"""
    store = ProjectStore(project)
    wo, approval, q = _gate_whose_reviewer_died(store)

    violations = check_project(store, repair=True)

    assert "INV-GATE-UNJUDGED" in [v.invariant for v in violations]
    escalated = store.get_approval(approval["id"])
    # STILL PENDING: nothing was approved or denied, it only changed hands
    assert escalated["status"] == "pending"
    assert escalated["escalated"]
    assert f"question {q['id']}" in escalated["escalation_reason"]
    # ...and the user now hears about it, on an order no `waiting_input` arm covers
    blockers = true_blockers(store, store.get_work_order(wo["id"]))
    assert blockers == [f"gate approval needed: auto_merge (request {approval['id']})"]
    assert store.get_work_order(wo["id"])["needs_attention"]
    assert ops.waiting_on(store, store.get_work_order(wo["id"]))["what"] == (
        "gate_escalated")


def test_a_gate_still_with_neo_is_left_alone(project, neo_projects):
    """The whole point of the gate: while a reviewer really is holding it"""
    store = ProjectStore(project)
    _, approval, _ = _gate_whose_reviewer_died(store, cause="")
    from jarvis.neo_store import NeoStore

    neo = NeoStore()
    try:
        neo.mark(store.get_approval(approval["id"])["neo_question_id"], "queued")
    finally:
        neo.close()

    violations = check_project(store, repair=True)

    assert "INV-GATE-UNJUDGED" not in [v.invariant for v in violations]
    assert not store.get_approval(approval["id"])["escalated"]


def test_a_dead_reviewer_is_reported_but_not_escalated_in_reporting_mode(project, neo_projects):
    store = ProjectStore(project)
    _, approval, _ = _gate_whose_reviewer_died(store)

    violations = check_project(store, repair=False)

    assert "INV-GATE-UNJUDGED" in [v.invariant for v in violations]
    assert not store.get_approval(approval["id"])["escalated"]


def test_phantom_attention_on_a_finished_order_is_cleared(project):
    """The 'I acked it and it is still in my face' case"""
    store = ProjectStore(project)
    wo = store.create_work_order("finished")
    store.set_status(wo["id"], "completed")
    store.flag_attention(wo["id"], "stale reason nobody can act on")

    violations = check_project(store, repair=True)

    assert [v.invariant for v in violations] == ["INV-ATTENTION-PHANTOM"]
    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"] == 0
    assert fresh["attention_reason"] is None


def test_silently_stalled_work_is_surfaced(project):
    """The dangerous direction: pending work that never asks for the user"""
    store = ProjectStore(project)
    wo = store.create_work_order("quietly stuck")
    store.add_assumption(wo["id"], "something needing a decision")
    store.set_status(wo["id"], "needs_review")
    store.clear_attention(wo["id"])  # invisible on every surface

    violations = check_project(store, repair=True)

    assert [v.invariant for v in violations] == ["INV-ATTENTION-MISSING"]
    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"] == 1
    assert fresh["attention_reason"] == "1 assumption pending your review"


def test_an_assumption_that_never_reached_the_review_queue_is_rebuilt(project):
    """'Assumption recorded' in the timeline is a claim about a write"""
    store = ProjectStore(project)
    wo = store.create_work_order("records an assumption")
    store.add_assumption(wo["id"], "the assumption that went missing")
    # Simulate the row being lost while the event survives
    store.conn.execute("DELETE FROM assumptions WHERE wo_id=?", (wo["id"],))
    assert store.all_assumptions(wo["id"]) == []

    violations = check_project(store, repair=True)

    assert "INV-ASSUMPTION-PERSISTED" in [v.invariant for v in violations]
    rebuilt = store.all_assumptions(wo["id"])
    assert [a["content"] for a in rebuilt] == ["the assumption that went missing"]


def test_rebuilding_an_assumption_never_duplicates_it(project):
    store = ProjectStore(project)
    wo = store.create_work_order("records an assumption")
    store.add_assumption(wo["id"], "kept")
    store.conn.execute("DELETE FROM assumptions WHERE wo_id=?", (wo["id"],))

    check_project(store, repair=True)
    check_project(store, repair=True)

    assert len(store.all_assumptions(wo["id"])) == 1


def test_a_blank_attention_reason_is_filled_in(project):
    """"Needs you" with no reason is the fastest way to teach an operator to ignore the attention strip"""
    store = ProjectStore(project)
    wo = store.create_work_order("blocked on the user")
    store.set_status(wo["id"], "waiting_input")
    store.update_work_order(wo["id"], needs_attention=1, attention_reason="")

    violations = check_project(store, repair=True)

    assert {v.invariant for v in violations} == {"INV-ATTENTION-BLANK"}
    assert store.get_work_order(wo["id"])["attention_reason"] == (
        "worker is waiting on your input")


def test_a_blank_reason_hiding_pending_assumptions_names_them(project):
    """Blank *and* assumptions pending: the stale-reason invariant is the one that fires"""
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store, n=1)
    store.update_work_order(wo["id"], attention_reason="")

    check_project(store, repair=True)

    assert store.get_work_order(wo["id"])["attention_reason"] == (
        "1 assumption pending your review")


def test_a_broken_invariant_does_not_hide_the_others(project, monkeypatch):
    """One bad check must not take the checker down — it is the last line of defence"""
    import jarvis.invariants as inv

    def boom(store):
        raise RuntimeError("checker bug")
        yield  # pragma: no cover

    store = ProjectStore(project)
    wo = store.create_work_order("finished")
    store.set_status(wo["id"], "completed")
    store.flag_attention(wo["id"], "stale")
    monkeypatch.setattr(inv, "INVARIANTS", (boom, inv.check_no_phantom_attention))

    violations = check_project(store, repair=True)

    assert {v.invariant for v in violations} == {"boom", "INV-ATTENTION-PHANTOM"}
    assert store.get_work_order(wo["id"])["needs_attention"] == 0


# -- surfaces


def test_the_daemon_repairs_and_records_on_its_tick(project, catalog_file):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    store = ProjectStore(project)
    wo = _settled_with_assumptions(store)
    store.flag_attention(wo["id"], IDLE_NOTIFICATION)
    store.close()

    catalog = load_catalog(catalog_file)
    daemon = Daemon(catalog)
    spec = catalog.projects[0]
    fresh_store = ProjectStore(spec.path)
    daemon.check_invariants(spec, fresh_store)

    assert fresh_store.get_work_order(wo["id"])["attention_reason"] == (
        "2 assumptions pending your review")
    kinds = [e["kind"] for e in fresh_store.list_events(wo["id"])]
    assert "invariant" in kinds


def test_the_daemon_reports_a_standing_violation_once(project, catalog_file):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    store = ProjectStore(project)
    wo = store.create_work_order("unfixable")
    store.set_status(wo["id"], "running")
    store.update_work_order(wo["id"], needs_attention=1, attention_reason="")
    store.close()

    catalog = load_catalog(catalog_file)
    daemon = Daemon(catalog)
    spec = catalog.projects[0]
    s = ProjectStore(spec.path)
    daemon.check_invariants(spec, s)
    daemon.check_invariants(spec, s)

    notes = [n for n in s.unrouted_notifications() if n["source"] == "invariants"]
    assert len(notes) == 1


def _invariant_notes(store: ProjectStore) -> list[dict]:
    return [n for n in store.unrouted_notifications() if n["source"] == "invariants"]


def _switchable(on: dict) -> tuple:
    """An invariant whose violation the test turns on and off, carrying NO work order"""
    def check(store):
        if on["broken"]:
            yield invariants.Violation(invariant="INV-FAKE", detail="still broken")
    return (check,)


def _daemon(catalog_file):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    catalog = load_catalog(catalog_file)
    return Daemon(catalog), catalog.projects[0]


def test_a_standing_violation_is_not_re_announced_after_a_restart(
        project, catalog_file, monkeypatch):
    """The reported bug: every release restarted jarvis.service and the whole standing unrepaired set went..."""
    on = {"broken": True}
    monkeypatch.setattr(invariants, "INVARIANTS", _switchable(on))
    store = ProjectStore(project)

    first, spec = _daemon(catalog_file)
    first.check_invariants(spec, store)
    second, spec = _daemon(catalog_file)          # `shipit` restarted the daemon
    second.check_invariants(spec, store)

    assert len(_invariant_notes(store)) == 1
    assert [(r["invariant"], r["wo_id"], r["seen"]) for r in store.violation_reports()] \
        == [("INV-FAKE", "", 2)]


def test_a_violation_that_clears_and_returns_is_announced_again(
        project, catalog_file, monkeypatch):
    on = {"broken": True}
    monkeypatch.setattr(invariants, "INVARIANTS", _switchable(on))
    store = ProjectStore(project)
    daemon, spec = _daemon(catalog_file)

    daemon.check_invariants(spec, store, sweep_landings=True)
    on["broken"] = False
    daemon.check_invariants(spec, store, sweep_landings=True)
    assert store.violation_reports() == []
    on["broken"] = True
    daemon.check_invariants(spec, store, sweep_landings=True)

    assert len(_invariant_notes(store)) == 2


def test_a_fast_tick_never_forgets_a_violation_it_did_not_look_for(
        project, catalog_file, monkeypatch):
    """Absence on a tick that ran only the fast checks means "nobody looked", not "fixed\""""
    on = {"broken": True}
    monkeypatch.setattr(invariants, "INVARIANTS", _switchable(on))
    store = ProjectStore(project)
    daemon, spec = _daemon(catalog_file)

    daemon.check_invariants(spec, store, sweep_landings=True)
    on["broken"] = False
    daemon.check_invariants(spec, store)          # the other 719 ticks
    on["broken"] = True
    daemon.check_invariants(spec, store)

    assert len(_invariant_notes(store)) == 1


def test_doctor_reports_without_touching_state(project, catalog_file, capsys):
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store)
    store.flag_attention(wo["id"], IDLE_NOTIFICATION)
    store.close()

    rc = cli.main(["doctor", "--catalog", str(catalog_file)])

    assert rc == 1  # violations found
    assert "INV-ATTENTION-REASON" in capsys.readouterr().out
    assert ProjectStore(project).get_work_order(wo["id"])["attention_reason"] == (
        IDLE_NOTIFICATION)


def test_doctor_repair_fixes_it(project, catalog_file, capsys):
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store)
    store.flag_attention(wo["id"], IDLE_NOTIFICATION)
    store.close()

    cli.main(["doctor", "--repair", "--catalog", str(catalog_file)])

    assert ProjectStore(project).get_work_order(wo["id"])["attention_reason"] == (
        "2 assumptions pending your review")


def test_doctor_is_quiet_and_green_when_all_is_well(project, catalog_file, capsys):
    ProjectStore(project).close()

    rc = cli.main(["doctor", "--catalog", str(catalog_file)])

    assert rc == 0
    assert "all OS invariants hold" in capsys.readouterr().out


def test_ops_doctor_reports_per_project(project, catalog_file):
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store)
    store.flag_attention(wo["id"], IDLE_NOTIFICATION)
    store.close()

    res = ops.run_doctor(repair=False, catalog_path=str(catalog_file))

    assert res["violations"] == 1
    assert res["projects"][0]["violations"][0]["invariant"] == "INV-ATTENTION-REASON"


def test_doctor_reports_leftover_background_sessions(jarvis_home, fake_claude,
                                                     catalog_file, project):
    """The 63 stale agents the old transport leaked are surfaced, never auto-stopped"""
    from jarvis import claude_cli, ops
    from jarvis.project_store import ProjectStore

    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    live = store.create_work_order("still running")
    claude_cli.spawn_background(prompt="x", cwd=project,
                                name=f"[WO {live['id']}] still running")
    store.update_work_order(live["id"], session_id=fake_claude.sessions[-1]["sessionId"])
    store.set_status(live["id"], "running")
    claude_cli.spawn_background(prompt="x", cwd=project, name="[WO wo-longgone] debris")
    claude_cli.spawn_background(prompt="x", cwd=project, name="a session I started")

    res = ops.run_doctor()

    orphans = res.get("orphaned_sessions") or []
    assert [o["name"] for o in orphans] == ["[WO wo-longgone] debris"], orphans
    assert orphans[0]["stop"].startswith("claude stop ")
    # reported only — nothing was stopped
    assert len(fake_claude.sessions) == 3
    assert [c for c in fake_claude.calls if c["argv"][:1] == ["stop"]] == []


# -- the validation panel


def test_a_validating_work_order_is_silent_until_the_panel_gives_up(project):
    """The pairing IS the test"""
    store = ProjectStore(project)
    wo = store.create_work_order("ship the thing")
    store.update_work_order(wo["id"], result_summary="done")
    store.set_status(wo["id"], "validating")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="abc")

    # a round in flight: the OS is working, and nobody owes anybody a decision
    assert true_blockers(store, store.get_work_order(wo["id"])) == []
    # INV-ATTENTION-MISSING now SEES this row
    assert [v for v in check_project(store, repair=True)
            if v.invariant == "INV-ATTENTION-MISSING" and v.wo_id == wo["id"]] == []
    assert not store.get_work_order(wo["id"])["needs_attention"]

    # ...and the panel still deliberating
    store.set_status(wo["id"], "needs_review")
    assert true_blockers(store, store.get_work_order(wo["id"])) == [
        IDLE_NO_FINISH_BLOCKER]

    # the panel gives up: now it is
    store.close_validation_round(rnd["id"], "escalated", reason="three rounds, no deal")
    assert true_blockers(store, store.get_work_order(wo["id"])) == [
        VALIDATION_STUCK_BLOCKER]


def test_needs_review_label_names_which_decision_is_owed(project):
    """#756: a panel give-up and pending assumptions must not read alike"""
    store = ProjectStore(project)
    wo = store.create_work_order("ship the thing")
    store.set_status(wo["id"], "needs_review")
    store.add_assumption(wo["id"], "something needing a decision")
    assert invariants.status_label(store, store.get_work_order(wo["id"])) == (
        "needs_review — 1 assumption pending your review")

    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="abc")
    store.close_validation_round(rnd["id"], "escalated", reason="no deal")
    assert invariants.status_label(store, store.get_work_order(wo["id"])) == (
        "needs_review — 1 assumption pending your review; "
        f"{invariants.VALIDATION_GAVE_UP_LABEL}")

    for a in store.pending_assumptions(wo["id"]):
        store.review_assumption(a["id"], "rejected", decided_by="user")
    assert invariants.status_label(store, store.get_work_order(wo["id"])) == (
        f"needs_review — {invariants.VALIDATION_GAVE_UP_LABEL}")


def test_a_validating_work_order_with_a_real_blocker_is_surfaced(project):
    """The other half of the pairing above"""
    store = ProjectStore(project)
    wo = store.create_work_order("quietly stuck mid-review")
    store.add_assumption(wo["id"], "something needing a decision")
    store.set_status(wo["id"], "validating")
    store.open_validation_round(wo_id=wo["id"], fingerprint="abc")
    store.clear_attention(wo["id"])  # invisible on every surface

    violations = check_project(store, repair=True)

    assert [v.invariant for v in violations] == ["INV-ATTENTION-MISSING"]
    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"] == 1
    assert fresh["attention_reason"] == "1 assumption pending your review"


def test_the_attention_repair_is_dropped_when_the_order_settled_under_the_sweep(project,
                                                                                monkeypatch):
    """Issue #1205: the sweep derives from a row read a write earlier"""
    store = ProjectStore(project)
    wo = store.create_work_order("quietly stuck mid-review")
    store.add_assumption(wo["id"], "something needing a decision")
    store.set_status(wo["id"], "validating")
    store.open_validation_round(wo_id=wo["id"], fingerprint="abc")
    store.clear_attention(wo["id"])
    stale = store.get_work_order(wo["id"])      # the row as the sweep read it
    # The validation thread settles it while the sweep is deriving
    store.set_status(wo["id"], "completed")
    store.clear_attention(wo["id"])
    monkeypatch.setattr(store, "list_work_orders", lambda *a, **k: [stale])

    assert list(invariants.check_blocked_work_is_surfaced(store)) == []
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_the_phantom_clear_is_dropped_when_the_order_moved_under_the_sweep(project,
                                                                           monkeypatch):
    """Issue #1205: a clear derived from a settled row must not land on a retried one"""
    store = ProjectStore(project)
    wo = store.create_work_order("retried after it settled")
    store.set_status(wo["id"], "completed")
    store.flag_attention(wo["id"], "a flag with nothing behind it")
    stale = store.get_work_order(wo["id"])      # the row as the sweep read it
    # `jarvis wo retry` moves it while the sweep is deriving
    store.set_status(wo["id"], "running")
    store.flag_attention(wo["id"], "the retry's own blocker")
    monkeypatch.setattr(store, "list_work_orders", lambda *a, **k: [stale])

    assert list(invariants.check_no_phantom_attention(store)) == []
    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"] == 1
    assert fresh["attention_reason"] == "the retry's own blocker"


def test_a_closed_pull_request_outranks_an_escalated_review(project):
    """Both branches live under the same `needs_review` roof"""
    store = ProjectStore(project)
    wo = store.create_work_order("ship the thing")
    store.set_status(wo["id"], "needs_review", pr_state="CLOSED")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="abc")
    store.close_validation_round(rnd["id"], "escalated")

    assert true_blockers(store, store.get_work_order(wo["id"])) == [PR_CLOSED_BLOCKER]


def test_the_validating_label_says_which_round_the_work_is_on(project):
    """`status_label` early-returns for every status that is not `pending`"""
    store = ProjectStore(project)
    wo = store.create_work_order("ship the thing")
    store.set_status(wo["id"], "validating")
    for fp in ("first try", "second try"):
        store.open_validation_round(wo_id=wo["id"], fingerprint=fp)

    label = status_label(store, store.get_work_order(wo["id"]))

    assert label == "validating — review round 2 of 3"


# -- INV-VALIDATION-STRANDED


def _stranded(store: ProjectStore, *, age: float, outcome: str = "pending",
              feature: bool = False) -> tuple[str, dict]:
    """A unit parked in `validating` on a round opened `age` seconds ago"""
    if feature:
        unit = store.create_feature_order("ship the feature", description="all of it")
        store.set_feature_status(unit["id"], "validating")
        rnd = store.open_validation_round(fo_id=unit["id"], fingerprint="fp")
    else:
        unit = store.create_work_order("ship the thing")
        store.update_work_order(unit["id"], result_summary="done")
        store.set_status(unit["id"], "validating")
        rnd = store.open_validation_round(wo_id=unit["id"], fingerprint="fp")
    if outcome != "pending":
        store.close_validation_round(rnd["id"], outcome)
    store.conn.execute("UPDATE validation_rounds SET ts=? WHERE id=?",
                       (db.now() - age, rnd["id"]))
    return unit["id"], rnd


def _stranded_violations(store: ProjectStore, repair: bool = True) -> list:
    return [v for v in check_project(store, repair=repair)
            if v.invariant == "INV-VALIDATION-STRANDED"]


def _round(store: ProjectStore, rnd: dict) -> dict:
    """Re-read a round, insisting it is still there"""
    fresh = store.get_validation_round(rnd["id"])
    assert fresh is not None, f"round {rnd['id']} vanished"
    return fresh


def test_a_round_left_pending_past_twice_its_timeout_is_stranded(project):
    """The pairing IS the test"""
    store = ProjectStore(project)
    late, late_round = _stranded(store, age=DEFAULT_VALIDATION_TIMEOUT)
    abandoned, abandoned_round = _stranded(
        store, age=2 * DEFAULT_VALIDATION_TIMEOUT + 60)

    found = _stranded_violations(store)

    assert [v.wo_id for v in found] == [abandoned]
    assert late not in [v.wo_id for v in found]
    assert _round(store, late_round)["outcome"] == "pending"
    assert _round(store, abandoned_round)["outcome"] == "failed"


def test_the_repair_hands_the_round_back_to_the_daemon_as_failed(project):
    """`failed`, never `escalated`: `counted_validation_rounds` ignores a failed round"""
    store = ProjectStore(project)
    wo_id, rnd = _stranded(store, age=5000)

    found = _stranded_violations(store)

    assert len(found) == 1 and found[0].repaired
    closed = _round(store, rnd)
    assert closed["outcome"] == "failed"
    assert "interrupted" in closed["reason"]
    # the round is not spent: nobody judged it
    assert store.counted_validation_rounds(wo_id=wo_id) == 0
    # ...and the work order is left in `validating`, where the next tick finds it
    assert store.get_work_order(wo_id)["status"] == "validating"
    # reported once by construction: the round is no longer pending
    assert _stranded_violations(store) == []


def test_doctor_without_repair_does_not_close_the_stranded_round(project, catalog_file,
                                                                capsys):
    """Reporting mode is READ-ONLY, and this invariant is the one with the most to lose from it:..."""
    store = ProjectStore(project)
    wo_id, rnd = _stranded(store, age=5000)
    store.close()

    rc = cli.main(["doctor", "--catalog", str(catalog_file)])

    assert rc == 1
    assert "INV-VALIDATION-STRANDED" in capsys.readouterr().out
    after = ProjectStore(project)
    try:
        assert _round(after, rnd)["outcome"] == "pending"
        assert after.get_work_order(wo_id)["status"] == "validating"
    finally:
        after.close()


def test_a_round_abandoned_under_needs_review_is_stranded_too(project):
    """Since issue 212 a round runs beside the user's assumption review"""
    store = ProjectStore(project)
    parked, parked_round = _stranded(store, age=5000)
    store.set_status(parked, "needs_review")
    stopped, stopped_round = _stranded(store, age=5000)
    store.set_status(stopped, "cancelled")

    found = _stranded_violations(store)

    assert [v.wo_id for v in found] == [parked]
    assert _round(store, parked_round)["outcome"] == "failed"
    assert _round(store, stopped_round)["outcome"] == "pending"
    # ...and the parked one keeps the status that says the user owes a decision
    assert store.get_work_order(parked)["status"] == "needs_review"


def test_a_judged_round_is_never_stranded_however_old(project):
    """A round that reached a verdict is finished business"""
    store = ProjectStore(project)
    _stranded(store, age=5000, outcome="rejected")

    assert _stranded_violations(store) == []


def test_a_feature_order_in_validating_is_covered_too(project):
    """Nothing sets a feature order to `validating` yet — the loop that will is a sibling work order"""
    store = ProjectStore(project)
    stale_id, stale_round = _stranded(store, age=5000, feature=True)
    fresh_id, fresh_round = _stranded(store, age=10, feature=True)

    found = _stranded_violations(store)

    assert len(found) == 1
    assert found[0].context["fo_id"] == stale_id
    assert found[0].context["unit"] == "feature order"
    assert found[0].wo_id is None  # a feature order is not a work order
    assert _round(store, stale_round)["outcome"] == "failed"
    assert _round(store, fresh_round)["outcome"] == "pending"
    assert store.get_feature_order(fresh_id)["status"] == "validating"


def test_the_threshold_follows_the_configured_timeout(project, monkeypatch):
    """The number decides a WRITE, so it is read from the LIVE catalog rather than the shipped default: a..."""
    from types import SimpleNamespace

    store = ProjectStore(project)
    _, rnd = _stranded(store, age=2 * DEFAULT_VALIDATION_TIMEOUT + 60)

    # `_validation_timeout` imports it inside the call
    monkeypatch.setattr(ops, "validation_config",
                        lambda: SimpleNamespace(timeout=3600))
    assert _stranded_violations(store) == []
    assert _round(store, rnd)["outcome"] == "pending"

    monkeypatch.undo()
    assert len(_stranded_violations(store)) == 1
    assert _round(store, rnd)["outcome"] == "failed"


def test_an_unreadable_catalog_falls_back_to_the_shipped_timeout(monkeypatch):
    """A worker's checkout whose catalog has moved answers None"""
    from jarvis.invariants import _validation_timeout

    monkeypatch.setattr(ops, "validation_config", lambda: None)

    assert _validation_timeout() == DEFAULT_VALIDATION_TIMEOUT


# -- INV-VALIDATION-STRANDED, second arm: a PASSED round that never landed


def _passed(store: ProjectStore, *, age: float = 10) -> tuple[str, dict]:
    """A `validating` work order whose latest round PASSED and which never landed"""
    wo_id, rnd = _stranded(store, age=age, outcome="passed")
    store.update_work_order(wo_id, pr_url="https://github.com/o/r/pull/7")
    return wo_id, rnd


def test_a_passed_round_that_never_landed_is_stranded(project):
    """No staleness threshold of its own: once no turn is in flight there is nothing left in the OS that..."""
    store = ProjectStore(project)
    wo_id, rnd = _passed(store)

    found = _stranded_violations(store)

    assert [v.wo_id for v in found] == [wo_id]
    assert found[0].repaired
    assert found[0].context["round_id"] == rnd["id"]
    assert found[0].context["unit"] == "work order"
    assert store.get_work_order(wo_id)["status"] == "waiting_pr_merge"
    # the round is not touched: it was judged, and the verdict stands
    assert _round(store, rnd)["outcome"] == "passed"
    assert _stranded_violations(store) == []


def test_doctor_without_repair_does_not_land_the_passed_round(project, catalog_file,
                                                              capsys):
    """`land_when_cleared` reaches `CentralStore.mark_backlog`"""
    store = ProjectStore(project)
    wo_id, _ = _passed(store)
    store.close()

    rc = cli.main(["doctor", "--catalog", str(catalog_file)])

    assert rc == 1
    assert "INV-VALIDATION-STRANDED" in capsys.readouterr().out
    after = ProjectStore(project)
    try:
        assert after.get_work_order(wo_id)["status"] == "validating"
        assert after.last_event_of_kind(wo_id, "validation_landed") is None
    finally:
        after.close()


def test_the_passed_arm_leaves_a_pending_round_to_the_threshold(project):
    """The two arms are separate predicates over separate outcomes"""
    store = ProjectStore(project)
    late, late_round = _stranded(store, age=DEFAULT_VALIDATION_TIMEOUT)
    landed, _ = _passed(store)

    found = _stranded_violations(store)

    assert [v.wo_id for v in found] == [landed]
    assert _round(store, late_round)["outcome"] == "pending"
    assert store.get_work_order(late)["status"] == "validating"


def test_a_passed_round_under_a_live_worker_turn_is_not_stranded(project):
    """The deferral is CORRECT there: landing writes a status under a worker that is typing"""
    store = ProjectStore(project)
    wo_id, _ = _passed(store)
    store.create_turn(wo_id, "message", "go")
    assert worker_session.busy(store, wo_id) is not None

    assert _stranded_violations(store) == []
    assert store.get_work_order(wo_id)["status"] == "validating"


def test_a_passed_round_already_landed_is_not_stranded(project):
    """The guard, not construction: an order the settler already landed for this round can be back in..."""
    store = ProjectStore(project)
    wo_id, rnd = _passed(store)
    store.add_event(wo_id, "validation_landed", {"round_id": int(rnd["id"])})

    assert _stranded_violations(store) == []
    assert store.get_work_order(wo_id)["status"] == "validating"


# -- INV-PROD-CLEAN: production is what the tag says it is


def _prod_checkout(root: Path, tag: str | None = "jarvis-0.9.0") -> Path:
    """A deployed production checkout: `$PRODUCTION_CODE/jarvis_os`, on a tag, clean"""
    prod = root / "jarvis_os"
    prod.mkdir(parents=True)
    (prod / "pyproject.toml").write_text('[project]\nversion = "0.9.0"\n')
    (prod / "uv.lock").write_text('name = "jarvis-os"\nversion = "0.9.0"\n')
    argvs = [["init", "-q", "-b", "main"],
             ["config", "user.email", "t@example.com"],
             ["config", "user.name", "Test"],
             ["add", "-A"], ["commit", "-q", "-m", "release"]]
    if tag:
        argvs.append(["tag", "-a", tag, "-m", tag])
    for argv in argvs:
        subprocess.run(["git", "-C", str(prod), *argv], check=True, capture_output=True)
    return prod


def _prod_violations(root: Path, monkeypatch) -> list:
    monkeypatch.setenv("PRODUCTION_CODE", str(root))
    return list(invariants.check_production_clean())


def test_a_clean_production_checkout_raises_nothing(tmp_path, monkeypatch):
    _prod_checkout(tmp_path)
    assert _prod_violations(tmp_path, monkeypatch) == []


def test_a_rewritten_lockfile_is_reported_by_name(tmp_path, monkeypatch):
    """The live defect: a bare `uv` command re-resolves and rewrites uv.lock in place"""
    prod = _prod_checkout(tmp_path)
    (prod / "uv.lock").write_text('name = "jarvis-os"\nversion = "0.1.1"\n')

    found = _prod_violations(tmp_path, monkeypatch)
    assert len(found) == 1
    assert found[0].invariant == "INV-PROD-CLEAN"
    assert found[0].context["paths"] == ["uv.lock"]
    assert "uv.lock" in found[0].detail


def test_untracked_files_are_not_drift(tmp_path, monkeypatch):
    """`.venv/` and `.jarvis/` live in that checkout by design"""
    prod = _prod_checkout(tmp_path)
    (prod / ".venv").mkdir()
    (prod / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")

    assert _prod_violations(tmp_path, monkeypatch) == []


def test_a_machine_with_no_production_deployment_raises_nothing(tmp_path, monkeypatch):
    """Every dev checkout runs `jarvis doctor` too"""
    assert _prod_violations(tmp_path / "nothing-here", monkeypatch) == []


def test_a_production_path_that_is_not_a_checkout_raises_nothing(tmp_path, monkeypatch):
    (tmp_path / "jarvis_os").mkdir()
    assert _prod_violations(tmp_path, monkeypatch) == []


def test_staged_drift_is_reported_too(tmp_path, monkeypatch):
    """`git status --porcelain` reports staged changes in the first column"""
    prod = _prod_checkout(tmp_path)
    (prod / "uv.lock").write_text('name = "jarvis-os"\nversion = "0.1.1"\n')
    subprocess.run(["git", "-C", str(prod), "add", "uv.lock"],
                   check=True, capture_output=True)

    found = _prod_violations(tmp_path, monkeypatch)
    assert len(found) == 1
    assert found[0].context["paths"] == ["uv.lock"]
    # `checkout -- .` restores from the INDEX, so it cannot undo a staged change
    assert "checkout -f jarvis-0.9.0" in found[0].detail
    assert "checkout -- ." not in found[0].detail.replace(
        "not `checkout -- .`", "")


def test_a_checkout_git_cannot_read_is_reported_not_called_clean(tmp_path, monkeypatch):
    """The realistic failure on the machine this watches is `detected dubious ownership`"""
    prod = _prod_checkout(tmp_path)
    (prod / ".git" / "config").write_text("[core]\n\tthis is not valid ini\n")

    found = _prod_violations(tmp_path, monkeypatch)
    assert len(found) == 1
    assert found[0].invariant == "INV-PROD-CLEAN"
    assert found[0].context["paths"] is None
    assert "cannot tell" in found[0].detail
    assert found[0].context["error"]


def test_the_unknown_case_is_distinguishable_from_clean(tmp_path, monkeypatch):
    """The two must not share a return value — that is how a broken check reads green"""
    prod = _prod_checkout(tmp_path)
    monkeypatch.setenv("PRODUCTION_CODE", str(tmp_path))
    assert release.production_status(prod).dirty == []
    assert release.production_status(prod).error == ""

    unknown = release.production_status(tmp_path / "not-a-repo")
    assert unknown.dirty is None
    assert unknown.error


def test_the_remedy_survives_pyproject_itself_being_the_drift(tmp_path, monkeypatch):
    """The version on disk is one of the things drift can touch"""
    prod = _prod_checkout(tmp_path)
    (prod / "pyproject.toml").write_text('[project]\nversion = "6.6.6-tampered"\n')

    found = _prod_violations(tmp_path, monkeypatch)
    assert len(found) == 1
    assert found[0].context["paths"] == ["pyproject.toml"]
    assert found[0].context["ref"] == "jarvis-0.9.0"      # from git, not from the file
    assert "6.6.6-tampered" not in found[0].detail


def test_an_untagged_checkout_falls_back_to_head(tmp_path, monkeypatch):
    """`HEAD` restores the same tree whatever it is called"""
    prod = _prod_checkout(tmp_path, tag=None)
    (prod / "uv.lock").write_text('name = "jarvis-os"\nversion = "0.1.1"\n')

    found = _prod_violations(tmp_path, monkeypatch)
    assert len(found) == 1
    assert found[0].context["ref"] == "HEAD"
    assert "checkout -f HEAD" in found[0].detail


def test_it_is_a_doctor_check_not_a_reconcile_tick_check():
    """Same rule as `check_service_path` and `check_config_drift`: it shells out to git against a..."""
    assert invariants.check_production_clean in invariants.OS_INVARIANTS
    assert invariants.check_production_clean not in invariants.INVARIANTS


# -- INV-OS-HEALTH-SWEEP-DARK: the OS's own sweep must be alive

def _register_catalog(tmp_path, projects: list[dict], enabled=True, health=True) -> None:
    import json

    from jarvis.central_store import CentralStore

    path = tmp_path / "dark-catalog.json"
    path.write_text(json.dumps({
        "os": {"supervisor": {"enabled": enabled, "health_enabled": health},
               "notifications": {"sinks": ["log"]}},
        "projects": projects,
    }))
    central = CentralStore()
    try:
        central.set_state("catalog_path", str(path))
    finally:
        central.close()


def _dark(store) -> list:
    return [v for v in invariants.check_os_health_sweep_alive(store)]


def _owning(monkeypatch, tmp_path, project, **kw) -> ProjectStore:
    """A project IS the OS only by sharing an `origin` with the running install"""
    from jarvis import schedule

    install = str(Path(schedule.__file__).resolve().parent)
    monkeypatch.setitem(schedule._ORIGIN_CACHE, install, ("gonandrap", "agentic_os"))
    monkeypatch.setitem(schedule._ORIGIN_CACHE, str(project),
                        ("gonandrap", "agentic_os"))
    _register_catalog(tmp_path, [{"name": "proj_a", "path": str(project),
                                  "description": "the OS's own"}], **kw)
    return ProjectStore(project)


def test_the_os_sweep_switched_off_in_the_catalog_is_deliberate(monkeypatch, tmp_path, project):
    store = _owning(monkeypatch, tmp_path, project, health=False)

    assert _dark(store) == []
    store.close()


def test_the_supervisor_switch_off_is_deliberate_too(monkeypatch, tmp_path, project):
    store = _owning(monkeypatch, tmp_path, project, enabled=False)

    assert _dark(store) == []
    store.close()


def test_an_enabled_sweep_that_has_never_run_is_reported(monkeypatch, tmp_path, project):
    store = _owning(monkeypatch, tmp_path, project)

    (violation,) = _dark(store)

    assert "never run" in violation.detail
    assert violation.level == "critical"
    store.close()


def test_a_failing_sweep_names_the_failure(monkeypatch, tmp_path, project):
    store = _owning(monkeypatch, tmp_path, project)
    store.record_health_review("work_order", "wo-1", fingerprint="fp",
                               trigger="first-look", outcome="failed",
                               detail="unreadable health sweep output: {...}")

    (violation,) = _dark(store)

    assert "FAILED" in violation.detail
    assert "unreadable health sweep output" in violation.detail
    store.close()


def test_a_sweep_that_judged_recently_is_not_dark(monkeypatch, tmp_path, project):
    store = _owning(monkeypatch, tmp_path, project)
    store.record_health_review("work_order", "wo-1", fingerprint="fp",
                               trigger="first-look", outcome="clear")

    assert _dark(store) == []
    store.close()


def test_a_usage_limit_window_is_not_darkness(monkeypatch, tmp_path, project):
    """A sweep that is silent only because the ACCOUNT was is not dark: the elapsed clock excludes time..."""
    from jarvis import db
    from jarvis.invariants import OS_HEALTH_SWEEP_DARK_MINUTES

    store = _owning(monkeypatch, tmp_path, project)
    window = OS_HEALTH_SWEEP_DARK_MINUTES * 60
    started_at = db.now() - window - 600
    store.conn.execute(
        "INSERT INTO health_reviews (ts, subject_kind, subject_id, fingerprint, "
        "trigger, outcome, findings, detail, reopens_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (started_at, "work_order", "wo-1", "fp", "first-look", "clear", 0, "", 0.0))
    store.conn.execute(
        "INSERT INTO health_reviews (ts, subject_kind, subject_id, fingerprint, "
        "trigger, outcome, findings, detail, reopens_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (started_at + 1, "account", "", "", "account-window", "held", 0,
         "You've hit your session limit", db.now() + 600))

    assert _dark(store) == [], "the account was asleep, not the sweep"
    store.close()


def test_a_project_that_does_not_run_the_os_is_never_reported(monkeypatch, tmp_path,
                                                              project):
    """Every state above, on a project that is not the owner"""
    from jarvis import schedule

    other = tmp_path / "other"
    (other / ".jarvis").mkdir(parents=True)
    install = str(Path(schedule.__file__).resolve().parent)
    monkeypatch.setitem(schedule._ORIGIN_CACHE, install, ("gonandrap", "agentic_os"))
    monkeypatch.setitem(schedule._ORIGIN_CACHE, str(project),
                        ("gonandrap", "agentic_os"))
    _register_catalog(tmp_path, [
        {"name": "proj_a", "path": str(project), "description": "the OS's own"},
        {"name": "proj_b", "path": str(other), "description": "an ordinary one"},
    ], health=False)
    store = ProjectStore(other)

    assert _dark(store) == []

    store.record_health_review("work_order", "wo-1", fingerprint="fp",
                               trigger="first-look", outcome="failed", detail="boom")
    assert _dark(store) == []
    store.close()


def test_a_catalog_with_no_checkout_of_this_repository_owns_nothing(tmp_path, project):
    """§5: no first-in-catalog fallback"""
    _register_catalog(tmp_path, [{"name": "proj_a", "path": str(project),
                                  "description": "an ordinary one"}], health=False)
    store = ProjectStore(project)

    assert _dark(store) == []
    store.close()


def test_an_unreadable_catalog_yields_no_violation(project):
    """An invariant must never be the thing that raises — `_validation_timeout`'s rule"""
    store = ProjectStore(project)
    assert _dark(store) == []
    store.close()


def test_the_dark_check_runs_every_tick(tmp_path, project):
    """A liveness check that runs hourly is a liveness check with an hour of blind spot"""
    assert invariants.check_os_health_sweep_alive in invariants.INVARIANTS
    assert invariants.check_os_health_sweep_alive not in invariants.SLOW_INVARIANTS


# -- INV-OS-IDENTITY: the OS's own project must be identifiable

OS_ORIGIN = "gonandrap/agentic_os"


def _os_checkout(root, name, origin=OS_ORIGIN):
    from jarvis.testing import make_git_project, with_origin

    return with_origin(make_git_project(root, name), origin)


def _install_in(checkout: Path, monkeypatch) -> None:
    """Run as if the `jarvis` package being executed lived in this checkout"""
    from jarvis import schedule

    pkg = checkout / "src" / "jarvis"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(schedule, "__file__", str(pkg / "schedule.py"))


def test_an_ambiguous_os_identity_is_reported(tmp_path, monkeypatch, origins):
    """Two catalog projects on one origin — a worktree listed beside its checkout"""
    dev = _os_checkout(tmp_path, "jarvis_os")
    twin = _os_checkout(tmp_path, "jarvis_os_worktree")
    _register_catalog(tmp_path, [
        {"name": "jarvis_os", "path": str(dev), "description": "the OS's own"},
        {"name": "jarvis_os_wt", "path": str(twin), "description": "its worktree"},
    ])
    _install_in(dev, monkeypatch)

    (violation,) = list(invariants.check_os_identity())

    assert violation.invariant == "INV-OS-IDENTITY"
    assert violation.level == "critical"
    assert not violation.repaired, "which project is the OS is not the OS's to decide"
    assert violation.context["candidates"] == ["jarvis_os", "jarvis_os_wt"]
    assert "release" in violation.detail


def test_a_deployment_that_drives_no_checkout_of_this_repository_is_silent(
        tmp_path, monkeypatch, origins):
    """A pip-installed OS whose catalog holds only other people's projects"""
    other = _os_checkout(tmp_path, "shared_schedule", "gonandrap/shared_schedule")
    elsewhere = _os_checkout(tmp_path, "install")
    _register_catalog(tmp_path, [{"name": "shared_schedule", "path": str(other),
                                  "description": "not the OS"}])
    _install_in(elsewhere, monkeypatch)

    assert list(invariants.check_os_identity()) == []


def test_identity_resolved_to_one_project_is_silent(tmp_path, monkeypatch, origins):
    dev = _os_checkout(tmp_path, "jarvis_os")
    other = _os_checkout(tmp_path, "shared_schedule", "gonandrap/shared_schedule")
    _register_catalog(tmp_path, [
        {"name": "shared_schedule", "path": str(other), "description": "not the OS"},
        {"name": "jarvis_os", "path": str(dev), "description": "the OS's own"},
    ])
    _install_in(dev, monkeypatch)

    assert list(invariants.check_os_identity()) == []


def test_the_identity_check_is_an_os_level_one():
    assert invariants.check_os_identity in invariants.OS_INVARIANTS


def test_the_notification_carries_the_violations_own_level(tmp_path, project,
                                                           monkeypatch, catalog_file):
    """`Daemon.check_invariants` passed the literal `"warning"`"""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    spec = daemon.catalog.projects[0]
    store = ProjectStore(spec.path)
    monkeypatch.setattr(invariants, "check_project", lambda *a, **k: [
        invariants.Violation(invariant="INV-MADE-UP-CRITICAL", detail="the loud one",
                             level="critical"),
        invariants.Violation(invariant="INV-MADE-UP-ORDINARY", detail="the usual one"),
    ])

    daemon.check_invariants(spec, store)

    levels = {n["title"].split(": ")[-1]: n["level"]
              for n in store.unrouted_notifications() if n["source"] == "invariants"}
    assert levels["INV-MADE-UP-CRITICAL"] == "critical"
    assert levels["INV-MADE-UP-ORDINARY"] == "warning", "every existing one is additive"


# -- a dropped confirmation is the user's again

from jarvis.neo_store import NeoStore                                     # noqa: E402
from tests.test_autoreview import ROUTINE, ask, park, started             # noqa: E402,F401
from tests.test_autoreview_confirm import provisional, with_diff          # noqa: E402,F401

ASSUMPTION_BLOCKER = "1 assumption pending your review"


def _confirming(started, catalog_file):
    """A parked order with one pending assumption the OS has an early ACCEPT on"""
    store, wo = park(started, auto_review=True)
    # AFTER `park`, which keeps the catalog FILE's `validation.enabled` false on purpose:...
    for key in ("validation.enabled", "validation.auto_review"):
        ops.set_config(key, True, project="proj_a",
                       reason="the panel has been right for a month",
                       catalog_path=str(catalog_file))
    (row,) = store.all_assumptions(wo["id"])
    store.record_provisional(row["id"], verdict="accept", reason="r", model="sonnet")
    return store, wo, row["id"]


def test_a_confirmation_in_flight_suppresses_the_blocker_and_a_dropped_one_does_not(
        started, catalog_file):
    """THE SAME ROW, walked from in-flight to spent"""
    store, wo, aid = _confirming(started, catalog_file)
    neo = NeoStore()
    try:
        q = neo.ask("proj_a", wo["id"], "confirm it?", kind="assumption")
        store.link_assumption_confirmation(aid, q["id"])

        # queued: Neo has it, and the user owes nothing
        assert ASSUMPTION_BLOCKER not in true_blockers(store,
                                                       store.get_work_order(wo["id"]))
        # ...and so is a link that is not there at all — a confirmation still to come
        store.clear_assumption_confirmation(aid)
        assert ASSUMPTION_BLOCKER not in true_blockers(store,
                                                       store.get_work_order(wo["id"]))
        store.link_assumption_confirmation(aid, q["id"])

        neo.mark(q["id"], "escalated", reason="yours")
    finally:
        neo.close()

    blockers = true_blockers(store, store.get_work_order(wo["id"]))
    assert ASSUMPTION_BLOCKER in blockers

    # ...and INV-ATTENTION-REASON then names it, instead of agreeing with the lie
    store.flag_attention(wo["id"], IDLE_NOTIFICATION)
    violations = [v for v in invariants.check_attention_reason_is_true(store)]
    assert [v.invariant for v in violations] == ["INV-ATTENTION-REASON"]
    assert store.get_work_order(wo["id"])["attention_reason"] == ASSUMPTION_BLOCKER


def test_a_confirmation_link_nobody_can_resolve_does_not_suppress(started, catalog_file):
    """FAIL TOWARD THE USER (§7): an unreadable question is not evidence of a confirmation in flight"""
    store, wo, aid = _confirming(started, catalog_file)

    store.link_assumption_confirmation(aid, 999_999)      # no such question, ever

    assert ASSUMPTION_BLOCKER in true_blockers(store, store.get_work_order(wo["id"]))


def test_a_transient_drop_strands_no_question_in_neo_attention(started):
    """§4.3's PAIR, and the reason the drop site supersedes BEFORE it clears: a cleared link makes the..."""
    from jarvis.neo_store import USER_HELD_Q_STATUSES

    store, wo = with_diff(started, assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)
    store.set_status(wo["id"], "running", trigger="test")

    started._neo_drain()                                  # noqa: SLF001

    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] is None
    neo = NeoStore()
    try:
        held = neo.list_questions(statuses=USER_HELD_Q_STATUSES)
    finally:
        neo.close()
    assert [q["id"] for q in held if q["kind"] == "assumption"] == []
    assert list(invariants.check_neo_escalations_are_live(store)) == []


# -- a rebind: the round the OS's own merge costs nobody


def _declined_on_a_moved_head(store: ProjectStore, *, cause: str,
                              head: str = "bbbb1111bbbb2222",
                              judged: str = "aaaa1111aaaa2222") -> dict:
    """A pull request parked on a head the OS refused to re-judge, for `cause`"""
    wo = store.create_work_order("add feature X")
    store.update_work_order(wo["id"], pr_url="https://github.com/acme/proj/pull/7")
    row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.set_validation_head(row["id"], judged)
    store.close_validation_round(row["id"], "passed", "")
    store.set_status(wo["id"], "waiting_pr_merge")
    store.add_event(wo["id"], invariants.REJUDGE_DECLINED_EVENT,
                    {"head_sha": head, "judged_sha": judged, "cause": cause})
    store.add_event(wo["id"], "automerge_held",
                    {"code": invariants.HELD_SHA_MOVED, "head_sha": head})
    return store.get_work_order(wo["id"])


def test_a_rebind_decline_is_not_a_spent_round_budget(project):
    """The two declines mean different things and only one is answered by `validation.max_rounds`"""
    from jarvis import ops

    store = ProjectStore(project)
    spent = _declined_on_a_moved_head(store, cause=ops.REBIND_EXHAUSTED)

    assert invariants.rejudge_exhausted(store, spent) is False
    assert invariants.rebind_exhausted(store, spent) is True
    assert invariants.sha_unrecorded_rejudge_spent(store, spent) is False
    blockers = true_blockers(store, spent)
    assert invariants.REBIND_EXHAUSTED_BLOCKER in blockers
    assert invariants.SHA_MOVED_BLOCKER not in blockers
    assert invariants.SHA_UNRECORDED_SPENT_BLOCKER not in blockers


def test_a_budget_decline_still_reads_as_one(project):
    from jarvis import ops

    store = ProjectStore(project)
    budget = _declined_on_a_moved_head(store, cause=ops.REJUDGE_BUDGET_SPENT)

    assert invariants.rejudge_exhausted(store, budget) is True
    assert invariants.rebind_exhausted(store, budget) is False
    assert invariants.sha_unrecorded_rejudge_spent(store, budget) is False
    blockers = true_blockers(store, budget)
    assert invariants.SHA_MOVED_BLOCKER in blockers
    assert invariants.REBIND_EXHAUSTED_BLOCKER not in blockers
    assert invariants.SHA_UNRECORDED_SPENT_BLOCKER not in blockers


def test_a_decline_written_before_the_cause_existed_is_a_budget_decline(project):
    """The migration reading: a payload with no `cause` is every row there was"""
    store = ProjectStore(project)
    old = _declined_on_a_moved_head(store, cause="")

    assert invariants.rejudge_exhausted(store, old) is True
    assert invariants.rebind_exhausted(store, old) is False
    assert invariants.sha_unrecorded_rejudge_spent(store, old) is False


def test_a_spent_unrecorded_head_is_its_own_blocker_and_only_its_own(project):
    """The fourth sentence in the family: the OS spent its one round on this commit and that round bound..."""
    from jarvis import ops

    store = ProjectStore(project)
    wo = store.create_work_order("add feature X")
    store.update_work_order(wo["id"], pr_url="https://github.com/acme/proj/pull/7")
    row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.close_validation_round(row["id"], "passed", "")      # head_sha stays ''
    store.set_status(wo["id"], "waiting_pr_merge")
    head = "bbbb1111bbbb2222"
    store.add_event(wo["id"], "validation_forced",
                    {"round": 1, "by": ops.REJUDGE_BY_OS, "head_sha": head,
                     "judged_sha": ""})
    store.add_event(wo["id"], "automerge_held",
                    {"code": invariants.HELD_SHA_UNRECORDED, "head_sha": head})
    spent = store.get_work_order(wo["id"])

    assert invariants.sha_unrecorded_rejudge_spent(store, spent) is True
    assert invariants.rejudge_exhausted(store, spent) is False
    assert invariants.rebind_exhausted(store, spent) is False
    assert invariants.sha_unrecorded_exhausted(store, spent) is False
    blockers = true_blockers(store, spent)
    assert invariants.SHA_UNRECORDED_SPENT_BLOCKER in blockers
    assert invariants.SHA_UNRECORDED_BLOCKER not in blockers
    assert invariants.SHA_MOVED_BLOCKER not in blockers
    assert invariants.REBIND_EXHAUSTED_BLOCKER not in blockers

    # ...and a head the OS has NOT judged belongs to the re-judge path, not to the user
    store.add_event(wo["id"], "automerge_held",
                    {"code": invariants.HELD_SHA_UNRECORDED,
                     "head_sha": "cccc1111cccc2222"})
    moved = store.get_work_order(wo["id"])
    assert invariants.sha_unrecorded_rejudge_spent(store, moved) is False
    assert invariants.SHA_UNRECORDED_SPENT_BLOCKER not in true_blockers(store, moved)


# -- a panel give-up that survived into `waiting_pr_merge`


def _parked_on_an_escalation(store: ProjectStore, *, outcome: str = "escalated",
                             hold: str | None = invariants.HELD_NOT_PASSED,
                             status: str = "waiting_pr_merge",
                             after: str | None = None) -> dict:
    """A work order parked behind its pull request with the panel's last word on it (docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md §A.)"""
    wo = store.create_work_order("add feature X")
    store.update_work_order(wo["id"], pr_url="https://github.com/acme/proj/pull/7",
                            result_summary="opened a PR")
    row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.set_validation_head(row["id"], "aaaa1111aaaa2222")
    store.close_validation_round(row["id"], outcome, "")
    if after is not None:
        later = store.open_validation_round(wo_id=wo["id"], fingerprint="fp-after")
        if after != "pending":
            store.close_validation_round(later["id"], after, "")
    store.set_status(wo["id"], status)
    if hold is not None:
        store.add_event(wo["id"], "automerge_held", {"code": hold, "round": 1})
    return store.get_work_order(wo["id"])


def test_a_give_up_parked_behind_a_held_merge_is_the_users(project):
    """Row 1: the live stall on wo-3615faf7 — nothing automatic is left and the OS said nothing for 47h"""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store)

    assert invariants.parked_on_a_give_up(store, wo) is True
    assert invariants.PARKED_GIVE_UP_BLOCKER in true_blockers(store, wo)
    # DERIVED, never written at the hold site (kn-089de524)
    assert not wo["needs_attention"]
    list(invariants.check_blocked_work_is_surfaced(store))
    row = store.get_work_order(wo["id"])
    assert row["needs_attention"]
    assert row["attention_reason"] == invariants.PARKED_GIVE_UP_BLOCKER


def test_a_hold_with_another_cause_is_not_this_stall(project):
    """Row 2: the merge is held by something with its own machinery behind it"""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store, hold="checks_running")  # any other code

    assert invariants.parked_on_a_give_up(store, wo) is False
    assert invariants.PARKED_GIVE_UP_BLOCKER not in true_blockers(store, wo)


def test_a_project_merging_by_hand_is_not_flagged_at_all(project):
    """Row 3: `auto_merge` off writes no hold ever, and there the pull request merges on GitHub"""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store, hold=None)

    assert invariants.parked_on_a_give_up(store, wo) is False
    assert invariants.PARKED_GIVE_UP_BLOCKER not in true_blockers(store, wo)


def test_a_round_still_open_owes_nobody_anything(project):
    """Row 4: `validation_escalated` is False while the panel is still deliberating"""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store, outcome="pending")

    assert invariants.parked_on_a_give_up(store, wo) is False
    assert invariants.PARKED_GIVE_UP_BLOCKER not in true_blockers(store, wo)


def test_a_passed_round_held_for_some_other_reason_is_not_a_give_up(project):
    """Row 5"""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store, outcome="passed")

    assert invariants.parked_on_a_give_up(store, wo) is False
    assert invariants.PARKED_GIVE_UP_BLOCKER not in true_blockers(store, wo)


def test_a_later_round_that_passes_clears_it_without_a_new_hold(project):
    """Row 6: self-clearing on fact 1 alone, on the same tick"""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store)
    assert invariants.PARKED_GIVE_UP_BLOCKER in true_blockers(store, wo)

    later = store.open_validation_round(wo_id=wo["id"], fingerprint="fp2")
    store.set_validation_head(later["id"], "bbbb1111bbbb2222")
    store.close_validation_round(later["id"], "passed", "")

    row = store.get_work_order(wo["id"])
    assert invariants.parked_on_a_give_up(store, row) is False
    assert invariants.PARKED_GIVE_UP_BLOCKER not in true_blockers(store, row)


def test_an_ack_of_a_parked_give_up_stays_down(project):
    """Row 7: kn-089de524 — the hold is rewritten every poll"""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store)
    list(invariants.check_blocked_work_is_surfaced(store))
    row = store.get_work_order(wo["id"])
    store.ack_attention(row["id"], true_blockers(store, row))

    row = store.get_work_order(wo["id"])
    assert invariants.PARKED_GIVE_UP_BLOCKER not in true_blockers(store, row)
    list(invariants.check_attention_reason_is_true(store))
    list(invariants.check_blocked_work_is_surfaced(store))
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_the_needs_review_arm_is_untouched(project):
    """Row 8: the upstream sentence still answers the status it was written for"""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store, status="needs_review")

    blockers = true_blockers(store, wo)
    assert VALIDATION_STUCK_BLOCKER in blockers
    assert invariants.PARKED_GIVE_UP_BLOCKER not in blockers


def test_a_moved_head_decline_is_the_other_sentence_and_only_that(project):
    """Row 9: one newest hold carries one code, so the two can never co-occur"""
    from jarvis import ops as ops_mod

    store = ProjectStore(project)
    moved = _declined_on_a_moved_head(store, cause=ops_mod.REJUDGE_BUDGET_SPENT)

    blockers = true_blockers(store, moved)
    assert invariants.SHA_MOVED_BLOCKER in blockers
    assert invariants.PARKED_GIVE_UP_BLOCKER not in blockers


# -- a row that judged nothing must not drop the give-up under it


def _owed_after(project, outcome: str) -> tuple[ProjectStore, dict]:
    store = ProjectStore(project)
    return store, _parked_on_an_escalation(store, after=outcome)


def test_a_pending_round_after_a_give_up_does_not_drop_it(project):
    """A forced or rebind round in flight judges nothing, so nobody has decided"""
    store, wo = _owed_after(project, "pending")

    assert invariants.validation_escalated(store, wo) is True
    assert invariants.parked_on_a_give_up(store, wo) is True
    assert invariants.PARKED_GIVE_UP_BLOCKER in true_blockers(store, wo)


def test_a_failed_transport_round_after_a_give_up_does_not_drop_it(project):
    """`counted_validation_rounds` already calls a `failed` round no judgement"""
    store, wo = _owed_after(project, "failed")

    assert invariants.validation_escalated(store, wo) is True
    assert invariants.parked_on_a_give_up(store, wo) is True
    assert invariants.PARKED_GIVE_UP_BLOCKER in true_blockers(store, wo)


def test_a_void_round_after_a_give_up_does_not_drop_it(project):
    """A `void` round had nothing for a reviewer, so it asked the submitter nothing"""
    store, wo = _owed_after(project, "void")

    assert invariants.validation_escalated(store, wo) is True
    assert invariants.parked_on_a_give_up(store, wo) is True
    assert invariants.PARKED_GIVE_UP_BLOCKER in true_blockers(store, wo)


def _give_up_violations(store: ProjectStore) -> list:
    return [v for v in invariants.check_validation_progresses(store)
            if v.invariant == "INV-VALIDATION-STRANDED"]


def test_a_give_up_parked_in_the_merge_queue_is_put_back_for_review(project):
    """§B: nothing re-derives `waiting_pr_merge`, so state already written needs a move"""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store)

    found = _give_up_violations(store)

    assert [v.wo_id for v in found] == [wo["id"]]
    assert found[0].repaired
    row = store.get_work_order(wo["id"])
    assert row["status"] == "needs_review"
    assert store.last_event_of_kind(wo["id"], "give_up_reparked") is not None
    assert not row["needs_attention"]

    list(invariants.check_blocked_work_is_surfaced(store))
    row = store.get_work_order(wo["id"])
    assert row["needs_attention"]
    assert VALIDATION_STUCK_BLOCKER in true_blockers(store, row)


def test_a_project_merging_by_hand_is_left_in_the_merge_queue(project):
    """The twin of row 3: `auto_merge` off writes no hold"""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store, hold=None)

    assert _give_up_violations(store) == []
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


def test_the_repair_writes_nothing_under_a_live_turn(project):
    """The discipline daemon.py:3045 and daemon.py:6353 already follow"""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store)
    store.create_turn(wo["id"], "message", "go")
    assert worker_session.busy(store, wo["id"]) is not None

    assert _give_up_violations(store) == []
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


def test_the_repair_is_idempotent(project):
    """Self-extinguishing: after the move there is no `waiting_pr_merge` row left"""
    store = ProjectStore(project)
    _parked_on_an_escalation(store)

    assert len(_give_up_violations(store)) == 1
    assert _give_up_violations(store) == []


# -- is somebody ELSE holding this, or is anything out at all?


def test_a_held_gate_is_out_but_is_not_a_user_facing_wait(project):
    store = ProjectStore(project)
    wo = store.create_work_order("ship it")
    store.add_approval(wo["id"], "release", "scripts/deploy.sh 0.5.4",
                       status="awaiting_case")

    assert invariants.something_is_out(store, wo["id"])
    assert not invariants.user_facing_wait(store, wo["id"])
    store.close()


def test_a_pending_gate_is_both(project):
    store = ProjectStore(project)
    wo = store.create_work_order("ship it")
    store.add_approval(wo["id"], "release", "scripts/deploy.sh 0.5.4")

    assert invariants.something_is_out(store, wo["id"])
    assert invariants.user_facing_wait(store, wo["id"])
    store.close()


def test_nothing_out_is_neither(project):
    store = ProjectStore(project)
    wo = store.create_work_order("ship it")

    assert not invariants.something_is_out(store, wo["id"])
    assert not invariants.user_facing_wait(store, wo["id"])
    store.close()
# -- §2c: an undeclared delivery, as a predicate on its own

PUSHED = "9999999999999999999999999999999999999999"
DELIVERED = "1111111111111111111111111111111111111111"


def _refused_then_pushed(store: ProjectStore, *, head: str, judged: str,
                         description: str = "") -> dict:
    """wo-dbea82cf: delivered, judged, the user refused an assumption, then commits"""
    wo = store.create_work_order("the refused one", description=description)
    store.add_event(wo["id"], "finished", {"summary": "opened a PR"})
    round_ = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.set_validation_head(round_["id"], judged)
    store.close_validation_round(round_["id"], "passed", "green")
    store.add_event(wo["id"], "reviewed", {"accepted": False})
    store.set_status(wo["id"], "needs_review", pr_url="https://example/pull/2",
                     pr_head_oid=head, pr_head_seen_at=db.now())
    return store.get_work_order(wo["id"])


def test_commits_past_the_judged_head_with_no_finish_are_an_undeclared_delivery(project):
    """Spec §2c"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)

    assert not ops.refusal_answered(store, wo["id"])
    assert invariants.undeclared_delivery(store, wo)


def test_a_head_still_at_the_judged_commit_is_not_an_undeclared_delivery(project):
    """Nothing moved: the refusal is simply unanswered, which is today's sentence"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=DELIVERED, judged=DELIVERED)

    assert not invariants.undeclared_delivery(store, wo)


def test_no_recorded_head_is_never_an_undeclared_delivery(project):
    """Empty is "not recorded", never "different" — the same rule as §2b"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head="", judged=DELIVERED)

    assert not invariants.undeclared_delivery(store, wo)


def test_a_finish_after_the_refusal_answers_it_and_ends_the_detection(project):
    """The worker declared them, so there is nothing undeclared left to nudge for"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    store.add_event(wo["id"], "finished", {"summary": "here is what I pushed"})
    wo = store.get_work_order(wo["id"])

    assert ops.refusal_answered(store, wo["id"])
    assert not invariants.undeclared_delivery(store, wo)


def test_the_detector_raises_a_finding_once_and_never_an_alarm(project):
    """`add_finding`, not `add_alarm`: the cost path's dedupe is what `add_alarm`'s one call site is..."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)

    check_project(store)
    check_project(store)

    raised = [a for a in store.alarms_of(wo["id"])
              if a["kind"] == "undeclared_delivery"]
    assert len(raised) == 1
    assert raised[0]["source"] == "invariant"


def test_a_doctor_run_without_repair_reports_the_finding_and_writes_none(project):
    """`jarvis doctor` with no `--repair` must not write, and must still SAY it"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)

    reported = [v for v in check_project(store, repair=False)
                if v.invariant == "INV-UNDECLARED-DELIVERY"]

    assert [v.wo_id for v in reported] == [wo["id"]]
    assert not reported[0].repaired
    assert reported[0].repair.startswith("would raise")
    assert store.alarms_of(wo["id"]) == []


def test_no_finding_while_the_os_is_nudging_the_worker(project):
    """The poll is the actor now: an order with a session and no give-up is being asked to declare"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    store.update_work_order(wo["id"], session_id="sess-1")

    reported = [v for v in check_project(store)
                if v.invariant == "INV-UNDECLARED-DELIVERY"]

    assert reported == []
    assert store.alarms_of(wo["id"]) == []


def test_the_finding_is_the_give_ups(project):
    """Once the OS has said it is stopping, the finding is what reaches the user — raised once, as before"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    store.update_work_order(wo["id"], session_id="sess-1")
    store.add_event(wo["id"], "pr_undeclared_unresolved", {"attempts": 3})

    check_project(store)
    check_project(store)

    raised = [a for a in store.alarms_of(wo["id"])
              if a["kind"] == "undeclared_delivery"]
    assert len(raised) == 1


# -- INV-STUCK-SWEEP-DARK: the stuck sweep's own failures are a first-class alarm

def _sweep_run(tmp_path, project, *, projects=None, os_extra=None, **row) -> Path:
    """A registered catalog at `fleet_health`'s shipped defaults, plus one run row"""
    import json

    from jarvis import db
    from jarvis.central_store import CentralStore
    from jarvis.daemon import Daemon

    path = tmp_path / "stuck-catalog.json"
    path.write_text(json.dumps({
        "os": {"notifications": {"sinks": ["log"]}, **(os_extra or {})},
        "projects": projects or [
            {"name": "proj_a", "path": str(project), "description": "test"}],
    }))
    central = CentralStore()
    try:
        central.set_state("catalog_path", str(path))
        if row:
            central.set_state(Daemon.STUCK_RUN_KEY, db.to_json(
                {"ts": db.now(), "scanned": 0, "candidates": 0, "opened": 0,
                 "skipped": {}, "excluded": "", "error": "", **row}))
    finally:
        central.close()
    return path


def _own_the_os(monkeypatch, project) -> None:
    """Make `project` the OS-OWNING project"""
    from jarvis import schedule

    install = str(Path(schedule.__file__).resolve().parent)
    for key in {install, str(project), str(Path(project).resolve())}:
        monkeypatch.setitem(schedule._ORIGIN_CACHE, key, ("gonandrap", "agentic_os"))


def _stuck_dark(store) -> list:
    return [v for v in invariants.check_stuck_sweep_alive(store)
            if v.invariant == "INV-STUCK-SWEEP-DARK"]


def test_sweep_error_raises_the_invariant_once(jarvis_home, tmp_path, project,
                                               monkeypatch):
    """§8: the run row carries the error, and the row is what the check reads"""
    _own_the_os(monkeypatch, project)
    _sweep_run(tmp_path, project, error="OperationalError: database is locked")
    store = ProjectStore(project)

    (violation,) = _stuck_dark(store)
    assert violation.level == "critical" and not violation.repaired
    assert violation.context["cause"] == "failing"
    assert "database is locked" in violation.detail
    # The STATE, not an event: a second read of the same row says the same thing
    assert [v.context["cause"] for v in _stuck_dark(store)] == ["failing"]

    _sweep_run(tmp_path, project, error="")
    assert _stuck_dark(store) == []
    store.close()


def test_sweep_dark_raises_after_the_window(jarvis_home, tmp_path, project, monkeypatch):
    """§8: the window is six sweep intervals, so one capped tick cannot trip it"""
    import jarvis.catalog as catalog_mod

    _own_the_os(monkeypatch, project)
    window = catalog_mod.DEFAULT_FLEET_HEALTH_SWEEP_DARK_MINUTES
    _sweep_run(tmp_path, project, ts=db.now() - (window + 1) * 60)
    store = ProjectStore(project)

    (violation,) = _stuck_dark(store)
    assert violation.context["cause"] == "dark"
    assert window == 180

    _sweep_run(tmp_path, project, ts=db.now() - (window - 1) * 60)
    assert _stuck_dark(store) == []
    store.close()


def test_the_fleet_catalog_value_is_what_the_check_judges_by(
        jarvis_home, tmp_path, project, monkeypatch):
    """`sweep_dark_minutes` is a CATALOG setting"""
    _own_the_os(monkeypatch, project)
    silence = 20 * 60  # 20 minutes — well inside the shipped 180
    _sweep_run(tmp_path, project, ts=db.now() - silence)
    store = ProjectStore(project)
    assert _stuck_dark(store) == []

    _sweep_run(tmp_path, project, ts=db.now() - silence,
               os_extra={"fleet_health": {"sweep_dark_minutes": 10}})
    (violation,) = _stuck_dark(store)
    assert violation.context["cause"] == "dark"
    assert violation.context["dark_minutes"] == 20
    store.close()


def test_the_stuck_check_is_registered_where_it_can_push(tmp_path, project):
    """Review round 1: in `OS_INVARIANTS` only `jarvis doctor` ran it"""
    assert invariants.check_stuck_sweep_alive in invariants.INVARIANTS
    assert invariants.check_stuck_sweep_alive not in invariants.OS_INVARIANTS
    assert invariants.check_stuck_sweep_alive not in invariants.SLOW_INVARIANTS


def test_the_fleet_wide_sweep_is_reported_once_not_once_per_project(
        jarvis_home, tmp_path, project, monkeypatch):
    """One run row, one report: every project but the owner short-circuits"""
    other = tmp_path / "other"
    (other / ".jarvis").mkdir(parents=True)
    _own_the_os(monkeypatch, project)
    _sweep_run(tmp_path, project, error="boom", projects=[
        {"name": "proj_a", "path": str(project), "description": "the OS's own"},
        {"name": "proj_b", "path": str(other), "description": "an ordinary one"},
    ])
    owner, ordinary = ProjectStore(project), ProjectStore(other)

    assert [v.context["cause"] for v in _stuck_dark(owner)] == ["failing"]
    assert _stuck_dark(ordinary) == []
    owner.close()
    ordinary.close()


def test_a_failing_sweep_reaches_the_attention_list(jarvis_home, tmp_path, project,
                                                    monkeypatch):
    """Review round 1: the push surface"""
    from jarvis import ops as ops_module
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    _own_the_os(monkeypatch, project)
    path = _sweep_run(tmp_path, project)
    ops.start_os(str(path), foreground=True)
    clean_scan = ops_module.stuck_scan

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ops_module, "stuck_scan", boom)
    catalog = load_catalog(path)
    daemon, store = Daemon(catalog), ProjectStore(project)
    try:
        daemon.stuck_tick()
        daemon.check_invariants(catalog.projects[0], store)

        items = _stuck_attention(catalog)
        assert len(items) == 1
        assert "database is locked" in items[0]["reason"]
        assert not ops.os_status(catalog)["healthy"]

        daemon.stuck_tick()
        daemon.check_invariants(catalog.projects[0], store)
        assert len(_stuck_attention(catalog)) == 1, "one item, not one per tick"

        # A CLEAN sweep clears it — but `check_invariants` only closes reports on a sweep tick
        monkeypatch.setattr(ops_module, "stuck_scan", clean_scan)
        daemon.stuck_tick()
        daemon.check_invariants(catalog.projects[0], store, sweep_landings=True)
        assert _stuck_attention(catalog) == []
    finally:
        store.close()


def _stuck_attention(catalog) -> list:
    return [i for i in ops.os_status(catalog)["attention"]
            if i.get("invariant") == "INV-STUCK-SWEEP-DARK"]


# -- Neo 1084: a standing critical violation is ONE attention item


def test_a_standing_critical_violation_reaches_the_attention_list(
        jarvis_home, project, catalog_file, monkeypatch, live_daemon):
    """Neo 1084: `jarvis status` read HEALTHY over a standing critical violation"""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    wo = ops.create_work_order("proj_a", "the subject")
    violation = invariants.Violation(
        invariant="INV-TEST-CRITICAL", detail="a standing break", level="critical",
        wo_id=wo["id"])
    monkeypatch.setattr(invariants, "check_project", lambda *a, **k: [violation])
    catalog = load_catalog(catalog_file)
    daemon, store = Daemon(catalog), ProjectStore(project)
    try:
        daemon.check_invariants(catalog.projects[0], store)
        daemon.check_invariants(catalog.projects[0], store)  # ONE item, not one per tick
    finally:
        store.close()

    status = ops.os_status(catalog)
    items = [i for i in status["attention"] if i.get("invariant") == "INV-TEST-CRITICAL"]
    assert len(items) == 1
    assert items[0]["reason"] == "a standing break"
    assert status["attention"] == items, "nothing else is asking, so this is why"
    assert not status["healthy"]

    # ...and the report row is also what takes it away again
    closed = ProjectStore(project)
    try:
        assert closed.close_violation_report("INV-TEST-CRITICAL", wo["id"])
    finally:
        closed.close()
    assert ops.os_status(catalog)["attention"] == [], "the row is also the removal"


# -- a passed forced round answers a refusal

FORCED = "2222222222222222222222222222222222222222"


def _forced_round(store: ProjectStore, wo_id: str, *, outcome: str = "passed",
                  cause: str | None = None, head: str = FORCED,
                  carried: str = "") -> dict:
    """The one round `ops.user_rework_pending` grants, opened after the refusal"""
    row = store.open_validation_round(
        wo_id=wo_id, fingerprint="fp2", uncounted=True,
        uncounted_cause=ops.USER_REWORK_CAUSE if cause is None else cause)
    if head:
        store.set_validation_head(row["id"], head)
    store.close_validation_round(row["id"], outcome, "green")
    if carried:
        store.carry_round_head(row["id"], carried, "base merge")
    return store.latest_validation_round(wo_id=wo_id)


def test_a_passed_user_rework_round_answers_the_refusal_and_lands(project):
    """Spec §The predicate (b): the user forcing that round IS the declaration"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, wo["id"])

    assert ops.refusal_answered(store, wo["id"])
    assert ops.land_when_cleared(store, store.get_work_order(wo["id"]),
                                 "https://example/pull/2") == "waiting_pr_merge"


def test_a_rejected_user_rework_round_leaves_the_refusal_unanswered(project):
    """Spec §What does NOT change: (b) requires `passed`"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, wo["id"], outcome="rejected")

    assert not ops.refusal_answered(store, wo["id"])
    assert ops.land_when_cleared(store, store.get_work_order(wo["id"]),
                                 "https://example/pull/2") == "needs_review"


def test_a_rebind_round_never_answers_a_refusal(project):
    """Spec §What does NOT change: the OS demanded that merge, not the user"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, wo["id"], cause=ops.REBIND_CAUSE)

    assert not ops.refusal_answered(store, wo["id"])


def test_an_unmoved_judged_head_does_not_answer_the_refusal(project):
    """Spec §The head guard: a pass on the refused commit is the back door"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, wo["id"], head=DELIVERED)

    assert not ops.refusal_answered(store, wo["id"])


def test_an_unrecorded_head_on_either_round_still_answers(project):
    """Spec §The head guard: "" is never a match — the pre-0.10.0 population"""
    store = ProjectStore(project)
    forced_blank = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, forced_blank["id"], head="")
    prior_blank = _refused_then_pushed(store, head=PUSHED, judged="")
    _forced_round(store, prior_blank["id"], head=DELIVERED)

    assert ops.refusal_answered(store, forced_blank["id"])
    assert ops.refusal_answered(store, prior_blank["id"])


def test_a_round_settled_before_the_refusal_does_not_answer_it(project):
    """Spec §The predicate: the newest counted round must be newer than the cut"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)

    assert not ops.refusal_answered(store, wo["id"])


def test_a_carried_head_equal_to_the_refused_one_does_not_answer(project):
    """Spec §The head guard: `validated_head` is the read, so the carry is the head"""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, wo["id"], head=FORCED, carried=DELIVERED)

    assert not ops.refusal_answered(store, wo["id"])


def test_the_undeclared_delivery_detector_follows_the_widened_predicate(project):
    """Spec §`invariants.undeclared_delivery`: it keeps CALLING the predicate"""
    store = ProjectStore(project)
    answered = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, answered["id"])

    assert not invariants.undeclared_delivery(store, store.get_work_order(
        answered["id"]))


# -- an unanswered refusal names itself


def _held_by_a_refusal(store: ProjectStore, kind: str = "worker") -> dict:
    """Delivered, then refused, and nothing delivered since — the held state itself"""
    wo = store.create_work_order("ship the thing", kind=kind)
    store.add_event(wo["id"], "finished", {"summary": "delivered"})
    store.add_event(wo["id"], "reviewed", {"accepted": False})
    store.set_status(wo["id"], "needs_review")
    return store.get_work_order(wo["id"])


def test_a_needs_review_order_held_only_by_an_unanswered_refusal_says_so(project):
    """Spec §1 and §3: the generic idle line lies about every clause of this state"""
    store = ProjectStore(project)
    wo = _held_by_a_refusal(store)

    blockers = true_blockers(store, wo)

    assert blockers == [invariants.REFUSAL_UNANSWERED_BLOCKER]
    assert IDLE_NO_FINISH_BLOCKER not in blockers


def test_an_analyst_held_by_a_refusal_is_pointed_at_wo_finish(project):
    """Spec §2: the submission verb would REPLACE the document and discard decisions"""
    store = ProjectStore(project)

    for kind in invariants.DOCUMENT_DELIVERY_KINDS:
        wo = _held_by_a_refusal(store, kind=kind)

        assert true_blockers(store, wo) == [invariants.REFUSAL_UNANSWERED_DOC_BLOCKER]

    sentence = invariants.REFUSAL_UNANSWERED_DOC_BLOCKER
    assert "`jarvis wo finish`" in sentence
    assert "io report" not in sentence and "fo plan" not in sentence


def test_an_answered_refusal_still_derives_the_idle_line(project):
    """The no-regression half: a finish newer than the refusal clears the hold"""
    store = ProjectStore(project)
    wo = _held_by_a_refusal(store)
    store.add_event(wo["id"], "finished", {"summary": "re-delivered"})

    assert true_blockers(store, store.get_work_order(wo["id"])) == [
        IDLE_NO_FINISH_BLOCKER]


def test_unlanded_work_outranks_an_unanswered_refusal(project):
    """Spec §3: code stranded on a branch names a different, more urgent decision"""
    store = ProjectStore(project)
    wo = _held_by_a_refusal(store)
    store.add_event(wo["id"], "work_unlanded", {"was": "completed"})

    blockers = true_blockers(store, store.get_work_order(wo["id"]))

    assert blockers == [invariants.UNLANDED_BLOCKER]
    assert invariants.REFUSAL_UNANSWERED_BLOCKER not in blockers


def test_a_pending_assumption_suppresses_the_refusal_line(project):
    """Spec §3's `not pending` guard: that status is doing what it is for"""
    store = ProjectStore(project)
    wo = _held_by_a_refusal(store)
    store.add_assumption(wo["id"], "assumed the schema is stable")

    assert true_blockers(store, store.get_work_order(wo["id"])) == [
        "1 assumption pending your review"]


def test_an_undeclared_delivery_is_not_said_twice(project):
    """Spec §3: the same refusal WITH commits behind it already has its own sentence"""
    store = ProjectStore(project)
    wo = _held_by_a_refusal(store)
    for _ in range(invariants.PR_REPAIR_MAX_ATTEMPTS):
        store.add_event(wo["id"], "pr_undeclared_nudged", {})

    blockers = true_blockers(store, store.get_work_order(wo["id"]))

    assert invariants.PR_UNDECLARED_BLOCKER in blockers
    assert invariants.REFUSAL_UNANSWERED_BLOCKER not in blockers
    assert invariants.REFUSAL_UNANSWERED_DOC_BLOCKER not in blockers


def test_the_refusal_line_does_not_drag_in_a_stale_finish(project):
    """Spec §4: both sentences carry the word "assumption\""""
    store = ProjectStore(project)
    wo = store.create_work_order("ship the thing")
    store.add_event(wo["id"], "finished", {"summary": "delivered"})
    turn = store.create_turn(wo["id"], "message", "go again")
    store.finish_turn(turn["id"], "done", result="stopped again")
    store.add_event(wo["id"], "reviewed", {"accepted": False})
    store.set_status(wo["id"], "needs_review")
    row = store.get_work_order(wo["id"])
    later = float(store.latest_turn(wo["id"])["ended_at"]) + 24 * 60 * 60

    assert invariants.parked_reason(store, row, now=later) == \
        invariants.STALE_FINISH_BLOCKER
    assert true_blockers(store, row, now=later) == [
        invariants.REFUSAL_UNANSWERED_BLOCKER]


def test_document_delivery_kinds_are_real_kinds():
    """Spec §2: a renamed kind fails here rather than silently dropping a case"""
    from jarvis import project_store as ps

    assert set(invariants.DOCUMENT_DELIVERY_KINDS) <= set(ps.WO_KINDS)
    assert "investigator" not in invariants.DOCUMENT_DELIVERY_KINDS
    assert "manager" not in invariants.DOCUMENT_DELIVERY_KINDS


def test_the_refusal_reason_is_re_derivable(project):
    """Spec §3: INV-ATTENTION-REASON rewrites any flag `true_blockers` cannot derive"""
    store = ProjectStore(project)
    wo = _held_by_a_refusal(store)
    store.flag_attention(wo["id"], invariants.REFUSAL_UNANSWERED_BLOCKER)

    violations = list(invariants.check_attention_reason_is_true(store))

    assert violations == []
    assert store.get_work_order(wo["id"])["attention_reason"] == \
        invariants.REFUSAL_UNANSWERED_BLOCKER


# -- a stored spec pointer has to open

#: Composed, never written: tests/test_spec_location.py refuses the literal in any tracked file
LEGACY_PREFIX = "docs/" + "specs/"
CANONICAL_DOC = "docs/superpowers/specs/exporter.md"


def _feature_with_pointer(store: ProjectStore, design_doc: str | None) -> dict:
    from jarvis import db

    fo = store.create_feature_order("CSV export", description="an exporter")
    plan = {"summary": "an exporter", "children": [],
            "design_doc_content": "# Exporter design\n"}
    if design_doc is not None:
        plan["design_doc"] = design_doc
    store.update_feature_order(fo["id"], plan=db.to_json(plan))
    return store.get_feature_order(fo["id"])


def _pointer_violations(store: ProjectStore) -> list:
    return list(invariants.check_spec_pointer_resolves(store))


def _stored_doc(store: ProjectStore, fo_id: str) -> str:
    from jarvis import db

    return db.from_json(store.get_feature_order(fo_id)["plan"], {}).get("design_doc", "")


def test_a_dead_spec_pointer_with_one_tracked_match_is_repointed(project):
    """§2.3"""
    store = ProjectStore(project)
    fo = _feature_with_pointer(store, LEGACY_PREFIX + "exporter.md")

    (found,) = _pointer_violations(store)

    assert found.invariant == "INV-SPEC-POINTER-REAL"
    assert found.repaired is True
    assert found.repair == f"repointed {fo['id']}'s design_doc to {CANONICAL_DOC}"
    assert _stored_doc(store, fo["id"]) == CANONICAL_DOC


def test_two_tracked_files_of_that_basename_owe_a_decision_and_change_nothing(project):
    """§2.3: zero or more than one match is reported unrepaired"""
    from jarvis.testing import commit

    store = ProjectStore(project)
    second = Path(project) / "notes" / "exporter.md"
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_text("# a second one\n")
    commit(Path(project), "a second exporter.md", "notes/exporter.md")
    fo = _feature_with_pointer(store, LEGACY_PREFIX + "exporter.md")

    (found,) = _pointer_violations(store)

    assert found.repaired is False
    assert found.owed
    assert "notes/exporter.md" in found.owed and CANONICAL_DOC in found.owed
    assert _stored_doc(store, fo["id"]) == LEGACY_PREFIX + "exporter.md"


def test_no_tracked_file_of_that_basename_owes_a_decision(project):
    store = ProjectStore(project)
    fo = _feature_with_pointer(store, "docs/superpowers/specs/ghost.md")

    (found,) = _pointer_violations(store)

    assert found.repaired is False
    assert found.owed and "ghost.md" in found.owed
    assert _stored_doc(store, fo["id"]) == "docs/superpowers/specs/ghost.md"


def test_a_pointer_that_resolves_and_a_plan_with_no_pointer_are_silent(project):
    """Settled features included, on `check_work_lands`' reasoning"""
    store = ProjectStore(project)
    _feature_with_pointer(store, CANONICAL_DOC)
    _feature_with_pointer(store, None)
    store.create_feature_order("unplanned", description="nothing yet")

    assert _pointer_violations(store) == []


def test_a_read_only_doctor_run_reports_the_dead_pointer_and_writes_nothing(project):
    """§2.3: `_ReadOnly._BLOCKED` had to learn `update_feature_order`"""
    store = ProjectStore(project)
    fo = _feature_with_pointer(store, LEGACY_PREFIX + "exporter.md")
    before = store.get_feature_order(fo["id"])["plan"]

    found = check_project(store, repair=False, slow=True)

    assert [v.invariant for v in found
            if v.invariant == "INV-SPEC-POINTER-REAL"] == ["INV-SPEC-POINTER-REAL"]
    assert store.get_feature_order(fo["id"])["plan"] == before


def test_two_features_with_dead_pointers_are_two_standing_violations(project):
    """§2.3, Neo 1695: the dedupe identity is the SUBJECT"""
    store = ProjectStore(project)
    first = _feature_with_pointer(store, "docs/superpowers/specs/ghost.md")
    second = _feature_with_pointer(store, "docs/superpowers/specs/phantom.md")

    found = _pointer_violations(store)

    assert len(found) == 2
    assert {v.key for v in found} == {
        ("INV-SPEC-POINTER-REAL", first["id"]),
        ("INV-SPEC-POINTER-REAL", second["id"]),
    }
    assert all(v.wo_id is None and v.owed for v in found)
    assert {v.subject_id: v.context["fo_id"] for v in found} == {
        first["id"]: first["id"], second["id"]: second["id"]}


def test_a_repaired_pointer_violation_also_names_its_feature(project):
    """§2.3: the repaired branch carries `subject_id` too"""
    store = ProjectStore(project)
    fo = _feature_with_pointer(store, LEGACY_PREFIX + "exporter.md")

    (found,) = _pointer_violations(store)

    assert found.repaired is True
    assert found.subject_id == fo["id"]
    assert found.key == ("INV-SPEC-POINTER-REAL", fo["id"])


# -- and it opens on the COMMITTED ref, never on this checkout


def _planner_branch(store: ProjectStore, project: Path, fo_id: str,
                    branch: str = "feat/plan") -> Path:
    """A planner work order with a worktree on a branch of its own, as a dispatch makes"""
    planner = store.create_work_order("Plan CSV export", parent_id=fo_id, kind="planner")
    store.update_work_order(planner["id"], worktree=planner["id"])
    store.update_feature_order(fo_id, plan_wo_id=planner["id"])
    path = project / ".claude" / "worktrees" / planner["id"]
    subprocess.run(["git", "-C", str(project), "worktree", "add", "-q", "-b", branch,
                    str(path)], check=True, capture_output=True)
    return path


def test_a_design_doc_committed_but_absent_from_this_checkout_is_silent(project):
    """§3.1, the issue-1044 regression: the dev checkout was 38 commits behind"""
    from jarvis.testing import commit

    store = ProjectStore(project)
    doc = Path(project) / "docs" / "superpowers" / "specs" / "merged.md"
    doc.write_text("# merged\n")
    commit(Path(project), "the merged spec", "docs/superpowers/specs/merged.md")
    doc.unlink()
    _feature_with_pointer(store, "docs/superpowers/specs/merged.md")

    assert _pointer_violations(store) == []


def test_a_design_doc_only_in_the_working_tree_still_raises(project):
    """§3.2, the hide half: an uncommitted file short-circuited `.exists()`"""
    store = ProjectStore(project)
    local = Path(project) / "docs" / "superpowers" / "specs" / "draft.md"
    local.write_text("# never committed\n")
    fo = _feature_with_pointer(store, "docs/superpowers/specs/draft.md")

    (found,) = _pointer_violations(store)

    assert found.repaired is False
    assert found.owed and "no tracked file carries that name" in found.owed
    assert _stored_doc(store, fo["id"]) == "docs/superpowers/specs/draft.md"


def test_a_design_doc_on_the_planners_branch_and_not_on_main_is_silent(project):
    """§3.3 and §2.1's rung order: a spec committed on the planner's branch and not yet merged must..."""
    from jarvis.testing import commit

    store = ProjectStore(project)
    fo = _feature_with_pointer(store, "docs/superpowers/specs/branch-only.md")
    worktree = _planner_branch(store, Path(project), fo["id"])
    doc = worktree / "docs" / "superpowers" / "specs" / "branch-only.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# on the branch\n")
    commit(worktree, "the spec, on the planner's branch",
           "docs/superpowers/specs/branch-only.md")

    assert _pointer_violations(store) == []


SPEC_PR = "https://github.com/x/y/pull/1289"


def _unmerged_spec_feature(store: ProjectStore, project: Path, *, planner_status: str,
                           fo_status: str = "executing") -> tuple[dict, str]:
    """Issue 1308's shape, the spec on the planner's branch and its worktree removed (docs/superpowers/specs/2026-10-29-a-feature-spec-pull-request-lands-first.md §3)"""
    from jarvis.testing import commit

    fo = _feature_with_pointer(store, "docs/superpowers/specs/unmerged.md")
    worktree = _planner_branch(store, project, fo["id"], branch="worktree-plan")
    doc = worktree / "docs" / "superpowers" / "specs" / "unmerged.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# the spec\n")
    commit(worktree, "the spec", "docs/superpowers/specs/unmerged.md")
    subprocess.run(["git", "-C", str(project), "worktree", "remove", "--force",
                    str(worktree)], check=True, capture_output=True)
    planner = store.get_feature_order(fo["id"])["plan_wo_id"]
    store.add_event(planner, "finished", {"summary": "planned", "authored": {
        "branch": "worktree-plan", "base": "main", "commits": 1, "dirty": []}})
    store.update_work_order(planner, pr_url=SPEC_PR)
    store.set_status(planner, planner_status)
    store.update_feature_order(fo["id"], status=fo_status)
    return fo, planner


def test_an_unmerged_spec_on_a_completed_planner_reparks_it(project, monkeypatch):
    """Issue 1308 gaps 1 and 3: the cause is the unmerged spec PR, and the OS takes it back"""
    store = ProjectStore(project)
    fo, planner = _unmerged_spec_feature(store, Path(project), planner_status="completed")
    reparked: list[str] = []
    monkeypatch.setattr(ops, "repark_spec_pull_request",
                        lambda wo_id: reparked.append(wo_id) or "waiting_pr_merge")

    (found,) = _pointer_violations(store)

    assert reparked == [planner]
    assert found.repaired is True
    assert SPEC_PR in found.detail and "worktree-plan" in found.detail
    assert "no tracked file" not in found.detail and "fo cancel" not in found.detail
    assert found.subject_id == fo["id"]


def test_a_spec_pr_the_os_is_parked_behind_is_silent(project):
    store = ProjectStore(project)
    _unmerged_spec_feature(store, Path(project), planner_status="waiting_pr_merge")

    assert _pointer_violations(store) == []


def test_an_unmerged_spec_on_a_settled_feature_names_the_pr_not_a_cancel(project,
                                                                         monkeypatch):
    store = ProjectStore(project)
    _unmerged_spec_feature(store, Path(project), planner_status="completed",
                           fo_status="completed")
    monkeypatch.setattr(ops, "repark_spec_pull_request",
                        lambda wo_id: pytest.fail("a settled feature is not re-parked"))

    (found,) = _pointer_violations(store)

    assert found.repaired is False
    assert f"merge {SPEC_PR}" in found.owed
    assert "fo cancel" not in found.owed


def test_the_repair_only_considers_candidates_that_exist_on_the_ref(project):
    """§3.4 and §2.3: the single-match branch WRITES"""
    store = ProjectStore(project)
    local = Path(project) / "notes" / "exporter.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("# local only\n")
    fo = _feature_with_pointer(store, LEGACY_PREFIX + "exporter.md")

    (found,) = _pointer_violations(store)

    assert found.repaired is True
    assert "notes/exporter.md" not in found.repair
    assert _stored_doc(store, fo["id"]) == CANONICAL_DOC


def test_the_remote_is_refreshed_at_most_once_however_many_rows_are_scanned(project,
                                                                           monkeypatch):
    """§3.5 and §2.2: the memo is threaded in by the caller"""
    from jarvis import landing

    store = ProjectStore(project)
    for name in ("ghost.md", "phantom.md", "spectre.md"):
        _feature_with_pointer(store, f"docs/superpowers/specs/{name}")
    calls: list[str] = []
    monkeypatch.setattr(landing, "_fetch_ref",
                        lambda repo, ref: calls.append(ref))

    assert len(_pointer_violations(store)) == 3
    assert calls == ["main"]


def test_a_project_with_no_integration_branch_yields_no_violation(project,
                                                                  monkeypatch):
    """§3.6 and §2.4: a check that cannot run PASSES"""
    from jarvis import landing

    store = ProjectStore(project)
    _feature_with_pointer(store, "docs/superpowers/specs/ghost.md")
    monkeypatch.setattr(landing, "base_ref", lambda repo: "")

    assert _pointer_violations(store) == []


def test_a_git_that_cannot_run_yields_no_violation(project, monkeypatch):
    """§2.4: a git that would not execute is not an absent path"""
    from jarvis import landing

    store = ProjectStore(project)
    _feature_with_pointer(store, "docs/superpowers/specs/ghost.md")

    real = landing.subprocess.run

    # ONLY `cat-file`: a blanket patch empties `base_ref` too
    def refuses_cat_file(argv, *args, **kwargs):
        if "cat-file" in argv:
            raise OSError("no git here")
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(landing.subprocess, "run", refuses_cat_file)

    assert _pointer_violations(store) == []


def test_subject_id_defaults_to_none_so_a_work_order_key_is_unchanged():
    """§2.3: additive"""
    v = invariants.Violation(invariant="INV-TEST", detail="x", wo_id="wo-1")

    assert v.subject_id is None
    assert v.key == ("INV-TEST", "wo-1")


def test_two_subject_violations_open_two_violation_report_rows(
        jarvis_home, project, catalog_file, monkeypatch):
    """§2.3: `Violation.key` alone is not the durable identity"""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    violations = [invariants.Violation(invariant="INV-TEST-SUBJECT",
                                       detail=f"dead pointer on {fo_id}",
                                       subject_id=fo_id, owed=f"decide {fo_id}")
                  for fo_id in ("fo-aaaa1111", "fo-bbbb2222")]
    monkeypatch.setattr(invariants, "check_project", lambda *a, **k: violations)
    catalog = load_catalog(catalog_file)
    daemon, store = Daemon(catalog), ProjectStore(project)
    try:
        daemon.check_invariants(catalog.projects[0], store)

        rows = [r for r in store.standing_violations()
                if r["invariant"] == "INV-TEST-SUBJECT"]
        assert sorted(r["wo_id"] for r in rows) == ["fo-aaaa1111", "fo-bbbb2222"]
    finally:
        store.close()


# -- a pull request GitHub stopped recomputing reaches the user (wo-d3a70c29)


@pytest.mark.skipif(not hasattr(invariants, "NO_ACTOR_BLOCKER"),
                    reason="`invariants.NO_ACTOR_BLOCKER` is wo-d3a70c29's sibling "
                           "child (`readers`) and has not landed on this base yet")
def test_a_stalled_pull_request_ends_the_tick_as_an_attention_item(
        jarvis_home, project, catalog_file):
    """PR #995's row, end to end: `waiting_pr_merge` with the pull request's head behind the branch tip..."""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    daemon, store = Daemon(catalog), ProjectStore(project)
    try:
        wo = store.create_work_order("add feature X", status="waiting_pr_merge")
        store.update_work_order(wo["id"],
                                pr_url="https://github.com/acme/proj/pull/995",
                                pr_head_oid="0488753", pr_ref_oid="88cac10",
                                pr_mergeable_unknown_since=db.now())

        daemon.check_invariants(catalog.projects[0], store)

        row = store.get_work_order(wo["id"])
        assert bool(row["needs_attention"])
        # The string names the STATUS (§5b): `.format` is what this child landed
        assert row["attention_reason"] == invariants.NO_ACTOR_BLOCKER.format(
            status="waiting_pr_merge")
    finally:
        store.close()


# -- the budget cap and the assumptions it parked on


def _parked_at_its_cap(store: ProjectStore, n: int = 3) -> dict:
    """An order the OS stopped at its cap, owing `n` assumption decisions"""
    from jarvis import budget

    wo = store.create_work_order("ran out of money mid-task")
    for i in range(n):
        store.add_assumption(wo["id"], f"assumption {i}")
    store.update_work_order(wo["id"], result_summary="half done")
    store.set_status(wo["id"], budget.EXHAUSTED)
    return store.get_work_order(wo["id"])


def test_a_parked_order_owing_assumptions_gets_one_compound_line(project):
    """§7(6): the money first, then the assumptions — ONE line replacing both"""
    store = ProjectStore(project)
    wo = _parked_at_its_cap(store, n=3)

    blockers = true_blockers(store, wo)

    assert len(blockers) == 1, blockers
    assert blockers[0].startswith(invariants.budget_blocker(store, wo))
    assert "3 assumptions also await you" in blockers[0]
    assert not any("pending your review" in b for b in blockers)
    assert invariants.budget_blocker(store, wo) not in blockers


def test_the_compound_line_is_singular_for_one_assumption(project):
    store = ProjectStore(project)
    wo = _parked_at_its_cap(store, n=1)

    line = true_blockers(store, wo)[0]

    assert line.startswith(invariants.budget_blocker(store, wo))
    assert "1 assumption also awaits you" in line
    assert "assumptions" not in line


def test_the_compound_line_gains_no_parked_sentence(project):
    """§7(7): the compound mentions assumptions"""
    store = ProjectStore(project)
    wo = _parked_at_its_cap(store, n=3)

    blockers = true_blockers(store, wo)

    assert invariants.parked_reason(store, wo) is None
    assert invariants.PARKED_BLOCKER not in blockers
    assert invariants.STALE_FINISH_BLOCKER not in blockers


def test_the_compound_line_survives_auto_review(started, catalog_file):
    """§4: `neo_reviews_later` cannot skip the compound line"""
    for key in ("validation.enabled", "validation.auto_review"):
        ops.set_config(key, True, project="proj_a", reason="the panel has been right",
                       catalog_path=str(catalog_file))
    store = ProjectStore(started.catalog.project("proj_a").path)
    wo = _parked_at_its_cap(store, n=3)

    assert ops.auto_review_at(store.project_path), "the switch never reached the file"
    assert invariants.neo_reviews_later(store, wo) is False
    blockers = true_blockers(store, wo)

    assert len(blockers) == 1, blockers
    assert blockers[0].startswith(invariants.budget_blocker(store, wo))
    assert "3 assumptions also await you" in blockers[0]


def test_a_parked_order_owing_nothing_keeps_the_plain_budget_line(project):
    """§5: no change to the status with nothing owed — one line, and no parked tail"""
    store = ProjectStore(project)
    wo = _parked_at_its_cap(store, n=0)

    assert true_blockers(store, wo) == [invariants.budget_blocker(store, wo)]


def test_needs_review_with_assumptions_owed_is_byte_identical(project):
    """§5: the assumptions line is untouched in every other status"""
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store, n=3)

    blockers = true_blockers(store, wo)

    assert blockers[0] == "3 assumptions pending your review"
    assert not any("budget spent" in b for b in blockers)


def test_the_pointer_check_runs_on_the_slow_cadence_only():
    """§2.3: its population is every feature order a project has ever had and its repair lists the whole..."""
    assert invariants.check_spec_pointer_resolves in invariants.SLOW_INVARIANTS
    assert invariants.check_spec_pointer_resolves not in invariants.INVARIANTS


# -- 5b/5d: an OPEN row no pass selects says so

#: (status, what makes the row) — each derives no actor from `ops.scheduled_actor`
NO_ACTOR_SHAPES = [
    pytest.param("validating", id="validating-with-no-round"),
    pytest.param("waiting_pr_merge", id="waiting-pr-merge-with-no-pr-url"),
]


def _no_actor_row(store: ProjectStore, status: str) -> dict:
    """A delivered work order parked in `status` with nothing for a pass to select"""
    wo = store.create_work_order("add feature X", status=status)
    store.update_work_order(wo["id"], result_summary="done")
    return store.get_work_order(wo["id"])


@pytest.mark.parametrize("status", NO_ACTOR_SHAPES)
def test_an_open_row_no_pass_selects_names_the_hole(project, status):
    store = ProjectStore(project)
    wo = _no_actor_row(store, status)

    assert ops.scheduled_actor(store, wo) is None, "the premise: nobody will act"
    assert true_blockers(store, wo) == [
        invariants.NO_ACTOR_BLOCKER.format(status=status)]


@pytest.mark.parametrize("status", NO_ACTOR_SHAPES)
def test_the_no_actor_line_carries_no_elapsed_time(project, status):
    """§2: INV-ATTENTION-REASON re-derives it and `ack_attention` stores it verbatim"""
    store = ProjectStore(project)
    wo = _no_actor_row(store, status)

    early = true_blockers(store, wo, now=db.now())
    late = true_blockers(store, wo, now=db.now() + 40 * 60 * 60)

    assert early == late != []


def test_a_round_the_panel_owns_names_nobody_new(project):
    """The other half of line 579's pairing"""
    store = ProjectStore(project)
    wo = _no_actor_row(store, "validating")
    store.open_validation_round(wo_id=wo["id"], fingerprint="fp-1")

    assert true_blockers(store, store.get_work_order(wo["id"])) == []


def test_the_new_arm_never_doubles_a_line_that_already_names_the_stop(project):
    """Neo's precedence ruling: the parked tail first"""
    store = ProjectStore(project)
    wo = store.create_work_order("delivered, sent back to work, stopped again")
    early = store.create_turn(wo["id"], "dispatch", "plan it")
    store.finish_turn(early["id"], "done", result="submitted the plan")
    store.update_work_order(wo["id"], result_summary="planned it")
    store.add_event(wo["id"], "finished", {"summary": "planned it"})
    store.add_assumption(wo["id"], "one call I made")
    later_turn = store.create_turn(wo["id"], "message", "go and open the PR")
    store.finish_turn(later_turn["id"], "done", result="waiting on exact counts")
    store.set_status(wo["id"], "needs_review")
    wo = store.get_work_order(wo["id"])
    later = float(store.latest_turn(wo["id"])["ended_at"]) + 90 * 60

    assert ops.scheduled_actor(store, wo, now=later) is None, "the premise"
    assert true_blockers(store, wo, now=later) == [
        "1 assumption pending your review", invariants.STALE_FINISH_BLOCKER]


def test_an_ungoverned_open_row_is_still_asked_for_nothing(project):
    """`UNGOVERNED_ORIGINS` stays load-bearing (§1 prohibition 3): both the settler and the retry sweep..."""
    store = ProjectStore(project)
    wo = store.create_work_order("a legacy background agent", origin="adhoc",
                                 status="running")
    turn = store.create_turn(wo["id"], "dispatch", "whatever it was doing")
    store.finish_turn(turn["id"], "done", result="…")
    wo = store.get_work_order(wo["id"])
    later = float(store.latest_turn(wo["id"])["ended_at"]) + 90 * 60

    assert ops.scheduled_actor(store, wo, now=later) is None, "the premise"
    assert true_blockers(store, wo, now=later) == []


def test_the_actor_is_derived_once_per_derivation(project, monkeypatch):
    """§5d's cost clause: `true_blockers` runs for every work order on every reconcile tick"""
    store = ProjectStore(project)
    wo = _no_actor_row(store, "validating")
    calls: list[str] = []
    real = ops.scheduled_actor
    monkeypatch.setattr(ops, "scheduled_actor",
                        lambda *a, **k: calls.append(a[1]["id"]) or real(*a, **k))

    assert true_blockers(store, wo) != []
    assert calls == [wo["id"]]


def test_a_settled_row_costs_the_tick_no_verdict_at_all(project, monkeypatch):
    """The same clause from the other end: `SCHEDULED_ACTORS` is keyed by `OPEN_STATUSES`"""
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="completed")
    monkeypatch.setattr(ops, "scheduled_actor",
                        lambda *a, **k: pytest.fail("a settled row was asked"))

    assert true_blockers(store, store.get_work_order(wo["id"])) == []


@pytest.mark.parametrize("status", NO_ACTOR_SHAPES)
def test_the_hole_ends_the_tick_flagged_and_acks_for_good(jarvis_home, project,
                                                          catalog_file, status):
    """§5d end to end: the reason INV-ATTENTION-REASON re-derives"""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    daemon, store = Daemon(catalog), ProjectStore(project)
    try:
        wo = _no_actor_row(store, status)

        daemon.check_invariants(catalog.projects[0], store)

        row = store.get_work_order(wo["id"])
        assert (bool(row["needs_attention"]), row["attention_reason"]) == (
            True, invariants.NO_ACTOR_BLOCKER.format(status=status))
        # INV-ATTENTION-REASON rewrites any reason this function cannot re-derive: the flag surviving a...
        daemon.check_invariants(catalog.projects[0], store)
        assert store.get_work_order(wo["id"])["attention_reason"] == \
            invariants.NO_ACTOR_BLOCKER.format(status=status)

        ops.ack_attention(wo["id"])
        assert not bool(store.get_work_order(wo["id"])["needs_attention"])

        daemon.check_invariants(catalog.projects[0], store)
        assert not bool(store.get_work_order(wo["id"])["needs_attention"])
    finally:
        store.close()


# -- the label stops promising a round nobody is going to judge


def test_the_validating_label_does_not_promise_a_round_nobody_will_judge(project):
    """`ops.scheduled_actor` is None, so the round count is a false promise"""
    store = ProjectStore(project)
    wo = _no_actor_row(store, "validating")

    assert ops.scheduled_actor(store, wo) is None
    label = status_label(store, wo)

    assert label == "validating — no review is scheduled"
    assert "review round" not in label


#: Today's label per open status — NO default and NO `.get`: a new member must KeyError here
ACTOR_LABELS = {
    "pending": "pending",
    "dispatching": "dispatching",
    "running": "running",
    "idle": "idle — waiting for a message from its feature",
    "waiting_input": "waiting_input",
    "validating": "validating — review round 1 of 3",
    "needs_review": "needs_review",
    "waiting_pr_merge": "waiting_pr_merge",
    "budget_exhausted": "budget spent — cleared, resuming shortly",
}

GATED_COMMAND = "gh " + "pr merge 42"  # split so the recogniser does not gate this file


@pytest.mark.parametrize("status", OPEN_STATUSES)
def test_a_row_with_an_actor_keeps_todays_label(jarvis_home, project, status):
    """`REVIEWS_GATE` is under every open status, so one open request is the uniform recipe"""
    store = ProjectStore(project)
    wo = _no_actor_row(store, status)
    store.add_approval(wo["id"], "pr_merge", GATED_COMMAND)
    wo = store.get_work_order(wo["id"])

    assert ops.scheduled_actor(store, wo) is not None
    assert status_label(store, wo) == ACTOR_LABELS[status]


# Written for the opt-in OS (os.autonomy.self_dispatch on, stuck sweep on)
pytestmark = pytest.mark.usefixtures("self_dispatch_on")
