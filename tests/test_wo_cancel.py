"""Cancelling (or deleting) a work order must not leave its worker running.

A cancelled work order nobody reads any more, but its worker turn keeps going: burning
tokens and editing the worktree. Cancel therefore kills the turn it started — best
effort, and never at the cost of the status change itself.
"""

from __future__ import annotations

import pytest

from jarvis import claude_cli, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def running_wo(daemon, project, fake_claude):
    """A dispatched work order with a live turn in flight."""
    fake_claude.hold_turns()  # the turn blocks, so it is genuinely still running
    wo = ops.create_work_order("proj_a", "task to cancel")
    daemon.tick()
    store = ProjectStore(project)
    turn = store.latest_turn(wo["id"])
    assert turn["state"] == "running" and turn["pid"]
    return wo, turn


def test_cancel_kills_the_worker_turn(started, fake_claude, project):
    daemon = started
    wo, turn = running_wo(daemon, project, fake_claude)
    assert claude_cli.process_alive(turn["pid"])

    out = ops.cancel(wo["id"])

    assert out["status"] == "cancelled"
    assert out["session_stopped"] is True
    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["status"] == "cancelled"
    assert store.latest_turn(wo["id"])["state"] == "failed"
    assert "turn_cancelled" in [e["kind"] for e in store.list_events(wo["id"])]


def test_cancel_before_dispatch_touches_no_session(started, fake_claude, project):
    wo = ops.create_work_order("proj_a", "never dispatched")

    out = ops.cancel(wo["id"])

    assert out["status"] == "cancelled"
    assert out["session_stopped"] is False
    assert [c for c in fake_claude.calls if c["argv"][:1] == ["stop"]] == []


def test_cancel_still_cancels_when_the_turn_is_already_gone(
    started, fake_claude, project, settle_turns
):
    daemon = started
    wo = ops.create_work_order("proj_a", "task to cancel")
    daemon.tick()
    store = ProjectStore(project)
    assert settle_turns(store)  # the turn finished on its own

    out = ops.cancel(wo["id"])

    assert out["status"] == "cancelled"
    assert out["session_stopped"] is False
    assert store.get_work_order(wo["id"])["status"] == "cancelled"


def test_delete_also_kills_the_worker_turn(started, fake_claude, project):
    daemon = started
    wo, turn = running_wo(daemon, project, fake_claude)

    out = ops.delete_work_order(wo["id"])

    assert out["session_stopped"] is True
    assert not claude_cli.process_alive(turn["pid"])


def test_cancel_releases_a_legacy_background_session(started, fake_claude, project):
    """A work order dispatched before headless turns has no turn to kill, but it does
    have a background agent — and cancelling must still take that down."""
    daemon = started
    wo = ops.create_work_order("proj_a", "legacy work order")
    claude_cli.spawn_background(prompt="legacy", cwd=project,
                                name=f"[WO {wo['id']}] legacy work order")
    sess = fake_claude.sessions[-1]
    store = ProjectStore(project)
    store.update_work_order(wo["id"], session_id=sess["sessionId"], job_id=sess["id"])
    store.set_status(wo["id"], "running")

    out = ops.cancel(wo["id"])

    assert out["session_stopped"] is True
    assert [s for s in fake_claude.sessions if s["sessionId"] == sess["sessionId"]] == []
