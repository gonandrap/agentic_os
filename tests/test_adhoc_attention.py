"""Ad-hoc sessions are observed, not governed — and an acknowledged flag stays down.

Reconstructed from a live fleet that accumulated 17 attention items, 15 of which were
the same two bugs:

1. The reconciler adopts every background session it finds in a project directory "for
   visibility" (a session the user started themselves in `claude agents`). It then held
   that session to the Jarvis worker contract: end a turn without calling
   `jarvis wo finish` and you are flagged as having stopped mid-task;
   vanish from `claude agents` and you are marked `failed` — "worker session
   disappeared". An adopted session was never dispatched by Jarvis, has no
   `JARVIS_WO_ID`, and never received the contract, so it *cannot* satisfy it. Every
   adopted session therefore became an attention item with certainty.

2. Clearing a flag by hand did nothing: `check_blocked_work_is_surfaced` re-derives
   attention from status on every reconcile tick, so an acknowledged work order was
   re-flagged seconds later. The user acked the whole inbox and the dashboard kept
   showing the same 17 items — because attention lives on the work order, not in the
   inbox, and nothing could put it down.
"""

from __future__ import annotations

import json

import pytest

from jarvis import db, ops
from jarvis.hooks import handle_hook
from jarvis.invariants import (IDLE_NO_FINISH_BLOCKER, check_project,
                               true_blockers)
from jarvis.project_store import ProjectStore

from test_pipeline import _add_session, _inject, started  # noqa: F401


def _injected_wo(store: ProjectStore) -> dict:
    return [w for w in store.list_work_orders() if w["origin"] == "injected"][0]


def _drop_session(fake_claude, sid: str) -> None:
    """The user deletes the session from `claude agents` (or it is simply pruned)."""
    remaining = [s for s in fake_claude.sessions if s.get("sessionId") != sid]
    fake_claude.sessions[:] = remaining
    (fake_claude.dir / "sessions.json").write_text(json.dumps(remaining))


def _age_out(store: ProjectStore, wo_id: str, seconds: float = 400.0) -> None:
    """Backdate the work order past the reconciler's grace period."""
    row = store.get_work_order(wo_id)
    store.conn.execute(
        "UPDATE work_orders SET updated_at=? WHERE id=?",
        (row["updated_at"] - seconds, wo_id),
    )


# -- 1. an injected session is not a worker under contract ----------------------------


def test_injected_session_going_idle_is_not_held_to_the_contract(
    started, fake_claude, project
):
    """The user's own session ends a turn. That is not an anomaly — it is a turn ending.

    This is the exact shape of six live attention items carrying
    IDLE_NO_FINISH_BLOCKER, one of which was the very session the user was talking to
    at the time.
    """
    daemon = started
    _inject(fake_claude, project, "working", sid="adhoc-1", name="my manual hack")

    store = ProjectStore(project)
    fake_claude.set_session_state("adhoc-1", "done")
    daemon.tick_count = 0
    daemon.tick()

    fresh = store.get_work_order(_injected_wo(store)["id"])
    assert fresh["status"] == "completed"
    assert not fresh["needs_attention"]
    assert not true_blockers(store, fresh)


def test_injected_session_removed_from_agents_view_is_not_a_failure(
    started, fake_claude, project
):
    """Deleting your own background session is a normal thing to do, not a fault.

    Seven live work orders sat in `failed` / "worker session disappeared" because the
    user cleaned up short-lived sessions in `claude agents` after the work was done.
    """
    daemon = started
    _inject(fake_claude, project, "working", sid="adhoc-1",
            name="serena-and-mode-split-1e")

    store = ProjectStore(project)
    wo_id = _injected_wo(store)["id"]
    _drop_session(fake_claude, "adhoc-1")
    _age_out(store, wo_id)
    daemon.tick_count = 0
    daemon.tick()

    fresh = store.get_work_order(wo_id)
    assert fresh["status"] == "completed"
    assert not fresh["needs_attention"]
    # and it must not page the user either
    titles = [r["title"] for r in
              store.conn.execute("SELECT title FROM notifications").fetchall()]
    assert not any("disappeared" in t for t in titles)


