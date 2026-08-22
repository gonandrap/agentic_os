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

from jarvis import cli, ops
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


# -- the three validation watchdogs ---------------------------------------------------
#
# `validating` raises no attention on purpose, which is what makes a STUCK one dangerous:
# on every human surface the OS has it is indistinguishable from a working one. There is
# no error, no flag and no inbox row, so the only thing that can catch these is a
# steady-state predicate. Each test below therefore pairs the broken state with the
# healthy one it must stay silent on — an invariant is a predicate, and a test that only
# shows it firing cannot tell a correct one from `return True`.


def _round(store, *, wo_id=None, fo_id=None, outcome="pending", fingerprint="abc"):
    rnd = store.open_validation_round(wo_id=wo_id, fo_id=fo_id, fingerprint=fingerprint)
    if outcome != "pending":
        store.close_validation_round(rnd["id"], outcome, reason="not good enough")
    return store.get_validation_round(rnd["id"])


def _feedback_envelope(store, rnd, *, state, attempts=0, wo_id=None, fo_id=None):
    """A `review_feedback` envelope for one round, left in the state under test.

    Posted through the bus so the payload is the shape the round machine will really
    write — the join `lost_feedback` makes is on the round NUMBER inside that payload.
    """
    from jarvis.bus import ReviewFeedback, Subject, post

    env_id = post(store, subject=Subject(wo_id=wo_id, fo_id=fo_id), from_role="reviewer",
                  to_role="implementor" if wo_id else "manager",
                  payload=ReviewFeedback(round=rnd["round"], outcome="rejected",
                                         reason="tests are missing"))
    if state != "queued":
        store.mark_envelope(env_id, state, note="nobody fills that role")
    for _ in range(attempts):
        store.bump_envelope_attempt(env_id)
    return env_id


def _validating_wo(store, title="ship the thing"):
    wo = store.create_work_order(title)
    store.update_work_order(wo["id"], result_summary="done")
    store.set_status(wo["id"], "validating")
    return wo["id"]


def _validating_fo(store, title="ship the exporter"):
    fo = store.create_feature_order(title)
    store.set_feature_status(fo["id"], "validating")
    return fo["id"]


def _fired(violations, invariant):
    return [v for v in violations if v.invariant == invariant]


# -- INV-VALIDATION-ORPHAN ------------------------------------------------------------


def test_a_unit_parked_in_validating_with_no_round_is_reported_for_both_units(project):
    """Both units in ONE test, because "covers half the units" is exactly what would
    otherwise hide here: a work order and a feature order park in the same status by the
    same mechanism, and an invariant covering one leaves the other silently stalled with
    the checker's name on the box saying it was covered.

    Paired with the healthy state of both, which is the whole point: a round in flight
    is the OS working and must stay silent.
    """
    store = ProjectStore(project)
    healthy_wo, healthy_fo = _validating_wo(store, "under review"), _validating_fo(store)
    _round(store, wo_id=healthy_wo)
    _round(store, fo_id=healthy_fo)

    assert _fired(check_project(store, repair=True), "INV-VALIDATION-ORPHAN") == []

    orphan_wo = _validating_wo(store, "parked with nothing running")
    orphan_fo = _validating_fo(store, "a feature nobody is judging")

    fired = _fired(check_project(store, repair=True), "INV-VALIDATION-ORPHAN")

    assert len(fired) == 2
    assert {v.wo_id for v in fired} == {orphan_wo, None}
    assert {v.context["unit"] for v in fired} == {"work_order", "feature_order"}
    assert orphan_fo in next(v.detail for v in fired if v.wo_id is None)
    # ...and the healthy pair is still untouched by it
    assert store.get_work_order(healthy_wo)["needs_attention"] == 0
    assert store.get_feature_order(healthy_fo)["needs_attention"] == 0


