"""`jarvis inspect` — where a session's TIME went, and the alarm for a burning turn.

Two halves, and they are tested from opposite ends.

The REPORT is pinned against a real session, `tests/data/transcripts` — the planner of
fo-306b8f48, reduced to its skeleton by `scripts/redact_transcript.py`. Its three cache
writes are the reason this module exists (a cold start, a re-write twelve seconds after
the previous call, a re-write after a 450-second block) and no synthetic file would
reproduce them except by being told the answer. Every number asserted below was measured
from the unredacted transcript before this code existed.

The ALARM is tested synthetically, because what matters about it is not arithmetic but
restraint: it must fire once, on the turn that is still running, and never again after
the user has put it down.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jarvis import catalog, inspection, ops, usage
from jarvis.catalog import InspectConfig, load_catalog
from jarvis.daemon import Daemon

FIXTURE_ROOT = Path(__file__).parent / "data" / "transcripts"
FIXTURE_SESSION = "ec8236c7-b418-4f09-80f0-1edea61f099f"


@pytest.fixture()
def real_session(monkeypatch):
    """The committed skeleton of wo-5a6b2d6d's planner session."""
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(FIXTURE_ROOT))
    return inspection.read_session(FIXTURE_SESSION)


# -- the three findings the command exists to produce ----------------------------------


def test_the_wall_clock_splits_the_way_the_method_says(real_session):
    """`docs/findings/anatomy-of-an-expensive-turn.md`'s summary table, to the second:
    1886s of wall clock, 576s blocked on a subagent join, 58s executing tools.

    The question that started this: a DESIGN-ONLY agent with three 14-minute turns. A
    third of it was the lead agent asleep with no API call in flight, which is invisible
    to every token-based surface Jarvis had.
    """
    part = real_session.partition()

    assert round(part["wall"]) == 1886
    assert round(part["blocked"]) == 576
    assert round(part["tools"]) == 58
    assert sum(part[k] for k in inspection.PARTS) == pytest.approx(part["wall"])


def test_the_method_s_66_percent_is_generating_plus_idle(real_session):
    """THE ONE PLACE THIS DEPARTS FROM THE METHOD, and it is a decomposition rather than
    a disagreement. The method charges everything outside a tool span to the model, which
    on this session folds in 46 seconds during which no `claude -p` process existed —
    invisible here, and ELEVEN DAYS on a fleet turn whose work order was parked in
    `waiting_input`. Split out, the method's figure is still recoverable exactly."""
    part = real_session.partition()

    assert round(part["generating"] + part["idle"]) == 1252  # the method's number
    assert round(part["idle"]) == 46
    assert round((part["generating"] + part["idle"]) / part["wall"], 2) == 0.66


def test_each_turn_matches_the_method_s_per_turn_table(real_session):
    """§2: 865s / 136s blocked / 26s tools, then 885s / 440s / 23s, then 136s / 0 / 9s."""
    rows = [(round(t.wall), round(t.blocked), round(t.tools))
            for t in real_session.turns]

    assert rows == [(865, 136, 26), (885, 440, 23), (136, 0, 9)]
    # "Half of turn 2 was the lead agent doing nothing, holding a 193k context."
    assert round(real_session.turns[1].share()["blocked"], 2) == 0.50


def test_the_three_big_writes_are_told_apart_by_cause(real_session):
    """THE point of the command. Two of these are ~180k tokens each and look identical
    on a bill; one is the cache expiring and one is a defect, and they have completely
    different fixes."""
    start = real_session.turns[0].started
    at = {round((w.ts - start) / 60, 2): w for w in real_session.writes}
    assert sorted(at) == [0.04, 14.53, 23.57]

    cold, miss, expiry = at[0.04], at[14.53], at[23.57]
    assert (cold.written, cold.read, cold.cause) == (45_169, 0, inspection.COLD_START)
    # Twelve seconds. NOT the TTL — a resumed turn re-writing a prefix that is still warm.
    assert (miss.written, miss.read, miss.cause) == (157_098, 15_862,
                                                     inspection.PREFIX_MISS)
    assert round(miss.gap, 1) == 12.4
    # 450 seconds, and the cache is a 5-minute one: this one genuinely expired.
    assert (expiry.written, expiry.read, expiry.cause) == (193_139, 0,
                                                           inspection.TTL_EXPIRY)
    assert expiry.gap > inspection.TTL_5M


def test_every_blocking_join_is_named_by_what_it_waited_on(real_session):
    """A task id is not an answer. The join is resolved through the subagent meta file
    Claude Code writes beside the transcript."""
    joins = real_session.joins()

    assert [round(s.seconds) for s in joins] == [440, 136]
    assert "Test lead: acceptance for 6 children" in joins[0].detail
    assert "jarvis-architect" in joins[1].detail


def test_the_tool_profile_prices_reading_the_whole_codebase(real_session):
    """§3.6: 55 Bash calls at 0.8s each. Individually invisible, collectively 45 seconds
    — which is why optimising how an agent searches a codebase would save nothing."""
    rows = {r["name"]: r for r in real_session.tool_profile()}

    assert rows["Bash"]["calls"] == 55
    assert round(rows["Bash"]["seconds"], 1) == 45.6
    assert round(rows["Bash"]["mean"], 1) == 0.8
    # The summary table's "58s across 79 calls": the profile counts all 81 tool calls,
    # and the 2 joins are the ones charged to `blocked` rather than to tool execution.
    assert sum(r["calls"] for r in real_session.tool_profile()) == 81
    assert rows["TaskOutput"]["calls"] == 2
    assert rows["TaskOutput"]["seconds"] > rows["Bash"]["seconds"] * 10


def test_the_one_hour_cache_was_never_once_requested(real_session):
    """§3.2, and the finding is a ZERO — a number no surface stated before this one.

    THE METHOD'S ABSOLUTE TOTAL IS WRONG AND ITS CONCLUSION IS NOT. §3.2 reports
    1,797,566 cache-write tokens; that is the sum over transcript ROWS, and a single
    assistant message is written once per content block, so it counts the same API
    response up to three times (the trap `usage._assistant_messages` exists for). Deduped
    by message id the lead agent wrote 569,173. The ratio the finding rests on — 1h
    against 5m — is unaffected, because the duplicates inflate both sides equally.
    """
    split = real_session.cache_ttl()

    assert split["cache_1h"] == 0
    assert split["cache_5m"] == 569_173
    assert split["unknown"] == 0


def test_the_peak_context_is_reported_per_turn(real_session):
    """§1 step 1 asks for it per turn, and per turn is the only grain that answers "how
    large did the conversation get before that re-write"."""
    peaks = [t.context_peak for t in real_session.turns]

    assert peaks == sorted(peaks) and peaks[-1] == 233_585
    assert real_session.rewrite_excess() > 0


