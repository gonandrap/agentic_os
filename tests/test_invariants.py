"""Post-conditions — the OS checking that its own state still means what it says.

The regression these exist for is reconstructed verbatim in
`test_idle_notification_no_longer_clobbers_the_real_reason` and
`test_repairs_the_state_the_live_bug_left_behind`: a work order settles correctly into
`needs_review` with "assumptions pending review", and ~90s later Claude Code's routine
idle Notification overwrites the reason with "Claude is waiting for your input". Two
live work orders shipped to the dashboard that way, both telling the user they were
blocked on a question that did not exist.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from jarvis import cli, invariants, ops, release, worker_session
from jarvis.catalog import DEFAULT_VALIDATION_TIMEOUT
from jarvis.hooks import handle_hook
from jarvis.invariants import (
    BLOCKED_STATUSES,
    IDLE_NO_FINISH_BLOCKER,
    PR_CLOSED_BLOCKER,
    VALIDATION_STUCK_BLOCKER,
    check_project,
    status_label,
    true_blockers,
)
from jarvis.project_store import ProjectStore

IDLE_NOTIFICATION = "Claude is waiting for your input"


def _settled_with_assumptions(store: ProjectStore, n: int = 2) -> dict:
    """A work order in the state the reconciler leaves a finished worker in."""
    wo = store.create_work_order("status of this project")
    for i in range(n):
        store.add_assumption(wo["id"], f"assumption {i}")
    store.update_work_order(wo["id"], result_summary="done")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], "assumptions pending review")
    return store.get_work_order(wo["id"])


# -- the derivation everything is checked against ------------------------------------


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


# -- the root-cause fix ---------------------------------------------------------------


def test_idle_notification_no_longer_clobbers_the_real_reason(project):
    """The bug, at the hook layer: the idle prompt must not touch a settled order."""
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
    """The guard must not swallow the case the hook exists for."""
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


# -- the invariants (defence in depth: they catch it however it happens) --------------


def test_repairs_the_state_the_live_bug_left_behind(project):
    """Whatever clobbers the reason, the next tick puts it right."""
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
    """Only assumptions are enforced: a hook reason is more specific than anything
    derivable, and overwriting it would repeat the bug in the other direction."""
    store = ProjectStore(project)
    wo = store.create_work_order("running work")
    store.set_status(wo["id"], "waiting_input")
    store.flag_attention(wo["id"], "Claude needs permission to run npm test")

    assert check_project(store, repair=True) == []
    assert store.get_work_order(wo["id"])["attention_reason"] == (
        "Claude needs permission to run npm test")


def _finished_with_a_gate_still_pending(store: ProjectStore, escalated: bool = True):
    """The state wo-52a6164d ended in: shipped, completed, still asking permission."""
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
    """A gate is a control on something about to happen. Once the work order is over
    there is no worker left to run the command, so an approval could permit nothing and
    a denial could stop nothing — but the user was still being asked."""
    store = ProjectStore(project)
    wo, approval = _finished_with_a_gate_still_pending(store)

    violations = check_project(store, repair=True)

    # The gate is closed first, so the attention sweep that follows sees the truth.
    assert [v.invariant for v in violations] == ["INV-GATE-ORPHAN",
                                                 "INV-ATTENTION-PHANTOM"]
    assert violations[0].repaired
    closed = store.get_approval(approval["id"])
    assert closed["status"] == "expired"
    assert closed["decided_by"] == "os"
    assert store.pending_approvals(wo["id"]) == []
    # ...and with the blocker gone, the attention flag goes down with it.
    assert true_blockers(store, store.get_work_order(wo["id"])) == []
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_closing_an_orphan_gate_records_no_verdict(project):
    """Nobody authorised anything. The record must not be readable as if they had."""
    store = ProjectStore(project)
    _, approval = _finished_with_a_gate_still_pending(store)

    check_project(store, repair=True)

    closed = store.get_approval(approval["id"])
    assert closed["status"] not in ("approved", "denied", "dismissed")
    # A dismissal would also have inflated the classifier false-positive count.
    assert store.dismissed_count() == 0


def test_an_orphan_gate_is_reported_but_not_closed_in_reporting_mode(project):
    store = ProjectStore(project)
    _, approval = _finished_with_a_gate_still_pending(store)

    violations = check_project(store, repair=False)

    assert "INV-GATE-ORPHAN" in [v.invariant for v in violations]
    assert store.get_approval(approval["id"])["status"] == "pending"


def test_a_gate_pending_on_live_work_is_left_alone(project):
    """The whole point of the gate: while the worker is still there, the question is
    real and must keep waiting for an answer."""
    store = ProjectStore(project)
    wo = store.create_work_order("ship it")
    approval = store.add_approval(wo["id"], "release", "scripts/deploy.sh 0.5.4")
    store.set_status(wo["id"], "waiting_input")

    check_project(store, repair=True)

    assert store.get_approval(approval["id"])["status"] == "pending"


def test_phantom_attention_on_a_finished_order_is_cleared(project):
    """The 'I acked it and it is still in my face' case."""
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
    """The dangerous direction: pending work that never asks for the user."""
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
    """'Assumption recorded' in the timeline is a claim about a write. Check the write."""
    store = ProjectStore(project)
    wo = store.create_work_order("records an assumption")
    store.add_assumption(wo["id"], "the assumption that went missing")
    # Simulate the row being lost while the event survives.
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
    """"Needs you" with no reason is the fastest way to teach an operator to ignore
    the attention strip."""
    store = ProjectStore(project)
    wo = store.create_work_order("blocked on the user")
    store.set_status(wo["id"], "waiting_input")
    store.update_work_order(wo["id"], needs_attention=1, attention_reason="")

    violations = check_project(store, repair=True)

    assert {v.invariant for v in violations} == {"INV-ATTENTION-BLANK"}
    assert store.get_work_order(wo["id"])["attention_reason"] == (
        "worker is waiting on your input")


def test_a_blank_reason_hiding_pending_assumptions_names_them(project):
    """Blank *and* assumptions pending: the stale-reason invariant is the one that
    fires, and it names the actionable thing. Either route must end in a true reason."""
    store = ProjectStore(project)
    wo = _settled_with_assumptions(store, n=1)
    store.update_work_order(wo["id"], attention_reason="")

    check_project(store, repair=True)

    assert store.get_work_order(wo["id"])["attention_reason"] == (
        "1 assumption pending your review")


def test_a_broken_invariant_does_not_hide_the_others(project, monkeypatch):
    """One bad check must not take the checker down — it is the last line of defence."""
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


# -- surfaces -------------------------------------------------------------------------


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
    """An invariant whose violation the test turns on and off, carrying NO work order —
    the INV-HEALTH-SWEEP-MUTE shape, which has no timeline to remember anything on."""
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
    """The reported bug: every release restarted jarvis.service and the whole standing
    unrepaired set went to Telegram again."""
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
    """Absence on a tick that ran only the fast checks means "nobody looked", not
    "fixed" — `LANDING_SWEEP_EVERY_TICKS` is 720, so closing on it would re-announce
    INV-WORK-LANDED hourly: the bug with a slower clock."""
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
    """The 63 stale agents the old transport leaked are surfaced, never auto-stopped.

    They live in the user's own agents view, so bulk-killing them is theirs to
    authorise — doctor lists them with the exact command instead.
    """
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


# -- the validation panel ------------------------------------------------------------
#
# Two rules, and they pull in opposite directions: a round in flight must cost the user
# nothing, and a round that gave up must reach them. Both are asserted in one call
# sequence below, because a branch that was never written passes the first half alone.


def test_a_validating_work_order_is_silent_until_the_panel_gives_up(project):
    """The pairing IS the test.

    Assert only the `validating` half and it passes against an implementation where the
    escalation branch was never written — nothing raises attention, and nothing is
    supposed to. So the same work order is walked from an open round to an escalated
    one, and the blocker has to appear exactly once it does.
    """
    store = ProjectStore(project)
    wo = store.create_work_order("ship the thing")
    store.update_work_order(wo["id"], result_summary="done")
    store.set_status(wo["id"], "validating")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="abc")

    # a round in flight: the OS is working, and nobody owes anybody a decision
    assert true_blockers(store, store.get_work_order(wo["id"])) == []
    assert "validating" not in BLOCKED_STATUSES

    # ...and the panel still deliberating, with the work order parked for review, is
    # likewise not the user's problem yet
    store.set_status(wo["id"], "needs_review")
    assert true_blockers(store, store.get_work_order(wo["id"])) == [
        IDLE_NO_FINISH_BLOCKER]

    # the panel gives up: now it is
    store.close_validation_round(rnd["id"], "escalated", reason="three rounds, no deal")
    assert true_blockers(store, store.get_work_order(wo["id"])) == [
        VALIDATION_STUCK_BLOCKER]


def test_a_closed_pull_request_outranks_an_escalated_review(project):
    """Both branches live under the same `needs_review` roof, and the order matters: a
    pull request shut without merging is a fact about the outside world, and it is the
    thing the user has to act on whatever the panel thought."""
    store = ProjectStore(project)
    wo = store.create_work_order("ship the thing")
    store.set_status(wo["id"], "needs_review", pr_state="CLOSED")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="abc")
    store.close_validation_round(rnd["id"], "escalated")

    assert true_blockers(store, store.get_work_order(wo["id"])) == [PR_CLOSED_BLOCKER]


def test_the_validating_label_says_which_round_the_work_is_on(project):
    """`status_label` early-returns for every status that is not `pending`, and
    `validating` is in ACTIVE_STATUSES besides — so this branch is only reachable from
    the very top of the function, and a label written anywhere else is dead code."""
    store = ProjectStore(project)
    wo = store.create_work_order("ship the thing")
    store.set_status(wo["id"], "validating")
    for fp in ("first try", "second try"):
        store.open_validation_round(wo_id=wo["id"], fingerprint=fp)

    label = status_label(store, store.get_work_order(wo["id"]))

    assert label == "validating — review round 2 of 3"


# -- INV-VALIDATION-STRANDED ---------------------------------------------------------
#
# Nothing outside the daemon moves a `validating` unit, so a daemon that dies mid-round
# leaves a work order nobody will ever look at again.


def _stranded(store: ProjectStore, *, age: float, outcome: str = "pending",
              feature: bool = False) -> tuple[str, dict]:
    """A unit parked in `validating` on a round opened `age` seconds ago.

    The timestamp is FABRICATED. The threshold is twice `os.validation.timeout`, which
    is 600s at the shipped default, and a test that waited for it would be a ten-minute
    test.
    """
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
                       (time.time() - age, rnd["id"]))
    return unit["id"], rnd


def _stranded_violations(store: ProjectStore, repair: bool = True) -> list:
    return [v for v in check_project(store, repair=repair)
            if v.invariant == "INV-VALIDATION-STRANDED"]


def _round(store: ProjectStore, rnd: dict) -> dict:
    """Re-read a round, insisting it is still there — the repair closes rounds, it never
    deletes them, and a `None` here would otherwise read as a passing assertion."""
    fresh = store.get_validation_round(rnd["id"])
    assert fresh is not None, f"round {rnd['id']} vanished"
    return fresh


def test_a_round_left_pending_past_twice_its_timeout_is_stranded(project):
    """The pairing IS the test. A round only a single timeout old is LATE, not
    abandoned — one timeout is what a round is allowed to take — and firing on it would
    close a review that was about to come back. Assert only the stale half and an
    implementation with no threshold at all passes."""
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
    """`failed`, never `escalated`: `counted_validation_rounds` ignores a failed round,
    so an interrupted one costs the submitter nothing, and `Daemon.validation_tick`
    picks up `pending` AND `failed` rounds — so closing it is what puts the work order
    back in front of the machinery that dropped it."""
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
    """Reporting mode is READ-ONLY, and this invariant is the one with the most to lose
    from it: describing a stranded round must never be the thing that ends it."""
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
    """Since issue 212 a round runs beside the user's assumption review, so the work
    order holding it is parked in `needs_review`. PAIRED with a cancelled one, which is
    the reason the query is bounded by `OPEN_STATUSES` rather than not bounded at all:
    reopening that round would judge — and land — work the user stopped."""
    store = ProjectStore(project)
    parked, parked_round = _stranded(store, age=5000)
    store.set_status(parked, "needs_review")
    stopped, stopped_round = _stranded(store, age=5000)
    store.set_status(stopped, "cancelled")

    found = _stranded_violations(store)

    assert [v.wo_id for v in found] == [parked]
    assert _round(store, parked_round)["outcome"] == "failed"
    assert _round(store, stopped_round)["outcome"] == "pending"
    # ...and the parked one keeps the status that says the user owes a decision.
    assert store.get_work_order(parked)["status"] == "needs_review"


def test_a_judged_round_is_never_stranded_however_old(project):
    """A round that reached a verdict is finished business. The unit sitting in
    `validating` after one is a different bug and not this one's to repair."""
    store = ProjectStore(project)
    _stranded(store, age=5000, outcome="rejected")

    assert _stranded_violations(store) == []


