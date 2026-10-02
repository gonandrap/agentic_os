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
import os
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
    1886s of wall clock, 582s blocked on a subagent join, 52s executing tools.

    CORRECTED from 576/58 by issue #845: the session's two foreground `Agent` spans (2.8s
    and 2.9s) are a JOIN and were being counted as tools the lead ran. Six seconds move
    between two buckets; the wall clock and the finding's conclusion are unchanged.

    The question that started this: a DESIGN-ONLY agent with three 14-minute turns. A
    third of it was the lead agent asleep with no API call in flight, which is invisible
    to every token-based surface Jarvis had.
    """
    part = real_session.partition()

    assert round(part["wall"]) == 1886
    assert round(part["blocked"]) == 582
    assert round(part["tools"]) == 52
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
    """§2: 865s / 139s blocked / 23s tools, then 885s / 443s / 20s, then 136s / 0 / 9s.

    CORRECTED from 136/26 and 440/23 for the reason above (issue #845): each of the first
    two turns delegated once in the foreground, and those ~3s are a wait, not a tool.
    """
    rows = [(round(t.wall), round(t.blocked), round(t.tools))
            for t in real_session.turns]

    assert rows == [(865, 139, 23), (885, 443, 20), (136, 0, 9)]
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


def awaiting(*, since: float, wall: float, calls: int = 0) -> tuple:
    """A one-turn anatomy whose last row is an input — a request is in flight."""
    turn = inspection.Turn(seq=1, started=since, ended=since + wall)
    turn.awaiting_since = since
    turn.calls = [api_call(since + 1.0 + n) for n in range(calls)]
    return inspection.Anatomy(session_id="s", found=True, turns=[turn]), since + wall


def test_a_turn_whose_last_row_is_an_input_is_awaiting_and_not_stalled():
    """Spec §1: the absence of an assistant row is a request in flight, not a dead turn."""
    cfg = InspectConfig()
    anatomy, now = awaiting(since=1_000.0, wall=cfg.alarm_awaiting_minutes * 60 - 60)

    assert anatomy.turns[0].awaiting is True
    assert inspection.alarms(anatomy, cfg, now=now, dispatched=0.0) == []


def test_past_the_awaiting_threshold_exactly_one_informational_alarm_is_raised():
    """Spec §2 branch 1."""
    cfg = InspectConfig()
    anatomy, now = awaiting(since=1_000.0, wall=cfg.alarm_awaiting_minutes * 60 + 60)

    raised = inspection.alarms(anatomy, cfg, now=now, dispatched=0.0)

    assert [a.kind for a in raised] == [inspection.SLOW_RESPONSE_ALARM]
    assert raised[0].reason.startswith(inspection.awaiting_note(anatomy.turns[0]))
    assert "no API call" not in raised[0].reason
    assert inspection.SLOW_RESPONSE_ALARM in inspection.INFORMATIONAL_KINDS


def test_a_billed_turn_awaiting_a_tool_result_still_raises_the_long_turn_alarm():
    """THE TWO ARE INDEPENDENT, NOT EXCLUSIVE. `awaiting_since` is set on every
    `tool_result` row, so a healthy turn 27 calls deep is awaiting at the sampling
    moment — an `elif` here would make `long-turn` unraisable on most running turns."""
    cfg = InspectConfig()
    anatomy, now = awaiting(since=1_000.0, wall=cfg.alarm_turn_minutes * 60 + 60,
                            calls=3)
    # Awaiting only since the last tool result, well inside the awaiting threshold.
    anatomy.turns[0].awaiting_since = now - 60

    kinds = [a.kind for a in inspection.alarms(anatomy, cfg, now=now, dispatched=0.0)]

    assert kinds == [inspection.TURN_ALARM]


def test_a_long_billed_turn_can_raise_both():
    """Both findings are true at once: it is being billed AND the current request has
    completed nothing for an hour."""
    cfg = InspectConfig()
    anatomy, now = awaiting(since=1_000.0, wall=cfg.alarm_turn_minutes * 60 + 60,
                            calls=3)

    kinds = [a.kind for a in inspection.alarms(anatomy, cfg, now=now, dispatched=0.0)]

    assert kinds == [inspection.TURN_ALARM, inspection.SLOW_RESPONSE_ALARM]


def test_the_awaiting_clock_is_the_active_one():
    """The user's 2026-09-18 ruling: a held order is obeying, not answering slowly."""
    cfg = InspectConfig()
    anatomy, now = awaiting(since=1_000.0, wall=cfg.alarm_awaiting_minutes * 60 + 60)
    anatomy.holds = [inspection.Hold(cause="usage_window", started=1_000.0, ended=now)]

    assert inspection.alarms(anatomy, cfg, now=now, dispatched=0.0) == []


def test_the_stalled_turn_alarm_is_never_raised_live():
    """Spec §2 branch 2: the evidence for "the work never started" was an absence, and
    the absence is what this spec proves unreliable. `worker_session.TURN_STALL_SECONDS`
    judges the PROCESS and still catches a genuinely hung turn."""
    cfg = InspectConfig()
    stalled, now = burning(wall=cfg.alarm_turn_minutes * 60 + 1, calls=0)

    assert inspection.alarms(stalled, cfg, now=now, dispatched=0.0) == []


def test_a_historical_stalled_turn_row_still_renders():
    """Spec §2: the kind and its legend stay for the rows already in `wo_alarms`."""
    row = {"kind": inspection.STALL_ALARM, "source": "cost"}

    assert ops.alarm_kind_label(row, {}) == "stalled-turn"
    assert "was open" in inspection.ALARM_KINDS[inspection.STALL_ALARM]


def test_awaiting_is_derived_in_the_walk_read_transcript_already_does(write_transcript):
    """Spec §1: no second pass, no new file read."""
    session = write_transcript("s", [
        prompt_row(1_000.0, "You are the worker agent for wo-1"),
        assistant_row(1_030.0, "m1"),
        prompt_row(1_100.0, "carry on"),
    ])

    first, second = inspection.read_session(session).turns

    assert first.awaiting is False and first.awaiting_since == 0.0
    assert second.awaiting is True and second.awaiting_since == 1_100.0


def test_a_tool_result_reopens_the_wait(write_transcript):
    """Spec §1: the model is answering again after every tool_result too."""
    session = write_transcript("s", [
        prompt_row(1_000.0, "You are the worker agent for wo-1"),
        *tool_rows(1_030.0, 1_060.0, "t1", "Bash"),
    ])

    (turn,) = inspection.read_session(session).turns

    assert turn.awaiting is True and turn.awaiting_since == 1_060.0


def test_the_turn_dict_carries_awaiting_additively():
    """Spec §1: the rule `subagents` was added under."""
    turn = inspection.Turn(seq=1, started=1_000.0, ended=1_060.0)
    turn.awaiting_since = 1_000.0

    said = turn.as_dict()

    assert said["awaiting"] is True and said["awaiting_since"] == 1_000.0
    assert said["observed"] is False


def test_the_awaiting_sentence_is_written_once_for_every_surface():
    """Spec §1: one formatter, so no surface invents a second vocabulary."""
    turn = inspection.Turn(seq=1, started=1_000.0, ended=1_060.0)
    turn.awaiting_since = 1_000.0

    said = inspection.awaiting_note(turn)

    assert said.startswith("awaiting the model since ")
    assert "request in flight, no block completed" in said


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


# -- a foreground delegation is a WAIT, and a hung subagent call is the alarm -----------
#
# docs/superpowers/specs/2026-09-29-a-runaway-tool-call-is-not-a-slow-subagent.md,
# issue #845.


def test_a_foreground_delegation_is_a_wait_and_not_a_tool_the_lead_ran(write_transcript):
    """Spec §1. `Agent` returns immediately only when the subagent is BACKGROUNDED; a
    foreground call blocks the lead until the subagent returns, so its seconds are
    `blocked` and not `tools`."""
    session = write_transcript("delegated", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(10, 130, "t1", "Agent", {"description": "write the spec"}),
    ])
    anatomy = inspection.read_session(session)
    (turn,) = anatomy.turns
    (span,) = turn.spans

    assert span.is_join is True
    assert anatomy.joins(0) == [span]
    assert (turn.blocked, turn.tools) == (120.0, 0.0)


def test_a_backgrounded_agent_is_not_a_wait(write_transcript):
    """The other half of §1, and why `backgrounded` is read from the RAW `tool_use`
    input: `run_in_background: true` hands the wait to `TaskOutput`."""
    session = write_transcript("backgrounded", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(10, 130, "t1", "Agent",
                   {"description": "write the spec", "run_in_background": True}),
    ])
    anatomy = inspection.read_session(session)
    (turn,) = anatomy.turns
    (span,) = turn.spans

    assert span.is_join is False
    assert anatomy.joins(0) == []
    assert (turn.blocked, turn.tools) == (0.0, 120.0)


def delegation_transcript(write_transcript, *, session: str, sub_rows: list[dict],
                          background: bool = False, label: str = "") -> str:
    """A lead whose foreground `Agent` span is still open, plus one subagent transcript.

    The `Agent` input carries a `description` and no task id, which is what Claude Code
    actually writes — so the subagent is UNATTACHED and the alarm has to scan the
    session's anatomies rather than one turn's children.
    """
    inp: dict = {"description": "write the spec", "subagent_type": "jarvis-spec-writer"}
    if background:
        inp["run_in_background"] = True
    written = write_transcript(session, [
        prompt_row(0, "You are the worker agent for wo-1"),
        assistant_row(10, "m2", content=[{"type": "tool_use", "id": "t1",
                                          "name": "Agent", "input": inp}]),
    ], subagents={"agent-a7b62083": sub_rows})
    if label:
        root = Path(os.environ[usage.TRANSCRIPT_ROOT_ENV])
        (root / "-proj" / session / "subagents" / "agent-a7b62083.meta.json").write_text(
            json.dumps({"agentType": label, "description": "write the spec"}))
    return written


def healthy_sub_rows() -> list[dict]:
    return [prompt_row(100, "write the spec"),
            *tool_rows(110, 130, "s1", "Read", {"file_path": "/x.py"})]


