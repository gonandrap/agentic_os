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
from jarvis.invariants import true_blockers
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
    assert argv[argv.index("--permission-mode") + 1] == "auto"
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
    for i in range(7):
        ops.create_work_order("proj_a", f"task {i}")
    daemon.tick()
    bg = [c for c in fake_claude.calls if "--bg" in c["argv"]]
    assert len(bg) == 5  # default max_concurrent = 5; the other 2 stay queued


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


def test_user_reply_clears_attention(started, project):
    """Regression (wo-f883d243): replying to a work order that is waiting on the
    user must immediately drop it from the attention list. The user has responded,
    so the OS must stop showing "needs input" even before the daemon delivers the
    message. Previously the flag was only cleared on delivery, so a reply left the
    WO stuck under "NEEDS YOU" (and acking the inbox notification never helped —
    that's a separate signal)."""
    daemon = started
    wo = ops.create_work_order("proj_a", "give me a summary of the current status")
    daemon.tick()
    store = ProjectStore(project)
    sid = bind_session(daemon, project, wo["id"])

    # worker blocks on a permission prompt → flagged for the user
    env = {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)}
    handle_hook({"hook_event_name": "Notification", "session_id": sid,
                 "cwd": str(project), "message": "Claude needs your permission"}, env)
    assert store.get_work_order(wo["id"])["needs_attention"] == 1
    assert any(a["wo_id"] == wo["id"] for a in ops.os_status()["attention"])

    # the user replies — no daemon delivery has happened yet
    ops.send_message(wo["id"], "permission for what?", source="ui")

    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"] == 0
    assert fresh["attention_reason"] is None
    assert not any(a["wo_id"] == wo["id"] for a in ops.os_status()["attention"])
    # the reply is still queued for the worker; delivery stays the daemon's job
    assert store.list_messages(wo["id"])[0]["status"] == "queued"


def test_status_warns_on_prompting_permission_mode(jarvis_home, fake_claude, project, tmp_path):
    """Misconfig safeguard: a fleet running a prompting mode (acceptEdits/default/
    plan/manual) will stall its --bg workers on the first permission prompt, so
    `jarvis status` must flag it. `auto`/`bypassPermissions`/`dontAsk` never prompt."""
    data = {
        "os": {"defaults": {"model": "sonnet", "permission_mode": "acceptEdits"},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project), "description": "t"}],
    }
    cf = tmp_path / "acceptedits-catalog.json"
    cf.write_text(json.dumps(data))
    ops.start_os(str(cf), foreground=True)

    st = ops.os_status()
    warn = [a for a in st["attention"]
            if a["status"] == "config" and "permission mode" in a["title"].lower()]
    assert warn, st["attention"]
    assert warn[0]["project"] == "proj_a"
    assert "acceptEdits" in warn[0]["reason"]
    assert "auto" in warn[0]["reason"]  # tells the user what to switch to
    assert st["healthy"] is False


def test_status_quiet_under_autonomous_mode(started):
    """The default fleet mode is `auto`; it must NOT produce a permission-mode warning."""
    st = ops.os_status()
    assert not [a for a in st["attention"]
                if a["status"] == "config" and "permission mode" in a["title"].lower()]


