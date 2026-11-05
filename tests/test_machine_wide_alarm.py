"""A machine-wide alarm names no work order (docs/superpowers/specs/2026-11-04-standing-meter-condition.md)"""

from __future__ import annotations

import json
import sqlite3

import pytest

from jarvis import daemon, db, inspection, project_store
from jarvis.project_store import ProjectStore


def test_the_kinds_are_the_ones_the_daemon_raises():
    """`project_store` cannot import either module, so it spells them out"""
    assert set(project_store.MACHINE_WIDE_KINDS) == {
        *daemon.METER_INBOX_TITLE, daemon.METER_DIAGNOSED_ALARM,
        *daemon.CACHE_1H_INBOX_TITLE, inspection.CACHE_1H_FOREIGN_ALARM}


def _as_released(path) -> None:
    """Put a store back to the required `wo_id` every released database has"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=OFF")
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='wo_alarms'").fetchone()["sql"]
    indexes = [r["sql"] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='wo_alarms' AND type='index' "
        "AND sql IS NOT NULL")]
    conn.execute("DROP TABLE wo_alarms")
    conn.execute(sql.replace("wo_id TEXT REFERENCES", "wo_id TEXT NOT NULL REFERENCES", 1))
    for index in indexes:
        conn.execute(index)
    conn.commit()
    conn.close()


@pytest.fixture
def released(tmp_path):
    """A released database holding one row of each shape, and their events"""
    store = ProjectStore(tmp_path)
    carrier = store.create_work_order("an innocent order", status="completed")["id"]
    guilty = store.create_work_order("a slow order", status="completed")["id"]
    store.close()
    _as_released(store.db_path)

    conn = sqlite3.connect(store.db_path)

    def row(alarm_id: str, wo_id: str, kind: str, status: str) -> None:
        conn.execute("INSERT INTO wo_alarms (id, wo_id, ts, kind, seq, reason, status) "
                     "VALUES (?,?,?,?,-1,'why',?)", (alarm_id, wo_id, db.now(), kind, status))
        conn.execute("INSERT INTO wo_events (wo_id, ts, kind, payload) VALUES (?,?,?,?)",
                     (wo_id, db.now(), "cost_alarm",
                      json.dumps({"kind": kind, "seq": -1, "alarm_id": alarm_id})))

    row("al-meter", carrier, "cost_residual_high", "acked")
    row("al-cache", carrier, "cache-arm-missed", "escalated")
    row("al-gated", carrier, "cache-1h-dispatched", "proposed")
    row("al-turn", guilty, "turn-cost", "raised")
    conn.execute("INSERT INTO wo_events (wo_id, ts, kind, payload) VALUES (?,?,?,?)",
                 (carrier, db.now(), "alarm_reviewed", json.dumps({"alarm_id": "al-meter"})))
    conn.commit()
    conn.close()
    return tmp_path, carrier, guilty


def test_opening_a_released_database_takes_machine_wide_rows_off_their_carriers(released):
    path, carrier, guilty = released

    store = ProjectStore(path)
    try:
        assert store.get_alarm("al-meter")["wo_id"] is None
        assert store.get_alarm("al-meter")["status"] == "acked", "the row keeps its verdict"
        assert store.get_alarm("al-cache")["wo_id"] is None
        assert store.get_alarm("al-gated")["wo_id"] == carrier, "its gate's remedy needs it"
        assert store.get_alarm("al-turn")["wo_id"] == guilty

        left = {db.from_json(e["payload"], {})["alarm_id"]
                for e in store.events_of_kind(carrier, "cost_alarm")}
        assert left == {"al-gated"}
        assert store.events_of_kind(carrier, "alarm_reviewed") == []
        assert len(store.events_of_kind(guilty, "cost_alarm")) == 1

        assert {r["id"] for r in store.alarms_across()} == {
            "al-meter", "al-cache", "al-gated", "al-turn"}
        names = {r["name"] for r in store.conn.execute("PRAGMA index_list(wo_alarms)")}
        assert {"idx_alarms_wo", "idx_alarms_status"} <= names
    finally:
        store.close()


def test_a_second_open_changes_nothing_and_no_row_comes_back(released):
    """`_backfill_alarms` rebuilds rows from events: a freed row must not return on its carrier"""
    path, carrier, _ = released
    ProjectStore(path).close()

    store = ProjectStore(path)
    try:
        assert store.get_alarm("al-meter")["wo_id"] is None
        assert [a["id"] for a in store.alarms_of(carrier)] == ["al-gated"]
    finally:
        store.close()


def test_a_deleted_order_takes_no_machine_wide_row_with_it(tmp_path):
    store = ProjectStore(tmp_path)
    try:
        order = store.create_work_order("an order", status="completed")["id"]
        store.add_finding(None, kind="cost_residual_high", reason="why")
        store.delete_work_order(order)
        assert len(store.alarms_across()) == 1
        assert store.search_alarms(["cost_residual_high"])[0]["wo_title"] is None
    finally:
        store.close()