HUNG_PATTERN = r"^def .*\n(.*\n)*?.*return"


def hung_sub_rows(at: float = 130.0) -> list[dict]:
    return [prompt_row(100, "write the spec"),
            *tool_rows(110, 120, "s1", "Read", {"file_path": "/x.py"}),
            assistant_row(at, "s-m2", content=[
                {"type": "tool_use", "id": "s2",
                 "name": "mcp__serena__search_for_pattern",
                 "input": {"substring_pattern": HUNG_PATTERN}}])]


def test_a_working_subagent_raises_nothing_however_long_the_delegation(write_transcript):
    """Spec §2 test 1, THE REGRESSION GUARD: 43 of the fleet's 272 foreground
    delegations run past 20 minutes and nearly all of them are real work. Elapsed wait
    alone is not evidence of a hang."""
    session = delegation_transcript(write_transcript, session="working",
                                    sub_rows=healthy_sub_rows())

    assert inspection.live_alarms(session, InspectConfig(), wo_id="wo-1", now=311,
                                  dispatched=0.0) == []


def test_a_backgrounded_delegation_is_never_considered_by_the_join_loop(write_transcript):
    """Spec §2 test 2. Not a join at all, so the hang evidence is never even read — the
    wait it defers is `TaskOutput`'s."""
    session = delegation_transcript(write_transcript, session="bg-hung",
                                    sub_rows=hung_sub_rows(), background=True)
    anatomy = inspection.read_session(session)
    (span,) = anatomy.turns[0].spans

    assert span.is_join is False
    assert inspection.live_alarms(session, InspectConfig(), wo_id="wo-1", now=500,
                                  dispatched=0.0) == []


def test_a_hung_subagent_tool_call_raises_one_join_alarm_that_names_it(write_transcript):
    """Spec §2 tests 3 and §2c: the alarm names the hung TOOL and the subagent it found,
    which is what issue #845 asks for."""
    cfg = InspectConfig()
    session = delegation_transcript(write_transcript, session="hung",
                                    sub_rows=hung_sub_rows(),
                                    label="jarvis-spec-writer")
    now = 130 + cfg.alarm_subagent_tool_minutes * 60 + 1

    raised = inspection.live_alarms(session, cfg, wo_id="wo-1", now=now, dispatched=0.0)

    assert [a.kind for a in raised] == [inspection.JOIN_ALARM]
    assert "mcp__serena__search_for_pattern" in raised[0].reason
    assert "jarvis-spec-writer" in raised[0].reason
    # The pattern itself, through `_detail_of` — the bound and the redaction already in
    # place, and the thing the user needs to see to know what to stop writing.
    assert HUNG_PATTERN[:20] in raised[0].reason
    assert "jarvis inspect wo-1" in raised[0].reason


def test_a_subagent_tool_call_under_the_threshold_is_not_a_hang(write_transcript):
    """The threshold is a threshold: one minute into an MCP call is normal."""
    session = delegation_transcript(write_transcript, session="slow-not-hung",
                                    sub_rows=hung_sub_rows())

    assert inspection.live_alarms(session, InspectConfig(), wo_id="wo-1", now=190,
                                  dispatched=0.0) == []


def test_task_output_still_alarms_on_elapsed_wait_alone(write_transcript):
    """Spec §2a, unchanged byte for byte: a pure wait with no subagent transcript of its
    own to inspect, judged on the 5-minute cache TTL."""
    cfg = InspectConfig()
    session = write_transcript("collected", [
        prompt_row(0, "You are the worker agent for wo-1"),
        assistant_row(10, "m2", content=[{"type": "tool_use", "id": "t1",
                                          "name": "TaskOutput",
                                          "input": {"task_id": "a7b62083"}}]),
    ], subagents={"agent-a7b62083": healthy_sub_rows()})

    raised = inspection.live_alarms(session, cfg, wo_id="wo-1",
                                    now=10 + cfg.alarm_join_seconds + 1, dispatched=0.0)

    assert [a.kind for a in raised] == [inspection.JOIN_ALARM]
    assert "blocked" in raised[0].reason


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
    # The hang evidence behind a foreground delegation's join alarm (issue #845). ONE
    # opinion about how long a single tool call may take: the same boundary as
    # `alarm_join_seconds` and as the `MCP_TOOL_TIMEOUT` every worker is launched with.
    assert cfg.alarm_subagent_tool_minutes == 5
    assert cfg.alarm_subagent_tool_minutes * 60 == cfg.alarm_join_seconds
    # p99 of time-to-first-API-call over the fleet's 4,543 turns is 61 seconds, so this
    # is fifteen times a slow start and fires on 0.26% of them.
    assert cfg.alarm_stalled_minutes == 15
    # The measured slow response was 19 minutes, so anything under the hour would
    # re-raise the very case the spec exists to stop reporting.
    assert cfg.alarm_awaiting_minutes == 60
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

    WHAT IS EXEMPT IS EVERYTHING THAT IS NOT A CHOICE. `TTL_5M`/`TTL_1H` are the two
    durations Anthropic's cache actually offers; `1e6` is the unit its prices are quoted
    in. Neither is a number anyone gets to set, so neither belongs in a catalog — and
    listing them here rather than widening the rule is what keeps the rule meaning
    something. `NAMED_SESSIONS` is the one judgement call in the list: it bounds how much
    of a list an alarm's prose carries, which is a display decision like
    `DEFAULT_INSPECT_QUOTE_CHARS` and not a condition anything fires on.
    """
    import ast
    import inspect as stdlib_inspect

    tree = ast.parse(stdlib_inspect.getsource(inspection))
    allowed = {0, 1, 2, 4, 60, 300.0, 3600.0,   # indices, seconds-per-minute, the TTLs
               1e6,                             # tokens per million: the price unit
               inspection.NAMED_SESSIONS,       # a display bound, see the docstring
               # A measurement of the order two files are written in (spec 2026-09-27
               # §3), not a judgement anyone gets to set — so not a catalog setting.
               inspection.TURN_BIND_TOLERANCE_SECONDS,
               # The parameter caps (spec §4a) are a STRUCTURAL bound on how big one
               # report may get, not a per-project judgement about what is expensive —
               # no catalog wants its own answer to "may this print a megabyte". 6 and
               # 20 are `autoreview`'s credential-length tests, copied with the shapes.
               # Same reasoning for the leaf walk's depth bound: a guard against blowing
               # the stack on a worker's JSON, not a project's choice (review round 2).
               6, 20, inspection._PARAM_WALK_MAX_DEPTH,
               *inspection.PARAM_CAPS.as_dict().values()}
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
        "os": {"inspect": {"alarm_turn_minutes": 90, "report_write_floor": 50_000,
                           "alarm_awaiting_minutes": 120}},
        "projects": [{"name": "quick", "path": str(tmp_path),
                      "inspect": {"alarm_turn_minutes": 15}}],
    })
    project = cat.project("quick")

    assert project.inspect.alarm_subagent_tool_minutes == 5  # inherited from the default
    assert project.inspect.alarm_awaiting_minutes == 120   # inherited from os
    assert project.inspect.alarm_turn_minutes == 15        # its own
    assert project.inspect.report_write_floor == 50_000    # inherited from os
    assert project.inspect.alarm_write_tokens == 300_000   # inherited from the default
    assert cat.os.inspect.alarm_turn_minutes == 90         # the OS block is untouched
    assert config_version.resolve(cat)[
        "projects.quick.inspect.alarm_turn_minutes"] == 15


def test_the_hang_threshold_is_settable_fleet_wide_and_per_project(tmp_path):
    """Spec §2 test 5. A project whose MCP server is genuinely slow raises it; the rest
    of `InspectConfig` still inherits field by field."""
    from jarvis import config_version

    cat = catalog.parse_catalog({
        "os": {"inspect": {"alarm_subagent_tool_minutes": 10}},
        "projects": [{"name": "slow-mcp", "path": str(tmp_path),
                      "inspect": {"alarm_subagent_tool_minutes": 20}}],
    })

    assert cat.os.inspect.alarm_subagent_tool_minutes == 10
    assert cat.project("slow-mcp").inspect.alarm_subagent_tool_minutes == 20
    assert cat.project("slow-mcp").inspect.alarm_join_seconds == 300   # inherited
    assert config_version.resolve(cat)[
        "projects.slow-mcp.inspect.alarm_subagent_tool_minutes"] == 20


@pytest.mark.parametrize("key", ["alarm_write_tokens", "report_write_floor",
                                 "quote_chars", "alarm_turn_minutes",
                                 "alarm_stalled_minutes", "alarm_awaiting_minutes",
                                 "alarm_subagent_tool_minutes"])
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


def test_the_dashboards_partition_is_keyed_off_parts():
    """THE THIRD SURFACE THE BRIEF NAMES — and it now exists: `/wo/{p}/{id}/debug` renders
    an `Anatomy` (spec §7 of docs/specs/2026-09-24-order-observability.md), which is the
    case this test's previous form said to convert it to when it arrived.

    So the pin moves from "the dashboard reads no anatomy" to the property that actually
    protects a fifth bucket: the page walks `PARTS` and takes its wording from
    `cli.PART_LABELS`/`PART_SHORT` rather than keeping a list of its own, so a bucket
    cannot exist in one renderer and be silently absent from the other.
    """
    import ast
    from pathlib import Path

    imported: set[str] = set()
    ui = Path(inspection.__file__).parent / "ui"
    for path in sorted(ui.rglob("*.py")):
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                    "inspection"):
                imported |= {a.name for a in node.names}
            if isinstance(node, ast.ImportFrom) and node.module in (None, "", "."):
                imported |= {a.name for a in node.names if a.name == "inspection"}

    assert imported == {"ALARM_KINDS", "PARTS"}, (
        "the dashboard reaches into `inspection` for something new — if it is a bucket "
        "of the partition, key it off `PARTS` and pin it like `PART_LABELS`")
    app = (ui / "app.py").read_text()
    assert "parts=PARTS" in app and "part_labels=PART_LABELS" in app
    assert "part_short=PART_SHORT" in app


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


def test_a_turn_awaiting_the_model_is_not_flagged_as_having_made_no_call(capsys):
    """Spec §1: the flag is a claim about the API, and here a request is in flight."""
    from jarvis import cli

    turn = inspection.Turn(seq=3, started=1_000.0, ended=1_000.0 + 65 * 60)
    turn.awaiting_since = 1_000.0
    anatomy = inspection.Anatomy(session_id="s", found=True, turns=[turn])
    unit = {"wo_id": "wo-1", "title": "t", **anatomy.as_dict()}

    cli._print_anatomy(unit, InspectConfig().report_write_floor)
    line = next(l for l in capsys.readouterr().out.splitlines() if "turn  3" in l)

    assert cli.AWAITING_FLAG in line and cli.NO_CALL_FLAG not in line
    # One column, so the split after it stays a column whichever flag is printed.
    assert cli.FLAG_WIDTH >= max(len(cli.AWAITING_FLAG), len(cli.NO_CALL_FLAG))


def test_a_work_order_with_no_session_reports_no_transcript(started):
    wo = ops.create_work_order("proj_a", "never dispatched")

    (unit,) = ops.inspect_report(wo["id"])["units"]

    assert unit["found"] is False and unit["turns"] == []


def test_inspect_names_the_biggest_os_side_input_for_the_order(started, capsys):
    """Spec §3, docs/superpowers/specs/2026-09-26-bounded-model-inputs.md: `jarvis
    inspect` names the biggest OS-side input for the order it is inspecting."""
    from jarvis import agent_usage, cli

    wo = ops.create_work_order("proj_a", "an order Neo answered twice")
    agent_usage.record("neo_answer", project="proj_a", wo_id=wo["id"], label="question",
                       model="claude-opus-5",
                       usage={"output": 1, "prompt_chars": 8_000,
                              "system_prompt_chars": 500})
    agent_usage.record("panel_seat", project="proj_a", wo_id=wo["id"], label="premise",
                       model="claude-opus-5",
                       usage={"output": 1, "prompt_chars": 140_000,
                              "system_prompt_chars": 2_000})

    (unit,) = ops.inspect_report(wo["id"])["units"]

    biggest = unit["largest_os_input"]
    assert biggest["kind"] == "panel_seat" and biggest["label"] == "premise"
    assert (biggest["prompt_chars"], biggest["system_prompt_chars"]) == (140_000, 2_000)

    cli._print_anatomy(unit, InspectConfig().report_write_floor)
    out = capsys.readouterr().out
    assert "140,000" in out and "2,000" in out and "premise" in out


def test_an_order_whose_os_calls_measured_nothing_names_none(started):
    """NEVER a fabricated number: a call recorded before the columns existed reads 0,
    and 0 is not a size."""
    from jarvis import agent_usage

    wo = ops.create_work_order("proj_a", "an order from before the sizes existed")
    agent_usage.record("neo_answer", project="proj_a", wo_id=wo["id"], label="question",
                       model="claude-opus-5", usage={"output": 1})

    (unit,) = ops.inspect_report(wo["id"])["units"]

    assert unit["largest_os_input"] is None


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


def test_a_slow_response_is_recorded_and_never_escalated(
        started, monkeypatch, tmp_path):
    """END TO END, spec §2's "what must not escalate means mechanically"."""
    from jarvis.project_store import ProjectStore

    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))
    daemon = started
    wo = ops.create_work_order("proj_a", "the slow one")

    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        at = turn["started_at"]
        # A prompt and nothing after it: a request is in flight, no block has landed.
        (root / "-proj" / "slow.jsonl").write_text(
            json.dumps(prompt_row(at + 1, "You are the worker agent for wo-1")) + "\n")
        store.update_work_order(wo["id"], status="running", session_id="slow")
        monkeypatch.setattr("jarvis.daemon.time.time", lambda: at + 2 * 3600)
        daemon.check_burning_turns(daemon.catalog.projects[0], store)

        (alarm,) = store.alarms_of(wo["id"])
        assert alarm["kind"] == inspection.SLOW_RESPONSE_ALARM
        assert alarm["status"] == "informational"
        # The queue's own predicate is the enforcement — no new filter anywhere.
        assert store.claim_next_alarm() is None
        assert store.get_work_order(wo["id"])["needs_attention"] == 0
        # The timeline and the dedupe memory are unchanged.
        assert len(store.events_of_kind(wo["id"], "cost_alarm")) == 1
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


