"""A live child under a settled feature says so — a HOLD, never a reopen"""

from __future__ import annotations

import pytest

from jarvis import invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def hold(fo_id: str, status: str, wo_id: str) -> str:
    """The sentence the child is expected to carry, built from the constants themselves"""
    remedy = invariants.SETTLED_FEATURE_REMEDY[status].format(fo_id=fo_id, wo_id=wo_id)
    return invariants.SETTLED_FEATURE_BLOCKER.format(fo_id=fo_id, status=status,
                                                     remedy=remedy)


def a_feature_with_a_live_child(store: ProjectStore, status: str = "failed",
                                dead: bool = True) -> tuple[dict, dict]:
    """A settled feature, one dead child that settled it, and one still running"""
    fo = store.create_feature_order(title="CSV export", description="export things")
    store.set_feature_status(fo["id"], "executing")
    killer = store.create_work_order(title="the dead one", description="do it",
                                     origin="jarvis", kind="worker", parent_id=fo["id"])
    live = store.create_work_order(title="the live one", description="do it",
                                   origin="jarvis", kind="worker", parent_id=fo["id"])
    store.set_status(live["id"], "running")
    if dead:
        store.set_status(killer["id"], "failed")
    else:
        store.set_status(killer["id"], "completed")
    if status != "executing":
        store.set_feature_status(fo["id"], status)
        store.flag_feature_attention(fo["id"], f"{killer['id']} failed")
    return store.get_feature_order(fo["id"]), store.get_work_order(live["id"])


# -- the hold


def test_a_live_child_under_a_failed_feature_leads_with_the_hold(started, store):
    """THE MISSING LINE"""
    fo, live = a_feature_with_a_live_child(store)

    blockers = invariants.true_blockers(store, live)

    assert blockers[0] == hold(fo["id"], "failed", live["id"])
    assert f"jarvis fo resume {fo['id']}" in blockers[0]


def test_one_reconcile_tick_raises_the_flag_with_that_string(started, store):
    """It reaches the user with NO new writer: INV-ATTENTION-MISSING runs over every open status plus..."""
    fo, live = a_feature_with_a_live_child(store)

    found = invariants.check_project(store)

    assert [v for v in found if v.invariant == "INV-ATTENTION-MISSING"
            and v.wo_id == live["id"]]
    row = store.get_work_order(live["id"])
    assert row["needs_attention"]
    assert row["attention_reason"] == hold(fo["id"], "failed", live["id"])


def test_reopening_the_feature_clears_the_hold_with_no_extra_command(started, store):
    """DERIVED, so self-clearing: nothing was stored at the settle site that survives the feature going..."""
    fo, live = a_feature_with_a_live_child(store)
    invariants.check_project(store)
    assert store.get_work_order(live["id"])["needs_attention"]

    ops.resume_feature_order(fo["id"])

    after = store.get_work_order(live["id"])
    assert invariants.true_blockers(store, after) == []
    assert not after["needs_attention"]


def test_the_flag_is_not_re_raised_once_the_feature_is_live(started, store):
    """The other half of kn-089de524: a tick must not write a flag the next tick cannot re-derive"""
    fo, live = a_feature_with_a_live_child(store)
    invariants.check_project(store)
    ops.resume_feature_order(fo["id"])

    for _ in range(3):
        invariants.check_project(store)

    assert not store.get_work_order(live["id"])["needs_attention"]


def test_the_false_failure_repair_lowers_the_holds_as_well(started, store):
    """The OTHER path that reopens a feature"""
    fo, live = a_feature_with_a_live_child(store)
    killer = [c for c in store.feature_children(fo["id"])
              if c["id"] != live["id"]][0]
    invariants.check_project(store)
    assert store.get_work_order(live["id"])["needs_attention"]
    store.set_status(killer["id"], "completed")  # a retry, a `wo done`, a late merge

    found = invariants.check_project(store)

    assert [v for v in found if v.invariant == "INV-FEATURE-FALSE-FAILURE"]
    assert store.get_feature_order(fo["id"])["status"] == "executing"
    after = store.get_work_order(live["id"])
    assert not after["needs_attention"]
    assert not [b for b in invariants.true_blockers(store, after)
                if "nothing will coordinate" in b]


def test_a_child_flagged_for_something_else_keeps_its_own_flag(started, store):
    """The control, and the rule the clear is written under: lowered only when `true_blockers` is EMPTY"""
    fo, live = a_feature_with_a_live_child(store)
    killer = [c for c in store.feature_children(fo["id"])
              if c["id"] != live["id"]][0]
    store.set_status(live["id"], "needs_review")
    store.add_assumption(live["id"], "assumed the schema is stable")
    invariants.check_project(store)
    store.set_status(killer["id"], "completed")

    invariants.check_project(store)

    after = store.get_work_order(live["id"])
    assert after["needs_attention"]
    assert after["attention_reason"] == "1 assumption pending your review"


# -- the controls


def test_a_child_under_an_executing_feature_carries_nothing(started, store):
    """The control for every assertion above: the hold is about the FEATURE's status"""
    _fo, live = a_feature_with_a_live_child(store, status="executing", dead=False)

    assert invariants.true_blockers(store, live) == []


def test_the_manager_order_itself_carries_nothing(started, store):
    """A manager has `parent_id` set to its feature"""
    fo, _live = a_feature_with_a_live_child(store)
    manager = store.create_manager_order(fo["id"])
    store.set_status(manager["id"], "idle")

    assert invariants.true_blockers(store, store.get_work_order(manager["id"])) == []


def test_a_cancelled_feature_names_the_cancel_remedy_and_not_fo_resume(started, store):
    """`jarvis fo resume` refuses anything but `failed`"""
    fo, live = a_feature_with_a_live_child(store, status="cancelled")

    blocker = invariants.true_blockers(store, live)[0]

    assert blocker == hold(fo["id"], "cancelled", live["id"])
    assert f"jarvis wo cancel {live['id']}" in blocker
    assert "fo resume" not in blocker


def test_a_completed_feature_names_both_ways_out(started, store):
    """The third settled status: the feature delivered and this child did not finish"""
    fo, live = a_feature_with_a_live_child(store, status="completed", dead=False)

    blocker = invariants.true_blockers(store, live)[0]

    assert blocker == hold(fo["id"], "completed", live["id"])
    assert f"jarvis wo done {live['id']}" in blocker


def test_a_child_in_validating_now_carries_the_hold_too(started, store):
    """It used to be silent, and only because the old status gate left `validating` out (docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md §5c.)"""
    fo, live = a_feature_with_a_live_child(store)
    store.set_status(live["id"], "validating")

    invariants.check_project(store)

    row = store.get_work_order(live["id"])
    assert row["needs_attention"]
    assert row["attention_reason"] == hold(fo["id"], fo["status"], live["id"])


def test_an_assumption_the_user_owes_still_comes_first(started, store):
    """The hold is a fact about the work order's CONTEXT"""
    _fo, live = a_feature_with_a_live_child(store)
    store.set_status(live["id"], "needs_review")
    store.add_assumption(live["id"], "assumed the schema is stable")

    blockers = invariants.true_blockers(store, store.get_work_order(live["id"]))

    assert blockers[0] == "1 assumption pending your review"
    assert any("nothing will coordinate" in b for b in blockers)


def test_a_standalone_work_order_pays_for_no_parent_read(started, store):
    """No `parent_id`, no hold and no query: the arm is gated on the column"""
    wo = ops.create_work_order("proj_a", "ship the thing", description="do it")
    store.set_status(wo["id"], "running")

    assert invariants.true_blockers(store, store.get_work_order(wo["id"])) == []