def test_a_settled_round_leaves_the_unit_just_as_orphaned(project):
    """`latest_validation_round` returning a row is not the same as a review being under
    way. A round that already passed, was rejected or gave up moves nothing."""
    store = ProjectStore(project)
    wo_id = _validating_wo(store)
    rnd = _round(store, wo_id=wo_id)

    assert _fired(check_project(store, repair=True), "INV-VALIDATION-ORPHAN") == []

    store.close_validation_round(rnd["id"], "passed")

    fired = _fired(check_project(store, repair=True), "INV-VALIDATION-ORPHAN")
    assert len(fired) == 1
    assert "already finished `passed`" in fired[0].detail


def test_the_orphan_is_never_repaired_even_on_the_daemons_own_path(project):
    """The daemon calls `check_project(store, repair=True)`, so this assertion is the one
    that stops a future edit turning a report into a guess: un-parking the unit correctly
    needs the status it came FROM, which is in the timeline and not in current state."""
    store = ProjectStore(project)
    wo_id = _validating_wo(store)

    fired = _fired(check_project(store, repair=True), "INV-VALIDATION-ORPHAN")

    assert [v.repaired for v in fired] == [False]
    assert store.get_work_order(wo_id)["status"] == "validating"  # nothing moved
    assert "jarvis wo done" in fired[0].detail  # ...but the reader is told what to do


def test_the_orphan_raises_attention_that_the_next_tick_agrees_with(project,
                                                                   catalog_file):
    """Without this the flag survives exactly one tick: INV-ATTENTION-REASON rewrites any
    attention reason `true_blockers` cannot re-derive."""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon
    from jarvis.invariants import VALIDATION_ORPHAN_BLOCKER

    store = ProjectStore(project)
    wo_id = _validating_wo(store)
    check_project(store, repair=True)
    assert store.get_work_order(wo_id)["attention_reason"] == VALIDATION_ORPHAN_BLOCKER
    assert true_blockers(store, store.get_work_order(wo_id)) == [
        VALIDATION_ORPHAN_BLOCKER]
    store.close()

    catalog = load_catalog(catalog_file)
    spec = catalog.projects[0]
    fresh = ProjectStore(spec.path)
    Daemon(catalog).check_invariants(spec, fresh)

    wo = fresh.get_work_order(wo_id)
    assert wo["needs_attention"] == 1
    assert wo["attention_reason"] == VALIDATION_ORPHAN_BLOCKER
    assert "invariant" in [e["kind"] for e in fresh.list_events(wo_id)]


# -- INV-VALIDATION-FEEDBACK-LOST -----------------------------------------------------


def test_feedback_that_reached_nobody_is_reported_and_delivered_feedback_is_not(project):
    """The pairing IS the test. All three envelope fates for the same rejection shape:
    undeliverable and queued-past-the-ceiling are stalls nobody else will ever mention,
    delivered is the loop working exactly as designed.

    Read-only, because that is where the queued half is observable at all — see the test
    below, which pins the reason.
    """
    from jarvis.bus import DELIVERY_ATTEMPT_CEILING

    store = ProjectStore(project)
    lost = _validating_wo(store, "rejected, feedback undeliverable")
    stuck = _validating_wo(store, "rejected, feedback still queued")
    fine = _validating_wo(store, "rejected, feedback delivered")
    for wo_id, state, attempts in ((lost, "undeliverable", 1),
                                   (stuck, "queued", DELIVERY_ATTEMPT_CEILING),
                                   (fine, "delivered", 1)):
        rnd = _round(store, wo_id=wo_id, outcome="rejected")
        _feedback_envelope(store, rnd, state=state, attempts=attempts, wo_id=wo_id)

    fired = _fired(check_project(store, repair=False), "INV-VALIDATION-FEEDBACK-LOST")

    assert {v.wo_id for v in fired} == {lost, stuck}
    assert all(v.repaired is False for v in fired)
    assert all("never reached anyone" in v.detail for v in fired)


