"""A turn that dies AFTER the work was delivered stays `failed` — and must not fail its
feature.

wo-604b5b99's turn died in a stampede cleanup with PR #839 open and three assumptions on
record. `Daemon.settle_work_order` read the TURN's fate and never the ORDER's delivery, so
the child went `failed` — which `dead_feature_children` counted as dead, so
`Daemon.settle_features` failed fo-ac00376e, which closed its manager, which left two live
siblings with no addressee. One branch, three defects.

THE STATUS IS NOT WHERE THE FIX GOES. `needs_review` is the queue the user works through
to decide on DELIVERED work; an order whose turn died mid-flight does not belong in it,
however much it had already produced. So the order stays `failed`, says so on the phone,
and the exemption lives in `dead_feature_children`'s predicate — keyed on the
`TURN_DIED_AFTER_DELIVERY_EVENT` event, never on a live `pr_url`.

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


def tick(daemon: Daemon, store: ProjectStore) -> None:
    daemon.settle_features(daemon.catalog.project("proj_a"), store)


# -- the delivered child: `failed`, and said so -----------------------------------------


def test_a_delivered_child_whose_turn_died_lands_in_failed(started, store):
    """The user's ruling: an order whose turn died mid-duration is `failed`, not queued
    for review. What the delivery buys it is the EVENT, which is what the feature-level
    predicate reads."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    for n in ("one", "two", "three"):
        store.add_assumption(child["id"], f"assumed {n}")
    a_dead_turn(store, child["id"])

    row = settle(started, store, child["id"])

    assert row["status"] == "failed"
    kinds = [e["kind"] for e in store.list_events(child["id"])]
    assert invariants.TURN_DIED_AFTER_DELIVERY_EVENT in kinds
    assert "turn_retries_exhausted" in kinds, "the retries WERE spent; say so too"


def test_an_order_that_delivered_nothing_still_fails_with_the_generic_reason(started,
                                                                            store):
    """The control. Same status either way now, so the thing that separates the two paths
    is the event and the sentence — not where the order sits."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    a_dead_turn(store, child["id"])

    row = settle(started, store, child["id"])

    assert row["status"] == "failed"
    assert row["needs_attention"]
    # ONE SPELLING. The write site and `true_blockers` carried two, so
    # INV-ATTENTION-REASON relabelled this flag on the very next tick.
    assert row["attention_reason"] == invariants.WORKER_FAILED_BLOCKER
    assert invariants.true_blockers(store, row)[0] == invariants.WORKER_FAILED_BLOCKER
    assert invariants.TURN_DIED_AFTER_DELIVERY_EVENT not in [
        e["kind"] for e in store.list_events(child["id"])]


def test_an_assumption_alone_counts_as_delivery(started, store):
    """`pr_url` is not the only artefact an order cannot take back. A recorded assumption
    is a decision the user owes, and burying it would be the same defect."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.add_assumption(child["id"], "assumed the schema is stable")
    a_dead_turn(store, child["id"])

    settle(started, store, child["id"])

    assert store.events_of_kind(child["id"],
                                invariants.TURN_DIED_AFTER_DELIVERY_EVENT)


def test_a_result_summary_alone_is_not_delivery(started, store):
    """Deliberately NOT `result_summary`: a worker can write one and keep working, so it
    does not say the order has nothing left to run."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], result_summary="half done")
    a_dead_turn(store, child["id"])

    settle(started, store, child["id"])

    assert not store.events_of_kind(child["id"],
                                    invariants.TURN_DIED_AFTER_DELIVERY_EVENT)


# -- the feature underneath it ----------------------------------------------------------


def test_the_feature_stays_executing_and_the_child_is_not_dead(started, store):
    """Defect 2 and 3 stop being reachable from this cause: the feature never settles, so
    its manager is never closed and its siblings keep their addressee."""
    fo = a_feature(store)
    child, sibling = a_child(store, fo["id"]), a_child(store, fo["id"], "the sibling")
    store.update_work_order(child["id"], pr_url=PR)
    a_dead_turn(store, child["id"])
    settle(started, store, child["id"])

    tick(started, store)

    assert store.get_feature_order(fo["id"])["status"] == "executing"
    assert invariants.dead_feature_children(
        store, store.feature_children(fo["id"])) == []
    assert store.get_work_order(sibling["id"])["status"] == "pending"


def test_a_failed_child_without_the_event_still_fails_its_feature(started, store):
    """THE CONTROL THAT PROVES THE EXEMPTION IS NARROW. Keyed on the event and never on
    `pr_url`: a failed order holding a pull request for any other reason is still dead to
    the feature."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    store.set_status(child["id"], "failed")

    assert [c["id"] for c in invariants.dead_feature_children(
        store, store.feature_children(fo["id"]))] == [child["id"]]

    tick(started, store)

    assert store.get_feature_order(fo["id"])["status"] == "failed"