def test_resume_in_auto_flips_mode_and_unblocks(started, project):
    """Resume-in-auto: recover a worker stalled on a permission prompt by flipping it
    to `auto` and nudging it to continue — the daemon then resume-forks in auto mode."""
    daemon = started
    wo = ops.create_work_order("proj_a", "task", permission_mode="acceptEdits")
    daemon.tick()
    store = ProjectStore(project)
    bind_session(daemon, project, wo["id"])
    env = {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)}
    handle_hook({"hook_event_name": "Notification", "session_id":
                 store.get_work_order(wo["id"])["session_id"], "cwd": str(project),
                 "message": "Claude needs your permission"}, env)
    assert store.get_work_order(wo["id"])["needs_attention"] == 1

    ops.resume_in_auto(wo["id"])

    fresh = store.get_work_order(wo["id"])
    assert fresh["permission_mode"] == "auto"
    assert fresh["needs_attention"] == 0          # nudge clears the attention flag
    assert store.queued_messages(wo["id"])        # a continue-nudge is queued for delivery
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert "permission_mode_changed" in kinds


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
    daemon.tick_count = 0
    daemon.tick()  # worker session still running → not deliverable
    daemon.delivery_pool.shutdown(wait=True)
    assert [c for c in fake_claude.calls if "--resume" in c["argv"]] == []
    from concurrent.futures import ThreadPoolExecutor
    daemon.delivery_pool = ThreadPoolExecutor(max_workers=2)

    fake_claude.set_session_state(sid, "done")  # worker went idle
    daemon.tick_count = 0
    daemon.tick()
    daemon.delivery_pool.shutdown(wait=True)  # let the delivery thread finish

    # delivered as a NEW bg agent resuming the worker's session (stays in agents view)
    resumes = [c for c in fake_claude.calls
               if "--bg" in c["argv"] and "--resume" in c["argv"]]
    assert len(resumes) == 1
    argv = resumes[0]["argv"]
    assert argv[argv.index("--resume") + 1] == sid
    assert argv[argv.index("--name") + 1].startswith(f"[WO {wo['id']}]")
    assert "update the docs" in argv[-1]

    msgs = store.list_messages(wo["id"])
    assert msgs[0]["status"] == "delivered"

    # The fork's reply is recovered by the next reconcile pass, not inline: delivery
    # runs on a small pool and a worker turn can take many minutes.
    daemon.tick_count = 0
    daemon.tick()

    replies = [m["content"] for m in store.list_messages(wo["id"])
               if m["direction"] == "agent_to_user"]
    assert any("ack:" in r for r in replies), replies


def test_worker_final_message_is_recorded_alongside_the_summary(
    started, fake_claude, project
):
    """The work order must stand alone — the user and Neo decide from it and never open
    the worker session. `--summary` is a headline; the worker's full closing message is
    kept next to it. Regression: `wo finish` flips the order to `completed` before its
    session goes idle, so a status-filtered sweep would drop exactly that reply."""
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = bind_session(daemon, project, wo["id"])

    ops.finish(wo["id"], "one-line headline")
    assert store.get_work_order(wo["id"])["status"] == "completed"

    fake_claude.set_session_state(sid, "done")
    daemon.tick_count = 0
    daemon.tick()

    fresh = store.get_work_order(wo["id"])
    assert fresh["result_summary"] == "one-line headline"   # headline survives
    replies = [m["content"] for m in store.list_messages(wo["id"])
               if m["direction"] == "agent_to_user"]
    assert any(r.startswith("final:") for r in replies), replies
    assert "worker_reply" in [e["kind"] for e in store.list_events(wo["id"])]

    # Capture is idempotent: further passes must not duplicate the reply.
    daemon.tick_count = 0
    daemon.tick()
    again = [m["content"] for m in store.list_messages(wo["id"])
             if m["direction"] == "agent_to_user"]
    assert again == replies


def test_missing_job_result_does_not_stall_the_work_order(started, fake_claude, project):
    """If the supervisor never publishes a result, give up after a bounded number of
    passes rather than holding the work order open forever."""
    import shutil

    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = bind_session(daemon, project, wo["id"])

    fake_claude.set_session_state(sid, "done")
    job_id = store.get_work_order(wo["id"])["job_id"]
    shutil.rmtree(fake_claude.dir / "jobs" / job_id)  # result file never appears

    for _ in range(4):
        daemon.tick_count = 0
        daemon.tick()

    fresh = store.get_work_order(wo["id"])
    assert fresh["reply_job_id"] == job_id          # marked resolved, stops retrying
    assert fresh["status"] == "needs_review"        # settled, not stuck in `running`
    assert "worker_reply_lost" in [e["kind"] for e in store.list_events(wo["id"])]