def test_a_queued_envelope_the_bus_can_still_deliver_is_the_buss_problem_not_this_one(
        project):
    """The registration order, asserted rather than commented. INV-ENVELOPE-STUCK runs
    first and RETRIES a queued envelope; only what that retry cannot place is
    `undeliverable`. So on the daemon's repairing path the same work order that looks
    lost in a read-only snapshot has its feedback delivered instead — and this invariant
    must not have flagged it on the way past."""
    from jarvis.bus import DELIVERY_ATTEMPT_CEILING

    store = ProjectStore(project)
    wo_id = _validating_wo(store, "rejected, feedback still queued")
    rnd = _round(store, wo_id=wo_id, outcome="rejected")
    env_id = _feedback_envelope(store, rnd, state="queued",
                                attempts=DELIVERY_ATTEMPT_CEILING, wo_id=wo_id)

    assert len(_fired(check_project(store, repair=False),
                      "INV-VALIDATION-FEEDBACK-LOST")) == 1

    fired = _fired(check_project(store, repair=True), "INV-VALIDATION-FEEDBACK-LOST")

    assert fired == []
    assert next(e for e in store.envelopes() if e["id"] == env_id)["state"] == "delivered"
    assert store.get_work_order(wo_id)["needs_attention"] == 0


def test_the_orphan_stays_silent_while_a_rejection_is_still_in_flight(project):
    """The two round outcomes `validation_orphaned` deliberately does NOT treat as the
    end of the review, paired with the two it does.

    A rejected round leaves the work order parked until the feedback is delivered, and a
    transport-failed round is retried on the next tick with the unit still parked — both
    are the OS working, both last at least a full tick, and flagging either would put a
    permanent attention flag on a healthy work order.
    """
    store = ProjectStore(project)
    healthy = {outcome: _validating_wo(store, f"round {outcome}")
               for outcome in ("rejected", "failed")}
    ends = {outcome: _validating_wo(store, f"round {outcome}")
            for outcome in ("passed", "escalated")}
    for outcome, wo_id in {**healthy, **ends}.items():
        _round(store, wo_id=wo_id, outcome=outcome)
    # ...and the rejection's feedback is on its way, so nothing is lost either
    _feedback_envelope(store, store.latest_validation_round(wo_id=healthy["rejected"]),
                       state="delivered", wo_id=healthy["rejected"])

    fired = _fired(check_project(store, repair=True), "INV-VALIDATION-ORPHAN")

    assert {v.wo_id for v in fired} == set(ends.values())
    assert all(store.get_work_order(wo_id)["needs_attention"] == 0
               for wo_id in healthy.values())


def test_feedback_still_queued_below_the_ceiling_is_the_bus_working(project):
    """An envelope on its way is not an envelope that was lost. Firing here would put a
    permanent flag on every rejection in the fleet for the seconds before it lands."""
    from jarvis.bus import DELIVERY_ATTEMPT_CEILING

    store = ProjectStore(project)
    wo_id = _validating_wo(store)
    rnd = _round(store, wo_id=wo_id, outcome="rejected")
    env_id = _feedback_envelope(store, rnd, state="queued",
                                attempts=DELIVERY_ATTEMPT_CEILING - 1, wo_id=wo_id)

    assert _fired(check_project(store, repair=False),
                  "INV-VALIDATION-FEEDBACK-LOST") == []

    store.bump_envelope_attempt(env_id)

    assert len(_fired(check_project(store, repair=False),
                      "INV-VALIDATION-FEEDBACK-LOST")) == 1


