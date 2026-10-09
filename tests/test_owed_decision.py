"""An invariant the OS cannot repair owes a DECISION, and says so on the work order.

Spec docs/superpowers/specs/2026-10-09-an-unrepairable-invariant-owes-a-decision.md.
The seven test obligations in its last section, in order.

The fixtures come from `test_work_lands`: INV-WORK-LANDED is the first owed checker, and
driving it end to end needs the same repository with a real history and the same fake
`gh`. Reproducing them here would be a second copy of the one seam that matters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import invariants, ops
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from test_work_lands import (PR, _delivered, _refresh, _row,  # noqa: F401
                             started)


def _tick(daemon: Daemon, project: Path, *, sweep: bool = False) -> None:
    store = ProjectStore(project)
    try:
        daemon.check_invariants(daemon.catalog.project("proj_a"), store,
                                sweep_landings=sweep)
    finally:
        store.close()


def _events(project: Path, wo_id: str, kind: str) -> list[dict]:
    store = ProjectStore(project)
    try:
        return store.events_of_kind(wo_id, kind)
    finally:
        store.close()


def _invariant_notes(project: Path) -> list[dict]:
    store = ProjectStore(project)
    try:
        return [n for n in store.unrouted_notifications()
                if n["source"] == "invariants"]
    finally:
        store.close()


def _unmerged(project: Path, daemon: Daemon, fake_gh) -> dict:
    """A completed order whose recorded pull request is open — the wo-1abd3886 shape."""
    wo = _delivered(project, "launcher contract", PR, code="launcher")
    fake_gh.set_pr(PR, "OPEN")
    _refresh(daemon, project)
    return wo


# -- 1. flagged exactly once, and on the attention list --------------------------------


def test_an_unmerged_pull_request_flags_its_order_exactly_once(started, project,
                                                               fake_gh):
    """Obligation 1. wo-1abd3886 sat `completed` with an open PR for 6.5 days with
    nothing but one `warning` among 230 unacked inbox items."""
    wo = _unmerged(project, started, fake_gh)

    _tick(started, project, sweep=True)
    _tick(started, project, sweep=True)
    _tick(started, project)

    row = _row(project, wo["id"])
    assert row["needs_attention"] == 1
    assert "Merge it" in row["attention_reason"]
    assert len(_events(project, wo["id"], "attention")) == 1
    # The attention item REPLACES the inbox line rather than joining it.
    assert _invariant_notes(project) == []
    items = [i for i in ops.os_status()["attention"] if i["wo_id"] == wo["id"]]
    assert [i["reason"] for i in items] == [row["attention_reason"]]


def test_the_owed_blocker_is_derived_and_not_a_reason_of_the_daemons_own(started,
                                                                        project,
                                                                        fake_gh):
    """§3: the flag carries `true_blockers(store, wo)[0]`, so every surface agrees."""
    wo = _unmerged(project, started, fake_gh)

    _tick(started, project, sweep=True)

    store = ProjectStore(project)
    try:
        fresh = store.get_work_order(wo["id"])
        assert invariants.true_blockers(store, fresh)[0] == fresh["attention_reason"]
        assert [r["owed"] for r in store.owed_violations(wo["id"])] == \
            [fresh["attention_reason"]]
    finally:
        store.close()


# -- 2. an ack puts it down for good ---------------------------------------------------


@pytest.mark.parametrize("sweep", [True, False])
def test_an_acked_owed_blocker_is_never_raised_again(started, project, fake_gh, sweep):
    """Obligation 2. `acknowledged()` filtering at the tail of `true_blockers` is the
    whole of the mechanism — no new ack machinery."""
    wo = _unmerged(project, started, fake_gh)
    _tick(started, project, sweep=True)

    assert ops.ack_attention(wo["id"])["acknowledged"] == [wo["id"]]
    _tick(started, project, sweep=sweep)

    assert _row(project, wo["id"])["needs_attention"] == 0
    assert len(_events(project, wo["id"], "attention")) == 1


def test_a_flag_cleared_by_something_else_comes_back(started, project, fake_gh):
    """§4's self-heal (kn-2efe73e4): CLEARED is not ACKED, and a violation that still
    stands raises itself again on the next sweep — which is what the raise sitting BEFORE
    the dedupe `continue` buys. After it, only the first sweep to see the violation could
    ever flag it, and INV-ATTENTION-MISSING cannot put it back (no `completed` in
    `BLOCKED_STATUSES`)."""
    wo = _unmerged(project, started, fake_gh)
    _tick(started, project, sweep=True)

    store = ProjectStore(project)
    try:
        store.clear_attention(wo["id"])
    finally:
        store.close()
    _tick(started, project, sweep=True)

    assert _row(project, wo["id"])["needs_attention"] == 1


# -- 3. INV-ATTENTION-PHANTOM stops clearing what is owed ------------------------------


def test_phantom_attention_spares_an_owed_flag_and_still_clears_a_stale_one(
        started, project, fake_gh):
    """Obligation 3, both halves in one pass: the guard is `true_blockers` being
    non-empty, so the existing behaviour is untouched for every other terminal order."""
    owed = _unmerged(project, started, fake_gh)
    _tick(started, project, sweep=True)
    stale = ops.create_work_order("proj_a", "finished long ago")
    store = ProjectStore(project)
    try:
        store.set_status(stale["id"], "completed")
        store.flag_attention(stale["id"], "stale reason nobody can act on")
        violations = invariants.check_project(store, repair=True)
        phantom = [v.wo_id for v in violations if v.invariant == "INV-ATTENTION-PHANTOM"]
    finally:
        store.close()

    assert phantom == [stale["id"]]
    assert _row(project, owed["id"])["needs_attention"] == 1
    assert _row(project, stale["id"])["needs_attention"] == 0


# -- 4. --abandon silences it within a tick --------------------------------------------


def test_abandon_closes_the_report_and_the_next_tick_lowers_the_flag(started, project,
                                                                    fake_gh):
    """Obligation 4. Without §8's `close_violation_report` the user clears the alert and
    watches it sit there for up to an hour — the wo-5eedc84d shape."""
    wo = _unmerged(project, started, fake_gh)
    _tick(started, project, sweep=True)
    assert _row(project, wo["id"])["needs_attention"] == 1

    ops.finish(wo["id"], "dropping it", abandon="superseded by the rewrite")

    store = ProjectStore(project)
    try:
        assert store.owed_violations(wo["id"]) == []
    finally:
        store.close()
    _tick(started, project)          # a fast tick: no landing sweep

    assert _row(project, wo["id"])["needs_attention"] == 0


# -- 5 & 7. what does NOT change -------------------------------------------------------


def _switchable(**kwargs) -> tuple:
    def check(store):
        yield invariants.Violation(invariant="INV-FAKE", detail="still broken", **kwargs)
    return (check,)


def test_a_plain_unrepaired_violation_still_only_notifies(started, project,
                                                          monkeypatch):
    """Obligation 5. A violation with neither `repaired` nor `owed` is unchanged."""
    wo = ops.create_work_order("proj_a", "unfixable")
    monkeypatch.setattr(invariants, "INVARIANTS", _switchable(wo_id=wo["id"]))

    _tick(started, project)

    assert len(_invariant_notes(project)) == 1
    assert _row(project, wo["id"])["needs_attention"] == 0


def test_an_owed_violation_with_no_work_order_keeps_its_notification(started, project,
                                                                    monkeypatch):
    """Obligation 7. INV-LANDING-AUDIT-FRESH's shape: project-level, no order to flag,
    so the inbox is the only surface it has."""
    monkeypatch.setattr(invariants, "INVARIANTS",
                        _switchable(owed="decide something about the project"))

    _tick(started, project)

    assert len(_invariant_notes(project)) == 1
    store = ProjectStore(project)
    try:
        assert [w for w in store.list_work_orders() if w["needs_attention"]] == []
    finally:
        store.close()


def test_a_critical_owed_violation_is_one_line_and_not_two(started, project,
                                                           monkeypatch):
    """§7. The critical seam in `ops.os_status` builds its own project-level line from
    the report row, and a `critical` owed checker would therefore be reported twice —
    once as the work order's flag, once as an `invariant` item pointing at
    `jarvis doctor`. No owed checker is critical today; the skip keeps that safe."""
    wo = ops.create_work_order("proj_a", "owes a critical decision")
    monkeypatch.setattr(invariants, "INVARIANTS",
                        _switchable(wo_id=wo["id"], level="critical",
                                    owed="decide it: `jarvis wo done`"))

    _tick(started, project)

    items = [i for i in ops.os_status()["attention"] if i["wo_id"] == wo["id"]]
    assert [i["reason"] for i in items] == ["decide it: `jarvis wo done`"]
    assert [i for i in items if i["status"] == "invariant"] == []


# -- 6. the reason is left alone -------------------------------------------------------


def test_attention_reason_leaves_the_owed_blocker_alone(started, project, fake_gh):
    """Obligation 6. INV-ATTENTION-REASON re-derives `true_blockers[0]`, and the owed
    string IS that — so there is nothing to rewrite and nothing to report."""
    wo = _unmerged(project, started, fake_gh)
    _tick(started, project, sweep=True)
    before = _row(project, wo["id"])["attention_reason"]

    store = ProjectStore(project)
    try:
        reported = [v.wo_id for v in invariants.check_project(store, repair=True)
                    if v.invariant == "INV-ATTENTION-REASON"]
    finally:
        store.close()

    assert reported == []
    assert _row(project, wo["id"])["attention_reason"] == before
