"""A turn may not END on a background task it never collected.

§5 of docs/superpowers/specs/2026-09-29-a-lead-must-not-block-past-its-cache.md, issue
868. This is what makes §4 safe: backgrounding the three long shapes is permitted
BECAUSE ending the turn on one is not — the task dies with the `claude -p` process and
nothing wakes anybody (issue #575).

The evidence is the transcript, through `background.jobs_left_running`: one parser, two
readers — `background.orphaned_in_turn` at reap and this hook at Stop. The rows here are
the shape of a real one, as in tests/test_background_orphan.py.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jarvis import gates, hooks, ops
from jarvis.hooks import handle_hook
from jarvis.invariants import TERMINAL_STATUSES
from jarvis.project_store import ProjectStore

ALL_GATES = gates.GateConfig(enabled=frozenset(gates.KIND_NAMES))
SUITE = "uv run pytest tests/ -q"
SESSION = "sess-stop"
EVENT = "background_task_uncollected"

#: How far into the past the fixture's turn starts. The guard bounds its scan with
#: `until=time.time()`, so a turn stamped `now` puts the rows these tests write ON that
#: bound: on CI the guard ran under 2 ms after `create_turn` and `+0.002` fell past
#: `until`, so a collected job read as uncollected. Seconds of slack, not milliseconds.
BACK_DATED = 60.0


def _stamp(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).isoformat().replace("+00:00", "Z")


def _launch(when: float, *, job_id: str = "b51fl7bhe", name: str = "Bash",
            payload: dict | None = None, reported: bool = True) -> list[dict]:
    call_id = f"toolu_{job_id}"
    result: dict = {"stdout": "", "stderr": "", "interrupted": False}
    if reported:
        result["backgroundTaskId"] = job_id
    return [
        {"type": "assistant", "timestamp": _stamp(when),
         "message": {"model": "claude-opus-5", "role": "assistant", "content": [
             {"type": "tool_use", "id": call_id, "name": name,
              "input": {**(payload or {"command": SUITE}),
                        "run_in_background": True}}]}},
        {"type": "user", "timestamp": _stamp(when + 0.0001),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": call_id,
              "content": f"Running in background with ID: {job_id}."}]},
         "toolUseResult": result},
    ]


def _poll(when: float, *, tool: str = "BashOutput", key: str = "bash_id",
          job_id: str = "b51fl7bhe", status: str = "completed") -> list[dict]:
    return [
        {"type": "assistant", "timestamp": _stamp(when),
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": f"toolu_p{when}", "name": tool,
              "input": {key: job_id}}]}},
        {"type": "user", "timestamp": _stamp(when + 0.0001),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": f"toolu_p{when}",
              "content": f"<status>{status}</status>\n42 passed"}]}},
    ]


def _refused(when: float, *, call_id: str = "toolu_denied",
             command: str = "./server.sh") -> list[dict]:
    """A launch a PreToolUse hook REFUSED: `is_error` on the result, and `toolUseResult`
    a string starting with the harness's `Error: ` (real shape, a denied Bash call)."""
    return [
        {"type": "assistant", "timestamp": _stamp(when),
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": call_id, "name": "Bash",
              "input": {"command": command, "run_in_background": True}}]}},
        {"type": "user", "timestamp": _stamp(when + 0.0001),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": call_id, "is_error": True,
              "content": "Error: backgrounding is refused by default"}]},
         "toolUseResult": "Error: backgrounding is refused by default"},
    ]


@pytest.fixture()
def worker(jarvis_home, fake_claude, catalog_file, project, tmp_path):
    """A running work order mid-turn, with a transcript this test writes by hand."""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    wo = store.create_work_order("run the suite")
    store.set_status(wo["id"], "running")
    turn = store.create_turn(wo["id"], kind="message", prompt="go")
    # Back-dated because the guard scans up to `until=time.time()`: see BACK_DATED.
    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                       (time.time() - BACK_DATED, turn["id"]))
    store.conn.commit()
    env = {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT": "proj_a",
           "JARVIS_PROJECT_PATH": str(project), "JARVIS_GATES": ALL_GATES.to_json()}

    class Handle:
        def __init__(self):
            self.store, self.wo_id, self.env = store, wo["id"], env
            self.project = project

        def started(self) -> float:
            return store.latest_turn(wo["id"])["started_at"]

        def transcript(self, rows: list[dict]) -> Path:
            path = tmp_path / f"{SESSION}.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            return path

        def payload(self, rows: list[dict] | None = None, **extra) -> dict:
            out = {"hook_event_name": "Stop", "session_id": SESSION,
                   "cwd": str(project)}
            if rows is not None:
                out["transcript_path"] = str(self.transcript(rows))
            return {**out, **extra}

        def guard(self, rows: list[dict] | None = None, **extra):
            return hooks.uncollected_task_turn_block(
                store, wo["id"], self.payload(rows, **extra), env)

        def stop(self, rows: list[dict] | None = None, **extra):
            return handle_hook(self.payload(rows, **extra), env)

    yield Handle()
    store.close()


def test_fixture_turn_is_back_dated(worker):
    """The fixture's own post-condition: every row these tests write must be in the
    past, because the guard bounds its scan with `until=time.time()`. A turn stamped
    `now` split the `+0.001` launch from the `+0.002` poll on CI (under 2 ms) and read a
    collected job as uncollected."""
    started = worker.started()

    assert time.time() - started >= BACK_DATED - 1
    assert started + 0.001 < started + 0.002 < time.time()