def test_each_turn_says_why_it_happened(real_session):
    """Quoted from the injected prompt. A turn Jarvis started for two reasons at once
    reports both — `Daemon.deliver_messages` coalesces, so one would be a false story."""
    kinds = [[t.kind for t in turn.triggers] for turn in real_session.turns]

    assert kinds == [["dispatch"],
                     ["a subagent finished", "a Neo answer"],
                     ["a subagent finished", "a message"]]
    assert real_session.turns[0].triggers[0].quote.startswith("You are the PLANNER")


def test_the_report_reads_the_same_session_jarvis_cost_does(real_session):
    """The two commands must cut the session at the same points or they cannot be laid
    beside each other. Every API call the bill counts lands in exactly one turn."""
    calls = usage.session_calls(FIXTURE_SESSION)

    assert sum(len(t.calls) for t in real_session.turns) == len(calls)


# -- the reader's own honesty ----------------------------------------------------------


def test_a_session_with_no_transcript_is_absent_not_empty(monkeypatch, tmp_path):
    """`jarvis cost`'s rule, held to: an unmeasurable clock and an idle one are
    different answers."""
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(tmp_path))

    anatomy = inspection.read_session("nothing-here")

    assert anatomy.found is False and anatomy.turns == []
    assert anatomy.partition()["wall"] == 0.0
    assert anatomy.cache_ttl() == {"cache_1h": 0, "cache_5m": 0, "unknown": 0}


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def prompt_row(at: float, text: str, *, sdk: bool = True, meta: bool = False) -> dict:
    row = {"type": "user", "timestamp": stamp(at), "message": {"content": text}}
    if sdk:
        row["promptSource"] = "sdk"
    if meta:
        row["isMeta"] = True
    return row


def assistant_row(at: float, mid: str, *, write: int = 0, read: int = 0,
                  content: list | None = None, ttl_1h: int = 0,
                  ttl_5m: int = 0) -> dict:
    counts: dict = {"input_tokens": 0, "cache_creation_input_tokens": write,
                    "cache_read_input_tokens": read, "output_tokens": 1}
    # Only written when a test asks for it: most rows in the wild predate the split, and
    # `one_hour_writes` has to read their absence as "not the expensive write".
    if ttl_1h or ttl_5m:
        counts["cache_creation"] = {"ephemeral_1h_input_tokens": ttl_1h,
                                    "ephemeral_5m_input_tokens": ttl_5m}
    return {
        "type": "assistant", "timestamp": stamp(at),
        "message": {"id": mid, "model": "claude-opus-5", "usage": counts,
                    "content": content or [{"type": "text", "text": "ok"}]},
    }


