"""A turn that dies AFTER the work was delivered stays `failed` — and must not fail its feature (#839)"""

from __future__ import annotations

import pytest

from jarvis import invariants, ops, worker_session
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

PR = "https://github.com/x/y/pull/839"

#: A usage-limit refusal whose window has long since reopened, so the pause is due and
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
    """The order mid-turn, and the turn gone: `failed`, with its retries spent"""
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


# -- the delivered child: `failed`, and said so


def test_a_delivered_child_whose_turn_died_lands_in_failed(started, store):
    """The user's ruling: an order whose turn died mid-duration is `failed`, not queued for review"""
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
    """The control"""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    a_dead_turn(store, child["id"])

    row = settle(started, store, child["id"])

    assert row["status"] == "failed"
    assert row["needs_attention"]
    # ONE SPELLING
    assert row["attention_reason"] == invariants.WORKER_FAILED_BLOCKER
    assert invariants.true_blockers(store, row)[0] == invariants.WORKER_FAILED_BLOCKER
    assert invariants.TURN_DIED_AFTER_DELIVERY_EVENT not in [
        e["kind"] for e in store.list_events(child["id"])]


def test_an_assumption_alone_counts_as_delivery(started, store):
    """`pr_url` is not the only artefact an order cannot take back"""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.add_assumption(child["id"], "assumed the schema is stable")
    a_dead_turn(store, child["id"])

    settle(started, store, child["id"])

    assert store.events_of_kind(child["id"],
                                invariants.TURN_DIED_AFTER_DELIVERY_EVENT)


def test_a_result_summary_alone_is_not_delivery(started, store):
    """Deliberately NOT `result_summary`: a worker can write one and keep working"""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], result_summary="half done")
    a_dead_turn(store, child["id"])

    settle(started, store, child["id"])

    assert not store.events_of_kind(child["id"],
                                    invariants.TURN_DIED_AFTER_DELIVERY_EVENT)


# -- the feature underneath it


def test_the_feature_stays_executing_and_the_child_is_not_dead(started, store):
    """Defect 2 and 3 stop being reachable from this cause: the feature never settles"""
    fo = a_feature(store)
    child, sibling = a_child(store, fo["id"]), a_child(store, fo["id"], "the sibling")
    store.update_work_order(child["id"], pr_url=PR)
    a_dead_turn(store, child["id"])
    settle(started, store, child["id"])

    tick(started, store)

    assert store.get_feature_order(fo["id"])["status"] == "executing"
    assert invariants.dead_feature_children(
        store, store.feature_children(fo["id"], kinds=("worker",))) == []
    assert store.get_work_order(sibling["id"])["status"] == "pending"


def test_a_failed_child_without_the_event_still_fails_its_feature(started, store):
    """THE CONTROL THAT PROVES THE EXEMPTION IS NARROW"""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    store.set_status(child["id"], "failed")

    assert [c["id"] for c in invariants.dead_feature_children(
        store, store.feature_children(fo["id"], kinds=("worker",)))] == [child["id"]]

    tick(started, store)

    assert store.get_feature_order(fo["id"])["status"] == "failed"


def test_the_feature_does_not_complete_while_the_child_sits_there(started, store):
    """Dead to NEITHER rule, unlike a `superseded` child: the exemption stops the feature failing and does..."""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    a_dead_turn(store, child["id"])
    settle(started, store, child["id"])

    tick(started, store)

    assert store.get_feature_order(fo["id"])["status"] == "executing"


def test_and_completes_once_the_user_resolves_the_child(started, store):
    """The other half: `jarvis wo retry`"""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    a_dead_turn(store, child["id"])
    settle(started, store, child["id"])
    tick(started, store)

    store.set_status(child["id"], "completed")  # `jarvis wo done`
    tick(started, store)

    assert store.get_feature_order(fo["id"])["status"] == "completed"


# -- the sentence, which is derived and must survive a re-derivation


def test_the_failed_arm_names_the_dead_session_and_the_ways_out(started, store):
    """The `failed` arm of `true_blockers` is where the read lives now"""
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
    """Nothing settles to `needs_review` on this path any more"""
    wo = ops.create_work_order("proj_a", "ship the thing", description="do it")
    store.set_status(wo["id"], "needs_review")
    store.add_event(wo["id"], invariants.TURN_DIED_AFTER_DELIVERY_EVENT, {})

    assert invariants.true_blockers(store, store.get_work_order(wo["id"]))[0] == \
        invariants.IDLE_NO_FINISH_BLOCKER


def test_pending_assumptions_are_what_the_flag_says(started, store):
    """wo-604b5b99's case: three decisions the user owes outrank anything about the session that died"""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    store.update_work_order(child["id"], pr_url=PR)
    for n in ("one", "two", "three"):
        store.add_assumption(child["id"], f"assumed {n}")
    a_dead_turn(store, child["id"])

    row = settle(started, store, child["id"])

    assert row["attention_reason"] == "3 assumptions pending your review"


def test_the_reason_survives_a_full_invariant_pass(started, store):
    """kn-089de524 and INV-ATTENTION-REASON: a flag this module cannot re-derive is relabelled on the next..."""
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


# -- the notification, the second half of the ruling


def test_the_notification_names_the_delivery_and_not_a_bare_failure(started, store):
    """"Proper notification to the user about the failed order\""""
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
    """The control: an order with nothing on record still gets the bare line"""
    fo = a_feature(store)
    child = a_child(store, fo["id"])
    a_dead_turn(store, child["id"])

    settle(started, store, child["id"])

    notes = _notifications(store, child["id"])
    assert len(notes) == 1
    assert "still failing after 1 usage-limit retries" in notes[0]["title"]


def _notifications(store: ProjectStore, wo_id: str) -> list[dict]:
    return [n for n in store.unrouted_notifications() if n["wo_id"] == wo_id]


# -- the ungoverned order


def test_an_ungoverned_order_settles_and_asks_for_nothing(started, store):
    """A session the user injected got no briefing and no `jarvis wo finish` contract"""
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
