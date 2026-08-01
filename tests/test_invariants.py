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
from jarvis.invariants import check_project, true_blockers
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