def test_delivery_retires_the_previous_session(started, fake_claude, project):
    """Each delivered turn forks a fresh bg agent; the one it forked from is stopped
    afterwards, so a multi-turn conversation keeps exactly one live agent per WO."""
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = bind_session(daemon, project, wo["id"])

    def turn(text: str, current_sid: str) -> str:
        fake_claude.set_session_state(current_sid, "done")  # worker idle → deliverable
        ops.send_message(wo["id"], text, source="ui")
        daemon.tick_count = 0
        daemon.tick()
        daemon.delivery_pool.shutdown(wait=True)
        from concurrent.futures import ThreadPoolExecutor
        daemon.delivery_pool = ThreadPoolExecutor(max_workers=2)
        return bind_session(daemon, project, wo["id"])

    sid = turn("first follow-up", sid)
    sid = turn("second follow-up", sid)

    mine = [s for s in fake_claude.sessions if s["name"].startswith(f"[WO {wo['id']}]")]
    assert len(mine) == 1, f"stale sessions accumulated: {[s['sessionId'] for s in mine]}"
    assert mine[0]["sessionId"] == sid
    # and the WO is bound to the survivor, not to a session that was stopped
    assert store.get_work_order(wo["id"])["session_id"] == sid


def test_reopened_session_does_not_walk_the_binding_backwards(started, fake_claude, project):
    """Re-opening a spent session must not re-point the work order at it.

    From wo-9478c1be, live: after turn one the user opened the finished agent in the
    agents view to read it. Re-opening respawns that session under its ORIGINAL id, so
    SessionStart fired again — and the hook rebound the work order to a session that
    had already been stopped. The next delivered turn then forked from that stale
    conversation (losing turn two entirely) and "retired" the already-stopped agent,
    so the genuinely live one was orphaned into the agents view for good.
    """
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    first_sid = bind_session(daemon, project, wo["id"])

    fake_claude.set_session_state(first_sid, "done")
    ops.send_message(wo["id"], "first follow-up", source="ui")
    daemon.tick_count = 0
    daemon.tick()
    daemon.delivery_pool.shutdown(wait=True)
    from concurrent.futures import ThreadPoolExecutor
    daemon.delivery_pool = ThreadPoolExecutor(max_workers=2)
    fork_sid = bind_session(daemon, project, wo["id"])
    assert fork_sid != first_sid

    # the user re-opens turn one's agent: same session id, SessionStart fires again
    handle_hook({"hook_event_name": "SessionStart", "session_id": first_sid,
                 "cwd": str(project)},
                {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)})
    assert store.get_work_order(wo["id"])["session_id"] == fork_sid
    assert "session_rebind_ignored" in [e["kind"] for e in store.list_events(wo["id"])]

    # ...and the next turn still forks from the live session, not the re-opened one
    fake_claude.set_session_state(fork_sid, "done")
    ops.send_message(wo["id"], "second follow-up", source="ui")
    daemon.tick_count = 0
    daemon.tick()
    daemon.delivery_pool.shutdown(wait=True)

    resumes = [c for c in fake_claude.calls
               if "--bg" in c["argv"] and "--resume" in c["argv"]]
    assert [c["argv"][c["argv"].index("--resume") + 1] for c in resumes] == \
        [first_sid, fork_sid]
    mine = [s for s in fake_claude.sessions if s["name"].startswith(f"[WO {wo['id']}]")]
    assert len(mine) == 1, f"orphaned sessions: {[s['sessionId'] for s in mine]}"


def test_hooks_from_a_superseded_session_do_not_steer_the_work_order(started, fake_claude,
                                                                    project):
    """A spent session still fires hooks — its SessionEnd must not end the work order.

    Stopping the session Jarvis forked from raises SessionEnd for it moments after the
    fork started working. Acted on, that files a live work order for review with
    "session ended without `jarvis wo finish`".
    """
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    first_sid = bind_session(daemon, project, wo["id"])

    fake_claude.set_session_state(first_sid, "done")
    ops.send_message(wo["id"], "follow-up", source="ui")
    daemon.tick_count = 0
    daemon.tick()
    daemon.delivery_pool.shutdown(wait=True)
    bind_session(daemon, project, wo["id"])  # the fork takes the binding

    for event in ("SessionEnd", "Notification", "Stop"):
        handle_hook({"hook_event_name": event, "session_id": first_sid,
                     "cwd": str(project), "message": "Claude is waiting for your input"},
                    {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)})

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "running"
    assert fresh["needs_attention"] == 0