def tool_rows(start: float, end: float, tool_id: str, name: str,
              payload: dict | None = None) -> list[dict]:
    return [
        {"type": "assistant", "timestamp": stamp(start),
         "message": {"id": f"m-{tool_id}", "model": "claude-opus-5",
                     "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0, "output_tokens": 1},
                     "content": [{"type": "tool_use", "id": tool_id, "name": name,
                                  "input": payload or {}}]}},
        {"type": "user", "timestamp": stamp(end),
         "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]}},
    ]


@pytest.fixture()
def write_transcript(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, rows: list[dict], *, slug: str = "-proj",
              subagents: dict[str, list[dict]] | None = None) -> str:
        directory = root / slug
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
        for name, sub_rows in (subagents or {}).items():
            sub_dir = directory / session_id / "subagents"
            sub_dir.mkdir(parents=True, exist_ok=True)
            (sub_dir / f"{name}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in sub_rows))
        return session_id

    return write


def test_two_prompts_coalesced_into_one_turn_are_one_turn(write_transcript):
    """`Daemon.deliver_messages` sends everything queued as ONE turn. Counting prompts
    instead of turns would double the turn count of every work order that ever had a
    message arrive while a subagent was finishing."""
    session = write_transcript("coalesced", [
        prompt_row(1000, "<task-notification>x"),
        prompt_row(1000.02, "[Neo, answering for the user] yes"),
        assistant_row(1010, "m1"),
    ])

    anatomy = inspection.read_session(session)

    assert len(anatomy.turns) == 1
    assert [t.kind for t in anatomy.turns[0].triggers] == ["a subagent finished",
                                                           "a Neo answer"]


def test_a_human_typing_into_a_worker_session_starts_a_turn(write_transcript):
    """An injected or picked-up session has turns Jarvis never sent. Reading only the
    `sdk` ones fused a real session's last two days into one 'turn'."""
    session = write_transcript("adopted", [
        prompt_row(0, "You are the worker agent for wo-1"),
        assistant_row(10, "m1"),
        prompt_row(20, "is it still running?", sdk=False),
        assistant_row(30, "m2"),
    ])

    anatomy = inspection.read_session(session)

    assert [t.seq for t in anatomy.turns] == [1, 2]
    assert [t.triggers[0].source for t in anatomy.turns] == ["sdk", "user"]


def test_a_skill_loading_does_not_cut_a_turn_in_half(write_transcript):
    """`isMeta` rows are Claude Code talking to itself. One of them sits in the middle
    of the real fixture's second turn."""
    session = write_transcript("meta", [
        prompt_row(0, "You are the worker agent for wo-1"),
        assistant_row(10, "m1"),
        prompt_row(20, "Base directory for this skill: /x", sdk=False, meta=True),
        assistant_row(30, "m2"),
    ])

    assert len(inspection.read_session(session).turns) == 1


def test_a_turn_ends_at_its_last_api_call_not_at_a_row_written_days_later(
        write_transcript):
    """A transcript can be appended to long after its last call — an old-transport
    session picked up by hand under the same id. One such file charged a 21-minute turn
    with twelve days of wall clock, and every fleet percentile above the median was that
    gap rather than the work."""
    day = 86_400
    session = write_transcript("appended", [
        prompt_row(0, "You are the worker agent for wo-1"),
        assistant_row(60, "m1"),
        # No prompt row: the rows below are outside anything `usage` can count.
        {"type": "assistant", "timestamp": stamp(12 * day),
         "message": {"content": [{"type": "text", "text": "much later"}]}},
    ])

    (turn,) = inspection.read_session(session).turns

    assert turn.wall == pytest.approx(60, abs=1)
    assert turn.idle == 0.0  # nothing to be idle BETWEEN: there is no next turn


def test_an_unfinished_tool_call_is_counted_but_not_timed(write_transcript):
    """A turn killed mid-call. Reporting zero seconds would subtract exactly the time
    the reader is hunting for."""
    session = write_transcript("killed", [
        prompt_row(0, "You are the worker agent for wo-1"),
        {"type": "assistant", "timestamp": stamp(10),
         "message": {"id": "m1", "model": "claude-opus-5",
                     "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0, "output_tokens": 1},
                     "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                  "input": {"description": "never returned"}}]}},
    ])

    (turn,) = inspection.read_session(session).turns
    (row,) = inspection.read_session(session).tool_profile()

    assert turn.unfinished == 1 and turn.tools == 0.0
    assert (row["calls"], row["unfinished"], row["mean"]) == (1, 1, 0.0)


# -- a turn nothing was ever observed doing --------------------------------------------


def api_call(at: float, output: int = 1) -> usage.Call:
    """One real, billed API call — what the fifth bucket exists to require."""
    return usage.Call(ts=at, model="claude-opus-5", output=output)


def test_a_turn_with_no_api_call_is_not_reported_as_generating():
    """THE REGRESSION (issue 227). `generating` was the remainder of a four-way
    partition, so a turn that made no call and ran no tool had every second of its wall
    clock charged to it — rendering identically to a turn that really did generate for
    an hour. wo-f1ce0f24 turn 3: 65.3 minutes, `gen 100%`, 0 calls, peak 0, $0.00."""
    turn = inspection.Turn(seq=1, started=0.0, ended=65 * 60)

    assert turn.generating == 0.0
    assert turn.unaccounted == 65 * 60
    assert turn.share()["unaccounted"] == 1.0
    assert sum(turn.share().values()) == pytest.approx(1.0)


def test_a_long_turn_that_really_generated_still_says_generating():
    """THE NEGATIVE CONTROL, and the half that rots (kn-67364b3a): the new bucket must
    not be passing by swallowing every long turn. Same wall clock as the case above,
    with calls to vouch for it."""
    turn = inspection.Turn(seq=1, started=0.0, ended=65 * 60)
    turn.calls = [api_call(60.0), api_call(64 * 60)]
    turn.active_ended = 64 * 60

    assert turn.unaccounted == 0.0
    assert turn.generating == pytest.approx(64 * 60)
    assert turn.idle == pytest.approx(60)
    assert turn.share()["generating"] > 0.98


def test_the_partition_still_sums_to_the_wall_clock_with_five_buckets():
    """A percentage table that no longer adds to 100 is worse than the bug it replaced,
    and the fifth bucket is exactly the kind of addition that breaks one."""
    stalled = inspection.Turn(seq=1, started=0.0, ended=600.0)
    worked = inspection.Turn(seq=2, started=600.0, ended=1_200.0)
    worked.calls = [api_call(900.0)]
    worked.active_ended = 900.0
    anatomy = inspection.Anatomy(session_id="s", found=True, turns=[stalled, worked])

    part = anatomy.partition()

    assert sum(part[k] for k in inspection.PARTS) == pytest.approx(part["wall"])
    assert part["unaccounted"] == pytest.approx(600.0)


# -- classifying a cache write ---------------------------------------------------------


def call(at: float, write: int, read: int = 0, cache_1h: int = 0) -> usage.Call:
    return usage.Call(ts=at, model="claude-opus-5", cache_write=write,
                      cache_read=read, cache_1h=cache_1h)


def test_a_small_write_seconds_after_the_last_call_is_the_cache_working():
    """THE trap this classification has to avoid. Inside a turn every call writes the
    delta it just added — a short gap, so the gap test alone calls it a prefix-miss. The
    floor is what makes the label mean something."""
    calls = [call(0, 50_000), call(1, 900), call(2, 1_200)]
    floor = InspectConfig().report_write_floor

    causes = [w.cause for w in inspection.classify_writes(calls, floor)]

    assert causes == [inspection.COLD_START]


def test_a_one_hour_write_is_not_called_a_defect_for_surviving_ten_minutes():
    """Every Jarvis write before `FORCE_PROMPT_CACHING_5M` was a 1h write. Testing an
    old transcript against 300 seconds would report an honest expiry as a defect."""
    hour = [call(0, 50_000, cache_1h=50_000), call(600, 200_000)]
    five = [call(0, 50_000), call(600, 200_000)]
    floor = InspectConfig().report_write_floor

    assert inspection.classify_writes(hour, floor)[1].cause == inspection.PREFIX_MISS
    assert inspection.classify_writes(five, floor)[1].cause == inspection.TTL_EXPIRY


# -- the live alarm --------------------------------------------------------------------


def burning(*, wall: float = 0.0, write: int = 0, join: float = 0.0,
            calls: int = 1) -> tuple:
    """A one-turn anatomy that trips exactly the condition asked for.

    IT MAKES AN API CALL BY DEFAULT, because every alarm below it is about SPEND and a
    turn with no call has not spent anything (issue 227). `calls=0` is the stalled turn.
    """
    turn = inspection.Turn(seq=1, started=0.0, ended=wall)
    turn.calls = [api_call(1.0 + n) for n in range(calls)]
    if join:
        turn.spans.append(inspection.ToolSpan(name="TaskOutput", tool_id="t1",
                                              started=0.0, detail="a subagent"))
    anatomy = inspection.Anatomy(session_id="s", found=True, turns=[turn])
    if write:
        anatomy.writes.append(inspection.Write(ts=1.0, written=write, read=0, gap=5.0,
                                               cause=inspection.PREFIX_MISS))
    return anatomy, max(wall, join)


def test_each_threshold_raises_its_own_alarm():
    cfg = InspectConfig()

    long_turn, now = burning(wall=cfg.alarm_turn_minutes * 60 + 1)
    big_write, _ = burning(wall=60, write=cfg.alarm_write_tokens + 1)
    blocked, when = burning(join=cfg.alarm_join_seconds + 1)

    assert [a.kind for a in inspection.alarms(long_turn, cfg, now=now,
                                              dispatched=0.0)] == [inspection.TURN_ALARM]
    assert [a.kind for a in inspection.alarms(big_write, cfg, now=60,
                                              dispatched=0.0)] == [inspection.WRITE_ALARM]
    assert [a.kind for a in inspection.alarms(blocked, cfg, now=when,
                                              dispatched=0.0)] == [inspection.JOIN_ALARM]


def test_a_stalled_turn_is_alarmed_as_stalled_and_never_as_billed():
    """THE DETECTOR THAT SHOULD HAVE FIRED on wo-f1ce0f24 turn 3. `going-in-circles`
    said effort was being re-spent and the escalation said it was BILLED IN FULL; the
    turn had made no API call at all, so nothing had started and nothing was billed."""
    cfg = InspectConfig()
    stalled, now = burning(wall=cfg.alarm_stalled_minutes * 60 + 1, calls=0)

    raised = inspection.alarms(stalled, cfg, now=now, dispatched=0.0)

    assert [a.kind for a in raised] == [inspection.STALL_ALARM]
    assert "no API call" in raised[0].reason
    assert "billed" not in raised[0].reason


def test_a_stalled_turn_never_raises_the_long_turn_alarm():
    """The two are the same wall clock read two ways, and only one of them is true. The
    long-turn alarm's whole claim is that money is being spent."""
    cfg = InspectConfig()
    stalled, now = burning(wall=cfg.alarm_turn_minutes * 60 + 1, calls=0)

    kinds = [a.kind for a in inspection.alarms(stalled, cfg, now=now, dispatched=0.0)]

    assert kinds == [inspection.STALL_ALARM]


def test_the_stall_is_named_before_the_long_turn_alarm_could_call_it_spend():
    """A stall must be diagnosable while it is still a stall. Fifteen minutes of silence
    is already fifteen times the p99 time-to-first-call; the hour the long-turn alarm
    waits is another forty-five minutes of the user being told nothing."""
    assert InspectConfig().alarm_stalled_minutes < InspectConfig().alarm_turn_minutes


def test_the_long_turn_alarm_states_what_the_turn_actually_cost():
    """NO ALARM MAY ASSERT SPEND IT DID NOT READ. The per-turn cost record already
    existed and no layer consulted it — the supervisor asserted "BILLED IN FULL" in the
    same sentence as "zero context peak"."""
    cfg = InspectConfig()
    anatomy, now = burning(wall=cfg.alarm_turn_minutes * 60 + 1, calls=3)

    reason = inspection.alarms(anatomy, cfg, now=now, dispatched=0.0)[0].reason

    assert "3 API calls" in reason
    assert "$" in reason, "the claim is about money, so it carries the money"


def test_the_alarm_measures_the_running_turn_against_the_wall_clock():
    """A turn that is GENERATING has written no row for minutes. Judged against its own
    last line it reports how long ago it spoke, not how long it has been running."""
    anatomy, _ = burning(wall=60)
    cfg = InspectConfig(alarm_turn_minutes=10)

    assert inspection.alarms(anatomy, cfg, dispatched=0.0) == []
    assert inspection.alarms(anatomy, cfg, now=11 * 60,
                             dispatched=0.0)[0].kind == inspection.TURN_ALARM


def test_nothing_is_raised_about_a_turn_that_is_already_paid_for():
    """Only the LAST turn is judged. An alarm about spend the user cannot now prevent is
    the noise that gets a cost alarm ignored."""
    spent = inspection.Turn(seq=1, started=0.0, ended=9_999.0)
    quiet = inspection.Turn(seq=2, started=10_000.0, ended=10_060.0)
    anatomy = inspection.Anatomy(session_id="s", found=True, turns=[spent, quiet])

    assert inspection.alarms(anatomy, InspectConfig(), now=10_060.0, dispatched=0.0) == []


def test_a_join_still_open_past_the_cache_ttl_is_raised_from_the_live_file(
        write_transcript):
    """End to end, and the one threshold that is principled rather than empirical: past
    the TTL the prefix is cold, so the wait converts into a re-write. It is what the
    450-second block in the fixture bought."""
    session = write_transcript("blocked", [
        prompt_row(0, "You are the worker agent for wo-1"),
        assistant_row(5, "m1"),
        {"type": "assistant", "timestamp": stamp(10),
         "message": {"id": "m2", "model": "claude-opus-5",
                     "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0, "output_tokens": 1},
                     "content": [{"type": "tool_use", "id": "t1", "name": "TaskOutput",
                                  "input": {"task_id": "abc"}}]}},
    ])

    quiet = inspection.live_alarms(session, InspectConfig(), now=200, dispatched=0.0)
    loud = inspection.live_alarms(session, InspectConfig(), wo_id="wo-1", now=400,
                                  dispatched=0.0)


    assert quiet == []
    assert [a.kind for a in loud] == [inspection.JOIN_ALARM]
    assert "jarvis inspect wo-1" in loud[0].reason


def test_a_join_that_came_back_is_not_an_alarm(write_transcript):
    """Only an OPEN join. A subagent that has already returned cost what it cost and the
    turn moved on."""
    session = write_transcript("returned", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(10, 400, "t1", "TaskOutput", {"task_id": "abc"}),
    ])

    assert inspection.live_alarms(session, InspectConfig(), now=500, dispatched=0.0) == []


def synthetic_row(at: float, mid: str, text: str) -> dict:
    """The zero-token assistant message Claude Code writes ITSELF — the usage-limit
    notice, and the copy of it laid down beside the next prompt."""
    return {
        "type": "assistant", "timestamp": stamp(at),
        "message": {"id": mid, "model": usage.SYNTHETIC_MODEL,
                    "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0, "output_tokens": 0},
                    "content": [{"type": "text", "text": text}]},
    }


# wo-c83d7e93's real shape: 66 seconds of work at 08:22, the session limit, then the
# next turn dispatched at the 11:40 reset.
LIMIT_HIT, RESET = 30_133.0, 42_008.0


def session_limit_transcript(write_transcript, session: str = "limited") -> str:
    return write_transcript(session, [
        prompt_row(LIMIT_HIT, "You are the worker agent for wo-1"),
        *[assistant_row(LIMIT_HIT + 3 + i * 6, f"m{i}") for i in range(11)],
        synthetic_row(LIMIT_HIT + 64, "s1", "You've hit your session limit"),
        synthetic_row(RESET, "s2", "You've hit your session limit"),
        prompt_row(RESET + 2, "<task-notification> carry on"),
        assistant_row(RESET + 8, "m11"),
    ])


def test_a_turn_the_transcript_has_not_reached_yet_is_not_judged(write_transcript):
    """THE DISPATCH RACE, and it is the whole of the fleet's `long-turn` history. Between
    Jarvis opening the turn row and `claude` writing that turn's first row, the last
    transcript turn is still the PREVIOUS one, so `now - turn.started` measures the
    inter-turn gap: 197 minutes across a usage-limit window on three orders, 16.2 hours
    on a fourth parked overnight."""
    session = write_transcript("raced", [
        prompt_row(LIMIT_HIT, "You are the worker agent for wo-1"),
        assistant_row(LIMIT_HIT + 60, "m1"),
    ])
    cfg = InspectConfig()

    raced = inspection.live_alarms(session, cfg, now=RESET + 1, dispatched=RESET)
    stale = inspection.live_alarms(session, cfg, now=RESET + 1, dispatched=0.0)

    assert raced == [], "the turn being judged has written nothing yet"
    assert [a.kind for a in stale] == [inspection.TURN_ALARM], "and this was the bug"


def test_the_alarm_returns_once_the_dispatched_turn_is_in_the_transcript(
        write_transcript):
    """The guard is evidence, not suppression: the moment the new turn writes its prompt
    row it is judged, and against its OWN start."""
    session = session_limit_transcript(write_transcript)
    two_hours = RESET + 2 + 2 * 3600

    raised = inspection.live_alarms(session, InspectConfig(), wo_id="wo-1",
                                    now=two_hours, dispatched=RESET)

    assert [a.kind for a in raised] == [inspection.TURN_ALARM]
    assert "running 120 minutes" in raised[0].reason
    # "still being billed" now carries what it is billed FOR, so a turn wedged with no
    # call in flight cannot hide behind the wall clock.
    assert "1 API call, the last one 119m ago" in raised[0].reason


def test_a_synthetic_row_is_not_an_api_call_anywhere(write_transcript):
    """`<synthetic>` is Claude Code writing an assistant message itself and it was never
    billed, so counting one as a call misreports the turn three ways at once: the call
    count, the context peak, and — the one that matters — the moment it stopped working."""
    session = session_limit_transcript(write_transcript)

    first, second = inspection.read_session(session).turns

    assert (len(first.calls), first.usage.messages) == (11, 11)
    assert first.context_peak == 0  # the fixture's calls carry output only
    assert len(second.calls) == 1
    assert usage.session_calls(session) == first.calls + second.calls


def test_the_dead_gap_after_a_session_limit_is_idle_and_not_generating(
        write_transcript):
    """The acceptance case. The trailing `<synthetic>` two seconds before the next prompt
    dragged `active_ended` to the end of the turn, `idle` collapsed to zero, and `jarvis
    inspect` reported 197.8 minutes of GENERATING for 66 seconds of real work."""
    session = session_limit_transcript(write_transcript)

    first, _ = inspection.read_session(session).turns

    assert round(first.generating / 60) == 1
    assert round(first.idle / 60) == 197
    assert first.share()["idle"] > 0.99


def test_the_alarm_can_be_turned_off():
    anatomy, now = burning(wall=10 * 3600)

    assert inspection.alarms(anatomy, InspectConfig(enabled=False), now=now,
                             dispatched=0.0) == []


def test_the_defaults_are_the_measured_ones():
    """They are load-bearing: each was set where it fires on a small minority of the
    fleet's real work orders, and a change to one is a change to how often the user is
    interrupted. See `catalog.InspectConfig`."""
    cfg = InspectConfig()

    assert (cfg.alarm_turn_minutes, cfg.alarm_join_seconds,
            cfg.alarm_write_tokens) == (60, 300, 300_000)
    # p99 of time-to-first-API-call over the fleet's 4,543 turns is 61 seconds, so this
    # is fifteen times a slow start and fires on 0.26% of them.
    assert cfg.alarm_stalled_minutes == 15
    assert cfg.alarm_join_seconds == inspection.TTL_5M
    # The report is deliberately far more talkative than the alarm: the same blocking
    # join is worth a line at 30s and worth interrupting someone at 300s.
    assert (cfg.report_write_floor, cfg.report_join_floor) == (20_000, 30)
    assert cfg.report_join_floor < cfg.alarm_join_seconds
    assert cfg.report_write_floor < cfg.alarm_write_tokens


def test_nothing_in_the_module_hard_codes_a_threshold():
    """THE MAGIC-NUMBER GUARD, and it is the reason this test is worth its noise: the
    thresholds are policy and they belong in the catalog, so a later change that reaches
    for a literal instead of a setting fails here rather than in review.

    `TTL_5M`/`TTL_1H` are exempt and are the only exemption: they are the two durations
    Anthropic's cache actually offers, not a number anyone gets to choose.
    """
    import ast
    import inspect as stdlib_inspect

    tree = ast.parse(stdlib_inspect.getsource(inspection))
    allowed = {0, 1, 2, 4, 60, 300.0, 3600.0}  # indices, seconds-per-minute, the TTLs
    literals = {node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool)}

    assert not (literals - allowed), (
        f"undeclared numeric literal(s) {sorted(literals - allowed)} in inspection.py — "
        "a threshold belongs in catalog.InspectConfig, not in the code that reads it")


# -- configuration ---------------------------------------------------------------------


def test_the_thresholds_are_settable_through_the_config_console():
    """`os.inspect.*`, which `config_version.resolve` picks up reflectively — so
    `jarvis config set` reaches it with no edit to the console."""
    from jarvis import config_version

    cat = catalog.parse_catalog({"os": {"inspect": {"alarm_turn_minutes": 20}},
                                 "projects": []})
    resolved = config_version.resolve(cat)

    assert cat.os.inspect.alarm_turn_minutes == 20
    assert resolved["os.inspect.alarm_turn_minutes"] == 20
    # A default, materialised — which is what makes `jarvis config get` able to answer
    # for a key nobody has ever set.
    assert resolved["os.inspect.alarm_write_tokens"] == 300_000


def test_a_project_overrides_one_threshold_and_inherits_the_rest(tmp_path):
    """Field-level inheritance, the shape `_parse_validation` established (kn-6ca2bcd9).

    It matters here because a threshold is a claim about what is NORMAL, and normal
    differs by project: an hour-long turn is routine where the work is a design document
    and a symptom where it is a one-file fix.
    """
    from jarvis import config_version

    cat = catalog.parse_catalog({
        "os": {"inspect": {"alarm_turn_minutes": 90, "report_write_floor": 50_000}},
        "projects": [{"name": "quick", "path": str(tmp_path),
                      "inspect": {"alarm_turn_minutes": 15}}],
    })
    project = cat.project("quick")

    assert project.inspect.alarm_turn_minutes == 15        # its own
    assert project.inspect.report_write_floor == 50_000    # inherited from os
    assert project.inspect.alarm_write_tokens == 300_000   # inherited from the default
    assert cat.os.inspect.alarm_turn_minutes == 90         # the OS block is untouched
    assert config_version.resolve(cat)[
        "projects.quick.inspect.alarm_turn_minutes"] == 15


@pytest.mark.parametrize("key", ["alarm_write_tokens", "report_write_floor",
                                 "quote_chars", "alarm_turn_minutes",
                                 "alarm_stalled_minutes"])
def test_a_threshold_of_zero_is_refused_rather_than_flagging_everything(key):
    """Zero would report every write a session makes and flag every work order the fleet
    runs — and it arrives by a typo in a `jarvis config set`, so it is caught where the
    message can name the key."""
    with pytest.raises(catalog.CatalogError, match=key):
        catalog.parse_catalog({"os": {"inspect": {key: 0}}, "projects": []})


# -- the command, and the daemon -------------------------------------------------------


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def test_inspect_reports_a_work_order_by_its_session(started, monkeypatch):
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(FIXTURE_ROOT))
    wo = ops.create_work_order("proj_a", "plan the console")
    from jarvis.project_store import ProjectStore
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        store.update_work_order(wo["id"], session_id=FIXTURE_SESSION)
    finally:
        store.close()

    res = ops.inspect_report(wo["id"])

    (unit,) = res["units"]
    assert unit["found"] is True and unit["wo_id"] == wo["id"]
    assert [w["cause"] for w in unit["writes"]] == [inspection.COLD_START,
                                                    inspection.PREFIX_MISS,
                                                    inspection.TTL_EXPIRY]


def test_every_bucket_of_the_partition_has_a_label_on_the_page():
    """The renderers walk `PARTS` through these, so a bucket missing from either dict
    renders as silently absent and the percentages stop summing to 100 — the failure the
    fifth bucket was most likely to introduce (issue 227)."""
    from jarvis import cli

    assert tuple(cli.PART_LABELS) == inspection.PARTS
    assert tuple(cli.PART_SHORT) == inspection.PARTS


def test_the_dashboard_renders_no_partition_at_all():
    """THE THIRD SURFACE THE BRIEF NAMES, and the answer is that it does not exist: the
    dashboard reads alarms, never an `Anatomy`, so there is no four-way split under
    `src/jarvis/ui/` for a fifth bucket to go missing from.

    Pinned STRUCTURALLY rather than stated in a PR, because the claim is what rots: a
    later page that renders a turn's clock has to import something from `inspection`
    beyond the alarm labels, and that is the moment to key it off `PARTS` and add it to
    the pin above.
    """
    import ast
    from pathlib import Path

    imported: set[str] = set()
    ui = Path(inspection.__file__).parent / "ui"
    for path in sorted(ui.rglob("*.py")):
        source = path.read_text()
        assert "inspect_report" not in source, f"{path.name} reads an anatomy"
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                    "inspection"):
                imported |= {a.name for a in node.names}
            if isinstance(node, ast.ImportFrom) and node.module in (None, "", "."):
                imported |= {a.name for a in node.names if a.name == "inspection"}

    assert imported == {"ALARM_KINDS"}, (
        "the dashboard now reaches into `inspection` for more than the alarm labels — "
        "if it renders the partition, key it off `PARTS` and pin it like `PART_LABELS`")


