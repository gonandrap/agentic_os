"""A dashboard failure has to be as visible to the OS as a daemon failure"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import daemon as daemon_mod  # noqa: E402
from jarvis import db, invariants, ops, release, service_manager, uilog  # noqa: E402
from jarvis.catalog import UiHealthConfig  # noqa: E402
from jarvis.catalog import load_catalog  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.daemon import Daemon  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402
from jarvis.ui import app as ui_app  # noqa: E402
from jarvis.ui.app import LoopWatchdog, create_app  # noqa: E402


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return jarvis_home


def _boom(msg: str = "kaboom") -> Exception:
    try:
        raise RuntimeError(msg)
    except RuntimeError as e:
        return e


# -- the daemon tells the user

def test_daemon_raises_an_inbox_item_for_new_dashboard_errors(started, catalog_file):
    """The daemon is what turns things the OS notices into things the user is told"""
    d = Daemon(load_catalog(catalog_file))
    uilog.record_error("GET", "/wo/proj_a/wo-1", _boom("KeyError-ish"))

    assert d.check_ui_log() == 1

    central = CentralStore()
    try:
        items = central.unacked_inbox()
    finally:
        central.close()
    assert len(items) == 1
    assert "dashboard raised 1 unhandled error" in items[0]["title"]
    assert "/wo/proj_a/wo-1" in items[0]["body"]
    assert str(uilog.ui_log_path()) in items[0]["body"]
    assert items[0]["level"] == "warning"


def test_a_standing_dashboard_error_is_announced_once_not_every_tick(started,
                                                                    catalog_file):
    """The daemon ticks every five seconds"""
    d = Daemon(load_catalog(catalog_file))
    uilog.record_error("GET", "/x", _boom())

    assert d.check_ui_log() == 1
    assert d.check_ui_log() == 0
    assert d.check_ui_log() == 0

    uilog.record_error("GET", "/y", _boom("a second, different failure"))
    assert d.check_ui_log() == 1

    central = CentralStore()
    try:
        assert len(central.unacked_inbox()) == 2
    finally:
        central.close()


def test_a_crash_loop_raises_one_item_not_hundreds(started, catalog_file):
    d = Daemon(load_catalog(catalog_file))
    for i in range(50):
        uilog.record_error("GET", f"/p{i}", _boom(f"boom {i}"))

    assert d.check_ui_log() == 50

    central = CentralStore()
    try:
        items = central.unacked_inbox()
    finally:
        central.close()
    assert len(items) == 1
    assert "50 unhandled errors" in items[0]["title"]
    assert "+45 more" in items[0]["body"]


def test_the_first_tick_ever_does_not_alert_on_a_log_full_of_history(started,
                                                                    catalog_file):
    """What 0.5.5 actually did to production"""
    uilog.ui_log_path().parent.mkdir(parents=True, exist_ok=True)
    old = time.strftime(uilog._STAMP, time.localtime(db.now() - 7 * 24 * 3600))
    with uilog.ui_log_path().open("a") as f:
        for i in range(4):
            f.write(f"{old} [ERROR] GET /neo — UndefinedError: 'opinions{i}' undefined\n"
                    "    Traceback (most recent call last):\n")

    d = Daemon(load_catalog(catalog_file))
    assert d.check_ui_log() == 0

    central = CentralStore()
    try:
        assert central.unacked_inbox() == []
    finally:
        central.close()

    # And the cursor moved on, so a genuine error after it is still the next thing the user hears about
    uilog.record_error("GET", "/neo", _boom("a real, current failure"))
    assert d.check_ui_log() == 1


def test_the_ui_watch_never_stalls_the_tick(started, catalog_file, monkeypatch):
    """A tick that dies on the log watcher would stop dispatch, delivery and reconciliation"""
    d = Daemon(load_catalog(catalog_file))
    monkeypatch.setattr(uilog, "read_errors",
                        lambda *_: (_ for _ in ()).throw(OSError("disk gone")))
    d.tick()  # must not raise


# -- `jarvis status` and `jarvis doctor` say so

def test_status_reports_dashboard_errors_and_asks_for_attention(started):
    quiet = ops.os_status()
    assert quiet["ui"]["errors"] == 0
    assert not [a for a in quiet["attention"] if a["status"] == "ui"]

    uilog.record_error("GET", "/wo/proj_a/wo-1", _boom("the page is down"))

    st = ops.os_status()
    assert st["ui"]["errors"] == 1
    assert st["ui"]["recent"][0]["path"] == "/wo/proj_a/wo-1"
    items = [a for a in st["attention"] if a["status"] == "ui"]
    assert len(items) == 1
    assert "the page is down" in items[0]["reason"]
    assert not st["healthy"]


def test_doctor_reports_dashboard_errors_at_the_os_level(started):
    assert ops.run_doctor()["os"] == []

    uilog.record_error("GET", "/wo/proj_a/wo-1", _boom("the page is down"))

    res = ops.run_doctor()
    assert res["violations"] >= 1
    assert [v["invariant"] for v in res["os"]] == ["INV-UI-HEALTHY"]
    assert "the page is down" in res["os"][0]["detail"]
    # Not repairable: nothing here has a resolution derivable from state
    assert not res["os"][0]["repaired"]


def test_doctor_scoped_to_one_project_still_reports_the_dashboard(started):
    """`--project` narrows which project's invariants run"""
    uilog.record_error("GET", "/", _boom("still broken"))
    assert ops.run_doctor(project="proj_a")["os"][0]["invariant"] == "INV-UI-HEALTHY"


