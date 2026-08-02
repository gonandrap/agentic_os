"""End-to-end pipeline tests against the fake `claude` CLI:
start → create work order → daemon tick dispatches a turn → hooks update state →
messages deliver as the next turn → finish/review → notifications route → adhoc adoption.
"""

from __future__ import annotations

import json
import uuid

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


def turn_calls(fake_claude, resumed: bool | None = None,
               expect: int = 0) -> list[dict]:
    """Every worker-turn invocation the fake recorded, newest last.

    `resumed=False` selects opening turns (`--session-id`), `True` the ones that
    continue a conversation (`--resume`). Pass `expect` to wait for that many: a turn is
    a detached process, so it records itself a moment after the call that launched it
    returned, and asserting straight away races it.
    """
    def match(call):
        argv = call["argv"]
        if "-p" not in argv:
            return False
        if resumed is True:
            return "--resume" in argv
        if resumed is False:
            return "--session-id" in argv
        return "--session-id" in argv or "--resume" in argv

    if expect:
        return fake_claude.wait_calls(match, count=expect)
    return [c for c in fake_claude.calls if match(c)]


def run_turn(daemon, store, settle_turns):
    """A full cycle: tick (which launches whatever is due), wait for that turn's
    process to exit, then tick again so the work order settles against the result.

    Turns are detached processes now, so a test has to wait for one exactly the way
    the daemon does instead of assuming the work happened inside the call.
    """
    daemon.tick_count = 0
    daemon.tick()
    assert settle_turns(store), "a worker turn never finished"
    daemon.tick_count = 0
    daemon.tick()


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
    assert fresh["worktree"] == wo["id"]
    # The session id exists before the worker does — Jarvis mints it and passes it in,
    # so there is nothing to bind afterwards and nothing that can move it later.
    sid = fresh["session_id"]
    assert uuid.UUID(sid)

    opening = turn_calls(fake_claude, resumed=False, expect=1)
    assert len(opening) == 1
    argv = opening[0]["argv"]
    assert argv[argv.index("--session-id") + 1] == sid
    assert "--bg" not in argv, "workers must not enter the background-agent roster"
    assert argv[argv.index("-n") + 1].startswith(f"[WO {wo['id']}]")
    assert argv[argv.index("--worktree") + 1] == wo["id"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    # full settings (hooks + permissions + env) travel with the turn as a file,
    # because the worktree has no .claude/settings.json (it's untracked)
    settings_path = argv[argv.index("--settings") + 1]
    settings = json.loads(open(settings_path).read())
    assert settings["env"]["JARVIS_WO_ID"] == wo["id"]
    assert settings["env"]["JARVIS_PROJECT_PATH"] == str(project)
    assert "PATH" in settings["env"]
    assert "Stop" in settings["hooks"]
    assert "Bash(jarvis *)" in settings["permissions"]["allow"]
    assert argv[-2] == "--", "the prompt must stay fenced off from variadic options"
    prompt = argv[-1]
    assert "add feature X" in prompt and "OPERATION.md" in prompt

    # the turn is on the record as turn 1 of the conversation
    turn = store.latest_turn(wo["id"])
    assert turn["seq"] == 1 and turn["kind"] == "dispatch" and turn["state"] == "running"
    assert turn["pid"]


def test_worker_never_appears_in_the_agents_roster(started, fake_claude):
    """The pileup this transport exists to end: 63 dead `[WO …]` background agents.

    A worker turn is a process Jarvis owns, so nothing it does can leave a session
    behind in the roster to be cleaned up later.
    """
    daemon = started
    ops.create_work_order("proj_a", "task")
    daemon.tick()
    assert fake_claude.sessions == []


def test_concurrency_limit(started, fake_claude):
    daemon = started
    for i in range(7):
        ops.create_work_order("proj_a", f"task {i}")
    daemon.tick()
    assert len(turn_calls(fake_claude, expect=5)) == 5  # the other 2 stay queued


def test_knowledge_injected_into_prompt(started, fake_claude):
    daemon = started
    central = CentralStore()
    central.add_knowledge("always run make lint", project="proj_a", topic="ci")
    central.add_knowledge("global: prefer uv over pip", project="")
    ops.create_work_order("proj_a", "task")
    daemon.tick()
    prompt = turn_calls(fake_claude, expect=1)[0]["argv"][-1]
    assert "always run make lint" in prompt
    assert "prefer uv over pip" in prompt


def test_hook_events_update_state(started, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = store.get_work_order(wo["id"])["session_id"]

    env = {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)}
    handle_hook({"hook_event_name": "Notification", "session_id": sid,
                 "cwd": str(project), "message": "needs permission for Bash"}, env)
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "waiting_input"
    assert fresh["needs_attention"] == 1
    # a notification was queued for the user
    assert any("needs input" in n["title"] for n in store.unrouted_notifications())

    handle_hook({"hook_event_name": "Stop", "session_id": sid, "cwd": str(project),
                 "last_assistant_message": "here is what I did"}, env)
    stop = [e for e in store.list_events(wo["id"]) if e["kind"] == "hook:Stop"]
    assert stop, "the Stop hook must land on the timeline"
    assert json.loads(stop[-1]["payload"])["last_assistant_message"] == \
        "here is what I did"


def test_session_end_no_longer_settles_the_work_order(started, project):
    """Under `-p`, SessionEnd fires at the end of EVERY turn — verified against the real
    CLI. Left as it was (file the work order for review), a healthy work order would be
    filed for review after its very first turn. Settling is the turn reconciler's job.
    """
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = store.get_work_order(wo["id"])["session_id"]

    handle_hook({"hook_event_name": "SessionEnd", "session_id": sid,
                 "cwd": str(project), "reason": "other"},
                {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)})

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "running"
    assert fresh["needs_attention"] == 0


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
    sid = store.get_work_order(wo["id"])["session_id"]

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
    to `auto` and nudging it to continue — the daemon then sends the nudge as the next
    turn, which re-derives the now-`auto` mode from argv."""
    daemon = started
    wo = ops.create_work_order("proj_a", "task", permission_mode="acceptEdits")
    daemon.tick()
    store = ProjectStore(project)
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


def test_message_delivery_when_idle(started, fake_claude, project, settle_turns):
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = store.get_work_order(wo["id"])["session_id"]

    ops.send_message(wo["id"], "please also update the docs", source="ui")
    daemon.tick_count = 0
    daemon.tick()  # turn one is still in flight → nothing deliverable yet
    assert turn_calls(fake_claude, resumed=True) == []

    assert settle_turns(store)
    daemon.tick_count = 0
    daemon.tick()

    # delivered as the next turn of the SAME conversation
    resumes = turn_calls(fake_claude, resumed=True, expect=1)
    assert len(resumes) == 1
    argv = resumes[0]["argv"]
    assert argv[argv.index("--resume") + 1] == sid
    assert argv[argv.index("-n") + 1].startswith(f"[WO {wo['id']}]")
    assert "update the docs" in argv[-1]

    assert store.list_messages(wo["id"])[0]["status"] == "delivered"

    assert settle_turns(store)
    replies = [m["content"] for m in store.list_messages(wo["id"])
               if m["direction"] == "agent_to_user"]
    assert any("update the docs" in r for r in replies), replies


def test_session_id_never_moves_across_turns(started, fake_claude, project,
                                             settle_turns):
    """The property the whole refactor rests on: one work order, one session id, for
    every turn of its life.

    Under the old transport each delivered turn forked a new background agent under a
    fresh supervisor-assigned id, which needed `bind_session`, a `prior_sessions` trail
    and an invariant just to keep the pointer from walking backwards — and leaked the
    superseded agent whenever the retiring `claude stop` did not land.
    """
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = store.get_work_order(wo["id"])["session_id"]
    assert settle_turns(store)

    for text in ("first follow-up", "second follow-up"):
        ops.send_message(wo["id"], text, source="ui")
        daemon.tick_count = 0
        daemon.tick()
        assert settle_turns(store)

    assert store.get_work_order(wo["id"])["session_id"] == sid
    # every turn named that one id, and the fake — like the real CLI — handed it back
    assert list(fake_claude.turns) == [sid]
    assert len(fake_claude.turns[sid]) == 3
    assert [t["seq"] for t in store.list_turns(wo["id"])] == [1, 2, 3]
    assert fake_claude.sessions == []  # nothing left behind in the agents roster


def test_worker_final_message_is_recorded_alongside_the_summary(
    started, fake_claude, project, settle_turns
):
    """The work order must stand alone — the user and Neo decide from it and never open
    the worker session. `--summary` is a headline; the worker's full closing message is
    kept next to it. Regression: `wo finish` flips the order to `completed` before its
    turn ends, so a status-filtered sweep would drop exactly that reply."""
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)

    ops.finish(wo["id"], "one-line headline")
    assert store.get_work_order(wo["id"])["status"] == "completed"

    assert settle_turns(store)
    daemon.tick_count = 0
    daemon.tick()

    fresh = store.get_work_order(wo["id"])
    assert fresh["result_summary"] == "one-line headline"   # headline survives
    replies = [m["content"] for m in store.list_messages(wo["id"])
               if m["direction"] == "agent_to_user"]
    assert any(r.startswith("final:") for r in replies), replies
    assert "turn_ended" in [e["kind"] for e in store.list_events(wo["id"])]

    # Capture is idempotent: further passes must not duplicate the reply.
    for _ in range(2):
        daemon.tick_count = 0
        daemon.tick()
    again = [m["content"] for m in store.list_messages(wo["id"])
             if m["direction"] == "agent_to_user"]
    assert again == replies


def test_crashed_turn_fails_the_work_order(started, fake_claude, project, settle_turns):
    """A turn whose process dies without writing a result must fail the work order, not
    hang it in `running` forever waiting for output that is never coming."""
    daemon = started
    fake_claude.turns_fail("silent")  # exits 0, writes nothing at all
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)

    assert settle_turns(store)
    daemon.tick_count = 0
    daemon.tick()

    assert store.latest_turn(wo["id"])["state"] == "failed"
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "failed"
    assert fresh["needs_attention"]
    assert any("turn failed" in n["title"] or "turn failed" in n["body"]
               for n in store.unrouted_notifications())


def test_turn_reporting_is_error_fails_the_work_order(started, fake_claude, project,
                                                      settle_turns):
    """A well-formed result carrying `is_error` is a failed turn, however tidy it looks."""
    daemon = started
    fake_claude.turns_fail("error")
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    assert settle_turns(store)
    daemon.tick_count = 0
    daemon.tick()

    turn = store.latest_turn(wo["id"])
    assert turn["state"] == "failed" and "model call failed" in turn["error"]
    assert store.get_work_order(wo["id"])["status"] == "failed"


def test_a_turn_in_flight_blocks_a_second_one(started, fake_claude, project,
                                              settle_turns):
    """One turn at a time per work order: two concurrent `--resume`s of one session is
    something the CLI refuses, and would interleave into one transcript if it did not."""
    daemon = started
    gate = fake_claude.hold_turns()
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)

    ops.send_message(wo["id"], "hurry up", source="ui")
    for _ in range(3):
        daemon.tick_count = 0
        daemon.tick()
    assert turn_calls(fake_claude, resumed=True) == []
    assert store.list_messages(wo["id"])[0]["status"] == "queued"

    gate.unlink()  # turn one completes
    assert settle_turns(store)
    daemon.tick_count = 0
    daemon.tick()
    assert len(turn_calls(fake_claude, resumed=True, expect=1)) == 1


def test_hooks_from_an_unknown_session_do_not_steer_the_work_order(started, project):
    """A work order has one session id now, but legacy rows can still be hooked by a
    session they have left — a spent background agent the user re-opens from the agents
    view. Those hooks are recorded and acted on never."""
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)

    for event in ("SessionEnd", "Notification", "Stop"):
        handle_hook({"hook_event_name": event, "session_id": "some-other-session",
                     "cwd": str(project), "message": "Claude is waiting for your input"},
                    {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)})

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "running"
    assert fresh["needs_attention"] == 0
    assert "hook_ignored" in [e["kind"] for e in store.list_events(wo["id"])]


def test_follow_up_turn_is_briefed_like_the_first(started, fake_claude, project,
                                                  settle_turns):
    """A resumed session re-derives its system prompt at launch — it does not inherit
    the first turn's from the transcript. So every turn must carry the same briefing:
    without it the project's standing instructions to the worker (and the OS skills
    directory) silently vanish from turn two onwards.

    It must also resume from the directory the session was created in: transcripts are
    stored per-cwd, so resuming a worktree worker from the project root would not find
    its conversation.
    """
    daemon = started
    wo = ops.create_work_order("proj_a", "task", model="opus",
                               append_system_prompt="never touch production")
    daemon.tick()
    store = ProjectStore(project)
    wt = project / ".claude" / "worktrees" / wo["id"]
    wt.mkdir(parents=True, exist_ok=True)  # the real CLI makes this; the fake does not
    assert settle_turns(store)

    ops.send_message(wo["id"], "follow-up", source="ui")
    daemon.tick_count = 0
    daemon.tick()

    call = turn_calls(fake_claude, resumed=True, expect=1)[-1]
    argv = call["argv"]
    assert argv[argv.index("--append-system-prompt") + 1] == "never touch production"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--add-dir") + 1].endswith("agent-skills")
    # The work order carries no permission_mode of its own, so the follow-up must fall
    # back to the project's exactly as the opening turn does — otherwise turn two runs
    # in Claude's default mode and stalls on a prompt no headless worker can answer.
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert "--worktree" not in argv, "the worktree already exists; it is the cwd now"
    assert argv[-2] == "--" and argv[-1] == "follow-up"
    assert call["cwd"] == str(wt)  # where the session lives, not the project root


def test_injected_turn_gets_the_projects_permission_mode(started, fake_claude, project,
                                                         settle_turns):
    """An injected session's row has permission_mode NULL — it never went through
    dispatch, which is what resolves the mode and writes it back. Sending it a message
    hands it to Jarvis to drive, and a headless turn cannot answer a permission prompt:
    launched in Claude's default mode it would stall, costing the user a
    `jarvis wo resume-auto` to clear. So the turn resolves the project's worker mode at
    the call site — without backfilling the column, so an injected work order stays
    distinguishable from a dispatched one."""
    daemon = started
    res = _inject(fake_claude, project, "working", sid="adhoc-pm-1")
    store = ProjectStore(project)
    wo = store.get_work_order(res["wo_id"])
    assert wo["permission_mode"] is None, "injected rows must not carry a resolved mode"

    fake_claude.set_session_state("adhoc-pm-1", "done")
    ops.send_message(wo["id"], "carry on", source="ui")
    daemon.tick_count = 0
    daemon.tick()

    resumes = turn_calls(fake_claude, resumed=True, expect=1)
    assert resumes, "no turn was launched for the injected session"
    argv = resumes[-1]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    # resolved for the launch only — the row still says "nobody dispatched this"
    assert store.get_work_order(wo["id"])["permission_mode"] is None


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


def test_reconciler_settles_done_worker(started, fake_claude, project, settle_turns):
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)

    ops.finish(wo["id"], "all good")   # worker finished properly, then its turn ended
    assert settle_turns(store)
    daemon.tick_count = 0
    daemon.tick()
    assert store.get_work_order(wo["id"])["status"] == "completed"


def test_reconciler_flags_unfinished_idle_worker(started, fake_claude, project,
                                                 settle_turns):
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)

    assert settle_turns(store)  # turn ended but the worker never called finish
    daemon.tick_count = 0
    daemon.tick()
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review"
    assert "without `jarvis wo finish`" in fresh["attention_reason"]


def _add_session(fake_claude, project, state, sid="adhoc-session-1",
                 name="my manual hack", cwd=None):
    """A Claude session the USER started, in the project directory."""
    sessions = fake_claude.sessions
    sessions.append({"id": "abcd1234", "sessionId": sid,
                     "cwd": str(cwd or project), "kind": "background",
                     "name": name, "state": state, "startedAt": 0})
    (fake_claude.dir / "sessions.json").write_text(json.dumps(sessions))


def _inject(fake_claude, project, state, sid="adhoc-session-1",
            name="my manual hack", **kw):
    """Add the session and hand it to Jarvis, the way the user would."""
    _add_session(fake_claude, project, state, sid=sid, name=name)
    return ops.inject_session(sid, **kw)


def test_reconciler_does_not_adopt_sessions(started, fake_claude, project):
    """The heart of GitHub issue 47: a session the user started is theirs.

    Jarvis used to mirror every session running under a registered project path into an
    `origin=adhoc` work order "for visibility", and then treat that record as a work
    order like any other — renaming the session, flagging it for the user's attention,
    and (via `wo send` / `resume-auto`) resuming it headlessly with a worker briefing, so
    the user's own conversation received turns they never typed. It must not see the
    session at all until it is handed over.
    """
    daemon = started
    _add_session(fake_claude, project, "working")

    daemon.tick_count = 0
    daemon.tick()
    daemon.tick_count = 0
    daemon.tick()

    store = ProjectStore(project)
    assert store.list_work_orders() == [], "the session was adopted without consent"


def test_reconciler_does_not_read_the_roster_with_nothing_injected(started, fake_claude,
                                                                   project):
    """Tracking injected sessions is the only thing left that needs `claude agents
    --json`. With nothing injected, the subprocess must not run at all."""
    daemon = started
    _add_session(fake_claude, project, "working")
    before = [c for c in fake_claude.calls if c["argv"][:1] == ["agents"]]

    daemon.tick_count = 0
    daemon.tick()

    after = [c for c in fake_claude.calls if c["argv"][:1] == ["agents"]]
    assert len(after) == len(before), "listed the user's sessions with nothing to track"


def test_injected_session_is_tracked(started, fake_claude, project):
    daemon = started
    res = _inject(fake_claude, project, "working")
    store = ProjectStore(project)

    wo = store.get_work_order(res["wo_id"])
    assert wo["origin"] == "injected"
    assert wo["title"] == "my manual hack"
    assert wo["status"] == "running"
    assert wo["session_id"] == "adhoc-session-1"

    # tracked from here: the session ends, the record follows it — and stays singular
    daemon.tick_count = 0
    daemon.tick()
    assert len(store.list_work_orders()) == 1


def test_injecting_writes_nothing_into_the_session(started, fake_claude, project):
    """Injection is consent to *track*, not to drive. The session is not renamed and
    receives no turn; the first write is the user's own `wo send` / `resume-auto`."""
    daemon = started
    _inject(fake_claude, project, "working")
    daemon.tick_count = 0
    daemon.tick()

    assert not turn_calls(fake_claude, expect=0), "injection started a turn"
    assert fake_claude.sessions[-1]["name"] == "my manual hack", "session was renamed"


