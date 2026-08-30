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
    """`docs/anatomy-of-an-expensive-turn.md`'s summary table, to the second: 1886s of
    wall clock, 576s blocked on a subagent join, 58s executing tools.

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
                  content: list | None = None) -> dict:
    return {
        "type": "assistant", "timestamp": stamp(at),
        "message": {"id": mid, "model": "claude-opus-5",
                    "usage": {"input_tokens": 0, "cache_creation_input_tokens": write,
                              "cache_read_input_tokens": read, "output_tokens": 1},
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

    def write(session_id: str, rows: list[dict]) -> str:
        (root / "-proj" / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
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


# -- classifying a cache write ---------------------------------------------------------


def call(at: float, write: int, read: int = 0, cache_1h: int = 0) -> usage.Call:
    return usage.Call(ts=at, model="claude-opus-5", cache_write=write,
                      cache_read=read, cache_1h=cache_1h)


def test_a_small_write_seconds_after_the_last_call_is_the_cache_working():
    """THE trap this classification has to avoid. Inside a turn every call writes the
    delta it just added — a short gap, so the gap test alone calls it a prefix-miss. The
    floor is what makes the label mean something."""
    calls = [call(0, 50_000), call(1, 900), call(2, 1_200)]

    causes = [w.cause for w in inspection.classify_writes(calls)]

    assert causes == [inspection.COLD_START]


def test_a_one_hour_write_is_not_called_a_defect_for_surviving_ten_minutes():
    """Every Jarvis write before `FORCE_PROMPT_CACHING_5M` was a 1h write. Testing an
    old transcript against 300 seconds would report an honest expiry as a defect."""
    hour = [call(0, 50_000, cache_1h=50_000), call(600, 200_000)]
    five = [call(0, 50_000), call(600, 200_000)]

    assert inspection.classify_writes(hour)[1].cause == inspection.PREFIX_MISS
    assert inspection.classify_writes(five)[1].cause == inspection.TTL_EXPIRY


# -- the live alarm --------------------------------------------------------------------


def burning(*, wall: float = 0.0, write: int = 0, join: float = 0.0) -> tuple:
    """A one-turn anatomy that trips exactly the condition asked for."""
    turn = inspection.Turn(seq=1, started=0.0, ended=wall)
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

    long_turn, now = burning(wall=cfg.turn_minutes * 60 + 1)
    big_write, _ = burning(wall=60, write=cfg.write_tokens + 1)
    blocked, when = burning(join=cfg.join_seconds + 1)

    assert [a.kind for a in inspection.alarms(long_turn, cfg, now=now)] == \
        [inspection.TURN_ALARM]
    assert [a.kind for a in inspection.alarms(big_write, cfg, now=60)] == \
        [inspection.WRITE_ALARM]
    assert [a.kind for a in inspection.alarms(blocked, cfg, now=when)] == \
        [inspection.JOIN_ALARM]


def test_the_alarm_measures_the_running_turn_against_the_wall_clock():
    """A turn that is GENERATING has written no row for minutes. Judged against its own
    last line it reports how long ago it spoke, not how long it has been running."""
    anatomy, _ = burning(wall=60)
    cfg = InspectConfig(turn_minutes=10)

    assert inspection.alarms(anatomy, cfg) == []
    assert inspection.alarms(anatomy, cfg, now=11 * 60)[0].kind == inspection.TURN_ALARM


def test_nothing_is_raised_about_a_turn_that_is_already_paid_for():
    """Only the LAST turn is judged. An alarm about spend the user cannot now prevent is
    the noise that gets a cost alarm ignored."""
    spent = inspection.Turn(seq=1, started=0.0, ended=9_999.0)
    quiet = inspection.Turn(seq=2, started=10_000.0, ended=10_060.0)
    anatomy = inspection.Anatomy(session_id="s", found=True, turns=[spent, quiet])

    assert inspection.alarms(anatomy, InspectConfig(), now=10_060.0) == []


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

    quiet = inspection.live_alarms(session, InspectConfig(), now=200)
    loud = inspection.live_alarms(session, InspectConfig(), wo_id="wo-1", now=400)

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

    assert inspection.live_alarms(session, InspectConfig(), now=500) == []


def test_the_alarm_can_be_turned_off():
    anatomy, now = burning(wall=10 * 3600)

    assert inspection.alarms(anatomy, InspectConfig(enabled=False), now=now) == []


def test_the_defaults_are_the_measured_ones():
    """They are load-bearing: each was set where it fires on a small minority of the
    fleet's real work orders, and a change to one is a change to how often the user is
    interrupted. See `catalog.InspectConfig`."""
    cfg = InspectConfig()

    assert (cfg.turn_minutes, cfg.join_seconds, cfg.write_tokens) == (60, 300, 300_000)
    assert cfg.join_seconds == inspection.TTL_5M


# -- configuration ---------------------------------------------------------------------


def test_the_thresholds_are_settable_through_the_config_console():
    """`os.inspect.*`, which `config_version.resolve` picks up reflectively — so
    `jarvis config set` reaches it with no edit to the console."""
    from jarvis import config_version

    cat = catalog.parse_catalog({"os": {"inspect": {"turn_minutes": 20}},
                                 "projects": []})
    resolved = config_version.resolve(cat)

    assert cat.os.inspect.turn_minutes == 20
    assert resolved["os.inspect.turn_minutes"] == 20
    assert resolved["os.inspect.write_tokens"] == 300_000  # a default, materialised


def test_a_threshold_of_zero_is_refused_rather_than_flagging_everything():
    with pytest.raises(catalog.CatalogError):
        catalog.parse_catalog({"os": {"inspect": {"write_tokens": 0}}, "projects": []})


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
            prompt_row(started_at, "You are the worker agent for wo-1"),
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

        # AND NEVER AGAIN FOR THIS TURN. The user putting the flag down must not bring
        # the same sentence back on the next tick — that is how a cost alarm becomes
        # noise, and then it is worse than nothing.
        store.clear_attention(wo["id"])
        daemon.check_burning_turns(daemon.catalog.projects[0], store)

        assert store.get_work_order(wo["id"])["needs_attention"] == 0
        assert len(store.events_of_kind(wo["id"], "cost_alarm")) == 1
    finally:
        store.close()
