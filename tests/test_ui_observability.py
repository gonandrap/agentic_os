"""A dashboard failure has to be as visible to the OS as a daemon failure.

The bug behind these tests: the work-order page 500'd, the traceback went to the systemd
journal, and nothing else — not `jarvis status`, not `jarvis doctor`, not the inbox, not
Telegram — knew anything had happened. The user's only signal was clicking a link and
seeing "Internal Server Error". Each test below covers one surface that was blind.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import ops, uilog  # noqa: E402
from jarvis.catalog import load_catalog  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.daemon import Daemon  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return jarvis_home


def _boom(msg: str = "kaboom") -> Exception:
    try:
        raise RuntimeError(msg)
    except RuntimeError as e:
        return e


# -- the daemon tells the user ------------------------------------------------------

def test_daemon_raises_an_inbox_item_for_new_dashboard_errors(started, catalog_file):
    """The daemon is what turns things the OS notices into things the user is told.
    Nothing read `ui.log`, so a broken dashboard reached the user only if they happened
    to be tailing a file."""
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
    """The daemon ticks every five seconds. Re-announcing the same failure each time
    would bury the inbox — and Telegram — under one bug."""
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
    """What 0.5.5 actually did to production. This watcher shipped a release after the
    writer it reads, so the first tick met an existing `ui.log`, found no cursor in
    `os_state`, resumed at byte 0 and sent the user a Telegram alert for four errors
    that had been fixed for a week — a "the dashboard is broken" page about a dashboard
    that was serving HTTP 200. Every fresh install walks into the same trap.
    """
    uilog.ui_log_path().parent.mkdir(parents=True, exist_ok=True)
    old = time.strftime(uilog._STAMP, time.localtime(time.time() - 7 * 24 * 3600))
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

    # And the cursor moved on, so a genuine error after it is still the next thing the
    # user hears about — the history is skipped, not the file.
    uilog.record_error("GET", "/neo", _boom("a real, current failure"))
    assert d.check_ui_log() == 1


def test_the_ui_watch_never_stalls_the_tick(started, catalog_file, monkeypatch):
    """A tick that dies on the log watcher would stop dispatch, delivery and
    reconciliation — the log watcher is the least important thing in it."""
    d = Daemon(load_catalog(catalog_file))
    monkeypatch.setattr(uilog, "read_errors",
                        lambda *_: (_ for _ in ()).throw(OSError("disk gone")))
    d.tick()  # must not raise


# -- `jarvis status` and `jarvis doctor` say so -------------------------------------

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
    # Not repairable: nothing here has a resolution derivable from state.
    assert not res["os"][0]["repaired"]


def test_doctor_scoped_to_one_project_still_reports_the_dashboard(started):
    """`--project` narrows which project's invariants run; the OS's own web UI is not
    one of them, and filtering it out would hide the failure from the exact user who
    scoped their check."""
    uilog.record_error("GET", "/", _boom("still broken"))
    assert ops.run_doctor(project="proj_a")["os"][0]["invariant"] == "INV-UI-HEALTHY"


# -- the dashboard writes what the above reads --------------------------------------

def test_a_500_flows_all_the_way_from_the_browser_to_the_inbox(started, catalog_file,
                                                               monkeypatch):
    """End to end, and the whole point of this work order: a user hits a broken page,
    and without anyone tailing anything the OS ends up holding an inbox item about it.

    `raise_server_exceptions=False` because Starlette re-raises after responding so the
    server can log; a real browser still gets the page.
    """
    client = TestClient(create_app(), follow_redirects=False,
                        raise_server_exceptions=False)
    real_os_status = ops.os_status
    monkeypatch.setattr(ops, "os_status", lambda: 1 / 0)

    assert client.get("/").status_code == 500

    # Restore by hand, NOT monkeypatch.undo(): `undo` reverts *every* patch on the
    # shared monkeypatch instance, including the JARVIS_HOME the `jarvis_home` fixture
    # set — which points the rest of the test at the real fleet's state directory.
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


# -- the access log -----------------------------------------------------------------

def test_requests_land_in_the_access_log(started):
    """"When I click on the link I get an internal server error" was impossible to
    place in time because nothing recorded which links were followed."""
    client = TestClient(create_app(), follow_redirects=False)
    client.get("/")
    client.get("/wo/proj_gone/wo-1")

    lines = uilog.access_log_path().read_text().splitlines()
    assert any("[200] GET /" in ln for ln in lines)
    assert any("[404] GET /wo/proj_gone/wo-1" in ln for ln in lines)


def test_the_dashboards_own_refresh_poll_is_not_logged_unless_it_fails(started,
                                                                      monkeypatch):
    """`/api/status` fires every 15s. Logged, it is ~95% of the file and buries the
    user navigation the access log exists to show."""
    client = TestClient(create_app(), follow_redirects=False,
                        raise_server_exceptions=False)
    client.get("/api/status")
    assert not uilog.access_log_path().exists()

    monkeypatch.setattr(ops, "os_status", lambda: 1 / 0)
    client.get("/api/status")
    assert "[500] GET /api/status" in uilog.access_log_path().read_text()


# -- time in each state (spec 2026-09-27-time-in-each-state §7) ----------------------

@pytest.fixture()
def browser(started):
    return TestClient(create_app(), follow_redirects=False)


def test_the_work_order_page_shows_time_in_state(browser, project):
    """The fourth tab. Diagnostic, so it lives behind a tab and not above them."""
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
    # Two figures, labelled differently: equal numbers are the common case.
    assert "in this status" in page
    assert "nothing on the record for" in page


def test_the_feature_page_labels_a_coarse_span_as_approximate(browser, project):
    """The flag, on the surface: an unlabelled coarse span is the defect Neo's ruling
    names. A feature that predates the table is reproduced by dropping its rows — the
    next open of the store backfills the one approximate span."""
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


# -- a usage-limit hold on the page (spec 2026-09-30 §3) ----------------------------

#: 5h24m in `running`, of which the order was let work 24m — figures deliberately away
#: from a rounding edge, since the render happens a fraction of a second after `now`.
HELD_WALL = 5 * 3600 + 24 * 60
HELD_WORKED = 24 * 60


def _event_at(store: ProjectStore, wo_id: str, kind: str, ts: float,
              payload: dict) -> None:
    store.add_event(wo_id, kind, payload)
    store.conn.execute(
        "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events WHERE wo_id=?)",
        (ts, wo_id))


def _held_order(project, *, resumed: bool):
    """Issue 887 on the page: 45m worked of 5h24m in `running`, the rest held."""
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    now = time.time()
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
    """The common case acquires no noise: no parenthesis, no `held`, no `wall`."""
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
    """`running 24m (5.0h held by a fleet usage limit, 5.4h wall)` — Neo 1130."""
    wo = _held_order(project, resumed=True)

    page = browser.get(f"/wo/proj_a/{wo['id']}").text

    assert '<span class="mono" style="width: 70px; text-align: right;">24m</span>' in page
    assert "(5.0h held by a fleet usage limit, 5.4h wall)" in _text(page)
    # the Gantt line for the held span says how much of it was a hold
    assert "· 5.0h held" in _text(page)


def test_an_open_hold_reads_as_held_on_the_page(browser, project):
    """While the hold is open the current-status line says HELD, not `in this status`."""
    wo = _held_order(project, resumed=False)

    page = browser.get(f"/wo/proj_a/{wo['id']}").text

    assert "HELD 5.0h by a fleet usage limit, running 24m of 5.4h" in _text(page)
    assert "in this status" not in page


def test_the_tab_badge_shows_active_not_wall(browser, project):
    """A badge reading 5.4h on an order that was let work 24m is the issue's headline."""
    wo = _held_order(project, resumed=False)

    page = browser.get(f"/wo/proj_a/{wo['id']}").text

    assert '<span class="n">24m</span>' in page
    assert '<span class="n">5.4h</span>' not in page
