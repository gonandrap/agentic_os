"""Work order timeline: signal vs debug.

The raw `wo_events` table mixes two very different things — the story of the work
order (what was asked, what was decided, what came back) and the plumbing that
carries it (message delivery, session hooks, turn boundaries). The timeline shows
the story by default; the plumbing is debug and only surfaces on request.
"""

from __future__ import annotations

from jarvis.timeline import build_timeline, event_level

SIGNAL_KINDS = [
    "created", "dispatched", "status", "attention", "assumption",
    "question_asked", "neo_answered", "escalation_answered", "reviewed",
    "finished",
]
DEBUG_KINDS = [
    "message_queued", "delivering", "message_delivered", "turn_ended",
    "session_bound", "permission_mode_changed",
    "hook:SessionStart", "hook:Stop", "hook:SessionEnd", "hook:Notification",
]


def ev(kind, ts=0.0, **payload):
    return {"ts": ts, "kind": kind, "payload": payload}


def test_lifecycle_events_are_signal():
    assert [event_level(k) for k in SIGNAL_KINDS] == ["signal"] * len(SIGNAL_KINDS)


def test_messaging_and_hook_events_are_debug():
    assert [event_level(k) for k in DEBUG_KINDS] == ["debug"] * len(DEBUG_KINDS)


def test_unknown_hook_events_are_debug():
    assert event_level("hook:SomethingNew") == "debug"


def test_unknown_kinds_default_to_signal():
    """Better to show an unclassified event than to silently swallow it."""
    assert event_level("brand_new_kind") == "signal"


def test_debug_events_hidden_by_default():
    events = [ev("created", 1.0, origin="jarvis"), ev("turn_ended", 2.0),
              ev("message_delivered", 3.0, msg_id=1, via="bg-resume")]
    kinds = [e["kind"] for e in build_timeline({}, events, [])]
    assert kinds == ["created"]


def test_debug_events_included_when_asked():
    events = [ev("created", 1.0, origin="jarvis"), ev("turn_ended", 2.0)]
    entries = build_timeline({}, events, [], include_debug=True)
    assert [e["kind"] for e in entries] == ["created", "turn_ended"]
    assert [e["level"] for e in entries] == ["signal", "debug"]


def test_created_entry_says_what_the_work_order_is_about():
    wo = {"title": "Fix the citation exporter", "description": "BibTeX output drops DOIs"}
    entry = build_timeline(wo, [ev("created", 1.0, origin="jarvis")], [])[0]
    assert entry["label"] == "Work order created"
    assert "Fix the citation exporter" in entry["detail"]
    assert "BibTeX output drops DOIs" in entry["detail"]


def test_signal_entries_read_as_prose_not_json():
    events = [
        ev("status", 1.0, status="running"),
        ev("attention", 2.0, reason="Claude needs your permission"),
        ev("assumption", 3.0, content="assuming UTF-8 input"),
        ev("finished", 4.0, summary="exporter fixed"),
    ]
    entries = build_timeline({}, events, [])
    labels = [e["label"] for e in entries]
    assert labels == ["Running", "Needs you", "Assumption recorded", "Finished"]
    assert [e["detail"] for e in entries] == [
        "", "Claude needs your permission", "assuming UTF-8 input", "exporter fixed",
    ]


def test_messages_appear_as_prompt_and_reply():
    messages = [
        {"ts": 2.0, "direction": "user_to_agent", "content": "also cover EndNote",
         "source": "ui"},
        {"ts": 3.0, "direction": "agent_to_user", "content": "done, EndNote covered",
         "source": "worker"},
    ]
    entries = build_timeline({}, [ev("created", 1.0)], messages)
    assert [e["label"] for e in entries] == [
        "Work order created", "You → worker", "Worker → you"]
    assert entries[1]["detail"] == "also cover EndNote"
    assert entries[2]["detail"] == "done, EndNote covered"


def test_entries_are_ordered_by_time():
    events = [ev("finished", 9.0, summary="s"), ev("created", 1.0)]
    messages = [{"ts": 5.0, "direction": "user_to_agent", "content": "hi"}]
    ts = [e["ts"] for e in build_timeline({}, events, messages)]
    assert ts == [1.0, 5.0, 9.0]


def test_message_plumbing_events_never_duplicate_the_message_itself():
    """`delivering`/`message_delivered` are debug; the message content is the signal."""
    events = [ev("message_queued", 1.0, msg_id=1), ev("delivering", 2.0, msg_id=1),
              ev("message_delivered", 3.0, msg_id=1)]
    messages = [{"ts": 1.0, "direction": "user_to_agent", "content": "the ask"}]
    entries = build_timeline({}, events, messages)
    assert len(entries) == 1
    assert entries[0]["detail"] == "the ask"


def test_cli_wo_show_hides_debug_entries_by_default(jarvis_home, fake_claude,
                                                    catalog_file, capsys):
    """`jarvis wo show` speaks the same timeline as the web UI, with --debug to
    reveal the plumbing."""
    import json as _json

    from jarvis import cli, ops

    ops.start_os(str(catalog_file), foreground=True)
    wo = ops.create_work_order("proj_a", "export citations")
    _, path, _ = ops.find_work_order(wo["id"], "proj_a")
    from jarvis.project_store import ProjectStore
    store = ProjectStore(path)
    store.add_event(wo["id"], "turn_ended")
    store.close()

    cli.main(["wo", "show", wo["id"], "--json"])
    plain = _json.loads(capsys.readouterr().out)
    assert [e["kind"] for e in plain["timeline"]] == ["created"]

    cli.main(["wo", "show", wo["id"], "--json", "--debug"])
    debug = _json.loads(capsys.readouterr().out)
    assert "turn_ended" in [e["kind"] for e in debug["timeline"]]
