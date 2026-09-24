"""An assumption recorded mid-turn is not yet the user's. GitHub issue #711.

`autoreview.decide`'s condition 2 says Neo judges an assumption only in `needs_review`.
The attention list has to say the same thing, or a project that has opted in asks the
user for every decision it has already promised to take itself — while the worker is
still typing. The bug it was found as: three assumptions flagged "Needs you" while the
worker spent another 25 minutes in the same turn, so the order read as stalled.

The suppression's whole safety rests on it being TEMPORARY, so the test that matters
most here is `test_delivery_hands_the_decision_straight_back`.
"""

from __future__ import annotations

import pytest

from jarvis import invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

#: `test_autoreview.ROUTINE`'s shape: an assumption no reviewer would hold.
ROUTINE = "named the helper `_render_row`, matching the two beside it"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    # `test_autoreview.started`'s reason: these tests stand in for the user at the
    # console, and `ops._refuse_worker_write` refuses a worker on purpose.
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def opt_in(catalog_file) -> None:
    """Auto-review ON IN THE CATALOG FILE, which is what `ops.auto_review_at` reads.

    `test_autoreview.park` flips the daemon's in-memory spec instead; nothing a `jarvis`
    process resolves for itself can see that, so these tests write the file.
    """
    for key in ("validation.enabled", "validation.auto_review"):
        ops.set_config(key, True, project="proj_a", reason="testing #711",
                       catalog_path=str(catalog_file))


def running(started) -> tuple[ProjectStore, dict]:
    store = ProjectStore(started.catalog.project("proj_a").path)
    wo = ops.create_work_order("proj_a", "add feature X", description="d")
    store.set_status(wo["id"], "running")
    return store, store.get_work_order(wo["id"])


def test_an_assumption_recorded_mid_turn_does_not_ask_the_user(started, catalog_file):
    opt_in(catalog_file)
    store, wo = running(started)

    ops.assume(wo["id"], ROUTINE)

    fresh = store.get_work_order(wo["id"])
    assert not fresh["needs_attention"]
    assert invariants.true_blockers(store, fresh) == []
    # ...and the assumption itself is untouched: still pending, still on the record, and
    # still what Neo will be asked about on delivery.
    assert len(store.pending_assumptions(wo["id"])) == 1


def test_the_same_assumption_asks_the_user_when_the_project_has_not_opted_in(started):
    """The negative half. Nothing about the suppression may leak into a project whose
    assumptions have always been the user's."""
    store, wo = running(started)

    ops.assume(wo["id"], ROUTINE)

    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"]
    assert "assumption" in invariants.true_blockers(store, fresh)[0]


def test_neo_switched_off_is_not_an_opt_in(started, catalog_file, monkeypatch):
    """`Daemon.auto_review`'s second guard, and it decides this too: a fleet with Neo off
    files questions nothing drains, so the assumption is the user's after all."""
    opt_in(catalog_file)

    def neo_off(*a, **k):
        catalog = load_catalog(catalog_file)
        catalog.os.neo.enabled = False
        return catalog

    monkeypatch.setattr(ops, "resolve_catalog", neo_off)
    store, wo = running(started)

    ops.assume(wo["id"], ROUTINE)

    assert store.get_work_order(wo["id"])["needs_attention"]


@pytest.mark.parametrize("status", ["needs_review", "failed", "budget_exhausted"])
def test_delivery_hands_the_decision_straight_back(started, catalog_file, status):
    """NOT A SUPPRESSION THAT OUTLIVES THE TURN. `needs_review` is where Neo picks the
    assumption up; the other two it never reaches at all. The blocker is derivable again
    in all three, which is what keeps every case Neo holds (`decide` conditions 4-7)
    reaching the user."""
    opt_in(catalog_file)
    store, wo = running(started)
    ops.assume(wo["id"], ROUTINE)
    store.set_status(wo["id"], status)

    fresh = store.get_work_order(wo["id"])
    assert any("assumption" in b for b in invariants.true_blockers(store, fresh))
    # ...and the flag goes back up by itself, through the invariant that exists for work
    # that needs the user and does not say so.
    list(invariants.check_blocked_work_is_surfaced(store))
    assert store.get_work_order(wo["id"])["needs_attention"]


def test_a_flag_raised_before_the_fix_clears_itself(started, catalog_file):
    """INV-ATTENTION-PREMATURE. Every order flagged by the old code drops the flag on the
    next reconcile tick rather than waiting for someone to clear it by hand."""
    opt_in(catalog_file)
    store, wo = running(started)
    ops.assume(wo["id"], ROUTINE)
    store.flag_attention(wo["id"], "assumptions pending review")

    (v,) = list(invariants.check_assumption_flags_are_owed(store))

    assert v.invariant == "INV-ATTENTION-PREMATURE" and v.repaired
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_a_hook_reason_on_the_same_order_is_left_alone(started, catalog_file):
    """NARROWER THAN "true_blockers is empty", and this is the case that forces it. A
    hook's reason is more specific than anything this module can derive —
    INV-ATTENTION-REASON refuses to clobber one, and clearing it here would be the same
    bug from the other side."""
    opt_in(catalog_file)
    store, wo = running(started)
    ops.assume(wo["id"], ROUTINE)
    store.flag_attention(wo["id"], "Claude is waiting for your input")

    assert list(invariants.check_assumption_flags_are_owed(store)) == []
    assert store.get_work_order(wo["id"])["needs_attention"]
