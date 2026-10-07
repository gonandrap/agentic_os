"""A turn that dies AFTER the work was delivered must not settle its order `failed`.

wo-604b5b99's turn died in a stampede cleanup with PR #839 open and three assumptions on
record. `Daemon.settle_work_order` read the TURN's fate and never the ORDER's delivery, so
the child went `failed` — which `dead_feature_children` counts as dead, so
`Daemon.settle_features` failed fo-ac00376e, which closed its manager, which left two live
siblings with no addressee. One branch, three defects.

Design: docs/superpowers/specs/2026-10-07-a-settled-features-live-children-must-have-a-
manager-or-a-hold.md §(a).
"""

from __future__ import annotations

import pytest

from jarvis import invariants, ops, worker_session
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

PR = "https://github.com/x/y/pull/839"

#: A usage-limit refusal whose window has long since reopened, so the pause is due and —
#: with the cap monkeypatched to zero — exhausted. The shape `claude_cli.usage_limit`
#: parses, as staged by tests/test_rate_limit_retry.py.
REFUSAL = "Claude AI usage limit reached|1000000000"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    monkeypatch.setattr(worker_session, "MAX_RATE_LIMIT_RETRIES", 0)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def a_dead_turn(store: ProjectStore, wo_id: str) -> None:
    """The order mid-turn, and the turn gone: `failed`, with its retries spent."""
    store.set_status(wo_id, "running")
    turn = store.create_turn(wo_id, "dispatch", "do the work")
    store.finish_turn(turn["id"], "failed", error=REFUSAL, cost_usd=0.1)


def a_child(store: ProjectStore, fo_id: str, title: str = "build the exporter") -> dict:
    return store.create_work_order(title=title, description="do it", origin="jarvis",
                                   kind="worker", parent_id=fo_id)


def a_feature(store: ProjectStore) -> dict:
    fo = store.create_feature_order(title="CSV export", description="export things")
    store.set_feature_status(fo["id"], "executing")
    return fo


def settle(daemon: Daemon, store: ProjectStore, wo_id: str) -> dict:
    spec = daemon.catalog.project("proj_a")
    daemon.settle_work_order(spec, store, store.get_work_order(wo_id))
    return store.get_work_order(wo_id)


# -- the delivered child -----------------------------------------------------------------


def test_a_delivered_child_whose_turn_died_lands_in_needs_review(started, store):
    """THE BUG. The order delivered a pull request and three assumptions; the only thing
    that failed is the session, and `needs_review` is the status that means a person
    decides what happens to delivered work."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    for n in ("one", "two", "three"):
        store.add_assumption(child["id"], f"assumed {n}")
    a_dead_turn(store, child["id"])

    row = settle(started, store, child["id"])

    assert row["status"] == "needs_review"
    kinds = [e["kind"] for e in store.list_events(child["id"])]
    assert invariants.TURN_DIED_AFTER_DELIVERY_EVENT in kinds
    assert "turn_retries_exhausted" in kinds, "the retries WERE spent; say so too"


def test_the_feature_stays_executing_and_the_child_is_not_dead(started, store):
    """Defect 2 and 3 stop being reachable from this cause at all: the feature never
    settles, so its manager is never closed and its siblings keep their addressee."""
    fo = a_feature(store)
    child, sibling = a_child(store, fo["id"]), a_child(store, fo["id"], "the sibling")
    store.update_work_order(child["id"], pr_url=PR)
    a_dead_turn(store, child["id"])
    settle(started, store, child["id"])

    started.settle_features(started.catalog.project("proj_a"), store)

    assert store.get_feature_order(fo["id"])["status"] == "executing"
    assert invariants.dead_feature_children(store.feature_children(fo["id"])) == []
    assert store.get_work_order(sibling["id"])["status"] == "pending"


def test_an_order_that_delivered_nothing_still_fails(started, store):
    """The control, and the whole `failed` path: no pull request, no assumption, nothing
    to review. A turn death there IS the order's failure."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    a_dead_turn(store, child["id"])

    row = settle(started, store, child["id"])

    assert row["status"] == "failed"
    assert row["needs_attention"]
    assert row["attention_reason"] == "worker turn failed — review and retry"
    assert invariants.TURN_DIED_AFTER_DELIVERY_EVENT not in [
        e["kind"] for e in store.list_events(child["id"])]


