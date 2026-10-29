"""Who is still buying the ONE-HOUR cache write, and which of the two faults that is (docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md)"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jarvis import inspection, ops, probes, usage
from jarvis.catalog import CatalogError, InspectConfig, load_catalog, parse_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import NO_TURN, ProjectStore

DAY = 86_400


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(started):
    s = ProjectStore(ops.registered_project_paths()["proj_a"])
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def transcripts(tmp_path, monkeypatch):
    """A fake `~/.claude/projects` tree — the only place a foreign session exists"""
    root = tmp_path / "transcripts"
    root.mkdir()
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, rows: list[dict], *, slug: str = "-somewhere") -> None:
        directory = root / slug
        directory.mkdir(exist_ok=True)
        (directory / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    return write


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def prompt(at: float, *, sdk: bool) -> dict:
    row = {"type": "user", "timestamp": _stamp(at),
           "message": {"content": "You are the worker agent for wo-1" if sdk else "hi"}}
    if sdk:
        row["promptSource"] = "sdk"
    return row


def wrote_1h(at: float, mid: str, tokens: int) -> dict:
    return {
        "type": "assistant", "timestamp": _stamp(at),
        "message": {"id": mid, "model": "claude-opus-5", "usage": {
            "input_tokens": 0, "cache_creation_input_tokens": tokens,
            "cache_read_input_tokens": 0, "output_tokens": 1,
            "cache_creation": {"ephemeral_1h_input_tokens": tokens,
                               "ephemeral_5m_input_tokens": 0}}},
    }


def _both_halves(transcripts, now, *, dispatched: int, foreign: int) -> None:
    """ONE session file holding both — wo-2df8828c's shape, which is the whole point"""
    transcripts("s-mixed", [
        prompt(now - DAY, sdk=True),
        wrote_1h(now - DAY + 1, "m1", dispatched),
        prompt(now - DAY + 2, sdk=False),
        wrote_1h(now - DAY + 3, "m2", foreign),
    ])


def _hour_titles(daemon_mod) -> set[str]:
    """The inbox title of the 1h breach — the dict also carries the arm-miss kind"""
    return {daemon_mod.CACHE_1H_INBOX_TITLE[inspection.CACHE_1H_DISPATCHED_ALARM]}


def _found(now, *, dispatched=0, foreign=0):
    """An `HourWrites` list without touching a disk — for the prose tests below"""
    return [inspection.HourWrites(session_id="s-mixed", directory="-somewhere",
                                  dispatched=dispatched, foreign=foreign,
                                  first_ts=now - DAY, last_ts=now - DAY)]


# -- 1. the arithmetic and the two thresholds


def test_only_the_dispatched_half_raises(transcripts):
    """A person's own sessions are never alarmed on: the 1h default can win at human pace (wo-10a5f151)"""
    now = 1_789_000_000.0
    cfg = InspectConfig(alarm_cache_1h_dispatched_tokens=50_000)

    over = inspection.hour_alarms(_found(now, dispatched=60_000, foreign=9_000_000),
                                  cfg, days=7)
    under = inspection.hour_alarms(_found(now, dispatched=10_000, foreign=9_000_000),
                                   cfg, days=7)

    assert [a.kind for a in over] == [inspection.CACHE_1H_DISPATCHED_ALARM]
    assert under == []


def test_the_dispatched_reason_blames_the_os_and_never_the_users_config():
    """The opposite sentence for the opposite fault"""
    now = 1_789_000_000.0
    (alarm,) = inspection.hour_alarms(_found(now, dispatched=900_000), InspectConfig(),
                                      days=7)

    assert "BREACH" in alarm.reason and "defect in the OS" in alarm.reason
    # The claim is about the turns stamped `5m` (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)
    assert "wo_turns.cache_ttl" in alarm.reason and "5m" in alarm.reason
    assert "~/.claude/settings.json" not in alarm.reason
    assert "MUST NOT TRY" not in alarm.reason


