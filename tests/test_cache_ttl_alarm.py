"""Who is still buying the ONE-HOUR cache write, and which of the two faults that is.

Issue 164 item 3; finding 3 of
docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md.

THE DISCRIMINATING FIXTURE IS THE ONE FILE THAT HOLDS BOTH HALVES, because that is the
real shape: wo-2df8828c's transcript is a Jarvis-dispatched worker session whose worktree
was reopened by hand after the order completed, and keying on the session id blames the OS
for all 2.5M of a person's tokens. So the tests below mix the two inside one session
wherever the arithmetic allows it — a fixture that is all one half passes under "classify
the file" just as well as under "classify the turn".

The second thing pinned throughout is the ASYMMETRY. `cache-1h-dispatched` is a defect in
this repository; `cache-1h-foreign` is a line in a person's own configuration that Jarvis
must never write. Every surface has to keep them apart, and a test that only checks a row
exists would be green with the two merged.
"""

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
    """A fake `~/.claude/projects` tree — the only place a foreign session exists."""
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
    """ONE session file holding both — wo-2df8828c's shape, which is the whole point."""
    transcripts("s-mixed", [
        prompt(now - DAY, sdk=True),
        wrote_1h(now - DAY + 1, "m1", dispatched),
        prompt(now - DAY + 2, sdk=False),
        wrote_1h(now - DAY + 3, "m2", foreign),
    ])


def _found(now, *, dispatched=0, foreign=0):
    """An `HourWrites` list without touching a disk — for the prose tests below."""
    return [inspection.HourWrites(session_id="s-mixed", directory="-somewhere",
                                  dispatched=dispatched, foreign=foreign,
                                  first_ts=now - DAY, last_ts=now - DAY)]


# -- 1. the arithmetic and the two thresholds -----------------------------------------


def test_each_kind_answers_to_its_own_threshold(transcripts):
    """Paired in both directions, because one threshold serving both would satisfy any
    single-sided assertion. The dispatched floor is an order of magnitude lower, so the
    middle case — over one, under the other — is the one that discriminates."""
    now = 1_789_000_000.0
    cfg = InspectConfig(alarm_cache_1h_dispatched_tokens=50_000,
                        alarm_cache_1h_tokens=500_000)

    over_both = inspection.hour_alarms(_found(now, dispatched=60_000, foreign=600_000),
                                       cfg, days=7)
    between = inspection.hour_alarms(_found(now, dispatched=60_000, foreign=100_000),
                                     cfg, days=7)
    under_both = inspection.hour_alarms(_found(now, dispatched=10_000, foreign=100_000),
                                        cfg, days=7)

    assert [a.kind for a in over_both] == [inspection.CACHE_1H_DISPATCHED_ALARM,
                                           inspection.CACHE_1H_FOREIGN_ALARM]
    assert [a.kind for a in between] == [inspection.CACHE_1H_DISPATCHED_ALARM]
    assert under_both == []


def test_the_dispatched_alarm_comes_first_because_it_is_the_worse_finding(transcripts):
    """`alarms`' ordering contract is most-actionable-first, and only one of these two
    is a defect anybody here can fix."""
    now = 1_789_000_000.0
    raised = inspection.hour_alarms(_found(now, dispatched=9_000_000, foreign=10_000_000),
                                    InspectConfig(), days=7)

    assert raised[0].kind == inspection.CACHE_1H_DISPATCHED_ALARM


def test_the_foreign_reason_carries_the_remedy_and_says_it_is_not_ours_to_apply():
    """THE HONEST CON, in the alarm rather than in a comment. The supervisor files a work
    order off this text and nothing else, so the line to add has to be IN it — and so
    does the limit, or the remedy proposed will be for Jarvis to write the file."""
    now = 1_789_000_000.0
    (alarm,) = inspection.hour_alarms(_found(now, foreign=900_000), InspectConfig(),
                                      days=7)

    assert "FORCE_PROMPT_CACHING_5M" in alarm.reason
    assert "~/.claude/settings.json" in alarm.reason
    assert "MUST NOT TRY" in alarm.reason
    assert "s-mixed in -somewhere" in alarm.reason, "the session, which finding 3 turns on"
    assert "900,000 tokens" in alarm.reason
    assert "last 7 days" in alarm.reason