def test_the_spend_line_refuses_to_claim_money_for_a_turn_with_no_calls():
    """The precondition in code, not only in the docstring. `alarms` branches on
    `Turn.observed` before it gets here, so this is for the SECOND caller: it must fail
    visibly rather than raise `ValueError` out of `max()` inside a reconcile tick."""
    empty = inspection.Turn(seq=1, started=0.0, ended=3_900.0)

    said = inspection._spend_so_far(empty, now=3_900.0)

    assert "NO API CALL" in said
    # And it names no figure, so nothing downstream can quote it as spend.
    assert "$" not in said and "token" not in said


def test_the_turn_line_says_no_api_call_before_it_says_anything_about_duration(
        capsys):
    """Part 2 of issue 227. `0 calls` and `peak 0` were already printed BESIDE a number
    that contradicted them and nothing reconciled the three."""
    from jarvis import cli

    stalled = inspection.Turn(seq=3, started=0.0, ended=65 * 60)
    anatomy = inspection.Anatomy(session_id="s", found=True, turns=[stalled])
    unit = {"wo_id": "wo-1", "title": "t", **anatomy.as_dict()}

    cli._print_anatomy(unit, InspectConfig().report_write_floor)
    line = next(l for l in capsys.readouterr().out.splitlines() if "turn  3" in l)

    assert line.index(cli.NO_CALL_FLAG) < line.index("gen")
    assert "unacc 100%" in line and "gen   0%" in line