def test_a_feature_order_in_validating_is_covered_too(project):
    """Nothing sets a feature order to `validating` yet — the loop that will is a
    sibling work order. An invariant that silently covered only half the units would be
    worse than one that covered none: the half it missed would LOOK checked.

    Paired with a healthy feature order, because "no violation for the fresh one" is
    the only thing that separates a real predicate from one that never fires."""
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
    """The number decides a WRITE, so it is read from the LIVE catalog rather than the
    shipped default: a project that raised `os.validation.timeout` must not have its
    perfectly healthy long rounds closed out from under it by a checker still using
    300s. Paired with the same round at the default, which must be repaired — otherwise
    "no violation" is indistinguishable from a predicate that never fires."""
    from types import SimpleNamespace

    store = ProjectStore(project)
    _, rnd = _stranded(store, age=2 * DEFAULT_VALIDATION_TIMEOUT + 60)

    # `_validation_timeout` imports it inside the call, so patching the module
    # attribute is what reaches it.
    monkeypatch.setattr(ops, "validation_config",
                        lambda: SimpleNamespace(timeout=3600))
    assert _stranded_violations(store) == []
    assert _round(store, rnd)["outcome"] == "pending"

    monkeypatch.undo()
    assert len(_stranded_violations(store)) == 1
    assert _round(store, rnd)["outcome"] == "failed"


