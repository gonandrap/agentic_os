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
    "question_asked", "reviewed",
    "finished",
    # A turn that failed or was cancelled is the story, not the plumbing: it is why
    # the work order stopped.
    "turn_failed", "turn_cancelled",
    # So is a turn Claude Code refused for the usage limit, and so is the OS putting
    # itself right afterwards — that recovery is the answer to "why did it start again
    # on its own at midnight", which is a question only the timeline can answer.
    "rate_limited", "rate_limit_retry", "rate_limit_exhausted",
    "turn_paused", "turn_resumed", "turn_retries_exhausted",
]
DEBUG_KINDS = [
    "message_queued", "delivering", "message_delivered",
    "turn_started", "turn_ended", "session_released", "permission_mode_changed",
    "hook:SessionStart", "hook:Stop", "hook:SessionEnd", "hook:Notification",
    # An answer is the message it was queued as, not the bookkeeping beside it. Both
    # writers do the two in the same breath, so as signal these cost the reader a line
    # that says an answer arrived, directly above the answer.
    "neo_answered", "escalation_answered",
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
              ev("message_delivered", 3.0, msg_id=1, turn=2)]
    kinds = [e["kind"] for e in build_timeline({}, events, [])]
    assert kinds == ["created"]


def test_debug_events_included_when_asked():
    events = [ev("created", 1.0, origin="jarvis"), ev("turn_ended", 2.0)]
    entries = build_timeline({}, events, [], include_debug=True)
    assert [e["kind"] for e in entries] == ["created", "turn_ended"]
    assert [e["level"] for e in entries] == ["signal", "debug"]


def test_created_entry_does_not_restate_the_work_order():
    """Every surface that renders this timeline puts the title and description at the
    top of the same page, so an opening entry repeating them is the reader's first
    scroll spent on text they have just read."""
    wo = {"title": "Fix the citation exporter", "description": "BibTeX output drops DOIs"}
    entry = build_timeline(wo, [ev("created", 1.0, origin="jarvis")], [])[0]
    assert entry["label"] == "Work order created"
    assert entry["detail"] == ""


