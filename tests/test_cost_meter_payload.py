"""The `meter` subtree, the one sentence, and the surfaces that render it (docs/superpowers/specs/2026-10-08-usage-meter-samples-and-outside-spend.md)"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jarvis import cli, db, fleetcost, ops, usage_meter
from jarvis.catalog import CostConfig, load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon, METER_RECONCILE_EVERY_TICKS
from jarvis.project_store import ProjectStore

MINUTE = 60.0
HOUR = 3600.0


def stamp(text: str) -> float:
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


#: Inside the acceptance week (Mon 2026-09-28 21:00 PDT -> the next Monday)
T0 = stamp("2026-09-30T12:00:00+00:00")
SPAN_SINCE = stamp("2026-09-30T10:00:00+00:00")
SPAN_UNTIL = stamp("2026-09-30T15:00:00+00:00")


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def prompt_row(text: str, *, at: float) -> dict:
    return {"type": "user", "timestamp": iso(at), "message": {"content": text}}


def store_samples(rows) -> None:
    central = CentralStore()
    try:
        for row in rows:
            usage_meter.record(central, row)
        central.conn.commit()
    finally:
        central.close()


def series(start: float, *, n: int, first: float, last: float, resets: float):
    step = (last - first) / (n - 1) if n > 1 else 0.0
    return [usage_meter.Sample(ts=start + i * MINUTE, ok=True,
                               five_hour_pct=first + i * step,
                               five_hour_resets_at=resets,
                               seven_day_pct=5.0 + i * step / 10,
                               seven_day_resets_at=resets + 86_400)
            for i in range(n)]


def calibrate(f, start: float) -> None:
    """Three clean 40-minute spans of 10 points each, $8.20 apiece"""
    for offset in (-8 * HOUR, -6 * HOUR, -4 * HOUR):
        at = start + offset
        store_samples(series(at, n=40, first=0.0, last=10.0, resets=start + 9 * HOUR))
        wo = f.order("a calibration order")
        f.turn(wo, started_at=at + MINUTE, ended_at=at + 2 * MINUTE, cost_usd=8.2)


def scene(f, *, since: float = SPAN_SINCE, until: float = SPAN_SINCE + HOUR,
          samples: int | None = None, points: float = 12.0,
          worker_usd: float = 6.0, os_usd: float = 0.5,
          outside: bool = True) -> dict:
    """The worked example, as records"""
    calibrate(f, since)
    n = samples if samples is not None else int((until - since) / MINUTE)
    store_samples(series(since, n=n, first=10.0, last=10.0 + points, resets=until))
    wo = f.order("the order that spent in this span")
    f.turn(wo, started_at=since + 2 * MINUTE, ended_at=since + 3 * MINUTE,
           cost_usd=worker_usd)
    if os_usd:
        f.os_call("neo_answer", ts=since + 3 * MINUTE, wo_id=wo, cost_usd=os_usd)
    if outside:
        f.transcript("1a9cd19d-0000-4000-8000-000000000001", [
            prompt_row("look at the cost page", at=since + 4 * MINUTE),
            # $2.00 of cache write + $0.48 of output at Opus list = $2.48
            f.call_row(at=since + 5 * MINUTE, write=320_000, out=19_200, mid="m-out"),
        ], directory="-home-someone-elsewhere")
    return {"since": since, "until": until, "source": "session-meter",
            "label": "the span", "window": "5h", "offset": 0, "zone": "UTC"}


def meter_of(f, resolved, **over) -> dict:
    kwargs = {"resolved": resolved, "home": f.home, "now": resolved["until"]}
    kwargs.update(over)
    return ops.cost_meter(**kwargs)["meter"]


# -- 29. the subtree


def test_the_subtree_keys_and_version_are_pinned(fleet_fixture):
    """The contract test the rest of the cost payload already has (§9)"""
    meter = meter_of(fleet_fixture, scene(fleet_fixture))

    assert meter["version"] == 1
    assert set(meter) == {"version", "window", "five_hour", "seven_day", "coverage",
                          "dollars_per_point", "spend", "outside", "timeline",
                          "sentence", "alerts", "reliable", "unreliable"}
    assert set(meter["window"]) == {"since", "until", "source"}
    assert set(meter["five_hour"]) == {"start_pct", "end_pct", "delta_points",
                                       "reset_count", "max_resets", "anomalies",
                                       "reliable", "unreliable", "segments"}
    assert set(meter["seven_day"]) == set(meter["five_hour"])
    assert set(meter["five_hour"]["segments"][0]) == {"since", "until", "start_pct",
                                                      "end_pct", "delta", "cause"}
    assert set(meter["coverage"]) == {"samples", "expected", "share", "gap_rows",
                                      "longest_gap_seconds", "head_uncovered_seconds",
                                      "first_ts", "last_ts", "reliable",
                                      "min_reliable_share"}
    assert set(meter["dollars_per_point"]) == {"value", "low", "high", "source",
                                               "basis_spans", "basis_points",
                                               "basis_usd"}
    assert set(meter["spend"]) == {"implied_usd", "workers_usd", "jarvis_calls_usd",
                                   "jarvis_usd", "outside_usd", "residual_usd",
                                   "residual_share", "outside_share", "jarvis_share",
                                   "scope_project", "scope_workers_usd",
                                   "scope_jarvis_calls_usd", "scope_jarvis_usd"}
    assert set(meter["outside"]) == {"total_usd", "n", "sessions"}
    assert set(meter["alerts"]) == {"outside", "residual", "unreliable", "suppressed",
                                    "thresholds", "min_usd"}
    assert set(meter["timeline"][0]) == {"ts", "five_hour_pct", "seven_day_pct", "ok"}


def test_the_outside_rows_are_capped_in_rows_and_never_in_dollars(fleet_fixture):
    resolved = scene(fleet_fixture, outside=False)
    for i in range(4):
        fleet_fixture.transcript(f"outsider-{i}", [
            prompt_row("a hand-opened session", at=resolved["since"] + 4 * MINUTE),
            fleet_fixture.call_row(at=resolved["since"] + 5 * MINUTE, write=10_000 * (i + 1),
                                   out=100, mid=f"m-{i}"),
        ], directory=f"-elsewhere-{i}")
    fleet_fixture.set_cost(meter_outside_rows=2)

    meter = meter_of(fleet_fixture, resolved)

    assert meter["outside"]["n"] == 4
    assert len(meter["outside"]["sessions"]) == 2
    rows_usd = sum(s["usd"] for s in meter["outside"]["sessions"])
    assert meter["outside"]["total_usd"] > rows_usd, "the cap is on rows, not dollars"
    assert meter["spend"]["outside_usd"] == pytest.approx(meter["outside"]["total_usd"])


def test_the_timeline_is_decimated_to_three_hundred_points(fleet_fixture):
    resolved = scene(fleet_fixture, until=SPAN_UNTIL)       # 5h = 300 minutes...
    store_samples(series(SPAN_SINCE, n=600, first=10.0, last=22.0, resets=SPAN_UNTIL))

    meter = meter_of(fleet_fixture, resolved)

    assert len(meter["timeline"]) <= 300
    assert meter["timeline"][0]["ts"] >= resolved["since"]


# -- 30-32. the sentence


def test_the_sentence_matches_the_shape_in_the_spec(fleet_fixture):
    meter = meter_of(fleet_fixture, scene(fleet_fixture))
    said = meter["sentence"]
    spend = meter["spend"]

    assert meter["coverage"]["share"] == pytest.approx(1.0)
    assert meter["dollars_per_point"]["source"] == "measured"
    assert "the 5h meter rose 12 points (~$9.8)" in said
    assert f"Jarvis spent ${spend['jarvis_usd']:.2f} (66%)" in said
    assert f"other sessions on this machine ${spend['outside_usd']:.2f} (25%" in said
    assert f"unexplained ${spend['residual_usd']:.2f} (9%)" in said
    # The top session, named so the user can go and stop it
    assert "session 1a9cd19d" in said and "look at the cost page" in said
    # BOTH halves of the label, every time and in that order (§7)
    assert said.index("usage this machine cannot see") < \
        said.index("accounting error in Jarvis")
    shares = [spend["jarvis_share"], spend["outside_share"], spend["residual_share"]]
    assert sum(round(100 * s) for s in shares) == pytest.approx(100, abs=1)


def test_a_span_with_no_samples_says_so_and_derives_nothing(fleet_fixture):
    resolved = {"since": SPAN_SINCE, "until": SPAN_SINCE + HOUR, "source": "session-window"}

    meter = meter_of(fleet_fixture, resolved)

    assert meter["sentence"] == "no usage-meter samples cover this span"
    for key in ("implied_usd", "residual_usd", "residual_share", "outside_share",
                "jarvis_share"):
        assert meter["spend"][key] is None, key
    assert "%" not in meter["sentence"] and "$" not in meter["sentence"]
    assert meter["alerts"]["outside"] is False and meter["alerts"]["residual"] is False


def test_a_negative_residual_is_reported_negative_and_never_clamped(fleet_fixture):
    """The meter moved less than the measured spend implies: evidence about the estimator"""
    meter = meter_of(fleet_fixture, scene(fleet_fixture, worker_usd=50.0))

    assert meter["spend"]["residual_usd"] < 0
    assert meter["spend"]["residual_share"] < 0
    assert "unexplained -$" in meter["sentence"]
    assert "usage this machine cannot see" in meter["sentence"]


def test_a_scoped_query_keeps_the_residual_fleet_wide(fleet_fixture):
    """Another project's spend is EXPLAINED, never unexplained (§7)"""
    from jarvis.testing import FleetCostFixture, make_git_project

    second = make_git_project(fleet_fixture.catalog_path.parent, "proj_b")
    data = json.loads(fleet_fixture.catalog_path.read_text())
    data["projects"].append({"name": "proj_b", "path": str(second)})
    fleet_fixture.catalog_path.write_text(json.dumps(data))
    central = CentralStore()
    try:
        central.upsert_project("proj_b", str(second), "the other project")
        central.conn.commit()
    finally:
        central.close()
    other = FleetCostFixture(fleet_fixture.home, second, fleet_fixture.transcript_root,
                             fleet_fixture.catalog_path, name="proj_b")
    resolved = scene(fleet_fixture, outside=False)
    wo_b = other.order("the order proj_b spent on")
    other.turn(wo_b, started_at=resolved["since"] + 2 * MINUTE,
               ended_at=resolved["since"] + 3 * MINUTE, cost_usd=3.0)

    meter = meter_of(fleet_fixture, resolved, project="proj_a")
    spend = meter["spend"]
    implied = float(spend["implied_usd"])

    # proj_b's $3.00 is counted, and the residual is the fleet-wide one
    assert spend["jarvis_usd"] == pytest.approx(9.5)
    assert spend["workers_usd"] == pytest.approx(9.0)
    assert spend["residual_usd"] == pytest.approx(round(implied - 9.5, 2))
    assert spend["residual_usd"] != pytest.approx(round(implied - 6.5, 2))
    # The scoped figures sit beside it, never subtracted from the meter
    assert spend["scope_project"] == "proj_a"
    assert spend["scope_jarvis_usd"] == pytest.approx(6.5)
    assert spend["scope_workers_usd"] == pytest.approx(6.0)
    assert spend["scope_jarvis_calls_usd"] == pytest.approx(0.5)
    assert "proj_a's own share of that is $6.50" in meter["sentence"]