def test_a_work_order_with_no_session_reports_no_transcript(started):
    wo = ops.create_work_order("proj_a", "never dispatched")

    (unit,) = ops.inspect_report(wo["id"])["units"]

    assert unit["found"] is False and unit["turns"] == []


def test_a_burning_turn_reaches_the_user_the_way_everything_else_does(
        started, monkeypatch, tmp_path):
    """The live half. The reconciler already ticks and `jarvis status` already has an
    attention list; a cost alarm does not get a channel of its own."""
    from jarvis.project_store import ProjectStore

    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))
    daemon = started
    wo = ops.create_work_order("proj_a", "the long one")

    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        # A turn row is opened `running` before the process is spawned, which is exactly
        # the state this check exists to look at.
        turn = store.create_turn(wo["id"], "dispatch", "go")
        started_at = turn["started_at"]
        (root / "-proj" / "burning.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            # AFTER the turn row, as `claude` writes it: a transcript turn older than the
            # dispatch is the PREVIOUS turn and `alarms` refuses to judge one.
            prompt_row(started_at + 1, "You are the worker agent for wo-1"),
            assistant_row(started_at + 5, "m1"),
        ]))
        store.update_work_order(wo["id"], status="running", session_id="burning")
        # Two hours in: past the 60-minute default and still billing.
        monkeypatch.setattr("jarvis.daemon.time.time", lambda: started_at + 2 * 3600)
        daemon.check_burning_turns(daemon.catalog.projects[0], store)

        flagged = store.get_work_order(wo["id"])
        events = store.events_of_kind(wo["id"], "cost_alarm")
        assert flagged["needs_attention"] == 1
        assert "still being billed" in flagged["attention_reason"]
        assert len(events) == 1

        # The row and the event are one raise: the row is the identity, the event is
        # the dedupe memory, and the event's payload points at the row.
        alarms = store.alarms_of(wo["id"])
        assert len(alarms) == 1
        assert alarms[0]["id"].startswith("al-")
        assert alarms[0]["status"] == "raised"
        assert alarms[0]["seq"] == 1
        assert json.loads(events[0]["payload"])["alarm_id"] == alarms[0]["id"]

        # AND NEVER AGAIN FOR THIS TURN. The user putting the flag down must not bring
        # the same sentence back on the next tick — that is how a cost alarm becomes
        # noise, and then it is worse than nothing.
        store.clear_attention(wo["id"])
        daemon.check_burning_turns(daemon.catalog.projects[0], store)

        assert store.get_work_order(wo["id"])["needs_attention"] == 0
        assert len(store.events_of_kind(wo["id"], "cost_alarm")) == 1
        # The half a single-tick test cannot see, and the one that costs a model call
        # per tick per alarm once the supervisor reads this table.
        assert len(store.alarms_of(wo["id"])) == 1
    finally:
        store.close()


