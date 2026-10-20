"""A stopped or wedged jarvisd is a liveness gap only a reader outside it can assert.

Spec docs/superpowers/specs/2026-10-16-a-stopped-daemon-is-a-liveness-gap.md.
"""

from __future__ import annotations

import json

import pytest

from jarvis import db, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import (
    DAEMON_DARK_INTERVALS,
    DAEMON_DARK_MIN_SECONDS,
    RECONCILE_EVERY_TICKS,
    RECONCILE_TICK_KEY,
    Daemon,
)
from jarvis.project_store import ProjectStore

#: Well past the window, in seconds.
LONG_DARK = 10 * DAEMON_DARK_MIN_SECONDS

POLL_INTERVAL = 2.0


def _register(catalog_file, *, catalog_path: bool = True) -> None:
    """What `jarvis start` writes to the central store, minus starting anything."""
    data = json.loads(catalog_file.read_text())
    central = CentralStore()
    try:
        if catalog_path:
            central.set_state("catalog_path", str(catalog_file))
        for spec in data["projects"]:
            central.upsert_project(spec["name"], spec["path"], spec.get("description", ""))
    finally:
        central.close()


def _stamp(value: str) -> None:
    central = CentralStore()
    try:
        central.set_state(RECONCILE_TICK_KEY, value)
    finally:
        central.close()


def _started_at(ts: float) -> None:
    central = CentralStore()
    try:
        central.set_state("daemon_started_at", str(ts))
    finally:
        central.close()


def _alive(monkeypatch, pid: int = 4242) -> int:
    monkeypatch.setattr(ops, "daemon_running", lambda: pid)
    return pid


def _daemon_items(catalog_file) -> list[dict]:
    return [a for a in ops.os_status()["attention"] if a["status"] == "daemon"]


# -- the reader ------------------------------------------------------------------------


def test_a_stopped_daemon_is_one_item_naming_jarvis_start(project, catalog_file,
                                                          monkeypatch):
    monkeypatch.setattr(ops, "daemon_running", lambda: None)
    store = ProjectStore(project)
    try:
        for i in range(3):
            store.create_work_order(f"open order {i}")
    finally:
        store.close()
    _register(catalog_file)

    items = _daemon_items(catalog_file)

    assert len(items) == 1
    assert items[0]["cause"] == "stopped"
    assert items[0]["project"] == "os"
    assert items[0]["wo_id"] is None
    assert items[0]["decide"] == f"jarvis start --catalog {catalog_file}"


def test_a_stopped_daemon_with_no_registered_catalog_names_bare_jarvis_start(
        project, catalog_file, monkeypatch):
    monkeypatch.setattr(ops, "daemon_running", lambda: None)
    _register(catalog_file, catalog_path=False)

    assert _daemon_items(catalog_file)[0]["decide"] == "jarvis start"


def test_a_fresh_stamp_says_nothing(project, catalog_file, monkeypatch):
    _alive(monkeypatch)
    _register(catalog_file)
    _stamp(db.to_json({"ts": db.now(), "interval": 30.0}))

    assert _daemon_items(catalog_file) == []


def test_a_stamp_older_than_the_window_is_one_dark_item(project, catalog_file,
                                                        monkeypatch):
    pid = _alive(monkeypatch)
    _register(catalog_file)
    _stamp(db.to_json({"ts": db.now() - LONG_DARK, "interval": 30.0}))

    items = _daemon_items(catalog_file)

    assert len(items) == 1
    assert items[0]["cause"] == "dark"
    assert str(pid) in items[0]["reason"]
    assert "decide" not in items[0]


def test_a_fresh_install_with_no_stamp_and_no_start_says_nothing(project, catalog_file,
                                                                 monkeypatch):
    """invariants.check_stuck_sweep_alive's own rule: never report a fresh install."""
    _alive(monkeypatch)
    _register(catalog_file)

    assert _daemon_items(catalog_file) == []


def test_no_stamp_but_a_long_past_start_is_dark(project, catalog_file, monkeypatch):
    _alive(monkeypatch)
    _register(catalog_file)
    _started_at(db.now() - LONG_DARK)

    items = _daemon_items(catalog_file)

    assert len(items) == 1
    assert items[0]["cause"] == "dark"


@pytest.mark.parametrize("value", ["not json at all", "{}", '{"ts": 0}',
                                   '{"ts": "nope", "interval": "nope"}'])
def test_a_malformed_stamp_behaves_as_no_stamp(project, catalog_file, monkeypatch,
                                               value):
    _alive(monkeypatch)
    _register(catalog_file)
    _stamp(value)

    assert _daemon_items(catalog_file) == []

    _started_at(db.now() - LONG_DARK)
    assert [a["cause"] for a in _daemon_items(catalog_file)] == ["dark"]


def test_the_window_is_the_wider_of_the_floor_and_ten_intervals(project, catalog_file,
                                                                monkeypatch):
    """A slow poll interval widens the window; a fast one keeps the floor."""
    _alive(monkeypatch)
    _register(catalog_file)
    wide = DAEMON_DARK_MIN_SECONDS
    _stamp(db.to_json({"ts": db.now() - (DAEMON_DARK_MIN_SECONDS + 60),
                       "interval": wide}))

    assert _daemon_items(catalog_file) == []


# -- the writer ------------------------------------------------------------------------


def test_a_reconcile_tick_stamps_the_interval_it_was_launched_with(
        project, catalog_file, jarvis_home, fake_claude):
    daemon = Daemon(load_catalog(catalog_file), poll_interval=POLL_INTERVAL)

    daemon.tick()

    central = CentralStore()
    try:
        stamp = db.from_json(central.get_state(RECONCILE_TICK_KEY), None)
    finally:
        central.close()
    assert stamp["interval"] == RECONCILE_EVERY_TICKS * POLL_INTERVAL
    assert float(stamp["ts"]) > 0.0


def test_a_non_reconcile_tick_does_not_advance_the_stamp(project, catalog_file,
                                                         jarvis_home, fake_claude):
    daemon = Daemon(load_catalog(catalog_file), poll_interval=POLL_INTERVAL)
    daemon.tick()
    central = CentralStore()
    try:
        first = central.get_state(RECONCILE_TICK_KEY)

        daemon.tick()

        assert daemon.tick_count == 2
        assert central.get_state(RECONCILE_TICK_KEY) == first
    finally:
        central.close()


def test_the_dark_window_constants(project):
    assert (DAEMON_DARK_INTERVALS, DAEMON_DARK_MIN_SECONDS) == (10, 300)
