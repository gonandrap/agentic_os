"""The message bus: typed envelopes posted to a ROLE, delivered by a pure router.

The substrate the validation panel stands on, and it has no callers yet — later work
orders in that feature post to it. What these tests protect is therefore the *shape*
rather than any feature built on it:

* **the sender never resolves anything.** `delivered_wo_id` is written by the router and
  by nothing else, which is the only record of who read an envelope;
* **the router, not the sender, decides what happens when a role is unfilled** — a
  deferral with no manager is filed as a backlog item here, exactly as it is today;
* **the vocabulary is walked, not listed.** Adding a kind or an addressable role without
  wiring it fails this file, instead of producing an `undeliverable` row months later.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from jarvis import bus, invariants
from jarvis.bus import BusError, DeferralRequest, ReviewFeedback, Subject
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import (
    ENVELOPE_KINDS,
    ENVELOPE_ROLES,
    ENVELOPE_STATES,
    ProjectStore,
)


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def central(jarvis_home):
    c = CentralStore()
    yield c
    c.close()


@pytest.fixture()
def daemon(catalog_file):
    return Daemon(load_catalog(catalog_file))


def spec(catalog_file):
    return load_catalog(catalog_file).projects[0]


def a_work_order(store, title="implement the thing", **kw):
    return store.create_work_order(title, "do it", **kw)["id"]


def a_manager(store, fo_id, status="waiting_input"):
    """A manager work order under `fo_id`.

    `manager` joined WO_KINDS with the validation layer's vocabulary (wo-3ce42dc7), but
    nothing creates one yet, so every manager in this file is built by hand. The router
    only ever asks the database what kind a row is.
    """
    wo_id = a_work_order(store, "own the feature", parent_id=fo_id, kind="manager")
    store.set_status(wo_id, status)
    return wo_id


def env(store, env_id):
    return next(e for e in store.envelopes() if e["id"] == env_id)


def messages(store, wo_id):
    return [m for m in store.list_messages(wo_id) if m["direction"] == "user_to_agent"]


def sample_payload(cls):
    """Build a payload of `cls` without naming any kind — see the vocabulary test."""
    kw = {}
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING:
            continue
        kw[f.name] = 1 if "int" in str(f.type) else f"sample {f.name}"
    return cls(**kw)


def feedback(**kw):
    base = {"round": 1, "outcome": "rejected", "reason": "the tests do not exercise it",
            "asks": ("add a test that fails without the change",)}
    return ReviewFeedback(**{**base, **kw})


# -- posting resolves nothing --------------------------------------------------------


def test_post_queues_and_only_delivery_ever_resolves(store, central):
    """Paired deliberately: without the second half, "the sender resolved nothing" cannot
    be told apart from "the column is never written at all"."""
    wo_id = a_work_order(store)
    env_id = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                      to_role="implementor", payload=feedback())

    posted = env(store, env_id)
    assert posted["state"] == "queued"
    assert posted["delivered_wo_id"] is None
    assert posted["kind"] == "review_feedback"

    assert bus.deliver(store, central, posted) == "delivered"
    assert env(store, env_id)["delivered_wo_id"] == wo_id


def test_an_envelope_to_the_implementor_reaches_the_subject_work_order(store, central):
    wo_id = a_work_order(store)
    env_id = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                      to_role="implementor",
                      payload=feedback(reason="no test covers the new branch"))

    assert bus.deliver(store, central, env(store, env_id)) == "delivered"

    queued = messages(store, wo_id)
    assert len(queued) == 1
    assert "no test covers the new branch" in queued[0]["content"]
    assert queued[0]["source"] == bus.MESSAGE_SOURCE
    assert env(store, env_id)["state"] == "delivered"


def test_the_manager_route_and_what_the_router_does_without_one(store, central):
    """Both halves of the manager rule in one test: with a manager the envelope reaches
    it; with none, the ROUTER files the backlog item itself rather than the sender
    branching on whether a recipient exists."""
    fo_id = store.create_feature_order("ship the exporter")["id"]
    manager = a_manager(store, fo_id)
    child = a_work_order(store, parent_id=fo_id)

    with_manager = bus.post(store, subject=Subject(wo_id=child), from_role="implementor",
                            to_role="manager",
                            payload=DeferralRequest("rate limit the exporter",
                                                    "out of scope for this work order"))
    assert bus.deliver(store, central, env(store, with_manager)) == "delivered"
    assert env(store, with_manager)["delivered_wo_id"] == manager
    assert len(messages(store, manager)) == 1

    orphan = a_work_order(store, "a standalone work order")  # no parent feature
    without = bus.post(store, subject=Subject(wo_id=orphan), from_role="implementor",
                       to_role="manager",
                       payload=DeferralRequest("cache the catalog read",
                                               "noticed on the way past"))
    assert bus.deliver(store, central, env(store, without)) == "handled_by_router"

    row = env(store, without)
    assert row["delivered_wo_id"] is None
    assert not messages(store, orphan)
    titles = [b["title"] for b in central.list_backlog("proj_a")]
    assert "cache the catalog read" in titles
    assert "filed backlog item" in row["note"]


def test_feedback_to_a_cancelled_work_order_is_undeliverable(store, central):
    """Paired with the live case: feedback that reached nobody must never look like
    feedback that was acted on."""
    live = a_work_order(store, "still running")
    dead = a_work_order(store, "cancelled halfway")
    store.set_status(dead, "cancelled")

    to_dead = bus.post(store, subject=Subject(wo_id=dead), from_role="reviewer",
                       to_role="implementor", payload=feedback())
    to_live = bus.post(store, subject=Subject(wo_id=live), from_role="reviewer",
                       to_role="implementor", payload=feedback())

    assert bus.deliver(store, central, env(store, to_dead)) == "undeliverable"
    assert bus.deliver(store, central, env(store, to_live)) == "delivered"

    assert env(store, to_dead)["note"]
    assert env(store, to_dead)["delivered_wo_id"] is None
    assert not messages(store, dead)
    assert len(messages(store, live)) == 1


def test_a_cancelled_manager_under_a_live_feature_flags_the_feature(store, central):
    """A feature whose manager is gone cannot run its loop, and only the user can decide
    what to do about that — so this one is not silently filed anywhere."""
    fo_id = store.create_feature_order("ship the exporter")["id"]
    store.set_feature_status(fo_id, "executing")
    a_manager(store, fo_id, status="cancelled")
    child = a_work_order(store, parent_id=fo_id)

    env_id = bus.post(store, subject=Subject(wo_id=child), from_role="implementor",
                      to_role="manager",
                      payload=DeferralRequest("split the exporter", "too big to finish"))
    assert bus.deliver(store, central, env(store, env_id)) == "undeliverable"

    assert "cancelled" in env(store, env_id)["note"]
    feature = store.get_feature_order(fo_id)
    assert feature["needs_attention"] == 1
    assert "manager" in (feature["attention_reason"] or "")
    assert not central.list_backlog("proj_a")  # NOT quietly turned into a backlog item


# -- ordering, transactionality, exactly-once ----------------------------------------


def test_envelopes_are_delivered_oldest_first(store, daemon, catalog_file):
    wo_id = a_work_order(store)
    for n in (1, 2, 3):
        bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                 to_role="implementor", payload=feedback(round=n, reason=f"round {n}"))

    daemon.deliver_envelopes(spec(catalog_file), store)

    bodies = [m["content"] for m in messages(store, wo_id)]
    assert [f"round {n}" in b for n, b in zip((1, 2, 3), bodies)] == [True] * 3


def test_delivery_is_transactional(store, central, monkeypatch):
    """The redelivery guard, and nothing else tests it: if the state moved without the
    message, a daemon dying between the two would drop the envelope on the floor."""
    wo_id = a_work_order(store)
    env_id = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                      to_role="implementor", payload=feedback())

    def boom(*a, **k):
        raise RuntimeError("the database went away mid-insert")

    monkeypatch.setattr(ProjectStore, "queue_message", boom)
    assert bus.deliver(store, central, env(store, env_id)) == "queued"

    assert env(store, env_id)["state"] == "queued"
    assert env(store, env_id)["delivered_wo_id"] is None
    assert store.conn.execute("SELECT COUNT(*) c FROM wo_messages").fetchone()["c"] == 0
    assert env(store, env_id)["attempts"] == 1  # the attempt survives the rollback


def test_ticking_twice_delivers_an_envelope_exactly_once(store, daemon, catalog_file):
    wo_id = a_work_order(store)
    bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
             to_role="implementor", payload=feedback())

    daemon.deliver_envelopes(spec(catalog_file), store)
    daemon.deliver_envelopes(spec(catalog_file), store)

    assert len(messages(store, wo_id)) == 1


def test_an_empty_envelope_table_costs_one_query(store, daemon, catalog_file):
    """With no envelope ever posted the OS behaves exactly as it does today: the new tick
    step is one indexed lookup that finds nothing."""
    seen: list[str] = []
    store.conn.set_trace_callback(seen.append)
    try:
        daemon.deliver_envelopes(spec(catalog_file), store)
    finally:
        store.conn.set_trace_callback(None)
    assert len(seen) == 1, seen


def test_the_daemon_tick_routes_envelopes(store, daemon):
    """The wiring itself: `deliver_envelopes` is part of the tick, not just a method
    somebody could call."""
    wo_id = a_work_order(store)
    store.set_status(wo_id, "running")  # no session_id, so nothing is spawned from it
    bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
             to_role="implementor", payload=feedback(reason="reached through the tick"))

    daemon.tick()

    assert [m["source"] for m in messages(store, wo_id)] == [bus.MESSAGE_SOURCE]


# -- the typing at the boundary ------------------------------------------------------


def test_an_illegal_role_kind_or_state_is_refused(store):
    wo_id = a_work_order(store)
    ok = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                  to_role="implementor", payload=feedback())
    assert env(store, ok)["state"] == "queued"

    with pytest.raises(BusError):
        bus.post(store, subject=Subject(wo_id=wo_id), from_role="revewer",
                 to_role="implementor", payload=feedback())
    with pytest.raises(BusError):
        bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                 to_role="implementer", payload=feedback())
    with pytest.raises(AssertionError):
        store.post_envelope(subject_wo_id=wo_id, from_role="reviewer",
                            to_role="implementor", kind="review_feedbck")
    with pytest.raises(AssertionError):
        store.mark_envelope(ok, "delivred")
    store.mark_envelope(ok, "delivered")  # ...and the legal one goes through
    assert env(store, ok)["state"] == "delivered"


def test_a_subject_is_exactly_one_of_a_work_order_and_a_feature(store):
    assert Subject(wo_id="wo-1").wo_id == "wo-1"
    assert Subject(fo_id="fo-1").fo_id == "fo-1"
    with pytest.raises(BusError):
        Subject()
    with pytest.raises(BusError):
        Subject(wo_id="wo-1", fo_id="fo-1")
    with pytest.raises(ValueError):
        store.post_envelope(subject_wo_id="wo-1", subject_fo_id="fo-1",
                            from_role="reviewer", to_role="implementor",
                            kind="review_feedback")


def test_post_refuses_a_payload_that_is_not_a_registered_dataclass(store):
    """A bare dict getting through means the typing is decorative."""
    wo_id = a_work_order(store)
    with pytest.raises(BusError):
        bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                 to_role="implementor",
                 payload={"round": 1, "outcome": "rejected",  # type: ignore[arg-type]
                          "reason": "r"})
    ok = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                  to_role="implementor", payload=feedback())
    assert env(store, ok)["kind"] == "review_feedback"


def test_kind_always_matches_the_payload_type(store):
    """The signature assertion is the one that survives a later refactor: a `kind`
    parameter is the "kind disagrees with its payload" bug, reintroduced."""
    wo_id = a_work_order(store)
    fb = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                  to_role="implementor", payload=feedback())
    df = bus.post(store, subject=Subject(wo_id=wo_id), from_role="implementor",
                  to_role="manager", payload=DeferralRequest("t", "w"))

    assert env(store, fb)["kind"] == "review_feedback"
    assert env(store, df)["kind"] == "deferral_request"
    assert "kind" not in inspect.signature(bus.post).parameters


def test_a_stored_payload_that_no_longer_parses_is_undeliverable(store, central):
    """Paired with a well-formed one: a malformed message is never delivered as a
    message, and the reason lands in `note` where somebody can read it."""
    wo_id = a_work_order(store)
    broken = store.post_envelope(subject_wo_id=wo_id, from_role="reviewer",
                                 to_role="implementor", kind="review_feedback",
                                 payload={"rounds": 1, "verdict": "rejected"})
    fine = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                    to_role="implementor", payload=feedback())

    assert bus.deliver(store, central, env(store, broken)) == "undeliverable"
    assert bus.deliver(store, central, env(store, fine)) == "delivered"

    assert "ReviewFeedback" in env(store, broken)["note"]
    assert len(messages(store, wo_id)) == 1  # only the well-formed one


def test_a_payload_survives_the_round_trip(store):
    """Tuples are the trap: JSON has none, so `asks` comes back a list unless the parse
    coerces it — and these dataclasses are frozen and compared by value."""
    wo_id = a_work_order(store)
    sent = feedback(asks=("add a failing test", "name the branch it covers"))
    env_id = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                      to_role="implementor", payload=sent)
    row = env(store, env_id)
    assert bus.parse_payload(row["kind"], row["payload"]) == sent


# -- the vocabulary is walked, not listed --------------------------------------------


def test_every_kind_and_every_addressable_role_is_wired(store, central):
    """Iterating the tuples is the whole value: a kind or a role added without a routing
    rule fails here, instead of quietly producing `undeliverable` rows.
    """
    assert set(bus.ADDRESSABLE_ROLES) | set(bus.SENDER_ONLY_ROLES) == set(ENVELOPE_ROLES)
    assert not set(bus.ADDRESSABLE_ROLES) & set(bus.SENDER_ONLY_ROLES)

    fo_id = store.create_feature_order("ship the exporter")["id"]
    a_manager(store, fo_id)
    subject_wo = a_work_order(store, "a child of the feature", parent_id=fo_id)
    fillers = {"implementor": Subject(wo_id=subject_wo),
               "manager": Subject(fo_id=fo_id)}
    assert set(fillers) == set(bus.ADDRESSABLE_ROLES), "a role with no filler fixture"

    for role, subject in fillers.items():
        env_id = bus.post(store, subject=subject, from_role="reviewer", to_role=role,
                          payload=DeferralRequest("t", "w"))
        assert bus.resolve(store, env(store, env_id)) is not None, role

    for kind in ENVELOPE_KINDS:
        payload = sample_payload(bus.PAYLOADS[kind])  # KeyError = a kind with no type
        env_id = bus.post(store, subject=Subject(wo_id=subject_wo),
                          from_role="reviewer", to_role="implementor", payload=payload)
        state = bus.deliver(store, central, env(store, env_id))
        assert state != "undeliverable", f"{kind} is not wired to anything"
        assert state in ENVELOPE_STATES


# -- liveness: the bus owns its own -------------------------------------------------


def test_inv_envelope_stuck_retries_and_then_gives_up(store, monkeypatch):
    """Paired with a healthy envelope: an invariant that fires on healthy state is worse
    than no invariant."""
    wo_id = a_work_order(store)
    stuck = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                     to_role="implementor", payload=feedback())
    fresh = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                     to_role="implementor", payload=feedback(round=2))
    store.conn.execute("UPDATE envelopes SET attempts=? WHERE id=?",
                       (bus.DELIVERY_ATTEMPT_CEILING, stuck))

    def boom(*a, **k):
        raise RuntimeError("nothing is listening")

    monkeypatch.setattr(ProjectStore, "queue_message", boom)
    found = [v for v in invariants.check_project(store)
             if v.invariant == "INV-ENVELOPE-STUCK"]

    assert len(found) == 1, found
    assert found[0].repaired and found[0].repair
    assert found[0].wo_id == wo_id
    assert env(store, stuck)["state"] == "undeliverable"
    assert env(store, stuck)["note"]
    assert env(store, fresh)["state"] == "queued"  # healthy: left entirely alone
    assert env(store, fresh)["attempts"] == 0


def test_inv_envelope_stuck_prefers_a_retry_that_works(store):
    """The repair is a real delivery attempt, not a giving-up: `bus.deliver` is
    transactional and therefore safe to run again."""
    wo_id = a_work_order(store)
    env_id = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                      to_role="implementor", payload=feedback())
    store.conn.execute("UPDATE envelopes SET attempts=? WHERE id=?",
                       (bus.DELIVERY_ATTEMPT_CEILING, env_id))

    found = [v for v in invariants.check_project(store)
             if v.invariant == "INV-ENVELOPE-STUCK"]

    assert len(found) == 1
    assert env(store, env_id)["state"] == "delivered"
    assert len(messages(store, wo_id)) == 1


def test_a_read_only_check_delivers_nothing(store):
    """`jarvis doctor` without --repair promises to change nothing, and the repair here
    would write to the CENTRAL store too — which no proxy over the project store can
    intercept."""
    wo_id = a_work_order(store)
    env_id = bus.post(store, subject=Subject(wo_id=wo_id), from_role="reviewer",
                      to_role="implementor", payload=feedback())
    store.conn.execute("UPDATE envelopes SET attempts=? WHERE id=?",
                       (bus.DELIVERY_ATTEMPT_CEILING, env_id))

    found = [v for v in invariants.check_project(store, repair=False)
             if v.invariant == "INV-ENVELOPE-STUCK"]

    assert len(found) == 1
    assert "would" in found[0].repair
    assert env(store, env_id)["state"] == "queued"
    assert not messages(store, wo_id)


# -- the seam ------------------------------------------------------------------------


def test_the_bus_imports_nothing_above_the_two_stores():
    """Walked as an AST rather than checked in `sys.modules`: this codebase's style is
    lazy imports inside function bodies, which a loaded-modules check cannot see. Same
    precedent as `test_neo_panel.py::test_neo_never_imports_the_panel`.
    """
    tree = ast.parse(Path(bus.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [a.name for a in node.names] + [node.module or ""]

    forbidden = {"daemon", "ops", "panel", "validation", "neo"}
    offenders = [n for n in imported if forbidden & set(n.split("."))]
    assert not offenders, offenders