def test_injected_session_is_never_dispatched(started, fake_claude, project):
    """The record must not exist as `pending` for even one tick: the daemon claims
    pending work orders and would launch a worker turn inside the user's session."""
    daemon = started
    res = _inject(fake_claude, project, "working")
    store = ProjectStore(project)
    assert store.get_work_order(res["wo_id"])["status"] == "running"

    daemon.tick()  # dispatch_pending runs every tick, not just reconcile ones
    assert not turn_calls(fake_claude, expect=0)
    assert store.get_work_order(res["wo_id"])["worktree"] is None


def test_injected_working_session_does_not_ask_for_input(started, fake_claude, project):
    """A healthy session must never be recorded as "waiting on you".

    Regression: the reconciler compared Claude Code's session state against
    "running" — a word the CLI never emits (it says "working") — so every live
    session was recorded straight into waiting_input and the UI claimed the
    session wanted the user while it was quietly making progress.
    """
    daemon = started
    res = _inject(fake_claude, project, "working")
    daemon.tick_count = 0
    daemon.tick()

    store = ProjectStore(project)
    wo = store.get_work_order(res["wo_id"])
    assert wo["status"] == "running"
    assert not wo["needs_attention"]
    assert not true_blockers(store, wo)


def test_injected_blocked_session_asks_for_input(started, fake_claude, project):
    daemon = started
    res = _inject(fake_claude, project, "blocked")
    daemon.tick_count = 0
    daemon.tick()

    store = ProjectStore(project)
    wo = store.get_work_order(res["wo_id"])
    assert wo["status"] == "waiting_input"
    assert wo["needs_attention"]