def test_the_premium_is_the_rate_difference_and_not_the_price():
    """These tokens were written either way"""
    assert inspection._hour_premium_usd(1_000_000) == pytest.approx(3.75)

    now = 1_789_000_000.0
    (alarm,) = inspection.hour_alarms(_found(now, dispatched=1_000_000), InspectConfig(),
                                      days=7)
    assert "$3.75" in alarm.reason and "Opus list" in alarm.reason


# -- 2. the daemon: who raises it, on what, and how often


def _raise(daemon, store, transcripts, *, dispatched=0, foreign=0, now=None, **over):
    now = now if now is not None else __import__("jarvis.db", fromlist=["db"]).now()
    _both_halves(transcripts, now, dispatched=dispatched, foreign=foreign)
    project = daemon.catalog.projects[0]
    project.inspect = InspectConfig(**over)
    daemon.check_cache_ttl(project, store)
    return store.alarms_across()


def test_the_daemon_raises_one_alarm_per_fault_on_the_latest_settled_order(
        started, store, transcripts):
    store.create_work_order("an old one", status="completed")
    carrier = store.create_work_order("the newest settled one", status="completed")

    rows = _raise(started, store, transcripts, dispatched=90_000, foreign=900_000)

    assert {r["kind"] for r in rows} == {inspection.CACHE_1H_DISPATCHED_ALARM}
    for row in rows:
        assert row["wo_id"] == carrier["id"]
        assert row["seq"] == NO_TURN          # it judged no turn
        assert row["source"] == "cost"
        assert row["alarm_status"] == "raised"     # on the supervisor's queue
    # The carrier is a hook, not a subject: it must not be flagged for the user
    assert not store.get_work_order(carrier["id"])["needs_attention"]


def test_the_inbox_says_it_is_a_transport_defect(started, store, transcripts):
    """The inbox row is the DURABLE half — it is what reaches `jarvis status` and Telegram"""
    store.create_work_order("carrier", status="completed")
    _raise(started, store, transcripts, dispatched=90_000, foreign=900_000)

    from jarvis import daemon as daemon_mod

    titles = {r["title"] for r in started.central.unacked_inbox()}

    assert _hour_titles(daemon_mod) <= titles
    assert len({t for t in titles if "one-hour cache" in t}) == 1


def test_only_the_os_owning_project_scans(started, store, transcripts, tmp_path,
                                          jarvis_home, monkeypatch):
    """A fleet reading carried once"""
    from jarvis import schedule

    store.create_work_order("carrier", status="completed")
    project = started.catalog.projects[0]
    owner = schedule.os_owner((p.name, p.path) for p in started.catalog.projects)
    assert owner == project.name, "the single test project owns the install by fallback"

    # Same project, now NOT the owner
    monkeypatch.setattr(Daemon, "_os_owner", lambda self: "somebody-else")
    assert _raise(started, store, transcripts, dispatched=9_000_000) == []


def test_it_is_raised_once_per_window_and_again_after_it(started, store, transcripts):
    """The condition is STANDING — still true on the next pass"""
    from jarvis import db

    store.create_work_order("carrier", status="completed")
    first = _raise(started, store, transcripts, dispatched=900_000)
    assert len(first) == 1

    started.check_cache_ttl(started.catalog.projects[0], store)
    assert len(store.alarms_across()) == 1, "a standing condition must not re-raise"

    store.conn.execute("UPDATE wo_alarms SET ts=?", (db.now() - 30 * DAY,))
    store.conn.commit()
    started.check_cache_ttl(started.catalog.projects[0], store)
    assert len(store.alarms_across()) == 2


def test_the_off_switch_covers_this_too(started, store, transcripts):
    """`inspect.enabled` is one switch for "raise nothing here\""""
    store.create_work_order("carrier", status="completed")
    assert _raise(started, store, transcripts, dispatched=9_000_000, enabled=False) == []


def test_with_no_settled_order_nothing_is_raised_and_nothing_breaks(started, store,
                                                                    transcripts):
    """`wo_alarms.wo_id` is a real foreign key and a young project has nothing to hang one on"""
    store.create_work_order("still running", status="running")

    assert _raise(started, store, transcripts, dispatched=9_000_000) == []


