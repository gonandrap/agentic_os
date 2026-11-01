"""A worker conversation as ONE background session (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)"""

import json
import shutil
import subprocess
import time

import pytest

from jarvis import claude_cli


def _wait_idle(session_id: str, timeout: float = 10.0) -> claude_cli.BgEntry:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = claude_cli.bg_roster().get(session_id)
        if entry is not None and entry.idle:
            return entry
        time.sleep(0.02)
    raise AssertionError(f"{session_id} never went idle")


# -- the primitives, against the fake's model of the measured CLI


def test_spawn_learns_the_session_id_the_cli_minted(fake_claude, tmp_path):
    entry = claude_cli.spawn_bg_turn("do the thing", tmp_path, "[WO wo-bg01] t",
                                     model="claude-fake-1")
    assert entry.id == entry.session_id[:8]
    assert entry.alive
    assert _wait_idle(entry.session_id).session_id == entry.session_id
    assert fake_claude.turns[entry.session_id] == ["do the thing"]


def test_spawn_never_passes_the_print_only_budget_flag(fake_claude, tmp_path):
    claude_cli.spawn_bg_turn("x", tmp_path, "n", max_budget_usd=3.0)
    argv = fake_claude.wait_calls(lambda c: "--bg" in c["argv"])[0]["argv"]
    assert "--max-budget-usd" not in argv


def test_resume_continues_the_same_session_bare(fake_claude, tmp_path):
    first = claude_cli.spawn_bg_turn("one", tmp_path, "n", model="claude-fake-1")
    _wait_idle(first.session_id)

    again = claude_cli.resume_bg_turn(first.session_id, "two", tmp_path)

    assert again.session_id == first.session_id
    _wait_idle(first.session_id)
    assert fake_claude.turns[first.session_id] == ["one", "two"]
    resumes = [c["argv"] for c in fake_claude.calls if "--resume" in c["argv"]]
    # BARE: any flag would have started a copy (fact 6)
    assert resumes == [["--bg", "--resume", first.session_id, "--", "two"]]
    # …and the live original was stopped first (fact 5)
    assert any(c["argv"][:1] == ["stop"] for c in fake_claude.calls)
    assert len([s for s in fake_claude.sessions if s.get("worker")]) == 1


def test_a_resume_that_forks_is_taken_down_and_refused(fake_claude, tmp_path, monkeypatch):
    first = claude_cli.spawn_bg_turn("one", tmp_path, "n")
    _wait_idle(first.session_id)
    # A stop that never takes: the original stays alive, so the resume would fork
    monkeypatch.setattr(claude_cli, "stop_bg", lambda short, wait=0: True)

    with pytest.raises(claude_cli.ClaudeCliError, match="started a copy"):
        claude_cli.resume_bg_turn(first.session_id, "two", tmp_path)

    assert [s["sessionId"] for s in fake_claude.sessions
            if s.get("worker")] == [first.session_id]


def test_respawn_keeps_the_id_and_sends_nothing(fake_claude, tmp_path):
    first = claude_cli.spawn_bg_turn("one", tmp_path, "n")
    _wait_idle(first.session_id)
    assert claude_cli.stop_bg(first.id)
    assert not claude_cli.bg_roster()[first.session_id].alive

    assert claude_cli.respawn_bg(first.id)

    back = claude_cli.bg_roster()[first.session_id]
    assert back.alive and back.idle
    assert fake_claude.turns[first.session_id] == ["one"]


def test_remove_deletes_the_session(fake_claude, tmp_path):
    first = claude_cli.spawn_bg_turn("one", tmp_path, "n")
    _wait_idle(first.session_id)
    assert claude_cli.remove_bg(first.id)
    assert first.session_id not in claude_cli.bg_roster()


def test_an_oversized_prompt_goes_by_file(fake_claude, tmp_path):
    big = "x" * (claude_cli.BG_PROMPT_ARGV_LIMIT + 1)
    promptfile = tmp_path / "turn.prompt"
    claude_cli.spawn_bg_turn(big, tmp_path, "n", promptfile=promptfile)
    assert promptfile.read_text() == big
    argv = fake_claude.wait_calls(lambda c: "--bg" in c["argv"])[0]["argv"]
    assert str(promptfile) in argv[-1] and len(argv[-1]) < 1000