def test_an_unscoped_query_carries_the_scope_keys_as_none(fleet_fixture):
    meter = meter_of(fleet_fixture, scene(fleet_fixture))

    for key in ("scope_project", "scope_workers_usd", "scope_jarvis_calls_usd",
                "scope_jarvis_usd"):
        assert meter["spend"][key] is None, key
    assert "own share of that" not in meter["sentence"]


# -- the honesty gate


def test_a_span_the_meter_barely_covers_derives_no_dollar_figure(fleet_fixture):
    """3% covered: the rise is not a bound in either direction, so nothing is derived"""
    meter = meter_of(fleet_fixture, scene(fleet_fixture, until=SPAN_UNTIL, samples=9))
    said = meter["sentence"]
    spend = meter["spend"]

    assert meter["coverage"]["share"] == pytest.approx(0.03)
    assert meter["reliable"] is False
    assert "low_coverage" in meter["unreliable"]
    assert usage_meter.UNRELIABLE in said
    assert "3% covered" in said
    for forbidden in ("lower bound", usage_meter.RESIDUAL_LABEL, "unexplained"):
        assert forbidden not in said, forbidden
    # The measured halves are measured, not derived, and stay numbers
    assert f"${spend['jarvis_usd']:.2f}" in said
    assert f"${spend['outside_usd']:.2f}" in said
    for key in ("implied_usd", "residual_usd", "residual_share", "outside_share",
                "jarvis_share"):
        assert spend[key] is None, key