def test_signal_entries_read_as_prose_not_json():
    events = [
        ev("status", 1.0, status="running"),
        ev("attention", 2.0, reason="Claude needs your permission"),
        ev("assumption", 3.0, content="assuming UTF-8 input"),
        ev("finished", 4.0, summary="exporter fixed"),
    ]
    entries = build_timeline({}, events, [])
    labels = [e["label"] for e in entries]
    assert labels == ["Running", "Needs you", "Assumption #1 recorded", "Finished"]
    assert [e["detail"] for e in entries] == [
        "", "Claude needs your permission", "", "exporter fixed",
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


# -- rendered details ----------------------------------------------------------------
# Every kind below reads a payload key. A renderer that reads a key no writer stores
# renders an empty detail forever, and asserting only on event_level (as the tests
# above do) cannot see it — that is exactly how the empty "Worker asked a question"
# entry shipped. These tests assert the DETAIL, so a key that goes unwritten fails here.
#
# Second face of the same trap: a detail that renders text the reader already has
# elsewhere on the page. Each test below names the surface it is duplicated from — see
# §7 of docs/superpowers/specs/2026-08-23-the-work-order-record.md.

def test_question_asked_points_at_the_question_instead_of_reprinting_it():
    """Only the question's own record holds it and its answer together."""
    events = [ev("question_asked", 1.0, neo_question_id=7,
                 question="CSV or JSON for the export default?")]
    entry = build_timeline({}, events, [])[0]
    assert entry["label"] == "Worker asked a question"
    assert entry["detail"] == ""
    assert entry["ref"] == {"kind": "neo_question", "id": 7, "label": "question #7"}


def test_a_question_event_with_no_id_falls_back_to_its_text():
    """Nothing to point at, so the text is all there is — an entry saying neither what
    was asked nor where to read it says nothing at all."""
    events = [ev("question_asked", 1.0, question="what was asked")]
    entry = build_timeline({}, events, [])[0]
    assert entry["ref"] is None
    assert entry["detail"] == "what was asked"


def test_most_entries_point_at_nothing():
    """`ref` is present on every entry and set on almost none: a template that reads it
    must not have to ask whether the key is there."""
    entries = build_timeline({}, [ev("created", 1.0), ev("finished", 2.0, summary="s")],
                             [{"ts": 3.0, "direction": "agent_to_user", "content": "x"}])
    assert [e["ref"] for e in entries] == [None, None, None]


def test_assumptions_are_numbered_rather_than_repeated():
    """The page that shows this timeline lists the assumptions themselves, numbered the
    same way — so the text here was the same paragraph twice, feet apart."""
    events = [ev("assumption", 1.0, content="assuming UTF-8 input", n=1),
              ev("assumption", 2.0, content="assuming ISO dates", n=2)]
    entries = build_timeline({}, events, [])
    assert [e["label"] for e in entries] == ["Assumption #1 recorded",
                                             "Assumption #2 recorded"]
    assert [e["detail"] for e in entries] == ["", ""]


def test_assumptions_written_before_they_were_numbered_are_numbered_here():
    """Rows on disk carry no `n`; positional numbering gives the writer's answer."""
    events = [ev("assumption", 1.0, content="first"),
              ev("assumption", 2.0, content="second"),
              ev("assumption", 3.0, content="third", n=3)]
    entries = build_timeline({}, events, [])
    assert [e["label"] for e in entries] == [
        "Assumption #1 recorded", "Assumption #2 recorded", "Assumption #3 recorded"]


def test_neos_answer_is_one_line_and_says_neo_said_it():
    """It used to be two lines, and the survivor was misattributed to the user."""
    events = [ev("question_asked", 1.0, neo_question_id=7),
              ev("neo_answered", 3.0, neo_question_id=7)]
    messages = [{"ts": 4.0, "direction": "user_to_agent", "source": "neo",
                 "content": "[Neo] go with CSV"}]
    entries = build_timeline({}, events, messages)
    assert [(e["kind"], e["label"]) for e in entries] == [
        ("question_asked", "Worker asked a question"),
        ("message", "Neo → worker"),
    ]


def test_the_user_answering_still_reads_as_the_user():
    """Only Neo's own messages are relabelled; everything else inbound is the user."""
    events = [ev("escalation_answered", 3.0, neo_question_id=8)]
    messages = [{"ts": 4.0, "direction": "user_to_agent", "source": "ui",
                 "content": "[Answer from the user] and gzip it"}]
    entries = build_timeline({}, events, messages)
    assert [e["label"] for e in entries] == ["You → worker"]


def test_the_bookkeeping_is_still_on_the_record_for_anyone_who_asks():
    """Debug, not deleted — the moment Neo answered is an audit fact."""
    events = [ev("neo_answered", 3.0, neo_question_id=7),
              ev("escalation_answered", 5.0, neo_question_id=8)]
    entries = build_timeline({}, events, [], include_debug=True)
    assert [(e["kind"], e["label"], e["detail"]) for e in entries] == [
        ("neo_answered", "Neo answered the worker", ""),
        ("escalation_answered", "You answered the worker", ""),
    ]


def test_answers_are_not_repeated_on_top_of_their_message():
    """Both answer paths queue the answer as a message in the same breath, so the
    text is already the next line — an event detail here would print it twice."""
    events = [ev("question_asked", 1.0, neo_question_id=7),
              ev("neo_answered", 3.0, neo_question_id=7),
              ev("escalation_answered", 5.0, neo_question_id=8)]
    messages = [{"ts": 2.0, "direction": "user_to_agent", "source": "neo",
                 "content": "[Neo] go with CSV"},
                {"ts": 4.0, "direction": "user_to_agent", "source": "ui",
                 "content": "[user] and gzip it"}]
    entries = build_timeline({}, events, messages)
    # ...and the answers themselves are still on the record, once each.
    assert [e["detail"] for e in entries if e["kind"] == "message"] == [
        "[Neo] go with CSV", "[user] and gzip it"]


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


def test_a_failed_round_never_reads_as_a_rejection():
    """The kind a reader is most likely to misread. `validation_failed` means NOTHING
    JUDGED THE WORK — a transport outage, or no validator configured at all — and a
    reader who takes it for a rejection goes looking for something to fix that nobody
    asked for. Both causes are asserted, because they are two different sentences, and
    both are asserted to differ from what a real rejection renders as."""
    entries = build_timeline({}, [
        ev("validation_failed", 1.0, round=1, cause="transport", attempt=2,
           error="connection reset"),
        ev("validation_failed", 2.0, round=1, cause="no_validator",
           reason="no validator was configured"),
        ev("validation_rejected", 3.0, reason="no test touches the new branch"),
    ], [])

    labels = [e["label"] for e in entries]
    assert len(set(labels)) == 3, labels
    assert all(label and label != entries[i]["kind"]
               for i, label in enumerate(labels)), labels
    assert "reject" not in (labels[0] + labels[1]).lower()
    assert "attempt 2" in entries[0]["detail"]


def test_the_four_validation_events_read_as_four_different_things():
    """The LABELS, not the level.

    `event_level` returns "signal" for any kind it does not recognise, so asserting
    these four are signal is vacuous — it passes just as well when `_describe` has never
    heard of them and renders the raw kind beside a JSON blob. What has to be true is
    that a reader can tell submitted from passed from rejected from escalated, so the
    four rendered labels are asserted distinct, and one known-debug kind is asserted in
    the same test to prove the classifier is still discriminating at all.
    """
    events = [
        ev("validation_submitted", 1.0, round=2),
        ev("validation_passed", 2.0, reason="tests cover the change"),
        ev("validation_rejected", 3.0, reason="no test touches the new branch"),
        ev("validation_escalated", 4.0, reason="three rounds, no new evidence"),
    ]

    entries = build_timeline({}, events, [])

    labels = [e["label"] for e in entries]
    assert len(set(labels)) == 4, labels
    assert all(label and label != entries[i]["kind"]
               for i, label in enumerate(labels)), labels
    # the reason a rejection gives IS the ask the worker has to answer
    assert entries[2]["detail"] == "no test touches the new branch"
    assert entries[0]["detail"] == "round 2"
    assert event_level("message_delivered") == "debug"


def test_the_validating_status_change_has_a_human_label():
    entry = build_timeline({}, [ev("status", 1.0, status="validating")], [])[0]
    assert entry["label"] == "Under review by the validation panel"
