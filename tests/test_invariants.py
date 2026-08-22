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

import time

from jarvis import cli, ops
from jarvis.catalog import DEFAULT_VALIDATION_TIMEOUT
from jarvis.hooks import handle_hook
from jarvis.invariants import (
    BLOCKED_STATUSES,
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
        "finished without a completion signal — review the session"]

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
# `validating` is the one active status nothing outside the daemon moves: it raises no
# attention flag and `settle_work_order` returns early for it. A daemon that dies
# mid-round therefore leaves a work order nobody will ever look at again, which is the
# exact shape of invisible stall this module exists to catch.


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