def test_an_assumption_alone_counts_as_delivery(started, store):
    """`pr_url` is not the only artefact an order cannot take back. A recorded assumption
    is a decision the user owes, and failing the order would bury it."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.add_assumption(child["id"], "assumed the schema is stable")
    a_dead_turn(store, child["id"])

    assert settle(started, store, child["id"])["status"] == "needs_review"


def test_a_result_summary_alone_is_not_delivery(started, store):
    """Deliberately NOT `result_summary`: a worker can write one and keep working, so it
    does not say the order has nothing left to run."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], result_summary="half done")
    a_dead_turn(store, child["id"])

    assert settle(started, store, child["id"])["status"] == "failed"


# -- the attention reason, which is derived and must survive a re-derivation -------------


def test_pending_assumptions_are_what_the_flag_says(started, store):
    """wo-604b5b99's case: three decisions the user owes outrank anything about the
    session that died."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    for n in ("one", "two", "three"):
        store.add_assumption(child["id"], f"assumed {n}")
    a_dead_turn(store, child["id"])

    row = settle(started, store, child["id"])

    assert row["attention_reason"] == "3 assumptions pending your review"


def test_with_nothing_pending_the_flag_names_the_dead_session(started, store):
    """The sentence that did not exist: the ladder's final arm said the worker stopped
    mid-task without `jarvis wo finish`, which is false twice — it finished, and the turn
    did not stop, it died."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    a_dead_turn(store, child["id"])

    row = settle(started, store, child["id"])

    assert row["attention_reason"] == invariants.TURN_DIED_AFTER_DELIVERY_BLOCKER


def test_an_ungoverned_order_settles_and_asks_for_nothing(started, store):
    """A session the user injected got no briefing and no `jarvis wo finish` contract, so
    `true_blockers`' whole `needs_review` ladder is behind `governed` and derives NOTHING
    for it. Subscripting that empty list raised inside the reconcile tick — a worse
    failure than the one being fixed — and asking an injected session for nothing is
    correct (`Daemon.retire_ungoverned`).

    Paired with the governed control in the same test: without it, a flag that was never
    raised at all would pass the first half perfectly.
    """
    loose = store.create_work_order(title="the user's own session", description="do it",
                                    origin="injected", kind="worker")
    store.update_work_order(loose["id"], pr_url=PR)
    a_dead_turn(store, loose["id"])

    row = settle(started, store, loose["id"])

    assert row["status"] == "needs_review"
    assert invariants.TURN_DIED_AFTER_DELIVERY_EVENT in [
        e["kind"] for e in store.list_events(loose["id"])]
    assert not row["needs_attention"]
    assert invariants.true_blockers(store, row) == []

    governed = ops.create_work_order("proj_a", "the OS's own", description="do it")
    store.update_work_order(governed["id"], pr_url=PR)
    a_dead_turn(store, governed["id"])

    asks = settle(started, store, governed["id"])

    assert asks["needs_attention"]
    assert asks["attention_reason"] == invariants.TURN_DIED_AFTER_DELIVERY_BLOCKER


def test_the_reason_survives_a_full_invariant_pass(started, store):
    """kn-089de524 and INV-ATTENTION-REASON: a flag this module cannot re-derive is
    relabelled on the next tick, so the daemon's write and `true_blockers` have to agree
    exactly."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    a_dead_turn(store, child["id"])
    settle(started, store, child["id"])

    found = invariants.check_project(store)

    assert not [v for v in found if v.wo_id == child["id"]
                and v.invariant.startswith("INV-ATTENTION")]
    assert (store.get_work_order(child["id"])["attention_reason"]
            == invariants.TURN_DIED_AFTER_DELIVERY_BLOCKER)


def test_the_idle_sentence_still_covers_an_order_that_just_stopped(started, store):
    """The control for the split arm: with no delivered-death event the same status and
    the same empty pending list still read IDLE_NO_FINISH_BLOCKER. Splitting case 4 must
    not take the sentence away from the state it was written for."""
    wo = ops.create_work_order("proj_a", "ship the thing", description="do it")
    store.set_status(wo["id"], "needs_review")

    assert invariants.true_blockers(store, store.get_work_order(wo["id"]))[0] == \
        invariants.IDLE_NO_FINISH_BLOCKER