# -- tool parameters: redaction, bounds, additivity (spec §4a) -------------------------


def test_a_private_key_block_is_named_and_never_quoted():
    """The marker names the SHAPE. Quoting the match would move the credential out of
    the transcript and into the report — `kn-deef42ea`, one table along."""
    body = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAA\n"
            "-----END OPENSSH PRIVATE KEY-----")

    out = inspection.redact_param(f"cat <<EOF\n{body}\nEOF")

    assert "<redacted: a private key block>" in out
    assert "b3BlbnNzaC1rZXktdjEA" not in out
    assert "BEGIN OPENSSH PRIVATE KEY" not in out


def test_an_ssh_key_line_is_redacted():
    out = inspection.redact_param(
        "echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHxK9qQ' >> authorized_keys")

    assert "<redacted: an ssh key line>" in out
    assert "AAAAC3NzaC1lZDI1NTE5" not in out


def test_an_authorization_header_value_is_redacted():
    out = inspection.redact_param(
        'curl -H "Authorization: Bearer sk-live-9f2a8c7b1d" https://api.example.com')

    assert "<redacted: an Authorization header value>" in out
    assert "sk-live-9f2a8c7b1d" not in out
    assert "https://api.example.com" in out


@pytest.mark.parametrize("line, leak", [
    ("export API_KEY=sk-live-9f2a8c7b1d4e", "sk-live-9f2a8c7b1d4e"),
    ('  "token": "ghp_A1b2C3d4E5f6G7h8I9",', "ghp_A1b2C3d4E5f6G7h8I9"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI2K7MDENGbPxRfiCY", "wJalrXUtnFEMI"),
    ("password=hunter2000000", "hunter2000000"),
    ("access_key: AKIAIOSFODNN7EXAMPLE9", "AKIAIOSFODNN7EXAMPLE9"),
    ('passwd = "p4ssw0rd-aa12"', "p4ssw0rd-aa12"),
])
def test_a_credential_assignment_loses_its_value(line, leak):
    out = inspection.redact_param(line)

    assert "<redacted: a credential value>" in out
    assert leak not in out


@pytest.mark.parametrize("line", [
    "the api_key is read from the environment at boot, never committed",
    "API_KEY=changeme",
    "API_KEY=",
    'password: ""',
    "monkey=banana12345",
    'api_key = os.environ["API_KEY"]',
    "mysql --password changeme -h db",
    'gh auth login --token ""',
    "gh pr list --token $GH_TOKEN",
    "gh pr list --token ${GH_TOKEN}",
    "export GH_TOKEN=$OTHER_TOKEN && gh pr list",
    "curl https://api.example.com/v1/models",
])
def test_a_line_that_carries_no_credential_is_left_exactly_as_it_is(line):
    """The negative control. Redacting a placeholder or a mention would make the report
    useless for the case it exists for: reading what the worker actually ran."""
    assert inspection.redact_param(line) == line


# -- shapes that carry a credential with no credential-named key beside it -------------


@pytest.mark.parametrize("line, leak", [
    ("ANTHROPIC_API_KEY=sk-ant-api03-abc123def456 claude -p \"go\"",
     "sk-ant-api03-abc123def456"),
    ("export GH_TOKEN=ghp_A1b2C3d4E5 && gh pr list", "ghp_A1b2C3d4E5"),
    ("cd x; TOKEN=abc123def ./deploy.sh", "abc123def"),
    ("env AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI2K7MDENG aws s3 ls",
     "wJalrXUtnFEMI2K7MDENG"),
])
def test_an_inline_credential_assignment_loses_its_value(line, leak):
    """The commonest way a credential reaches `params["command"]`: an assignment that is
    not the whole line, in front of the command it is for."""
    out = inspection.redact_param(line)

    assert inspection.CREDENTIAL_VALUE_MARKER in out
    assert leak not in out


def test_an_inline_assignment_keeps_the_command_around_it():
    out = inspection.redact_param("cd x; TOKEN=abc123def ./deploy.sh")

    assert out == f"cd x; TOKEN={inspection.CREDENTIAL_VALUE_MARKER} ./deploy.sh"


def test_an_anthropic_key_is_named_and_never_quoted():
    out = inspection.redact_param("claude --api-key-helper 'echo sk-ant-api03-Zq9x8W7v'")

    assert "<redacted: an Anthropic API key>" in out
    assert "sk-ant-api03-Zq9x8W7v" not in out


@pytest.mark.parametrize("secret", [
    "sk-proj-9f2a8c7b1d4e6a5b", "sk-9f2a8c7b1d4e6a5b3c7d9e1f",
])
def test_a_bare_sk_key_is_named(secret):
    out = inspection.redact_param(f"curl -H 'x-key: {secret}' https://x")

    assert "<redacted: an sk- API key>" in out
    assert secret not in out


@pytest.mark.parametrize("secret", [
    "ghp_A1b2C3d4E5f6G7h8I9j0", "gho_A1b2C3d4E5f6G7h8I9j0",
    "ghs_A1b2C3d4E5f6G7h8I9j0", "ghu_A1b2C3d4E5f6G7h8I9j0",
    "ghr_A1b2C3d4E5f6G7h8I9j0", "github_pat_11ABCDE0y0aBcDeFgH",
])
def test_a_github_token_is_named(secret):
    out = inspection.redact_param(f"echo {secret} | gh auth login --with-token")

    assert "<redacted: a GitHub token>" in out
    assert secret not in out


def test_an_aws_access_key_id_is_named():
    out = inspection.redact_param("aws configure set AKIAIOSFODNN7EXAMPLE")

    assert "<redacted: an AWS access key id>" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_a_slack_token_is_named():
    out = inspection.redact_param("echo xoxb-2468013579-AbCdEfGh | slack-cli auth")

    assert "<redacted: a Slack token>" in out
    assert "xoxb-2468013579-AbCdEfGh" not in out


def test_a_password_in_a_url_is_named_and_the_host_survives():
    out = inspection.redact_param(
        "psql postgres://deploy:s3cr3t-pa55@db.internal:5432/app")

    assert "<redacted: a password in a URL>" in out
    assert "s3cr3t-pa55" not in out
    assert "postgres://deploy:" in out
    assert "@db.internal:5432/app" in out


def test_a_curl_u_credential_is_named():
    out = inspection.redact_param("curl -u deploy:s3cr3t-pa55 https://api.example.com")

    assert "<redacted: a curl -u credential>" in out
    assert "s3cr3t-pa55" not in out
    assert "https://api.example.com" in out


@pytest.mark.parametrize("line, leak", [
    ("mysql --password s3cr3tpa55 -h db", "s3cr3tpa55"),
    ("mysql --password=s3cr3tpa55 -h db", "s3cr3tpa55"),
    ("gh auth login --token ghXYZ012abc9", "ghXYZ012abc9"),
    ("call --api-key 9f2a8c7b1d4e6a5b", "9f2a8c7b1d4e6a5b"),
    ("call --api-key=9f2a8c7b1d4e6a5b", "9f2a8c7b1d4e6a5b"),
])
def test_a_credential_flag_value_is_named(line, leak):
    out = inspection.redact_param(line)

    assert "<redacted: a credential passed as a flag>" in out
    assert leak not in out


def test_a_credential_flag_keeps_the_flag_it_was_passed_to():
    out = inspection.redact_param("mysql --password s3cr3tpa55 -h db")

    assert out.startswith("mysql --password <redacted:")
    assert out.endswith(" -h db")


def test_a_bare_token_in_a_bash_command_never_reaches_the_payload(write_transcript):
    """The transcript-level proof for the shapes that need no assignment: a token with
    nothing around it naming it, straight into `params["command"]`."""
    secret = "ghp_A1b2C3d4E5f6G7h8I9j0"
    session = write_transcript("bare-token", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Bash",
                   {"command": f"echo {secret} | gh auth login --with-token",
                    "description": "log in"}),
    ])
    anatomy = inspection.read_session(session)

    assert secret not in json.dumps(anatomy.as_dict())
    assert "<redacted: a GitHub token>" in anatomy.turns[0].spans[0].params["command"]