def test_an_unreadable_catalog_falls_back_to_the_shipped_timeout(monkeypatch):
    """A worker's checkout whose catalog has moved answers None, and an invariant must
    never be the thing that raises. The default is what it falls back to."""
    from jarvis.invariants import _validation_timeout

    monkeypatch.setattr(ops, "validation_config", lambda: None)

    assert _validation_timeout() == DEFAULT_VALIDATION_TIMEOUT


# -- INV-VALIDATION-STRANDED, second arm: a PASSED round that never landed -----------
#
# Spec docs/superpowers/specs/2026-10-09-a-passed-round-that-never-landed-is-stranded.md.
# The daemon defers the landing of a passed round while a worker turn is in flight and
# delegates it to `settle_work_order`, which returns early for `validating` — so the
# landing lives in one control-flow path and nowhere in the state.


def _passed(store: ProjectStore, *, age: float = 10) -> tuple[str, dict]:
    """A `validating` work order whose latest round PASSED and which never landed.

    Carries a `pr_url`, so the expected landing is `waiting_pr_merge` — the helper above
    writes a `result_summary` and no pull request, which would land `completed`.
    """
    wo_id, rnd = _stranded(store, age=age, outcome="passed")
    store.update_work_order(wo_id, pr_url="https://github.com/o/r/pull/7")
    return wo_id, rnd


def test_a_passed_round_that_never_landed_is_stranded(project):
    """No staleness threshold of its own: once no turn is in flight there is nothing
    left in the OS that could ever move it, so the state is already the defect. ONE
    `check_project(repair=True)` lands it, and the arm reports once — the landing takes
    the order out of `validating`, so conjunct 1 fails on the next tick."""
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
    """`land_when_cleared` reaches `CentralStore.mark_backlog`, which no proxy over the
    PROJECT store can intercept — so reporting mode skips the call outright rather than
    relying on `_ReadOnly`."""
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
    """The two arms are separate predicates over separate outcomes. A round one timeout
    old is LATE, not abandoned, and the new arm must not be the thing that fires on
    it — paired with a passed round, so "nothing reported" cannot pass by accident."""
    store = ProjectStore(project)
    late, late_round = _stranded(store, age=DEFAULT_VALIDATION_TIMEOUT)
    landed, _ = _passed(store)

    found = _stranded_violations(store)

    assert [v.wo_id for v in found] == [landed]
    assert _round(store, late_round)["outcome"] == "pending"
    assert store.get_work_order(late)["status"] == "validating"


def test_a_passed_round_under_a_live_worker_turn_is_not_stranded(project):
    """The deferral is CORRECT there: landing writes a status under a worker that is
    typing, off a row minutes stale. An order whose turn is in flight is waiting
    correctly, not stranded."""
    store = ProjectStore(project)
    wo_id, _ = _passed(store)
    store.create_turn(wo_id, "message", "go")
    assert worker_session.busy(store, wo_id) is not None

    assert _stranded_violations(store) == []
    assert store.get_work_order(wo_id)["status"] == "validating"


def test_a_passed_round_already_landed_is_not_stranded(project):
    """The guard, not construction: an order the settler already landed for this round
    can be back in `validating` — `land_when_cleared` returns there for an open round,
    and a later turn re-opens nothing — and landing it twice is what the event pair
    exists to prevent."""
    store = ProjectStore(project)
    wo_id, rnd = _passed(store)
    store.add_event(wo_id, "validation_landed", {"round_id": int(rnd["id"])})

    assert _stranded_violations(store) == []
    assert store.get_work_order(wo_id)["status"] == "validating"


# -- INV-PROD-CLEAN: production is what the tag says it is ---------------------------
#
# Issue #202 lived for nine releases because its only symptom was a version string.
# "Reproduce in prod, fix it in dev, ship it" is only trustworthy while the production
# checkout is byte-identical to its tag, and nothing was checking.


def _prod_checkout(root: Path, tag: str | None = "jarvis-0.9.0") -> Path:
    """A deployed production checkout: `$PRODUCTION_CODE/jarvis_os`, on a tag, clean."""
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
    """The live defect: a bare `uv` command re-resolves and rewrites uv.lock in place."""
    prod = _prod_checkout(tmp_path)
    (prod / "uv.lock").write_text('name = "jarvis-os"\nversion = "0.1.1"\n')

    found = _prod_violations(tmp_path, monkeypatch)
    assert len(found) == 1
    assert found[0].invariant == "INV-PROD-CLEAN"
    assert found[0].context["paths"] == ["uv.lock"]
    assert "uv.lock" in found[0].detail