def test_a_quiet_fleet_raises_nothing(started, store, transcripts):
    """What this looks like when it is WORKING"""
    from jarvis import db

    store.create_work_order("carrier", status="completed")
    now = db.now()
    transcripts("s-good", [
        prompt(now - DAY, sdk=True),
        {"type": "assistant", "timestamp": _stamp(now - DAY + 1),
         "message": {"id": "m1", "model": "claude-opus-5", "usage": {
             "input_tokens": 0, "cache_creation_input_tokens": 9_000_000,
             "cache_read_input_tokens": 0, "output_tokens": 1,
             "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                "ephemeral_5m_input_tokens": 9_000_000}}}},
    ])
    project = started.catalog.projects[0]
    project.inspect = InspectConfig()
    started.check_cache_ttl(project, store)

    assert store.alarms_across() == []


def test_a_write_outside_the_window_is_not_in_it(started, store, transcripts):
    """The window is a cohort, and a leak fixed a month ago must stop being reported"""
    from jarvis import db

    store.create_work_order("carrier", status="completed")
    now = db.now()
    project = started.catalog.projects[0]
    project.inspect = InspectConfig()

    transcripts("s-old", [prompt(now - 60 * DAY, sdk=True),
                          wrote_1h(now - 60 * DAY + 1, "m1", 9_000_000)])
    started.check_cache_ttl(project, store)
    assert store.alarms_across() == []

    transcripts("s-new", [prompt(now - DAY, sdk=True),
                          wrote_1h(now - DAY + 1, "m2", 9_000_000)])
    started.check_cache_ttl(project, store)
    assert [r["kind"] for r in store.alarms_across()] == \
        [inspection.CACHE_1H_DISPATCHED_ALARM]


def test_the_real_loop_raises_these_on_the_offset_tick(started, store, transcripts):
    """THE WIRE, and the only test that fails if `Daemon.tick` stops calling this at all"""
    from jarvis import daemon as daemon_mod, db

    carrier = store.create_work_order("carrier", status="completed")
    _both_halves(transcripts, db.now(), dispatched=9_000_000, foreign=9_000_000)
    started.tick_count = daemon_mod.CACHE_TTL_TICK_OFFSET - 1

    started.tick()

    assert started.tick_count == daemon_mod.CACHE_TTL_TICK_OFFSET
    rows = store.alarms_across()
    assert {r["kind"] for r in rows} == {inspection.CACHE_1H_DISPATCHED_ALARM}
    assert all(r["wo_id"] == carrier["id"] for r in rows)
    titles = {r["title"] for r in started.central.unacked_inbox()}
    assert _hour_titles(daemon_mod) <= titles


# -- 3. the arm the turn ran on (spec 2026-10-15-the-hour-on-worker-turns-only.md §5)


def _armed_turn(store, transcripts, now, *, arm: str, tokens: int) -> None:
    """A dispatched 1h write inside a turn STAMPED with `arm`, in that turn's session"""
    wo = store.create_work_order("armed", status="running", session_id="s-armed")
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="go", cache_ttl=arm)
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (now - DAY - 10, now - DAY + 10, turn["id"]))
    store.conn.commit()
    transcripts("s-armed", [prompt(now - DAY, sdk=True),
                            wrote_1h(now - DAY + 1, "m1", tokens)])


def test_an_hour_the_catalog_asked_for_raises_no_alarm(started, store, transcripts):
    """The OS must not accuse itself of a defect it was told to have"""
    from jarvis import db

    store.create_work_order("carrier", status="completed")
    now = db.now()
    _armed_turn(store, transcripts, now, arm="1h", tokens=9_000_000)
    project = started.catalog.projects[0]
    project.inspect = InspectConfig()

    started.check_cache_ttl(project, store)

    assert store.alarms_across() == []


def test_the_same_write_on_the_control_arm_still_raises(started, store, transcripts):
    """The pair: one stamp apart, and the breach is still reported"""
    from jarvis import db

    store.create_work_order("carrier", status="completed")
    now = db.now()
    _armed_turn(store, transcripts, now, arm="5m", tokens=9_000_000)
    project = started.catalog.projects[0]
    project.inspect = InspectConfig()

    started.check_cache_ttl(project, store)

    assert [r["kind"] for r in store.alarms_across()] == \
        [inspection.CACHE_1H_DISPATCHED_ALARM]


def test_one_tick_of_the_real_loop_does_not_scan(started, store, transcripts):
    """The negative half of the pair above"""
    from jarvis import db

    store.create_work_order("carrier", status="completed")
    _both_halves(transcripts, db.now(), dispatched=9_000_000, foreign=9_000_000)

    started.tick()

    assert started.tick_count == 1
    assert store.alarms_across() == []


def test_the_offset_is_a_real_offset(started):
    """The constant the pair above rests on"""
    from jarvis import daemon as daemon_mod

    assert daemon_mod.CACHE_TTL_TICK_OFFSET != 1
    assert 0 < daemon_mod.CACHE_TTL_TICK_OFFSET < daemon_mod.CACHE_TTL_EVERY_TICKS


# -- 3. the alarm actually reaches the supervisor


def _raised(started, store, transcripts):
    store.create_work_order("carrier", status="completed")
    rows = _raise(started, store, transcripts, dispatched=900_000)
    return rows[0]


def test_the_drain_hands_it_to_the_judge(started, store, transcripts):
    """THE ENGAGEMENT PIN"""
    from jarvis.neo_store import NeoStore

    alarm = _raised(started, store, transcripts)
    project = started.catalog.projects[0]
    assert alarm["id"] in {r["id"] for r in store.alarms_across(statuses=("raised",))}

    seen: list[tuple] = []

    class _Judge:
        @staticmethod
        def review(pstore, neo_store, project_name, wo, row, cfg, **kw):
            seen.append((row["id"], row["kind"], wo["status"]))
            return {"decision": "ack", "reason": "the user's own sessions",
                    "note": "ok", "failed": False}

    neo_store, central = NeoStore(), started.central
    try:
        started._drain_project_alarms(project, store, neo_store, central, _Judge)
    finally:
        neo_store.close()

    judged = {row[0]: row for row in seen}
    assert alarm["id"] in judged, "the drain never handed this alarm to the judge"
    assert judged[alarm["id"]][1:] == (inspection.CACHE_1H_DISPATCHED_ALARM, "completed")
    assert not [r for r in store.alarms_across() if r["alarm_status"] == "skipped"]


def test_the_evidence_packet_carries_the_kind_and_no_turn_number(started, store,
                                                                   transcripts):
    """The judge sees the packet and never the alarm row"""
    from jarvis import supervisor
    from jarvis.catalog import SupervisorConfig

    alarm = _raised(started, store, transcripts)
    carrier = store.get_work_order(alarm["wo_id"])

    packet = supervisor.build_evidence(
        store, {"kind": "work_order", "row": carrier}, alarm,
        SupervisorConfig(), InspectConfig())

    assert "raised on no particular turn" in packet
    assert "turn -1" not in packet
    assert inspection.CACHE_1H_DISPATCHED_ALARM in packet
    assert "dispatch.cache_ttl_env" in packet


def test_the_supervisor_is_told_these_two_are_not_this_projects_fault(started):
    """The persona is the only place the judge learns that the carrier means nothing and that one of the..."""
    from jarvis import supervisor

    for prompt_text in (supervisor.SUPERVISOR_PERSONA,
                        supervisor.ALARM_REVIEWER_PERSONA):
        assert inspection.CACHE_1H_FOREIGN_ALARM in prompt_text
        assert inspection.CACHE_1H_DISPATCHED_ALARM in prompt_text
        assert "must never write" in prompt_text


# -- 4. the guards around the two new kinds


def test_each_alarm_family_owns_its_own_inbox_titles():
    """WHY THERE ARE TWO DICTS"""
    from jarvis import daemon as daemon_mod

    assert set(daemon_mod.CACHE_1H_INBOX_TITLE) == {
        inspection.CACHE_1H_DISPATCHED_ALARM, inspection.CACHE_ARM_MISSED_ALARM}
    assert not (set(daemon_mod.CACHE_1H_INBOX_TITLE)
                & set(daemon_mod.REWRITE_INBOX_TITLE))


def test_a_probe_cannot_be_named_after_one_of_these_alarms():
    """`probes.RESERVED_IDS` duplicates `inspection.ALARM_KINDS` as literals"""
    assert set(inspection.ALARM_KINDS) == set(probes.RESERVED_IDS)


def test_the_thresholds_are_settings_and_a_zero_is_refused_where_it_was_typed(tmp_path):
    """A window of zero days reports nothing for ever and a token floor of zero alarms on the first write..."""
    def _catalog(**inspect):
        return {"os": {"defaults": {"model": "sonnet"}, "inspect": inspect},
                "projects": [{"name": "p", "path": str(tmp_path)}]}

    cfg = parse_catalog(_catalog(alarm_cache_1h_dispatched_tokens=1_000)).os.inspect
    assert cfg.alarm_cache_1h_dispatched_tokens == 1_000
    assert cfg.alarm_cache_1h_window_days == 7, "the others still fall through"

    for key in ("alarm_cache_1h_window_days", "alarm_cache_1h_dispatched_tokens", "alarm_cache_arm_missed_tokens"):
        with pytest.raises(CatalogError, match=key):
            parse_catalog(_catalog(**{key: 0}))


# -- 5. the stamp against the RATE (spec 2026-10-22-the-arm-that-did-not-apply.md)


def _rows(*turns):
    """`(project, wo_id, seq, arm, ts, cache_1h, cache_5m)`, the shape the daemon gathers"""
    return [("proj_a", f"wo-{n}", n, arm, 1_789_000_000.0, hour, five)
            for n, (arm, hour, five) in enumerate(turns, start=1)]


def test_an_arm_that_did_not_apply_is_a_miss_in_both_directions():
    """The 1h stamp that paid 5m is issue 1187; the mirror is the hour nobody asked for"""
    misses = inspection.arm_misses(_rows(("1h", 0, 300_000), ("5m", 90_000, 0)))

    assert [(m.arm, m.tokens) for m in misses] == [("1h", 300_000), ("5m", 90_000)]


def test_an_arm_that_applied_and_an_arm_that_claims_nothing_are_not_misses():
    """`cli` asks for no rate, so no rate can be withheld from it"""
    assert inspection.arm_misses(_rows(
        ("1h", 300_000, 0),        # the hour was armed and the hour was sold
        ("5m", 0, 300_000),        # likewise, the control arm
        ("cli", 0, 300_000), ("cli", 300_000, 0),
        ("", 0, 300_000),          # a pre-column turn
    )) == []


def test_a_turn_whose_split_was_never_reported_is_not_evidence():
    """kn-dbf6f779: only the ephemeral split says what was paid — silence says nothing"""
    assert inspection.arm_misses(_rows(("1h", 0, 0), ("5m", 0, 0))) == []


def test_the_missed_tokens_answer_to_their_own_threshold():
    cfg = InspectConfig(alarm_cache_arm_missed_tokens=100_000)
    under = inspection.arm_misses(_rows(("1h", 0, 99_999)))
    over = inspection.arm_misses(_rows(("1h", 0, 60_000), ("5m", 40_000, 0)))

    assert inspection.arm_miss_alarms(under, cfg, days=7) == []
    assert [a.kind for a in inspection.arm_miss_alarms(over, cfg, days=7)] == \
        [inspection.CACHE_ARM_MISSED_ALARM]


def test_the_reason_names_both_sides_the_premium_and_the_turns():
    misses = inspection.arm_misses(_rows(("1h", 0, 1_000_000), ("5m", 1_000_000, 0)))
    (alarm,) = inspection.arm_miss_alarms(misses, InspectConfig(), days=7)

    assert "2,000,000 cache-write tokens" in alarm.reason and "last 7 days" in alarm.reason
    assert "ephemeral_1h = 0" in alarm.reason and "ephemeral_5m = 0" in alarm.reason
    assert "$3.75" in alarm.reason, "the premium is charged on the 5m-armed half only"
    assert "DEFECT IN THE OS" in alarm.reason
    assert "wo-1 turn 1 in proj_a (armed 1h" in alarm.reason
    assert "~/.claude/settings.json" not in alarm.reason, "never the user's own file"


def _stamped_rate(store, *, arm: str, cache_1h: int, cache_5m: int) -> dict:
    """A SETTLED turn stamped `arm` whose recorded usage says what it really paid"""
    wo = store.create_work_order(f"armed {arm}", status="completed", session_id="s-rate")
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="go", cache_ttl=arm)
    store.finish_turn(turn["id"], "done",
                      usage_json=json.dumps({"usage_v": 9, "cache_1h": cache_1h,
                                             "cache_5m": cache_5m}))
    return wo


