"""Outside-Jarvis spend: the ownership predicate and the transcript walk.

Spec docs/superpowers/specs/2026-10-08-usage-meter-samples-and-outside-spend.md §6.
Tests 15-23 of its §13. No test touches the real `~/.claude` tree: `fleet_fixture`
points `usage.TRANSCRIPT_ROOT_ENV` at a tmp directory.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from jarvis import nav_volume, usage_meter
from jarvis.central_store import CentralStore

T0 = 1_760_000_000.0
SINCE = T0 - 3600
UNTIL = T0 + 3600


def prompt_row(text: str, *, at: float = T0) -> dict:
    return {
        "type": "user",
        "timestamp": (datetime.fromtimestamp(at, tz=timezone.utc)
                      .isoformat().replace("+00:00", "Z")),
        "message": {"content": text},
    }


def walk(fixture, **over):
    kwargs = {"since": SINCE, "until": UNTIL, "home": fixture.home}
    kwargs.update(over)
    return usage_meter.outside_sessions(**kwargs)


def usd(sessions) -> float:
    return sum(s.usd for s in sessions)


def register(path: str, name: str) -> None:
    central = CentralStore()
    try:
        central.upsert_project(name, path, name)
        central.conn.commit()
    finally:
        central.close()


# -- 15-18, ownership -------------------------------------------------------------------


def test_owned_worker_session_excluded_lead_and_subagent(fleet_fixture):
    fleet_fixture.order(session_id="owned-1")
    fleet_fixture.transcript("owned-1", [
        prompt_row("do the thing"),
        fleet_fixture.call_row(at=T0, read=100_000, out=2_000, mid="m-lead"),
    ], subagents=[[fleet_fixture.call_row(at=T0 + 10, read=50_000, out=1_000,
                                          mid="m-sub")]])

    assert walk(fleet_fixture) == []
    assert usd(walk(fleet_fixture)) == 0.0


def test_outside_session_in_the_same_slug_is_included(fleet_fixture):
    slug = nav_volume.slug_of(fleet_fixture.project_path)
    fleet_fixture.order(session_id="owned-1")
    fleet_fixture.transcript("owned-1", [
        fleet_fixture.call_row(at=T0, read=100_000, out=2_000, mid="m-owned")],
        directory=slug)
    fleet_fixture.transcript("loose-1", [
        prompt_row("what is in this repo?"),
        fleet_fixture.call_row(at=T0 + 5, read=200_000, out=3_000, mid="m-loose"),
    ], directory=slug)

    found = walk(fleet_fixture)

    assert [s.session_id for s in found] == ["loose-1"]
    assert found[0].title == "what is in this repo?"
    assert found[0].project == "proj_a", "the owned session's own slug, attributed"
    assert found[0].project_dir == slug
    assert found[0].calls == 1
    assert found[0].usd > 0


def test_prior_sessions_id_is_owned(fleet_fixture):
    fleet_fixture.order(session_id="current-1", prior_sessions=["spent-1"])
    fleet_fixture.transcript("spent-1", [
        fleet_fixture.call_row(at=T0, read=100_000, out=2_000, mid="m-spent")])

    assert walk(fleet_fixture) == []


def test_agent_calls_session_id_is_owned(fleet_fixture):
    fleet_fixture.os_call("neo", ts=T0, cost_usd=0.4, session_id="neo-1")
    fleet_fixture.transcript("neo-1", [
        fleet_fixture.call_row(at=T0, read=100_000, out=2_000, mid="m-neo")])

    assert walk(fleet_fixture) == []


# -- 19-20, attribution -----------------------------------------------------------------


def test_unregistered_directory_reports_no_project(fleet_fixture):
    fleet_fixture.transcript("loose-1", [
        fleet_fixture.call_row(at=T0, read=100_000, out=2_000, mid="m-loose")],
        directory="-home-someone-scratch")

    found = walk(fleet_fixture)

    assert len(found) == 1
    assert found[0].project is None
    assert found[0].project_dir == "-home-someone-scratch"


def test_colliding_slugs_attribute_to_the_longer_match(fleet_fixture):
    register("/ws/jarvis", "jarvis")
    register("/ws/jarvis_os", "jarvis_os")
    directory = nav_volume.slug_of("/ws/jarvis_os")
    fleet_fixture.transcript("loose-1", [
        fleet_fixture.call_row(at=T0, read=100_000, out=2_000, mid="m-loose")],
        directory=directory)

    found = walk(fleet_fixture)

    assert [s.project for s in found] == ["jarvis_os"]
    # A bare prefix test cannot split the pair — the wrong answer, pinned.
    assert directory.startswith(nav_volume.slug_of("/ws/jarvis"))


# -- 21-23, the walk --------------------------------------------------------------------


def test_span_cuts_through_one_file(fleet_fixture):
    fleet_fixture.transcript("loose-1", [
        fleet_fixture.call_row(at=SINCE - 600, read=400_000, out=9_000, mid="m-before"),
        fleet_fixture.call_row(at=T0, read=100_000, out=2_000, mid="m-inside"),
        fleet_fixture.call_row(at=UNTIL + 600, read=400_000, out=9_000, mid="m-after"),
    ])

    inside = walk(fleet_fixture)
    only = usd([s for s in walk(fleet_fixture, since=T0 - 1, until=T0 + 1)])

    assert inside[0].calls == 1
    assert inside[0].first_ts == inside[0].last_ts == T0
    assert inside[0].usd == only, "the two outside calls contributed nothing"


def test_repeated_usage_lines_count_once(fleet_fixture):
    rewritten = [fleet_fixture.call_row(at=T0, read=100_000, out=out, mid="m-grow")
                 for out in (1, 900, 2_000)]
    fleet_fixture.transcript("loose-1", rewritten + [
        fleet_fixture.call_row(at=T0 + 30, read=100_000, out=2_000, mid="m-retry")])

    found = walk(fleet_fixture)[0]

    assert found.calls == 2, "one rewritten message plus one genuine retry"
    assert found.tokens["cache_read"] == 200_000
    assert found.tokens["output"] == 4_000, "the MAX of the rewritten copies, once"


def test_mtime_prefilter_skips_a_stale_file(fleet_fixture):
    fleet_fixture.transcript("fresh-1", [
        fleet_fixture.call_row(at=T0, read=100_000, out=2_000, mid="m-fresh")])
    fleet_fixture.transcript("stale-1", [
        fleet_fixture.call_row(at=T0, read=500_000, out=9_000, mid="m-stale")])
    stale = fleet_fixture.transcript_root / "-proj" / "stale-1.jsonl"
    os.utime(stale, (SINCE - 86_400, SINCE - 86_400))

    found = walk(fleet_fixture)

    assert [s.session_id for s in found] == ["fresh-1"]
    assert json.loads(next(iter(
        stale.read_text().splitlines())))["message"]["usage"][
            "cache_read_input_tokens"] == 500_000, "its calls WERE inside the span"


def test_subagent_of_an_outside_lead_is_counted_once(fleet_fixture):
    fleet_fixture.transcript("loose-1", [
        fleet_fixture.call_row(at=T0, read=100_000, out=2_000, mid="m-lead")],
        subagents=[[fleet_fixture.call_row(at=T0 + 10, read=50_000, out=1_000,
                                           mid="m-sub")]])

    found = walk(fleet_fixture)

    assert len(found) == 1, "the subagent is not a session of its own"
    assert found[0].calls == 2
    assert found[0].tokens["cache_read"] == 150_000


def test_the_walk_writes_no_rows(fleet_fixture):
    fleet_fixture.order(session_id="owned-1")
    fleet_fixture.transcript("loose-1", [
        fleet_fixture.call_row(at=T0, read=1_000, out=10, mid="m-loose")])
    central = CentralStore()
    try:
        counts = [central.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                  for table in ("agent_calls", "usage_samples", "inbox")]
    finally:
        central.close()

    walk(fleet_fixture)

    central = CentralStore()
    try:
        after = [central.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                 for table in ("agent_calls", "usage_samples", "inbox")]
    finally:
        central.close()
    assert after == counts


def test_visible_usd_includes_outside_spend(fleet_fixture):
    wo = fleet_fixture.order(session_id="owned-1")
    fleet_fixture.turn(wo, started_at=T0, ended_at=T0 + 60, cost_usd=1.0)
    fleet_fixture.transcript("loose-1", [
        fleet_fixture.call_row(at=T0 + 5, read=1_000_000, out=20_000, mid="m-loose")])

    total = usage_meter._visible_usd(since=SINCE, until=UNTIL,
                                    home=Path(fleet_fixture.home))

    assert total > 1.0
    assert total == 1.0 + usd(walk(fleet_fixture))
