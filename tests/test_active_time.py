"""The ACTIVE clock: wall time minus what the OS itself was holding the order for.

The defect these exist for was a REPORT, not a crash. On 2026-09-18 `jarvis inspect`
read two running work orders as 59% and 67% "between turns, nothing running" over six
and five hours and Jarvis put that to the user as waste. The user's correction: "I hit
the claude limit in between, so that wall time is not actually net time." The account's
usage window had been spent for four of those hours and the OS was holding both orders
exactly as designed — so the number was right and the meaning was wrong, which is the
failure mode that teaches a user to stop reading alarms.

`holds` derives the hold windows from the OS's own timeline, and every number asserted
below for the two production shapes was measured off the real record before this code
existed (`tests/test_active_time.py::PRODUCTION`).
"""

from __future__ import annotations

import json

import pytest
from test_cost_report import registered  # noqa: F401 — a project `ops` can resolve
from test_inspection import (assistant_row, prompt_row,  # noqa: F401 — fixture by name
                             tool_rows, write_transcript)

from jarvis import holds, inspection, invariants, ops
from jarvis.catalog import InspectConfig
from jarvis.project_store import ProjectStore

#: The two production shapes, from `wo_turns` and `wo_events` on 2026-09-18. Both hit the
#: limit MID-TURN — after 69 and 43 minutes of real work — which is why the hold is the
#: gap between the refused turn ending and its retry starting, and not a turn that never
#: ran. Reproduced as the fixtures below.
PRODUCTION = {
    # wo-7e08ac40 turn 5: 308.5m wall, 78% idle, 26 API calls, peak 320k.
    # worked 09:39:30 -> 10:48:38 (69.1m), held to 14:48:02 (239.4m).
    "wo-7e08ac40": {"wall": 308.5, "worked": 69.1, "held": 239.4,
                    "reset": "resets 2:10pm (America/Los_Angeles)"},
    # wo-16a488ee turn 1: 240.1m wall, 82% idle, 185 API calls, peak 367k.
    # worked 10:10:01 -> 10:52:47 (42.8m), held to 14:10:04 (197.3m).
    "wo-16a488ee": {"wall": 240.1, "worked": 42.8, "held": 197.3,
                    "reset": "resets 2:10pm (America/Los_Angeles)"},
}

MINUTE = 60.0
#: An arbitrary epoch the fixtures hang off. Big enough that nothing reads as "unset".
T0 = 1_700_000_000.0


class Record:
    """A work order's own record — turns and timeline — built a fact at a time.

    Written against the REAL store rather than by handing `holds.held` a list, because
    what is under test is the pairing of timeline events the rest of the OS writes. A
    hand-built list of `Hold`s would pass whatever `_OPEN` and `_CLOSE` happened to say.
    """

    def __init__(self, store: ProjectStore, wo_id: str) -> None:
        self.store, self.wo_id, self.seq = store, wo_id, 0

    def turn(self, started: float, ended: float | None,
             state: str = "done") -> int:
        """One turn of the conversation, placed on the clock.

        The timestamps are written with SQL rather than through `create_turn` for the
        reason `test_bill` and `test_auth_failure` already do it: the store stamps
        `db.now()` and there is no back-dating API, so a fixture that needs a turn to
        have happened four hours ago has to say so afterwards.
        """
        self.seq += 1
        row = self.store.create_turn(self.wo_id, kind="message", prompt="go")
        if ended is not None:
            self.store.finish_turn(row["id"], state=state)
        self.store.conn.execute(
            "UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
            (started, ended, row["id"]))
        return self.seq

    def event(self, kind: str, at: float, payload: dict) -> None:
        self.store.add_event(self.wo_id, kind, payload)
        self.store.conn.execute(
            "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events)", (at,))


@pytest.fixture()
def record(registered):
    """An empty work order with a store to hang turns and timeline events on.

    On the REGISTERED project so `ops.inspect_report` can resolve it by id — the report
    is half of what is under test here and it goes through the same lookup the CLI does.
    """
    store = ProjectStore(registered)
    wo = store.create_work_order(title="t", description="d")
    try:
        yield Record(store, wo["id"]), store, wo["id"]
    finally:
        store.close()