def test_the_feature_does_not_complete_while_the_child_sits_there(started, store):
    """Dead to NEITHER rule, unlike a `superseded` child: the exemption stops the feature
    failing and does NOT count the child towards completion. There is an open pull request
    outstanding and the honest state is `executing`."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    a_dead_turn(store, child["id"])
    settle(started, store, child["id"])

    tick(started, store)

    assert store.get_feature_order(fo["id"])["status"] == "executing"


def test_and_completes_once_the_user_resolves_the_child(started, store):
    """The other half: `jarvis wo retry`, `jarvis wo done` or `jarvis fo resume` is what
    moves it, and then the ordinary completion rule runs."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    a_dead_turn(store, child["id"])
    settle(started, store, child["id"])
    tick(started, store)

    store.set_status(child["id"], "completed")  # `jarvis wo done`
    tick(started, store)

    assert store.get_feature_order(fo["id"])["status"] == "completed"


# -- the sentence, which is derived and must survive a re-derivation --------------------


def test_the_failed_arm_names_the_dead_session_and_the_ways_out(started, store):
    """The `failed` arm of `true_blockers` is where the read lives now. The sentence has
    to say the delivery is on record, that the order is `failed` because the turn never
    finished, and name every way out."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    a_dead_turn(store, child["id"])

    row = settle(started, store, child["id"])

    assert row["attention_reason"] == invariants.TURN_DIED_AFTER_DELIVERY_BLOCKER
    sentence = invariants.TURN_DIED_AFTER_DELIVERY_BLOCKER
    assert "jarvis wo retry" in sentence
    assert "jarvis wo done" in sentence
    assert "jarvis wo review" in sentence
    assert "failed" in sentence


def test_the_needs_review_ladder_is_back_to_the_idle_sentence(started, store):
    """Nothing settles to `needs_review` on this path any more, so the split in the
    ladder's last arm is unreachable and is gone. A `needs_review` order with nothing
    pending reads IDLE_NO_FINISH_BLOCKER, exactly as it did before."""
    wo = ops.create_work_order("proj_a", "ship the thing", description="do it")
    store.set_status(wo["id"], "needs_review")
    store.add_event(wo["id"], invariants.TURN_DIED_AFTER_DELIVERY_EVENT, {})

    assert invariants.true_blockers(store, store.get_work_order(wo["id"]))[0] == \
        invariants.IDLE_NO_FINISH_BLOCKER


def test_pending_assumptions_are_what_the_flag_says(started, store):
    """wo-604b5b99's case: three decisions the user owes outrank anything about the
    session that died. `failed` assumptions are deliberately not gated behind a
    confirmation pass — no such pass ever reaches this status."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    for n in ("one", "two", "three"):
        store.add_assumption(child["id"], f"assumed {n}")
    a_dead_turn(store, child["id"])

    row = settle(started, store, child["id"])

    assert row["attention_reason"] == "3 assumptions pending your review"


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


# -- the notification, the second half of the ruling ------------------------------------


def test_the_notification_names_the_delivery_and_not_a_bare_failure(started, store):
    """"Proper notification to the user about the failed order". The old line read
    `wo-… worker turn failed`, which sent the user looking for a bug in the work. ONE
    notification, and it says what is on record."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    store.add_assumption(child["id"], "assumed the schema is stable")
    a_dead_turn(store, child["id"])

    settle(started, store, child["id"])

    notes = _notifications(store, child["id"])
    assert len(notes) == 1, "one notification, never two"
    note = notes[0]
    assert "delivered" in note["title"]
    assert child["id"] in note["title"]
    assert PR in note["body"]
    assert "1 assumption" in note["body"]
    assert f"jarvis wo retry {child['id']}" in note["body"]


def test_the_undelivered_notification_is_unchanged(started, store):
    """The control: an order with nothing on record still gets the bare line, because
    there is nothing else true to say about it."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    a_dead_turn(store, child["id"])

    settle(started, store, child["id"])

    notes = _notifications(store, child["id"])
    assert len(notes) == 1
    assert "still failing after 1 usage-limit retries" in notes[0]["title"]


def _notifications(store: ProjectStore, wo_id: str) -> list[dict]:
    return [n for n in store.unrouted_notifications() if n["wo_id"] == wo_id]


# -- the ungoverned order ---------------------------------------------------------------


def test_an_ungoverned_order_settles_and_asks_for_nothing(started, store):
    """A session the user injected got no briefing and no `jarvis wo finish` contract, so
    `true_blockers`' `failed` arm is behind `governed` and derives NOTHING for it.
    Subscripting that empty list raised inside the reconcile tick — a worse failure than
    the one being fixed — and asking an injected session for nothing is correct
    (`Daemon.retire_ungoverned`).

    Paired with the governed control in the same test: without it, a flag that was never
    raised at all would pass the first half perfectly.
    """
    loose = store.create_work_order(title="the user's own session", description="do it",
                                    origin="injected", kind="worker")
    store.update_work_order(loose["id"], pr_url=PR)
    a_dead_turn(store, loose["id"])

    row = settle(started, store, loose["id"])

    assert row["status"] == "failed"
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