def test_the_dispatched_reason_blames_the_os_and_never_the_users_config():
    """The opposite sentence for the opposite fault. If this one carried the personal
    settings line, the supervisor would file a work order telling a person to fix a bug
    in this repository."""
    now = 1_789_000_000.0
    (alarm,) = inspection.hour_alarms(_found(now, dispatched=900_000), InspectConfig(),
                                      days=7)

    assert "BREACH" in alarm.reason and "defect in the OS" in alarm.reason
    assert "claude_cli.cache_env" in alarm.reason
    assert "~/.claude/settings.json" not in alarm.reason
    assert "MUST NOT TRY" not in alarm.reason


def test_the_premium_is_the_rate_difference_and_not_the_price():
    """These tokens were written either way; the avoidable money is 2.0x - 1.25x, not the
    whole write (kn-f94abf34 (0)). 1M tokens at Opus $5/MTok: 0.75 x 5 = $3.75."""
    assert inspection._hour_premium_usd(1_000_000) == pytest.approx(3.75)

    now = 1_789_000_000.0
    (alarm,) = inspection.hour_alarms(_found(now, foreign=1_000_000), InspectConfig(),
                                      days=7)
    assert "$3.75" in alarm.reason and "Opus list" in alarm.reason


# -- 2. the daemon: who raises it, on what, and how often ------------------------------


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

    assert {r["kind"] for r in rows} == {inspection.CACHE_1H_DISPATCHED_ALARM,
                                         inspection.CACHE_1H_FOREIGN_ALARM}
    for row in rows:
        assert row["wo_id"] == carrier["id"]
        assert row["seq"] == NO_TURN          # it judged no turn
        assert row["source"] == "cost"
        assert row["alarm_status"] == "raised"     # on the supervisor's queue
    # The carrier is a hook, not a subject: it must not be flagged for the user.
    assert not store.get_work_order(carrier["id"])["needs_attention"]


def test_the_inbox_says_which_of_the_two_faults_it_is(started, store, transcripts):
    """The inbox row is the DURABLE half — it is what reaches `jarvis status` and
    Telegram. Two rows reading the same would merge the exact distinction this change
    exists to make, and send the reader to the wrong file."""
    store.create_work_order("carrier", status="completed")
    _raise(started, store, transcripts, dispatched=90_000, foreign=900_000)

    from jarvis import daemon as daemon_mod

    titles = {r["title"] for r in started.central.unacked_inbox()}

    assert set(daemon_mod.CACHE_1H_INBOX_TITLE.values()) <= titles
    assert len({t for t in titles if "one-hour cache" in t}) == 2


def test_only_the_os_owning_project_scans(started, store, transcripts, tmp_path,
                                          jarvis_home, monkeypatch):
    """A fleet reading carried once. Transcripts belong to no project, so N projects
    raising it is N-1 copies of the same alarm — `schedule.JobContext` made this call
    already for the OS-level doctor checks."""
    from jarvis import schedule

    store.create_work_order("carrier", status="completed")
    project = started.catalog.projects[0]
    owner = schedule.os_owner((p.name, p.path) for p in started.catalog.projects)
    assert owner == project.name, "the single test project owns the install by fallback"

    # Same project, now NOT the owner.
    monkeypatch.setattr(Daemon, "_os_owner", lambda self: "somebody-else")
    assert _raise(started, store, transcripts, foreign=9_000_000) == []


def test_it_is_raised_once_per_window_and_again_after_it(started, store, transcripts):
    """The condition is STANDING — still true on the next pass, which is what makes it
    different from a burning turn. Both halves: a second pass inside the window adds
    nothing, and back-dating the row past the window lets it raise afresh. Without the
    negative half a dedupe that never raises again would pass."""
    from jarvis import db

    store.create_work_order("carrier", status="completed")
    first = _raise(started, store, transcripts, foreign=900_000)
    assert len(first) == 1

    started.check_cache_ttl(started.catalog.projects[0], store)
    assert len(store.alarms_across()) == 1, "a standing condition must not re-raise"

    store.conn.execute("UPDATE wo_alarms SET ts=?", (db.now() - 30 * DAY,))
    store.conn.commit()
    started.check_cache_ttl(started.catalog.projects[0], store)
    assert len(store.alarms_across()) == 2