def test_an_inline_assignment_never_reaches_the_payload(write_transcript):
    secret = "sk-ant-api03-abc123def456"
    session = write_transcript("inline-assign", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Bash",
                   {"command": f'ANTHROPIC_API_KEY={secret} claude -p "go"'}),
    ])
    anatomy = inspection.read_session(session)

    assert secret not in json.dumps(anatomy.as_dict())


def test_a_secret_in_a_bash_command_never_reaches_the_payload(write_transcript):
    """Redaction runs BEFORE the value is stored, not at render time — spec §4a."""
    secret = "sk-live-0ff1ce9a7b3c2d"
    session = write_transcript("leaky", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Bash",
                   {"command": f'curl -H "Authorization: Bearer {secret}" https://x',
                    "description": "call the API"}),
    ])
    anatomy = inspection.read_session(session)

    assert secret not in json.dumps(anatomy.as_dict())
    params = anatomy.turns[0].spans[0].params
    assert params["description"] == "call the API"
    assert "<redacted: an Authorization header value>" in params["command"]


def test_a_long_value_is_cut_short_and_its_key_is_listed(write_transcript):
    session = write_transcript("long", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Write",
                   {"file_path": "/tmp/x.py", "content": "a" * 5_000}),
    ])
    span = inspection.read_session(session).turns[0].spans[0]

    assert len(span.params["content"]) == inspection.PARAM_CAPS.per_value + 1
    assert span.params["content"].endswith("…")
    assert span.params_truncated == ["content"]
    assert span.params_dropped == []
    assert span.params["file_path"] == "/tmp/x.py"


def test_the_span_cap_drops_the_keys_that_do_not_fit(write_transcript):
    payload = {f"k{i}": "b" * inspection.PARAM_CAPS.per_value for i in range(8)}
    session = write_transcript("wide", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Bash", payload),
    ])
    span = inspection.read_session(session).turns[0].spans[0]

    kept = inspection.PARAM_CAPS.per_span // inspection.PARAM_CAPS.per_value
    assert list(span.params) == [f"k{i}" for i in range(kept)]
    assert span.params_dropped == [f"k{i}" for i in range(kept, 8)]


def test_the_turn_budget_empties_a_later_span_s_params(write_transcript):
    """Once a turn has spent its budget every later span reports `{}` AND says which
    keys it dropped — a report that truncates silently is not reproducible."""
    spans = inspection.PARAM_CAPS.per_turn // inspection.PARAM_CAPS.per_span
    rows = [prompt_row(0, "You are the worker agent for wo-1")]
    for i in range(spans + 1):
        rows += tool_rows(1 + i, 2 + i, f"t{i}", "Bash",
                          {f"k{j}": "c" * (inspection.PARAM_CAPS.per_span // 4)
                           for j in range(4)})
    turn = inspection.read_session(write_transcript("burn", rows)).turns[0]

    assert turn.spans[0].params
    last = turn.spans[-1]
    assert last.params == {}
    assert last.params_dropped == ["k0", "k1", "k2", "k3"]


def test_the_caps_are_stated_in_the_payload(real_session):
    assert real_session.as_dict()["param_caps"] == {
        "per_value": inspection.PARAM_CAPS.per_value,
        "per_span": inspection.PARAM_CAPS.per_span,
        "per_turn": inspection.PARAM_CAPS.per_turn,
    }


def test_the_real_session_grows_keys_and_changes_none(real_session):
    """Additivity against the committed session: `detail` is what it always was."""
    span = real_session.turns[0].spans[0].as_dict()

    assert span["detail"] == "List repo structure"
    assert span["params"] == {"description": "List repo structure"}
    assert span["params_truncated"] == [] and span["params_dropped"] == []
    assert {"name", "tool_id", "started", "ended", "seconds", "detail", "join",
            "finished"} <= set(span)


# -- subagent anatomy (spec §4b) -------------------------------------------------------


TASK = "a7b62083-1111-2222-3333-444455556666"


def sub_rows(base: float, *, write: int = 0, read: int = 0) -> list[dict]:
    """A subagent transcript: one prompt, two calls, one tool span."""
    return [
        prompt_row(base, "do the thing", sdk=False),
        assistant_row(base + 1, "s-m1", write=write, read=read),
        *tool_rows(base + 2, base + 4, "s-t1", "Grep", {"pattern": "needle"}),
        assistant_row(base + 5, "s-m2", write=write, read=read),
    ]


def parent_rows(task_id: str = TASK, *, tool: str = "TaskOutput") -> list[dict]:
    """A parent session whose turn 1 joins on `task_id` and whose turn 2 does not."""
    return [
        prompt_row(1000, "You are the worker agent for wo-1"),
        assistant_row(1001, "m1", write=30_000),
        *tool_rows(1002, 1400, "p-t1", tool, {"task_id": task_id}),
        prompt_row(1500, "carry on"),
        assistant_row(1501, "m2", read=30_000),
        *tool_rows(1502, 1510, "p-t2", "Bash", {"command": "ls"}),
    ]


def write_meta(tmp_path, session_id: str, task_id: str, label: str) -> None:
    directory = tmp_path / "projects" / "-proj" / session_id / "subagents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"agent-{task_id}.meta.json").write_text(
        json.dumps({"agentType": label, "description": "dig"}))


def test_a_subagent_s_turns_writes_and_peak_hang_off_the_turn_that_joined_it(
        write_transcript, tmp_path):
    """Spec §4b: the subagent's own anatomy, attached to the parent turn that names it."""
    session = write_transcript(
        "nested", parent_rows(),
        subagents={f"agent-{TASK}": sub_rows(1100, write=25_000, read=5_000)})
    write_meta(tmp_path, session, TASK, "explorer")
    anatomy = inspection.read_session(session)

    sub = anatomy.turns[0].subagents[0]
    assert sub.task_id == TASK and sub.label == "explorer · dig"
    assert len(sub.turns) == 1 and sub.turns[0].tools == pytest.approx(2.0)
    assert [w.cause for w in sub.writes] == [inspection.COLD_START,
                                             inspection.PREFIX_MISS]
    assert sub.context_peak == 30_000
    assert anatomy.turns[1].subagents == []
    assert anatomy.unattached_subagents == []


def test_attaching_a_subagent_changes_no_parent_number(write_transcript, tmp_path):
    """THE PARTITION RULE (kn-7a2180ba): a subagent is drawn OUT of its parent turn,
    never added to it. The same session read with and without the transcript present
    must be identical in every parent figure."""
    without = inspection.read_session(
        write_transcript("partition-a", parent_rows())).as_dict()
    with_sub = inspection.read_session(write_transcript(
        "partition-b", parent_rows(),
        subagents={f"agent-{TASK}": sub_rows(1100, write=900_000, read=5_000)}))
    grown = with_sub.as_dict()

    for key in ("partition", "share", "context_peak", "rewrite_excess", "cache_ttl",
                "tools", "writes", "joins"):
        assert grown[key] == without[key], key
    for a, b in zip(grown["turns"], without["turns"]):
        for key in ("wall", "generating", "blocked", "tools", "share", "usage",
                    "context_peak", "api_calls"):
            assert a[key] == b[key], key
    assert with_sub.turns[0].subagents[0].context_peak == 905_000


def test_a_subagent_s_prefix_miss_is_never_folded_into_the_parent_s_writes(
        write_transcript):
    """A parent turn that merely waited on a join did not pay that write — attributing
    it upward names the wrong turn as the prefix break (spec §4b)."""
    anatomy = inspection.read_session(write_transcript(
        "own-writes", parent_rows(),
        subagents={f"agent-{TASK}": sub_rows(1100, write=400_000, read=5_000)}))

    assert [w.written for w in anatomy.writes] == [30_000]
    assert inspection.PREFIX_MISS in [w.cause
                                      for w in anatomy.turns[0].subagents[0].writes]
    assert inspection.PREFIX_MISS not in [w.cause for w in anatomy.writes]


