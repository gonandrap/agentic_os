"""A background job a turn left behind, and the promise it makes false.

Issue #575, spec
docs/superpowers/specs/2026-09-22-a-dead-background-job-is-not-a-live-one.md. Three
halves, in the order the OS meets them: the transcript detector (§2), the void on the
message every surface prints (§3), and the note a resume carries (§4).

The transcript rows here are the SHAPE OF A REAL ONE, copied off
`wo-d81fcc15`'s session — the launch, the `toolUseResult.backgroundTaskId` that names
the job, and the `<task-notification>` a worker turn never receives. kn-89230548: a fake
that stands in for an external tool has to differ from it nowhere.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jarvis import background, ops, timeline, usage, worker_session
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

SUITE = "uv run pytest tests/ -q -x -p no:randomly 2>&1 | tail -30"


def _stamp(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _launch(when: float, job_id: str = "b51fl7bhe",
            command: str = SUITE) -> list[dict]:
    """The two rows a background launch writes: the call, and the id it came back with."""
    call_id = f"toolu_{job_id}"
    return [
        {"type": "assistant", "timestamp": _stamp(when),
         "message": {"model": "claude-opus-5", "role": "assistant", "content": [
             {"type": "tool_use", "id": call_id, "name": "Bash",
              "input": {"command": command, "description": "Run the suite",
                        "run_in_background": True}}]}},
        {"type": "user", "timestamp": _stamp(when + 0.1),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": call_id,
              "content": f"Command running in background with ID: {job_id}."}]},
         "toolUseResult": {"stdout": "", "stderr": "", "interrupted": False,
                           "backgroundTaskId": job_id}},
    ]


def _notification(when: float, job_id: str = "b51fl7bhe",
                  status: str = "completed") -> dict:
    return {"type": "queue-operation", "operation": "enqueue",
            "timestamp": _stamp(when),
            "content": (f"<task-notification>\n<task-id>{job_id}</task-id>\n"
                        f"<status>{status}</status>\n</task-notification>")}


def _transcript(root: Path, session_id: str, rows: list[dict]) -> Path:
    directory = root / "-a-worktree"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


@pytest.fixture()
def root(tmp_path, monkeypatch) -> Path:
    where = tmp_path / "orphan-transcripts"
    where.mkdir()
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(where))
    return where


# -- 1. the detector -------------------------------------------------------------------


def test_a_job_the_turn_never_collected_is_orphaned(root):
    """The reproduction case, row for row: launched, id recorded, turn over."""
    now = time.time()
    _transcript(root, "sess-1", _launch(now))

    jobs = background.jobs_left_running("sess-1", since=now - 1, until=now + 60)

    assert [(j.job_id, j.command) for j in jobs] == [("b51fl7bhe", SUITE)]
    assert "`b51fl7bhe`" in jobs[0].label()
    assert "uv run pytest" in jobs[0].label()


def test_a_notification_that_the_job_ended_clears_it(root):
    """What an INTERACTIVE session gets and a `claude -p` worker never does. Present,
    the job was collected and nobody is being accused of anything."""
    now = time.time()
    _transcript(root, "sess-1", [*_launch(now), _notification(now + 1)])

    assert background.jobs_left_running("sess-1", since=now - 1,
                                        until=now + 60) == []


def test_a_notification_that_it_is_still_running_does_not(root):
    now = time.time()
    _transcript(root, "sess-1",
                [*_launch(now), _notification(now + 1, status="running")])

    assert [j.job_id for j in background.jobs_left_running(
        "sess-1", since=now - 1, until=now + 60)] == ["b51fl7bhe"]


def test_killing_the_job_is_collecting_it(root):
    """The worker knew the job was there and dealt with it. Nothing was abandoned and
    nothing it said about the job can be a promise."""
    now = time.time()
    kill = {"type": "assistant", "timestamp": _stamp(now + 1),
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_k", "name": "KillShell",
                 "input": {"shell_id": "b51fl7bhe"}}]}}
    _transcript(root, "sess-1", [*_launch(now), kill])

    assert background.jobs_left_running("sess-1", since=now - 1,
                                        until=now + 60) == []


def test_a_poll_reporting_the_job_finished_clears_it(root):
    """`BashOutput` said the job was over before the turn was. No transcript in the
    fleet has ever carried one — which is why this bug reached production — so the
    status is read tolerantly (`background.RUNNING`)."""
    now = time.time()
    poll = [
        {"type": "assistant", "timestamp": _stamp(now + 1),
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_p", "name": "BashOutput",
              "input": {"bash_id": "b51fl7bhe"}}]}},
        {"type": "user", "timestamp": _stamp(now + 1.1),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_p",
              "content": "<status>completed</status>\n42 passed"}]}},
    ]
    _transcript(root, "sess-1", [*_launch(now), *poll])

    assert background.jobs_left_running("sess-1", since=now - 1,
                                        until=now + 60) == []


def test_a_poll_that_finds_it_still_running_does_not_clear_it(root):
    now = time.time()
    poll = [
        {"type": "assistant", "timestamp": _stamp(now + 1),
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_p", "name": "BashOutput",
              "input": {"bash_id": "b51fl7bhe"}}]}},
        {"type": "user", "timestamp": _stamp(now + 1.1),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_p",
              "content": "<status>running</status>"}]}},
    ]
    _transcript(root, "sess-1", [*_launch(now), *poll])

    assert [j.job_id for j in background.jobs_left_running(
        "sess-1", since=now - 1, until=now + 60)] == ["b51fl7bhe"]


def test_a_previous_turns_job_belongs_to_that_turn(root):
    """`said_in_session`'s rule one reader along: a session outlives the turn that
    stalled in it, and a job the LAST turn abandoned is already on the record."""
    now = time.time()
    _transcript(root, "sess-1", _launch(now - 3600))

    assert background.jobs_left_running("sess-1", since=now - 1,
                                        until=now + 60) == []


def test_a_foreground_command_is_not_a_job(root):
    """The ordinary case, and the one that must never raise a flag: everything the
    fleet runs is in the foreground."""
    now = time.time()
    _transcript(root, "sess-1", [
        {"type": "assistant", "timestamp": _stamp(now),
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_f", "name": "Bash",
              "input": {"command": "uv run pytest tests/"}}]}}])

    assert background.jobs_left_running("sess-1", since=now - 1,
                                        until=now + 60) == []


def test_no_transcript_is_not_a_finding(root):
    assert background.jobs_left_running("nobody", since=0, until=time.time()) == []


# -- 2. the detector against a work order ----------------------------------------------


@pytest.fixture()
def fleet(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    return {"daemon": Daemon(catalog), "project": catalog.projects[0],
            "store": ProjectStore(project)}


def _turn_that_ended(store, wo_id: str, started: float) -> dict:
    """A settled turn row, without the transport: the detector is what is under test."""
    turn = store.create_turn(wo_id, kind="message", prompt="go")
    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                       (started, turn["id"]))
    return store.finish_turn(turn["id"], "done", result="still running in the background")


def test_the_reaper_files_the_finding_against_the_reply(fleet, root):
    """§2: the event names the turn, the jobs and the MESSAGE the promise is in."""
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    store.update_work_order(wo["id"], session_id="sess-1")
    started = time.time() - 10
    _transcript(root, "sess-1", _launch(started + 1))
    turn = _turn_that_ended(store, wo["id"], started)
    msg_id = store.record_agent_reply(wo["id"], "Suite is running in the background.")

    jobs = background.orphaned_in_turn(store, wo["id"], turn)
    background.record(store, wo["id"], turn, jobs, msg_id)

    event = store.events_of_kind(wo["id"], background.EVENT)[-1]
    payload = json.loads(event["payload"])
    assert payload["msg_id"] == msg_id and payload["seq"] == turn["seq"]
    assert payload["jobs"] == [{"id": "b51fl7bhe", "command": SUITE}]


def test_a_turn_that_finished_is_not_judged(fleet, root):
    """The exemption, §2: `jarvis wo finish` IS the authoritative last word, and a job
    collected on the way to it must not be reported as abandoned."""
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    store.update_work_order(wo["id"], session_id="sess-1")
    started = time.time() - 10
    _transcript(root, "sess-1", _launch(started + 1))
    turn = _turn_that_ended(store, wo["id"], started)
    store.add_event(wo["id"], "finished", {"summary": "done"})

    assert background.orphaned_in_turn(store, wo["id"], turn) == []


def test_a_work_order_with_no_session_is_not_judged(fleet):
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    turn = _turn_that_ended(store, wo["id"], time.time() - 10)

    assert background.orphaned_in_turn(store, wo["id"], turn) == []


# -- 3. the void on every surface that prints the message ------------------------------


def _voided_conversation(store, wo_id: str) -> list[dict]:
    return timeline.build_conversation(store.list_events(wo_id),
                                       store.list_messages(wo_id))


def test_the_promise_is_marked_void_in_the_conversation(fleet, root):
    """§3. One derivation, so `jarvis wo show`, the dashboard page and the conversation
    the supervisor judges from all carry it."""
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    store.update_work_order(wo["id"], session_id="sess-1")
    started = time.time() - 10
    _transcript(root, "sess-1", _launch(started + 1))
    turn = _turn_that_ended(store, wo["id"], started)
    msg_id = store.record_agent_reply(
        wo["id"], "Suite is running in the background; I'll report when it lands.")
    background.record(store, wo["id"], turn,
                      background.orphaned_in_turn(store, wo["id"], turn), msg_id)

    said = [t for t in _voided_conversation(store, wo["id"]) if t["msg_id"] == msg_id]

    assert len(said) == 1
    assert "VOID" in said[0]["void"] and "b51fl7bhe" in said[0]["void"]
    # The words themselves are untouched: the record is what the worker SAID.
    assert said[0]["content"].startswith("Suite is running")
    # And the void is printed above them — `cli._readable_conversation`'s ordering.
    assert list(said[0]).index("void") < list(said[0]).index("content")


def test_every_other_message_carries_an_empty_void(fleet):
    """A key that comes and goes is one every consumer has to guard."""
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    store.record_agent_reply(wo["id"], "all good")

    assert [t["void"] for t in _voided_conversation(store, wo["id"])] == [""]


def test_wo_show_prints_the_void_above_the_words_and_nothing_elsewhere(
        jarvis_home, catalog_file, project, capsys):
    """The surface the issue is about. Every OTHER message must stay clean: an empty
    `void:` over each one is what would make the one that matters invisible."""
    from jarvis import cli

    ops.start_os(str(catalog_file), foreground=True)
    wo = ops.create_work_order("proj_a", "long suite")
    store = ProjectStore(project)
    try:
        store.record_agent_reply(wo["id"], "first, an ordinary report")
        msg_id = store.record_agent_reply(
            wo["id"], "Suite is running in the background; I'll report when it lands.")
        store.add_event(wo["id"], background.EVENT,
                        {"seq": 1, "msg_id": msg_id,
                         "jobs": [{"id": "b51fl7bhe", "command": SUITE}]})
    finally:
        store.close()

    assert cli.main(["wo", "show", wo["id"]]) == 0
    out = capsys.readouterr().out

    assert out.count("void:") == 1
    assert "b51fl7bhe" in out
    assert out.index("void:") < out.index("I'll report when it lands")


def test_the_timeline_says_what_the_detector_found(fleet):
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    store.add_event(wo["id"], background.EVENT,
                    {"seq": 1, "msg_id": None,
                     "jobs": [{"id": "b51fl7bhe", "command": SUITE}]})

    entry = [e for e in timeline.build_timeline(store.get_work_order(wo["id"]),
                                                store.list_events(wo["id"]), [])
             if "background job" in e["label"]]

    assert entry and "b51fl7bhe" in entry[0]["detail"]


def test_the_two_modules_say_the_same_thing(fleet):
    """`timeline` is a leaf that imports nothing, so it spells the constants out. This
    is the pin that keeps the two copies equal."""
    assert timeline.BACKGROUND_ORPHANED == background.EVENT
    assert timeline.BACKGROUND_NUDGED == background.NUDGED
    assert timeline.BACKGROUND_UNRESOLVED == background.UNRESOLVED
    assert background.SOURCE in timeline.UNAUTHORED_SOURCES
    assert timeline.JOB_COMMAND_CHARS == background.COMMAND_CHARS
    job = {"id": "b51fl7bhe", "command": SUITE}
    assert timeline._job_labels({"jobs": [job]}) == background.labels(
        background.jobs_of({"jobs": [job]}))


# -- 4. the resume that differs from the attempt ---------------------------------------


def _orphaned(store, wo_id: str) -> dict:
    turn = store.create_turn(wo_id, kind="message", prompt="go")
    store.finish_turn(turn["id"], "done", result="running in the background")
    store.add_event(wo_id, background.EVENT,
                    {"seq": turn["seq"], "msg_id": None,
                     "jobs": [{"id": "b51fl7bhe", "command": SUITE}]})
    return store.get_work_order(wo_id)


def test_a_resume_carries_the_dead_job_into_the_next_turn(fleet, fake_claude,
                                                          settle_turns):
    """§4, and the whole point of it: the retry is told what went wrong, so it is not
    the identical turn shape that looped twice on wo-d81fcc15."""
    store, daemon = fleet["store"], fleet["daemon"]
    wo = ops.create_work_order("proj_a", "task")
    worker_session.start(store, fleet["project"], store.get_work_order(wo["id"]), "go")
    assert settle_turns(store)
    store.add_event(wo["id"], background.EVENT,
                    {"seq": store.latest_turn(wo["id"])["seq"], "msg_id": None,
                     "jobs": [{"id": "b51fl7bhe", "command": SUITE}]})
    ops.send_message(wo["id"], "resume")

    daemon.deliver_messages(fleet["project"], store)
    assert settle_turns(store)

    note = [m for m in store.list_messages(wo["id"])
            if m["source"] == background.SOURCE]
    assert len(note) == 1
    assert "b51fl7bhe" in note[0]["content"] and "FOREGROUND" in note[0]["content"]
    assert note[0]["status"] == "delivered"
    # It reaches the worker in the SAME turn as the user's words, ahead of them.
    prompt = store.latest_turn(wo["id"])["prompt"]
    assert prompt.index("b51fl7bhe") < prompt.index("resume")
    # And it is Jarvis speaking, never the user (timeline.UNAUTHORED_SOURCES).
    said = [t for t in _voided_conversation(store, wo["id"])
            if t["msg_id"] == note[0]["id"]]
    assert said[0]["who"] == "jarvis → worker"


def test_the_turn_is_still_charged_to_the_users_message(fleet, fake_claude,
                                                        settle_turns):
    """The cost of a turn belongs to the ASK. The note is the OS talking about the last
    turn, not a new one being asked for."""
    store, daemon = fleet["store"], fleet["daemon"]
    wo = ops.create_work_order("proj_a", "task")
    worker_session.start(store, fleet["project"], store.get_work_order(wo["id"]), "go")
    assert settle_turns(store)
    store.add_event(wo["id"], background.EVENT,
                    {"seq": store.latest_turn(wo["id"])["seq"], "msg_id": None,
                     "jobs": [{"id": "b51fl7bhe", "command": SUITE}]})
    msg_id = ops.send_message(wo["id"], "resume")["msg_id"]

    daemon.deliver_messages(fleet["project"], store)

    assert store.latest_turn(wo["id"])["msg_id"] == msg_id


def test_the_note_is_written_once_even_if_delivery_is_retried(fleet):
    """A delivery whose transport failed leaves its messages queued, so the note comes
    back in the next tick's batch. Seeing itself there is the guard."""
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    fresh = _orphaned(store, wo["id"])
    note = background.resume_note(store, fresh)
    assert note

    already = [{"id": 1, "content": note, "source": background.SOURCE}]
    assert background.resume_note(store, fresh, already) == ""