def test_a_stalled_turn_reaches_the_user_as_a_stall_and_not_as_a_bill(
        started, monkeypatch, tmp_path):
    """END TO END, the case that was reported backwards (issue 227). The attention line
    is what became a Telegram saying an hour of generation had been billed."""
    from jarvis.project_store import ProjectStore

    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))
    daemon = started
    wo = ops.create_work_order("proj_a", "the dead one")

    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        at = turn["started_at"]
        # A prompt and nothing else: the turn opened and no API call was ever made.
        (root / "-proj" / "dead.jsonl").write_text(
            json.dumps(prompt_row(at + 1, "You are the worker agent for wo-1")) + "\n")
        store.update_work_order(wo["id"], status="running", session_id="dead")
        monkeypatch.setattr("jarvis.daemon.time.time", lambda: at + 2 * 3600)
        daemon.check_burning_turns(daemon.catalog.projects[0], store)

        flagged = store.get_work_order(wo["id"])
        (alarm,) = store.alarms_of(wo["id"])
        assert alarm["kind"] == inspection.STALL_ALARM
        assert "no API call at all" in flagged["attention_reason"]
        assert "billed" not in flagged["attention_reason"]
        assert "still being billed" not in flagged["attention_reason"]
    finally:
        store.close()


# -- the review surface: reading the alarms back, and answering one --------------------