# -- the dashboard writes what the above reads

def test_a_500_flows_all_the_way_from_the_browser_to_the_inbox(started, catalog_file,
                                                               monkeypatch):
    """End to end, and the whole point of this work order: a user hits a broken page"""
    client = TestClient(create_app(), follow_redirects=False,
                        raise_server_exceptions=False)
    real_os_status = ops.os_status
    monkeypatch.setattr(ops, "os_status", lambda: 1 / 0)

    assert client.get("/").status_code == 500

    # Restore by hand, NOT monkeypatch.undo(): `undo` reverts *every* patch on the shared monkeypatch...
    monkeypatch.setattr(ops, "os_status", real_os_status)
    Daemon(load_catalog(catalog_file)).check_ui_log()

    central = CentralStore()
    try:
        items = central.unacked_inbox()
    finally:
        central.close()
    assert len(items) == 1
    assert "ZeroDivisionError" in items[0]["body"]
    assert ops.os_status()["ui"]["errors"] == 1


# -- the access log

def test_requests_land_in_the_access_log(started):
    """"When I click on the link I get an internal server error" was impossible to place in time because..."""
    client = TestClient(create_app(), follow_redirects=False)
    client.get("/")
    client.get("/wo/proj_gone/wo-1")

    lines = uilog.access_log_path().read_text().splitlines()
    assert any("[200] GET /" in ln for ln in lines)
    assert any("[404] GET /wo/proj_gone/wo-1" in ln for ln in lines)


def test_the_dashboards_own_refresh_poll_is_not_logged_unless_it_fails(started,
                                                                      monkeypatch):
    """`/api/status` fires every 15s"""
    client = TestClient(create_app(), follow_redirects=False,
                        raise_server_exceptions=False)
    client.get("/api/status")
    assert not uilog.access_log_path().exists()

    monkeypatch.setattr(ops, "os_status", lambda: 1 / 0)
    client.get("/api/status")
    assert "[500] GET /api/status" in uilog.access_log_path().read_text()


# -- time in each state (spec 2026-09-27-time-in-each-state §7)

@pytest.fixture()
def browser(started):
    return TestClient(create_app(), follow_redirects=False)


def test_the_work_order_page_shows_time_in_state(browser, project):
    """The fourth tab"""
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        store.set_status(wo["id"], "needs_review")
        store.add_event(wo["id"], "turn_ended")   # so both figures have a value
    finally:
        store.close()

    page = browser.get(f"/wo/proj_a/{wo['id']}").text

    assert "Time in state" in page
    assert "tab-states" in page
    assert "Needs your review" in page              # a per-status row, labelled
    assert "Queued" in page                         # and the span it came from
    # Two figures, labelled differently: equal numbers are the common case
    assert "in this status" in page
    assert "nothing on the record for" in page


def test_the_feature_page_labels_a_coarse_span_as_approximate(browser, project):
    """The flag, on the surface: an unlabelled coarse span is the defect Neo's ruling names"""
    store = ProjectStore(project)
    try:
        fo = store.create_feature_order("CSV export", description="the whole ask")
        store.conn.execute("DELETE FROM wo_state_spans WHERE order_id=?", (fo["id"],))
        store.conn.commit()
    finally:
        store.close()

    page = browser.get(f"/fo/proj_a/{fo['id']}").text

    assert "Time in state" in page
    assert "a feature order keeps no event trail" in page


# -- a usage-limit hold on the page (spec 2026-09-30 §3)

#: 5h24m in `running`, of which the order was let work 24m
HELD_WALL = 5 * 3600 + 24 * 60
HELD_WORKED = 24 * 60


def _event_at(store: ProjectStore, wo_id: str, kind: str, ts: float,
              payload: dict) -> None:
    store.add_event(wo_id, kind, payload)
    store.conn.execute(
        "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events WHERE wo_id=?)",
        (ts, wo_id))


