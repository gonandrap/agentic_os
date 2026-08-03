"""Dependency edges between work orders: `depends_on`, and what it does to dispatch.

Phase 1 of the feature-order design. The whole mechanism is one column plus one `WHERE`
clause, and the reason it is only that is worth restating where the tests live: a
`blocked` status would have to be kept in step with the dependencies' real statuses for
ever, whereas a derived one cannot go stale. So these tests assert on what the daemon
*claims*, not on a stored label.

The rule under test is the strict one: a dependency is satisfied when it reaches
`completed`, and `waiting_pr_merge` is not it. That is affordable only because the merge
poller lands the completion by itself — see test_wo_pr_merge.py — so the user pays no
extra step per edge.
"""

from __future__ import annotations

import json

import pytest

from jarvis import cli, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.invariants import DEAD_DEPENDENCY_BLOCKER, check_project, true_blockers
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


# -- claiming -----------------------------------------------------------------------


def test_a_work_order_with_no_dependencies_is_claimed_as_before(started, store):
    """The migration is invisible to everything that does not opt in."""
    wo = ops.create_work_order("proj_a", "ordinary work")

    claimed = store.claim_next_pending()

    assert claimed is not None and claimed["id"] == wo["id"]
    assert store.dependencies(claimed) == []


def test_a_blocked_work_order_is_passed_over_and_stays_pending(started, store):
    first = ops.create_work_order("proj_a", "the schema change")
    second = ops.create_work_order("proj_a", "the code that needs it",
                                   depends_on=[first["id"]])

    # `second` is younger, so the only reason to skip it is the edge — but claim the
    # dependency out of the way first, otherwise "oldest wins" would explain the result.
    assert store.claim_next_pending()["id"] == first["id"]
    assert store.claim_next_pending() is None

    assert store.get_work_order(second["id"])["status"] == "pending"


def test_the_blocked_one_does_not_hold_up_unrelated_work(started, store):
    """Blocking is not a queue head. An older blocked work order must not starve a
    younger free one — that would make one dependency edge stall the whole project."""
    dep = ops.create_work_order("proj_a", "dependency")
    blocked = ops.create_work_order("proj_a", "blocked", depends_on=[dep["id"]])
    free = ops.create_work_order("proj_a", "unrelated")

    claimed = [store.claim_next_pending(), store.claim_next_pending()]

    ids = [c["id"] for c in claimed if c]
    assert ids == [dep["id"], free["id"]]
    assert blocked["id"] not in ids


def test_completing_the_dependency_releases_it(started, store):
    dep = ops.create_work_order("proj_a", "dependency")
    child = ops.create_work_order("proj_a", "child", depends_on=[dep["id"]])
    store.claim_next_pending()  # takes `dep`
    assert store.claim_next_pending() is None

    store.set_status(dep["id"], "completed")

    assert store.claim_next_pending()["id"] == child["id"]


@pytest.mark.parametrize("status", ["running", "needs_review", "waiting_pr_merge",
                                    "cancelled", "failed"])
def test_only_completed_satisfies_a_dependency(started, store, status):
    """`waiting_pr_merge` is the load-bearing case: the dependency's code is in an
    unmerged branch, so a child cut from the main tree would not contain it."""
    dep = ops.create_work_order("proj_a", "dependency")
    child = ops.create_work_order("proj_a", "child", depends_on=[dep["id"]])
    store.claim_next_pending()

    store.set_status(dep["id"], status)

    assert store.claim_next_pending() is None
    assert store.get_work_order(child["id"])["status"] == "pending"


def test_every_dependency_must_be_satisfied_not_just_one(started, store):
    a = ops.create_work_order("proj_a", "a")
    b = ops.create_work_order("proj_a", "b")
    ops.create_work_order("proj_a", "child", depends_on=[a["id"], b["id"]])
    store.claim_next_pending(); store.claim_next_pending()

    store.set_status(a["id"], "completed")
    assert store.claim_next_pending() is None

    store.set_status(b["id"], "completed")
    assert store.claim_next_pending() is not None


def test_the_daemon_does_not_dispatch_a_blocked_work_order(started, project):
    """The claim filter is the only thing standing between an edge and a worker, so
    assert it at the level the user actually experiences: a tick."""
    dep = ops.create_work_order("proj_a", "dependency")
    blocked = ops.create_work_order("proj_a", "blocked", depends_on=[dep["id"]])

    started.tick()

    store = ProjectStore(project)
    try:
        assert store.get_work_order(blocked["id"])["status"] == "pending"
        assert store.get_work_order(dep["id"])["status"] != "pending"
    finally:
        store.close()


# -- writing the edge ---------------------------------------------------------------


def test_a_dependency_that_does_not_exist_is_refused(started):
    with pytest.raises(ops.OpsError, match="wo-nosuch"):
        ops.create_work_order("proj_a", "child", depends_on=["wo-nosuch"])


def test_a_work_order_cannot_depend_on_itself(started, store):
    with pytest.raises(ValueError, match="itself"):
        store.create_work_order("child", wo_id="wo-selfref",
                                depends_on=["wo-selfref"])


def test_the_cli_splits_a_comma_separated_list(started, capsys, store):
    dep = ops.create_work_order("proj_a", "dependency")
    other = ops.create_work_order("proj_a", "other")

    assert cli.main(["wo", "create", "proj_a", "child",
                     "--depends-on", f"{dep['id']}, {other['id']}", "--json"]) == 0

    created = json.loads(capsys.readouterr().out)
    assert created["depends_on"] == [dep["id"], other["id"]]
    row = store.get_work_order(created["created"])
    assert store.dependencies(row) == [dep["id"], other["id"]]