def test_turn_with_uncollected_bash_task_blocked(worker):
    """The clear case, and the one this guard exists for: a background start with no
    collection call at all in the turn."""
    blocked = worker.guard(_launch(worker.started() + 0.001))

    assert blocked["decision"] == "block"
    assert "b51fl7bhe" in blocked["reason"]
    assert "BashOutput" in blocked["reason"] and "TaskOutput" in blocked["reason"]
    assert "3-4 minutes" in blocked["reason"]
    # No `hookSpecificOutput`: the Stop hook has its own shape (`main_hook`).
    assert "hookSpecificOutput" not in blocked


def test_turn_with_uncollected_agent_task_blocked(worker):
    """The user's ruling, on the `Agent` half — Neo question 1047."""
    blocked = worker.guard(_launch(
        worker.started() + 0.001, job_id="task_9c2", name="Agent",
        payload={"subagent_type": "jarvis-implementer", "description": "write it"}))

    assert blocked["decision"] == "block"
    assert "task_9c2" in blocked["reason"]


def test_collected_task_does_not_block(worker):
    """Never block on a task proven finished. Each of the three clears it, and
    `jobs_left_running` already implements each."""
    started = worker.started()
    kill = {"type": "assistant", "timestamp": _stamp(started + 0.002),
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_k", "name": "KillShell",
                 "input": {"shell_id": "b51fl7bhe"}}]}}
    notified = {"type": "queue-operation", "operation": "enqueue",
                "timestamp": _stamp(started + 0.002),
                "content": ("<task-notification>\n<task-id>b51fl7bhe</task-id>\n"
                            "<status>completed</status>\n</task-notification>")}

    for label, rows in (
            ("a poll", [*_launch(started + 0.001), *_poll(started + 0.002)]),
            ("a kill", [*_launch(started + 0.001), kill]),
            ("a notification", [*_launch(started + 0.001), notified])):
        assert worker.guard(rows) is None, label


def test_refused_launch_does_not_block(worker):
    """A launch `background_task_decision` DENIED started no process, so a turn ending
    on it is ending on nothing. Blocking there would park a turn over a refusal the OS
    itself wrote (§5 of
    docs/superpowers/specs/2026-09-29-a-lead-must-not-block-past-its-cache.md)."""
    assert worker.guard(_refused(worker.started() + 0.001)) is None

    # The successful launch beside it is still the finding.
    blocked = worker.guard([*_refused(worker.started() + 0.001),
                            *_launch(worker.started() + 0.002)])
    assert blocked["decision"] == "block"
    assert "toolu_denied" not in blocked["reason"]
    assert "b51fl7bhe" in blocked["reason"]


def test_stop_hook_active_does_not_block_twice(worker):
    """One continuation, not a loop: a lead that ignored the reason once is better
    parked than spun, and a genuinely hung task must not make the turn unendable."""
    rows = _launch(worker.started() + 0.001)
    assert worker.guard(rows)["decision"] == "block"

    assert worker.guard(rows, stop_hook_active=True) is None


def test_terminal_status_does_not_block(worker):
    rows = _launch(worker.started() + 0.001)
    for status in sorted(TERMINAL_STATUSES):
        worker.store.set_status(worker.wo_id, status)
        assert worker.guard(rows) is None, status


def test_missing_transcript_does_not_block(worker, tmp_path):
    """A guard that blocked on missing evidence would trap a worker with no way out."""
    rows = _launch(worker.started() + 0.001)

    assert worker.guard(rows, session_id="") is None
    assert hooks.uncollected_task_turn_block(
        worker.store, worker.wo_id,
        {"session_id": SESSION, "transcript_path": str(tmp_path / "nope.jsonl")},
        worker.env) is None
    assert hooks.uncollected_task_turn_block(
        worker.store, worker.wo_id,
        {"session_id": SESSION, "transcript_path": str(tmp_path)},
        worker.env) is None
    assert hooks.uncollected_task_turn_block(
        worker.store, worker.wo_id, {"session_id": SESSION}, worker.env) is None


def test_held_gate_request_block_still_wins(worker):
    """A gate request unargued is the stricter finding and should be the one the worker
    reads, so this guard sits AFTER `held_request_turn_block` in `handle_hook`."""
    action = gates.classify("gh pr merge 31 --squash", ALL_GATES)
    worker.store.add_approval(worker.wo_id, action.kind, action.command,
                              matched=action.matched, status=gates.AWAITING_CASE)
    assert worker.store.held_approvals(worker.wo_id)

    blocked = worker.stop(_launch(worker.started() + 0.001))

    assert blocked["decision"] == "block"
    assert "gate" in blocked["reason"].lower()
    assert "b51fl7bhe" not in blocked["reason"]


def test_block_writes_a_timeline_event(worker):
    """Nobody reads the worker's transcript: the record has to say why the turn was
    held."""
    blocked = worker.stop(_launch(worker.started() + 0.001))
    assert blocked["decision"] == "block"

    held = [e for e in worker.store.list_events(worker.wo_id) if e["kind"] == EVENT]

    assert len(held) == 1
    payload = json.loads(held[0]["payload"])
    assert payload["session_id"] == SESSION
    assert payload["jobs"] == ["b51fl7bhe"]
    assert time.time() - held[0]["ts"] < 60