def test_the_off_switch_covers_this_too(started, store, transcripts):
    """`inspect.enabled` is one switch for "raise nothing here" — a second way to turn
    one thing off is a second way to be surprised by it."""
    store.create_work_order("carrier", status="completed")
    assert _raise(started, store, transcripts, foreign=9_000_000, enabled=False) == []


def test_with_no_settled_order_nothing_is_raised_and_nothing_breaks(started, store,
                                                                    transcripts):
    """`wo_alarms.wo_id` is a real foreign key and a young project has nothing to hang
    one on. The finding is not lost — it is still true on the next pass."""
    store.create_work_order("still running", status="running")

    assert _raise(started, store, transcripts, foreign=9_000_000) == []


def test_a_quiet_fleet_raises_nothing(started, store, transcripts):
    """What this looks like when it is WORKING, which is the state the fleet is in as
    this ships: the user's own `FORCE_PROMPT_CACHING_5M` is holding and no 1h write has
    been made anywhere since 2026-08-31."""
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
    """The window is a cohort, and a leak fixed a month ago must stop being reported.
    Paired with the same tokens inside the window, or "raises nothing" would also be
    satisfied by a scan that read no files at all."""
    from jarvis import db

    store.create_work_order("carrier", status="completed")
    now = db.now()
    project = started.catalog.projects[0]
    project.inspect = InspectConfig()

    transcripts("s-old", [prompt(now - 60 * DAY, sdk=False),
                          wrote_1h(now - 60 * DAY + 1, "m1", 9_000_000)])
    started.check_cache_ttl(project, store)
    assert store.alarms_across() == []

    transcripts("s-new", [prompt(now - DAY, sdk=False),
                          wrote_1h(now - DAY + 1, "m2", 9_000_000)])
    started.check_cache_ttl(project, store)
    assert [r["kind"] for r in store.alarms_across()] == \
        [inspection.CACHE_1H_FOREIGN_ALARM]


def test_the_scan_is_not_on_the_daemons_first_tick(started, store, transcripts):
    """THE ONLY CADENCE IN THE DAEMON THAT IS NOT `== 1`, and the reason is measurable: a
    20-second disk walk on tick 1 sits in front of every dispatch the OS is starting, and
    a daemon restarted more often than the six-hour period would pay it on every boot and
    never reach a later tick to do the scan it skipped.

    Asserted on the arithmetic rather than by ticking, because reaching tick 60 takes 60
    ticks. Paired: "not on tick 1" alone is satisfied by a cadence that never fires.
    """
    from jarvis import daemon as daemon_mod

    period, offset = (daemon_mod.CACHE_TTL_EVERY_TICKS,
                      daemon_mod.CACHE_TTL_TICK_OFFSET)
    fires = [t for t in range(1, 2 * period + 1) if t % period == offset]

    assert 1 not in fires
    assert fires == [offset, offset + period], "once per period, and it does fire"


def test_one_tick_of_the_real_loop_does_not_scan(started, store, transcripts):
    """The other half of the offset, through `tick` itself rather than its arithmetic:
    `Daemon.tick` is what a `jarvis start` runs, and tick 1 must not raise one of these
    even with a transcript tree full of offending writes."""
    from jarvis import db

    store.create_work_order("carrier", status="completed")
    _both_halves(transcripts, db.now(), dispatched=9_000_000, foreign=9_000_000)
    started.catalog.projects[0].inspect = InspectConfig()

    started.tick()

    assert started.tick_count == 1
    assert store.alarms_across() == []


# -- 3. the alarm actually reaches the supervisor --------------------------------------


def _raised(started, store, transcripts):
    store.create_work_order("carrier", status="completed")
    rows = _raise(started, store, transcripts, foreign=900_000)
    return rows[0]