# -- what the user is told ----------------------------------------------------------


def test_a_blocked_work_order_does_not_read_as_merely_pending(started, store):
    dep = ops.create_work_order("proj_a", "dependency")
    child = ops.create_work_order("proj_a", "child", depends_on=[dep["id"]])

    label = invariants.status_label(store, store.get_work_order(child["id"]))

    assert label == f"pending — blocked by {dep['id']}"
    assert invariants.status_label(store, store.get_work_order(dep["id"])) == "pending"


def test_waiting_on_live_work_asks_nothing_of_the_user(started, store):
    """The ordinary case must stay silent. A dependency that is simply not finished yet
    is the system working, and putting it in the NEEDS YOU strip is how that strip stops
    being read (the same argument `waiting_pr_merge` was decided on)."""
    dep = ops.create_work_order("proj_a", "dependency")
    child = ops.create_work_order("proj_a", "child", depends_on=[dep["id"]])
    store.set_status(dep["id"], "running")

    assert true_blockers(store, store.get_work_order(child["id"])) == []


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_a_dependency_that_can_never_complete_does_ask(started, store, status):
    dep = ops.create_work_order("proj_a", "dependency")
    child = ops.create_work_order("proj_a", "child", depends_on=[dep["id"]])

    store.set_status(dep["id"], status)

    assert true_blockers(store, store.get_work_order(child["id"])) == \
        [DEAD_DEPENDENCY_BLOCKER]


def test_a_deleted_dependency_strands_rather_than_releases(started, store):
    """Deleting the thing a work order was told to build on must not be a way of
    quietly letting it run without that work."""
    dep = ops.create_work_order("proj_a", "dependency")
    child = ops.create_work_order("proj_a", "child", depends_on=[dep["id"]])
    store.claim_next_pending()
    ops.delete_work_order(dep["id"])

    assert store.claim_next_pending() is None
    blockers = store.unfinished_dependencies(child["id"])
    assert [b["status"] for b in blockers] == ["missing"]
    assert true_blockers(store, store.get_work_order(child["id"])) == \
        [DEAD_DEPENDENCY_BLOCKER]


def test_the_attention_reason_survives_a_reconcile_tick(started, store):
    """INV-ATTENTION-REASON rewrites any flag `true_blockers` cannot re-derive, so a new
    kind of attention that is not taught to it is relabelled within one tick."""
    dep = ops.create_work_order("proj_a", "dependency")
    child = ops.create_work_order("proj_a", "child", depends_on=[dep["id"]])
    store.set_status(dep["id"], "cancelled")

    check_project(store, repair=True)
    check_project(store, repair=True)

    row = store.get_work_order(child["id"])
    assert row["needs_attention"] == 1
    assert row["attention_reason"] == DEAD_DEPENDENCY_BLOCKER


# -- cutting the edge ---------------------------------------------------------------


def test_unblock_cuts_only_the_edges_that_can_never_clear(started, store):
    dead = ops.create_work_order("proj_a", "dead")
    live = ops.create_work_order("proj_a", "live")
    child = ops.create_work_order("proj_a", "child",
                                  depends_on=[dead["id"], live["id"]])
    store.set_status(dead["id"], "cancelled")
    store.set_status(live["id"], "running")

    out = ops.unblock_work_order(child["id"])

    assert out["dropped"] == [dead["id"]]
    assert out["still_blocked_by"] == [live["id"]]
    assert store.claim_next_pending() is None  # `live` still holds it

    store.set_status(live["id"], "completed")
    assert store.claim_next_pending()["id"] == child["id"]


def test_unblock_refuses_when_the_work_it_waits_on_is_still_live(started, store):
    dep = ops.create_work_order("proj_a", "dependency")
    child = ops.create_work_order("proj_a", "child", depends_on=[dep["id"]])
    store.set_status(dep["id"], "running")

    with pytest.raises(ops.OpsError, match="not stranded"):
        ops.unblock_work_order(child["id"])

    assert store.dependencies(store.get_work_order(child["id"])) == [dep["id"]]


def test_unblock_all_cuts_live_edges_too(started, store):
    dep = ops.create_work_order("proj_a", "dependency")
    child = ops.create_work_order("proj_a", "child", depends_on=[dep["id"]])
    store.set_status(dep["id"], "running")

    ops.unblock_work_order(child["id"], drop_all=True)

    assert store.dependencies(store.get_work_order(child["id"])) == []
    assert store.claim_next_pending()["id"] == child["id"]


def test_unblock_puts_the_attention_flag_down(started, store):
    dep = ops.create_work_order("proj_a", "dependency")
    child = ops.create_work_order("proj_a", "child", depends_on=[dep["id"]])
    store.set_status(dep["id"], "cancelled")
    check_project(store, repair=True)
    assert store.get_work_order(child["id"])["needs_attention"] == 1

    ops.unblock_work_order(child["id"])
    check_project(store, repair=True)

    assert store.get_work_order(child["id"])["needs_attention"] == 0


def test_unblock_on_an_unblocked_work_order_says_so(started):
    wo = ops.create_work_order("proj_a", "free")

    with pytest.raises(ops.OpsError, match="not blocked"):
        ops.unblock_work_order(wo["id"])