def test_the_daemon_raises_it_off_the_record_with_no_transcript_at_all(
        started, store, transcripts):
    """THE WHOLE POINT: the 1h-armed session never mentions the 1h key, so only the record sees it"""
    carrier = _stamped_rate(store, arm="1h", cache_1h=0, cache_5m=900_000)
    project = started.catalog.projects[0]
    project.inspect = InspectConfig()

    started.check_cache_ttl(project, store)

    rows = store.alarms_across()
    assert [r["kind"] for r in rows] == [inspection.CACHE_ARM_MISSED_ALARM]
    assert rows[0]["wo_id"] == carrier["id"] and rows[0]["seq"] == NO_TURN
    assert rows[0]["source"] == "cost" and rows[0]["alarm_status"] == "raised"

    from jarvis import daemon as daemon_mod

    titles = {r["title"] for r in started.central.unacked_inbox()}
    assert daemon_mod.CACHE_1H_INBOX_TITLE[inspection.CACHE_ARM_MISSED_ALARM] in titles


def test_an_arm_that_applied_raises_nothing(started, store, transcripts):
    """What this looks like when the transport is working"""
    _stamped_rate(store, arm="1h", cache_1h=9_000_000, cache_5m=0)
    project = started.catalog.projects[0]
    project.inspect = InspectConfig()

    started.check_cache_ttl(project, store)

    assert store.alarms_across() == []


