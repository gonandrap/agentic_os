"""The same pipeline as test_pipeline.py, but driven by a project whose sessions come
from somebody else's wrapper instead of `claude --bg`.

The wrapper is deliberately hostile to assumptions: different flags, different state
words, a different roster shape, no worktree support, no resume. If dispatch, reconcile,
reply capture and message delivery all work through it, the launcher abstraction is real
rather than a rename of claude_cli.
"""

from __future__ import annotations

import json

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def wrapped(jarvis_home, fake_claude, catalog_file, project, fake_wrapper):
    """A started OS whose one project launches sessions through the fake wrapper."""
    fake_wrapper.install(project)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def only_job(fake_wrapper) -> dict:
    assert len(fake_wrapper.jobs) == 1, fake_wrapper.jobs
    return fake_wrapper.jobs[0]


def test_dispatch_goes_through_the_contract_not_claude(wrapped, fake_wrapper,
                                                       fake_claude, project):
    wo = ops.create_work_order("proj_a", "add feature X", description="details here")
    wrapped.tick()

    job = only_job(fake_wrapper)
    assert job["label"] == f"[WO {wo['id']}] add feature X"
    # The whole worker prompt reached the wrapper, contract included.
    spawn = [c for c in fake_wrapper.calls if c["argv"][0] == "run"][0]
    prompt = spawn["argv"][spawn["argv"].index("--") + 1]
    assert wo["id"] in prompt and "Operating contract" in prompt
    # …and `claude` was never asked to start anything.
    assert not [c for c in fake_claude.calls if "--bg" in c["argv"]]

    store = ProjectStore(project)
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "running"
    assert fresh["job_id"] == job["job"]


def test_jarvis_makes_the_worktree_the_launcher_cannot(wrapped, fake_wrapper, project):
    """capabilities.worktree is false, so isolation becomes Jarvis's job — the worker
    must still never be pointed at the user's working copy."""
    wo = ops.create_work_order("proj_a", "isolated work")
    wrapped.tick()

    worktree = project / ".claude" / "worktrees" / wo["id"]
    assert worktree.is_dir()
    assert only_job(fake_wrapper)["dir"] == str(worktree)


def test_reconcile_reads_the_wrappers_state_words(wrapped, fake_wrapper, project):
    wo = ops.create_work_order("proj_a", "watch me")
    wrapped.tick()
    job = only_job(fake_wrapper)

    store = ProjectStore(project)
    # The reconciler binds by the [WO …] name prefix, exactly as with the native roster.
    wrapped.tick_count = 0
    wrapped.tick()
    assert store.get_work_order(wo["id"])["session_id"] == job["conversation"]

    fake_wrapper.finish(job["job"], "final: the feature is in")
    wrapped.tick_count = 0
    wrapped.tick()

    fresh = store.get_work_order(wo["id"])
    # EXIT mapped to `done`, so the work order settles and the reply is captured.
    assert fresh["status"] == "needs_review"
    assert "worker idle" in (fresh["attention_reason"] or "")
    assert [m["content"] for m in store.agent_replies(wo["id"])] == \
        ["final: the feature is in"]


def test_feedback_falls_back_to_send_when_the_launcher_cannot_resume(
        wrapped, fake_wrapper, project):
    wo = ops.create_work_order("proj_a", "needs feedback")
    wrapped.tick()
    job = only_job(fake_wrapper)
    wrapped.tick_count = 0
    wrapped.tick()  # bind the session
    fake_wrapper.finish(job["job"])

    ops.send_message(wo["id"], "use the other library")
    wrapped.tick_count = 0
    wrapped.tick()
    wrapped.delivery_pool.shutdown(wait=True)

    store = ProjectStore(project)
    msgs = store.list_messages(wo["id"])
    delivered = [m for m in msgs if m["direction"] == "user_to_agent"]
    assert delivered and delivered[0]["status"] == "delivered"
    assert [c for c in fake_wrapper.calls if c["argv"][0] == "say"]
    events = [e["kind"] for e in store.list_events(wo["id"])]
    assert "message_delivered" in events


def test_a_launcher_that_can_do_neither_flags_the_user_instead_of_losing_the_message(
        wrapped, fake_wrapper, project):
    contract = json.loads((project / ".jarvis" / "launcher.json").read_text())
    del contract["send"]  # no resume (already) and now no send either
    (project / ".jarvis" / "launcher.json").write_text(json.dumps(contract))

    wo = ops.create_work_order("proj_a", "undeliverable")
    wrapped.tick()
    job = only_job(fake_wrapper)
    wrapped.tick_count = 0
    wrapped.tick()
    fake_wrapper.finish(job["job"])
    ops.send_message(wo["id"], "please also do Y")
    wrapped.tick_count = 0
    wrapped.tick()
    wrapped.delivery_pool.shutdown(wait=True)

    store = ProjectStore(project)
    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"] == 1
    assert "by hand" in (fresh["attention_reason"] or "")
    # The message is still queued — nothing was silently dropped.
    assert store.queued_messages(wo["id"])


def test_a_broken_contract_stops_the_project_without_taking_the_fleet_down(
        wrapped, fake_wrapper, project):
    (project / ".jarvis" / "launcher.json").write_text('{"schema_version": 1}')
    ops.create_work_order("proj_a", "cannot dispatch")
    wrapped.tick()  # must not raise

    store = ProjectStore(project)
    wos = store.list_work_orders()
    # Nothing was spawned and the order stays claimable rather than being marked done.
    assert fake_wrapper.jobs == []
    assert wos[0]["status"] in ("pending", "dispatching")


def test_a_failing_spawn_is_counted_against_the_contract(wrapped, fake_wrapper, project):
    contract = json.loads((project / ".jarvis" / "launcher.json").read_text())
    contract["spawn"]["command"][0] = "definitely-not-a-real-binary-xyz"
    (project / ".jarvis" / "launcher.json").write_text(json.dumps(contract))

    from jarvis.onboarding import launcher_health, launcher_state
    for _ in range(3):
        ops.create_work_order("proj_a", "doomed")
        wrapped.tick()

    assert launcher_state("proj_a")["spawn_failures"] >= 3
    problems = launcher_health(wrapped.catalog.project("proj_a"))["problems"]
    assert any("consecutive spawns failed" in p for p in problems)
