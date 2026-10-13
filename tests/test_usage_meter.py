"""The account's usage meter: sampler, schema, segment sum, estimator, cadence.

Spec docs/superpowers/specs/2026-10-08-usage-meter-samples-and-outside-spend.md §§1-5, 8.
Tests 1-14, 24-28, 38-42 of its §13. No test performs a request: `fetch(opener=…)` is the
seam and `JARVIS_CLAUDE_CREDENTIALS` keeps `token()` on a sandbox file.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from pathlib import Path

import pytest

from jarvis import db, invariants, usage_meter
from jarvis.catalog import CostConfig
from jarvis.central_store import CentralStore
from jarvis.claude_cli import CREDENTIALS_ENV

TOKEN = "sk-ant-oat01-SANDBOX-TOKEN-VALUE"
MINUTE = 60.0
T0 = 1_760_000_000.0


# -- fixtures ---------------------------------------------------------------------------


@pytest.fixture()
def credentials(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    monkeypatch.setenv(CREDENTIALS_ENV, str(path))

    def write(token: str = TOKEN) -> None:
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))

    write()
    return write


@pytest.fixture()
def central(jarvis_home):
    store = CentralStore()
    try:
        yield store
    finally:
        store.close()


def payload(**over) -> dict:
    """The shape measured on this account 2026-10-08."""
    body = {
        "five_hour": {"utilization": 44.0, "resets_at": "2026-10-08T17:20:00Z"},
        "seven_day": {"utilization": 12.5, "resets_at": "2026-10-13T04:00:00Z"},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
        "limits": {"five_hour": {"limit_dollars": None, "used_dollars": None}},
        "extra_usage": {"is_enabled": False},
    }
    body.update(over)
    return body


class FakeOpener:
    """`urlopen`'s shape: a context manager with `.status` and `.read()`."""

    def __init__(self, body=None, *, status: int = 200, raises: Exception | None = None):
        self.body = body
        self.status = status
        self.raises = raises
        self.requests: list = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.raises is not None:
            raise self.raises
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(usage_meter.ENDPOINT, status, "nope", {}, None)


def rows(central: CentralStore) -> list[dict]:
    return db.rows_to_dicts(
        central.conn.execute("SELECT * FROM usage_samples ORDER BY id").fetchall())


# -- 1-7. sampler and schema ------------------------------------------------------------


def test_a_measured_payload_writes_one_ok_row(central, credentials):
    opener = FakeOpener(payload())
    usage_meter.sample_once(central, opener=opener)

    (row,) = rows(central)
    assert row["ok"] == 1 and row["reason"] == ""
    assert row["five_hour_pct"] == 44.0 and row["seven_day_pct"] == 12.5
    assert row["five_hour_resets_at"] == pytest.approx(1_791_480_000.0)
    assert row["seven_day_resets_at"] == pytest.approx(1_791_864_000.0)
    assert row["extra_json"] == "{}"
    assert row["http_status"] == 200


def test_a_known_extra_window_lands_in_extra_json(central, credentials):
    opener = FakeOpener(payload(seven_day_opus={"utilization": 12.0,
                                                "resets_at": "2026-10-13T04:00:00Z"}))
    usage_meter.sample_once(central, opener=opener)

    (row,) = rows(central)
    assert row["ok"] == 1
    assert json.loads(row["extra_json"])["seven_day_opus"] == {
        "utilization": 12.0, "resets_at": pytest.approx(1_791_864_000.0)}


def test_a_new_codename_lands_unchanged(central, credentials):
    opener = FakeOpener(payload(quartz_lantern={"utilization": 3.0,
                                                "resets_at": "2026-10-13T04:00:00Z"}))
    usage_meter.sample_once(central, opener=opener)

    (row,) = rows(central)
    assert row["ok"] == 1
    assert json.loads(row["extra_json"])["quartz_lantern"]["utilization"] == 3.0


def test_limits_is_in_the_input_and_in_no_stored_column(central, credentials):
    usage_meter.sample_once(central, opener=FakeOpener(payload()))

    (row,) = rows(central)
    assert "limits" not in json.loads(row["extra_json"])
    assert "limits" not in json.dumps(dict(row))