def test_inject_resolves_the_project_from_the_sessions_directory(started, fake_claude,
                                                                 project):
    daemon = started  # noqa: F841 — the OS must be up for projects to be registered
    _add_session(fake_claude, project, "working", sid="sub-1",
                 cwd=str(project) + "/src/deep")
    res = ops.inject_session("sub-1")
    assert res["project"] == "proj_a"


def test_inject_refuses_what_it_cannot_place(started, fake_claude, project, tmp_path):
    daemon = started  # noqa: F841
    _add_session(fake_claude, project, "working", sid="stray-1",
                 cwd=str(tmp_path / "somewhere-else"))

    with pytest.raises(ops.OpsError, match="not inside any registered project"):
        ops.inject_session("stray-1")
    with pytest.raises(ops.OpsError, match="no Claude session"):
        ops.inject_session("no-such-session")

    # --project is the override: the same session, placed by hand, is accepted
    assert ops.inject_session("stray-1", project_name="proj_a")["project"] == "proj_a"


def test_injecting_twice_does_not_duplicate_the_record(started, fake_claude, project):
    daemon = started  # noqa: F841
    first = _inject(fake_claude, project, "working")
    again = ops.inject_session("adhoc-session-1")

    assert again["wo_id"] == first["wo_id"]
    assert again["already_known"]
    store = ProjectStore(project)
    assert len(store.list_work_orders()) == 1


