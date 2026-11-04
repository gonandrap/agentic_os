"""A manager's turn end is its delivery (io-d5e62d4d)"""

from __future__ import annotations

import json

import pytest

from jarvis import autoreview, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

pytestmark = pytest.mark.usefixtures("neo_projects")

ROUTINE = "filed no order for the review item: the child already covers it"
MARKED = "reused the production api key"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    spec = daemon.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_review = True
    # The wide net's marker, which holds without a classifier call
    spec.validation.stakes_classifier = "regex"
    for key in ("validation.enabled", "validation.auto_review"):
        ops.set_config(key, True, project="proj_a",
                       reason="the manager's rows are under test",
                       catalog_path=str(catalog_file))
    assert ops.autoreview_off_because(spec.path) == ""
    return daemon


def idle_manager(*assumptions: str) -> tuple[ProjectStore, dict, str]:
    fo = ops.create_feature_order("proj_a", "export", description="an exporter")
    _, path, _ = ops.find_feature_order(fo["id"])
    store = ProjectStore(path)
    manager = store.create_manager_order(fo["id"])
    store.set_status(manager["id"], "running")
    for text in assumptions:
        ops.assume(manager["id"], text)
    store.set_status(manager["id"], "idle")
    return store, store.get_work_order(manager["id"]), fo["id"]


def ask(daemon, store):
    daemon.auto_review(daemon.catalog.project("proj_a"), store)


def sweep(store) -> None:
    for check in (invariants.check_assumption_flags_are_owed,
                  invariants.check_blocked_work_is_surfaced):
        list(check(store))


def questions() -> list[dict]:
    neo = NeoStore()
    try:
        return neo.list_questions()
    finally:
        neo.close()


def test_a_routine_row_is_confirmed_without_a_flag_and_the_status_is_unmoved(started):
    store, wo, _ = idle_manager(f"FORCE_ACCEPT — {ROUTINE}")

    ask(started, store)
    (q,) = questions()
    assert "a feature's manager delivers no result" in q["question"]
    # With Neo: not the user's yet
    assert not invariants.true_blockers(store, store.get_work_order(wo["id"]))

    started._neo_drain()
    sweep(store)

    (row,) = store.all_assumptions(wo["id"])
    fresh = store.get_work_order(wo["id"])
    assert row["status"] == "accepted"
    assert fresh["status"] == "idle"
    assert not fresh["needs_attention"]
    assert not ops.show_feature_order(fresh["parent_id"], "proj_a")["manager"][
        "pending_assumptions"]


def test_a_marker_held_row_flags_the_user_and_their_accept_clears_it(started):
    store, wo, fo_id = idle_manager(MARKED)

    ask(started, store)
    sweep(store)

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "idle"
    assert fresh["needs_attention"]
    assert "1 assumption pending your review" in fresh["attention_reason"]
    assert ops.waiting_on(store, fresh)["what"] == "assumptions"
    assert ops.show_feature_order(fo_id, "proj_a")["manager"]["pending_assumptions"] == [
        {"n": 1, "content": MARKED}]

    ops.review_work_order(wo["id"], accept=True)
    sweep(store)

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "idle"
    assert not fresh["needs_attention"]


def test_a_row_the_os_has_not_reached_is_not_the_users(started):
    store, wo, _ = idle_manager(ROUTINE)

    sweep(store)

    fresh = store.get_work_order(wo["id"])
    assert not fresh["needs_attention"]
    assert ops.waiting_on(store, fresh)["what"] != "assumptions"


@pytest.mark.parametrize("feedback, heard", [("the child does not cover it", "the child"),
                                             ("", "#1")])
def test_a_rejection_reaches_the_manager_session(started, feedback, heard):
    store, wo, _ = idle_manager(MARKED)
    ask(started, store)
    sweep(store)

    out = ops.review_work_order(wo["id"], accept=False, feedback=feedback)

    assert "delivered" in out
    (msg,) = store.queued_messages(wo["id"])
    assert heard in msg["content"]
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "idle"
    assert not fresh["needs_attention"]


def test_the_managers_next_turn_answers_a_refusal(started, monkeypatch):
    """A manager writes no `finished` event: without this every later row waits forever"""
    store, wo, _ = idle_manager(MARKED)
    ask(started, store)
    ops.review_work_order(wo["id"], accept=False, feedback="no")
    store.set_status(wo["id"], "running")
    ops.assume(wo["id"], ROUTINE)
    store.set_status(wo["id"], "idle")
    monkeypatch.setattr(store, "latest_turn", lambda _: {"state": "running", "ended_at": 0})
    assert not ops.refusal_answered(store, wo["id"])

    ask(started, store)
    held = [json.loads(e["payload"]) for e in store.events_of_kind(wo["id"],
                                                                   "autoreview_held")]
    assert held[-1]["code"] == autoreview.HELD_REFUSAL_UNANSWERED
    # A hold that lifts by itself is not the user's
    assert not invariants.true_blockers(store, store.get_work_order(wo["id"]))

    monkeypatch.setattr(store, "latest_turn", lambda _: {"state": "done", "ended_at": float("inf")})
    assert ops.refusal_answered(store, wo["id"])
    before = len(questions())
    ask(started, store)
    assert len(questions()) == before + 1


def test_another_kinds_idle_is_not_a_delivery(started):
    """`decide` and the scan both key on the manager's contract, not the bare status"""
    store, wo, _ = idle_manager(ROUTINE)
    assert autoreview.decide(store.all_assumptions(wo["id"])[0], {**wo, "kind": "worker"},
                             started.catalog.project("proj_a").validation
                             ).code == autoreview.HELD_STATUS