# -- the fake keeps faith with the measured CLI, or every test above proves nothing


def _claude(fake_claude, *args, cwd):
    return subprocess.run([str(fake_claude.dir / "claude"), *args], cwd=cwd,
                          capture_output=True, text=True, check=True)


def test_the_fake_forks_a_live_session_and_a_flagged_resume(fake_claude, tmp_path):
    first = claude_cli.spawn_bg_turn("one", tmp_path, "n")
    _wait_idle(first.session_id)
    live = _claude(fake_claude, "--bg", "--resume", first.session_id, "--", "x", cwd=tmp_path)
    assert "already running" in live.stderr
    claude_cli.stop_bg(first.id)
    flagged = _claude(fake_claude, "--bg", "--resume", first.session_id, "--model", "m",
                      "--", "y", cwd=tmp_path)
    assert "saved options" in flagged.stderr
    assert len({s["sessionId"] for s in fake_claude.sessions if s.get("worker")}) == 3


def test_the_fake_writes_a_transcript_jarvis_can_price(fake_claude, tmp_path):
    from jarvis import usage

    first = claude_cli.spawn_bg_turn("one", tmp_path, "n")
    _wait_idle(first.session_id)
    assert usage.cost_between(first.session_id, 0, float("inf")) > 0
    said = usage.said_in_session(first.session_id)
    assert said and said[-1][1] == "final: one"
    json.dumps(said)


# -- a work order's conversation, end to end on the background transport

from jarvis import invariants, ops, worker_session  # noqa: E402
from jarvis.catalog import load_catalog  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402


@pytest.fixture()
def bg_fleet(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    data = json.loads(catalog_file.read_text())
    data["os"]["defaults"]["transport"] = "background"
    catalog_file.write_text(json.dumps(data))
    # A process the fake kills is gone at once; nothing to wait out
    monkeypatch.setattr(worker_session, "BG_DEAD_GRACE", 0.0)
    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    return {"project": catalog.projects[0], "store": ProjectStore(project)}


def _bg_wo(bg_fleet, title="task"):
    wo = ops.create_work_order("proj_a", title)
    return bg_fleet["store"].get_work_order(wo["id"])


def test_a_background_order_learns_its_session_and_settles_from_the_transcript(
        bg_fleet, fake_claude, settle_turns):
    store, project = bg_fleet["store"], bg_fleet["project"]
    wo = _bg_wo(bg_fleet)

    turn, _ = worker_session.start(store, project, wo, "go")

    fresh = store.get_work_order(wo["id"])
    assert fresh["transport"] == "background"
    assert fresh["session_id"] and fresh["bg_id"] == fresh["session_id"][:8]
    assert turn["pid"] is None
    assert settle_turns(store)
    done = store.get_turn(turn["id"])
    assert done["state"] == "done"
    assert done["result"] == "final: go"
    assert done["cost_usd"] and done["cost_usd"] > 0
    assert done["cost_source"] == "transcript"
    envelope = json.loads(done["usage_json"])
    assert envelope["api_calls"] == 1 and envelope["context_peak"] == 3003
    # Settled and idle: the process is let go, the conversation kept
    entry = claude_cli.bg_roster()[fresh["session_id"]]
    assert not entry.alive


def test_every_later_turn_resumes_the_same_session(bg_fleet, fake_claude, settle_turns):
    store, project = bg_fleet["store"], bg_fleet["project"]
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)
    sid = store.get_work_order(wo["id"])["session_id"]

    for text in ("second", "third"):
        worker_session.send(store, project, store.get_work_order(wo["id"]), text)
        assert settle_turns(store)

    assert store.get_work_order(wo["id"])["session_id"] == sid
    assert fake_claude.turns[sid] == ["go", "second", "third"]
    assert [s["sessionId"] for s in fake_claude.sessions if s.get("worker")] == [sid]
    assert [t["result"] for t in store.list_turns(wo["id"])] == [
        "final: go", "final: second", "final: third"]