def test_untracked_files_are_not_drift(tmp_path, monkeypatch):
    """`.venv/` and `.jarvis/` live in that checkout by design, and the deploy's
    `git checkout -f` never removed them either."""
    prod = _prod_checkout(tmp_path)
    (prod / ".venv").mkdir()
    (prod / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")

    assert _prod_violations(tmp_path, monkeypatch) == []


def test_a_machine_with_no_production_deployment_raises_nothing(tmp_path, monkeypatch):
    """Every dev checkout runs `jarvis doctor` too."""
    assert _prod_violations(tmp_path / "nothing-here", monkeypatch) == []


def test_a_production_path_that_is_not_a_checkout_raises_nothing(tmp_path, monkeypatch):
    (tmp_path / "jarvis_os").mkdir()
    assert _prod_violations(tmp_path, monkeypatch) == []


def test_staged_drift_is_reported_too(tmp_path, monkeypatch):
    """`git status --porcelain` reports staged changes in the first column, so they are
    part of what this detects — and the remedy it prints has to be able to clear them."""
    prod = _prod_checkout(tmp_path)
    (prod / "uv.lock").write_text('name = "jarvis-os"\nversion = "0.1.1"\n')
    subprocess.run(["git", "-C", str(prod), "add", "uv.lock"],
                   check=True, capture_output=True)

    found = _prod_violations(tmp_path, monkeypatch)
    assert len(found) == 1
    assert found[0].context["paths"] == ["uv.lock"]
    # `checkout -- .` restores from the INDEX, so it cannot undo a staged change.
    assert "checkout -f jarvis-0.9.0" in found[0].detail
    assert "checkout -- ." not in found[0].detail.replace(
        "not `checkout -- .`", "")


def test_a_checkout_git_cannot_read_is_reported_not_called_clean(tmp_path, monkeypatch):
    """The realistic failure on the machine this watches is `detected dubious ownership`
    — the daemon and the user are different uids on one tree. Reporting that as clean
    would silence the invariant permanently on exactly the checkout it exists for."""
    prod = _prod_checkout(tmp_path)
    (prod / ".git" / "config").write_text("[core]\n\tthis is not valid ini\n")

    found = _prod_violations(tmp_path, monkeypatch)
    assert len(found) == 1
    assert found[0].invariant == "INV-PROD-CLEAN"
    assert found[0].context["paths"] is None
    assert "cannot tell" in found[0].detail
    assert found[0].context["error"]


def test_the_unknown_case_is_distinguishable_from_clean(tmp_path, monkeypatch):
    """The two must not share a return value — that is how a broken check reads green."""
    prod = _prod_checkout(tmp_path)
    monkeypatch.setenv("PRODUCTION_CODE", str(tmp_path))
    assert release.production_status(prod).dirty == []
    assert release.production_status(prod).error == ""

    unknown = release.production_status(tmp_path / "not-a-repo")
    assert unknown.dirty is None
    assert unknown.error


def test_the_remedy_survives_pyproject_itself_being_the_drift(tmp_path, monkeypatch):
    """The version on disk is one of the things drift can touch, so the remedy must not
    be derived from it: a tag built from a drifted version was never cut, the pathspec
    fails, and the reader concludes the CHECK is broken rather than the checkout."""
    prod = _prod_checkout(tmp_path)
    (prod / "pyproject.toml").write_text('[project]\nversion = "6.6.6-tampered"\n')

    found = _prod_violations(tmp_path, monkeypatch)
    assert len(found) == 1
    assert found[0].context["paths"] == ["pyproject.toml"]
    assert found[0].context["ref"] == "jarvis-0.9.0"      # from git, not from the file
    assert "6.6.6-tampered" not in found[0].detail


def test_an_untagged_checkout_falls_back_to_head(tmp_path, monkeypatch):
    """`HEAD` restores the same tree whatever it is called, so a checkout git cannot name
    still gets a remedy that works."""
    prod = _prod_checkout(tmp_path, tag=None)
    (prod / "uv.lock").write_text('name = "jarvis-os"\nversion = "0.1.1"\n')

    found = _prod_violations(tmp_path, monkeypatch)
    assert len(found) == 1
    assert found[0].context["ref"] == "HEAD"
    assert "checkout -f HEAD" in found[0].detail


def test_it_is_a_doctor_check_not_a_reconcile_tick_check():
    """Same rule as `check_service_path` and `check_config_drift`: it shells out to git
    against a checkout, which is not something the reconcile loop should do every tick."""
    assert invariants.check_production_clean in invariants.OS_INVARIANTS
    assert invariants.check_production_clean not in invariants.INVARIANTS


# -- INV-OS-HEALTH-SWEEP-DARK: the OS's own sweep must be alive -------------------------
#
# docs/superpowers/specs/2026-09-28-a-usage-limit-is-not-a-failed-sweep.md §5. USER RULE,
# 2026-09-28 (kn-7312c7de): the OS's own project must ALWAYS have the health sweep on and
# producing judgements.

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
    """A project IS the OS only by sharing an `origin` with the running install — by
    origin since issue 956, and still with no first-in-catalog fallback (spec §3).

    Through the cache rather than a real remote: a remote on a fixture project arms every
    other origin-gated path in the daemon. Real `git remote get-url` is
    `test_scheduler`'s job.
    """
    from jarvis import schedule

    install = str(Path(schedule.__file__).resolve().parent)
    monkeypatch.setitem(schedule._ORIGIN_CACHE, install, ("gonandrap", "agentic_os"))
    monkeypatch.setitem(schedule._ORIGIN_CACHE, str(project),
                        ("gonandrap", "agentic_os"))
    _register_catalog(tmp_path, [{"name": "proj_a", "path": str(project),
                                  "description": "the OS's own"}], **kw)
    return ProjectStore(project)


def test_the_os_sweep_switched_off_is_reported_critical(monkeypatch, tmp_path, project):
    store = _owning(monkeypatch, tmp_path, project, health=False)

    (violation,) = _dark(store)

    assert violation.invariant == "INV-OS-HEALTH-SWEEP-DARK"
    assert violation.level == "critical"
    assert not violation.repaired, "re-enabling it would be the OS editing the catalog"
    assert "DISABLED" in violation.detail
    assert "health_enabled" in violation.detail
    store.close()


def test_the_supervisor_switch_takes_the_sweep_dark_too(monkeypatch, tmp_path, project):
    """Two switches, either of which is fatal: `_health_projects` requires both."""
    store = _owning(monkeypatch, tmp_path, project, enabled=False)

    (violation,) = _dark(store)

    assert "supervisor.enabled" in violation.detail
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
    """A sweep that is silent only because the ACCOUNT was is not dark: the elapsed
    clock excludes time spent inside a hold."""
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
    """Every state above, on a project that is not the owner."""
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
    """§5: no first-in-catalog fallback. A project sharing no `origin` with the install
    is not the OS, however it is listed."""
    _register_catalog(tmp_path, [{"name": "proj_a", "path": str(project),
                                  "description": "an ordinary one"}], health=False)
    store = ProjectStore(project)

    assert _dark(store) == []
    store.close()


def test_an_unreadable_catalog_yields_no_violation(project):
    """An invariant must never be the thing that raises — `_validation_timeout`'s rule."""
    store = ProjectStore(project)
    assert _dark(store) == []
    store.close()


def test_the_dark_check_runs_every_tick(tmp_path, project):
    """A liveness check that runs hourly is a liveness check with an hour of blind
    spot."""
    assert invariants.check_os_health_sweep_alive in invariants.INVARIANTS
    assert invariants.check_os_health_sweep_alive not in invariants.SLOW_INVARIANTS


# -- INV-OS-IDENTITY: the OS's own project must be identifiable -------------------------
#
# Issue 956. `schedule.os_project` returning None makes three release guards, the
# health-sweep invariant and the config refusal all silently inert — the failure that went
# unnoticed for six duplicate release orders.

OS_ORIGIN = "gonandrap/agentic_os"


def _os_checkout(root, name, origin=OS_ORIGIN):
    from jarvis.testing import make_git_project, with_origin

    return with_origin(make_git_project(root, name), origin)


def _install_in(checkout: Path, monkeypatch) -> None:
    """Run as if the `jarvis` package being executed lived in this checkout."""
    from jarvis import schedule

    pkg = checkout / "src" / "jarvis"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(schedule, "__file__", str(pkg / "schedule.py"))


def test_an_ambiguous_os_identity_is_reported(tmp_path, monkeypatch, origins):
    """Two catalog projects on one origin — a worktree listed beside its checkout. The
    OS is in the catalog, so None here is a defect and not a deployment without one."""
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
    """A pip-installed OS whose catalog holds only other people's projects. None is the
    right answer there, so firing would be an alarm about a correct deployment."""
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
    """`Daemon.check_invariants` passed the literal `"warning"`, which would file this
    one beside a stale attention flag."""
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


# -- a dropped confirmation is the user's again ----------------------------------------
#
# §7 and §10.4 of docs/superpowers/specs/2026-09-28-a-dropped-confirmation-must-not-hold-
# an-assumption-for-ever.md. `_os_is_confirming` suppressed the blocker on
# `provisional_verdict == 'accept'` alone, so an abandoned confirmation left the order
# unflagged AND unclosable — the worst of both (§1.3).

from jarvis.neo_store import NeoStore                                     # noqa: E402
from tests.test_autoreview import ROUTINE, ask, park, started             # noqa: E402,F401
from tests.test_autoreview_confirm import provisional, with_diff          # noqa: E402,F401

ASSUMPTION_BLOCKER = "1 assumption pending your review"


def _confirming(started, catalog_file):
    """A parked order with one pending assumption the OS has an early ACCEPT on."""
    store, wo = park(started, auto_review=True)
    # AFTER `park`, which keeps the catalog FILE's `validation.enabled` false on purpose:
    # `ops.auto_review_at` re-reads the file, and it reads BOTH halves of the switch.
    for key in ("validation.enabled", "validation.auto_review"):
        ops.set_config(key, True, project="proj_a",
                       reason="the panel has been right for a month",
                       catalog_path=str(catalog_file))
    (row,) = store.all_assumptions(wo["id"])
    store.record_provisional(row["id"], verdict="accept", reason="r", model="sonnet")
    return store, wo, row["id"]


def test_a_confirmation_in_flight_suppresses_the_blocker_and_a_dropped_one_does_not(
        started, catalog_file):
    """THE SAME ROW, walked from in-flight to spent. Asserting the suppression alone
    passes against today's code, which suppresses for ever."""
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

    # ...and INV-ATTENTION-REASON then names it, instead of agreeing with the lie.
    store.flag_attention(wo["id"], IDLE_NOTIFICATION)
    violations = [v for v in invariants.check_attention_reason_is_true(store)]
    assert [v.invariant for v in violations] == ["INV-ATTENTION-REASON"]
    assert store.get_work_order(wo["id"])["attention_reason"] == ASSUMPTION_BLOCKER


def test_a_confirmation_link_nobody_can_resolve_does_not_suppress(started, catalog_file):
    """FAIL TOWARD THE USER (§7): an unreadable question is not evidence of a confirmation
    in flight, and the failure direction `check_blocked_work_is_surfaced` calls dangerous
    is the silent one."""
    store, wo, aid = _confirming(started, catalog_file)

    store.link_assumption_confirmation(aid, 999_999)      # no such question, ever

    assert ASSUMPTION_BLOCKER in true_blockers(store, store.get_work_order(wo["id"]))


def test_a_transient_drop_strands_no_question_in_neo_attention(started):
    """§4.3's PAIR, and the reason the drop site supersedes BEFORE it clears: a cleared
    link makes the question unresolvable through `assumption_for_question`, and
    unresolvable is deliberately left alone — so an `escalated` question plus a cleared
    link would sit in `ops._neo_attention` for ever, which is INV-NEO-ESCALATION-STALE's
    exact failure re-introduced by the fix."""
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


# -- a rebind: the round the OS's own merge costs nobody -------------------------------
# spec docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md §4.5


def _declined_on_a_moved_head(store: ProjectStore, *, cause: str,
                              head: str = "bbbb1111bbbb2222",
                              judged: str = "aaaa1111aaaa2222") -> dict:
    """A pull request parked on a head the OS refused to re-judge, for `cause`."""
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
    """The two declines mean different things and only one is answered by
    `validation.max_rounds` — so each derivation reads only its own."""
    from jarvis import ops

    store = ProjectStore(project)
    spent = _declined_on_a_moved_head(store, cause=ops.REBIND_EXHAUSTED)

    assert invariants.rejudge_exhausted(store, spent) is False
    assert invariants.rebind_exhausted(store, spent) is True
    blockers = true_blockers(store, spent)
    assert invariants.REBIND_EXHAUSTED_BLOCKER in blockers
    assert invariants.SHA_MOVED_BLOCKER not in blockers


def test_a_budget_decline_still_reads_as_one(project):
    from jarvis import ops

    store = ProjectStore(project)
    budget = _declined_on_a_moved_head(store, cause=ops.REJUDGE_BUDGET_SPENT)

    assert invariants.rejudge_exhausted(store, budget) is True
    assert invariants.rebind_exhausted(store, budget) is False
    blockers = true_blockers(store, budget)
    assert invariants.SHA_MOVED_BLOCKER in blockers
    assert invariants.REBIND_EXHAUSTED_BLOCKER not in blockers


def test_a_decline_written_before_the_cause_existed_is_a_budget_decline(project):
    """The migration reading: a payload with no `cause` is every row there was."""
    store = ProjectStore(project)
    old = _declined_on_a_moved_head(store, cause="")

    assert invariants.rejudge_exhausted(store, old) is True
    assert invariants.rebind_exhausted(store, old) is False


# -- a panel give-up that survived into `waiting_pr_merge` ----------------------------
# docs/superpowers/specs/2026-10-01-an-escalated-round-on-a-parked-order-raises-nothing.md


def _parked_on_an_escalation(store: ProjectStore, *, outcome: str = "escalated",
                             hold: str | None = invariants.HELD_NOT_PASSED,
                             status: str = "waiting_pr_merge") -> dict:
    """A work order parked behind its pull request with the panel's last word on it."""
    wo = store.create_work_order("add feature X")
    store.update_work_order(wo["id"], pr_url="https://github.com/acme/proj/pull/7",
                            result_summary="opened a PR")
    row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.set_validation_head(row["id"], "aaaa1111aaaa2222")
    store.close_validation_round(row["id"], outcome, "")
    store.set_status(wo["id"], status)
    if hold is not None:
        store.add_event(wo["id"], "automerge_held", {"code": hold, "round": 1})
    return store.get_work_order(wo["id"])


def test_a_give_up_parked_behind_a_held_merge_is_the_users(project):
    """Row 1: the live stall on wo-3615faf7 — nothing automatic is left and the OS said
    nothing for 47h."""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store)

    assert invariants.parked_on_a_give_up(store, wo) is True
    assert invariants.PARKED_GIVE_UP_BLOCKER in true_blockers(store, wo)
    # DERIVED, never written at the hold site (kn-089de524).
    assert not wo["needs_attention"]
    list(invariants.check_blocked_work_is_surfaced(store))
    row = store.get_work_order(wo["id"])
    assert row["needs_attention"]
    assert row["attention_reason"] == invariants.PARKED_GIVE_UP_BLOCKER


