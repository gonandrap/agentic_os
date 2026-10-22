"""A message reaches a RUNNING lead at its next tool call (docs/superpowers/specs/2026-10-22-messages-delivered-mid-turn.md)"""

from __future__ import annotations

import json

import pytest

from jarvis import hooks, ops
from jarvis.hooks import handle_hook
from jarvis.project_store import ProjectStore, inbox_flag

SESSION = "sess-mid"


@pytest.fixture()
def worker(jarvis_home, fake_claude, catalog_file, project):
    """A work order mid-turn, as its own PostToolUse hook sees it"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    wo = store.create_work_order("long turn")
    store.update_work_order(wo["id"], session_id=SESSION)
    store.set_status(wo["id"], "running")
    store.create_turn(wo["id"], kind="message", prompt="go")
    env = {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT": "proj_a",
           "JARVIS_PROJECT_PATH": str(project)}

    class Handle:
        def __init__(self):
            self.store, self.wo_id, self.env, self.project = store, wo["id"], env, project

        def tool_call(self, **extra):
            return handle_hook({"hook_event_name": "PostToolUse", "session_id": SESSION,
                                "cwd": str(project), "tool_name": "Bash",
                                "tool_input": {"command": "ls"}, **extra}, env)

        def send(self, text, source="jarvis"):
            return ops.send_message(self.wo_id, text, source=source)["msg_id"]

    yield Handle()
    store.close()


def _context(out):
    return out["hookSpecificOutput"]["additionalContext"]


def test_a_queued_message_reaches_the_lead_at_its_next_tool_call(worker):
    msg_id = worker.send("stop refactoring, only fix the bug")

    out = worker.tool_call()

    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "stop refactoring, only fix the bug" in _context(out)
    assert _context(out).startswith(hooks.MID_TURN_HEADER)
    assert worker.store.get_message(msg_id)["status"] == "delivered"
    [event] = [e for e in worker.store.list_events(worker.wo_id)
               if e["kind"] == "message_delivered"]
    assert json.loads(event["payload"]) == {"msg_ids": [msg_id], "turn": 1,
                                            "mid_turn": True}


def test_it_is_delivered_once(worker):
    worker.send("one")
    assert worker.tool_call() is not None

    assert worker.tool_call() is None
    assert not inbox_flag(worker.project, worker.wo_id).exists()


def test_several_messages_go_together_in_order(worker):
    worker.send("first")
    worker.send("second")

    text = _context(worker.tool_call())

    assert text.index("first") < text.index("second")


def test_no_flag_means_no_store_is_opened(worker, monkeypatch):
    """The hot path: every tool call of every worker runs this"""
    opened = []
    monkeypatch.setattr(hooks, "ProjectStore",
                        lambda root: opened.append(root) or ProjectStore(root))
    assert worker.tool_call() is None
    assert opened == []


def test_a_subagent_tool_call_does_not_take_the_leads_message(worker):
    msg_id = worker.send("for the lead")

    assert worker.tool_call(agent_type="general-purpose") is None

    assert worker.store.get_message(msg_id)["status"] == "queued"
    assert "for the lead" in _context(worker.tool_call())


@pytest.mark.parametrize("state", ["idle", "needs_review"])
def test_an_order_not_running_leaves_it_to_the_daemon(worker, state):
    msg_id = worker.send("later")
    worker.store.set_status(worker.wo_id, state)

    assert worker.tool_call() is None
    assert worker.store.get_message(msg_id)["status"] == "queued"
    assert inbox_flag(worker.project, worker.wo_id).exists()


def test_a_settled_turn_leaves_it_to_the_daemon(worker):
    msg_id = worker.send("next turn")
    turn = worker.store.latest_turn(worker.wo_id)
    worker.store.finish_turn(turn["id"], "done")

    assert worker.tool_call() is None
    assert worker.store.get_message(msg_id)["status"] == "queued"


def test_a_compaction_turn_is_not_interrupted(worker):
    turn = worker.store.latest_turn(worker.wo_id)
    worker.store.finish_turn(turn["id"], "done")
    worker.store.create_turn(worker.wo_id, kind="compact", prompt="/compact")
    msg_id = worker.send("after the compaction")

    assert worker.tool_call() is None
    assert worker.store.get_message(msg_id)["status"] == "queued"


def test_a_superseded_session_does_not_take_it(worker):
    msg_id = worker.send("for the current session")

    assert worker.tool_call(session_id="some-older-session") is None
    assert worker.store.get_message(msg_id)["status"] == "queued"


def test_a_relaunch_prompt_is_not_a_mid_turn_message(worker):
    """A queued relaunch is a whole turn's prompt: the daemon sends it when the turn ends"""
    relaunch = worker.store.queue_message(worker.wo_id, "carry on", source="relaunch")
    words = worker.send("the user's words")

    text = _context(worker.tool_call())

    assert "the user's words" in text and "carry on" not in text
    assert worker.store.get_message(relaunch)["status"] == "queued"
    assert worker.store.get_message(words)["status"] == "delivered"


def test_a_message_already_taken_is_not_sent_twice(worker):
    msg_id = worker.send("raced")
    assert worker.store.claim_message(msg_id)

    assert worker.tool_call() is None


def test_an_interactive_session_is_untouched(worker):
    worker.send("x")
    out = handle_hook({"hook_event_name": "PostToolUse", "session_id": "mine",
                       "cwd": str(worker.project), "tool_name": "Bash",
                       "tool_input": {"command": "ls"}}, {})
    assert out is None


def test_the_send_note_promises_mid_turn_delivery(worker):
    out = ops.send_message(worker.wo_id, "hello")
    assert "next tool call" in out["delivery"]