FAILURES = {
    "connection": FakeOpener(None, raises=urllib.error.URLError("no route to host")),
    "401": FakeOpener(None, raises=http_error(401)),
    "500": FakeOpener(None, raises=http_error(500)),
    "not-json": FakeOpener(b"<html>maintenance</html>"),
    "no-five-hour": FakeOpener(payload(five_hour={"resets_at": "2026-10-08T17:20:00Z"})),
    "string-utilization": FakeOpener(payload(five_hour={"utilization": "44"})),
}


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_every_failure_writes_exactly_one_gap_row(case, central, credentials):
    """A gap row is the only honest answer: 0.0 would read as a window reset (§2)."""
    usage_meter.sample_once(central, opener=FAILURES[case])

    (row,) = rows(central)
    assert row["ok"] == 0
    assert row["reason"]
    for column in ("five_hour_pct", "five_hour_resets_at",
                   "seven_day_pct", "seven_day_resets_at"):
        assert row[column] is None


def test_the_token_is_read_fresh_on_every_fetch(credentials):
    opener = FakeOpener(payload())
    usage_meter.fetch(opener=opener)
    credentials("sk-ant-oat01-ROTATED")
    usage_meter.fetch(opener=opener)

    bearers = [r.get_header("Authorization") for r in opener.requests]
    assert bearers == [f"Bearer {TOKEN}", "Bearer sk-ant-oat01-ROTATED"]
    assert opener.requests[0].get_header("Anthropic-beta") == usage_meter.BETA


def test_no_log_no_reason_and_no_column_carries_the_token_or_the_body(
        central, credentials, caplog):
    caplog.set_level(logging.DEBUG)
    usage_meter.sample_once(central, opener=FakeOpener(payload()))
    usage_meter.sample_once(central, opener=FakeOpener(None, raises=http_error(401)))

    written = json.dumps([dict(r) for r in rows(central)])
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (TOKEN, "extra_usage", "is_enabled"):
        assert secret not in written
        assert secret not in logged


# -- 8-14. the segment sum --------------------------------------------------------------


def sample(ts: float, pct: float | None, resets: float, *, key: str = "five_hour",
           ok: bool = True) -> usage_meter.Sample:
    fields = {f"{key}_pct": pct, f"{key}_resets_at": resets} if ok else {}
    return usage_meter.Sample(ts=ts, ok=ok, reason="" if ok else "500 from the endpoint",
                              **fields)


def series(start: float, pcts, resets: float, *, key: str = "five_hour", step=MINUTE):
    return [sample(start + i * step, p, resets, key=key) for i, p in enumerate(pcts)]


def test_a_span_with_no_reset_is_end_minus_start():
    found = usage_meter.segments(series(T0, [10.0, 12.0, 17.0], T0 + 3600),
                                 since=T0, until=T0 + 10 * MINUTE, key="five_hour")
    assert found.delta_points == pytest.approx(7.0)
    assert found.reset_count == 0 and len(found.segments) == 1


def test_a_reset_inside_the_span_sums_two_segments():
    """End-minus-start is asserted to be the WRONG answer, so a refactor to it fails."""
    rows_ = (series(T0, [10.0, 40.0], T0 + 3600)
             + series(T0 + 2 * MINUTE, [3.0, 9.0], T0 + 7200))
    found = usage_meter.segments(rows_, since=T0, until=T0 + 10 * MINUTE,
                                 key="five_hour")

    assert [s.start_pct for s in found.segments] == [10.0, 0.0]
    assert found.delta_points == pytest.approx(30.0 + 9.0)
    assert found.reset_count == 1
    assert found.delta_points != pytest.approx(9.0 - 10.0)


def test_a_drop_with_resets_at_unchanged_is_a_boundary_and_an_anomaly():
    rows_ = series(T0, [10.0, 40.0, 3.0, 9.0], T0 + 3600)
    found = usage_meter.segments(rows_, since=T0, until=T0 + 10 * MINUTE,
                                 key="five_hour")

    assert [s.cause for s in found.segments] == ["", "drop"]
    assert found.anomalies == 1
    assert found.delta_points >= 0.0