def _burning(daemon, monkeypatch, tmp_path, title="the long one"):
    """One work order with a live `long-turn` alarm against it. Returns its id."""
    from jarvis.project_store import ProjectStore

    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))
    wo = ops.create_work_order("proj_a", title)
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        at = turn["started_at"]
        (root / "-proj" / f"{wo['id']}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in [
                prompt_row(at + 1, "You are the worker agent for wo-1"),
                assistant_row(at + 5, "m1"),
            ]))
        store.update_work_order(wo["id"], status="running", session_id=wo["id"])
        monkeypatch.setattr("jarvis.daemon.time.time", lambda: at + 2 * 3600)
        daemon.check_burning_turns(daemon.catalog.projects[0], store)
    finally:
        store.close()
    return wo["id"]


def test_an_alarm_can_be_read_back_off_the_fleet(started, monkeypatch, tmp_path):
    """`jarvis wo show` puts one alarm on one timeline. Reviewing them is the opposite
    question — which orders across the fleet have any — and `events_of_kind` cannot
    answer it without reading every work order there is."""
    wo_id = _burning(started, monkeypatch, tmp_path)

    rows = ops.list_cost_alarms()

    assert [r["wo_id"] for r in rows] == [wo_id]
    assert rows[0]["kind"] == inspection.TURN_ALARM
    assert rows[0]["project"] == "proj_a"
    assert rows[0]["seq"] == 1
    assert "still being billed" in rows[0]["reason"]
    # `live` is the ONE derived field: it is a property of the order, not of the event.
    assert rows[0]["live"] is True


def test_acking_answers_the_ask_and_keeps_the_record(started, monkeypatch, tmp_path):
    """The whole point of the alarm being a timeline event rather than a flag: the user
    can put the ask down without erasing what the fleet spent."""
    wo_id = _burning(started, monkeypatch, tmp_path)
    ops.ack_attention(wo_id, project_name="proj_a")

    rows = ops.list_cost_alarms()

    assert len(rows) == 1, "the alarm survives the ack"
    assert rows[0]["live"] is False, "but it has stopped asking"


def test_the_alarms_page_lists_the_live_one_and_offers_the_ack(
        started, monkeypatch, tmp_path):
    """The dashboard half of the same read. Asserted on the page a user actually gets,
    not on the context dict, because the ack button is the thing under test."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    wo_id = _burning(started, monkeypatch, tmp_path, title="a very slow design")
    client = TestClient(create_app(), follow_redirects=False)

    page = client.get("/alarms").text
    assert "a very slow design" in page
    assert "still being billed" in page
    assert f'action="/wo/proj_a/{wo_id}/ack"' in page
    assert 'name="back" value="alarms"' in page
    # The nav badge counts ORDERS, not events: several alarms on one turn are one ask.
    assert 'alarms<span class="nav-badge">1</span>' in page.replace(" <span", "<span")

    ack = client.post(f"/wo/proj_a/{wo_id}/ack", data={"back": "alarms"})
    assert ack.status_code == 303
    assert ack.headers["location"] == "/alarms", "back to the queue, not to the order"

    after = client.get("/alarms").text
    assert "nothing is burning" in after, "the ask is gone"
    assert wo_id in after, "and the record is not"


def test_acking_from_the_order_s_own_page_still_lands_there(
        started, monkeypatch, tmp_path):
    """`back` must not change the behaviour of the button that was already there."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    wo_id = _burning(started, monkeypatch, tmp_path)
    client = TestClient(create_app(), follow_redirects=False)

    ack = client.post(f"/wo/proj_a/{wo_id}/ack")

    assert ack.headers["location"] == f"/wo/proj_a/{wo_id}"


def test_the_cli_answers_the_same_question_as_the_page(
        started, monkeypatch, tmp_path, capsys):
    """The CLI is the OS: no dashboard surface may be the only way to read something."""
    from jarvis import cli

    wo_id = _burning(started, monkeypatch, tmp_path)

    assert cli.main(["alarms"]) == 0

    out = capsys.readouterr().out
    assert wo_id in out
    assert "1 asking for you" in out
    assert "jarvis wo ack" in out


def test_every_alarm_has_an_id_and_one_work_order_s_can_be_read_alone(
        started, monkeypatch, tmp_path, capsys):
    """PR 159 shipped an alarm with no identity, so "link to THIS alarm" was not
    expressible. `--wo` is the read a worker makes when it writes its pull request."""
    from jarvis import cli

    wo_id = _burning(started, monkeypatch, tmp_path, title="the burning one")
    other = _burning(started, monkeypatch, tmp_path, title="the other one")

    rows = ops.list_cost_alarms(wo_id=wo_id)

    assert [r["wo_id"] for r in rows] == [wo_id], "the other order's alarm is not here"
    assert rows[0]["id"].startswith("al-")
    # Frozen for sections 2, 3 and 5, which are written against these keys.
    assert rows[0]["alarm_status"] == "raised"
    assert (rows[0]["verdict"], rows[0]["note"], rows[0]["neo_question_id"]) == (
        None, None, None)
    assert rows[0]["review_status"] == "unreviewed"

    assert cli.main(["alarms", "--wo", wo_id]) == 0
    out = capsys.readouterr().out
    assert rows[0]["id"] in out
    assert other not in out

    # And the listing that was already there renders as it did before: the alarm id is
    # section 4's to put on the surfaces, not this one's.
    assert cli.main(["alarms"]) == 0
    every = capsys.readouterr().out
    assert wo_id in every and other in every
    assert "al-" not in every