def test_a_busy_session_holds_the_turn_open(bg_fleet, fake_claude, settle_turns):
    """`busy` covers the worker's own background tasks: the OS never reads it as over (fact 10)"""
    store, project = bg_fleet["store"], bg_fleet["project"]
    gate = fake_claude.hold_turns()
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")

    worker_session.poll(store)
    assert worker_session.busy(store, wo["id"])["seq"] == 1
    gate.unlink()
    assert settle_turns(store)
    assert worker_session.busy(store, wo["id"]) is None


def test_a_turn_parked_on_a_queued_message_is_reaped(bg_fleet, fake_claude, monkeypatch):
    """Ended with a task left running, quiet past the threshold, a message waiting (issue #1400)"""
    store, project = bg_fleet["store"], bg_fleet["project"]
    gate = fake_claude.hold_turns()  # stands in for the task that keeps the session busy
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")
    store.add_event(wo["id"], "hook:Stop", {"last_assistant_message": "Waiting on Neo."})
    monkeypatch.setattr(worker_session, "BG_PARKED_QUIET", 0.0)

    # Nothing waits on it yet: still busy, never cut short
    worker_session.poll(store)
    assert worker_session.busy(store, wo["id"])

    store.queue_message(wo["id"], "[Neo] go with (a)", source="neo")
    worker_session.poll(store)
    gate.unlink()

    turn = store.latest_turn(wo["id"])
    assert turn["state"] == "done" and turn["result"] == "Waiting on Neo."
    sid = store.get_work_order(wo["id"])["session_id"]
    assert not claude_cli.bg_roster()[sid].alive
    assert worker_session.delivery_hold(store, store.get_work_order(wo["id"])) is None


def test_a_session_that_dies_mid_turn_is_a_transport_pause(bg_fleet, fake_claude,
                                                           settle_turns):
    store, project = bg_fleet["store"], bg_fleet["project"]
    fake_claude.turns_fail("silent")
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)

    pause = worker_session.turn_pause(store, wo["id"])
    assert pause is not None and pause.reason == worker_session.PAUSE_TRANSIENT
    assert worker_session.SESSION_LOST_ERROR in pause.message

    # …and the retry wakes the SAME session; it died before any API call, so the prompt goes again
    fake_claude.turns_recover()
    worker_session.retry(store, project, store.get_work_order(wo["id"]), pause)
    assert settle_turns(store)
    sid = store.get_work_order(wo["id"])["session_id"]
    assert fake_claude.turns[sid] == ["go", "go"]
    assert store.latest_turn(wo["id"])["state"] == "done"


def test_the_usage_limit_is_read_off_the_transcript(bg_fleet, fake_claude, settle_turns):
    store, project = bg_fleet["store"], bg_fleet["project"]
    fake_claude.turns_rate_limited()
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)

    pause = worker_session.turn_pause(store, wo["id"])
    assert pause is not None and pause.reason == worker_session.PAUSE_USAGE_LIMIT


def test_an_api_error_is_a_transient_pause_that_nudges(bg_fleet, fake_claude,
                                                       settle_turns):
    store, project = bg_fleet["store"], bg_fleet["project"]
    fake_claude.turns_api_error(529)
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)

    pause = worker_session.turn_pause(store, wo["id"])
    assert pause is not None and pause.reason == worker_session.PAUSE_TRANSIENT
    assert pause.status == 529
    assert worker_session._reached_model(pause.turn)


def test_cancel_stops_the_session_and_keeps_the_conversation(bg_fleet, fake_claude):
    store, project = bg_fleet["store"], bg_fleet["project"]
    gate = fake_claude.hold_turns()
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")

    out = worker_session.cancel(store, wo["id"])

    gate.unlink()
    assert out["stopped"] is True
    assert store.latest_turn(wo["id"])["state"] == "failed"
    sid = store.get_work_order(wo["id"])["session_id"]
    assert not claude_cli.bg_roster()[sid].alive