def test_two_resets_inside_one_span_give_three_segments():
    rows_ = (series(T0, [10.0, 40.0], T0 + 3600)
             + series(T0 + 2 * MINUTE, [3.0, 9.0], T0 + 7200)
             + series(T0 + 4 * MINUTE, [1.0, 6.0], T0 + 10800))
    found = usage_meter.segments(rows_, since=T0, until=T0 + 10 * MINUTE,
                                 key="five_hour")

    assert len(found.segments) == 3 and found.reset_count == 2
    assert found.delta_points == pytest.approx(30.0 + 9.0 + 6.0)


def test_the_nearest_earlier_sample_is_the_start_and_beyond_it_the_head_is_uncovered():
    rows_ = series(T0, [10.0, 12.0, 17.0], T0 + 3600)
    near = usage_meter.segments(rows_, since=T0 + 30, until=T0 + 10 * MINUTE,
                                key="five_hour")
    assert near.segments[0].start_pct == 10.0
    assert near.coverage.head_uncovered_seconds == 0.0

    far = usage_meter.segments(rows_, since=T0 - 600, until=T0 + 10 * MINUTE,
                               key="five_hour", nearest_seconds=300)
    assert far.segments[0].start_pct == 10.0
    assert far.delta_points == pytest.approx(7.0)
    assert far.coverage.head_uncovered_seconds == pytest.approx(600.0)


def test_a_seven_day_reset_runs_through_the_same_path():
    rows_ = (series(T0, [10.0, 40.0], T0 + 3600, key="seven_day")
             + series(T0 + 2 * MINUTE, [3.0, 9.0], T0 + 7200, key="seven_day"))
    found = usage_meter.segments(rows_, since=T0, until=T0 + 10 * MINUTE,
                                 key="seven_day")
    assert found.reset_count == 1
    assert found.delta_points == pytest.approx(39.0)


def test_gap_rows_are_excluded_from_segments_and_counted_in_coverage():
    rows_ = ([sample(T0, 10.0, T0 + 3600)]
             + [sample(T0 + i * MINUTE, None, 0.0, ok=False) for i in (1, 2, 3)]
             + [sample(T0 + 4 * MINUTE, 17.0, T0 + 3600)])
    found = usage_meter.segments(rows_, since=T0, until=T0 + 5 * MINUTE,
                                 key="five_hour")

    assert found.delta_points == pytest.approx(7.0)
    assert found.coverage.gap_rows == 3
    assert found.coverage.samples == 2
    assert found.coverage.share == pytest.approx(2 / 5)
    assert found.coverage.longest_gap_seconds == pytest.approx(4 * MINUTE)


# -- 24-28. dollars per point -----------------------------------------------------------


RESETS = T0 + 9 * 3600


def run(start: float, *, minutes: int, points: float, base: float = 0.0,
        resets: float = RESETS):
    """One run of consecutive ok samples rising `points` over `minutes`."""
    return [sample(start + i * MINUTE, base + points * i / (minutes - 1), resets)
            for i in range(minutes)]


def store_samples(central: CentralStore, rows_) -> None:
    for s in rows_:
        usage_meter.record(central, s)


def cfg_for(**over) -> CostConfig:
    return CostConfig(**over)


def test_three_clean_spans_are_measured_and_the_value_is_the_median(central, jarvis_home):
    starts = [T0, T0 + 2 * 3600, T0 + 4 * 3600]
    for start in starts:
        store_samples(central, run(start, minutes=40, points=10.0))
    central.conn.commit()
    usd = {starts[0]: 29.0, starts[1]: 30.0, starts[2]: 31.0}

    found = usage_meter.dollars_per_point(
        now=T0 + 7 * 3600, cfg=cfg_for(), home=Path(jarvis_home),
        visible=lambda *, since, until: usd[min(usd, key=lambda s: abs(s - since))])

    assert found["source"] == "measured"
    assert found["basis_spans"] == 3
    assert found["value"] == pytest.approx(3.0)       # median of 2.9, 3.0, 3.1
    assert found["basis_points"] == pytest.approx(30.0)
    assert found["basis_usd"] == pytest.approx(90.0)
    assert found["low"] < found["value"] < found["high"]