def _held_order(project, *, resumed: bool):
    """Issue 887 on the page: 45m worked of 5h24m in `running`, the rest held"""
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    now = db.now()
    t0 = now - HELD_WALL
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        store.set_status(wo["id"], "running")
        spans = [r["id"] for r in store.conn.execute(
            "SELECT id FROM wo_state_spans WHERE order_id=? ORDER BY id", (wo["id"],))]
        for span_id, ts in zip(spans, (t0 - 3600, t0)):
            store.conn.execute("UPDATE wo_state_spans SET ts=? WHERE id=?",
                               (ts, span_id))
        store.conn.execute("UPDATE work_orders SET created_at=? WHERE id=?",
                           (t0 - 3600, wo["id"]))
        turn = store.create_turn(wo["id"], kind="message", prompt="go")
        store.finish_turn(turn["id"], state="done")
        store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                           (t0, t0 + HELD_WORKED, turn["id"]))
        _event_at(store, wo["id"], "turn_paused", t0 + HELD_WORKED,
                  {"reason": PAUSE_USAGE_LIMIT, "seq": 1})
        if resumed:
            _event_at(store, wo["id"], "turn_resumed", now, {"retried_seq": 1})
        store.conn.commit()
    finally:
        store.close()
    return wo


def test_a_status_never_held_renders_without_a_held_suffix(browser, project):
    """The common case acquires no noise: no parenthesis, no `held`, no `wall`"""
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        store.set_status(wo["id"], "needs_review")
        store.add_event(wo["id"], "turn_ended")
    finally:
        store.close()

    page = browser.get(f"/wo/proj_a/{wo['id']}").text

    assert "held by" not in page
    assert "wall)" not in page
    assert "held</span>" not in page


def _text(page: str) -> str:
    return " ".join(page.split())


def test_the_page_headlines_active_and_keeps_wall_beside_it(browser, project):
    """`running 24m (5.0h held by a fleet usage limit, 5.4h wall)` — Neo 1130"""
    wo = _held_order(project, resumed=True)

    page = browser.get(f"/wo/proj_a/{wo['id']}").text

    assert '<span class="mono" style="width: 70px; text-align: right;">24m</span>' in page
    assert "(5.0h held by a fleet usage limit, 5.4h wall)" in _text(page)
    # the Gantt line for the held span says how much of it was a hold
    assert "· 5.0h held" in _text(page)


def test_an_open_hold_reads_as_held_on_the_page(browser, project):
    """While the hold is open the current-status line says HELD, not `in this status`"""
    wo = _held_order(project, resumed=False)

    page = browser.get(f"/wo/proj_a/{wo['id']}").text

    assert "HELD 5.0h by a fleet usage limit, running 24m of 5.4h" in _text(page)
    assert "in this status" not in page


def test_the_tab_badge_shows_active_not_wall(browser, project):
    """A badge reading 5.4h on an order that was let work 24m is the issue's headline"""
    wo = _held_order(project, resumed=False)

    page = browser.get(f"/wo/proj_a/{wo['id']}").text

    assert '<span class="n">24m</span>' in page
    assert '<span class="n">5.4h</span>' not in page


# -- the wedge: the dashboard reports and heals it


def _wedge_app(gate: threading.Event):
    app = create_app()

    @app.get("/_wedge")
    def wedge_handler():
        """The holder"""
        gate.wait()
        return {"ok": True}

    @app.get("/_sync_control")
    def sync_control():
        """A SYNC route that does nothing — the control for `/healthz` being async"""
        return {"ok": True}

    return app


def _fast_probe(monkeypatch, **kw) -> None:
    """The probe's knobs are catalog keys, so the test sets them through the resolver"""
    cfg = UiHealthConfig(**{"probe_interval_seconds": 1, "probe_timeout_seconds": 1,
                            "trip_threshold": 2, **kw})
    monkeypatch.setattr(ops, "ui_health_config", lambda project=None: cfg)


def _release_and_drain(gate, futs) -> None:
    """Open the gate, then let every in-flight request finish before the client closes"""
    gate.set()
    cf.wait(futs, timeout=30)


def _limiter(client) -> dict:
    return client.get("/healthz").json()["limiter"]


def _wait_until(predicate, timeout: float = 20.0) -> bool:
    # clock-exempt: class 3 — spin-waits on the wedged anyio thread pool's own limiter figures, which only real threads release
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_the_pool_can_be_saturated_deterministically(started, monkeypatch):
    """The rig the next two tests stand on: every token borrowed by sync handlers that never return"""
    _fast_probe(monkeypatch)
    gate = threading.Event()
    pool = cf.ThreadPoolExecutor(max_workers=64)
    futs: list = []
    try:
        with TestClient(_wedge_app(gate)) as client:
            try:
                total = _limiter(client)["total"]
                futs.extend(pool.submit(client.get, "/_wedge") for _ in range(total))
                assert _wait_until(lambda: _limiter(client)["borrowed"] == total)
                assert _limiter(client)["available"] == 0
            finally:
                _release_and_drain(gate, futs)
    finally:
        pool.shutdown(wait=False)