def test_an_ordinary_work_order_gets_no_note(fleet, fake_claude, settle_turns):
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    worker_session.start(store, fleet["project"], store.get_work_order(wo["id"]), "go")
    assert settle_turns(store)

    assert background.resume_note(store, store.get_work_order(wo["id"])) == ""


# -- 5. the OS typing `resume` itself, twice -------------------------------------------


def _settled(fleet, wo_id: str) -> dict:
    fleet["daemon"].settle_work_order(fleet["project"], fleet["store"],
                                      fleet["store"].get_work_order(wo_id))
    return fleet["store"].get_work_order(wo_id)


def test_the_os_sends_it_back_before_asking_the_user(fleet):
    """§4b. A stall with a known cure and a worker still holding the conversation is
    one the OS clears itself — making the user type `resume` is the bug, not the fix."""
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    _orphaned(store, wo["id"])

    fresh = _settled(fleet, wo["id"])

    queued = store.queued_messages(wo["id"])
    assert [m["source"] for m in queued] == [background.SOURCE]
    assert "b51fl7bhe" in queued[0]["content"]
    assert fresh["status"] != "needs_review", "parked on a stall it was about to clear"
    assert not fresh["needs_attention"]
    nudge = json.loads(store.events_of_kind(wo["id"], background.NUDGED)[0]["payload"])
    assert nudge["attempt"] == 1 and nudge["of"] == background.NUDGE_MAX