def test_the_daemon_drives_a_background_order_end_to_end(bg_fleet, fake_claude,
                                                         settle_turns, catalog_file):
    """Dispatch, settle, a user message, settle — one session the whole way"""
    from jarvis.daemon import Daemon

    store = bg_fleet["store"]
    daemon = Daemon(load_catalog(catalog_file))
    wo = ops.create_work_order("proj_a", "summarise the repo")
    daemon.tick()
    assert settle_turns(store)
    daemon.tick()
    sid = store.get_work_order(wo["id"])["session_id"]
    assert sid and store.get_work_order(wo["id"])["transport"] == "background"

    ops.send_message(wo["id"], "and the tests too", source="ui")
    daemon.tick()
    assert settle_turns(store)
    daemon.tick()

    assert store.get_work_order(wo["id"])["session_id"] == sid
    assert len(fake_claude.turns[sid]) == 2
    assert fake_claude.turns[sid][1].endswith("and the tests too")
    assert [s["sessionId"] for s in fake_claude.sessions if s.get("worker")] == [sid]
    settings = json.loads(next(
        (bg_fleet["project"].path / ".jarvis" / "worker-settings").glob("*.json")
    ).read_text())
    # The hooks key their one-shot rules on this: they must fall silent here
    assert settings["env"]["JARVIS_TURN_TRANSPORT"] == "background"


def test_wo_respawn_restarts_the_same_session(bg_fleet, fake_claude, settle_turns):
    store, project = bg_fleet["store"], bg_fleet["project"]
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)
    sid = store.get_work_order(wo["id"])["session_id"]

    out = ops.respawn_worker(wo["id"])

    assert out["respawned"] and out["session_id"] == sid
    assert claude_cli.bg_roster()[sid].idle
    assert fake_claude.turns[sid] == ["go"]  # nothing sent


def test_wo_respawn_refuses_a_headless_order(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    wo = ops.create_work_order("proj_a", "t")
    with pytest.raises(ops.OpsError, match="background session"):
        ops.respawn_worker(wo["id"])


def test_deleting_the_order_removes_its_session(bg_fleet, fake_claude, settle_turns):
    store, project = bg_fleet["store"], bg_fleet["project"]
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)
    sid = store.get_work_order(wo["id"])["session_id"]

    out = ops.delete_work_order(wo["id"])

    assert out["deleted"]["background_session"] == 1
    assert sid not in claude_cli.bg_roster()


def test_a_prompt_at_the_limit_stays_in_argv(fake_claude, tmp_path):
    fits = "x" * claude_cli.BG_PROMPT_ARGV_LIMIT
    promptfile = tmp_path / "turn.prompt"
    claude_cli.spawn_bg_turn(fits, tmp_path, "n", promptfile=promptfile)
    assert not promptfile.exists()
    assert fake_claude.wait_calls(lambda c: "--bg" in c["argv"])[0]["argv"][-1] == fits


def _settled_bg_wo(bg_fleet, settle_turns):
    store, project = bg_fleet["store"], bg_fleet["project"]
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)
    return store.get_work_order(wo["id"])


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_a_closed_order_loses_its_session_once(bg_fleet, fake_claude, settle_turns,
                                               status):
    store = bg_fleet["store"]
    wo = _settled_bg_wo(bg_fleet, settle_turns)
    store.set_status(wo["id"], status)

    assert worker_session.poll(store) == []

    assert wo["session_id"] not in claude_cli.bg_roster()
    fresh = store.get_work_order(wo["id"])
    assert fresh["bg_id"] is None and fresh["session_id"] == wo["session_id"]
    [event] = [e for e in store.list_events(wo["id"])
               if e["kind"] == "background_session_removed"]
    assert json.loads(event["payload"])["bg_id"] == wo["bg_id"]
    # Once: nothing left to remove, so the next poll asks the CLI nothing
    calls = len(fake_claude.calls)
    worker_session.poll(store)
    assert len(fake_claude.calls) == calls


