"""Work order timeline: signal vs debug"""

from __future__ import annotations

from jarvis.timeline import build_conversation, build_timeline, event_level

SIGNAL_KINDS = [
    "created", "dispatched", "status", "attention", "assumption",
    "question_asked", "reviewed",
    "finished",
    # A turn that failed or was cancelled is the story, not the plumbing: it is why the work order stopped
    "turn_failed", "turn_cancelled",
    # So is a turn Claude Code refused for the usage limit
    "rate_limited", "rate_limit_retry", "rate_limit_exhausted",
    "turn_paused", "turn_resumed", "turn_retries_exhausted",
]
DEBUG_KINDS = [
    "message_queued", "delivering", "message_delivered",
    "turn_started", "turn_ended", "session_released", "permission_mode_changed",
    "hook:SessionStart", "hook:Stop", "hook:SessionEnd", "hook:Notification",
    # An answer is the message it was queued as, not the bookkeeping beside it
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
    """Better to show an unclassified event than to silently swallow it"""
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
    """Every surface that renders this timeline puts the title and description at the top of the same..."""
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


def test_a_cleanup_failure_reads_as_a_sentence_and_not_a_json_dump():
    """THE TIMELINE IS THIS KIND'S ONLY SURFACE (#253)"""
    entry = build_timeline({}, [ev("automerge_cleanup_failed", 1.0,
                                   reason="cannot delete branch 'worktree-wo-1'",
                                   head_sha="a1b2c3d")], [])[0]
    assert entry["level"] == "signal"
    assert entry["label"] == "The merge landed; the cleanup after it did not"
    assert entry["detail"] == "cannot delete branch 'worktree-wo-1'"
    assert "{" not in entry["label"] + entry["detail"]


def test_a_merge_that_timed_out_is_not_described_as_a_cleanup():
    """The two landed shapes are separate kinds precisely so this sentence differs: a timed-out merge... (#253)"""
    entry = build_timeline({}, [ev("automerge_command_unfinished", 1.0,
                                   reason="the merge command did not complete: timeout",
                                   head_sha="a1b2c3d")], [])[0]
    assert entry["label"] == \
        "The merge command never finished — GitHub says the merge landed"
    assert "cleanup" not in entry["label"]
    assert entry["detail"] == "the merge command did not complete: timeout"


def test_a_merge_refused_because_the_base_moved_says_which_commit_moved():
    """Spec 2026-09-28 §3.1: the OS refused before GitHub heard anything"""
    entry = build_timeline({}, [ev("automerge_base_stale", 1.0, head_sha="fa85f88ccd0",
                                   base="main", base_oid="37fe650fab0", round=2,
                                   reason="the base moved")], [])[0]
    assert entry["label"] == "Merge refused — the base moved under it"
    assert "round 2 passed on fa85f88ccd" in entry["detail"]
    assert "main is now at 37fe650fab" in entry["detail"]
    assert "{" not in entry["label"] + entry["detail"]


def test_a_stall_with_a_tip_observed_names_both_commits():
    """wo-d3a70c29 condition (a): the evidence IS the disagreement, so both shas"""
    entry = build_timeline({}, [ev("pr_recompute_stalled", 1.0,
                                   head_oid="0488753aaaa", ref_oid="88cac10bbbb",
                                   branch="worktree-wo-cdee6f9b",
                                   mergeable="UNKNOWN")], [])[0]
    assert entry["label"] == "GitHub stopped recomputing the pull request"
    assert "branch at 88cac10bbb" in entry["detail"]
    assert "pull request head at 0488753aa" in entry["detail"]
    assert "{" not in entry["label"] + entry["detail"]


def test_a_stall_with_no_tip_observed_says_the_clock_and_names_no_branch():
    """Condition (b): nothing was compared"""
    entry = build_timeline({}, [ev("pr_recompute_stalled", 1.0,
                                   head_oid="0488753aaaa", ref_oid="",
                                   mergeable="UNKNOWN",
                                   unknown_since=1.0)], [])[0]
    assert entry["label"] == "GitHub stopped recomputing the pull request"
    assert entry["detail"].startswith("mergeability unanswered since ")
    assert "branch at" not in entry["detail"]
    assert "{" not in entry["label"] + entry["detail"]


def test_a_reparked_give_up_reads_as_a_sentence_and_not_a_json_dump():
    """THE TIMELINE IS THIS KIND'S ONLY SURFACE, and the move it records is the OS taking a status back (docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md)"""
    entry = build_timeline({}, [ev("give_up_reparked", 1.0, round_id=12, round=4,
                                   was="waiting_pr_merge")], [])[0]
    assert entry["level"] == "signal"
    assert entry["label"] == "Back on your list — the panel's give-up was never decided"
    assert entry["detail"] == "was waiting_pr_merge, round 4"
    assert "{" not in entry["label"] + entry["detail"]
    assert "give_up_reparked" not in entry["label"] + entry["detail"]


def test_messages_appear_as_prompt_and_reply():
    """Both directions are moments on the timeline; the words are the conversation's"""
    messages = [
        {"id": 1, "ts": 2.0, "direction": "user_to_agent", "content": "also cover EndNote",
         "source": "ui", "authored_by": "user"},
        {"id": 2, "ts": 3.0, "direction": "agent_to_user", "content": "done, EndNote covered",
         "source": "worker"},
    ]
    entries = build_timeline({}, [ev("created", 1.0)], messages)
    assert [e["label"] for e in entries] == [
        "Work order created", "You messaged the worker", "Worker replied"]
    assert [c["content"] for c in build_conversation([], messages)] == [
        "also cover EndNote", "done, EndNote covered"]


def test_entries_are_ordered_by_time():
    events = [ev("finished", 9.0, summary="s"), ev("created", 1.0)]
    messages = [{"ts": 5.0, "direction": "user_to_agent", "content": "hi"}]
    ts = [e["ts"] for e in build_timeline({}, events, messages)]
    assert ts == [1.0, 5.0, 9.0]


def test_message_plumbing_events_never_duplicate_the_message_itself():
    """`delivering`/`message_delivered` are debug; the message content is the signal"""
    events = [ev("message_queued", 1.0, msg_id=1), ev("delivering", 2.0, msg_id=1),
              ev("message_delivered", 3.0, msg_id=1)]
    messages = [{"ts": 1.0, "direction": "user_to_agent", "content": "the ask"}]
    entries = build_timeline({}, events, messages)
    assert len(entries) == 1
    assert entries[0]["detail"] == "the ask"


# -- rendered details

def test_question_asked_points_at_the_question_instead_of_reprinting_it():
    """Only the question's own record holds it and its answer together"""
    events = [ev("question_asked", 1.0, neo_question_id=7,
                 question="CSV or JSON for the export default?")]
    entry = build_timeline({}, events, [])[0]
    assert entry["label"] == "Worker asked a question"
    assert entry["detail"] == ""
    assert entry["ref"] == {"kind": "neo_question", "id": 7, "label": "question #7"}


def test_a_question_event_with_no_id_has_nothing_to_point_at():
    """It still does not reprint the text: the conversation renders the ask from this same payload"""
    events = [ev("question_asked", 1.0, question="what was asked")]
    entry = build_timeline({}, events, [])[0]
    assert entry["ref"] is None
    assert entry["detail"] == ""


def test_most_entries_point_at_nothing():
    """`ref` is present on every entry and set on almost none: a template that reads it must not have to..."""
    entries = build_timeline({}, [ev("created", 1.0), ev("finished", 2.0, summary="s")],
                             [{"ts": 3.0, "direction": "agent_to_user", "content": "x"}])
    assert [e["ref"] for e in entries] == [None, None, None]


def test_assumptions_are_numbered_rather_than_repeated():
    """The page that shows this timeline lists the assumptions themselves, numbered the same way"""
    events = [ev("assumption", 1.0, content="assuming UTF-8 input", n=1),
              ev("assumption", 2.0, content="assuming ISO dates", n=2)]
    entries = build_timeline({}, events, [])
    assert [e["label"] for e in entries] == ["Assumption #1 recorded",
                                             "Assumption #2 recorded"]
    assert [e["detail"] for e in entries] == ["", ""]


def test_assumptions_written_before_they_were_numbered_are_numbered_here():
    """Rows on disk carry no `n`; positional numbering gives the writer's answer"""
    events = [ev("assumption", 1.0, content="first"),
              ev("assumption", 2.0, content="second"),
              ev("assumption", 3.0, content="third", n=3)]
    entries = build_timeline({}, events, [])
    assert [e["label"] for e in entries] == [
        "Assumption #1 recorded", "Assumption #2 recorded", "Assumption #3 recorded"]


def test_neos_answer_is_one_line_and_says_neo_said_it():
    """It used to be two lines, and the survivor was misattributed to the user"""
    events = [ev("question_asked", 1.0, neo_question_id=7),
              ev("neo_answered", 3.0, neo_question_id=7)]
    messages = [{"id": 1, "ts": 4.0, "direction": "user_to_agent", "source": "neo",
                 "content": "[Neo] go with CSV"}]
    entries = build_timeline({}, events, messages)
    assert [(e["kind"], e["label"]) for e in entries] == [
        ("question_asked", "Worker asked a question"),
        ("message", "Neo answered the worker"),
    ]


def test_the_user_answering_still_reads_as_the_user():
    """Only Neo's own messages are relabelled; everything else inbound is the user"""
    events = [ev("escalation_answered", 3.0, neo_question_id=8)]
    messages = [{"ts": 4.0, "direction": "user_to_agent", "source": "ui",
                 "authored_by": "user", "content": "[Answer from the user] and gzip it"}]
    entries = build_timeline({}, events, messages)
    assert [e["label"] for e in entries] == ["You messaged the worker"]


def test_the_bookkeeping_is_still_on_the_record_for_anyone_who_asks():
    """Debug, not deleted — the moment Neo answered is an audit fact"""
    events = [ev("neo_answered", 3.0, neo_question_id=7),
              ev("escalation_answered", 5.0, neo_question_id=8)]
    entries = build_timeline({}, events, [], include_debug=True)
    assert [(e["kind"], e["label"], e["detail"]) for e in entries] == [
        ("neo_answered", "Neo answered the worker", ""),
        ("escalation_answered", "You answered the worker", ""),
    ]


def test_answers_are_not_repeated_on_top_of_their_message():
    """Both answer paths queue the answer as a message in the same breath"""
    events = [ev("question_asked", 1.0, neo_question_id=7),
              ev("neo_answered", 3.0, neo_question_id=7),
              ev("escalation_answered", 5.0, neo_question_id=8)]
    messages = [{"id": 1, "ts": 2.0, "direction": "user_to_agent", "source": "neo",
                 "content": "[Neo] go with CSV"},
                {"id": 2, "ts": 4.0, "direction": "user_to_agent", "source": "ui",
                 "content": "[user] and gzip it"}]
    entries = build_timeline({}, events, messages)
    assert [e["detail"] for e in entries if e["kind"] == "message"] == ["", ""]
    # ...and the answers themselves are still on the record, once each — over there
    assert [c["content"] for c in build_conversation(events, messages)] == [
        "[Neo] go with CSV", "[user] and gzip it"]


def test_cli_wo_show_hides_debug_entries_by_default(jarvis_home, fake_claude,
                                                    catalog_file, capsys):
    """`jarvis wo show` speaks the same timeline as the web UI, with --debug to reveal the plumbing"""
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
    """The kind a reader is most likely to misread"""
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
    """The LABELS, not the level"""
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


# --- The conversation owns what was SAID; the timeline points at it

def test_the_conversation_carries_the_question_the_worker_asked():
    """The ask and its answer are one exchange; the ask was only ever an event"""
    events = [ev("question_asked", 1.0, neo_question_id=7,
                 question="CSV or JSON for the export default?")]
    messages = [{"id": 3, "ts": 2.0, "direction": "user_to_agent", "source": "neo",
                 "content": "[Neo] go with CSV", "status": "delivered"}]
    convo = build_conversation(events, messages)
    assert [(c["kind"], c["who"], c["content"]) for c in convo] == [
        ("question", "worker → Neo", "CSV or JSON for the export default?"),
        ("message", "neo → worker", "[Neo] go with CSV"),
    ]
    assert convo[0]["ref"] == {"kind": "neo_question", "id": 7, "label": "question #7"}


def test_the_conversation_is_ordered_by_when_it_was_said():
    """Questions and messages interleave — they are turns in one exchange"""
    events = [ev("question_asked", 3.0, neo_question_id=7, question="second"),
              ev("dispatched", 2.0, worktree="wt")]
    messages = [{"id": 1, "ts": 1.0, "direction": "user_to_agent", "source": "ui",
                 "content": "first"},
                {"id": 2, "ts": 4.0, "direction": "agent_to_user", "content": "third"}]
    assert [c["content"] for c in build_conversation(events, messages)] == [
        "first", "second", "third"]


def test_the_conversation_holds_nothing_that_was_not_said():
    """Lifecycle events are the timeline's business, not the conversation's"""
    events = [ev("created", 1.0), ev("status", 2.0, status="running"),
              ev("finished", 3.0, summary="done")]
    assert build_conversation(events, []) == []


def test_a_question_with_no_recorded_text_is_not_a_silent_turn():
    """`question` has been written since the event existed"""
    convo = build_conversation([ev("question_asked", 1.0, neo_question_id=7)], [])
    assert convo == []


def test_every_conversation_turn_can_be_pointed_at():
    """The timeline's whole saving depends on the anchor resolving"""
    events = [ev("question_asked", 1.0, neo_question_id=7, question="q?")]
    messages = [{"id": 42, "ts": 2.0, "direction": "agent_to_user", "content": "a"}]
    assert [c["anchor"] for c in build_conversation(events, messages)] == [
        "q-7", "msg-42"]


def test_the_timeline_points_at_a_message_instead_of_reprinting_it():
    """The conversation tab is showing the reader this exact text a click away"""
    messages = [{"id": 528, "ts": 4.0, "direction": "user_to_agent", "source": "neo",
                 "content": "[Neo, answering for the user] A now, B filed, C rejected."}]
    entry = build_timeline({}, [], messages)[0]
    assert entry["label"] == "Neo answered the worker"
    assert entry["detail"] == ""
    assert entry["ref"] == {"kind": "message", "id": 528, "label": "in the conversation"}


def test_the_timeline_says_who_spoke_without_saying_what():
    """One label per speaker, and none of them carries the body"""
    messages = [
        {"id": 1, "ts": 1.0, "direction": "user_to_agent", "source": "ui", "content": "a",
         "authored_by": "user"},
        {"id": 2, "ts": 2.0, "direction": "user_to_agent", "source": "neo", "content": "b"},
        {"id": 3, "ts": 3.0, "direction": "user_to_agent", "source": "pr-conflict",
         "content": "c"},
        {"id": 4, "ts": 4.0, "direction": "agent_to_user", "content": "d"},
    ]
    entries = build_timeline({}, [], messages)
    assert [(e["label"], e["detail"]) for e in entries] == [
        ("You messaged the worker", ""),
        ("Neo answered the worker", ""),
        ("Jarvis messaged the worker", ""),
        ("Worker replied", ""),
    ]


def test_a_message_with_no_id_still_says_what_it_said():
    """Nothing to point at, so the text is all there is"""
    entry = build_timeline({}, [], [{"ts": 1.0, "direction": "agent_to_user",
                                     "content": "no id here"}])[0]
    assert entry["ref"] is None
    assert entry["detail"] == "no id here"


def test_the_timeline_never_reprints_the_question_either():
    """It has a home now: the conversation"""
    events = [ev("question_asked", 1.0, question="what was asked")]
    entry = build_timeline({}, events, [])[0]
    assert entry["detail"] == ""
    assert build_conversation(events, [])[0]["content"] == "what was asked"


# -- the cost alarm on the order's own record


def test_the_four_alarm_events_read_as_four_different_things():
    """The LABELS, in the shape the validation kinds forced"""
    events = [
        # Spelled out: `ev`'s first parameter is the event kind and this payload has a `kind` of its own
        {"ts": 1.0, "kind": "cost_alarm",
         "payload": {"kind": "turn_minutes", "seq": 3, "alarm_id": "al-1a2b",
                     "reason": "turn has been running 94 minutes"}},
        ev("alarm_reviewed", 2.0, alarm_id="al-1a2b", verdict="ack",
           reason="a long test run, not a stuck turn", note="left it running"),
        ev("alarm_escalated", 3.0, alarm_id="al-3c4d", neo_question_id=12),
        ev("alarm_advice", 4.0, alarm_id="al-3c4d", neo_question_id=12,
           answer="the re-write is a prefix miss; let it finish"),
    ]

    entries = build_timeline({}, events, [])

    labels = [e["label"] for e in entries]
    assert len(set(labels)) == 4, labels
    assert all(label and label != entries[i]["kind"]
               for i, label in enumerate(labels)), labels
    # The verdict's reason is the record's, and nothing else on either surface has it
    assert entries[1]["detail"] == "a long test run, not a stuck turn"
    assert entries[2]["detail"] == "question #12"
    assert event_level("message_delivered") == "debug"


def test_the_renderer_knows_exactly_the_kinds_the_store_freezes():
    """`timeline` is a leaf and must not import a store, so the four kinds are spelled out in both places"""
    from jarvis.project_store import ALARM_EVENT_KINDS
    from jarvis.timeline import ALARM_KINDS

    assert ALARM_KINDS == frozenset(ALARM_EVENT_KINDS)


def test_a_verdict_reads_as_what_it_decided():
    """`alarm_reviewed` carries both verdicts"""
    acked, escalated = (build_timeline({}, [ev("alarm_reviewed", 1.0, alarm_id="al-1",
                                               verdict=v, reason=v)], [])[0]
                        for v in ("ack", "escalate"))
    assert acked["label"] != escalated["label"]
    assert "cleared" in acked["label"] and "cleared" not in escalated["label"]


def test_every_alarm_event_points_at_the_alarm():
    """One `al-` id reaches every surface that knows about that alarm"""
    for kind in ("cost_alarm", "alarm_reviewed", "alarm_escalated", "alarm_advice"):
        entry = build_timeline({}, [ev(kind, 1.0, alarm_id="al-1a2b",
                                       neo_question_id=12)], [])[0]
        assert entry["ref"] == {"kind": "alarm", "id": "al-1a2b",
                                "label": "alarm al-1a2b"}, kind


def test_an_alarm_raised_before_the_table_existed_has_nothing_to_point_at():
    """`cost_alarm` events predating §1 carry no `alarm_id`"""
    entry = build_timeline({}, [{"ts": 1.0, "kind": "cost_alarm",
                                 "payload": {"kind": "turn_minutes", "seq": 1,
                                             "reason": "94 minutes"}}], [])[0]
    assert entry["ref"] is None
    assert entry["detail"] == "94 minutes"


def test_the_conversation_carries_the_note_and_the_advice_and_no_verdict():
    """What was SAID about the alarm: a note addressed to the user, and the advice behind it"""
    events = [
        ev("cost_alarm", 1.0, alarm_id="al-1a2b", reason="94 minutes"),
        ev("alarm_escalated", 2.0, alarm_id="al-1a2b", neo_question_id=12),
        ev("alarm_advice", 3.0, alarm_id="al-1a2b", neo_question_id=12,
           answer="a prefix miss on a long test run — let it finish"),
        ev("alarm_reviewed", 4.0, alarm_id="al-1a2b", verdict="ack",
           reason="explicable", note="it is re-running the suite; nothing is stuck"),
    ]
    convo = build_conversation(events, [])
    assert [(c["kind"], c["who"], c["content"]) for c in convo] == [
        ("advice", "neo → supervisor",
         "a prefix miss on a long test run — let it finish"),
        ("note", "supervisor → you",
         "it is re-running the suite; nothing is stuck"),
    ]
    assert convo[1]["ref"] == {"kind": "alarm", "id": "al-1a2b",
                               "label": "alarm al-1a2b"}


def test_the_timeline_never_reprints_the_note_or_the_advice():
    """Both have a home in the conversation"""
    events = [ev("alarm_reviewed", 1.0, alarm_id="al-1", verdict="ack", reason="fine",
                 note="it is re-running the suite"),
              ev("alarm_advice", 2.0, alarm_id="al-1", answer="let it finish")]
    assert [e["detail"] for e in build_timeline({}, events, [])] == ["fine", ""]
    assert [c["content"] for c in build_conversation(events, [])] == [
        "it is re-running the suite", "let it finish"]


def test_an_escalation_is_not_an_empty_speech_bubble():
    """`note` is empty by contract when the supervisor escalates (§2)"""
    assert build_conversation([ev("alarm_reviewed", 1.0, alarm_id="al-1",
                                  verdict="escalate", reason="cannot tell",
                                  note="")], []) == []


def test_every_alarm_status_has_a_reading_a_person_can_use():
    """All eight, because the supervisor ships OFF and `raised` is therefore the common case"""
    from jarvis.ops import ALARM_STANDING, alarm_standing_line
    from jarvis.project_store import ALARM_STATUSES

    assert set(ALARM_STANDING) == set(ALARM_STATUSES)
    alarms = [{"id": f"al-{i}", "status": s} for i, s in enumerate(ALARM_STATUSES)]
    assert alarm_standing_line(alarms) == (
        "8 (1 raised, 1 a note, never escalated, 1 with the supervisor, "
        "1 acked by the supervisor, 1 escalated to Neo, 1 a remedy proposed, "
        "1 not reviewed, 1 supervisor failed) — "
        "al-0, al-1, al-2, al-3, al-4, al-5, al-6, al-7")
    assert alarm_standing_line([{"id": "al-1a2b", "status": "acked"},
                                {"id": "al-3c4d", "status": "escalated"}]) == (
        "2 (1 acked by the supervisor, 1 escalated to Neo) — al-1a2b, al-3c4d")
    # The supervisor off, which is every fleet today
    assert alarm_standing_line([{"id": "al-1a2b", "status": "raised"}]) == (
        "1 (1 raised) — al-1a2b")


def test_cli_wo_show_says_where_the_alarms_stand(jarvis_home, fake_claude,
                                                 catalog_file, capsys):
    """The header line, and the rows behind it"""
    import json as _json

    from jarvis import cli, ops
    from jarvis.project_store import ProjectStore

    ops.start_os(str(catalog_file), foreground=True)
    wo = ops.create_work_order("proj_a", "export citations")
    _, path, _ = ops.find_work_order(wo["id"], "proj_a")

    cli.main(["wo", "show", wo["id"]])
    assert "alarms:" not in capsys.readouterr().out
    cli.main(["wo", "show", wo["id"], "--json"])
    assert _json.loads(capsys.readouterr().out)["alarms"] == []

    store = ProjectStore(path)
    acked = store.add_alarm(wo["id"], "turn_minutes", 3, "running 94 minutes")
    store.update_alarm(acked["id"], status="acked", verdict="ack",
                       note="it is re-running the suite")
    escalated = store.add_alarm(wo["id"], "cache_write", 3, "300k re-write")
    store.update_alarm(escalated["id"], status="escalated")
    store.close()

    cli.main(["wo", "show", wo["id"]])
    assert (f"alarms: 2 (1 acked by the supervisor, 1 escalated to Neo) — "
            f"{acked['id']}, {escalated['id']}") in capsys.readouterr().out

    cli.main(["wo", "show", wo["id"], "--json"])
    rows = _json.loads(capsys.readouterr().out)["alarms"]
    # The `wo_alarms` rows in full, not `ops.list_cost_alarms`' fleet-wide dict: this is one order's own...
    assert [r["id"] for r in rows] == [acked["id"], escalated["id"]]
    assert rows[0]["note"] == "it is re-running the suite"
    assert rows[0]["seq"] == 3 and rows[0]["kind"] == "turn_minutes"
    assert "title" not in rows[0] and "alarm_status" not in rows[0]


# -- auto-review's four


def test_the_four_auto_review_events_read_as_four_different_things():
    """Same trap the alarm kinds and the validation kinds paid for: `event_level` calls an unknown kind..."""
    events = [
        ev("autoreview_asked", 1.0, assumption_id=1, n=1, neo_question_id=41),
        ev("autoreview_accepted", 2.0, assumption_id=1, n=1, decided_by="neo",
           model="claude-opus-5", neo_question_id=41,
           reason="a naming convention, not a decision"),
        ev("autoreview_escalated", 3.0, assumption_id=2, n=2, neo_question_id=42,
           reason="dropping a flag changes the CLI surface"),
        ev("autoreview_held", 4.0, assumption_id=3, n=3, code="high_stakes",
           reason="assumption #3 mentions 'production' — the OS does not decide those"),
    ]

    entries = build_timeline({}, events, [])

    labels = [e["label"] for e in entries]
    assert len(set(labels)) == 4, labels
    assert all(label and label != entries[i]["kind"]
               for i, label in enumerate(labels)), labels


def test_an_accepted_assumption_says_the_os_decided_it_and_names_the_model():
    """THE RECORD POST-CONDITION, on the surface that narrates the order"""
    entries = build_timeline({}, [
        ev("autoreview_accepted", 1.0, assumption_id=1, n=1, decided_by="neo",
           model="claude-opus-5", reason="a naming convention, not a decision"),
    ], [])

    (entry,) = entries
    assert "the OS" in entry["label"] and "not by you" in entry["label"]
    assert "claude-opus-5" in entry["label"]
    assert entry["detail"] == "a naming convention, not a decision"


def test_a_model_the_record_does_not_have_is_said_so_on_the_timeline_too():
    """`ops.assumption_decider`'s words"""
    entries = build_timeline({}, [
        ev("autoreview_accepted", 1.0, assumption_id=1, n=1, decided_by="neo",
           reason="unchanged behaviour"),
    ], [])

    assert "model not recorded" in entries[0]["label"]


def test_an_escalated_round_is_labelled_by_its_family_and_never_gave_up():
    """Spec 2026-10-14-an-escalated-round-says-why-and-what-to-run-next §1: all three `handover` causes..."""
    from jarvis import ops
    from jarvis.timeline import _describe

    handover, _ = _describe("validation_escalated",
                            {"round": 3, "cause": ops.ESCALATION_USER_REWORK_CEILING})
    not_reviewed, _ = _describe("validation_escalated",
                                {"round": 1, "cause": ops.ESCALATION_NOTHING_TO_JUDGE})
    unjudged, _ = _describe("validation_escalated",
                            {"round": 1, "cause": ops.ESCALATION_NO_VERDICT})
    legacy, reason = _describe("validation_escalated",
                               {"round": 1, "reason": "no test covers the change"})

    assert handover == "Validation was rejected and handed to you"
    assert not_reviewed == "Validation was not reviewed — over to you"
    assert unjudged == "Validation could not be judged — over to you"
    assert legacy == "Validation was escalated — over to you"
    assert reason == "no test covers the change"
    for label in (handover, not_reviewed, unjudged, legacy):
        assert "gave up" not in label
    assert "rejected" not in not_reviewed


def test_the_timelines_families_are_the_os_s_own_and_its_verbs_too():
    """`timeline` imports nothing from `jarvis`"""
    from jarvis import ops
    from jarvis.timeline import (ESCALATION_HANDOVER, ESCALATION_NOT_REVIEWED,
                                 ESCALATION_UNJUDGED, _describe)

    by_family: dict[str, set[str]] = {}
    for cause, family in ops.ESCALATION_FAMILY.items():
        by_family.setdefault(family, set()).add(cause)

    assert ESCALATION_HANDOVER == by_family["handover"]
    assert ESCALATION_NOT_REVIEWED == by_family["not_reviewed"]
    assert ESCALATION_UNJUDGED == by_family["unjudged"]
    for cause, family in ops.ESCALATION_FAMILY.items():
        label, _ = _describe("validation_escalated", {"round": 1, "cause": cause})
        assert ops.ESCALATION_VERB[family] in label, cause


def test_a_dropped_ruling_is_not_narrated_as_neo_declining():
    """Spec §5.1's escalation carries `dropped` when the OS HAD a ruling and threw it away because the..."""
    plain, dropped = build_timeline({}, [
        ev("autoreview_escalated", 1.0, assumption_id=1, n=1,
           reason="Neo would have turned this down"),
        ev("autoreview_escalated", 2.0, assumption_id=2, n=2, dropped="panel_gave_up",
           reason="the validation panel gave up and put this in front of you"),
    ], [])

    assert "dropped its ruling" not in plain["label"]
    assert "dropped its ruling" in dropped["label"]
    assert plain["label"] != dropped["label"]


def test_a_dropped_ruling_the_os_will_re_ask_does_not_say_left_with_you():
    """io-51469add: a transient drop is not the user's decision"""
    (entry,) = build_timeline({}, [
        ev("autoreview_escalated", 1.0, assumption_id=1, n=1, dropped="status",
           re_asked=True, reason="the work order is running, not waiting on a review"),
    ], [])

    assert "left with you" not in entry["label"]
    assert "will ask Neo again" in entry["label"]


def test_an_auto_review_event_with_no_reason_still_says_something():
    """Every one of these rows exists to carry a WHY"""
    entries = build_timeline({}, [
        ev("autoreview_accepted", 1.0, n=1),
        ev("autoreview_escalated", 2.0, n=2),
        ev("autoreview_held", 3.0, n=3),
    ], [])

    assert all(e["detail"] == "no reason recorded" for e in entries), entries


def test_a_carried_verdict_and_a_refused_carry_both_render_and_name_the_proof():
    """docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §4"""
    carried, refused = build_timeline({}, [
        ev("validation_head_carried", 1.0, cause="base_merge_chain", round=1,
           judged_sha="709582ae53000000", carried_head_sha="c2120424ba000000",
           base="main", chain=["1a2b3c4d5e", "2b3c4d5e6f"], patch_id="b7f0deadbeef"),
        ev("validation_carry_refused", 2.0, proof="patch_id", round=1,
           judged_sha="709582ae53000000", head_sha="c2120424ba000000",
           detail="the branch's own diff changed"),
    ], [])

    assert carried["label"] == "Verdict carried to the new head"
    assert "709582ae53" in carried["detail"] and "c2120424ba" in carried["detail"]
    assert "2 merge(s) of main" in carried["detail"]
    assert refused["label"] == "Verdict not carried — re-judging"
    assert "c2120424ba" in refused["detail"] and "resolved content" in refused["detail"]


# -- a rebind: the round the OS's own merge costs nobody


def test_the_two_rejudge_declines_are_told_apart_by_their_cause():
    """One decline says the round budget is spent and offers `validation.max_rounds`"""
    from jarvis import ops
    from jarvis.timeline import _describe

    label, budget = _describe("validation_rejudge_declined",
                              {"head_sha": "bbbb1111bbbb2222", "round": 2,
                               "cause": ops.REJUDGE_BUDGET_SPENT, "next_round": 3,
                               "max_rounds": 3})
    rebind_label, rebind = _describe("validation_rejudge_declined",
                                     {"head_sha": "bbbb1111bbbb2222", "round": 2,
                                      "cause": ops.REBIND_EXHAUSTED, "rebinds": 2,
                                      "rebind_max": 2})

    assert "round 3 of 3 would be the last" in budget
    assert rebind_label == label
    assert "re-judged this merge 2 time(s)" in rebind
    assert "would be the last" not in rebind


def test_a_rebind_reads_as_a_round_nobody_was_charged_for():
    from jarvis import ops
    from jarvis.timeline import _describe

    reason = ops.REBIND_FORCED_REASON.format(n=3, judged="aaaa1111aa",
                                             head="bbbb1111bb", used=1,
                                             max=ops.REBIND_MAX)
    label, detail = _describe("validation_forced",
                              {"round": 4, "by": ops.REJUDGE_BY_OS, "rebind": True,
                               "reason": reason})

    assert label.startswith("Validation forced by the OS")
    assert "no round spent" in label
    assert "does not count against validation.max_rounds" in detail


# -- a rework the USER asked for: the second uncounted cause


def test_a_user_rework_round_reads_as_the_rework_the_user_asked_for():
    """Not a rebind: nobody merged anything"""
    from jarvis import ops
    from jarvis.timeline import _describe

    reason = ops.USER_REWORK_FORCED_REASON.format(n=3, judged="aaaa1111aa",
                                                  head="bbbb1111bb")
    label, detail = _describe("validation_forced",
                              {"round": 4, "by": ops.REJUDGE_BY_OS, "rebind": False,
                               "cause": ops.USER_REWORK_CAUSE, "reason": reason})

    assert label.startswith("Validation forced by the OS")
    assert "no round spent" in label and "rework you asked for" in label
    assert "does not count against validation.max_rounds" in detail


def test_a_rejected_user_rework_round_says_no_round_was_spent():
    from jarvis import ops
    from jarvis.timeline import _describe

    label, detail = _describe("validation_rejected",
                              {"round": 4, "of": 3, "budget_round": 4,
                               "uncounted": True,
                               "uncounted_cause": ops.USER_REWORK_CAUSE,
                               "reason": "the rework misses the case"})
    plain, _ = _describe("validation_rejected", {"round": 2, "reason": "no"})

    assert "no round spent" in label and "rework you asked for" in label
    assert detail == "the rework misses the case"
    assert "no round spent" not in plain


def test_an_old_forced_row_with_no_cause_still_reads_as_a_rebind():
    """No surface may print a wrong sentence for a row written before the cause existed: `rebind: true`..."""
    from jarvis import ops
    from jarvis.timeline import _describe

    label, _ = _describe("validation_forced",
                         {"round": 4, "by": ops.REJUDGE_BY_OS, "rebind": True,
                          "reason": "x"})

    assert "no round spent" in label and "rework you asked for" not in label


def test_a_retry_says_who_asked_for_it_and_whether_they_said_anything():
    """§8 of docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md"""
    entries = build_timeline({}, [ev("retry_requested", 1.0, msg_id=4, authored=True),
                                  ev("retry_requested", 2.0, msg_id=5, authored=False)],
                             [])

    assert [e["level"] for e in entries] == ["signal", "signal"]
    assert [e["label"] for e in entries] == ["You retried this order"] * 2
    assert entries[0]["detail"] == "with your message"
    assert entries[1]["detail"] == ("the OS's own relaunch note — you sent no message")
    assert "{" not in "".join(e["label"] + e["detail"] for e in entries)


def test_turn_harvested_is_signal():
    """What the OS read off disk when a turn died is the story, not the plumbing (docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md §5.)"""
    from jarvis.timeline import TURN_HARVESTED

    assert event_level(TURN_HARVESTED) == "signal"
    full = build_timeline({}, [ev(TURN_HARVESTED, 1.0, seq=7, version=1, empty=False,
                                  authored={"branch": "wo-2ae", "base": "origin/main",
                                            "commits": 3, "dirty": ["src/a.py"]},
                                  checkpoint="9f8e7d6")], [])[0]
    assert full["label"] == "Harvested what the turn left behind"
    assert full["detail"] == "3 commits, 1 uncommitted file checkpointed as 9f8e7d6"
    empty = build_timeline({}, [ev(TURN_HARVESTED, 1.0, seq=7, version=1, empty=True,
                                   authored={}, checkpoint="")], [])[0]
    assert empty["label"] == "Nothing to harvest"
    assert empty["detail"] == "the worktree was clean and the turn said nothing"


def test_a_message_is_the_users_only_when_the_os_can_prove_it():
    """Unstamped is Jarvis, whatever its source (io-ce566253 objection-reads-as-the-user)"""
    from jarvis.timeline import _message_event_label, _message_label

    base = {"direction": "user_to_agent", "content": "x"}
    # The early objection: delivered off the bus, deliberately left unattributed
    objection = {**base, "source": "bus", "authored_by": ""}
    assert _message_label(objection) == "jarvis → worker"
    assert _message_event_label(objection) == "Jarvis messaged the worker"
    # A source nobody has heard of is no longer asserted to be the user
    assert _message_label({**base, "source": "brand-new"}) == "jarvis → worker"
    # A legacy row from before `authored_by` existed reads as Jarvis, honestly
    assert _message_label({**base, "source": "ui"}) == "jarvis → worker"
    typed = {**base, "source": "ui", "authored_by": "user"}
    assert _message_label(typed) == "you → worker"
    assert _message_event_label(typed) == "You messaged the worker"


def test_the_conversation_and_the_gate_request_agree_on_the_users_words(tmp_path):
    """`you → worker` is exactly the rows `ProjectStore.user_messages` returns"""
    from jarvis import ops
    from jarvis.project_store import MESSAGE_AUTHOR_USER, ProjectStore
    from jarvis.timeline import AUTHOR_USER, _message_label

    assert AUTHOR_USER == MESSAGE_AUTHOR_USER
    store = ProjectStore(tmp_path)
    try:
        wo = store.create_work_order("t", "d")
        for source, author in [("ui", MESSAGE_AUTHOR_USER), ("cli", MESSAGE_AUTHOR_USER),
                               ("bus", ""), ("jarvis", ""), ("ui", ""),
                               ("pr-conflict", ""), ("neo", "")]:
            store.queue_message(wo["id"], f"{source}/{author}", source=source,
                                authored_by=author)
        store.queue_message(wo["id"], "reply", source="worker", direction="agent_to_user")
        rendered = {m["id"] for m in store.list_messages(wo["id"])
                    if _message_label(m) == "you → worker"}
        assert rendered == {m["id"] for m in store.user_messages(wo["id"])}
        assert len(rendered) == 2
    finally:
        store.close()
    assert ops.user_authorship(relay=False) == ""