def test_between_the_thresholds_the_figure_stands_and_the_coverage_is_incomplete(
        fleet_fixture):
    meter = meter_of(fleet_fixture, scene(fleet_fixture, until=SPAN_UNTIL, samples=210))
    said = meter["sentence"]

    assert meter["coverage"]["share"] == pytest.approx(0.7)
    assert meter["reliable"] is True and meter["unreliable"] == []
    assert "the 5h meter rose 12 points (~$9.8)" in said
    assert "70% of this span, so the rise is incomplete" in said
    assert "lower bound" not in said


def test_an_unreliable_span_suppresses_both_alarms_and_the_daemon_writes_nothing(
        fleet_fixture, fixed_now):
    fleet_fixture.order("the carrier", status="completed")
    scene(fleet_fixture, until=SPAN_UNTIL, samples=120)        # 40% covered
    meter = meter_of(fleet_fixture, {"since": SPAN_SINCE, "until": SPAN_UNTIL,
                                     "source": "session-meter"})

    assert meter["reliable"] is False
    assert meter["alerts"]["outside"] is False
    assert meter["alerts"]["residual"] is False
    assert meter["alerts"]["unreliable"] is True
    # The raw rise clears the floor, so this span WOULD have alarmed had the arithmetic been sound
    assert meter["alerts"]["suppressed"] is True
    assert meter["spend"]["implied_usd"] is None, "and no dollar figure in the payload"

    reconcile(fleet_fixture, daemon_for(fleet_fixture))

    assert alarms(fleet_fixture) == []
    central = CentralStore()
    try:
        assert central.unacked_inbox("warning") == []
    finally:
        central.close()
    store = ProjectStore(fleet_fixture.project_path)
    try:
        carrier = store.latest_settled_order()
        assert store.events_of_kind(carrier["id"], "cost_alarm") == []
    finally:
        store.close()