def test_a_hold_with_another_cause_is_not_this_stall(project):
    """Row 2: the merge is held by something with its own machinery behind it."""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store, hold="checks_running")  # any other code

    assert invariants.parked_on_a_give_up(store, wo) is False
    assert invariants.PARKED_GIVE_UP_BLOCKER not in true_blockers(store, wo)


def test_a_project_merging_by_hand_is_not_flagged_at_all(project):
    """Row 3: `auto_merge` off writes no hold ever, and there the pull request merges on
    GitHub — flagging it would be reporting a working flow as the user's problem."""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store, hold=None)

    assert invariants.parked_on_a_give_up(store, wo) is False
    assert invariants.PARKED_GIVE_UP_BLOCKER not in true_blockers(store, wo)


def test_a_round_still_open_owes_nobody_anything(project):
    """Row 4: `validation_escalated` is False while the panel is still deliberating."""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store, outcome="pending")

    assert invariants.parked_on_a_give_up(store, wo) is False
    assert invariants.PARKED_GIVE_UP_BLOCKER not in true_blockers(store, wo)


def test_a_passed_round_held_for_some_other_reason_is_not_a_give_up(project):
    """Row 5."""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store, outcome="passed")

    assert invariants.parked_on_a_give_up(store, wo) is False
    assert invariants.PARKED_GIVE_UP_BLOCKER not in true_blockers(store, wo)


