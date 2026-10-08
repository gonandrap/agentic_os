"""A live child under a settled feature says so — a HOLD, never a reopen.

fo-ac00376e settled `failed`, which closed its manager, and siblings wo-3db50904 and
wo-0cb6dc6b kept running with no addressee for the `manager` role. Nothing on either child
said anything: `true_blockers` had no line about a parent feature, so INV-ATTENTION-MISSING
had nothing to flag, and the one envelope that went `undeliverable` was reported by a
report-only invariant and left there.

Neo question 1393 ruled out reopening the feature at the fork: `Daemon.settle_features`
re-fails it on the next tick off the same dead child, through the predicate the settler and
INV-FEATURE-FALSE-FAILURE deliberately share, and the user watches it flap. The hold is
derived, so it clears itself.

Design: docs/superpowers/specs/2026-10-07-a-settled-features-live-children-must-have-a-
manager-or-a-hold.md §(b).
"""

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
    """The sentence the child is expected to carry, built from the constants themselves —
    a second copy of the template here would pass while the user read something else."""
    remedy = invariants.SETTLED_FEATURE_REMEDY[status].format(fo_id=fo_id, wo_id=wo_id)
    return invariants.SETTLED_FEATURE_BLOCKER.format(fo_id=fo_id, status=status,
                                                     remedy=remedy)


def a_feature_with_a_live_child(store: ProjectStore, status: str = "failed",
                                dead: bool = True) -> tuple[dict, dict]:
    """A settled feature, one dead child that settled it, and one still running."""
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


# -- the hold ----------------------------------------------------------------------------


def test_a_live_child_under_a_failed_feature_leads_with_the_hold(started, store):
    """THE MISSING LINE. Nothing will coordinate this work order until the feature is
    live again, and the way out under `failed` is `jarvis fo resume`."""
    fo, live = a_feature_with_a_live_child(store)

    blockers = invariants.true_blockers(store, live)

    assert blockers[0] == hold(fo["id"], "failed", live["id"])
    assert f"jarvis fo resume {fo['id']}" in blockers[0]


def test_one_reconcile_tick_raises_the_flag_with_that_string(started, store):
    """It reaches the user with NO new writer: INV-ATTENTION-MISSING runs over
    BLOCKED_STATUSES and raises `blockers[0]`."""
    fo, live = a_feature_with_a_live_child(store)

    found = invariants.check_project(store)

    assert [v for v in found if v.invariant == "INV-ATTENTION-MISSING"
            and v.wo_id == live["id"]]
    row = store.get_work_order(live["id"])
    assert row["needs_attention"]
    assert row["attention_reason"] == hold(fo["id"], "failed", live["id"])


def test_reopening_the_feature_clears_the_hold_with_no_extra_command(started, store):
    """DERIVED, so self-clearing: nothing was stored at the settle site that survives the
    feature going back to work (kn-089de524)."""
    fo, live = a_feature_with_a_live_child(store)
    invariants.check_project(store)
    assert store.get_work_order(live["id"])["needs_attention"]

    ops.resume_feature_order(fo["id"])

    after = store.get_work_order(live["id"])
    assert invariants.true_blockers(store, after) == []
    assert not after["needs_attention"]


def test_the_flag_is_not_re_raised_once_the_feature_is_live(started, store):
    """The other half of kn-089de524: a tick must not write a flag the next tick cannot
    re-derive, and it must not put back one the reopening took down."""
    fo, live = a_feature_with_a_live_child(store)
    invariants.check_project(store)
    ops.resume_feature_order(fo["id"])

    for _ in range(3):
        invariants.check_project(store)

    assert not store.get_work_order(live["id"])["needs_attention"]


def test_the_false_failure_repair_lowers_the_holds_as_well(started, store):
    """The OTHER path that reopens a feature. INV-FEATURE-FALSE-FAILURE reopens it with
    nobody typing, so a hold left standing there is a sentence that is false and that
    nothing re-derives — the defect class this work order exists to fix."""
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
    """The control, and the rule the clear is written under: lowered only when
    `true_blockers` is EMPTY, so another blocker is somebody else's reason and is left
    exactly as it was."""
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


# -- the controls ------------------------------------------------------------------------


def test_a_child_under_an_executing_feature_carries_nothing(started, store):
    """The control for every assertion above: the hold is about the FEATURE's status, and
    an ordinary live child under a live feature is asked for nothing."""
    _fo, live = a_feature_with_a_live_child(store, status="executing", dead=False)

    assert invariants.true_blockers(store, live) == []


def test_the_manager_order_itself_carries_nothing(started, store):
    """A manager has `parent_id` set to its feature, and `_close_feature_manager`
    settling it when the feature settles is correct rather than a hold."""
    fo, _live = a_feature_with_a_live_child(store)
    manager = store.create_manager_order(fo["id"])
    store.set_status(manager["id"], "idle")

    assert invariants.true_blockers(store, store.get_work_order(manager["id"])) == []


def test_a_cancelled_feature_names_the_cancel_remedy_and_not_fo_resume(started, store):
    """`jarvis fo resume` refuses anything but `failed`, so naming it under a cancelled
    feature would send the user at a command that errors."""
    fo, live = a_feature_with_a_live_child(store, status="cancelled")

    blocker = invariants.true_blockers(store, live)[0]

    assert blocker == hold(fo["id"], "cancelled", live["id"])
    assert f"jarvis wo cancel {live['id']}" in blocker
    assert "fo resume" not in blocker


def test_a_completed_feature_names_both_ways_out(started, store):
    """The third settled status: the feature delivered and this child did not finish, so
    the question is whether it is still wanted."""
    fo, live = a_feature_with_a_live_child(store, status="completed", dead=False)

    blocker = invariants.true_blockers(store, live)[0]

    assert blocker == hold(fo["id"], "completed", live["id"])
    assert f"jarvis wo done {live['id']}" in blocker


def test_a_child_in_validating_is_never_flagged_with_the_hold(started, store):
    """Deliberate, and it is BLOCKED_STATUSES that makes it so: `validating` is the one
    open status it leaves out, because the round machine owns that work order and nobody
    is waiting on the manager."""
    _fo, live = a_feature_with_a_live_child(store)
    store.set_status(live["id"], "validating")

    invariants.check_project(store)

    assert not store.get_work_order(live["id"])["needs_attention"]


def test_an_assumption_the_user_owes_still_comes_first(started, store):
    """The hold is a fact about the work order's CONTEXT, not about its delivery, and it
    must not displace a decision the user owes."""
    _fo, live = a_feature_with_a_live_child(store)
    store.set_status(live["id"], "needs_review")
    store.add_assumption(live["id"], "assumed the schema is stable")

    blockers = invariants.true_blockers(store, store.get_work_order(live["id"]))

    assert blockers[0] == "1 assumption pending your review"
    assert any("nothing will coordinate" in b for b in blockers)


def test_a_standalone_work_order_pays_for_no_parent_read(started, store):
    """No `parent_id`, no hold and no query: the arm is gated on the column."""
    wo = ops.create_work_order("proj_a", "ship the thing", description="do it")
    store.set_status(wo["id"], "running")

    assert invariants.true_blockers(store, store.get_work_order(wo["id"])) == []