def _worktree_bg_wo(bg_fleet, settle_turns, leftovers: dict[str, str]):
    """A settled order whose worktree holds `leftovers` (path -> text), uncommitted (#1257)"""
    store, project = bg_fleet["store"], bg_fleet["project"]
    wo = _bg_wo(bg_fleet)
    tree = project.path / ".claude" / "worktrees" / wo["id"]
    subprocess.run(["git", "worktree", "add", "-q", "-b", f"b-{wo['id']}", str(tree)],
                   cwd=project.path, check=True)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)
    for rel, text in leftovers.items():
        path = tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return store.get_work_order(wo["id"]), tree


def test_an_untracked_build_cache_does_not_keep_a_closed_order_session(
        bg_fleet, fake_claude, settle_turns):
    """What `pytest` left behind is regenerable, and `claude rm` kept the session for it (#1257)"""
    store = bg_fleet["store"]
    wo, tree = _worktree_bg_wo(bg_fleet, settle_turns, {
        "__pycache__/app.cpython-313.pyc": "x",
        "tests/__pycache__/test_app.cpython-313.pyc": "x",
    })
    store.set_status(wo["id"], "completed")

    worker_session.poll(store)

    assert store.get_work_order(wo["id"])["bg_id"] is None
    assert wo["session_id"] not in claude_cli.bg_roster()
    assert not (tree / "__pycache__").exists()


def test_a_session_kept_over_real_work_is_recorded_once(bg_fleet, fake_claude,
                                                        settle_turns, caplog):
    """An untracked SOURCE file is not regenerable: the refusal is said once, not every tick (#1257)"""
    store = bg_fleet["store"]
    wo, tree = _worktree_bg_wo(bg_fleet, settle_turns, {"notes.md": "unsaved work\n"})
    store.set_status(wo["id"], "completed")

    with caplog.at_level("WARNING"):
        worker_session.poll(store)
        worker_session.poll(store)

    assert store.get_work_order(wo["id"])["bg_id"] == wo["bg_id"]
    assert (tree / "notes.md").exists()
    kept = [e for e in store.list_events(wo["id"])
            if e["kind"] == "background_session_kept"]
    assert len(kept) == 1
    assert "uncommitted changes" in json.loads(kept[0]["payload"])["refusal"]
    assert len([r for r in caplog.records if "kept" in r.getMessage()]) == 1
    # …and it is surfaced for as long as it lasts
    assert [v.invariant for v in invariants.check_closed_session_removed(store)] == [
        "INV-SESSION-KEPT"]


@pytest.mark.parametrize("status,removed", [("completed", True), ("cancelled", False)])
def test_a_consumed_verdict_does_not_keep_an_investigator_session(
        bg_fleet, fake_claude, settle_turns, status, removed):
    """verdict.json was read by `jarvis investigate verdict`; a cancelled one maybe never was (wo-b6e2ddda)"""
    store = bg_fleet["store"]
    wo, tree = _worktree_bg_wo(bg_fleet, settle_turns, {"verdict.json": "{}"})
    store.conn.execute("UPDATE work_orders SET kind='investigator' WHERE id=?", (wo["id"],))
    store.conn.commit()
    store.set_status(wo["id"], status)

    worker_session.poll(store)

    assert (store.get_work_order(wo["id"])["bg_id"] is None) is removed
    assert (tree / "verdict.json").exists() is not removed


def test_read_only_directories_are_made_removable(tmp_path):
    """A read-only __pycache__ failed the removal half-way with EACCES (wo-b6e2ddda)"""
    from jarvis import disk

    locked = tmp_path / "pkg" / "__pycache__"
    locked.mkdir(parents=True)
    (locked / "m.pyc").write_text("x")
    locked.chmod(0o555)

    assert disk.make_dirs_writable(tmp_path) == 1
    shutil.rmtree(tmp_path / "pkg")


def test_a_tree_only_a_person_may_delete_names_the_command():
    refusal = ("claude rm 267c11ef... failed (rc=1): kept 267c11ef — its worktree is "
               "still at “/w/.claude/worktrees/wo-x”\n  Worktree not removed. Refused "
               "before anything was deleted; remove it by hand.")
    assert "`rm -rf /w/.claude/worktrees/wo-x`" in invariants._session_kept_ask(refusal)


