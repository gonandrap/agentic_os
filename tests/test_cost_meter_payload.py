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
    return [usage_meter.Sample(ts=start + i * usage_meter.SAMPLE_SECONDS, ok=True,
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
        f.spend("a calibration order", 8.2, at=at + MINUTE)


def scene(f, *, since: float = SPAN_SINCE, until: float = SPAN_SINCE + HOUR,
          samples: int | None = None, points: float = 12.0,
          worker_usd: float = 6.0, os_usd: float = 0.5,
          outside: bool = True) -> dict:
    """The worked example, as records"""
    calibrate(f, since)
    n = samples if samples is not None else int((until - since)
                                                / usage_meter.SAMPLE_SECONDS)
    store_samples(series(since, n=n, first=10.0, last=10.0 + points, resets=until))
    wo = f.spend("the order that spent in this span", worker_usd, at=since + 2 * MINUTE)
    if os_usd:
        f.os_call("neo_answer", ts=since + 3 * MINUTE, wo_id=wo, cost_usd=os_usd,
                  output=f.output_for(os_usd))
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
    assert set(meter["coverage"]) == {"samples", "share", "gap_rows",
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
    assert set(meter["timeline"][0]) == {"ts", "five_hour_pct", "seven_day_pct", "ok",
                                         "five_hour_calc_jarvis", "five_hour_calc_outside",
                                         "seven_day_calc_jarvis", "seven_day_calc_outside"}


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


def test_the_calculated_series_is_the_windows_spend_in_points_split_by_owner(fleet_fixture):
    """Each point: $ spent since its window opened, over $/point; Jarvis and outside apart"""
    resolved = scene(fleet_fixture)
    meter = meter_of(fleet_fixture, resolved)
    rate = meter["dollars_per_point"]["value"]
    assert meter["dollars_per_point"]["source"] == "measured"
    last = meter["timeline"][-1]

    # The 5h window opened at until - 5h: one $8.20 calibration turn, the $6 turn, the $0.50 call
    assert last["five_hour_calc_jarvis"] == pytest.approx((8.2 + 6.0 + 0.5) / rate, rel=1e-3)
    assert last["five_hour_calc_outside"] == pytest.approx(2.48 / rate, rel=1e-3)
    first = meter["timeline"][0]
    assert first["five_hour_calc_jarvis"] == pytest.approx(8.2 / rate, rel=1e-3)
    assert first["five_hour_calc_outside"] == 0.0
    # 7d never calibrated on its own, so it is the 5h rate scaled; a 7d point costs more
    assert 0 < last["seven_day_calc_jarvis"] < last["five_hour_calc_jarvis"]
    assert last["seven_day_calc_outside"] is not None


def test_the_zoom_endpoint_carries_the_calculated_series(fleet_fixture):
    resolved = scene(fleet_fixture)

    tl = ops.cost_meter_timeline(resolved["since"], resolved["until"])

    assert tl[-1]["five_hour_calc_jarvis"] > tl[0]["five_hour_calc_jarvis"] > 0
    assert tl[-1]["five_hour_calc_outside"] > 0


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
    other.spend("the order proj_b spent on", 3.0, at=resolved["since"] + 2 * MINUTE)

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
    meter = meter_of(fleet_fixture, scene(fleet_fixture, until=SPAN_UNTIL, samples=4))
    said = meter["sentence"]
    spend = meter["spend"]

    assert meter["coverage"]["share"] == pytest.approx(3 * 150 / 18_000, abs=1e-4)
    assert meter["reliable"] is False
    assert "low_coverage" in meter["unreliable"]
    assert usage_meter.UNRELIABLE in said
    assert "2% covered" in said
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
    meter = meter_of(fleet_fixture, scene(fleet_fixture, until=SPAN_UNTIL, samples=84))
    said = meter["sentence"]

    assert meter["coverage"]["share"] == pytest.approx(83 * 150 / 18_000, abs=1e-4)
    assert meter["reliable"] is True and meter["unreliable"] == []
    assert "the 5h meter rose 12 points (~$9.8)" in said
    assert "69% of this span, so the rise is incomplete" in said
    assert "lower bound" not in said


def test_an_unreliable_span_suppresses_both_alarms_and_the_daemon_writes_nothing(
        fleet_fixture, fixed_now):
    fleet_fixture.order("the carrier", status="completed")
    # `fixed_now` is 2h in: 20 readings measure 47m30s of them, 40% covered, the rest a real hole
    scene(fleet_fixture, until=SPAN_UNTIL, samples=20)
    meter = meter_of(fleet_fixture, {"since": SPAN_SINCE, "until": SPAN_UNTIL,
                                     "source": "session-meter"}, now=fixed_now)

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


def test_a_window_ending_ahead_of_now_reconciles_like_the_same_span_ending_now(fleet_fixture):
    """io-82bcb830: the current 5h window ends in the future by design"""
    resolved = scene(fleet_fixture)
    now = resolved["until"]

    ended = meter_of(fleet_fixture, resolved)
    ahead = meter_of(fleet_fixture, {**resolved, "until": SPAN_SINCE + 5 * HOUR}, now=now)

    assert ahead["reliable"] is True and ahead["unreliable"] == []
    assert ahead["coverage"]["share"] == pytest.approx(1.0)
    assert ahead["spend"]["implied_usd"] == ended["spend"]["implied_usd"]
    assert "incomplete" not in ahead["sentence"]


def test_a_window_that_just_opened_raises_no_alarm(fleet_fixture):
    """Coverage no longer holds the current window back; the dollar floor still must"""
    resolved = scene(fleet_fixture, until=SPAN_SINCE + 15 * MINUTE, samples=6, points=1.0,
                     worker_usd=0.0, os_usd=0.0, outside=False)

    meter = meter_of(fleet_fixture, {**resolved, "until": SPAN_SINCE + 5 * HOUR},
                     now=resolved["until"])

    assert meter["reliable"] is True
    assert meter["alerts"]["outside"] is False and meter["alerts"]["residual"] is False


def test_a_gap_longer_than_the_window_derives_nothing(fleet_fixture):
    """A whole 5h window could have opened and closed inside the hole"""
    since, until = SPAN_SINCE, SPAN_SINCE + 24 * HOUR
    calibrate(fleet_fixture, since)
    store_samples(series(since, n=216, first=10.0, last=19.0, resets=until))
    store_samples(series(since + 15 * HOUR, n=216, first=19.0, last=28.0, resets=until))

    meter = meter_of(fleet_fixture, {"since": since, "until": until,
                                     "source": "session-meter"})

    assert meter["coverage"]["share"] == pytest.approx(0.75, abs=0.005)
    assert "gap_longer_than_window" in meter["unreliable"]
    assert "low_coverage" not in meter["unreliable"]
    assert meter["spend"]["implied_usd"] is None
    assert usage_meter.UNRELIABLE in meter["sentence"]
    assert "unexplained" not in meter["sentence"]


# -- 33-35. the CLI


def _windowed_orders(f):
    now = db.now()
    recent = f.order("an order inside the 5h window")
    #: `now - 120` sits in the PREVIOUS 5h block for 2 minutes after every boundary
    since, _ = fleetcost.session_window(now, CostConfig())
    f.turn(recent, started_at=max(now - 120, since), ended_at=now, cost_usd=1.0)
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
    scene(fleet_fixture, until=SPAN_UNTIL, samples=120)
    daemon = daemon_for(fleet_fixture)

    reconcile(fleet_fixture, daemon)
    raised = alarms(fleet_fixture)

    assert [r["kind"] for r in raised] == ["cost_outside_spend_high"]
    assert "1a9cd19d" in raised[0]["reason"]
    assert "look at the cost page" in raised[0]["reason"]
    assert raised[0]["wo_id"] is None, "no order settled yet, and none would carry it"
    central = CentralStore()
    try:
        (told,) = central.unacked_inbox("warning")
        assert told["wo_id"] is None
        assert "Your action:" in told["body"]
    finally:
        central.close()

    reconcile(fleet_fixture, daemon)
    assert len(alarms(fleet_fixture)) == 1, "one alarm per kind per window"


def seed_occurrences(f, kind: str, ages_days: list[float]) -> None:
    """Rows of `kind` already on the record, this many days old, with the review's verdict"""
    store = ProjectStore(f.project_path)
    try:
        for age in ages_days:
            row = store.add_finding(None, kind=kind, reason=f"seen {age}d ago")
            store.update_alarm(row["id"], status="acked", verdict="escalate",
                               verdict_reason=f"cannot be explained {age}",
                               ts=T0 - age * 86_400)
    finally:
        store.close()


def filings(monkeypatch) -> list[dict]:
    from jarvis import bugreport

    filed: list[dict] = []

    def report(**kwargs):
        filed.append(kwargs)
        return {"url": "https://example.test/issues/1"}

    monkeypatch.setattr(bugreport, "report_bug", report)
    return filed


def outside_scene(f) -> None:
    f.order("the carrier", status="completed")
    scene(f, until=SPAN_UNTIL, samples=120)


def residual_scene(f, monkeypatch) -> Daemon:
    """A residual that went negative, as wo-c9337339's did"""
    f.order("the carrier", status="completed")
    meter = {"alerts": {"outside": False, "residual": True},
             "spend": {"residual_usd": -80.09, "implied_usd": 133.48,
                       "residual_share": -0.6}}
    monkeypatch.setattr(usage_meter, "reconciliation", lambda **_: {"meter": meter})
    return daemon_for(f)


def test_a_repeat_is_counted_and_reaches_nobody(fleet_fixture, fixed_now, monkeypatch):
    outside_scene(fleet_fixture)
    filed = filings(monkeypatch)
    seed_occurrences(fleet_fixture, "cost_outside_spend_high", [3.0])

    reconcile(fleet_fixture, daemon_for(fleet_fixture))

    newest = alarms(fleet_fixture)[0]
    assert "Occurrence 2 of this standing condition" in newest["reason"]
    assert newest["alarm_status"] == "raised", "the supervisor still judges it"
    central = CentralStore()
    try:
        assert central.unacked_inbox("warning") == []
    finally:
        central.close()
    assert filed == []


def test_a_gap_over_the_limit_starts_a_new_condition(fleet_fixture, fixed_now, monkeypatch):
    outside_scene(fleet_fixture)
    seed_occurrences(fleet_fixture, "cost_outside_spend_high", [20.0, 8.0])

    reconcile(fleet_fixture, daemon_for(fleet_fixture))

    assert "Occurrence" not in alarms(fleet_fixture)[0]["reason"]
    central = CentralStore()
    try:
        assert len(central.unacked_inbox("warning")) == 1
    finally:
        central.close()


def test_outside_spend_counts_and_never_files(fleet_fixture, fixed_now, monkeypatch):
    outside_scene(fleet_fixture)
    filed = filings(monkeypatch)
    seed_occurrences(fleet_fixture, "cost_outside_spend_high", [6.0, 3.0])

    reconcile(fleet_fixture, daemon_for(fleet_fixture))

    newest = alarms(fleet_fixture)[0]
    assert "Occurrence 3" in newest["reason"]
    assert newest["alarm_status"] == "informational", "no model call on a known condition"
    assert filed == []


def test_a_standing_residual_files_once_and_silently(fleet_fixture, fixed_now, monkeypatch):
    filed = filings(monkeypatch)
    daemon = residual_scene(fleet_fixture, monkeypatch)
    seed_occurrences(fleet_fixture, "cost_residual_high", [6.0, 3.0])

    reconcile(fleet_fixture, daemon)
    reconcile(fleet_fixture, daemon)

    assert len(filed) == 1
    assert filed[0]["notify"] is False and filed[0]["priority"] == "high"
    assert "recurred 3 times" in filed[0]["description"]
    assert "usage_meter" in filed[0]["description"]
    # The tracker is public: spend stays on the alarm rows it names
    assert "$" not in filed[0]["description"] and "$" not in filed[0]["actual"]
    assert all(r["id"] in filed[0]["description"] for r in alarms(fleet_fixture)
               if r["kind"] == "cost_residual_high")
    kinds = [r["kind"] for r in alarms(fleet_fixture)]
    assert kinds.count("cost_residual_diagnosed") == 1
    central = CentralStore()
    try:
        assert central.unacked_inbox("warning") == []
    finally:
        central.close()


def test_a_failed_filing_retries_on_the_next_pass(fleet_fixture, fixed_now, monkeypatch):
    from jarvis import bugreport

    daemon = residual_scene(fleet_fixture, monkeypatch)
    seed_occurrences(fleet_fixture, "cost_residual_high", [6.0, 3.0])

    def refuse(**kwargs):
        raise bugreport.BugReportError("the tracker is down")

    monkeypatch.setattr(bugreport, "report_bug", refuse)
    reconcile(fleet_fixture, daemon)
    assert "cost_residual_diagnosed" not in [r["kind"] for r in alarms(fleet_fixture)]

    filed = filings(monkeypatch)
    reconcile(fleet_fixture, daemon)
    assert len(filed) == 1


def test_the_packet_carries_prior_verdicts_and_a_verdict_stays_on_the_row(
        fleet_fixture, fixed_now):
    from jarvis import supervisor
    from jarvis.catalog import SupervisorConfig

    seed_occurrences(fleet_fixture, "cost_residual_high", [3.0])
    store = ProjectStore(fleet_fixture.project_path)
    central = CentralStore()
    try:
        alarm = store.add_finding(None, kind="cost_residual_high", reason="again")

        packet = supervisor.build_evidence(store, {"kind": "machine", "row": {}}, alarm)
        assert "# This standing condition" in packet
        assert "occurrence 2" in packet
        assert "cannot be explained 3.0" in packet
        assert "# The work order" not in packet

        supervisor._apply(store, None, central, "proj_a", None, alarm,
                          {"decision": "escalate", "failed": False, "reason": "unexplained",
                           "note": ""}, packet, SupervisorConfig())
        row = store.get_alarm(alarm["id"])
        assert (row["status"], row["verdict"]) == ("acked", "escalate")
        assert central.unacked_inbox("warning") == []
    finally:
        central.close()
        store.close()


def test_the_inbox_line_states_what_the_user_owes(fleet_fixture, fixed_now, monkeypatch):
    daemon = residual_scene(fleet_fixture, monkeypatch)
    reconcile(fleet_fixture, daemon)

    central = CentralStore()
    try:
        (told,) = central.unacked_inbox("warning")
    finally:
        central.close()
    assert told["wo_id"] is None
    assert "No action is owed from you" in told["body"]
    assert alarms(fleet_fixture)[0]["id"] in told["body"]
    assert "wo-" not in told["body"]


def test_a_machine_wide_review_runs_with_no_work_order(fleet_fixture, fixed_now, monkeypatch):
    from jarvis import structured, supervisor
    from jarvis.catalog import SupervisorConfig
    from jarvis.neo_store import NeoStore

    seen: list[str] = []

    def judged(evidence, **_):
        seen.append(evidence)
        return {"decision": "escalate", "failed": False, "reason": "unexplained", "note": ""}

    monkeypatch.setattr(structured, "request", judged)
    store = ProjectStore(fleet_fixture.project_path)
    neo = NeoStore()
    try:
        alarm = store.add_finding(None, kind="cost_residual_high", reason="again")
        supervisor.review(store, neo, "proj_a", None, alarm, SupervisorConfig())
        assert store.get_alarm(alarm["id"])["verdict"] == "escalate"
        assert "The whole machine" in seen[0] and "# The work order" not in seen[0]
    finally:
        neo.close()
        store.close()


def test_a_work_order_alarm_gets_no_standing_section(fleet_fixture, fixed_now):
    from jarvis import supervisor

    order = fleet_fixture.order("an order", status="completed")
    store = ProjectStore(fleet_fixture.project_path)
    try:
        alarm = store.add_finding(order, kind="some_cost_kind", reason="x")
        assert "# This standing condition" not in supervisor.build_evidence(
            store, {"kind": "work_order", "row": store.get_work_order(order)}, alarm)
    finally:
        store.close()


def test_the_alarm_is_silent_below_the_floor_and_on_the_seed(fleet_fixture, fixed_now):
    fleet_fixture.order("the carrier", status="completed")
    scene(fleet_fixture, until=SPAN_UNTIL, samples=120)
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
    scene(fleet_fixture, until=SPAN_UNTIL, samples=120)
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