def test_a_dispatched_worker_whose_turn_dies_is_still_a_failure(
    started, fake_claude, project, settle_turns
):
    """Regression guard: the fix must not silence the case it exists for.

    Jarvis dispatched this one and promised to see it through, so a worker that stops
    without a result is a real failure the user has to know about.
    """
    daemon = started
    fake_claude.turns_fail("silent")
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)

    assert settle_turns(store)
    daemon.tick_count = 0
    daemon.tick()

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "failed"
    assert "turn failed" in fresh["attention_reason"]


def test_a_dispatched_worker_idle_without_finish_is_still_flagged(
    started, fake_claude, project, settle_turns
):
    """The other half of the guard: framework workers do owe a completion signal."""
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)

    assert settle_turns(store)
    daemon.tick_count = 0
    daemon.tick()

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review"
    assert "without `jarvis wo finish`" in fresh["attention_reason"]


@pytest.mark.parametrize("origin", ["injected", "adhoc"])
def test_true_blockers_does_not_invent_a_contract(project, origin):
    """Both the session the user handed over and the legacy one Jarvis took."""
    store = ProjectStore(project)
    wo = store.create_work_order("a session I started myself", origin=origin)
    store.set_status(wo["id"], "needs_review")
    assert true_blockers(store, store.get_work_order(wo["id"])) == []

    store.set_status(wo["id"], "failed")
    assert true_blockers(store, store.get_work_order(wo["id"])) == []


@pytest.mark.parametrize("origin", ["injected", "adhoc"])
def test_a_blocked_session_still_asks_for_you(project, origin):
    """Not everything about these is noise: stuck on a prompt is real."""
    store = ProjectStore(project)
    wo = store.create_work_order("my manual hack", origin=origin)
    store.set_status(wo["id"], "waiting_input")
    assert true_blockers(store, store.get_work_order(wo["id"])) == [
        "worker is waiting on your input"
    ]


def test_injected_assumptions_are_never_swallowed(project):
    """An injected session that used `jarvis wo assume` still gets its review."""
    store = ProjectStore(project)
    wo = store.create_work_order("injected session", origin="injected")
    store.add_assumption(wo["id"], "picked postgres over sqlite")
    store.set_status(wo["id"], "needs_review")
    assert true_blockers(store, store.get_work_order(wo["id"])) == [
        "1 assumption pending your review"
    ]


def test_the_wreckage_already_on_disk_is_repaired(project):
    """The 15 stale items in the live fleet must clear themselves on the next tick.

    Shipping a fix that only prevents *new* false positives would leave the user
    staring at the same dashboard.
    """
    store = ProjectStore(project)
    gone = store.create_work_order("serena-and-mode-split-1e", origin="injected")
    store.set_status(gone["id"], "failed")
    store.flag_attention(gone["id"], "worker session disappeared")
    idle = store.create_work_order("adversarial design review jarvis", origin="injected")
    store.set_status(idle["id"], "needs_review")
    store.flag_attention(idle["id"], IDLE_NO_FINISH_BLOCKER)

    violations = check_project(store, repair=True)

    assert {v.invariant for v in violations} == {"INV-ADHOC-NOT-GOVERNED"}
    for wo_id in (gone["id"], idle["id"]):
        fresh = store.get_work_order(wo_id)
        assert fresh["status"] == "completed"
        assert not fresh["needs_attention"]
    # ...and it is a one-time repair, not a per-tick alarm
    assert check_project(store, repair=True) == []


def test_repair_is_only_proposed_when_repair_is_off(project):
    store = ProjectStore(project)
    wo = store.create_work_order("serena-and-mode-split-de", origin="adhoc")
    store.set_status(wo["id"], "failed")
    store.flag_attention(wo["id"], "worker session disappeared")

    violations = check_project(store, repair=False)

    assert [v.invariant for v in violations] == ["INV-ADHOC-NOT-GOVERNED"]
    assert store.get_work_order(wo["id"])["status"] == "failed"  # untouched


# -- 3. the sessions Jarvis adopted before it stopped adopting -------------------------