def test_the_alarm_status_is_not_the_thing_the_page_calls_live(
        started, monkeypatch, tmp_path):
    """`live` is a property of the ORDER's attention flag. Deriving it from the row's
    own status instead would make an answered alarm disappear from the ask before the
    user had put the flag down — and leave it there after they had."""
    from jarvis.project_store import ProjectStore

    wo_id = _burning(started, monkeypatch, tmp_path)
    path = ops.find_work_order(wo_id)[1]
    store = ProjectStore(path)
    try:
        alarm = store.alarms_of(wo_id)[0]
        store.update_alarm(alarm["id"], status="acked", verdict="ack",
                           note="a design document; the hour is normal here")
    finally:
        store.close()

    rows = ops.list_cost_alarms()

    assert rows[0]["alarm_status"] == "acked"
    assert rows[0]["verdict"] == "ack"
    assert rows[0]["live"] is True, "the order is still flagged, so it is still an ask"

    ops.ack_attention(wo_id, project_name="proj_a")
    assert ops.list_cost_alarms()[0]["live"] is False


# -- who is still buying the one-hour write --------------------------------------------
#
# Finding 3 of docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md. The
# value of this reading is entirely in the SPLIT: the same tokens mean a defect in this
# OS or a person's own session, and those have unrelated fixes.

#: Before every timestamp below, so `since` is inert unless a test says otherwise.
ALL_OF_IT = -1.0


def test_one_transcript_holding_both_halves_is_split_per_turn(write_transcript):
    """THE CASE THE DESIGN TURNS ON, and it is wo-2df8828c's real shape: a dispatched
    worker session whose worktree was reopened by hand after the order completed, so one
    file and one session id hold both. Paired — a reader that classified the FILE rather
    than each turn would satisfy either half alone."""
    write_transcript("both", [
        prompt_row(0, "You are the worker agent for wo-1"),
        assistant_row(10, "m1", write=900, ttl_1h=900),
        prompt_row(20, "carry on, I'll drive", sdk=False),
        assistant_row(30, "m2", write=4_000, ttl_1h=4_000),
    ])

    (entry,) = inspection.one_hour_writes(ALL_OF_IT)

    assert (entry.dispatched, entry.foreign) == (900, 4_000)
    assert entry.total == 4_900 and entry.directory == "-proj"


def test_a_coalesced_turn_with_one_typed_prompt_is_not_jarvis_s(write_transcript):
    """`Daemon.deliver_messages` coalesces, so a turn has several triggers. One of them
    typed means a person was at the keyboard, and `dispatched` is a claim about the
    PROCESS — getting it wrong accuses the OS of a defect it does not have."""
    write_transcript("mixed", [
        prompt_row(0, "<task-notification>done"),
        prompt_row(0.02, "actually, do this instead", sdk=False),
        assistant_row(10, "m1", write=2_000, ttl_1h=2_000),
    ])

    (entry,) = inspection.one_hour_writes(ALL_OF_IT)

    assert (entry.dispatched, entry.foreign) == (0, 2_000)


def test_a_subagent_inherits_its_parents_classification_either_way(write_transcript):
    """Subagent transcripts carry NO `promptSource` — measured over every one on this
    machine, 1,002 user rows and not a single field. So classifying them on their own
    evidence reads every subagent as a person's, which would hide the breach this exists
    to catch. Both directions in one test, because inheriting only one of them is
    indistinguishable from a constant."""
    write_transcript("dispatched-parent", [
        prompt_row(0, "You are the worker agent for wo-1"),
        assistant_row(10, "m1", write=10, ttl_1h=10),
    ], subagents={"agent-a": [assistant_row(12, "s1", write=2_000, ttl_1h=2_000)]})
    write_transcript("typed-parent", [
        prompt_row(0, "have a look at this", sdk=False),
        assistant_row(10, "m1", write=10, ttl_1h=10),
    ], slug="-other",
        subagents={"agent-b": [assistant_row(12, "s2", write=3_000, ttl_1h=3_000)]})

    split = {e.session_id: (e.dispatched, e.foreign)
             for e in inspection.one_hour_writes(ALL_OF_IT)}

    assert split == {"dispatched-parent": (2_010, 0), "typed-parent": (0, 3_010)}


def test_an_unknown_ttl_is_not_counted_as_the_expensive_one(write_transcript):
    """A row written before Claude Code reported the split has no `cache_creation` at
    all. Reading that absence as 1h would fire this on every old transcript in the fleet
    — `usage`'s module note calls the 5-minute rate the floor, and so does this."""
    write_transcript("cheap", [
        prompt_row(0, "hi", sdk=False),
        assistant_row(10, "m1", write=8_000, ttl_5m=8_000),
        assistant_row(20, "m2", write=9_000),  # no split reported at all
    ])
    write_transcript("leaky", [
        prompt_row(0, "hi", sdk=False),
        assistant_row(10, "m1", write=10, ttl_1h=10),
    ], slug="-other")

    assert [e.session_id for e in inspection.one_hour_writes(ALL_OF_IT)] == ["leaky"]


def test_since_cuts_on_the_write_not_on_the_session(write_transcript):
    """A session outlives a window: wo-2df8828c's was still being written to four days
    after its work order completed. So one old write and one new one report only the
    new one, and the span reported is the WRITES' own."""
    day = 86_400
    write_transcript("long-lived", [
        prompt_row(0, "hi", sdk=False),
        assistant_row(10, "m1", write=7_000, ttl_1h=7_000),
        assistant_row(5 * day, "m2", write=300, ttl_1h=300),
    ])

    (recent,) = inspection.one_hour_writes(2 * day)
    assert (recent.foreign, recent.first_ts, recent.last_ts) == (300, 5 * day, 5 * day)
    assert inspection.one_hour_writes(ALL_OF_IT)[0].foreign == 7_300


def test_the_biggest_offender_is_first(write_transcript):
    """The caller quotes the top of this list into an alarm, so the order is the ranking
    by what it cost rather than whatever the filesystem returned."""
    write_transcript("small", [
        prompt_row(0, "hi", sdk=False),
        assistant_row(10, "m1", write=50, ttl_1h=50),
    ])
    write_transcript("big", [
        prompt_row(0, "hi", sdk=False),
        assistant_row(10, "m1", write=9_000, ttl_1h=9_000),
    ], slug="-other")

    assert [e.session_id for e in inspection.one_hour_writes(ALL_OF_IT)] == \
        ["big", "small"]