def test_follow_up_turn_is_briefed_like_the_first(started, fake_claude, project):
    """A resumed session re-derives its system prompt at launch — it does not inherit
    the first turn's from the transcript. So the fork must carry the same briefing:
    without it the project's standing instructions to the worker (and the OS skills
    directory) silently vanish from turn two onwards."""
    daemon = started
    wo = ops.create_work_order("proj_a", "task", model="opus",
                               append_system_prompt="never touch production")
    daemon.tick()
    sid = bind_session(daemon, project, wo["id"])

    fake_claude.set_session_state(sid, "done")
    ops.send_message(wo["id"], "follow-up", source="ui")
    daemon.tick_count = 0
    daemon.tick()
    daemon.delivery_pool.shutdown(wait=True)

    resumes = [c for c in fake_claude.calls
               if "--bg" in c["argv"] and "--resume" in c["argv"]]
    argv = resumes[-1]["argv"]
    assert argv[argv.index("--append-system-prompt") + 1] == "never touch production"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--add-dir") + 1].endswith("agent-skills")
    # The work order carries no permission_mode of its own, so the fork must fall back
    # to the project's exactly as the initial dispatch does — otherwise turn two runs
    # in Claude's default mode and stalls on a prompt no background worker can answer.
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert argv[-1] == "follow-up"  # the prompt still survives the variadic --add-dir


def test_headless_fallback_turn_is_briefed_like_the_first(started, fake_claude, project,
                                                          monkeypatch):
    """The bg resume-fork can fail, and the headless resume that catches it is still a
    worker turn — same briefing rules apply. It must also resume from the directory the
    session was created in: transcripts are stored per-cwd, so resuming a worktree
    worker from the project root would not find its conversation."""
    daemon = started
    wo = ops.create_work_order("proj_a", "task", model="opus",
                               append_system_prompt="never touch production")
    daemon.tick()
    sid = bind_session(daemon, project, wo["id"])
    wt = project / ".claude" / "worktrees" / wo["id"]
    wt.mkdir(parents=True, exist_ok=True)  # the real CLI makes this; the fake does not

    fake_claude.set_session_state(sid, "done")
    monkeypatch.setenv("FAKE_CLAUDE_BG_RESUME", "fail")
    ops.send_message(wo["id"], "follow-up", source="ui")
    daemon.tick_count = 0
    daemon.tick()
    daemon.delivery_pool.shutdown(wait=True)

    headless = [c for c in fake_claude.calls if "--bg" not in c["argv"]
                and "--resume" in c["argv"] and "-p" in c["argv"]]
    assert headless, "bg fork was forced to fail but no headless resume was attempted"
    call = headless[-1]
    argv = call["argv"]
    assert argv[argv.index("--append-system-prompt") + 1] == "never touch production"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--add-dir") + 1].endswith("agent-skills")
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert argv[argv.index("-p") + 1] == "follow-up"
    assert call["cwd"] == str(wt)  # where the session lives, not the project root


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


def _add_adhoc(fake_claude, project, state, sid="adhoc-session-1", name="my manual hack"):
    sessions = fake_claude.sessions
    sessions.append({"id": "abcd1234", "sessionId": sid,
                     "cwd": str(project), "kind": "background",
                     "name": name, "state": state, "startedAt": 0})
    (fake_claude.dir / "sessions.json").write_text(json.dumps(sessions))


def test_reconciler_adopts_adhoc_sessions(started, fake_claude, project):
    daemon = started
    # a bg session someone started by hand in the project dir
    _add_adhoc(fake_claude, project, "working")

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


def test_adopted_working_session_does_not_ask_for_input(started, fake_claude, project):
    """A healthy ad-hoc worker must never be adopted as "waiting on you".

    Regression: the reconciler compared Claude Code's session state against
    "running" — a word the CLI never emits (it says "working") — so every live
    ad-hoc session was adopted straight into waiting_input and the UI claimed the
    session wanted the user while it was quietly making progress.
    """
    daemon = started
    _add_adhoc(fake_claude, project, "working")
    daemon.tick_count = 0
    daemon.tick()

    store = ProjectStore(project)
    adhoc = [w for w in store.list_work_orders() if w["origin"] == "adhoc"][0]
    assert adhoc["status"] == "running"
    assert not adhoc["needs_attention"]
    assert not true_blockers(store, adhoc)