# -- the floor, named: an empty floored list is never an absence (spec 2026-10-02 §1) --


def under_floor_rows(base: float, *, count: int = 6, write: int = 5_000) -> list[dict]:
    """A subagent of many writes, every one of them under `report_write_floor`.

    wo-fb7c0fc2's `a8e11a7e` shape, scaled: 115 writes, largest 19,381, none at 20,000.
    """
    rows: list[dict] = [prompt_row(base, "do the thing", sdk=False)]
    rows += [assistant_row(base + 1 + i, f"u-m{i}", write=write,
                           read=100_000 + 1_000 * i)
             for i in range(count)]
    return rows


def test_a_subagent_whose_every_write_is_under_the_floor_reports_the_total_anyway(
        write_transcript):
    """§1.1: `writes` empty is "nothing AT THAT FLOOR", and the threshold-free figures
    beside it are the only thing that can say so."""
    anatomy = inspection.read_session(write_transcript(
        "under-floor", parent_rows(),
        subagents={f"agent-{TASK}": under_floor_rows(1100)}))
    sub = anatomy.turns[0].subagents[0]

    assert sub.writes == []
    assert sub.total_written == 30_000
    assert sub.max_write == 5_000 < sub.write_floor
    assert sub.write_floor == 20_000
    assert sub.api_call_count == 6


def test_a_subagent_that_wrote_nothing_is_a_different_answer_from_under_the_floor(
        write_transcript):
    anatomy = inspection.read_session(write_transcript(
        "no-writes", parent_rows(), subagents={f"agent-{TASK}": sub_rows(1100)}))
    sub = anatomy.turns[0].subagents[0]

    assert sub.writes == [] and sub.total_written == 0 and sub.max_write == 0


def test_a_subagent_whose_cache_read_goes_backwards_has_one_boundary(write_transcript):
    """§1.2: `usage.classify_boundaries` over THIS subagent's calls, reused and not
    reimplemented."""
    rows = [prompt_row(1100, "do the thing", sdk=False),
            assistant_row(1101, "b-m1", write=5_000, read=200_000),
            assistant_row(1102, "b-m2", write=5_000, read=1_000)]
    anatomy = inspection.read_session(
        write_transcript("sub-boundary", parent_rows(),
                         subagents={f"agent-{TASK}": rows}),
        cold_prefix_floor=50_000)
    sub = anatomy.turns[0].subagents[0]

    assert len(sub.boundaries) == 1
    assert sub.rewrite()["boundaries"] == 1
    assert sub.rewrite()["cache_write"] == sub.total_written == 10_000


def test_a_continuous_subagent_has_no_boundary_and_a_structural_zero_tax(
        write_transcript):
    """The structural zero §1.5 must label: a monotonic subagent whose written volume
    never exceeds its own context peak pays NO re-write tax, by arithmetic."""
    anatomy = inspection.read_session(
        write_transcript("sub-monotonic", parent_rows(),
                         subagents={f"agent-{TASK}": under_floor_rows(1100)}),
        cold_prefix_floor=50_000)
    sub = anatomy.turns[0].subagents[0]

    assert sub.boundaries == []
    assert sub.rewrite()["tokens"] == 0 and sub.rewrite()["boundaries"] == 0


def test_an_unknown_cold_prefix_floor_is_undecided_and_never_zero_prefix_misses(
        write_transcript):
    """§1.2: `None` yields `BOUNDARY_UNDECIDED`, so 0 prefix misses can never be read
    as a finding."""
    rows = [prompt_row(1100, "do the thing", sdk=False),
            assistant_row(1101, "u-m1", write=5_000, read=200_000),
            assistant_row(1500, "u-m2", write=5_000, read=1_000)]
    anatomy = inspection.read_session(write_transcript(
        "sub-undecided", parent_rows(), subagents={f"agent-{TASK}": rows}))
    sub = anatomy.turns[0].subagents[0]

    assert [b.cause for b in sub.boundaries] == [usage.BOUNDARY_UNDECIDED]
    assert sub.rewrite()["undecided_boundaries"] == 1
    assert sub.rewrite()["prefix_write"] == 0


def test_api_call_count_and_the_api_calls_property_can_disagree(write_transcript):
    """WHY `api_call_count` is a field and not the existing property: a subagent
    transcript with no prompt row yields NO turns (`read_transcript` opens one only at a
    prompt), so the property counts 0 calls over 0 turns while two were made. The
    threshold-free total has to be readable as a rate either way (§1.1)."""
    rows = [assistant_row(1101, "h-m1", write=5_000, read=100_000),
            assistant_row(1102, "h-m2", write=5_000, read=101_000)]
    anatomy = inspection.read_session(write_transcript(
        "headless-sub", parent_rows(), subagents={f"agent-{TASK}": rows}))
    sub = anatomy.turns[0].subagents[0]

    assert sub.turns == [] and sub.api_calls == 0
    assert sub.api_call_count == 2 and sub.total_written == 10_000


def test_the_five_new_fields_are_on_the_payload(write_transcript):
    """`as_dict()` carries them, because the dashboard DERIVES NOTHING (§1.1)."""
    anatomy = inspection.read_session(
        write_transcript("sub-payload", parent_rows(),
                         subagents={f"agent-{TASK}": under_floor_rows(1100)}),
        cold_prefix_floor=50_000)
    row = anatomy.turns[0].subagents[0].as_dict()

    assert row["total_written"] == 30_000 and row["max_write"] == 5_000
    assert row["write_floor"] == 20_000 and row["api_call_count"] == 6
    assert row["rewrite"]["boundaries"] == 0 and row["rewrite"]["tokens"] == 0


def test_both_rewrite_blocks_have_the_same_keys(write_transcript):
    """One spelling of that dict (§1.1): two is how one key comes to mean two things."""
    anatomy = inspection.read_session(
        write_transcript("rewrite-keys", parent_rows(),
                         subagents={f"agent-{TASK}": under_floor_rows(1100)}),
        cold_prefix_floor=50_000)

    assert (set(anatomy.turns[0].subagents[0].rewrite())
            == set(anatomy.rewrite()))


def test_a_subagent_no_span_names_is_unattached_and_on_no_turn(write_transcript):
    """Issue 227's mistake was inventing a parent the record does not name: no timestamp
    fallback, so an unnamed subagent is REPORTED as unattached."""
    orphan = "deadbeef-0000-0000-0000-000000000000"
    anatomy = inspection.read_session(write_transcript(
        "orphan", parent_rows(), subagents={f"agent-{orphan}": sub_rows(1100)}))

    assert [s.task_id for s in anatomy.unattached_subagents] == [orphan]
    assert all(t.subagents == [] for t in anatomy.turns)
    assert anatomy.as_dict()["unattached_subagents"][0]["task_id"] == orphan


def test_a_join_whose_detail_name_joins_already_rewrote_still_attaches(
        write_transcript, tmp_path):
    """`_name_joins` turns the bare id into "label (id)"; matching the bare string only
    would drop every labelled subagent — the common case."""
    session = write_transcript(
        "renamed", parent_rows(), subagents={f"agent-{TASK}": sub_rows(1100)})
    write_meta(tmp_path, session, TASK, "explorer")
    anatomy = inspection.read_session(session)

    assert anatomy.turns[0].spans[0].detail == f"explorer · dig ({TASK})"
    assert [s.task_id for s in anatomy.turns[0].subagents] == [TASK]


def test_an_agent_span_attaches_as_well_as_a_taskoutput_one(write_transcript):
    anatomy = inspection.read_session(write_transcript(
        "agent-span", parent_rows(tool="Agent"),
        subagents={f"agent-{TASK}": sub_rows(1100)}))

    assert [s.task_id for s in anatomy.turns[0].subagents] == [TASK]


def test_a_subagent_of_a_subagent_is_counted_and_the_depth_read_is_stated(
        write_transcript, tmp_path):
    """`_subagent_transcripts` globs ONE level, so say which depth was read rather than
    implying completeness (spec §4b)."""
    session = write_transcript(
        "deep", parent_rows(), subagents={f"agent-{TASK}": sub_rows(1100)})
    deeper = (tmp_path / "projects" / "-proj" / session / "subagents"
              / f"agent-{TASK}" / "subagents")
    deeper.mkdir(parents=True)
    # TWO, so the count is not trivially satisfied by any non-zero answer.
    for stem in ("agent-child-one", "agent-child-two"):
        (deeper / f"{stem}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in sub_rows(1150)))
    anatomy = inspection.read_session(session)

    sub = anatomy.turns[0].subagents[0]
    assert sub.deeper == 2
    assert sub.as_dict()["deeper"] == 2
    assert anatomy.as_dict()["subagent_depth_read"] == 1


def test_only_the_taskoutput_span_carries_the_task_id_in_a_real_transcript(real_session):
    """What `_names` can actually join on. An `Agent` span's params are a `description`
    and nothing else, so the id it spawned is NOT there; the `TaskOutput` span's
    `task_id` parameter is the only join in the committed transcript."""
    spawns = [s for s in real_session.spans if s.name in inspection.SPAWN_TOOLS]
    ids = set(real_session.subagent_labels)

    assert ids
    agents = [s for s in spawns if s.name == "Agent"]
    assert agents
    assert not any(i in v for s in agents for v in s.params.values() for i in ids)
    outputs = [s for s in spawns if s.name == "TaskOutput"]
    assert [s for s in outputs
            if any(i in v for v in s.params.values() for i in ids)] == outputs


def test_a_meta_only_directory_adds_nothing_to_the_real_session(real_session):
    """The committed session has `.meta.json` files and NO subagent transcripts: the new
    keys must EXIST and be empty. Absent is not zero."""
    payload = real_session.as_dict()

    assert real_session.subagent_labels  # the meta files are there
    assert payload["unattached_subagents"] == []
    assert payload["subagent_depth_read"] == 1
    assert all(t["subagents"] == [] for t in payload["turns"])


# -- `detail` is redacted too (spec §4a, decided on wo-5f4d8611 q687) ------------------


def test_a_secret_in_a_bash_command_never_reaches_the_detail(write_transcript):
    """`_detail_of` prefers `command`, so an undescribed `Bash` call put the raw command
    line in the payload while `params` beside it was redacted."""
    secret = "sk-live-0ff1ce9a7b3c2d"
    anatomy = inspection.read_session(write_transcript("leaky-detail", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Bash",
                   {"command": f'curl -H "Authorization: Bearer {secret}" https://x'}),
    ]))

    detail = anatomy.turns[0].spans[0].detail
    assert "<redacted: an Authorization header value>" in detail
    assert secret not in json.dumps(anatomy.as_dict())