def test_a_later_round_that_passes_clears_it_without_a_new_hold(project):
    """Row 6: self-clearing on fact 1 alone, on the same tick — it does not wait for the
    next `automerge_held` event to be rewritten."""
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
    """Row 7: kn-089de524 — the hold is rewritten every poll, so a flag written there
    would overwrite `jarvis wo ack` for ever."""
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
    """Row 8: the upstream sentence still answers the status it was written for, and the
    parked twin does not double up on it."""
    store = ProjectStore(project)
    wo = _parked_on_an_escalation(store, status="needs_review")

    blockers = true_blockers(store, wo)
    assert VALIDATION_STUCK_BLOCKER in blockers
    assert invariants.PARKED_GIVE_UP_BLOCKER not in blockers


def test_a_moved_head_decline_is_the_other_sentence_and_only_that(project):
    """Row 9: one newest hold carries one code, so the two can never co-occur."""
    from jarvis import ops as ops_mod

    store = ProjectStore(project)
    moved = _declined_on_a_moved_head(store, cause=ops_mod.REJUDGE_BUDGET_SPENT)

    blockers = true_blockers(store, moved)
    assert invariants.SHA_MOVED_BLOCKER in blockers
    assert invariants.PARKED_GIVE_UP_BLOCKER not in blockers
# -- is somebody ELSE holding this, or is anything out at all? -------------------------
# Fix 2 of docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md: two
# questions, two resolvers, side by side so they cannot drift (kn-4ea33fe6).


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
# -- §2c: an undeclared delivery, as a predicate on its own -----------------------------

PUSHED = "9999999999999999999999999999999999999999"
DELIVERED = "1111111111111111111111111111111111111111"


def _refused_then_pushed(store: ProjectStore, *, head: str, judged: str,
                         description: str = "") -> dict:
    """wo-dbea82cf: delivered, judged, the user refused an assumption, then commits.

    `description` is for the callers that drive the supervisor: the brief is in the
    evidence packet, which is how the fake `claude` is asked for a verdict.
    """
    wo = store.create_work_order("the refused one", description=description)
    store.add_event(wo["id"], "finished", {"summary": "opened a PR"})
    round_ = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.set_validation_head(round_["id"], judged)
    store.close_validation_round(round_["id"], "passed", "green")
    store.add_event(wo["id"], "reviewed", {"accepted": False})
    store.set_status(wo["id"], "needs_review", pr_url="https://example/pull/2",
                     pr_head_oid=head, pr_head_seen_at=time.time())
    return store.get_work_order(wo["id"])