def test_a_turn_outside_the_window_is_not_in_it(started, store, transcripts):
    """The window is the same cohort the 1h pair uses"""
    from jarvis import db

    wo = _stamped_rate(store, arm="1h", cache_1h=0, cache_5m=900_000)
    store.conn.execute("UPDATE wo_turns SET started_at=?", (db.now() - 60 * DAY,))
    store.conn.commit()
    project = started.catalog.projects[0]
    project.inspect = InspectConfig()

    started.check_cache_ttl(project, store)
    assert store.alarms_across() == []

    store.conn.execute("UPDATE wo_turns SET started_at=?", (db.now() - DAY,))
    store.conn.commit()
    started.check_cache_ttl(project, store)
    assert [r["wo_id"] for r in store.alarms_across()] == [wo["id"]]


def test_an_unreadable_transcript_tree_no_longer_hides_the_record_half(
        started, store, transcripts, monkeypatch):
    """The two halves have different sources, so one failing must not silence the other"""
    _stamped_rate(store, arm="5m", cache_1h=900_000, cache_5m=0)
    project = started.catalog.projects[0]
    project.inspect = InspectConfig()
    monkeypatch.setattr(inspection, "one_hour_writes",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("gone")))

    started.check_cache_ttl(project, store)

    assert [r["kind"] for r in store.alarms_across()] == \
        [inspection.CACHE_ARM_MISSED_ALARM]


def test_the_supervisor_is_told_this_one_is_always_the_oss_own_defect(started):
    """The judge must not answer it with a line in the user's settings file"""
    from jarvis import supervisor

    for prompt_text in (supervisor.SUPERVISOR_PERSONA,
                        supervisor.ALARM_REVIEWER_PERSONA):
        assert inspection.CACHE_ARM_MISSED_ALARM in prompt_text