def test_a_failed_order_keeps_its_session(bg_fleet, fake_claude, settle_turns):
    """`wo retry` relaunches a failed order in its own session"""
    store = bg_fleet["store"]
    wo = _settled_bg_wo(bg_fleet, settle_turns)
    store.set_status(wo["id"], "failed")

    worker_session.poll(store)

    assert wo["session_id"] in claude_cli.bg_roster()
    assert store.get_work_order(wo["id"])["bg_id"] == wo["bg_id"]


def test_cancelling_a_running_order_removes_its_session(bg_fleet, fake_claude):
    store, project = bg_fleet["store"], bg_fleet["project"]
    gate = fake_claude.hold_turns()
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")
    sid = store.get_work_order(wo["id"])["session_id"]

    ops.cancel(wo["id"])
    gate.unlink()
    worker_session.poll(store)

    assert sid not in claude_cli.bg_roster()
    assert store.get_work_order(wo["id"])["bg_id"] is None


def test_a_session_removed_by_hand_is_forgotten(bg_fleet, fake_claude, settle_turns):
    store = bg_fleet["store"]
    wo = _settled_bg_wo(bg_fleet, settle_turns)
    assert claude_cli.remove_bg(wo["bg_id"]) is not None
    store.set_status(wo["id"], "completed")

    worker_session.poll(store)

    assert store.get_work_order(wo["id"])["bg_id"] is None


def test_a_revived_order_resumes_its_removed_session(bg_fleet, fake_claude, settle_turns):
    """`claude rm` keeps the transcript: a bare resume brings the SAME id back (measured on 2.1.308)"""
    store, project = bg_fleet["store"], bg_fleet["project"]
    wo = _settled_bg_wo(bg_fleet, settle_turns)
    store.set_status(wo["id"], "completed")
    worker_session.poll(store)
    store.set_status(wo["id"], "idle")  # `revive_feature_manager`

    worker_session.send(store, project, store.get_work_order(wo["id"]), "one more")
    assert settle_turns(store)

    fresh = store.get_work_order(wo["id"])
    assert fresh["session_id"] == wo["session_id"] and fresh["bg_id"] == wo["bg_id"]
    assert fake_claude.turns[wo["session_id"]] == ["go", "one more"]


def test_a_running_turn_past_its_budget_is_stopped(bg_fleet, fake_claude, monkeypatch):
    """`--max-budget-usd` is print-only (fact 13): the poll enforces the cap"""
    from jarvis import budget

    store, project = bg_fleet["store"], bg_fleet["project"]
    gate = fake_claude.hold_turns()
    wo = _bg_wo(bg_fleet)
    store.update_work_order(wo["id"], budget_usd=1.0)
    worker_session.start(store, project, store.get_work_order(wo["id"]), "go")
    monkeypatch.setattr(budget, "in_flight", lambda store, wo_id: 5.0)

    settled = worker_session.poll(store)

    gate.unlink()
    assert [t["state"] for t in settled] == ["failed"]
    assert any(e["kind"] == "turn_budget_exhausted"
               for e in store.list_events(wo["id"], limit=50))


def test_settling_waits_for_the_process_so_an_immediate_resume_cannot_fork(
        bg_fleet, fake_claude, settle_turns, monkeypatch):
    """The roster drops the pid before the process exits (fact 8): a bare stop let a resume fork"""
    store, project = bg_fleet["store"], bg_fleet["project"]
    waited, bare = [], []
    real_stop_bg = claude_cli.stop_bg
    monkeypatch.setattr(claude_cli, "stop_bg",
                        lambda short, wait=claude_cli.BG_STOP_WAIT_SECONDS:
                        waited.append(short) or real_stop_bg(short, wait))
    monkeypatch.setattr(claude_cli, "stop_session",
                        lambda bg_id: bare.append(bg_id) or True)
    wo = _bg_wo(bg_fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)

    bg_id = store.get_work_order(wo["id"])["bg_id"]
    assert bg_id in waited
    assert bg_id not in bare