def test_a_gap_longer_than_the_window_derives_nothing(fleet_fixture):
    """A whole 5h window could have opened and closed inside the hole"""
    since, until = SPAN_SINCE, SPAN_SINCE + 24 * HOUR
    calibrate(fleet_fixture, since)
    store_samples(series(since, n=540, first=10.0, last=19.0, resets=until))
    store_samples(series(since + 15 * HOUR, n=540, first=19.0, last=28.0, resets=until))

    meter = meter_of(fleet_fixture, {"since": since, "until": until,
                                     "source": "session-meter"})

    assert meter["coverage"]["share"] == pytest.approx(0.75)
    assert "gap_longer_than_window" in meter["unreliable"]
    assert "low_coverage" not in meter["unreliable"]
    assert meter["spend"]["implied_usd"] is None
    assert usage_meter.UNRELIABLE in meter["sentence"]
    assert "unexplained" not in meter["sentence"]


# -- 33-35. the CLI


def _windowed_orders(f):
    now = db.now()
    recent = f.order("an order inside the 5h window")
    f.turn(recent, started_at=now - 120, ended_at=now - 60, cost_usd=1.0)
    old = f.order("an order from last week")
    f.turn(old, started_at=now - 10 * 86_400, ended_at=now - 10 * 86_400 + 60,
           cost_usd=1.0)
    return recent, old


def test_jarvis_cost_window_5h_now_windows_the_listing(fleet_fixture, capsys):
    """The CLI half of the window selector's defect 3: `--window` without `--fleet` silently windowed..."""
    recent, old = _windowed_orders(fleet_fixture)
    span = datetime.fromtimestamp(db.now() - 30 * 86_400, tz=timezone.utc).isoformat()

    assert cli.main(["cost", "--since", span]) == 0
    both = capsys.readouterr().out
    assert recent in both and old in both, "a span over both holds both"

    assert cli.main(["cost", "--window", "5h"]) == 0
    windowed = capsys.readouterr().out
    assert recent in windowed
    assert old not in windowed