def test_one_clean_span_is_thin(central, jarvis_home):
    store_samples(central, run(T0, minutes=40, points=10.0))
    central.conn.commit()

    found = usage_meter.dollars_per_point(now=T0 + 3600, cfg=cfg_for(),
                                          home=Path(jarvis_home),
                                          visible=lambda *, since, until: 20.0)
    assert found["source"] == "thin"
    assert found["basis_spans"] == 1
    assert found["value"] == pytest.approx(2.0)


def test_no_clean_span_falls_back_to_the_seed(central, jarvis_home):
    found = usage_meter.dollars_per_point(now=T0, cfg=cfg_for(), home=Path(jarvis_home),
                                          visible=lambda *, since, until: 0.0)
    assert found["source"] == "seed"
    assert found["value"] == cfg_for().meter_dollars_per_point
    assert found["basis_spans"] == 0


@pytest.mark.parametrize("case", ["reset", "gap", "short", "flat"])
def test_each_exclusion_rule_empties_the_basis(case, central, jarvis_home):
    """Each construction is 40 minutes and 10 points — enough but for the ONE rule."""
    if case == "reset":
        rows_ = (run(T0, minutes=20, points=5.0)
                 + run(T0 + 20 * MINUTE, minutes=20, points=5.0, resets=T0 + 14 * 3600))
    elif case == "gap":
        rows_ = (run(T0, minutes=20, points=5.0)
                 + run(T0 + 25 * MINUTE, minutes=20, points=5.0, base=5.0))
    elif case == "short":
        rows_ = run(T0, minutes=10, points=10.0)
    else:
        rows_ = run(T0, minutes=40, points=2.0)
    store_samples(central, rows_)
    central.conn.commit()

    found = usage_meter.dollars_per_point(now=T0 + 3 * 3600, cfg=cfg_for(),
                                          home=Path(jarvis_home),
                                          visible=lambda *, since, until: 20.0)
    assert found["basis_spans"] == 0
    assert found["source"] == "seed"


def test_the_band_widens_with_the_number_of_contributing_spans():
    """Quantisation is ±1 point PER SPAN, so the same total points over more spans is a
    wider band (§5)."""
    one = usage_meter.band(total_usd=60.0, total_points=30.0, spans=1)
    three = usage_meter.band(total_usd=60.0, total_points=30.0, spans=3)
    assert three[0] < one[0] and three[1] > one[1]


# -- 38-42. cadence, the once-per-streak notice, the invariant --------------------------


def gap_rows(central: CentralStore, count: int, *, start: float = T0) -> None:
    for i in range(count):
        usage_meter.record(central, usage_meter.Sample(
            ts=start + i * MINUTE, ok=False, reason="401 from the usage endpoint"))
    central.conn.commit()


def test_the_sampler_fires_once_per_tick_not_once_per_project(
        jarvis_home, catalog_file, credentials, fake_claude, monkeypatch):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon, USAGE_SAMPLE_EVERY_TICKS

    from jarvis.testing import make_git_project

    assert USAGE_SAMPLE_EVERY_TICKS == 12
    data = json.loads(catalog_file.read_text())
    second = make_git_project(catalog_file.parent, "proj_b")
    data["projects"].append({"name": "proj_b", "path": str(second)})
    catalog_file.write_text(json.dumps(data))

    catalog = load_catalog(catalog_file)
    assert len(catalog.projects) == 2
    daemon = Daemon(catalog)
    opener = FakeOpener(payload())
    daemon.meter_opener = opener

    for _ in range(USAGE_SAMPLE_EVERY_TICKS):
        daemon.sample_usage_meter()
    assert len(opener.requests) == USAGE_SAMPLE_EVERY_TICKS

    ticks = []
    monkeypatch.setattr(Daemon, "sample_usage_meter", lambda self: ticks.append(1))
    for _ in range(USAGE_SAMPLE_EVERY_TICKS):
        daemon.tick()
    assert len(ticks) == 1       # one per 12 ticks, and one per TICK, not per project