def test_lost_feedback_is_matched_to_its_own_round_on_a_feature_order_too(project):
    """The join is subject AND round number: an envelope for round one that never
    arrived says nothing about round two, which is the round the unit is actually
    parked on."""
    store = ProjectStore(project)
    fo_id = _validating_fo(store)
    first = _round(store, fo_id=fo_id, outcome="rejected", fingerprint="one")
    _feedback_envelope(store, first, state="undeliverable", fo_id=fo_id)
    second = _round(store, fo_id=fo_id, outcome="rejected", fingerprint="two")
    _feedback_envelope(store, second, state="delivered", fo_id=fo_id)

    assert _fired(check_project(store, repair=True),
                  "INV-VALIDATION-FEEDBACK-LOST") == []

    third = _round(store, fo_id=fo_id, outcome="rejected", fingerprint="three")
    _feedback_envelope(store, third, state="undeliverable", fo_id=fo_id)

    fired = _fired(check_project(store, repair=True), "INV-VALIDATION-FEEDBACK-LOST")
    assert len(fired) == 1
    assert fired[0].wo_id is None and fired[0].context["fo_id"] == fo_id
    assert store.get_feature_order(fo_id)["needs_attention"] == 1


def test_lost_feedback_raises_attention_the_next_tick_agrees_with(project, catalog_file):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon
    from jarvis.invariants import VALIDATION_FEEDBACK_LOST_BLOCKER

    store = ProjectStore(project)
    wo_id = _validating_wo(store)
    rnd = _round(store, wo_id=wo_id, outcome="rejected")
    _feedback_envelope(store, rnd, state="undeliverable", wo_id=wo_id)
    check_project(store, repair=True)
    assert true_blockers(store, store.get_work_order(wo_id)) == [
        VALIDATION_FEEDBACK_LOST_BLOCKER]
    store.close()

    catalog = load_catalog(catalog_file)
    spec = catalog.projects[0]
    fresh = ProjectStore(spec.path)
    Daemon(catalog).check_invariants(spec, fresh)

    assert fresh.get_work_order(wo_id)["attention_reason"] == (
        VALIDATION_FEEDBACK_LOST_BLOCKER)


# -- INV-MANAGER-MISSING --------------------------------------------------------------


def _feature_with_children(store, title, status="executing", n=2):
    fo = store.create_feature_order(title)
    store.set_feature_status(fo["id"], status)
    for i in range(n):
        store.create_work_order(f"{title} part {i}", parent_id=fo["id"])
    return fo["id"]


def test_a_live_feature_without_a_manager_is_reported_and_a_closed_one_is_not(project):
    """Three features in one test, and the two silent ones are what make it a predicate.
    A feature whose manager exists is healthy; a COMPLETED feature legitimately has none,
    and an invariant that flagged every closed feature in a project's history would be
    worse than no invariant at all."""
    store = ProjectStore(project)
    broken = _feature_with_children(store, "no manager, still running")
    managed = _feature_with_children(store, "properly managed")
    store.create_manager_order(managed)
    done = _feature_with_children(store, "finished long ago")
    store.set_feature_status(done, "completed")

    fired = _fired(check_project(store, repair=True), "INV-MANAGER-MISSING")

    assert [v.context["fo_id"] for v in fired] == [broken]
    assert fired[0].repaired is False
    assert store.get_feature_order(managed)["needs_attention"] == 0
    assert store.get_feature_order(done)["needs_attention"] == 0
    assert store.get_feature_order(broken)["needs_attention"] == 1


def test_a_feature_with_no_children_has_nothing_to_manage(project):
    """Released with nothing in it, or not released at all: no children means no
    rejections to route, so there is nothing for a manager to own."""
    store = ProjectStore(project)
    empty = store.create_feature_order("released empty")["id"]
    store.set_feature_status(empty, "executing")

    assert _fired(check_project(store, repair=True), "INV-MANAGER-MISSING") == []

    store.create_work_order("the first piece", parent_id=empty)

    assert len(_fired(check_project(store, repair=True), "INV-MANAGER-MISSING")) == 1