def test_adopted_blocked_session_asks_for_input(started, fake_claude, project):
    daemon = started
    _add_adhoc(fake_claude, project, "blocked")
    daemon.tick_count = 0
    daemon.tick()

    store = ProjectStore(project)
    adhoc = [w for w in store.list_work_orders() if w["origin"] == "adhoc"][0]
    assert adhoc["status"] == "waiting_input"
    assert adhoc["needs_attention"]


def test_reconciler_clears_attention_when_worker_resumes(started, fake_claude, project):
    """waiting_input must not be sticky: a worker that unblocks stops needing the user.

    Regression: nothing could ever move a work order back to `running`, so the
    "needs you" banner survived long after the worker had resumed.
    """
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = bind_session(daemon, project, wo["id"])

    fake_claude.set_session_state(sid, "blocked")  # hit a permission prompt
    daemon.tick_count = 0
    daemon.tick()
    blocked = store.get_work_order(wo["id"])
    assert blocked["status"] == "waiting_input"
    assert blocked["needs_attention"]

    fake_claude.set_session_state(sid, "working")  # user answered; worker carries on
    daemon.tick_count = 0
    daemon.tick()
    resumed = store.get_work_order(wo["id"])
    assert resumed["status"] == "running"
    assert not resumed["needs_attention"]
    assert not true_blockers(store, resumed)


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_reconciler_settles_non_done_terminal_states(started, fake_claude, project, state):
    """A session that died is finished too — it must not hang in `running` forever."""
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = bind_session(daemon, project, wo["id"])

    fake_claude.set_session_state(sid, state)
    daemon.tick_count = 0
    daemon.tick()
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review"
    assert fresh["needs_attention"]


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


def test_wo_lookup_scoped_to_unregistered_project(started):
    """A deep link can name a project the OS does not know — a notification that
    outlived its project, a typo, a test fixture that leaked into a real sink.
    That must arrive as an OpsError: the CLI and the dashboard only catch OpsError,
    so a raw KeyError here becomes a traceback and an HTTP 500.
    """
    with pytest.raises(ops.OpsError, match="not registered"):
        ops.find_work_order("wo-4fdb20ba", "proj_gone")


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
                            "tool_input": {"command": "cd /p && jarvis status"}}, {})
    assert d["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert preflight_decision({"tool_name": "Bash",
                               "tool_input": {"command": "git push"}}, {}) is None
    assert preflight_decision({"tool_name": "Edit", "tool_input": {}}, {}) is None


def test_pretooluse_allows_worker_edits_in_own_worktree(tmp_path):
    from jarvis.hooks import preflight_decision

    wt = tmp_path / "proj" / ".claude" / "worktrees" / "wo-1"
    wt.mkdir(parents=True)
    worker_env = {"JARVIS_WO_ID": "wo-1"}
    inside = {"tool_name": "Write", "cwd": str(wt),
              "tool_input": {"file_path": str(wt / "HELLO.txt")}}
    outside = {"tool_name": "Write", "cwd": str(wt),
               "tool_input": {"file_path": str(tmp_path / "proj" / "HELLO.txt")}}
    escape = {"tool_name": "Write", "cwd": str(wt),
              "tool_input": {"file_path": str(wt / ".." / ".." / ".." / "x.txt")}}

    d = preflight_decision(inside, worker_env)
    assert d["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert preflight_decision(outside, worker_env) is None
    assert preflight_decision(escape, worker_env) is None
    # interactive session (no JARVIS_WO_ID): never auto-allowed
    assert preflight_decision(inside, {}) is None
    # not in a worktree: never auto-allowed
    not_wt = {"tool_name": "Write", "cwd": str(tmp_path),
              "tool_input": {"file_path": str(tmp_path / "f.txt")}}
    assert preflight_decision(not_wt, worker_env) is None
