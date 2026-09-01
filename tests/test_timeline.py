"""Work order timeline: signal vs debug.

The raw `wo_events` table mixes two very different things — the story of the work
order (what was asked, what was decided, what came back) and the plumbing that
carries it (message delivery, session hooks, turn boundaries). The timeline shows
the story by default; the plumbing is debug and only surfaces on request.
"""

from __future__ import annotations

from jarvis.timeline import build_conversation, build_timeline, event_level

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
    """Both directions are moments on the timeline; the words are the conversation's."""
    messages = [
        {"id": 1, "ts": 2.0, "direction": "user_to_agent", "content": "also cover EndNote",
         "source": "ui"},
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


def test_a_question_event_with_no_id_has_nothing_to_point_at():
    """It still does not reprint the text: the conversation renders the ask from this
    same payload, so a fallback here would print it twice on one page."""
    events = [ev("question_asked", 1.0, question="what was asked")]
    entry = build_timeline({}, events, [])[0]
    assert entry["ref"] is None
    assert entry["detail"] == ""


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
    messages = [{"id": 1, "ts": 4.0, "direction": "user_to_agent", "source": "neo",
                 "content": "[Neo] go with CSV"}]
    entries = build_timeline({}, events, messages)
    assert [(e["kind"], e["label"]) for e in entries] == [
        ("question_asked", "Worker asked a question"),
        ("message", "Neo answered the worker"),
    ]


def test_the_user_answering_still_reads_as_the_user():
    """Only Neo's own messages are relabelled; everything else inbound is the user."""
    events = [ev("escalation_answered", 3.0, neo_question_id=8)]
    messages = [{"ts": 4.0, "direction": "user_to_agent", "source": "ui",
                 "content": "[Answer from the user] and gzip it"}]
    entries = build_timeline({}, events, messages)
    assert [e["label"] for e in entries] == ["You messaged the worker"]


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
    messages = [{"id": 1, "ts": 2.0, "direction": "user_to_agent", "source": "neo",
                 "content": "[Neo] go with CSV"},
                {"id": 2, "ts": 4.0, "direction": "user_to_agent", "source": "ui",
                 "content": "[user] and gzip it"}]
    entries = build_timeline({}, events, messages)
    assert [e["detail"] for e in entries if e["kind"] == "message"] == ["", ""]
    # ...and the answers themselves are still on the record, once each — over there.
    assert [c["content"] for c in build_conversation(events, messages)] == [
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


# --- The conversation owns what was SAID; the timeline points at it ---------------
#
# §3's rule was applied to the event half of `build_timeline` and to nothing else, which
# left the record with a hole and a duplicate:
#
#   * the worker's question to Neo is an EVENT, never a message, so the conversation
#     showed Neo's answer with nothing above it to answer;
#   * every message was merged into the timeline with its whole body, so the timeline
#     was a second, worse copy of the conversation.
#
# Both are the same missing half — a home for what was said. See
# docs/superpowers/specs/2026-08-24-the-conversation-owns-what-was-said.md.

def test_the_conversation_carries_the_question_the_worker_asked():
    """The ask and its answer are one exchange; the ask was only ever an event."""
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
    """Questions and messages interleave — they are turns in one exchange."""
    events = [ev("question_asked", 3.0, neo_question_id=7, question="second"),
              ev("dispatched", 2.0, worktree="wt")]
    messages = [{"id": 1, "ts": 1.0, "direction": "user_to_agent", "source": "ui",
                 "content": "first"},
                {"id": 2, "ts": 4.0, "direction": "agent_to_user", "content": "third"}]
    assert [c["content"] for c in build_conversation(events, messages)] == [
        "first", "second", "third"]


def test_the_conversation_holds_nothing_that_was_not_said():
    """Lifecycle events are the timeline's business, not the conversation's."""
    events = [ev("created", 1.0), ev("status", 2.0, status="running"),
              ev("finished", 3.0, summary="done")]
    assert build_conversation(events, []) == []


def test_a_question_with_no_recorded_text_is_not_a_silent_turn():
    """`question` has been written since the event existed, but a row that lacks it
    must not render an empty speech bubble — corollary 1 of §1."""
    convo = build_conversation([ev("question_asked", 1.0, neo_question_id=7)], [])
    assert convo == []


def test_every_conversation_turn_can_be_pointed_at():
    """The timeline's whole saving depends on the anchor resolving."""
    events = [ev("question_asked", 1.0, neo_question_id=7, question="q?")]
    messages = [{"id": 42, "ts": 2.0, "direction": "agent_to_user", "content": "a"}]
    assert [c["anchor"] for c in build_conversation(events, messages)] == [
        "q-7", "msg-42"]


def test_the_timeline_points_at_a_message_instead_of_reprinting_it():
    """The conversation tab is showing the reader this exact text a click away."""
    messages = [{"id": 528, "ts": 4.0, "direction": "user_to_agent", "source": "neo",
                 "content": "[Neo, answering for the user] A now, B filed, C rejected."}]
    entry = build_timeline({}, [], messages)[0]
    assert entry["label"] == "Neo answered the worker"
    assert entry["detail"] == ""
    assert entry["ref"] == {"kind": "message", "id": 528, "label": "in the conversation"}


def test_the_timeline_says_who_spoke_without_saying_what():
    """One label per speaker, and none of them carries the body."""
    messages = [
        {"id": 1, "ts": 1.0, "direction": "user_to_agent", "source": "ui", "content": "a"},
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
    """Nothing to point at, so the text is all there is — the same fallback the
    question entry has, and for the same reason."""
    entry = build_timeline({}, [], [{"ts": 1.0, "direction": "agent_to_user",
                                     "content": "no id here"}])[0]
    assert entry["ref"] is None
    assert entry["detail"] == "no id here"


def test_the_timeline_never_reprints_the_question_either():
    """It has a home now: the conversation. The id-less case is not a licence to
    print it twice, because the conversation renders it from the payload, not the id."""
    events = [ev("question_asked", 1.0, question="what was asked")]
    entry = build_timeline({}, events, [])[0]
    assert entry["detail"] == ""
    assert build_conversation(events, [])[0]["content"] == "what was asked"


# -- the cost alarm on the order's own record ---------------------------------------
# §4 of docs/superpowers/specs/2026-08-31-the-supervisor.md. The events are constructed
# here rather than produced by running a supervisor: the four kinds are frozen in §1 and
# the judge emitting three of them is a separate piece.


def test_the_four_alarm_events_read_as_four_different_things():
    """The LABELS, in the shape the validation kinds forced.

    `event_level` returns "signal" for any kind it has never heard of, so asserting
    these four are signal passes just as well when `_describe` renders each as its own
    name beside a JSON blob. What has to be true is that a reader can tell the raise
    from the verdict from the hand-off from the advice.
    """
    events = [
        # Spelled out: `ev`'s first parameter is the event kind and this payload has a
        # `kind` of its own — inspection's, the threshold that fired.
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
    # The verdict's reason is the record's, and nothing else on either surface has it.
    assert entries[1]["detail"] == "a long test run, not a stuck turn"
    assert entries[2]["detail"] == "question #12"
    assert event_level("message_delivered") == "debug"


def test_the_renderer_knows_exactly_the_kinds_the_store_freezes():
    """`timeline` is a leaf and must not import a store, so the four kinds are spelled
    out in both places. This is the only thing stopping the copies drifting — a fifth
    kind added to the store and not here gets no ref and no label, and `event_level`
    calls it signal, so it renders as a bare name beside a JSON blob and looks fine."""
    from jarvis.project_store import ALARM_EVENT_KINDS
    from jarvis.timeline import ALARM_KINDS

    assert ALARM_KINDS == frozenset(ALARM_EVENT_KINDS)


def test_a_verdict_reads_as_what_it_decided():
    """`alarm_reviewed` carries both verdicts, and "cleared" on an escalation would be
    the one falsehood this line is able to tell."""
    acked, escalated = (build_timeline({}, [ev("alarm_reviewed", 1.0, alarm_id="al-1",
                                               verdict=v, reason=v)], [])[0]
                        for v in ("ack", "escalate"))
    assert acked["label"] != escalated["label"]
    assert "cleared" in acked["label"] and "cleared" not in escalated["label"]


def test_every_alarm_event_points_at_the_alarm():
    """One `al-` id reaches every surface that knows about that alarm, so all four kinds
    spend their one pointer on it — the two carrying a Neo question id included, whose
    deliberation the alarm's own page quotes anyway."""
    for kind in ("cost_alarm", "alarm_reviewed", "alarm_escalated", "alarm_advice"):
        entry = build_timeline({}, [ev(kind, 1.0, alarm_id="al-1a2b",
                                       neo_question_id=12)], [])[0]
        assert entry["ref"] == {"kind": "alarm", "id": "al-1a2b",
                                "label": "alarm al-1a2b"}, kind


def test_an_alarm_raised_before_the_table_existed_has_nothing_to_point_at():
    """`cost_alarm` events predating §1 carry no `alarm_id`, and the backfill gives
    those rows ids the EVENT never learns. A ref that cannot resolve is not a pointer."""
    entry = build_timeline({}, [{"ts": 1.0, "kind": "cost_alarm",
                                 "payload": {"kind": "turn_minutes", "seq": 1,
                                             "reason": "94 minutes"}}], [])[0]
    assert entry["ref"] is None
    assert entry["detail"] == "94 minutes"


def test_the_conversation_carries_the_note_and_the_advice_and_no_verdict():
    """What was SAID about the alarm: a note addressed to the user, and the advice
    behind it. The raise, the verdict and the hand-off are events — they happened,
    nobody said them — and tuple equality is what catches one leaking in here."""
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
    """Both have a home in the conversation, rendered from these same payloads, so a
    fallback detail here would print each twice on one page."""
    events = [ev("alarm_reviewed", 1.0, alarm_id="al-1", verdict="ack", reason="fine",
                 note="it is re-running the suite"),
              ev("alarm_advice", 2.0, alarm_id="al-1", answer="let it finish")]
    assert [e["detail"] for e in build_timeline({}, events, [])] == ["fine", ""]
    assert [c["content"] for c in build_conversation(events, [])] == [
        "it is re-running the suite", "let it finish"]


def test_an_escalation_is_not_an_empty_speech_bubble():
    """`note` is empty by contract when the supervisor escalates (§2), so the verdict
    that reached no words must not open a bubble saying nothing."""
    assert build_conversation([ev("alarm_reviewed", 1.0, alarm_id="al-1",
                                  verdict="escalate", reason="cannot tell",
                                  note="")], []) == []


def test_every_alarm_status_has_a_reading_a_person_can_use():
    """All six, because the supervisor ships OFF and `raised` is therefore the common
    case — an example built from `acked` alone would grade the interesting one only.
    The ids are asserted too: they are what makes the line reachable rather than a
    count of things the reader cannot open."""
    from jarvis.ops import ALARM_STANDING, alarm_standing_line
    from jarvis.project_store import ALARM_STATUSES

    assert set(ALARM_STANDING) == set(ALARM_STATUSES)
    alarms = [{"id": f"al-{i}", "status": s} for i, s in enumerate(ALARM_STATUSES)]
    assert alarm_standing_line(alarms) == (
        "6 (1 raised, 1 with the supervisor, 1 acked by the supervisor, "
        "1 escalated to Neo, 1 not reviewed, 1 supervisor failed) — "
        "al-0, al-1, al-2, al-3, al-4, al-5")
    assert alarm_standing_line([{"id": "al-1a2b", "status": "acked"},
                                {"id": "al-3c4d", "status": "escalated"}]) == (
        "2 (1 acked by the supervisor, 1 escalated to Neo) — al-1a2b, al-3c4d")
    # The supervisor off, which is every fleet today.
    assert alarm_standing_line([{"id": "al-1a2b", "status": "raised"}]) == (
        "1 (1 raised) — al-1a2b")


def test_cli_wo_show_says_where_the_alarms_stand(jarvis_home, fake_claude,
                                                 catalog_file, capsys):
    """The header line, and the rows behind it. An order with no alarm keeps the
    document it had before the supervisor existed — which is most of them."""
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
    # The `wo_alarms` rows in full, not `ops.list_cost_alarms`' fleet-wide dict: this is
    # one order's own record and that dict's join columns are already on the document.
    assert [r["id"] for r in rows] == [acked["id"], escalated["id"]]
    assert rows[0]["note"] == "it is re-running the suite"
    assert rows[0]["seq"] == 3 and rows[0]["kind"] == "turn_minutes"
    assert "title" not in rows[0] and "alarm_status" not in rows[0]