def test_legacy_adopted_rows_are_let_go_on_upgrade(project):
    """Rows from the auto-adoption era have nothing tracking them any more.

    `Daemon.track_injected_sessions` follows `injected` rows only, so an `adhoc` row left
    in `waiting_input` would ask the user for something forever — about a session that
    may have ended weeks ago. "worker is waiting on your input" is a genuine blocker
    whoever started the session, so `true_blockers` will not quieten it; the row has to
    be closed.
    """
    store = ProjectStore(project)
    blocked = store.create_work_order("my manual hack", origin="adhoc")
    store.set_status(blocked["id"], "waiting_input")
    store.flag_attention(blocked["id"], "session blocked (permission or input needed)")
    live = store.create_work_order("serena-and-mode-split-1e", origin="adhoc")
    store.set_status(live["id"], "running")

    violations = check_project(store, repair=True)

    assert {v.invariant for v in violations} == {"INV-ADHOC-LEGACY-RETIRED"}
    for wo_id in (blocked["id"], live["id"]):
        fresh = store.get_work_order(wo_id)
        assert fresh["status"] == "completed"
        assert not fresh["needs_attention"]
        assert not true_blockers(store, fresh)
        # auditable: the record says the upgrade closed it, not the session
        why = [db.from_json(e["payload"], {}).get("why", "")
               for e in store.list_events(wo_id) if e["kind"] == "session_retired"]
        assert why and "issue 47" in why[0]
    # ...and a re-run is a no-op, not a per-tick alarm
    assert check_project(store, repair=True) == []


def test_legacy_retirement_leaves_the_record_and_its_assumptions_alone(project):
    """Closing the demand for the user must not close the work, or lose the history."""
    store = ProjectStore(project)
    pending = store.create_work_order("a session that asked something", origin="adhoc")
    store.add_assumption(pending["id"], "picked postgres over sqlite")
    store.set_status(pending["id"], "waiting_input")
    kept = store.create_work_order("my manual hack", origin="adhoc")
    store.set_status(kept["id"], "running")
    store.record_agent_reply(kept["id"], "here is what I found")

    check_project(store, repair=True)

    # the one with a question outstanding is left exactly where it is: the user owes it
    # an answer, and that is a real blocker rather than adoption noise
    assert store.get_work_order(pending["id"])["status"] == "waiting_input"
    assert len(store.pending_assumptions(pending["id"])) == 1
    # the retired one keeps everything it learned
    assert [m["content"] for m in store.list_messages(kept["id"])] == \
        ["here is what I found"]


def test_injected_rows_are_not_swept_up_by_the_upgrade(project):
    """The user handed these over deliberately and they are still tracked."""
    store = ProjectStore(project)
    wo = store.create_work_order("my manual hack", origin="injected")
    store.set_status(wo["id"], "running")

    assert check_project(store, repair=True) == []
    assert store.get_work_order(wo["id"])["status"] == "running"


def test_legacy_adopted_rows_are_not_tracked_from_the_roster(started, fake_claude,
                                                             project):
    """The retirement has to stick: if the tracker still followed `adhoc` rows it would
    reopen every one whose session is still alive, on the very next tick."""
    daemon = started
    _inject(fake_claude, project, "working", sid="live-1")  # something to track
    store = ProjectStore(project)
    legacy = store.create_work_order("my manual hack", origin="adhoc")
    store.update_work_order(legacy["id"], session_id="legacy-1")
    store.set_status(legacy["id"], "completed")
    _add_session(fake_claude, project, "working", sid="legacy-1", name="my manual hack")

    daemon.tick_count = 0
    daemon.tick()

    assert store.get_work_order(legacy["id"])["status"] == "completed"