def test_healthz_answers_while_the_pool_is_wedged_and_a_sync_route_cannot(
        started, monkeypatch):
    """The whole point of piece 1"""
    _fast_probe(monkeypatch)
    gate = threading.Event()
    pool = cf.ThreadPoolExecutor(max_workers=64)
    futs: list = []
    try:
        with TestClient(_wedge_app(gate)) as client:
            try:
                total = _limiter(client)["total"]
                futs.extend(pool.submit(client.get, "/_wedge") for _ in range(total))
                assert _wait_until(lambda: _limiter(client)["borrowed"] == total)
                assert _wait_until(
                    lambda: client.get("/healthz").json()["pool_healthy"] is False)

                # clock-exempt: class 4 — the instant compared against is real elapsed wall time proving /healthz answers while the pool is wedged, which no patched jarvis.db.now produces
                t0 = time.time()
                r = client.get("/healthz")
                assert time.time() - t0 < 5          # answers, while wedged
                body = r.json()
                assert r.status_code == 503
                assert body["pool_healthy"] is False
                assert body["limiter"]["available"] == 0
                assert body["last_ok_age_seconds"] is not None   # it WAS healthy
                assert body["uptime_seconds"] >= 0
                assert body["probe_failure_reason"] == "pool_timeout"

                control = pool.submit(client.get, "/_sync_control")
                futs.append(control)
                with pytest.raises(cf.TimeoutError):
                    control.result(timeout=3)
            finally:
                _release_and_drain(gate, futs)
    finally:
        pool.shutdown(wait=False)


def test_a_trip_dumps_every_threads_stack_and_stamps_the_wedge(started, monkeypatch):
    """The assertion the whole work order exists for: a dump that does not name the holder would leave the..."""
    _fast_probe(monkeypatch)
    gate = threading.Event()
    pool = cf.ThreadPoolExecutor(max_workers=64)
    futs: list = []
    try:
        with TestClient(_wedge_app(gate)) as client:
            try:
                total = _limiter(client)["total"]
                futs.extend(pool.submit(client.get, "/_wedge") for _ in range(total))
                assert _wait_until(lambda: uilog.read_wedge() is not None, timeout=30)
            finally:
                _release_and_drain(gate, futs)
    finally:
        pool.shutdown(wait=False)

    stamp = uilog.read_wedge()
    assert stamp["limiter"]["borrowed"] == total
    assert stamp["limiter"]["available"] == 0
    assert stamp["dump"] == str(uilog.stack_dump_path())
    assert stamp["since"] > 0
    assert stamp["version"]

    dump = uilog.stack_dump_path().read_text()
    assert "wedge_handler" in dump          # the holder, by name
    assert " in wait" in dump               # parked in threading.Event.wait

    # kn-57fb4919: `[ERROR]` in ui.log is a parsed contract with three readers
    ui_log = uilog.ui_log_path()
    assert "[ERROR]" not in (ui_log.read_text() if ui_log.exists() else "")


# -- a BLOCKED EVENT LOOP: the probe is an asyncio task, the watchdog is not (issue #1221)


def test_the_watchdog_dumps_while_the_event_loop_is_blocked(started, monkeypatch):
    """The assertion this fix exists for: no asyncio task can run here, so only a thread can dump"""
    _fast_probe(monkeypatch, loop_stall_seconds=1)
    monkeypatch.setattr(ui_app, "WATCHDOG_TICK_SECONDS", 0.2)
    app = create_app()

    @app.get("/_loopblock")
    async def loop_blocker():
        """Blocks the loop itself — not a threadpool token"""
        # clock-exempt: class 3 — a real sleep is what blocks a real event loop
        time.sleep(8)
        return {"ok": True}

    pool = cf.ThreadPoolExecutor(max_workers=2)
    futs: list = []
    try:
        with TestClient(app) as client:
            futs.append(pool.submit(client.get, "/_loopblock"))
            assert _wait_until(lambda: uilog.read_wedge() is not None, timeout=20), \
                "no wedge stamp was written while the event loop was blocked"
            stamp = uilog.read_wedge()
            cf.wait(futs, timeout=30)
    finally:
        pool.shutdown(wait=False)

    assert str(stamp["reason"]).startswith("event loop stalled")
    assert stamp["dump"] == str(uilog.stack_dump_path())
    dump = uilog.stack_dump_path().read_text()
    assert "event loop stalled" in dump
    assert "loop_blocker" in dump           # the frame that holds the loop, by name