def test_commits_past_the_judged_head_with_no_finish_are_an_undeclared_delivery(project):
    """Spec §2c. `ops.refusal_answered` stays False here — this round predates the
    refusal and is not a user-rework round — and the OS gets a detector."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)

    assert not ops.refusal_answered(store, wo["id"])
    assert invariants.undeclared_delivery(store, wo)


def test_a_head_still_at_the_judged_commit_is_not_an_undeclared_delivery(project):
    """Nothing moved: the refusal is simply unanswered, which is today's sentence."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=DELIVERED, judged=DELIVERED)

    assert not invariants.undeclared_delivery(store, wo)


def test_no_recorded_head_is_never_an_undeclared_delivery(project):
    """Empty is "not recorded", never "different" — the same rule as §2b."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head="", judged=DELIVERED)

    assert not invariants.undeclared_delivery(store, wo)


def test_a_finish_after_the_refusal_answers_it_and_ends_the_detection(project):
    """The worker declared them, so there is nothing undeclared left to nudge for."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    store.add_event(wo["id"], "finished", {"summary": "here is what I pushed"})
    wo = store.get_work_order(wo["id"])

    assert ops.refusal_answered(store, wo["id"])
    assert not invariants.undeclared_delivery(store, wo)


def test_the_detector_raises_a_finding_once_and_never_an_alarm(project):
    """`add_finding`, not `add_alarm`: the cost path's dedupe is what `add_alarm`'s one
    call site is fenced by, and it does not fence this."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)

    check_project(store)
    check_project(store)

    raised = [a for a in store.alarms_of(wo["id"])
              if a["kind"] == "undeclared_delivery"]
    assert len(raised) == 1
    assert raised[0]["source"] == "invariant"


def test_a_doctor_run_without_repair_reports_the_finding_and_writes_none(project):
    """`jarvis doctor` with no `--repair` must not write, and must still SAY it.

    `check_neo_escalations_are_live` is the shape: the repair that cannot be intercepted
    by the read-only proxy is skipped by the checker itself, and the violation is
    reported as proposed. Returning early instead loses the report as well as the write,
    which is a doctor that cannot see the thing it exists to see."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)

    reported = [v for v in check_project(store, repair=False)
                if v.invariant == "INV-UNDECLARED-DELIVERY"]

    assert [v.wo_id for v in reported] == [wo["id"]]
    assert not reported[0].repaired
    assert reported[0].repair.startswith("would raise")
    assert store.alarms_of(wo["id"]) == []


def test_no_finding_while_the_os_is_nudging_the_worker(project):
    """The poll is the actor now: an order with a session and no give-up is being asked
    to declare, so a finding would be raised and then answered by the OS itself."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    store.update_work_order(wo["id"], session_id="sess-1")

    reported = [v for v in check_project(store)
                if v.invariant == "INV-UNDECLARED-DELIVERY"]

    assert reported == []
    assert store.alarms_of(wo["id"]) == []


def test_the_finding_is_the_give_ups(project):
    """Once the OS has said it is stopping, the finding is what reaches the user —
    raised once, as before."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    store.update_work_order(wo["id"], session_id="sess-1")
    store.add_event(wo["id"], "pr_undeclared_unresolved", {"attempts": 3})

    check_project(store)
    check_project(store)

    raised = [a for a in store.alarms_of(wo["id"])
              if a["kind"] == "undeclared_delivery"]
    assert len(raised) == 1


# -- INV-STUCK-SWEEP-DARK: the stuck sweep's own failures are a first-class alarm -------
#
# §8 of docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets-investigated.md.

def _sweep_run(tmp_path, project, *, projects=None, os_extra=None, **row) -> Path:
    """A registered catalog at `fleet_health`'s shipped defaults, plus one run row."""
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
                {"ts": time.time(), "scanned": 0, "candidates": 0, "opened": 0,
                 "skipped": {}, "excluded": "", "error": "", **row}))
    finally:
        central.close()
    return path


def _own_the_os(monkeypatch, project) -> None:
    """Make `project` the OS-OWNING project. Identity is BY GIT ORIGIN since issue 956
    (`schedule.os_project`), so the install and this path share one `_ORIGIN_CACHE`
    entry — `tests/test_config_console.py`'s `os_project` fixture, verbatim in mechanism."""
    from jarvis import schedule

    install = str(Path(schedule.__file__).resolve().parent)
    for key in {install, str(project), str(Path(project).resolve())}:
        monkeypatch.setitem(schedule._ORIGIN_CACHE, key, ("gonandrap", "agentic_os"))


def _stuck_dark(store) -> list:
    return [v for v in invariants.check_stuck_sweep_alive(store)
            if v.invariant == "INV-STUCK-SWEEP-DARK"]


def test_sweep_error_raises_the_invariant_once(jarvis_home, tmp_path, project,
                                               monkeypatch):
    """§8: the run row carries the error, and the row is what the check reads."""
    _own_the_os(monkeypatch, project)
    _sweep_run(tmp_path, project, error="OperationalError: database is locked")
    store = ProjectStore(project)

    (violation,) = _stuck_dark(store)
    assert violation.level == "critical" and not violation.repaired
    assert violation.context["cause"] == "failing"
    assert "database is locked" in violation.detail
    # The STATE, not an event: a second read of the same row says the same thing.
    assert [v.context["cause"] for v in _stuck_dark(store)] == ["failing"]

    _sweep_run(tmp_path, project, error="")
    assert _stuck_dark(store) == []
    store.close()


def test_sweep_dark_raises_after_the_window(jarvis_home, tmp_path, project, monkeypatch):
    """§8: the window is six sweep intervals, so one capped tick cannot trip it."""
    import jarvis.catalog as catalog_mod

    _own_the_os(monkeypatch, project)
    window = catalog_mod.DEFAULT_FLEET_HEALTH_SWEEP_DARK_MINUTES
    _sweep_run(tmp_path, project, ts=time.time() - (window + 1) * 60)
    store = ProjectStore(project)

    (violation,) = _stuck_dark(store)
    assert violation.context["cause"] == "dark"
    assert window == 180

    _sweep_run(tmp_path, project, ts=time.time() - (window - 1) * 60)
    assert _stuck_dark(store) == []
    store.close()


