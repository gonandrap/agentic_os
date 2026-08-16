"""The conversation layer itself: minting, launching, reaping and killing turns.

`test_pipeline` covers what a turn means for a work order. This covers the layer under
it — the part that would be swapped out if the transport ever changed again.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from jarvis import claude_cli, ops, worker_session
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def fleet(jarvis_home, fake_claude, catalog_file, project):
    """A started OS plus the pieces the layer is called with directly."""
    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    return {
        "daemon": Daemon(catalog),
        "project": catalog.projects[0],
        "store": ProjectStore(project),
        "path": project,
    }


def _wo(fleet, **kwargs) -> dict:
    wo = ops.create_work_order("proj_a", kwargs.pop("title", "task"), **kwargs)
    return fleet["store"].get_work_order(wo["id"])


# -- session identity ------------------------------------------------------------------


def test_start_mints_a_uuid_and_records_the_turn(fleet, settle_turns):
    wo = _wo(fleet)
    turn = worker_session.start(fleet["store"], fleet["project"], wo, "go")

    fresh = fleet["store"].get_work_order(wo["id"])
    assert uuid.UUID(fresh["session_id"])       # a real uuid: --session-id demands one
    assert fresh["worktree"] == wo["id"]
    assert turn["seq"] == 1 and turn["kind"] == "dispatch"
    assert turn["state"] == "running" and turn["pid"]
    assert settle_turns(fleet["store"])


def test_send_reuses_the_session_and_numbers_the_turns(fleet, fake_claude,
                                                       settle_turns):
    store, project = fleet["store"], fleet["project"]
    wo = _wo(fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)
    sid = store.get_work_order(wo["id"])["session_id"]

    for text in ("second", "third"):
        worker_session.send(store, project, store.get_work_order(wo["id"]), text)
        assert settle_turns(store)

    assert store.get_work_order(wo["id"])["session_id"] == sid
    turns = store.list_turns(wo["id"])
    assert [t["seq"] for t in turns] == [1, 2, 3]
    assert [t["kind"] for t in turns] == ["dispatch", "message", "message"]
    assert fake_claude.turns[sid] == ["go", "second", "third"]


def test_busy_reports_the_in_flight_turn_and_only_that(fleet, fake_claude,
                                                       settle_turns):
    store, project = fleet["store"], fleet["project"]
    gate = fake_claude.hold_turns()
    wo = _wo(fleet)
    worker_session.start(store, project, wo, "go")

    assert worker_session.busy(store, wo["id"])["seq"] == 1
    gate.unlink()
    assert settle_turns(store)
    assert worker_session.busy(store, wo["id"]) is None


# -- reaping ---------------------------------------------------------------------------


def test_a_completed_turn_records_the_reply_exactly_once(fleet, settle_turns):
    store, project = fleet["store"], fleet["project"]
    wo = _wo(fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)

    replies = [m["content"] for m in store.list_messages(wo["id"])
               if m["direction"] == "agent_to_user"]
    assert len(replies) == 1 and replies[0].startswith("final:")
    turn = store.latest_turn(wo["id"])
    assert turn["state"] == "done" and turn["result"] == replies[0]
    assert turn["cost_usd"] == 0.01 and turn["num_turns"] == 1

    # poll is idempotent: a reaped turn is never reaped twice
    for _ in range(3):
        assert worker_session.poll(store) == []
    assert len([m for m in store.list_messages(wo["id"])
                if m["direction"] == "agent_to_user"]) == 1


def test_a_process_that_dies_without_output_fails_the_turn(fleet, fake_claude,
                                                           settle_turns):
    store, project = fleet["store"], fleet["project"]
    fake_claude.turns_fail("fail")
    wo = _wo(fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)

    turn = store.latest_turn(wo["id"])
    assert turn["state"] == "failed"
    assert "test-forced" in turn["error"], turn["error"]  # the stderr tail is kept


def test_an_empty_reply_falls_back_to_what_the_stop_hook_saw(fleet):
    """A turn whose JSON result comes back empty is not a turn with nothing to say:
    the Stop hook fires inside the session and carries the final message verbatim."""
    from jarvis.hooks import handle_hook

    store, project = fleet["store"], fleet["project"]
    wo = _wo(fleet)
    turn = worker_session.start(store, project, wo, "go")
    sid = store.get_work_order(wo["id"])["session_id"]

    handle_hook({"hook_event_name": "Stop", "session_id": sid,
                 "cwd": str(fleet["path"]),
                 "last_assistant_message": "what the hook saw"},
                {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(fleet["path"])})
    # the process finishes, but its result field is blank
    while claude_cli.process_alive(store.latest_turn(wo["id"])["pid"]):
        time.sleep(0.02)
    Path(turn["outfile"]).write_text(json.dumps(
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": sid, "result": ""}))

    worker_session.poll(store)

    replies = [m["content"] for m in store.list_messages(wo["id"])
               if m["direction"] == "agent_to_user"]
    assert replies == ["what the hook saw"]


def test_a_long_running_turn_is_reported_not_killed(fleet, fake_claude, settle_turns):
    """A legitimately long turn and a hung one look identical from out here, and
    killing loses real work — so a stall raises a flag and leaves the process alone."""
    store = fleet["store"]
    gate = fake_claude.hold_turns()
    wo = _wo(fleet)
    fleet["daemon"].tick()                       # dispatched the normal way
    turn = store.latest_turn(wo["id"])
    assert store.get_work_order(wo["id"])["status"] == "running"

    assert not worker_session.is_stalled(turn)
    aged = {**turn, "started_at": time.time() - worker_session.TURN_STALL_SECONDS - 1}
    assert worker_session.is_stalled(aged)

    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                       (aged["started_at"], turn["id"]))
    fleet["daemon"].tick_count = 0
    fleet["daemon"].tick()

    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"]
    assert "cancel" in fresh["attention_reason"]
    assert fresh["status"] == "running"                       # not failed
    assert claude_cli.process_alive(turn["pid"])              # and not killed
    gate.unlink()
    assert settle_turns(store)


# -- cancelling -------------------------------------------------------------------------


def test_cancel_kills_the_process_group(fleet, fake_claude):
    store, project = fleet["store"], fleet["project"]
    fake_claude.hold_turns()
    wo = _wo(fleet)
    turn = worker_session.start(store, project, wo, "go")
    assert claude_cli.process_alive(turn["pid"])

    out = worker_session.cancel(store, wo["id"])

    assert out["stopped"] is True and out["seq"] == 1
    deadline = time.monotonic() + 5
    while claude_cli.process_alive(turn["pid"]) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not claude_cli.process_alive(turn["pid"])
    assert store.latest_turn(wo["id"])["state"] == "failed"


def test_cancel_with_nothing_in_flight_is_a_no_op(fleet, settle_turns):
    store, project = fleet["store"], fleet["project"]
    wo = _wo(fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)

    assert worker_session.cancel(store, wo["id"])["stopped"] is False


# -- liveness --------------------------------------------------------------------------


def test_process_alive_rejects_a_pid_that_is_not_a_claude(fake_claude):
    """Pid reuse over a multi-hour turn would otherwise read as "still running" and
    hang the work order forever, so liveness checks what the pid actually is.

    The pytest process is the perfect adversarial case: it is alive, and it runs from
    `.claude/worktrees/<name>/.venv/bin/python`, so a substring match on "claude" calls
    it a live worker turn. Matching whole path components is what stops that.
    """
    import os

    assert claude_cli.process_alive(None) is False
    assert claude_cli.process_alive(999_999_999) is False
    assert claude_cli.process_alive(os.getpid()) is False


def test_a_finished_turn_is_not_reported_alive_as_a_zombie(fleet):
    """Nothing waits on a detached turn, so on exit it becomes a **zombie** — and
    signalling a zombie succeeds. `os.kill` alone would therefore report every finished
    turn as still running, forever, and no work order would ever settle.
    """
    import os

    store, project = fleet["store"], fleet["project"]
    wo = _wo(fleet)
    turn = worker_session.start(store, project, wo, "go")

    # wait for the process to finish WITHOUT reaping it — that is what makes a zombie
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if Path(turn["outfile"]).exists() and Path(turn["outfile"]).read_text().strip():
            break
        time.sleep(0.02)
    time.sleep(0.2)
    os.kill(turn["pid"], 0)  # still signallable: this is the trap

    assert claude_cli.process_alive(turn["pid"]) is False


def test_a_just_launched_turn_is_never_reported_dead(fleet, fake_claude, settle_turns):
    """The mirror of the zombie case, and the one CI caught.

    Between fork and exec a child's `/proc` cmdline is still the PARENT's, so judging
    liveness from the cmdline declares a turn dead the instant it is launched — and
    `poll` then reaps it before it has written anything, failing a perfectly healthy
    turn. Locally the window closed too fast to notice; CI lost the race every run.
    """
    store, project = fleet["store"], fleet["project"]
    gate = fake_claude.hold_turns()
    for _ in range(8):
        wo = _wo(fleet)
        turn = worker_session.start(store, project, wo, "go")
        assert claude_cli.process_alive(turn["pid"]), "reaped a turn that just started"
        assert worker_session.poll(store) == []
    gate.unlink()
    assert settle_turns(store)


# -- migration off background sessions ---------------------------------------------------


def test_a_work_order_in_flight_at_upgrade_is_surfaced_not_failed(fleet):
    """The deploy-day case: work orders that were already running when this release
    landed have a conversation but no turn on record.

    They must not be settled from that absence. Their background agent is still the
    thing driving them and this reconciler cannot see it, so failing them would be a
    lie about work that may be perfectly fine — and on the live fleet it would have
    hit every in-flight work order within five minutes of the restart.
    """
    store = fleet["store"]
    wo = _wo(fleet)
    store.update_work_order(wo["id"], session_id="a-session-from-before",
                            job_id="bg-42")
    store.set_status(wo["id"], "running")
    store.conn.execute("UPDATE work_orders SET updated_at=? WHERE id=?",
                       (time.time() - 3600, wo["id"]))

    fleet["daemon"].settle_work_order(fleet["project"], store,
                                      store.get_work_order(wo["id"]))

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "running", "an in-flight work order was failed on upgrade"
    assert fresh["needs_attention"]
    assert "message to resume" in fresh["attention_reason"]
    assert "pre_turn_carryover" in [e["kind"] for e in store.list_events(wo["id"])]


def test_a_work_order_claimed_but_never_launched_still_fails(fleet):
    """The other side of that branch, and why it exists: no session and no turn means
    the daemon died between claiming the work order and spawning anything. Nothing is
    running, and nothing ever will be."""
    store = fleet["store"]
    wo = _wo(fleet)
    store.set_status(wo["id"], "dispatching")
    store.conn.execute("UPDATE work_orders SET updated_at=? WHERE id=?",
                       (time.time() - 3600, wo["id"]))

    fleet["daemon"].settle_work_order(fleet["project"], store,
                                      store.get_work_order(wo["id"]))

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "failed"
    assert fresh["attention_reason"] == "worker turn never started"


def test_a_legacy_bg_session_is_released_before_its_next_turn(fleet, fake_claude,
                                                              settle_turns):
    """Work orders dispatched under the old transport migrate themselves.

    `--resume` refuses a session a background agent still holds, so the first message
    delivered to a legacy work order releases the agent and moves the conversation onto
    headless turns for good — which is also what drains the leaked roster as it goes.
    """
    store, project = fleet["store"], fleet["project"]
    wo = _wo(fleet)
    claude_cli.spawn_background(prompt="legacy", cwd=fleet["path"],
                                name=f"[WO {wo['id']}] legacy")
    sess = fake_claude.sessions[-1]
    store.update_work_order(wo["id"], session_id=sess["sessionId"], job_id=sess["id"],
                            worktree=wo["id"])
    store.set_status(wo["id"], "running")

    worker_session.send(store, project, store.get_work_order(wo["id"]), "carry on")
    assert settle_turns(store)

    assert fake_claude.sessions == [], "the background agent was not released"
    assert "session_released" in [e["kind"] for e in store.list_events(wo["id"])]
    assert fake_claude.turns[sess["sessionId"]] == ["carry on"]
    # the conversation continues under the SAME id it always had
    assert store.get_work_order(wo["id"])["session_id"] == sess["sessionId"]
    # and the roster is never consulted again for this work order
    assert store.get_work_order(wo["id"])["job_id"] is None


# -- the context bound reaches the CLI, from the catalog, on every turn ------------------


def _autocompact_of(argv: list[str]) -> str | None:
    return argv[argv.index("--autocompact") + 1] if "--autocompact" in argv else None


def test_every_turn_carries_the_projects_context_bound(fleet, fake_claude,
                                                       settle_turns):
    """Dispatch AND the resumed turns after it must both pass `--autocompact`.

    The bound is what keeps a worker's context — and therefore the cache-read charge on
    every API call it makes — from growing without limit. `briefing_for` is rebuilt per
    turn precisely so a resume cannot drop it.
    """
    store, project = fleet["store"], fleet["project"]
    wo = _wo(fleet)
    expected = str(project.worker.autocompact_window)
    assert project.worker.autocompact_window, "the fleet default should be a real bound"

    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)
    worker_session.send(store, project, store.get_work_order(wo["id"]), "carry on")
    assert settle_turns(store)

    windows = [_autocompact_of(c["argv"]) for c in fake_claude.calls if "-p" in c["argv"]]
    assert windows and all(w == expected for w in windows), (
        f"a turn was launched without the context bound: {windows}")


def test_lowering_the_bound_reaches_a_running_work_order(fleet, fake_claude,
                                                         settle_turns):
    """The bound is read from the CATALOG each turn, not frozen onto the row.

    Model, effort and permission mode ARE frozen at dispatch, so this is a deliberate
    difference: a spend control that only applied to work orders dispatched afterwards
    would miss the long-running ones that are the reason to tighten it.
    """
    store, project = fleet["store"], fleet["project"]
    wo = _wo(fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)

    project.worker.autocompact_window = 100_000  # the user tightens it mid-flight
    worker_session.send(store, project, store.get_work_order(wo["id"]), "carry on")
    assert settle_turns(store)

    assert _autocompact_of(fake_claude.calls[-1]["argv"]) == "100000"


def test_opting_out_emits_no_flag_at_all(fleet, fake_claude, settle_turns):
    """A project that sets the window to null gets the CLI's own default back."""
    store, project = fleet["store"], fleet["project"]
    project.worker.autocompact_window = None
    wo = _wo(fleet)

    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)

    assert _autocompact_of(fake_claude.calls[-1]["argv"]) is None


def test_workers_buy_the_five_minute_cache_not_the_one_hour_one(fleet, settle_turns):
    """A cache WRITE costs 1.25x base input at the 5-minute TTL and 2x at the 1-hour one.

    Claude Code opts a headless session into the 1h TTL by default. Measured across
    every transcript on the machine, 98.1% of cache-READ tokens land within five
    minutes of the previous call, so the longer window is bought and almost never
    used — and the gap it exists to cover (a turn boundary) is cold anyway because
    git status moves in the system prompt (kn-625e79f1).

    Asserting the literal, not a constant: this is a cost decision, and it should
    take a deliberate edit and a fresh measurement to reverse.
    """
    store, project = fleet["store"], fleet["project"]
    wo = _wo(fleet)
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)

    settings = json.loads(
        (fleet["path"] / ".jarvis" / "worker-settings" / f"{wo['id']}.json").read_text())
    assert settings["env"]["FORCE_PROMPT_CACHING_5M"] == "1"