def test_the_watchdog_dumps_once_per_stall_and_re_arms_on_recovery(started, monkeypatch):
    """A stall lasting hours must not rewrite the dump every tick"""
    _fast_probe(monkeypatch, loop_stall_seconds=10)
    probe = create_app().state.pool_probe
    probe.cfg = ops.ui_health_config()
    watchdog = LoopWatchdog(probe)

    # clock-exempt: class 3 — the heartbeat the watchdog reads is a time.monotonic() stamp
    now = time.monotonic()
    probe.heartbeat = now                       # fresh
    assert watchdog.check(now) is False
    probe.heartbeat = now - 11                   # stale past the threshold
    assert watchdog.check(now) is True
    assert watchdog.check(now) is False          # still stalled: one dump, not a stream
    probe.heartbeat = now                        # the loop came back
    assert watchdog.check(now) is False
    probe.heartbeat = now - 11                   # and stalled again
    assert watchdog.check(now) is True
    assert watchdog.dumps == 2


def test_the_watchdog_is_silent_when_ui_health_is_off(started, monkeypatch):
    """`ui_health.enabled: false` means the dashboard reports nothing about itself"""
    _fast_probe(monkeypatch, enabled=False, loop_stall_seconds=1)
    probe = create_app().state.pool_probe
    probe.cfg = ops.ui_health_config()
    watchdog = LoopWatchdog(probe)

    # clock-exempt: class 3 — an hour-stale monotonic heartbeat, which no db.now patch reaches
    probe.heartbeat = time.monotonic() - 3600
    assert watchdog.check(time.monotonic()) is False
    assert uilog.read_wedge() is None


# -- the detector is cause-independent (review round 1)


def test_a_wedge_that_never_saturates_the_pool_still_trips(started, monkeypatch):
    """Issue #986's own shape: far fewer blocked handlers than tokens"""
    _fast_probe(monkeypatch, trip_threshold=1)
    lock = threading.Lock()
    app = create_app()

    @app.get("/_lockwait")
    def lock_waiter():
        """The holder"""
        with lock:
            return {"ok": True}

    lock.acquire()
    pool = cf.ThreadPoolExecutor(max_workers=16)
    futs: list = []
    try:
        with TestClient(app) as client:
            try:
                total = _limiter(client)["total"]
                futs.extend(pool.submit(client.get, "/_lockwait") for _ in range(11))
                assert _wait_until(
                    lambda: client.get("/healthz").json()["wedged"] is True,
                    timeout=15), "/healthz never reported wedged on an unsaturated pool"

                body = client.get("/healthz").json()
                assert body["pool_healthy"] is False
                assert body["probe_failure_reason"] == "inflight_stall"
                assert body["inflight"] >= 1
                assert body["oldest_inflight_seconds"] >= 1
                # The discriminator: tokens were still free the whole time
                assert 11 < total
                assert _limiter(client)["available"] > 0
                assert _wait_until(lambda: uilog.read_wedge() is not None, timeout=15)
            finally:
                lock.release()
                cf.wait(futs, timeout=30)
    finally:
        pool.shutdown(wait=True)

    assert "lock_waiter" in uilog.stack_dump_path().read_text()


def _next_key(probe) -> int:
    key = probe.begin("GET", "/_counter")
    probe.end(key)
    return key


def test_only_routed_requests_count_as_in_flight(started):
    """A stream of 404s on unrouted paths must not read as a wedge: in the measured fault they answered in..."""
    app = create_app()
    probe = app.state.pool_probe
    seen: dict = {}

    @app.get("/_peek")
    def peek():
        seen["oldest"] = probe.oldest_inflight()
        return {"ok": True}

    @app.get("/_raises")
    def raiser():
        raise RuntimeError("kaboom")

    client = TestClient(app, raise_server_exceptions=False)

    k0 = _next_key(probe)
    assert client.get("/_definitely_not_a_route").status_code == 404
    assert _next_key(probe) == k0 + 1          # the 404 registered nothing

    k1 = _next_key(probe)
    assert client.get("/_peek").status_code == 200
    assert _next_key(probe) == k1 + 2          # the routed request registered one
    assert seen["oldest"] is not None
    assert seen["oldest"][1] == "GET /_peek"
    assert probe.oldest_inflight() is None     # cleared when it returned

    assert client.get("/_raises").status_code == 500
    assert probe.oldest_inflight() is None     # cleared when it raised

    # `/healthz` itself never counts, or the daemon's 5s poll would be the oldest entry
    client.get("/healthz")
    assert probe.oldest_inflight() is None


def test_healthz_publishes_no_version_and_no_filesystem_path(started):
    """`/healthz` is unauthenticated: it may not hand out a build version or a path"""
    body = TestClient(create_app()).get("/healthz").json()

    assert "version" not in body
    assert "stack_dump" not in body
    blob = json.dumps(body)
    assert "/" not in blob
    assert str(uilog.stack_dump_path()) not in blob