def test_a_sampler_exception_records_a_gap_row_and_does_not_take_the_tick_down(
        jarvis_home, catalog_file, credentials, fake_claude, caplog, monkeypatch):
    """Not a transport failure — the SAMPLER itself raising, which `fetch` never sees."""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    caplog.set_level(logging.WARNING)
    daemon = Daemon(load_catalog(catalog_file))

    def boom(*a, **k):
        raise RuntimeError("the sampler itself broke")

    monkeypatch.setattr(usage_meter, "sample_once", boom)
    spec = daemon.catalog.projects[0]
    daemon.central.upsert_project(spec.name, str(spec.path), "")
    daemon.central.conn.commit()
    daemon.tick()

    (row,) = rows(daemon.central)
    assert row["ok"] == 0 and "RuntimeError" in row["reason"]
    assert any("usage meter sampler failed" in r.getMessage() for r in caplog.records)
    # The rest of the tick still ran: the project was touched after the sampler failed.
    assert daemon.central.get_project("proj_a")["last_seen"] is not None


def test_the_repeated_failure_notice_fires_once_per_streak(jarvis_home, catalog_file):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    daemon = Daemon(load_catalog(catalog_file))
    central = daemon.central

    gap_rows(central, 9)
    daemon.notice_meter_gap()
    assert not _meter_inbox(central)

    gap_rows(central, 1, start=T0 + 9 * MINUTE)
    daemon.notice_meter_gap()
    assert len(_meter_inbox(central)) == 1
    assert central.get_state("usage_meter_notice_streak") == str(T0)

    for i in range(10, 30):
        gap_rows(central, 1, start=T0 + i * MINUTE)
        daemon.notice_meter_gap()
    assert len(_meter_inbox(central)) == 1

    usage_meter.record(central, usage_meter.Sample(
        ts=T0 + 30 * MINUTE, ok=True, five_hour_pct=1.0, seven_day_pct=1.0))
    central.conn.commit()
    daemon.notice_meter_gap()
    assert not central.get_state("usage_meter_notice_streak")

    second = T0 + 40 * MINUTE
    gap_rows(central, 10, start=second)
    daemon.notice_meter_gap()
    assert len(_meter_inbox(central)) == 2


def test_the_notice_survives_a_daemon_restart(jarvis_home, catalog_file):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    catalog = load_catalog(catalog_file)
    first = Daemon(catalog)
    gap_rows(first.central, 10)
    first.notice_meter_gap()
    assert len(_meter_inbox(first.central)) == 1

    second = Daemon(catalog)
    gap_rows(second.central, 5, start=T0 + 10 * MINUTE)
    second.notice_meter_gap()
    assert len(_meter_inbox(second.central)) == 1


def _meter_inbox(central: CentralStore) -> list[dict]:
    return [r for r in db.rows_to_dicts(
        central.conn.execute("SELECT * FROM inbox").fetchall())
        if "usage meter" in r["title"]]


def _stale(found):
    matching = [v for v in found if v.invariant == "INV-USAGE-METER-STALE"]
    assert len(matching) <= 1
    return matching[0] if matching else None


def test_a_fresh_ok_row_keeps_the_stale_invariant_silent(central):
    usage_meter.record(central, usage_meter.Sample(ts=db.now() - 30, ok=True,
                                                   five_hour_pct=4.0))
    central.conn.commit()
    assert _stale(invariants.check_os()) is None


def test_the_stale_invariant_is_derived_from_rows_with_no_daemon(central):
    """No daemon has ever run here: the rows alone fire it (§8)."""
    reason = "credentials file has no claudeAiOauth.accessToken"
    now = db.now()
    usage_meter.record(central, usage_meter.Sample(ts=now - 21 * MINUTE, ok=True,
                                                   five_hour_pct=4.0))
    for i in range(20):
        usage_meter.record(central, usage_meter.Sample(
            ts=now - (20 - i) * MINUTE, ok=False, reason=reason))
    central.conn.commit()

    found = _stale(invariants.check_os())
    assert found is not None
    assert reason in found.detail
    assert found.context["gap_rows"] == 20
    assert found.context["last_ok_ts"] == pytest.approx(now - 21 * MINUTE)