def usage_limit_shape(rec: Record, shape: dict) -> tuple[float, float]:
    """One production shape on the record: a turn that worked, hit the limit, and resumed.

    Returns the moments the held turn started and its retry began, so a transcript can be
    laid over exactly the same clock.
    """
    worked, held = shape["worked"] * MINUTE, shape["held"] * MINUTE
    refused_at = T0 + worked
    resumed_at = refused_at + held
    seq = rec.turn(T0, refused_at, state="failed")
    rec.event("turn_paused", refused_at,
              {"seq": seq, "reason": "usage_limit",
               "error": f"You've hit your session limit · {shape['reset']}"})
    rec.turn(resumed_at, None)
    rec.event("turn_resumed", resumed_at,
              {"seq": seq + 1, "retried_seq": seq, "reason": "usage_limit"})
    return refused_at, resumed_at


# -- the clock itself ------------------------------------------------------------------


@pytest.mark.parametrize("wo_id", sorted(PRODUCTION))
def test_the_hold_is_read_off_the_timeline_not_guessed_from_the_gap(record, wo_id):
    """Both production shapes, to the second. The window is `turn_paused` ->
    `turn_resumed`, which is the OS's own record of the decision it made — the work order
    that commissioned this said in as many words not to re-derive it from timestamps."""
    rec, store, wo = record
    shape = PRODUCTION[wo_id]
    refused_at, resumed_at = usage_limit_shape(rec, shape)

    spans = holds.held(store, wo, now=resumed_at)

    assert [h.cause for h in spans] == [holds.PAUSE_USAGE_LIMIT]
    assert (spans[0].started, spans[0].ended) == (refused_at, resumed_at)
    assert round((spans[0].finish() - spans[0].started) / MINUTE, 1) == shape["held"]


def test_a_hold_that_has_not_ended_runs_to_now(record):
    """The live case, and the only one a running alarm is ever judged against. A hold
    with no closing event is not missing data — it is the window still being shut."""
    rec, store, wo = record
    seq = rec.turn(T0, T0 + 60, state="failed")
    rec.event("turn_paused", T0 + 60, {"seq": seq, "reason": "usage_limit",
                                       "error": "session limit · resets 2pm"})

    spans = holds.held(store, wo, now=T0 + 60 + 4 * 3600)

    assert spans[0].open is True
    assert spans[0].ended is None
    assert spans[0].overlap(T0, T0 + 60 + 4 * 3600, T0 + 60 + 4 * 3600) == 4 * 3600


def test_a_hold_overlapping_a_running_turn_is_clipped_away(record):
    """A message queued while the worker is MID-TASK waits for the turn to end, and those
    seconds are the order working. Subtracting them would delete the work — the opposite
    of the mistake this module exists to fix."""
    rec, store, wo = record
    rec.turn(T0, T0 + 3600)                      # an hour of real work
    rec.event("message_queued", T0 + 600, {"msg_id": 7})   # queued ten minutes in
    rec.event("message_delivered", T0 + 4200, {"msg_ids": [7], "turn": 2})

    spans = holds.held(store, wo, now=T0 + 4200)

    # The 60 minutes inside the turn are gone; only the 10 after it survive.
    assert [(h.cause, h.finish() - h.started) for h in spans] == [(holds.MESSAGE, 600.0)]


def test_the_transport_outranks_the_message_it_was_blocking(record):
    """wo-16a488ee's 197-minute hold reported as "a message waiting to be delivered"
    until `_RANK` existed. `Daemon.deliver_messages` holds a queued message for exactly
    as long as the transport is paused, so the two open on the same reap — and naming the
    message is a true sentence that hides the only useful one."""
    rec, store, wo = record
    seq = rec.turn(T0, T0 + 3600, state="failed")
    rec.event("message_queued", T0 + 600, {"msg_id": 7})
    rec.event("turn_paused", T0 + 3600, {"seq": seq, "reason": "usage_limit",
                                         "error": "session limit · resets 2pm"})
    rec.turn(T0 + 7200, None)
    rec.event("turn_resumed", T0 + 7200, {"seq": seq + 1, "retried_seq": seq})
    rec.event("message_delivered", T0 + 7200, {"msg_ids": [7], "turn": 2})

    spans = holds.held(store, wo, now=T0 + 7200)

    assert [h.cause for h in spans] == [holds.PAUSE_USAGE_LIMIT]
    assert spans[0].finish() - spans[0].started == 3600.0