def test_the_drain_hands_it_to_the_judge(started, store, transcripts):
    """THE ENGAGEMENT PIN. A row in `wo_alarms` is not the deliverable — the ask is that
    the SUPERVISOR is engaged. `_drain_project_alarms` declines an alarm by marking it
    `skipped`, so that assertion is what discriminates: a settled carrier tripping an
    exclusion would leave the table looking identical."""
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
    assert judged[alarm["id"]][1:] == (inspection.CACHE_1H_FOREIGN_ALARM, "completed")
    assert not [r for r in store.alarms_across() if r["alarm_status"] == "skipped"]


def test_the_evidence_packet_carries_the_remedy_and_no_turn_number(started, store,
                                                                   transcripts):
    """The judge sees the packet and never the alarm row. `raised on turn -1` is nonsense
    a judge would have to interpret, and a packet missing the remedy line cannot produce
    the work order the user asked for."""
    from jarvis import supervisor
    from jarvis.catalog import SupervisorConfig

    alarm = _raised(started, store, transcripts)
    carrier = store.get_work_order(alarm["wo_id"])

    packet = supervisor.build_evidence(
        store, {"kind": "work_order", "row": carrier}, alarm,
        SupervisorConfig(), InspectConfig())

    assert "raised on no particular turn" in packet
    assert "turn -1" not in packet
    assert inspection.CACHE_1H_FOREIGN_ALARM in packet
    assert "FORCE_PROMPT_CACHING_5M" in packet


def test_the_supervisor_is_told_these_two_are_not_this_projects_fault(started):
    """The persona is the only place the judge learns that the carrier means nothing and
    that one of the two kinds is not the OS's to fix. Both prompts, because a judge that
    escalates reads the second one."""
    from jarvis import supervisor

    for prompt_text in (supervisor.SUPERVISOR_PERSONA,
                        supervisor.ALARM_REVIEWER_PERSONA):
        assert inspection.CACHE_1H_FOREIGN_ALARM in prompt_text
        assert inspection.CACHE_1H_DISPATCHED_ALARM in prompt_text
        assert "must never write" in prompt_text


# -- 4. the guards around the two new kinds --------------------------------------------


def test_each_alarm_family_owns_its_own_inbox_titles():
    """WHY THERE ARE TWO DICTS. A dict named for one family is a thing a test reads
    WHOLE — `test_rewrite_tax_alarm.test_the_carrier_is_not_flagged_for_attention`
    asserts its two titles are exactly the two rows a raise produced — so a third entry
    there silently changes what an existing assertion means. It did, and this pins the
    separation rather than the accident."""
    from jarvis import daemon as daemon_mod

    assert set(daemon_mod.CACHE_1H_INBOX_TITLE) == {
        inspection.CACHE_1H_DISPATCHED_ALARM, inspection.CACHE_1H_FOREIGN_ALARM}
    assert not (set(daemon_mod.CACHE_1H_INBOX_TITLE)
                & set(daemon_mod.REWRITE_INBOX_TITLE))


def test_a_probe_cannot_be_named_after_one_of_these_alarms():
    """`probes.RESERVED_IDS` duplicates `inspection.ALARM_KINDS` as literals — probes
    cannot import inspection without a cycle through catalog — so a collision would put
    two different things under one name on `/alarms`."""
    assert set(inspection.ALARM_KINDS) == set(probes.RESERVED_IDS)


def test_the_thresholds_are_settings_and_a_zero_is_refused_where_it_was_typed(tmp_path):
    """A window of zero days reports nothing for ever and a token floor of zero alarms on
    the first write any session makes; both arrive by a typo in `jarvis config set`."""
    def _catalog(**inspect):
        return {"os": {"defaults": {"model": "sonnet"}, "inspect": inspect},
                "projects": [{"name": "p", "path": str(tmp_path)}]}

    cfg = parse_catalog(_catalog(alarm_cache_1h_tokens=1_000)).os.inspect
    assert cfg.alarm_cache_1h_tokens == 1_000
    assert cfg.alarm_cache_1h_window_days == 7, "the others still fall through"

    for key in ("alarm_cache_1h_window_days", "alarm_cache_1h_tokens",
                "alarm_cache_1h_dispatched_tokens"):
        with pytest.raises(CatalogError, match=key):
            parse_catalog(_catalog(**{key: 0}))