def test_detail_is_redacted_before_it_is_truncated(write_transcript):
    """Order matters: truncating first would cut the header short and print the head of
    the credential. A cut marker is a cosmetic loss; half a secret is not."""
    secret = "sk-live-0ff1ce9a7b3c2dAAAABBBBCCCCDDDDEEEEFFFF"
    command = 'curl -H "Authorization: Bearer ' + secret + '" https://x'
    cfg = InspectConfig(quote_chars=len(command) - 20)
    anatomy = inspection.read_session(write_transcript("cut-detail", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Bash", {"command": command}),
    ]), cfg)

    assert secret[:12] not in anatomy.turns[0].spans[0].detail


def test_redacting_detail_changes_nothing_in_the_committed_session(real_session):
    """The no-op proof: the real session carries no secrets, so every pinned `detail`
    reads exactly as it always did."""
    details = [s.detail for t in real_session.turns for s in t.spans]

    assert details and all(d == inspection.redact_param(d) for d in details)
    assert real_session.turns[0].spans[0].detail == "List repo structure"


# -- the visual pass on `jarvis inspect` (spec §4c) ------------------------------------


def rendered(anatomy: inspection.Anatomy, **kwargs) -> None:
    from jarvis import cli

    unit = {"wo_id": "wo-1", "title": "t", **anatomy.as_dict()}
    cli._print_anatomy(unit, InspectConfig().report_write_floor, **kwargs)


def test_the_turn_line_carries_a_bar_whose_segments_follow_parts_order(
        write_transcript, capsys):
    from jarvis import cli

    anatomy = inspection.read_session(write_transcript("bars", parent_rows()))
    rendered(anatomy)
    line = next(l for l in capsys.readouterr().out.splitlines() if "turn  1" in l)

    bar = next(c for c in line.split() if set(c) <= set(cli.BAR_GLYPHS.values()))
    assert len(bar) == cli.BAR_WIDTH
    order = [g for g in cli.BAR_GLYPHS.values() if g in bar]
    assert list(dict.fromkeys(bar)) == order


def test_every_bar_glyph_is_keyed_by_parts():
    from jarvis import cli

    assert tuple(cli.BAR_GLYPHS) == inspection.PARTS


def test_parameters_are_printed_only_when_asked_for(write_transcript, capsys):
    anatomy = inspection.read_session(write_transcript("params", parent_rows()))

    rendered(anatomy)
    default = capsys.readouterr().out
    rendered(anatomy, params=True)
    asked = capsys.readouterr().out

    assert "command" not in default and "task_id" not in default
    assert "command = ls" in asked
    assert str(inspection.PARAM_CAPS.per_value) in asked
    assert f"{inspection.PARAM_CAPS.per_turn:,}" in asked


def test_the_parameter_report_says_when_a_value_was_cut(write_transcript, capsys):
    anatomy = inspection.read_session(write_transcript("cut", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Write", {"file_path": "/tmp/x.py",
                                         "content": "a" * 5_000}),
    ]))

    rendered(anatomy, params=True)

    assert "shortened: content" in capsys.readouterr().out


def test_a_subagent_renders_under_its_turn_with_its_writes_by_cause(
        write_transcript, tmp_path, capsys):
    session = write_transcript(
        "sub-render", parent_rows(),
        subagents={f"agent-{TASK}": sub_rows(1100, write=25_000, read=5_000)})
    write_meta(tmp_path, session, TASK, "explorer")

    rendered(inspection.read_session(session))
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "explorer" in l)

    assert inspection.COLD_START in out and inspection.PREFIX_MISS in out
    assert "api calls" in line and "peak" in line
    assert "partition" in out  # drawn OUT of the parent turn, never added to it


def test_a_deeper_subagent_says_its_levels_were_not_read(
        write_transcript, tmp_path, capsys):
    session = write_transcript(
        "deep-render", parent_rows(), subagents={f"agent-{TASK}": sub_rows(1100)})
    deeper = (tmp_path / "projects" / "-proj" / session / "subagents"
              / f"agent-{TASK}" / "subagents")
    deeper.mkdir(parents=True)
    (deeper / "agent-child.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in sub_rows(1150)))

    rendered(inspection.read_session(session))
    out = capsys.readouterr().out

    assert "NOT read" in out and "depth read 1" in out


def test_an_under_floor_subagent_never_renders_as_no_large_writes(
        write_transcript, capsys):
    """§1.4: the string `no large writes` is DELETED. An empty floored list reads as a
    floor, with the threshold-free total in the sentence."""
    session = write_transcript(
        "floor-render", parent_rows(),
        subagents={f"agent-{TASK}": under_floor_rows(1100)})

    rendered(inspection.read_session(session))
    out = capsys.readouterr().out

    assert "no large writes" not in out
    assert "no single write reached the" in out
    assert "30,000" in out and "6 calls" in out and "20,000 floor" in out


def test_a_subagent_that_wrote_nothing_says_so(write_transcript, capsys):
    session = write_transcript("zero-render", parent_rows(),
                               subagents={f"agent-{TASK}": sub_rows(1100)})

    rendered(inspection.read_session(session))
    out = capsys.readouterr().out

    from jarvis import cli

    assert cli.SUB_NO_WRITES in out and "no large writes" not in out


def test_a_continuous_subagent_s_zero_tax_is_rendered_as_structural(
        write_transcript, capsys):
    """§1.4: the zero is the FINDING, worded so it can never read as "nothing here"."""
    session = write_transcript("structural-render", parent_rows(),
                               subagents={f"agent-{TASK}": under_floor_rows(1100)})

    rendered(inspection.read_session(session, cold_prefix_floor=50_000))
    out = capsys.readouterr().out

    assert "no boundary" in out and "STRUCTURAL, not small" in out


def test_the_unread_depth_line_survives_the_new_states(
        write_transcript, tmp_path, capsys):
    """`SUBAGENT_DEPTH_READ` stays 1 and the unread-depth labelling q1215 requires
    stays exactly as it was (§1.4)."""
    session = write_transcript(
        "floor-deep-render", parent_rows(),
        subagents={f"agent-{TASK}": under_floor_rows(1100)})
    deeper = (tmp_path / "projects" / "-proj" / session / "subagents"
              / f"agent-{TASK}" / "subagents")
    deeper.mkdir(parents=True)
    (deeper / "agent-child.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in sub_rows(1150)))

    rendered(inspection.read_session(session))
    out = capsys.readouterr().out

    assert "NOT read — depth read 1" in out
    assert inspection.SUBAGENT_DEPTH_READ == 1


def test_an_unattached_subagent_gets_its_own_section(write_transcript, capsys):
    orphan = "deadbeef-0000-0000-0000-000000000000"
    anatomy = inspection.read_session(write_transcript(
        "orphan-render", parent_rows(), subagents={f"agent-{orphan}": sub_rows(1100)}))

    rendered(anatomy)
    out = capsys.readouterr().out

    assert "no span named" in out and orphan in out


# -- nested tool inputs, and values that end at a quote (spec §4a, review round 2) -----


def test_a_secret_in_a_nested_edit_never_reaches_the_payload(write_transcript):
    """A `MultiEdit` input is a list of dicts, so the credential is a LEAF — and before
    round 2 it was redacted only after `json.dumps`, where neither assignment regex can
    see it (escaped newlines, one line, quotes in the way)."""
    session = write_transcript("nested-edit", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "MultiEdit",
                   {"edits": [{"new_string": "DB_PASSWORD=hunter2000abc\n"}]}),
    ])
    anatomy = inspection.read_session(session)

    assert "hunter2000abc" not in json.dumps(anatomy.as_dict())
    assert inspection.CREDENTIAL_VALUE_MARKER in anatomy.turns[0].spans[0].params["edits"]


def test_a_secret_in_a_list_of_strings_never_reaches_the_payload(write_transcript):
    session = write_transcript("nested-list", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Bash",
                   {"args": ["--quiet", "export TOKEN=abc123def456", 7, None, True]}),
    ])
    span = inspection.read_session(session).turns[0].spans[0]

    assert "abc123def456" not in json.dumps(span.as_dict())
    assert "--quiet" in span.params["args"]
    # Non-string scalars pass through the walk untouched.
    assert "7" in span.params["args"] and "null" in span.params["args"]


def test_a_secret_two_levels_down_in_a_dict_never_reaches_the_payload(write_transcript):
    session = write_transcript("nested-dict", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "mcp__deploy__run",
                   {"env": {"stage": {"API_KEY": "sk-live-9f2a8c7b1d4e"}}}),
    ])
    span = inspection.read_session(session).turns[0].spans[0]

    assert "sk-live-9f2a8c7b1d4e" not in json.dumps(span.as_dict())
    assert "stage" in span.params["env"]


@pytest.mark.parametrize("payload, leak", [
    ({"password": "hunter2000000"}, "hunter2000000"),
    ({"api_key": "abc123def456"}, "abc123def456"),
    ({"env": {"DB_PASSWORD": "hunter2000abc"}}, "hunter2000abc"),
    ({"env": {"API_TOKEN": "abc123def456"}}, "abc123def456"),
])
def test_a_credential_under_a_credential_named_key_never_reaches_the_payload(
        write_transcript, payload, leak):
    """Round 3 blocker 1: `_redact_leaves` redacted key and value as SEPARATE strings, so
    the name-AND-value test never ran on the pair. None of these values matches a shape,
    and the dumped `{"password": "hunter2000000"}` matches neither assignment regex — the
    whole-line one is anchored at ^, the inline one needs `=`. So all four printed."""
    session = write_transcript(f"pair-{list(payload)[0]}-{len(leak)}", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "mcp__deploy__run", payload),
    ])
    anatomy = inspection.read_session(session)

    assert leak not in json.dumps(anatomy.as_dict())
    assert inspection.CREDENTIAL_VALUE_MARKER in json.dumps(anatomy.as_dict())