def test_a_waiting_nudge_is_never_sent_twice(fleet):
    """The reconciler is back in two minutes and the message is still queued."""
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    _orphaned(store, wo["id"])
    _settled(fleet, wo["id"])

    _settled(fleet, wo["id"])
    _settled(fleet, wo["id"])

    assert len(store.queued_messages(wo["id"])) == 1
    assert len(store.events_of_kind(wo["id"], background.NUDGED)) == 1


def test_a_worker_that_backgrounds_again_after_two_tries_reaches_the_user(fleet):
    """The cap, and what is deliberately NOT written with it: no attention reason of
    this module's own — `true_blockers` stays the one author of what a parked work
    order says, and #573 owns that sentence."""
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    for _ in range(background.NUDGE_MAX):
        _orphaned(store, wo["id"])
        assert _settled(fleet, wo["id"])["status"] != "needs_review"
        for msg in store.queued_messages(wo["id"]):
            store.mark_message(msg["id"], "delivered")
    _orphaned(store, wo["id"])

    fresh = _settled(fleet, wo["id"])

    assert fresh["status"] == "needs_review" and fresh["needs_attention"]
    assert store.queued_messages(wo["id"]) == []
    gave_up = store.events_of_kind(wo["id"], background.UNRESOLVED)
    assert len(gave_up) == 1
    assert json.loads(gave_up[0]["payload"])["attempts"] == background.NUDGE_MAX
    # Said once: the finding still matches the latest turn on every later tick.
    _settled(fleet, wo["id"])
    assert len(store.events_of_kind(wo["id"], background.UNRESOLVED)) == 1


def test_an_ordinary_idle_turn_still_parks(fleet):
    """The branch this sits in front of, unchanged for everything else."""
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    turn = store.create_turn(wo["id"], kind="message", prompt="go")
    store.finish_turn(turn["id"], "done", result="I had a look and stopped")

    fresh = _settled(fleet, wo["id"])

    assert fresh["status"] == "needs_review" and fresh["needs_attention"]
    assert store.queued_messages(wo["id"]) == []


def test_the_note_is_spent_by_the_turn_it_was_written_for(fleet):
    """Keyed on the turn, so nothing has to clear it: once the retry has run, the
    latest turn is no longer the one that abandoned a job."""
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "task")
    fresh = _orphaned(store, wo["id"])
    assert background.resume_note(store, fresh)

    later = store.create_turn(wo["id"], kind="message", prompt="again")
    store.finish_turn(later["id"], "done", result="ran it in the foreground")

    assert background.resume_note(store, store.get_work_order(wo["id"])) == ""