def test_the_fleet_catalog_value_is_what_the_check_judges_by(
        jarvis_home, tmp_path, project, monkeypatch):
    """`sweep_dark_minutes` is a CATALOG setting, not a module constant: a silence the
    180-minute default would not report fires once the fleet number is turned down."""
    _own_the_os(monkeypatch, project)
    silence = 20 * 60  # 20 minutes — well inside the shipped 180
    _sweep_run(tmp_path, project, ts=time.time() - silence)
    store = ProjectStore(project)
    assert _stuck_dark(store) == []

    _sweep_run(tmp_path, project, ts=time.time() - silence,
               os_extra={"fleet_health": {"sweep_dark_minutes": 10}})
    (violation,) = _stuck_dark(store)
    assert violation.context["cause"] == "dark"
    assert violation.context["dark_minutes"] == 20
    store.close()


def test_the_stuck_check_is_registered_where_it_can_push(tmp_path, project):
    """Review round 1: in `OS_INVARIANTS` only `jarvis doctor` ran it, so a failing
    sweep never reached the attention list. `check_os_health_sweep_alive`'s placement."""
    assert invariants.check_stuck_sweep_alive in invariants.INVARIANTS
    assert invariants.check_stuck_sweep_alive not in invariants.OS_INVARIANTS
    assert invariants.check_stuck_sweep_alive not in invariants.SLOW_INVARIANTS


def test_the_fleet_wide_sweep_is_reported_once_not_once_per_project(
        jarvis_home, tmp_path, project, monkeypatch):
    """One run row, one report: every project but the owner short-circuits."""
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
    """Review round 1: the push surface. A check the daemon runs per project writes a
    `violation_reports` row, and that row is what `ops.os_status` reads (Neo 1084)."""
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

        # A CLEAN sweep clears it — but `check_invariants` only closes reports on a
        # sweep tick, so the clean pass must be invoked with `sweep_landings=True`.
        monkeypatch.setattr(ops_module, "stuck_scan", clean_scan)
        daemon.stuck_tick()
        daemon.check_invariants(catalog.projects[0], store, sweep_landings=True)
        assert _stuck_attention(catalog) == []
    finally:
        store.close()


def _stuck_attention(catalog) -> list:
    return [i for i in ops.os_status(catalog)["attention"]
            if i.get("invariant") == "INV-STUCK-SWEEP-DARK"]


# -- Neo 1084: a standing critical violation is ONE attention item ----------------------


def test_a_standing_critical_violation_reaches_the_attention_list(
        jarvis_home, project, catalog_file, monkeypatch):
    """Neo 1084: `jarvis status` read HEALTHY over a standing critical violation."""
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

    # ...and the report row is also what takes it away again.
    closed = ProjectStore(project)
    try:
        assert closed.close_violation_report("INV-TEST-CRITICAL", wo["id"])
    finally:
        closed.close()
    assert ops.os_status(catalog)["attention"] == [], "the row is also the removal"


# -- a passed forced round answers a refusal --------------------------------------------
# docs/superpowers/specs/2026-10-08-a-passed-forced-round-answers-a-refusal.md

FORCED = "2222222222222222222222222222222222222222"


def _forced_round(store: ProjectStore, wo_id: str, *, outcome: str = "passed",
                  cause: str | None = None, head: str = FORCED,
                  carried: str = "") -> dict:
    """The one round `ops.user_rework_pending` grants, opened after the refusal."""
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
    """Spec §The predicate (b): the user forcing that round IS the declaration."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, wo["id"])

    assert ops.refusal_answered(store, wo["id"])
    assert ops.land_when_cleared(store, store.get_work_order(wo["id"]),
                                 "https://example/pull/2") == "waiting_pr_merge"


def test_a_rejected_user_rework_round_leaves_the_refusal_unanswered(project):
    """Spec §What does NOT change: (b) requires `passed`."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, wo["id"], outcome="rejected")

    assert not ops.refusal_answered(store, wo["id"])
    assert ops.land_when_cleared(store, store.get_work_order(wo["id"]),
                                 "https://example/pull/2") == "needs_review"


def test_a_rebind_round_never_answers_a_refusal(project):
    """Spec §What does NOT change: the OS demanded that merge, not the user."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, wo["id"], cause=ops.REBIND_CAUSE)

    assert not ops.refusal_answered(store, wo["id"])


def test_an_unmoved_judged_head_does_not_answer_the_refusal(project):
    """Spec §The head guard: a pass on the refused commit is the back door."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, wo["id"], head=DELIVERED)

    assert not ops.refusal_answered(store, wo["id"])


def test_an_unrecorded_head_on_either_round_still_answers(project):
    """Spec §The head guard: "" is never a match — the pre-0.10.0 population."""
    store = ProjectStore(project)
    forced_blank = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, forced_blank["id"], head="")
    prior_blank = _refused_then_pushed(store, head=PUSHED, judged="")
    _forced_round(store, prior_blank["id"], head=DELIVERED)

    assert ops.refusal_answered(store, forced_blank["id"])
    assert ops.refusal_answered(store, prior_blank["id"])


def test_a_round_settled_before_the_refusal_does_not_answer_it(project):
    """Spec §The predicate: the newest counted round must be newer than the cut."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)

    assert not ops.refusal_answered(store, wo["id"])


def test_a_carried_head_equal_to_the_refused_one_does_not_answer(project):
    """Spec §The head guard: `validated_head` is the read, so the carry is the head."""
    store = ProjectStore(project)
    wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, wo["id"], head=FORCED, carried=DELIVERED)

    assert not ops.refusal_answered(store, wo["id"])


def test_the_undeclared_delivery_detector_follows_the_widened_predicate(project):
    """Spec §`invariants.undeclared_delivery`: it keeps CALLING the predicate, so an
    answered refusal stops the nudge. A rejected forced round leaves the refusal
    unanswered (asserted above) but records no `validated_head`, so `judged_heads` is
    empty and the detector's own empty-set guard answers first — the True half is
    `test_commits_past_the_judged_head_with_no_finish_are_an_undeclared_delivery`."""
    store = ProjectStore(project)
    answered = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED)
    _forced_round(store, answered["id"])

    assert not invariants.undeclared_delivery(store, store.get_work_order(
        answered["id"]))