def test_the_daemons_tick_reports_the_missing_manager_but_never_creates_one(project,
                                                                           catalog_file):
    """No watchdog in this codebase starts a process on its own, and creating the manager
    work order is starting one — the dispatcher picks it up on the next tick. The daemon
    calls `check_project(repair=True)`, so `repair` alone must not be enough."""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    store = ProjectStore(project)
    fo_id = _feature_with_children(store, "no manager, still running")
    store.close()

    catalog = load_catalog(catalog_file)
    spec = catalog.projects[0]
    fresh = ProjectStore(spec.path)
    Daemon(catalog).check_invariants(spec, fresh)

    assert fresh.manager_work_order(fo_id) is None
    assert fresh.get_feature_order(fo_id)["needs_attention"] == 1  # reported, not fixed


def test_doctor_repair_creates_exactly_one_manager_and_only_once(project, catalog_file):
    """The other half of the same rule: the user asking by hand IS the authorisation.
    Idempotent because the predicate goes false the moment a manager exists."""
    store = ProjectStore(project)
    fo_id = _feature_with_children(store, "no manager, still running")
    store.close()

    cli.main(["doctor", "--repair", "--catalog", str(catalog_file)])

    after = ProjectStore(project)
    manager = after.manager_work_order(fo_id)
    assert manager is not None and manager["kind"] == "manager"
    assert after.get_feature_order(fo_id)["needs_attention"] == 0
    after.close()

    cli.main(["doctor", "--repair", "--catalog", str(catalog_file)])

    again = ProjectStore(project)
    managers = [w for w in again.list_work_orders(include_hidden=True)
                if w["kind"] == "manager"]
    assert [m["id"] for m in managers] == [manager["id"]]


# -- the three together ---------------------------------------------------------------


def test_doctor_without_repair_writes_nothing_for_any_of_the_three(project,
                                                                  catalog_file, capsys):
    """`jarvis doctor` is READ-ONLY without `--repair`, and each id has to reach the
    reader — an invariant nobody can see fired is an invariant that did not fire."""
    store = ProjectStore(project)
    orphan = _validating_wo(store, "parked with nothing running")
    lost = _validating_wo(store, "rejected, feedback undeliverable")
    rnd = _round(store, wo_id=lost, outcome="rejected")
    _feedback_envelope(store, rnd, state="undeliverable", wo_id=lost)
    fo_id = _feature_with_children(store, "no manager, still running")
    store.close()

    rc = cli.main(["doctor", "--catalog", str(catalog_file)])

    out = capsys.readouterr().out
    assert rc == 1
    for invariant in ("INV-VALIDATION-ORPHAN", "INV-VALIDATION-FEEDBACK-LOST",
                      "INV-MANAGER-MISSING"):
        assert invariant in out
    # ...and the one of the three that `--repair` CAN fix says so, which is the only
    # thing that puts the offer in front of the user
    assert "run with --repair to fix" in out
    assert "would fix: create the feature's manager work order" in out
    after = ProjectStore(project)
    assert after.get_work_order(orphan)["needs_attention"] == 0
    assert after.get_work_order(lost)["needs_attention"] == 0
    assert after.get_feature_order(fo_id)["needs_attention"] == 0
    assert after.manager_work_order(fo_id) is None


def test_with_validation_disabled_none_of_the_three_can_fire(project, catalog_file):
    """The feature ships with `os.validation.enabled` false, and at that default none of
    the state these look for can exist: nothing sets a unit to `validating`, nothing
    posts an envelope, and `create_plan_children` creates no manager. Asserted rather
    than assumed — a watchdog that fires on healthy state spams the timeline for ever,
    because violations dedupe on `Violation.key` and never expire.
    """
    from jarvis.catalog import load_catalog

    assert load_catalog(catalog_file).os.validation.enabled is False

    store = ProjectStore(project)
    finished = store.create_work_order("shipped last week")
    store.update_work_order(finished["id"], result_summary="done")
    store.set_status(finished["id"], "completed")
    store.set_status(store.create_work_order("still going")["id"], "running")

    violations = check_project(store, repair=True)

    assert [v.invariant for v in violations
            if v.invariant.startswith(("INV-VALIDATION", "INV-MANAGER"))] == []