# -- the daemon detects it and heals it


class _FakeRunner:
    """The `release_runner` seam, recording"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def restart_unit(self, unit: str) -> None:
        self.calls.append(("restart_unit", unit))

    def restart_unit_detached(self, unit: str, tag: str) -> None:
        self.calls.append(("restart_unit_detached", unit, tag))


WEDGED = {
    "pool_healthy": False, "wedged": True, "wedged_since": 1000.0,
    "consecutive_failures": 3, "stack_dump": "/tmp/state/logs/ui-stacks.log",
    "limiter": {"borrowed": 40, "available": 0, "total": 40, "waiting": 7},
}
HEALTHY = {
    "pool_healthy": True, "wedged": False, "wedged_since": None,
    "consecutive_failures": 0, "stack_dump": None,
    "limiter": {"borrowed": 0, "available": 40, "total": 40, "waiting": 0},
}


@pytest.fixture()
def ui_unit(monkeypatch):
    """`jarvis-ui.service` as its init system sees it — `active`, uptime unknown, unless a test says otherwise"""
    state = {"active": True, "uptime": None, "transitioning": False}
    monkeypatch.setattr(service_manager, "unit_active", lambda unit: state["active"])
    monkeypatch.setattr(service_manager, "uptime_seconds", lambda unit: state["uptime"])
    monkeypatch.setattr(service_manager, "unit_transitioning",
                        lambda unit: state["transitioning"])
    return state


@pytest.fixture()
def wedged(started, catalog_file, monkeypatch, ui_unit):
    """A daemon whose dashboard answers `/healthz` saying it is wedged"""
    answers = [HEALTHY]
    monkeypatch.setattr(daemon_mod, "fetch_healthz",
                        lambda url, timeout: answers[0])
    d = Daemon(load_catalog(catalog_file))
    d.release_runner = _FakeRunner()
    d.check_ui_health(now=0.0)          # one healthy sighting first
    answers[0] = WEDGED
    return d, d.release_runner, answers


def _inbox() -> list[dict]:
    central = CentralStore()
    try:
        return central.unacked_inbox()
    finally:
        central.close()


def test_a_dashboard_that_was_never_up_is_not_restarted(started, catalog_file,
                                                        monkeypatch, ui_unit):
    """A connection failure with no healthy sighting ever is "nobody started the dashboard", not a wedge"""
    monkeypatch.setattr(daemon_mod, "fetch_healthz", lambda url, timeout: None)
    d = Daemon(load_catalog(catalog_file))
    d.release_runner = _FakeRunner()

    assert d.check_ui_health(now=0.0) == "unseen"
    assert d.release_runner.calls == []
    assert _inbox() == []


def test_the_daemon_restarts_the_wedged_dashboard_once_inside_the_cooldown(wedged):
    d, runner, _ = wedged

    assert d.check_ui_health(now=1000.0) == "restarted"
    assert d.check_ui_health(now=1010.0) == "cooldown"
    assert d.check_ui_health(now=1299.0) == "cooldown"
    assert runner.calls == [("restart_unit", release.UI_UNIT)]

    assert d.check_ui_health(now=1301.0) == "restarted"
    assert runner.calls == [("restart_unit", release.UI_UNIT),
                            ("restart_unit", release.UI_UNIT)]

    # Every restart raises an inbox item naming the dump — no silent restarts, ever
    items = _inbox()
    assert len(items) == 2
    assert "wedged" in items[0]["title"]
    assert WEDGED["stack_dump"] in items[0]["body"]
    assert "40" in items[0]["body"]          # the limiter figures


def test_a_timeout_and_a_wedged_answer_are_the_same_verdict(wedged, monkeypatch):
    """A wedged process answers nothing at all if the event loop went too"""
    d, runner, _ = wedged
    monkeypatch.setattr(daemon_mod, "fetch_healthz", lambda url, timeout: None)

    assert d.check_ui_health(now=1000.0) == "restarted"
    assert runner.calls == [("restart_unit", release.UI_UNIT)]


def test_a_release_restart_of_the_dashboard_is_not_a_wedge(wedged, ui_unit):
    """Every release restarts `jarvis-ui`; the next tick probes before uvicorn binds (issue #1189)"""
    d, runner, answers = wedged
    answers[0] = None                     # the port is not open yet
    ui_unit["uptime"] = 0.07              # systemd restarted it 70ms ago

    assert d.check_ui_health(now=1000.0) == "booting"
    assert runner.calls == []
    assert _inbox() == []
    # Nothing was counted, so the restart cap is intact for a real wedge
    assert d._ui_restarts() == []

    # Past the grace period an unreachable dashboard IS the wedge it always was
    ui_unit["uptime"] = 61.0
    assert d.check_ui_health(now=1100.0) == "restarted"
    assert runner.calls == [("restart_unit", release.UI_UNIT)]


def test_the_stop_phase_of_a_restart_is_not_a_wedge(wedged, ui_unit):
    """While the unit is `deactivating`, uptime still holds the PREVIOUS start (issue #1194)"""
    d, runner, answers = wedged
    answers[0] = None                     # the port is already refusing
    ui_unit["transitioning"] = True
    ui_unit["uptime"] = 180.0             # the start 3 minutes ago, not the new one

    # The cap is spent, so without the state check this is the false CRITICAL
    d.central.set_state(daemon_mod.UI_WEDGE_RESTARTS,
                        db.to_json([995.0, 996.0, 997.0]))

    assert d.check_ui_health(now=1000.0) == "restarting"
    assert runner.calls == []
    assert _inbox() == []
    assert d._ui_restarts() == [995.0, 996.0, 997.0]   # nothing counted


def test_a_dashboard_that_answers_wedged_while_booting_is_still_a_wedge(wedged, ui_unit):
    """The grace period covers silence, not the dashboard's own `wedged: true`"""
    d, runner, _ = wedged
    ui_unit["uptime"] = 0.07

    assert d.check_ui_health(now=1000.0) == "restarted"
    assert runner.calls == [("restart_unit", release.UI_UNIT)]


def test_the_item_claims_no_stack_dump_when_none_was_written(wedged, monkeypatch):
    """`uilog.stack_dump_path()` is a path, not evidence that the dashboard dumped (issue #1189)"""
    d, _, answers = wedged
    answers[0] = None                     # no payload, so no `stack_dump` either

    assert d.check_ui_health(now=1000.0) == "restarted"
    body = _inbox()[0]["body"]
    assert str(uilog.stack_dump_path()) not in body
    assert "Stacks: none — no stack dump was written" in body
    assert "wrote NO stack dump" in body
    assert "Stamp:" not in body           # the dashboard never stamped either


def test_the_daily_cap_stops_restarting_and_still_reaches_the_user(wedged):
    d, runner, _ = wedged
    t = 1000.0
    for i in range(3):                       # `max_restarts_per_day`
        assert d.check_ui_health(now=t + i * 400) == "restarted"
    assert len(runner.calls) == 3

    assert d.check_ui_health(now=t + 2000) == "capped"
    assert len(runner.calls) == 3            # no further restart
    critical = [i for i in _inbox() if i["level"] == "critical"]
    assert len(critical) == 1
    assert "3" in critical[0]["body"]        # the cap that is spent
    assert WEDGED["stack_dump"] in critical[0]["body"]

    # It must not stop watching and must not fall silent
    assert d.check_ui_health(now=t + 2400) == "capped"
    assert len([i for i in _inbox() if i["level"] == "critical"]) == 1

    # …and restarting resumes once the 24h window rolls
    assert d.check_ui_health(now=t + 90000) == "restarted"
    assert len(runner.calls) == 4


def test_a_deliberately_stopped_dashboard_is_not_a_wedge(wedged, ui_unit):
    """`systemctl --user stop jarvis-ui` leaves the port refusing connections"""
    d, runner, answers = wedged
    answers[0] = None                     # nothing is listening
    ui_unit["active"] = False

    assert d.check_ui_health(now=1000.0) == "down"
    assert runner.calls == []
    assert _inbox() == []

    # The healthy sighting is left alone: the dashboard DID serve
    ui_unit["active"] = True
    answers[0] = WEDGED
    assert d.check_ui_health(now=1001.0) == "restarted"


def test_a_failed_restart_says_so_once_and_still_spends_the_cooldown(wedged,
                                                                    monkeypatch):
    """The other order — announce, then restart"""
    d, runner, _ = wedged

    def boom(unit: str) -> None:
        runner.calls.append(("restart_unit", unit))
        raise OSError("dbus: connection refused")

    monkeypatch.setattr(runner, "restart_unit", boom)

    assert d.check_ui_health(now=1000.0) == "restart_failed"
    items = _inbox()
    assert len(items) == 1
    assert items[0]["level"] == "critical"
    assert "could not be restarted" in items[0]["title"]
    assert "dbus: connection refused" in items[0]["body"]
    assert WEDGED["stack_dump"] in items[0]["body"]
    # The one instruction the user acts on by hand names the command THIS host has.
    assert f"systemctl --user restart {release.UI_UNIT}" in items[0]["body"]

    # The attempt IS recorded, so the cooldown holds and nothing is said again
    assert d.check_ui_health(now=1010.0) == "cooldown"
    assert len(_inbox()) == 1
    assert len(runner.calls) == 1


@pytest.fixture()
def wedged_on_a_mac(started, catalog_file, monkeypatch, fake_launchd):
    """The same wedge, on the host this defect was reported from — no `systemctl`
    anywhere, `com.jarvis.ui` answered by the stub."""
    answers = [HEALTHY]
    monkeypatch.setattr(daemon_mod, "fetch_healthz", lambda url, timeout: answers[0])
    fake_launchd.load("com.jarvis.ui", state="running", pid=73299)
    d = Daemon(load_catalog(catalog_file))
    d.release_runner = _FakeRunner()
    d.check_ui_health(now=0.0)              # one healthy sighting first
    answers[0] = WEDGED
    return d, d.release_runner, fake_launchd


def test_under_launchd_a_running_label_plus_a_wedge_is_restarted(wedged_on_a_mac):
    """THE DEFECT, end to end: `systemd_units.unit_active` swallowed the missing-binary
    `OSError`, returned "" and made every wedge read `down` — so the whole of the
    2026-10-08 self-heal spec was dead code on this host."""
    d, runner, _ = wedged_on_a_mac

    assert d.check_ui_health(now=1000.0) == "restarted"
    assert runner.calls == [("restart_unit", release.UI_UNIT)]


def test_under_launchd_an_absent_label_is_down_and_nothing_is_restarted(
        wedged_on_a_mac):
    """`launchctl bootout gui/$UID/com.jarvis.ui` makes the label unfindable, which is
    the deliberately-stopped case `down` exists for."""
    d, runner, fake_launchd = wedged_on_a_mac
    fake_launchd.bootout("com.jarvis.ui")

    assert d.check_ui_health(now=1000.0) == "down"
    assert runner.calls == []
    assert _inbox() == []


def test_under_launchd_a_failed_restart_names_the_launchctl_command(wedged_on_a_mac,
                                                                    monkeypatch):
    """`systemctl --user restart jarvis-ui.service` is not a command this host has."""
    d, runner, _ = wedged_on_a_mac
    monkeypatch.setattr(runner, "restart_unit",
                        lambda unit: (_ for _ in ()).throw(OSError("no launchctl")))

    assert d.check_ui_health(now=1000.0) == "restart_failed"
    body = _inbox()[0]["body"]
    assert f"launchctl kickstart -k gui/{os.getuid()}/com.jarvis.ui" in body
    assert "systemctl" not in body


def test_the_ui_health_check_is_off_when_the_catalog_says_so(started, catalog_file,
                                                             monkeypatch):
    monkeypatch.setattr(daemon_mod, "fetch_healthz", lambda url, timeout: WEDGED)
    cat = load_catalog(catalog_file)
    cat.os.ui_health.enabled = False
    d = Daemon(cat)
    d.release_runner = _FakeRunner()

    assert d.check_ui_health(now=1000.0) == "off"
    assert d.release_runner.calls == []


def test_the_ui_health_check_never_stalls_the_tick(started, catalog_file, monkeypatch,
                                                   ui_unit):
    """Same discipline as `check_ui_log`: the UI watch is the least important thing in the tick"""
    monkeypatch.setattr(daemon_mod, "fetch_healthz",
                        lambda url, timeout: (_ for _ in ()).throw(OSError("no net")))
    Daemon(load_catalog(catalog_file)).tick()      # must not raise


def test_doctor_reports_the_wedge_and_the_self_restarts(wedged):
    """`jarvis doctor` reports, the daemon heals — the OS_INVARIANTS rule"""
    d, _, _ = wedged
    uilog.record_wedge(limiter=WEDGED["limiter"], uptime_seconds=14400.0,
                       version="0.10.0")
    assert d.check_ui_health(now=db.now()) == "restarted"

    found = [v for v in invariants.check_os() if v.invariant == "INV-UI-WEDGED"]
    assert len(found) == 1
    assert str(uilog.stack_dump_path()) in found[0].detail
    assert found[0].context["restarts"] == 1
    assert found[0].context["cap_spent"] is False
    assert [v["invariant"] for v in ops.run_doctor()["os"]] == ["INV-UI-WEDGED"]
    assert not ops.run_doctor()["os"][0]["repaired"]


def test_doctor_stops_reporting_a_wedge_a_day_old(started):
    """The restarted dashboard is a NEW process and never comes back to delete the stamp"""
    uilog.record_wedge(limiter=WEDGED["limiter"], uptime_seconds=1.0, version="0.10.0")
    stamp = uilog.read_wedge()
    old = db.now() - uilog.ERROR_WINDOW_SECONDS - 60
    uilog.wedge_stamp_path().write_text(
        json.dumps({**stamp, "since": old, "at": old}))

    assert [v for v in invariants.check_os() if v.invariant == "INV-UI-WEDGED"] == []
