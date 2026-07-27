"""Cancelling (or deleting) a work order must not leave its worker behind.

A cancelled work order nobody reads any more, but its background session keeps
running: burning tokens, editing the worktree and cluttering the agents view.
Cancel therefore stops the session it dispatched — best effort, and never at the
cost of the status change itself.
"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

from test_pipeline import bind_session  # noqa: F401  (shared helper)


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def running_wo(daemon, project, fake_claude):
    """A dispatched work order with a live background session bound to it."""
    wo = ops.create_work_order("proj_a", "task to cancel")
    daemon.tick()
    sid = bind_session(daemon, project, wo["id"])
    return wo, sid


def test_cancel_stops_the_worker_session(started, fake_claude, project):
    daemon = started
    wo, sid = running_wo(daemon, project, fake_claude)
    assert [s for s in fake_claude.sessions if s["sessionId"] == sid]

    out = ops.cancel(wo["id"])

    assert out["status"] == "cancelled"
    assert out["session_stopped"] is True
    # gone from the agents view — no orphan left behind
    assert [s for s in fake_claude.sessions if s["sessionId"] == sid] == []
    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["status"] == "cancelled"
    assert "session_stopped" in [e["kind"] for e in store.list_events(wo["id"])]


def test_cancel_before_dispatch_touches_no_session(started, fake_claude, project):
    wo = ops.create_work_order("proj_a", "never dispatched")

    out = ops.cancel(wo["id"])

    assert out["status"] == "cancelled"
    assert out["session_stopped"] is False
    assert [c for c in fake_claude.calls if c["argv"][:1] == ["stop"]] == []


def test_cancel_still_cancels_when_the_session_is_already_gone(
    started, fake_claude, project
):
    daemon = started
    wo, sid = running_wo(daemon, project, fake_claude)
    # the session died on its own; the roster no longer knows it
    (fake_claude.dir / "sessions.json").write_text("[]")

    out = ops.cancel(wo["id"])

    assert out["status"] == "cancelled"
    assert out["session_stopped"] is False
    assert ProjectStore(project).get_work_order(wo["id"])["status"] == "cancelled"


def test_delete_also_stops_the_worker_session(started, fake_claude, project):
    daemon = started
    wo, sid = running_wo(daemon, project, fake_claude)

    out = ops.delete_work_order(wo["id"])

    assert out["session_stopped"] is True
    assert [s for s in fake_claude.sessions if s["sessionId"] == sid] == []