def test_a_placeholder_under_a_credential_named_key_survives_unchanged(write_transcript):
    """The negative control the pair test must not break: a report that redacts
    `password=changeme` is useless for reading what the worker actually ran."""
    session = write_transcript("pair-placeholder", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "mcp__deploy__run", {"password": "changeme"}),
    ])
    span = inspection.read_session(session).turns[0].spans[0]

    assert span.params["password"] == "changeme"


def test_two_keys_that_redact_alike_are_both_kept(write_transcript):
    """Round 3 follow-up: the dict keys are redacted in a comprehension, so two distinct
    keys collapsing to one marker silently dropped an entry — against `_params_of`'s
    promise that every key is kept, shortened or listed as dropped."""
    walked = inspection._redact_leaves(
        {"TOKEN=abc123def456": 1, "TOKEN=zzz999yyy888": 2})

    assert len(walked) == 2
    assert sorted(walked.values()) == [1, 2]


def test_a_leaf_marker_is_not_marked_a_second_time_by_the_serialised_pass(
        write_transcript):
    """Belt and braces must not read as two findings: the leaf pass and the line pass
    both see the same text, and the marker must appear ONCE."""
    session = write_transcript("double-mark", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Bash", {"parts": ["TOKEN=abc123def456"]}),
    ])
    value = inspection.read_session(session).turns[0].spans[0].params["parts"]

    assert value.count(inspection.CREDENTIAL_VALUE_MARKER) == 1


def test_a_pathologically_deep_input_is_bounded_and_not_reported():
    """Round 3 blocker 2: the old version passed only because 40 levels of escaped JSON
    pushed the secret past `ParamCaps.per_value` — a SHORTER deep input printed it, since
    `redact_param` cannot match `"TOKEN": "abc123def456"` at all. Walk the value directly
    so the cap cannot be what hides it: below the bound nothing is reported, and the
    marker states the bound."""
    nested: object = {"TOKEN": "abc123def456"}
    for _ in range(inspection._PARAM_WALK_MAX_DEPTH + 2):
        nested = {"next": nested}

    walked = json.dumps(inspection._redact_leaves(nested))

    assert inspection.PARAM_DEPTH_MARKER in walked
    assert str(inspection._PARAM_WALK_MAX_DEPTH) in inspection.PARAM_DEPTH_MARKER
    assert "abc123def456" not in walked


def test_a_nested_input_is_redacted_before_it_is_capped(write_transcript):
    """Order, not an extra allowance: the walk runs first and `ParamCaps` still bites."""
    session = write_transcript("nested-cap", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "MultiEdit",
                   {"edits": [{"new_string": "TOKEN=abc123def456 " + "z" * 5_000}]}),
    ])
    span = inspection.read_session(session).turns[0].spans[0]

    assert "abc123def456" not in json.dumps(span.as_dict())
    assert len(span.params["edits"]) == inspection.PARAM_CAPS.per_value + 1
    assert span.params_truncated == ["edits"]


@pytest.mark.parametrize("line, leak", [
    ('bash -c "export TOKEN=abc123def"', "abc123def"),
    ("ssh host 'API_KEY=abc123def456 ./run'", "abc123def456"),
    ("sh -c (PASSWORD=hunter2000abc ./deploy)", "hunter2000abc"),
    ('bash -c "printf %s ${VARS[API_KEY=abc123def456]}"', "abc123def456"),
])
def test_an_assignment_that_ends_at_a_quote_or_bracket_loses_its_value(line, leak):
    """It used to fail OPEN: the closing quote was captured INTO the value, so the
    credential test rejected it on a character the shell never gave it (round 2)."""
    out = inspection.redact_param(line)

    assert inspection.CREDENTIAL_VALUE_MARKER in out
    assert leak not in out


@pytest.mark.parametrize("line, redacted", [
    ('bash -c "export TOKEN=abc123def"',
     'bash -c "export TOKEN=<redacted: a credential value>"'),
    ("ssh host 'API_KEY=abc123def456 ./run'",
     "ssh host 'API_KEY=<redacted: a credential value> ./run'"),
    ("sh -c (PASSWORD=hunter2000abc ./deploy)",
     "sh -c (PASSWORD=<redacted: a credential value> ./deploy)"),
])
def test_the_quote_around_a_redacted_value_survives(line, redacted):
    """The marker replaces the VALUE only: the redacted line must still read as the
    command that was run."""
    assert inspection.redact_param(line) == redacted


@pytest.mark.parametrize("line, leak", [
    ('bash -c "mysql --password s3cr3tpa55 -h db"', "s3cr3tpa55"),
    ("sh -c 'gh auth login --token=ghXYZ012abc9'", "ghXYZ012abc9"),
])
def test_a_credential_flag_inside_quotes_loses_its_value(line, leak):
    out = inspection.redact_param(line)

    assert "<redacted: a credential passed as a flag>" in out
    assert leak not in out


# -- binding transcript turns to the OS's own numbering ---------------------------------
#
# Spec docs/superpowers/specs/2026-09-27-a-request-in-flight-is-not-a-stalled-turn.md §3.


def _numbered(write_transcript, starts: list[float], session: str = "numbered") -> str:
    """One transcript turn per entry, each with an assistant row so the next one opens."""
    rows: list[dict] = []
    for i, at in enumerate(starts):
        rows.append(prompt_row(at, f"<task-notification> turn {i}"))
        rows.append(assistant_row(at + 1, f"m{i}"))
    return write_transcript(session, rows)


def test_turn_numbers_match_the_turn_files_across_a_compaction(write_transcript):
    """§3's measured case, wo-dbea82cf: 11 `wo_turns` rows, 10 transcript turns, and the
    `/compact` turn leaves no prompt row of its own. Every later number shifted by one,
    so the alarm said turn 11 and `jarvis inspect` said turn 10 about one turn."""
    os_starts = [(1, 0.0), (2, 100.0), (3, 200.0), (4, 300.0), (5, 400.0),
                 (6, 420.0), (7, 500.0), (8, 600.0), (9, 700.0), (10, 800.0),
                 (11, 900.0)]
    # The turn file is written 3-4s before the transcript's prompt row (§3's tolerance).
    session = _numbered(write_transcript,
                        [4.0, 104.0, 204.0, 304.0, 424.0, 504.0, 604.0, 704.0,
                         804.0, 904.0])

    anatomy = inspection.read_session(session, turn_starts=os_starts)

    assert [t.seq for t in anatomy.turns] == [1, 2, 3, 4, 6, 7, 8, 9, 10, 11]
    assert anatomy.unmatched_os_turns == [5]
    assert anatomy.turns[-1].seq == 11


def test_read_session_without_turn_starts_still_numbers_one_to_n(write_transcript):
    """The regression guard for every existing caller — §3's first rule."""
    session = _numbered(write_transcript, [0.0, 100.0, 200.0], session="plain")

    anatomy = inspection.read_session(session)

    assert [t.seq for t in anatomy.turns] == [1, 2, 3]
    assert anatomy.unmatched_os_turns == []
    assert [t.part for t in anatomy.turns] == [1, 1, 1]


def test_two_transcript_turns_on_one_os_turn_are_parts_of_it(write_transcript):
    """§3: the compaction shape read forwards. Both keep the `seq`; `part` tells them
    apart, and `jarvis inspect` renders part 2 as `turn 1 (continued)`."""
    session = _numbered(write_transcript, [4.0, 50.0, 104.0], session="parts")

    anatomy = inspection.read_session(
        session, turn_starts=[(1, 0.0), (2, 100.0)])

    assert [(t.seq, t.part) for t in anatomy.turns] == [(1, 1), (1, 2), (2, 1)]
    assert anatomy.turns[0].as_dict()["part"] == 1


def test_a_transcript_turn_before_the_first_os_turn_is_unrecorded(write_transcript):
    """§3: what an injected or adopted session looks like. Never -1 — `NO_TURN` belongs
    to `wo_alarms` and this module must not import a store."""
    session = _numbered(write_transcript, [10.0, 104.0], session="injected")

    anatomy = inspection.read_session(session, turn_starts=[(1, 100.0)])

    assert [t.seq for t in anatomy.turns] == [0, 1]
    assert inspection.turn_name(0) == "an unrecorded turn"
    assert inspection.turn_name(7) == "turn 7"


def test_an_os_turn_with_no_transcript_turn_is_reported_not_dropped(write_transcript):
    """§3: the `/compact` case. `Anatomy.unmatched_os_turns` carries it and the renderer
    prints a line per entry rather than letting the number vanish."""
    session = _numbered(write_transcript, [4.0, 304.0], session="unmatched")

    anatomy = inspection.read_session(
        session, turn_starts=[(1, 0.0), (2, 100.0), (3, 200.0), (4, 300.0)])

    assert [t.seq for t in anatomy.turns] == [1, 4]
    assert anatomy.unmatched_os_turns == [2, 3]
    assert anatomy.as_dict()["unmatched_os_turns"] == [2, 3]


# -- the measurement of the fix: a polled turn against a blocked one -------------------
#
# §9 of docs/superpowers/specs/2026-09-29-a-lead-must-not-block-past-its-cache.md. The
# pair is the point: without the negative twin the positive proves only that the fixture
# is short.

SUITE_RUN = {"command": "uv run pytest tests/ -q", "run_in_background": True}
CHECK_INS = (260.0, 500.0, 740.0, 980.0, 1220.0)


def _polled_turn(write_transcript) -> str:
    """Twenty minutes of background suite, collected every four minutes."""
    rows = [prompt_row(0, "You are the worker agent for wo-1"),
            assistant_row(10, "m0", write=45_000),
            *tool_rows(20, 20.2, "t-launch", "Bash", SUITE_RUN)]
    for i, at in enumerate(CHECK_INS):
        # Each check-in is inside `TTL_5M` of the previous call, so its own delta write
        # is a PREFIX_MISS at worst and never an expiry, and the read is charged at 0.1x.
        rows.append(assistant_row(at - 1, f"m{i + 1}", write=25_000, read=150_000))
        rows.extend(tool_rows(at, at + 1.0, f"t{i}", "BashOutput",
                              {"bash_id": "b51fl7bhe"}))
    return write_transcript("polled", rows)


