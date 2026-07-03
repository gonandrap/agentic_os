"""End-to-end pipeline tests against the fake `claude` CLI:
start → create work order → daemon tick dispatches → hooks update state →
messages deliver → finish/review → notifications route → adhoc adoption.
"""

from __future__ import annotations

import json

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.hooks import handle_hook
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    """OS started (bootstrap + registration), daemon object ready to tick manually."""
    result = ops.start_os(str(catalog_file), foreground=True)  # no subprocess
    assert result["daemon"]["status"] == "foreground"
    catalog = load_catalog(catalog_file)
    daemon = Daemon(catalog)
    return daemon


def bind_session(daemon, project, wo_id: str) -> str:
    """Mirror reality: the SessionStart hook binds the supervisor-assigned session id.
    Returns the bound session id."""
    store = ProjectStore(project)
    try:
        import subprocess  # find the fake session by name via the daemon's own channel
        from jarvis import claude_cli
        sess = [s for s in claude_cli.list_background_sessions()
                if s.name.startswith(f"[WO {wo_id}]")]
        assert sess, f"no fake session named [WO {wo_id}]"
        sid = sess[0].session_id
        handle_hook(
            {"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(project)},
            {"JARVIS_WO_ID": wo_id, "JARVIS_PROJECT_PATH": str(project)},
        )
        return sid
    finally:
        store.close()


def test_start_bootstraps_and_registers(started, project, jarvis_home):
    assert (project / "OPERATION.md").exists()
    assert (project / ".jarvis" / "jarvis.db").parent.is_dir()
    central = CentralStore()
    assert [p["name"] for p in central.list_projects()] == ["proj_a"]


def test_dispatch_flow(started, fake_claude, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "add feature X", description="details here",
                               origin="jarvis")
    daemon.tick()

    store = ProjectStore(project)
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "running"
    assert fresh["session_id"] is None  # bound later by hook/reconciler
    assert fresh["worktree"] == wo["id"]

    # the SessionStart hook binds the supervisor-assigned session id
    sid = bind_session(daemon, project, wo["id"])
    assert store.get_work_order(wo["id"])["session_id"] == sid

    # the fake claude recorded a --bg spawn with our conventions
    bg = [c for c in fake_claude.calls if "--bg" in c["argv"]]
    assert len(bg) == 1
    argv = bg[0]["argv"]
    assert argv[argv.index("--name") + 1].startswith(f"[WO {wo['id']}]")
    assert argv[argv.index("--worktree") + 1] == wo["id"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    # full settings (hooks + permissions + env) travel with the spawn as a file,
    # because the worktree has no .claude/settings.json (it's untracked)
    settings_path = argv[argv.index("--settings") + 1]
    settings = json.loads(open(settings_path).read())
    assert settings["env"]["JARVIS_WO_ID"] == wo["id"]
    assert settings["env"]["JARVIS_PROJECT_PATH"] == str(project)
    assert "PATH" in settings["env"]
    assert "Stop" in settings["hooks"]
    assert "Bash(jarvis *)" in settings["permissions"]["allow"]
    prompt = argv[-1]
    assert "add feature X" in prompt and "OPERATION.md" in prompt
    # appears in the (fake) agents view
    assert fake_claude.sessions[0]["name"].startswith("[WO ")


def test_concurrency_limit(started, fake_claude):
    daemon = started
    for i in range(4):
        ops.create_work_order("proj_a", f"task {i}")
    daemon.tick()
    bg = [c for c in fake_claude.calls if "--bg" in c["argv"]]
    assert len(bg) == 2  # default max_concurrent = 2


def test_knowledge_injected_into_prompt(started, fake_claude):
    daemon = started
    central = CentralStore()
    central.add_knowledge("always run make lint", project="proj_a", topic="ci")
    central.add_knowledge("global: prefer uv over pip", project="")
    ops.create_work_order("proj_a", "task")
    daemon.tick()
    prompt = [c for c in fake_claude.calls if "--bg" in c["argv"]][0]["argv"][-1]
    assert "always run make lint" in prompt
    assert "prefer uv over pip" in prompt


def test_hook_events_update_state(started, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = bind_session(daemon, project, wo["id"])

    env = {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)}
    handle_hook({"hook_event_name": "Notification", "session_id": sid,
                 "cwd": str(project), "message": "needs permission for Bash"}, env)
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "waiting_input"
    assert fresh["needs_attention"] == 1
    # a notification was queued for the user
    assert any("needs input" in n["title"] for n in store.unrouted_notifications())

    handle_hook({"hook_event_name": "Stop", "session_id": sid, "cwd": str(project)}, env)
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert "turn_ended" in kinds


def test_hook_noop_for_non_worker_sessions(started, project):
    store = ProjectStore(project)
    before = store.list_work_orders()
    result = handle_hook({"hook_event_name": "Stop", "session_id": "random-interactive",
                          "cwd": str(project)}, {})
    assert result is None
    assert store.list_work_orders() == before


def test_message_delivery_when_idle(started, fake_claude, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = bind_session(daemon, project, wo["id"])

    ops.send_message(wo["id"], "please also update the docs", source="ui")
    daemon.tick()  # worker mid-turn (no turn_ended yet) → not delivered
    assert [c for c in fake_claude.calls if "--resume" in c["argv"]] == []

    env = {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)}
    handle_hook({"hook_event_name": "Stop", "session_id": sid, "cwd": str(project)}, env)
    daemon.tick()
    daemon.delivery_pool.shutdown(wait=True)  # let the delivery thread finish

    resumes = [c for c in fake_claude.calls if "--resume" in c["argv"]]
    assert len(resumes) == 1
    argv = resumes[0]["argv"]
    assert argv[argv.index("--resume") + 1] == sid
    assert "update the docs" in argv[argv.index("-p") + 1]

    msgs = store.list_messages(wo["id"])
    assert msgs[0]["status"] == "delivered"
    # worker's reply recorded as agent_to_user
    assert any(m["direction"] == "agent_to_user" for m in msgs)


def test_finish_and_assumption_review(started, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()

    ops.assume(wo["id"], "assumed dark mode uses CSS vars")
    md = (project / "ASSUMPTIONS.md").read_text()
    assert "assumed dark mode uses CSS vars" in md

    result = ops.finish(wo["id"], "shipped in PR #1")
    assert result["status"] == "needs_review"  # pending assumption blocks completion

    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["needs_attention"] == 1
    st = ops.os_status()
    assert any(a["wo_id"] == wo["id"] for a in st["attention"])

    ops.review_work_order(wo["id"], accept=True)
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "completed"
    assert fresh["needs_attention"] == 0


def test_finish_without_assumptions_completes(started, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    assert ops.finish(wo["id"], "done")["status"] == "completed"


def test_notification_routing(started, project, jarvis_home):
    daemon = started
    store = ProjectStore(project)
    store.add_notification("prod is down", "http 500s", level="critical")
    daemon.tick()

    central = CentralStore()
    items = central.unacked_inbox()
    assert len(items) == 1 and items[0]["level"] == "critical"
    assert items[0]["status"] == "notified"  # routed through sinks
    log = (jarvis_home / "logs" / "notifications.log").read_text()
    assert "prod is down" in log

    st = ops.os_status()
    assert st["inbox"]["critical"] == 1


def test_reconciler_binds_session_by_name(started, fake_claude, project):
    """Without any hook, the reconciler binds the session via the [WO id] name."""
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["session_id"] is None
    daemon.tick_count = 0
    daemon.tick()
    bound = store.get_work_order(wo["id"])["session_id"]
    assert bound and bound == fake_claude.sessions[0]["sessionId"]


def test_reconciler_settles_done_worker(started, fake_claude, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = bind_session(daemon, project, wo["id"])

    # worker finished properly, then its session went idle
    ops.finish(wo["id"], "all good")
    fake_claude.set_session_state(sid, "done")
    daemon.tick_count = 0  # force reconcile on next tick
    daemon.tick()
    assert store.get_work_order(wo["id"])["status"] == "completed"


def test_reconciler_flags_unfinished_idle_worker(started, fake_claude, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = bind_session(daemon, project, wo["id"])

    fake_claude.set_session_state(sid, "done")  # idle but never called finish
    daemon.tick_count = 0
    daemon.tick()
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review"
    assert "without `jarvis wo finish`" in fresh["attention_reason"]


def test_reconciler_adopts_adhoc_sessions(started, fake_claude, project):
    daemon = started
    # a bg session someone started by hand in the project dir
    sessions = fake_claude.sessions
    sessions.append({"id": "abcd1234", "sessionId": "adhoc-session-1",
                     "cwd": str(project), "kind": "background",
                     "name": "my manual hack", "state": "running", "startedAt": 0})
    (fake_claude.dir / "sessions.json").write_text(json.dumps(sessions))

    daemon.tick_count = 0
    daemon.tick()
    store = ProjectStore(project)
    adhoc = [w for w in store.list_work_orders() if w["origin"] == "adhoc"]
    assert len(adhoc) == 1
    assert adhoc[0]["title"] == "my manual hack"
    assert adhoc[0]["status"] == "running"
    # stable across ticks (no duplicates)
    daemon.tick_count = 0
    daemon.tick()
    assert len([w for w in store.list_work_orders() if w["origin"] == "adhoc"]) == 1


def test_backlog_promotion_with_dependencies(started, project):
    daemon = started
    central = CentralStore()
    a = central.add_backlog("proj_a", "build auth")
    b = central.add_backlog("proj_a", "build profile page", depends_on=[a["id"]])

    with pytest.raises(ops.OpsError, match="unfinished dependencies"):
        ops.promote_backlog(b["id"])

    result = ops.promote_backlog(b["id"], force=True)
    assert result["forced_over_blockers"] == [a["id"]]

    # completing the promoted WO marks the backlog item done
    daemon.tick()
    ops.finish(result["wo_id"], "profile page shipped")
    assert central.get_backlog(b["id"])["status"] == "done"


def test_wo_not_found(started):
    with pytest.raises(ops.OpsError, match="not found"):
        ops.find_work_order("wo-doesnotexist")


def test_pretooluse_auto_allows_jarvis_chains():
    from jarvis.hooks import is_jarvis_command_chain, preflight_decision

    assert is_jarvis_command_chain('jarvis wo finish wo-1 --summary "done"')
    assert is_jarvis_command_chain('cd /some/proj && jarvis wo assume wo-1 "x"')
    assert not is_jarvis_command_chain("jarvis status && rm -rf /")
    assert not is_jarvis_command_chain("jarvis status; whoami")
    assert not is_jarvis_command_chain("echo jarvis")
    assert not is_jarvis_command_chain("jarvis notify `whoami`")
    assert not is_jarvis_command_chain("cd /p && git push")

    d = preflight_decision({"tool_name": "Bash",
                            "tool_input": {"command": "cd /p && jarvis status"}})
    assert d["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert preflight_decision({"tool_name": "Bash",
                               "tool_input": {"command": "git push"}}) is None
    assert preflight_decision({"tool_name": "Edit", "tool_input": {}}) is None