def test_two_overlapping_causes_are_counted_once(record):
    """Otherwise the per-cause figures sum past the total and a reader who adds them up
    is told the OS lost track of an afternoon."""
    rec, store, wo = record
    rec.turn(T0, T0 + 60)
    rec.event("gate_requested", T0 + 60, {"approval_id": 1, "kind": "pr_merge"})
    rec.event("question_asked", T0 + 120, {"neo_question_id": 9})
    rec.event("neo_answered", T0 + 900, {"neo_question_id": 9})
    rec.event("gate_decided", T0 + 600, {"approval_id": 1, "decision": "approved"})

    spans = holds.held(store, wo, now=T0 + 900)
    total = sum(h.finish(T0 + 900) - h.started for h in spans)

    assert total == 840.0, "T0+60 to T0+900 once, not 540 + 780"
    assert holds.by_cause(spans, T0, T0 + 900, T0 + 900) == {holds.GATE: 540.0,
                                                             holds.NEO_QUESTION: 300.0}


# -- what the alarm now measures -------------------------------------------------------


def _worked_rows(shape: dict) -> list[dict]:
    """The turn doing its real work: a prompt, then an API call a minute for `worked`."""
    worked = shape["worked"] * MINUTE
    return [prompt_row(T0, "You are the worker agent for wo-1"),
            *(assistant_row(T0 + 60 + n * 60, f"m{n}")
              for n in range(int(worked // 60)))]


def held_turn(shape: dict, write_transcript, session: str) -> tuple[str, float, float]:
    """The SETTLED shape: a turn that worked, was held, and whose successor has arrived.

    The successor's prompt is what gives the first turn an `ended` past the hold, which
    is what made `wo-7e08ac40` turn 5 read as 308.5 minutes of wall clock. This is what
    `jarvis inspect` sees afterwards.
    """
    worked, held = shape["worked"] * MINUTE, shape["held"] * MINUTE
    rows = _worked_rows(shape) + [
        prompt_row(T0 + worked + held, "[Jarvis] Your previous turn was cut short"),
        assistant_row(T0 + worked + held + 30, "m-after")]
    return write_transcript(session, rows), T0 + worked, T0 + worked + held


def live_turn(shape: dict, write_transcript, session: str) -> tuple[str, float, float]:
    """The LIVE shape: the same turn, read WHILE the hold is still on.

    No successor row, because the retry has not happened yet — so the last transcript
    turn is the held one and its clock has to be measured against the wall rather than
    against its own last line. This is what `Daemon.check_burning_turns` sees, and the
    only shape an alarm is ever judged on.
    """
    worked, held = shape["worked"] * MINUTE, shape["held"] * MINUTE
    return (write_transcript(session, _worked_rows(shape)),
            T0 + worked, T0 + worked + held)


@pytest.mark.parametrize("wo_id", sorted(PRODUCTION))
def test_a_turn_spanning_a_usage_limit_hold_reports_the_active_clock(
        record, write_transcript, wo_id):
    """THE FIX, on both production shapes. wo-7e08ac40 turn 5 read 308.5 minutes of wall
    clock and worked 69.1 of them; wo-16a488ee turn 1 read 240.1 and worked 42.8. Neither
    number changes — what changes is that the report can now say which is which."""
    rec, store, wo = record
    shape = PRODUCTION[wo_id]
    usage_limit_shape(rec, shape)
    session, _refused, resumed = held_turn(shape, write_transcript, "held")

    spans = holds.held(store, wo, now=resumed)
    turn = inspection.read_session(session, spans=spans).turns[0]

    assert round(turn.wall / MINUTE, 1) == shape["wall"]
    assert round(turn.held / MINUTE, 1) == shape["held"]
    assert round(turn.active / MINUTE, 1) == shape["worked"]
    assert turn.held_by() == {holds.PAUSE_USAGE_LIMIT: pytest.approx(
        shape["held"] * MINUTE)}


def test_the_held_turn_raises_no_alarm_and_the_same_turn_unheld_does(
        record, write_transcript):
    """THE PAIR THAT MATTERS, and the second half is why the first cannot pass by never
    alarming. One transcript, one threshold, one difference: whether the OS has a record
    of holding the order.

    wo-16a488ee turn 1 is the shape that proves it, because it worked 42.8 minutes — under
    the hour `alarm_turn_minutes` allows — and then sat 197.3 minutes on a spent usage
    window. Judged on the wall clock it is a four-hour turn and shouts; judged on the time
    it was allowed to work it is a 43-minute turn and says nothing.
    """
    rec, store, wo = record
    shape = PRODUCTION["wo-16a488ee"]
    usage_limit_shape(rec, shape)
    session, _refused, resumed = live_turn(shape, write_transcript, "live")
    cfg = InspectConfig()
    now = resumed - MINUTE   # still inside the hold, which is when this is judged

    spans = holds.held(store, wo, now=now)
    quiet = inspection.live_alarms(session, cfg, now=now, dispatched=T0, spans=spans)
    loud = inspection.live_alarms(session, cfg, now=now, dispatched=T0)

    assert quiet == [], "an order the OS is holding is not an order burning money"
    assert [a.kind for a in loud] == [inspection.TURN_ALARM], "and this was the defect"
    assert shape["worked"] < cfg.alarm_turn_minutes, \
        "the fixture only proves anything while its ACTIVE time is under the threshold"


def test_the_alarm_still_fires_on_a_turn_that_genuinely_worked_for_an_hour(
        record, write_transcript):
    """The other direction: subtracting a hold must not buy an expensive turn an alibi.

    wo-7e08ac40 turn 5 is the shape that proves THAT, and it is why the pair above uses
    the other order: it worked 69.1 minutes before the limit stopped it. It really did
    raise `long-turn` on 2026-09-18, with 25 API calls and $12.89 behind it, and that
    alarm was correct. With its whole 239.4-minute hold on the record it still fires.
    """
    rec, store, wo = record
    shape = PRODUCTION["wo-7e08ac40"]
    usage_limit_shape(rec, shape)
    session, _refused, resumed = live_turn(shape, write_transcript, "live")
    cfg = InspectConfig()
    now = resumed - MINUTE

    raised = inspection.live_alarms(session, cfg, now=now, dispatched=T0,
                                    spans=holds.held(store, wo, now=now))

    assert [a.kind for a in raised] == [inspection.TURN_ALARM]
    assert "running 69 minutes" in raised[0].reason
    assert shape["worked"] > cfg.alarm_turn_minutes


def test_time_blocked_on_a_subagent_is_never_subtracted(record, write_transcript):
    """THE EXPLICIT EXCLUSION. wo-7e08ac40 spent 130.3 minutes blocked on its own
    subagents and that is the order's own choice and real cost — exactly what an alarm
    should still see. It falls out of the clip rather than needing a rule: a join happens
    INSIDE a running turn, and nothing inside a running turn can be held."""
    rec, store, wo = record
    blocked = 130.3 * MINUTE
    rec.turn(T0, T0 + blocked + 120)
    # A hold recorded across the very same seconds, so the test fails if the clip goes.
    rec.event("message_queued", T0 + 60, {"msg_id": 3})
    rec.event("message_delivered", T0 + 60 + blocked, {"msg_ids": [3], "turn": 1})
    session = write_transcript("joined", [
        prompt_row(T0, "You are the worker agent for wo-1"),
        *tool_rows(T0 + 60, T0 + 60 + blocked, "t1", "TaskOutput",
                   {"task_id": "abc"}),
        assistant_row(T0 + 60 + blocked + 30, "m1"),
    ])

    spans = holds.held(store, wo, now=T0 + blocked + 120)
    turn = inspection.read_session(session, spans=spans).turns[0]

    assert round(turn.blocked / MINUTE, 1) == pytest.approx(130.3, abs=0.1)
    assert turn.held == 0.0
    assert turn.active == turn.wall


# -- what the user reads ---------------------------------------------------------------


def test_the_report_renders_both_clocks_and_names_the_cause(
        record, write_transcript, capsys):
    """"held 3.9h by a fleet usage limit" is the sentence that would have saved the whole
    exchange. THE WALL CLOCK IS NOT DELETED — it is the honest answer to "how long did
    this take in the real world", which is what the user asked in the first place."""
    from jarvis import cli

    rec, store, wo = record
    shape = PRODUCTION["wo-7e08ac40"]
    usage_limit_shape(rec, shape)
    session, _refused, resumed = held_turn(shape, write_transcript, "held")
    store.update_work_order(wo, session_id=session)

    report = ops.inspect_report(wo)
    cli._print_anatomy(report["units"][0], report["write_floor"])
    out = capsys.readouterr().out

    assert "wall clock" in out and "active" in out
    assert "HELD by a fleet usage limit" in out
    unit = report["units"][0]
    # The shape is a fact about ONE turn; the partition above it sums the session, which
    # here is that turn plus the 30 seconds its successor has run so far.
    turn = unit["turns"][0]
    assert round(turn["wall"] / MINUTE, 1) == shape["wall"]
    assert round(turn["active"] / MINUTE, 1) == shape["worked"]
    assert round(turn["held"] / MINUTE, 1) == shape["held"]
    assert f"{shape['worked']}m active — held {shape['held']}m by a fleet usage limit" \
        in out
    assert round(unit["partition"]["held"] / MINUTE, 1) == shape["held"]


def test_the_residual_is_the_idle_no_hold_explains(record, write_transcript):
    """The question behind all of this: once the holds come out, is there idle LEFT?
    `unexplained` is exact rather than an estimate, because `held` is clipped to the gaps
    between turns and is therefore a subset of `idle` by construction."""
    rec, store, wo = record
    shape = PRODUCTION["wo-7e08ac40"]
    usage_limit_shape(rec, shape)
    session, _refused, resumed = held_turn(shape, write_transcript, "held")

    anatomy = inspection.read_session(session, spans=holds.held(store, wo, now=resumed))

    assert anatomy.unexplained == pytest.approx(
        sum(t.idle for t in anatomy.turns) - anatomy.held)
    assert anatomy.held <= sum(t.idle for t in anatomy.turns)


def test_a_reading_taken_without_the_record_holds_nothing(write_transcript):
    """`read_session` is called from a test, a `--json` consumer and the daemon alike and
    has never opened the OS's database. Given no spans it must report the turn it always
    did — `active == wall` — rather than claim a hold it cannot see."""
    shape = PRODUCTION["wo-7e08ac40"]
    session, _refused, _resumed = held_turn(shape, write_transcript, "bare")

    turn = inspection.read_session(session).turns[0]

    assert turn.held == 0.0
    assert turn.active == turn.wall


def test_the_alarm_sentence_carries_both_clocks(record, write_transcript):
    """A user who meets the difference in an attention line and then runs `jarvis inspect`
    must read the same sentence twice, not two accounts of one fact."""
    rec, store, wo = record
    shape = PRODUCTION["wo-7e08ac40"]
    usage_limit_shape(rec, shape)
    session, _refused, resumed = live_turn(shape, write_transcript, "live")
    # A threshold low enough that the ACTIVE hour still trips it, so the sentence exists.
    cfg = InspectConfig(alarm_turn_minutes=30)
    now = resumed - MINUTE

    raised = inspection.live_alarms(session, cfg, now=now, dispatched=T0,
                                    spans=holds.held(store, wo, now=now))

    assert [a.kind for a in raised] == [inspection.TURN_ALARM]
    assert "on the wall clock" in raised[0].reason
    assert "held by a fleet usage limit" in raised[0].reason


def test_the_supervisor_is_told_about_the_hold_before_it_judges(
        record, write_transcript):
    """The evidence packet is read by a MODEL deciding whether to spend the user's
    attention. A turn reported as 18,510 seconds of wall clock with no mention that
    14,364 of them were a spent usage window is the OS asking for a verdict on a
    condition it created itself — the self-healing standard, from the other side."""
    from jarvis import supervisor

    rec, store, wo = record
    shape = PRODUCTION["wo-7e08ac40"]
    usage_limit_shape(rec, shape)
    session, _refused, _resumed = held_turn(shape, write_transcript, "held")
    store.update_work_order(wo, session_id=session)
    row = store.get_work_order(wo)

    lines = supervisor._session_lines(row, InspectConfig(), store)
    blind = supervisor._session_lines(row, InspectConfig())

    assert any("HELD by a fleet usage limit" in line for line in lines)
    assert any("of it active" in line for line in lines)
    assert not any("HELD" in line for line in blind), \
        "without the record it must report the wall clock, not invent a hold"


# -- what the report must never carry ---------------------------------------------------

#: A gate command of the shape this repository's workers really propose. The token sits at
#: the FRONT of the URL, which is why `[:160]` looked like a defence and was not
#: (kn-1791a5e6). Fake, and shaped like the real thing on purpose.
TOKENISED_PUSH = ("git push https://x-access-token:ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
                  "@github.com/acme/proj.git HEAD:main")


def test_a_gate_command_never_reaches_the_timing_report(record, write_transcript):
    """A GATE'S COMMAND IS A CREDENTIAL CARRIER AND A TIMING REPORT IS NOT THE PLACE FOR IT.

    `gate_requested` records the privileged shell line a worker proposed, which in this
    repository routinely embeds a tokenised remote. An earlier draft of `holds` lifted it
    into `Hold.detail` and published it through `jarvis inspect --json`, truncated to 160
    characters — and truncation is not a defence when the credential is at the front of
    the URL. Nobody expects to redact a timing report before pasting it into a PR body.
    """
    rec, store, wo = record
    rec.turn(T0, T0 + 60)
    rec.event("gate_requested", T0 + 60,
              {"approval_id": 1, "kind": "pr_merge", "command": TOKENISED_PUSH})
    rec.event("gate_decided", T0 + 600,
              {"approval_id": 1, "decision": "approved", "reason": TOKENISED_PUSH})
    session, _refused, _resumed = held_turn(PRODUCTION["wo-7e08ac40"],
                                            write_transcript, "held")
    store.update_work_order(wo, session_id=session)

    payload = json.dumps(ops.inspect_report(wo))

    assert "ghp_" not in payload
    assert "x-access-token" not in payload
    # The hold itself is still reported — this is a redaction, not a blind spot.
    assert holds.GATE in json.loads(payload)["units"][0]["held_by"]


def test_a_hold_publishes_a_fixed_set_of_fields_and_no_free_text(record):
    """THE GUARD ON THE FUNCTION RATHER THAN ON THIS MONTH'S CALLERS (kn-1791a5e6).

    Every text a hold could quote — a refusal, a worker's question, a gate's command — is
    text this OS does not control, so the rule is structural: what `as_dict` publishes is
    an id, three numbers and a sentence from `HOLD_CAUSES`, which is our own fixed table.
    A new key here has to be added to this list deliberately, with this test asking why.
    """
    rec, store, wo = record
    rec.turn(T0, T0 + 60, state="failed")
    rec.event("turn_paused", T0 + 60, {"seq": 1, "reason": "usage_limit",
                                       "error": TOKENISED_PUSH})

    published = holds.held(store, wo, now=T0 + 3600)[0].as_dict(T0 + 3600)

    assert set(published) == {"cause", "phrase", "started", "ended", "open", "seconds"}
    assert published["phrase"] in holds.HOLD_CAUSES.values()
    assert "ghp_" not in json.dumps(published)


def test_every_hold_cause_has_a_phrase_for_the_user():
    """A cause missing from `HOLD_CAUSES` renders as a bare slug in an attention line —
    `PART_LABELS`' rule, and the same pin, because the two tables fail the same way."""
    named = set(holds.HOLD_CAUSES)
    opened = {c for c, _ in holds._OPEN.values() if c is not None} | holds.TRANSPORT
    closed = {c for causes, _, _ in holds._CLOSE.values() for c in causes}

    assert opened <= named and closed <= named
    assert set(holds._RANK) == named


# -- the two branches that fail silently ------------------------------------------------
#
# Both of these stop a work order being reported, so their failure mode is silence rather
# than a wrong number: a hold whose closer never arrives runs to `now` for ever, and the
# order it belongs to disappears off the attention list permanently. Every case below is
# therefore a PAIR — held and unheld, or the cause and its near neighbour — so neither
# branch can pass by never reporting anything.


#: Four hours held, then five quiet minutes of nobody's making. Chosen so the unheld
#: reading is over `alarm_parked_minutes` (60) and the held one is well under it: a margin
#: either side, rather than a number that happens to land on the threshold.
PARKED_HELD = 4 * 3600
PARKED_RESIDUAL = 300.0


def parked_shape(rec: Record, store: ProjectStore, wo: str) -> float:
    """An order whose worker settled a turn, and which was then held for four hours.

    The dispatch after that turn was refused before a process existed — `kn-fa875823`'s
    instant opening refusal, which writes `turn_paused` and no turn row — so the LATEST
    turn is still the settled one and `parked_reason` reaches its threshold test. That is
    the only shape in which this suppression can ever matter: a paused order whose latest
    turn is `failed` is turned away several lines earlier, and `ops.waiting_on` would
    answer `retry_pending` for it besides.

    Returns the moment to read the record at.
    """
    seq = rec.turn(T0, T0 + 60)
    store.set_status(wo, "running")
    rec.event("turn_paused", T0 + 60,
              {"seq": seq, "reason": "usage_limit",
               "error": "You've hit your session limit · resets 2:10pm"})
    rec.event("turn_resumed", T0 + 60 + PARKED_HELD,
              {"seq": seq + 1, "retried_seq": seq, "reason": "usage_limit"})
    return T0 + 60 + PARKED_HELD + PARKED_RESIDUAL


def test_an_order_held_for_four_hours_is_not_reported_as_parked(record):
    """The suppression itself. Four hours and five minutes of silence is well past
    `alarm_parked_minutes`, and all but five minutes of it was the OS holding the order —
    which is the whole correction this work order exists for, applied to the attention
    list rather than to the alarm."""
    rec, store, wo = record
    now = parked_shape(rec, store, wo)

    assert invariants.parked_reason(store, store.get_work_order(wo), now=now) is None


def test_the_same_record_without_the_hold_events_is_still_reported(record):
    """The other half, and the one that makes the first mean something. The record is
    IDENTICAL — same turn, same statuses, same `now` — but for the two events, so a
    suppression that had simply stopped reporting parked orders would fail here."""
    rec, store, wo = record
    now = parked_shape(rec, store, wo)
    store.conn.execute(
        "DELETE FROM wo_events WHERE wo_id=? AND kind IN ('turn_paused','turn_resumed')",
        (wo,))

    assert (invariants.parked_reason(store, store.get_work_order(wo), now=now)
            == invariants.PARKED_BLOCKER)


def test_a_validation_round_held_by_the_usage_window_holds_the_work_order(record):
    """`VALIDATION_HELD_CAUSE`: the panel could not be run because the window was spent.

    The only hold with no opening event of its own. `Daemon` closes the round `failed`
    and there is no `turn_paused` behind it, because no worker turn was ever launched —
    so `_episodes` synthesises the usage-limit span at the failure and ends it at the
    next submission, which is the OS reopening the round itself. Without it the four
    hours read as the work order idling.
    """
    rec, store, wo = record
    rec.turn(T0, T0 + 60)
    rec.event("validation_submitted", T0 + 120, {"round": 1})
    rec.event("validation_failed", T0 + 180,
              {"round": 1, "cause": "usage_limit", "reopens_at": T0 + 14_580})
    rec.event("validation_submitted", T0 + 14_580, {"round": 2})
    rec.event("validation_passed", T0 + 14_700, {"round": 2})

    spans = holds.held(store, wo, now=T0 + 14_800)

    assert [(h.cause, h.started, h.ended) for h in spans] == [
        (holds.VALIDATION, T0 + 120, T0 + 180),        # round 1, submitted -> failed
        (holds.PAUSE_USAGE_LIMIT, T0 + 180, T0 + 14_580),   # the window, synthesised
        (holds.VALIDATION, T0 + 14_580, T0 + 14_700),  # round 2, submitted -> passed
    ]


def test_a_validation_round_that_merely_failed_synthesises_no_usage_hold(record):
    """The near neighbour, because the branch turns on one payload key. A round that
    failed for any other reason — an outage, a validator crash — is the round ending and
    nothing holding the work order after it. Reading `cause` loosely here would hold
    every failed round open to `now` and silently retire the order from the attention
    list, which is the failure mode this pair exists to catch."""
    rec, store, wo = record
    rec.turn(T0, T0 + 60)
    rec.event("validation_submitted", T0 + 120, {"round": 1})
    rec.event("validation_failed", T0 + 180, {"round": 1, "cause": "outage"})

    spans = holds.held(store, wo, now=T0 + 14_800)

    assert [(h.cause, h.started, h.ended) for h in spans] == [
        (holds.VALIDATION, T0 + 120, T0 + 180)]
    assert all(h.cause != holds.PAUSE_USAGE_LIMIT for h in spans)


def test_a_held_round_never_resubmitted_is_still_reported_as_open(record):
    """The live case, stated rather than discovered. A synthesised hold whose closer has
    not arrived runs to `now`, exactly like every other open hold — which is right while
    the window is genuinely shut, and is the reason the pair above matters: if the branch
    ever fired on the wrong `cause`, THIS is the shape it would leave behind."""
    rec, store, wo = record
    rec.turn(T0, T0 + 60)
    rec.event("validation_submitted", T0 + 120, {"round": 1})
    rec.event("validation_failed", T0 + 180, {"round": 1, "cause": "usage_limit"})

    spans = holds.held(store, wo, now=T0 + 14_800)

    assert spans[-1].cause == holds.PAUSE_USAGE_LIMIT
    assert spans[-1].open is True
    assert spans[-1].finish(T0 + 14_800) == T0 + 14_800