def _blocked_turn(write_transcript) -> str:
    """The same twenty minutes as ONE foreground join, and nothing collected."""
    return write_transcript("blocked", [
        prompt_row(0, "You are the worker agent for wo-1"),
        assistant_row(10, "m0", write=45_000),
        # No `tool_result`: the join is still open at `now`, which is the shape every one
        # of the 85 measured re-writes had.
        {"type": "assistant", "timestamp": stamp(20),
         "message": {"id": "m1", "model": "claude-opus-5",
                     "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0, "output_tokens": 1},
                     "content": [{"type": "tool_use", "id": "t-join",
                                  "name": "TaskOutput",
                                  "input": {"description": "the suite"}}]}},
        assistant_row(1220, "m2", write=193_139, read=0),
    ])


def test_polled_turn_has_no_mid_turn_ttl_expiry(write_transcript):
    """The fix, measured on the clock it is about: every gap is under `TTL_5M`, so no
    write in the turn is a `TTL_EXPIRY` and no join is open long enough to alarm.

    Pins the MEASUREMENT, not new code — `inspection` is unchanged — and it is issue
    868's named acceptance criterion, so it stays green from arrival."""
    anatomy = inspection.read_session(_polled_turn(write_transcript))
    (turn,) = anatomy.turns

    inside = [w for w in anatomy.writes if w.ts >= turn.started]
    assert inside, "the fixture wrote nothing, so it measures nothing"
    assert [w.cause for w in inside if w.cause == inspection.TTL_EXPIRY] == []
    assert max(w.gap for w in inside) <= inspection.TTL_5M
    assert [a.kind for a in inspection.alarms(
        anatomy, InspectConfig(), now=CHECK_INS[-1] + 2, dispatched=0.0)] == []


def test_the_same_twenty_minutes_as_one_join_still_produces_both(write_transcript):
    """The negative twin. Same wall clock, one blocking call: the conversation is
    re-sent at the write rate and the OS raises the join alarm that says so.

    Pins the MEASUREMENT of issue 868's fix, not new code: it is what stops the positive
    twin proving only that its fixture is short."""
    anatomy = inspection.read_session(_blocked_turn(write_transcript))

    expiries = [w for w in anatomy.writes if w.cause == inspection.TTL_EXPIRY]
    assert [w.written for w in expiries] == [193_139]
    assert expiries[0].gap > inspection.TTL_5M
    raised = inspection.alarms(anatomy, InspectConfig(), now=1221.0, dispatched=0.0)
    assert inspection.JOIN_ALARM in [a.kind for a in raised]


def test_turn_starts_hands_inspection_the_pairs_it_binds_on(started):
    """`ProjectStore.turn_starts` is the only new read §3 needs: `(seq, started_at)`,
    in seq order, for `read_session(turn_starts=...)`."""
    from jarvis.project_store import ProjectStore

    wo = ops.create_work_order("proj_a", "numbered")
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        first = store.create_turn(wo["id"], "dispatch", "go")
        second = store.create_turn(wo["id"], "message", "more")

        assert store.turn_starts(wo["id"]) == [
            (1, first["started_at"]), (2, second["started_at"])]
    finally:
        store.close()


# -- a boundary is classified once, and every turn gets its own -------------------------
#
# Spec docs/superpowers/specs/2026-09-29-inspect-boundary-classification.md, issue #867:
# `Turn.usage` summed `usage.priced()`, which classifies nothing, so every
# classification key of every turn was structurally zero.

COLD_FLOOR = 5_000


def _compact_boundary_row(at: float, pre: int = 293_382, post: int = 4_697) -> dict:
    return {"type": "system", "subtype": "compact_boundary", "timestamp": stamp(at),
            "compactMetadata": {"trigger": "manual", "preTokens": pre,
                                "postTokens": post}}


@pytest.fixture()
def three_turns(write_transcript) -> str:
    """Three turns: an ordinary one, one that COMPACTED, one that came back past the TTL.

    One boundary per cause the session can have, in two different turns — which is the
    only shape that can tell "attributed to its own turn" from "summed and smeared".
    """
    return write_transcript("s-boundaries", [
        prompt_row(0.0, "dispatch"),
        assistant_row(5.0, "a", write=200_000, read=0),
        assistant_row(10.0, "b", write=3_000, read=200_000),
        prompt_row(60.0, "keep going"),
        assistant_row(65.0, "c", write=5_000, read=203_000),
        _compact_boundary_row(70.0),
        # Seconds after the summary landed: the static head, and by gap and read alone
        # indistinguishable from a prefix miss.
        assistant_row(90.0, "d", write=15_380, read=12_776),
        prompt_row(600.0, "after the cache expired"),
        assistant_row(605.0, "e", write=120_000, read=0),
    ])


def test_the_turn_that_compacted_reports_its_own_compacted_boundary(three_turns):
    """#867's ask, and the only assertion in the issue that was ever about the fix."""
    cfg = InspectConfig(report_write_floor=10_000)
    anatomy = inspection.read_session(three_turns, cfg, cold_prefix_floor=COLD_FLOOR)

    assert [t.usage.boundaries_compact for t in anatomy.turns] == [0, 1, 0]
    assert anatomy.turns[1].usage.rewrite_compact_write == 15_380
    # …and the same write, labelled by the other classifier, on the same turn.
    compacted = [w for w in anatomy.writes if w.cause == inspection.COMPACTION]
    assert [w.written for w in compacted] == [15_380]
    assert anatomy.turns[1].started <= compacted[0].ts <= anatomy.turns[1].ended


def test_a_turn_reports_the_boundary_that_fell_in_it_and_one_context_peak(three_turns):
    """The per-turn `usage` block's keys were all structurally zero (spec §1.2), and
    `context_peak` was emitted twice under one name with one of the two always 0."""
    anatomy = inspection.read_session(three_turns, cold_prefix_floor=COLD_FLOOR)

    assert [t.usage.resume_boundaries for t in anatomy.turns] == [0, 1, 1]
    assert [t.usage.boundaries_ttl for t in anatomy.turns] == [0, 0, 1]
    assert anatomy.turns[2].usage.rewrite_ttl_write == 120_000
    for turn in anatomy.turns:
        payload = turn.as_dict()
        assert payload["usage"]["context_peak"] == payload["context_peak"]
    assert anatomy.turns[0].usage.rewrite_excess == 0


def test_the_session_total_agrees_with_jarvis_cost_and_with_its_own_turns(three_turns):
    """Two grains and a third surface, laid side by side — the rule
    `test_the_cohort_script_classifies_boundaries_exactly_as_the_bill_does` already
    enforces for `scripts/compaction_cohort.py`."""
    anatomy = inspection.read_session(three_turns, cold_prefix_floor=COLD_FLOOR)
    block = anatomy.as_dict()["rewrite"]
    theirs = usage.read_session(three_turns, COLD_FLOOR).total

    for key, field in (("compact_write", "rewrite_compact_write"),
                       ("ttl_write", "rewrite_ttl_write"),
                       ("prefix_write", "rewrite_prefix_write"),
                       ("cache_write", "cache_write")):
        assert block[key] == getattr(theirs, field), key
    assert (block["boundaries"], block["compact_boundaries"],
            block["ttl_boundaries"], block["undecided_boundaries"]) == (2, 1, 1, 0)
    assert block["tokens"] == anatomy.rewrite_excess()

    summed = usage.Usage()
    for turn in anatomy.turns:
        summed = summed + turn.usage
    assert summed.resume_boundaries == block["boundaries"]
    assert summed.boundaries_compact == block["compact_boundaries"]
    assert summed.rewrite_ttl_write == block["ttl_write"]
    # `Usage.__add__` takes the MAX of `context_peak`, so summing turns yields the
    # session's peak rather than a nonsense total.
    assert summed.context_peak == anatomy.as_dict()["context_peak"]


def test_an_unknown_floor_counts_every_boundary_and_guesses_no_cause(three_turns):
    """`jarvis inspect` over a moved catalog must not fail and must not invent the
    threshold (spec §4, rejected alternative 4). `UNDECIDED` is what that costs."""
    known = inspection.read_session(three_turns, cold_prefix_floor=COLD_FLOOR)
    anatomy = inspection.read_session(three_turns)
    block = anatomy.as_dict()["rewrite"]

    assert block["boundaries"] == known.as_dict()["rewrite"]["boundaries"]
    assert block["compact_boundaries"] == 1  # rule 2 needs no floor
    assert block["ttl_write"] == 0 and block["prefix_write"] == 0
    assert block["undecided_boundaries"] == block["boundaries"] - 1
    # The turn whose only boundary was left open: nothing in any write bucket, so the
    # share is `None` — "not measured", which a renderer must not print as 0%.
    open_turn = anatomy.turns[2].usage
    assert open_turn.boundaries_undecided == 1 and open_turn.resume_boundaries == 1
    assert open_turn.rewrite_ttl_share is None


def test_the_boundary_census_is_printed_and_not_only_in_the_json(three_turns, capsys):
    """A `--json`-only field nobody can see is half a fix. With the split left open the
    line says so instead of printing "0 expired", which would read as a finding."""
    rendered(inspection.read_session(three_turns, cold_prefix_floor=COLD_FLOOR))
    assert "boundaries 2 — 0 prefix, 1 expired, 1 compacted" in capsys.readouterr().out

    rendered(inspection.read_session(three_turns))
    line = next(ln for ln in capsys.readouterr().out.splitlines()
                if ln.strip().startswith("boundaries "))
    assert "boundaries 2 — 1 compacted, 1 unclassified (no os.cold_prefix_floor)" in line
    assert "expired" not in line and "prefix," not in line


def test_the_cold_prefix_floor_is_none_when_no_catalog_can_be_reached(jarvis_home):
    """`ops.inspect_config`'s guarantee: a report over files on disk must not fail
    because a catalog has moved. Unlike it, this falls back to None and not a number."""
    assert ops.cold_prefix_floor() is None
