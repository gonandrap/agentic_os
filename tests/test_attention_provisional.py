"""Attention while the order runs. §9 of
docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md.

Three facts, and they are the only ones this section invents:

1. an assumption carrying a PROVISIONAL APPROVAL raises no blocker at `needs_review` —
   the OS's own confirmation pass owns it. Anchored there and not at `running`, so
   issue #711's suppression cannot make the row vacuous; every such test carries its
   negative control beside it.
2. an assumption whose objection was DELIVERED raises none either: it waits on the
   worker.
3. an assumption whose objection is UNDELIVERABLE raises one — the one row here that
   adds a blocker. Undeliverable is a STATE, never an elapsed time.
"""

from __future__ import annotations

import sqlite3

import pytest

from jarvis import invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ADDED_COLUMNS, ProjectStore

REASON = "the helper is not idempotent on retry, so the second call double-counts"
ASSUMPTION = "the API is idempotent"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    # `test_attention_premature.started`'s reason: these tests stand in for the user at
    # the console, and `ops._refuse_worker_write` refuses a worker on purpose.
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def opt_in(catalog_file) -> None:
    """Auto-review ON IN THE CATALOG FILE, which is what `ops.auto_review_at` reads."""
    for key in ("validation.enabled", "validation.auto_review"):
        ops.set_config(key, True, project="proj_a", reason="testing §9",
                       catalog_path=str(catalog_file))


def order(started, *, status: str = "needs_review", verdict: str = "accept",
          ) -> tuple[ProjectStore, dict, dict]:
    """A work order in `status` with one pending assumption carrying `verdict`."""
    store = ProjectStore(started.catalog.project("proj_a").path)
    wo = store.create_work_order(title="an order mid-flight", description="d")
    aid = store.add_assumption(wo["id"], ASSUMPTION)
    if verdict:
        store.record_provisional(aid, verdict=verdict, reason=REASON, model="opus",
                                 stakes="routine")
    store.set_status(wo["id"], status)
    row = next(a for a in store.all_assumptions(wo["id"]) if a["id"] == aid)
    return store, store.get_work_order(wo["id"]), row


def objection(store, wo, a, *, state: str = "queued", delivered: bool = False):
    """File an objection on `a` over an envelope left in `state`."""
    env_id = store.post_envelope(from_role="reviewer", to_role="implementor",
                                 kind="assumption_objection", subject_wo_id=wo["id"])
    store.record_objection(a["id"], envelope_id=env_id, transport="queue")
    if state != "queued":
        store.mark_envelope(env_id, state)
    if delivered:
        store.mark_objection_delivered(a["id"])
    return env_id


def assumptions_blocker(blockers: list[str]) -> list[str]:
    return [b for b in blockers if "pending your review" in b]


# -- a provisional approval is the OS confirming, not the user deciding ----------------


def test_a_provisional_approval_at_needs_review_asks_nothing_and_its_control(
        started, catalog_file):
    """(a) and (b) in one test on purpose: the control is what stops a deleted line
    passing. The two rows differ ONLY in `provisional_verdict`."""
    opt_in(catalog_file)
    store, wo, _ = order(started, verdict="accept")

    assert assumptions_blocker(invariants.true_blockers(store, wo)) == []

    store, plain, _ = order(started, verdict="")

    assert assumptions_blocker(invariants.true_blockers(store, plain)) == [
        "1 assumption pending your review"]


def test_a_delivered_objection_waits_on_the_worker_and_its_control(
        started, catalog_file):
    """(c). Delivered: the worker owes an answer. Still queued: nobody has been told
    anything yet, and at `needs_review` #711's suppression is over — the user owes it."""
    opt_in(catalog_file)
    store, wo, a = order(started, verdict="object")
    objection(store, wo, a, delivered=True)

    assert assumptions_blocker(invariants.true_blockers(store, wo)) == []

    store, wo2, a2 = order(started, verdict="object")
    objection(store, wo2, a2)                      # queued, never delivered

    assert assumptions_blocker(invariants.true_blockers(store, wo2)) == [
        "1 assumption pending your review"]


def test_a_project_that_never_opted_in_still_owes_every_assumption(started):
    """(d). No confirmation pass will ever run here, so a provisional verdict on the row
    changes nothing: suppressing the blocker would be silence with nothing behind it."""
    store, wo, _ = order(started, verdict="accept")

    assert assumptions_blocker(invariants.true_blockers(store, wo)) == [
        "1 assumption pending your review"]