def test_re_injecting_resumes_tracking_a_session_that_woke_up(started, fake_claude,
                                                              project):
    """The daemon stops reading the roster once a project has no live injected session —
    that is what keeps `claude agents --json` off the tick for everyone who never
    injects. So re-injecting is how a retired session is picked back up."""
    daemon = started
    res = _inject(fake_claude, project, "working")
    store = ProjectStore(project)

    fake_claude.set_session_state("adhoc-session-1", "done")
    daemon.tick_count = 0
    daemon.tick()
    assert store.get_work_order(res["wo_id"])["status"] == "completed"

    fake_claude.set_session_state("adhoc-session-1", "working")  # the user typed again
    again = ops.inject_session("adhoc-session-1")

    assert again["wo_id"] == res["wo_id"]
    assert store.get_work_order(res["wo_id"])["status"] == "running"
    assert "tracking has resumed" in again["note"]


def test_reconciler_clears_attention_when_worker_resumes(started, fake_claude, project,
                                                         settle_turns):
    """waiting_input must not be sticky: a worker that gets going again stops needing
    the user.

    Regression: nothing could ever move a work order back to `running`, so the
    "needs you" banner survived long after the worker had resumed.
    """
    daemon = started
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    sid = store.get_work_order(wo["id"])["session_id"]

    handle_hook({"hook_event_name": "Notification", "session_id": sid,
                 "cwd": str(project), "message": "Claude needs your permission"},
                {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)})
    blocked = store.get_work_order(wo["id"])
    assert blocked["status"] == "waiting_input"
    assert blocked["needs_attention"]

    assert settle_turns(store)
    ops.send_message(wo["id"], "yes, go ahead", source="ui")  # user answered
    daemon.tick_count = 0
    daemon.tick()

    resumed = store.get_work_order(wo["id"])
    assert resumed["status"] == "running"
    assert not resumed["needs_attention"]
    assert not true_blockers(store, resumed)


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