def test_the_json_payloads_carry_the_window_and_one_meter_subtree(fleet_fixture, capsys):
    _windowed_orders(fleet_fixture)

    assert cli.main(["cost", "--json", "--window", "5h"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert cli.main(["cost", "--fleet", "--json", "--window", "5h"]) == 0
    fleet = json.loads(capsys.readouterr().out)

    assert listing["window"]["window"] == "5h"
    assert listing["meter"]["version"] == 1
    # The SAME subtree from the SAME resolved window — one population, two payloads
    assert listing["meter"] == fleet["meter"]
    assert fleet["fleet"]["window"]["since"] == listing["window"]["since"]


def test_one_orders_bill_carries_no_meter_block(fleet_fixture, capsys):
    """A bill's span is the ORDER's life"""
    wo = fleet_fixture.order("one order")
    fleet_fixture.turn(wo, started_at=db.now() - 60, ended_at=db.now(), cost_usd=1.0)

    assert cli.main(["cost", "--json", wo]) == 0
    bill = json.loads(capsys.readouterr().out)
    assert "meter" not in bill

    assert cli.main(["cost", wo]) == 0
    assert "the 5h meter" not in capsys.readouterr().out


# -- 37. the anchor


def test_the_5h_window_re_anchors_on_the_meter_and_falls_back_to_the_guess(fleet_fixture):
    cfg = CostConfig()
    guess = fleetcost.session_window(T0, cfg)

    fallback = fleetcost.resolve_window(window="5h", cfg=cfg, now=T0)
    assert (fallback["since"], fallback["until"]) == guess
    assert fallback["source"] == "session-window"

    # The measured discrepancy of 2026-10-08
    real_reset = guess[1] + 80 * MINUTE
    store_samples([usage_meter.Sample(ts=T0 - 30, ok=True, five_hour_pct=44.0,
                                      five_hour_resets_at=real_reset)])

    anchored = fleetcost.resolve_window(window="5h", cfg=cfg, now=T0)
    assert anchored["source"] == "session-meter"
    assert anchored["until"] == real_reset
    assert anchored["since"] == real_reset - 5 * HOUR
    assert (anchored["since"], anchored["until"]) != guess

    # `offset` steps back in whole 5h multiples FROM the real boundary
    back = fleetcost.resolve_window(window="5h", offset=-2, cfg=cfg, now=T0)
    assert back["until"] == real_reset - 10 * HOUR

    # The note is for the fallback ONLY: it describes a guess, and this is not one
    assert fleetcost.SESSION_ANCHOR_NOTE not in \
        fleetcost.report(resolved=anchored)["fleet"]["notes"]
    assert fleetcost.SESSION_ANCHOR_NOTE in \
        fleetcost.report(resolved=fallback)["fleet"]["notes"]


def test_a_stale_sample_does_not_anchor_anything(fleet_fixture):
    """Older than `meter_nearest_seconds` is not a reading of THIS window"""
    cfg = CostConfig()
    store_samples([usage_meter.Sample(ts=T0 - 4 * cfg.meter_nearest_seconds, ok=True,
                                      five_hour_pct=44.0,
                                      five_hour_resets_at=T0 + 1_000)])

    found = fleetcost.resolve_window(window="5h", cfg=cfg, now=T0)
    assert found["source"] == "session-window"


# -- 43. the daemon


@pytest.fixture()
def fixed_now(monkeypatch):
    monkeypatch.setattr(db, "now", lambda: T0)
    return T0


def daemon_for(f) -> Daemon:
    return Daemon(load_catalog(f.catalog_path))


def alarms(f):
    store = ProjectStore(f.project_path)
    try:
        return store.alarms_across()
    finally:
        store.close()


def reconcile(f, daemon, *, name: str = "proj_a"):
    project = next(p for p in daemon.catalog.projects if p.name == name)
    store = ProjectStore(project.path)
    try:
        daemon.check_meter_residual(project, store)
    finally:
        store.close()


def test_the_outside_alarm_fires_once_per_window_for_the_os_owner(
        fleet_fixture, fixed_now):
    assert METER_RECONCILE_EVERY_TICKS == 360
    fleet_fixture.order("the carrier", status="completed")
    scene(fleet_fixture, until=SPAN_UNTIL, samples=300)
    daemon = daemon_for(fleet_fixture)

    reconcile(fleet_fixture, daemon)
    raised = alarms(fleet_fixture)

    assert [r["kind"] for r in raised] == ["cost_outside_spend_high"]
    assert "1a9cd19d" in raised[0]["reason"]
    assert "look at the cost page" in raised[0]["reason"]
    carrier = ProjectStore(fleet_fixture.project_path)
    try:
        assert raised[0]["wo_id"] == carrier.latest_settled_order()["id"]
    finally:
        carrier.close()
    central = CentralStore()
    try:
        told = central.unacked_inbox("warning")
        assert len(told) == 1 and told[0]["wo_id"] == raised[0]["wo_id"]
    finally:
        central.close()

    reconcile(fleet_fixture, daemon)
    assert len(alarms(fleet_fixture)) == 1, "one alarm per kind per window"


def test_the_alarm_is_silent_below_the_floor_and_on_the_seed(fleet_fixture, fixed_now):
    fleet_fixture.order("the carrier", status="completed")
    scene(fleet_fixture, until=SPAN_UNTIL, samples=300)
    fleet_fixture.set_cost(meter_alert_min_usd=50.0)

    reconcile(fleet_fixture, daemon_for(fleet_fixture))
    assert alarms(fleet_fixture) == [], "a quarter of a small window is small"


def test_an_alarm_computed_from_the_shipped_constant_is_about_the_constant(
        fleet_fixture, fixed_now):
    """No calibration span: `dollars_per_point.source == 'seed'` and nothing fires"""
    fleet_fixture.order("the carrier", status="completed")
    store_samples(series(SPAN_SINCE, n=120, first=10.0, last=22.0, resets=SPAN_UNTIL))
    fleet_fixture.transcript("1a9cd19d-0000-4000-8000-000000000001", [
        prompt_row("look at the cost page", at=SPAN_SINCE + 4 * MINUTE),
        fleet_fixture.call_row(at=SPAN_SINCE + 5 * MINUTE, write=320_000, out=19_200,
                               mid="m-out"),
    ], directory="-home-someone-elsewhere")

    reconcile(fleet_fixture, daemon_for(fleet_fixture))
    assert alarms(fleet_fixture) == []


def test_a_second_project_in_the_same_catalog_raises_nothing(fleet_fixture, fixed_now):
    """A FLEET reading carried by one project: N projects raising it is N-1 copies"""
    from jarvis.testing import make_git_project

    second = make_git_project(fleet_fixture.catalog_path.parent, "proj_b")
    data = json.loads(fleet_fixture.catalog_path.read_text())
    data["projects"].append({"name": "proj_b", "path": str(second)})
    fleet_fixture.catalog_path.write_text(json.dumps(data))
    fleet_fixture.order("the carrier", status="completed")
    scene(fleet_fixture, until=SPAN_UNTIL, samples=300)
    daemon = daemon_for(fleet_fixture)

    store = ProjectStore(second)
    try:
        store.create_work_order("a settled order over here", "", status="completed")
        reconcile(fleet_fixture, daemon, name="proj_b")
        assert store.alarms_across() == []
    finally:
        store.close()


# -- 44. a read writes nothing


def _counts(f) -> dict[str, int]:
    found = {}
    central = CentralStore()
    try:
        for table in ("inbox", "usage_samples", "agent_calls"):
            found[table] = central.conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    finally:
        central.close()
    store = ProjectStore(f.project_path)
    try:
        for table in ("wo_alarms", "wo_events"):
            found[table] = store.conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    finally:
        store.close()
    return found


def test_jarvis_cost_writes_nothing(fleet_fixture, capsys):
    scene(fleet_fixture)
    before = _counts(fleet_fixture)

    assert cli.main(["cost"]) == 0
    assert cli.main(["cost", "--window", "5h", "--json"]) == 0
    capsys.readouterr()
    ops.cost_meter(resolved=scene_window(), home=fleet_fixture.home, now=T0)

    assert _counts(fleet_fixture) == before


def scene_window() -> dict:
    return {"since": SPAN_SINCE, "until": SPAN_SINCE + HOUR, "source": "session-meter"}