# -- undeliverable IS the user's ------------------------------------------------------


def test_a_terminal_envelope_that_never_delivered_raises_it(started, catalog_file):
    """(e)(i). The envelope ended and the worker was never told."""
    opt_in(catalog_file)
    store, wo, a = order(started, status="running", verdict="object")
    objection(store, wo, a, state="undeliverable")

    blockers = invariants.true_blockers(store, wo)

    assert blockers[0] == invariants.OBJECTION_UNDELIVERABLE_BLOCKER


def test_a_message_whose_retries_are_spent_raises_it(started, catalog_file):
    """(e)(ii). The other carrier: the envelope routed fine and the `wo_messages` row it
    became is `failed`."""
    opt_in(catalog_file)
    store, wo, a = order(started, status="running", verdict="object")
    env_id = objection(store, wo, a)
    msg_id = store.deliver_envelope(env_id, wo["id"], "the OS objects: " + REASON)
    while store.record_delivery_failure(msg_id, "transport down") != "failed":
        pass
    assert store.get_message(msg_id)["status"] == "failed"

    blockers = invariants.true_blockers(store, wo)

    assert blockers[0] == invariants.OBJECTION_UNDELIVERABLE_BLOCKER


@pytest.mark.parametrize("case,state,withdrawn", [
    ("still queued", "queued", False),        # normal operation for a whole turn
    ("withdrawn", "withdrawn", True),         # §6.6: the order stopped, nothing failed
    ("delivered_ts NULL alone", "queued", False),   # the ambiguous fact, never a trigger
])
def test_the_three_states_that_must_not_raise_it(started, catalog_file, case, state,
                                                 withdrawn):
    """(f). Raising on any of these lights "Needs you" on every objection the OS sends."""
    opt_in(catalog_file)
    store, wo, a = order(started, status="running", verdict="object")
    objection(store, wo, a, state=state)
    if withdrawn:
        store.withdraw_objection(a["id"])

    blockers = invariants.true_blockers(store, wo)

    assert invariants.OBJECTION_UNDELIVERABLE_BLOCKER not in blockers
    # ...and #711 still holds on a running order: nothing at all is owed here
    assert assumptions_blocker(blockers) == []


# -- INV-ATTENTION-PREMATURE ----------------------------------------------------------


def test_premature_leaves_an_undeliverable_objection_flagged(started, catalog_file):
    """(g). `running` is in BLOCKED_STATUSES, so clearing here would be undone by
    INV-ATTENTION-MISSING on the same tick, for ever."""
    opt_in(catalog_file)
    store, wo, a = order(started, status="running", verdict="object")
    objection(store, wo, a, state="undeliverable")
    store.flag_attention(wo["id"], invariants.OBJECTION_UNDELIVERABLE_BLOCKER)

    assert list(invariants.check_assumption_flags_are_owed(store)) == []
    assert store.get_work_order(wo["id"])["needs_attention"]


def test_premature_still_clears_the_plain_711_case(started, catalog_file):
    """The half that must not regress: an assumption recorded mid-turn with nothing
    wrong with it."""
    opt_in(catalog_file)
    store, wo, _ = order(started, status="running", verdict="")
    store.flag_attention(wo["id"], "assumptions pending review")

    (v,) = list(invariants.check_assumption_flags_are_owed(store))

    assert v.invariant == "INV-ATTENTION-PREMATURE" and v.repaired
    assert not store.get_work_order(wo["id"])["needs_attention"]


# -- rows written before the columns existed ------------------------------------------


def test_a_row_that_predates_the_columns_renders_exactly_as_before(started):
    """kn-c712a5d6: a column is untested until a test reads a row written before it."""
    project = started.catalog.project("proj_a")
    store = ProjectStore(project.path)
    wo = store.create_work_order(title="an order from before", description="")
    store.add_assumption(wo["id"], "the old assumption")
    store.set_status(wo["id"], "needs_review")
    store.close()

    conn = sqlite3.connect(project.path / ".jarvis" / "jarvis.db")
    for col in ADDED_COLUMNS["assumptions"]:
        conn.execute(f"ALTER TABLE assumptions DROP COLUMN {col}")
    conn.commit()
    conn.close()

    store = ProjectStore(project.path)                    # the upgrade
    fresh = store.get_work_order(wo["id"])

    assert invariants.true_blockers(store, fresh)[0] == "1 assumption pending your review"