def test_session_end_hook_bills_nobody(project):
    """SessionEnd is inert under headless turns, and that is load-bearing twice over.

    It fires at the end of every turn now, so the settlement it used to do would file a
    healthy dispatched work order for review after turn one — and it had the ad-hoc bug
    too, billing a session the user started themselves against a contract it never
    received. Retirement is `Daemon.track_injected_sessions`'s job, from the roster.
    """
    store = ProjectStore(project)
    injected = store.create_work_order("my manual hack", origin="injected")
    store.update_work_order(injected["id"], session_id="adhoc-1")
    store.set_status(injected["id"], "running")
    dispatched = store.create_work_order("a real work order", origin="jarvis")
    store.update_work_order(dispatched["id"], session_id="wo-sess-1")
    store.set_status(dispatched["id"], "running")

    for wo, sid in ((injected, "adhoc-1"), (dispatched, "wo-sess-1")):
        handle_hook(
            {"hook_event_name": "SessionEnd", "session_id": sid, "cwd": str(project)},
            {"JARVIS_PROJECT_PATH": str(project)},
        )
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "running", wo["origin"]
        assert not fresh["needs_attention"], wo["origin"]


# -- 2. acknowledging a flag actually puts it down ------------------------------------


def test_ack_survives_the_reconcilers_next_pass(project):
    """The bug the user hit: ack, then watch the daemon put it straight back.

    `check_blocked_work_is_surfaced` re-derives attention from status, so before this
    fix the flag returned on the next tick — for as long as the work order existed.
    """
    store = ProjectStore(project)
    wo = store.create_work_order("task")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], IDLE_NO_FINISH_BLOCKER)

    fresh = store.get_work_order(wo["id"])
    store.ack_attention(wo["id"], true_blockers(store, fresh))
    assert not store.get_work_order(wo["id"])["needs_attention"]

    check_project(store, repair=True)  # the daemon's next tick

    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_ack_does_not_deafen_the_work_order_to_something_new(project):
    """An ack covers what you saw, not everything that will ever happen next."""
    store = ProjectStore(project)
    wo = store.create_work_order("task")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], IDLE_NO_FINISH_BLOCKER)
    store.ack_attention(wo["id"], true_blockers(store, store.get_work_order(wo["id"])))

    store.add_assumption(wo["id"], "swapped the auth library")
    check_project(store, repair=True)

    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"]
    assert fresh["attention_reason"] == "1 assumption pending your review"


def test_ack_refuses_to_bury_a_pending_assumption(started, project):
    """Assumptions need a decision, not a dismissal."""
    store = ProjectStore(project)
    wo = store.create_work_order("task")
    store.add_assumption(wo["id"], "picked postgres")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], "1 assumption pending your review")

    with pytest.raises(ops.OpsError, match="jarvis wo review"):
        ops.ack_attention(wo["id"])

    assert store.get_work_order(wo["id"])["needs_attention"]


def test_ack_all_clears_the_whole_attention_list(started, project):
    store = ProjectStore(project)
    ids = []
    for i in range(3):
        wo = store.create_work_order(f"task {i}")
        store.set_status(wo["id"], "needs_review")
        store.flag_attention(wo["id"], IDLE_NO_FINISH_BLOCKER)
        ids.append(wo["id"])
    keep = store.create_work_order("needs a decision")
    store.add_assumption(keep["id"], "picked postgres")
    store.set_status(keep["id"], "needs_review")
    store.flag_attention(keep["id"], "1 assumption pending your review")

    result = ops.ack_attention(all_projects=True)

    assert set(result["acknowledged"]) == set(ids)
    assert result["skipped"] == [
        {"wo_id": keep["id"], "reason": "1 assumption pending your review"}
    ]
    assert [w["id"] for w in store.list_work_orders() if w["needs_attention"]] == [keep["id"]]


def test_ack_is_recorded_on_the_timeline(started, project):
    store = ProjectStore(project)
    wo = store.create_work_order("task")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], IDLE_NO_FINISH_BLOCKER)

    ops.ack_attention(wo["id"])

    kinds = [e["kind"] for e in store.list_events(wo["id"], limit=50)]
    assert "acknowledged" in kinds


def test_status_stops_listing_acknowledged_work(started, project):
    store = ProjectStore(project)
    wo = store.create_work_order("task")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], IDLE_NO_FINISH_BLOCKER)
    assert any(a["wo_id"] == wo["id"] for a in ops.os_status()["attention"])

    ops.ack_attention(wo["id"])

    assert not any(a["wo_id"] == wo["id"] for a in ops.os_status()["attention"])
