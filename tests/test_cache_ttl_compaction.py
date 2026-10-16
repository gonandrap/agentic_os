"""The compact-before-relaunch clock runs on the TTL the project's leads buy. Under the
hour a conversation six minutes cold is still WARM — compacting it would pay the summary
for nothing — and only one past the hour is cold. Findings:
docs/superpowers/findings/2026-10-11-the-lead-buys-the-hour-the-crew-does-not.md
"""

from __future__ import annotations

import json

import pytest

from jarvis import claude_cli, ops, usage, worker_session
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

BIG = 100_000


@pytest.fixture()
def fleet(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    daemon = Daemon(catalog)
    daemon.catalog.os.compact_min_context = BIG
    return {"daemon": daemon, "project": catalog.projects[0],
            "store": ProjectStore(project), "catalog": catalog}


def _cold_big_wo(fleet, settle_turns, seconds: float) -> dict:
    wo = ops.create_work_order("proj_a", "task")
    store, project = fleet["store"], fleet["project"]
    wo = store.get_work_order(wo["id"])
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)
    turn = store.latest_turn(wo["id"])
    store.conn.execute(
        "UPDATE wo_turns SET ended_at=?, usage_json=? WHERE id=?",
        (turn["ended_at"] - seconds,
         json.dumps({"context_peak": BIG * 10,
                     "usage_v": claude_cli.USAGE_SCHEMA_VERSION}),
         turn["id"]))
    return store.get_work_order(wo["id"])


def test_six_minutes_cold_is_still_warm_under_the_hour(fleet, settle_turns):
    assert fleet["project"].worker.cache_ttl == "1h"
    wo = _cold_big_wo(fleet, settle_turns, usage.WRITE_TTL_SECONDS + 60)
    assert worker_session.compact_before_relaunch(
        fleet["store"], fleet["project"], wo, BIG, queue="next") is None


def test_past_the_hour_is_cold_under_the_hour(fleet, settle_turns):
    wo = _cold_big_wo(fleet, settle_turns, 3600 + 60)
    assert worker_session.compact_before_relaunch(
        fleet["store"], fleet["project"], wo, BIG, queue="next") is not None


def test_the_predicate_takes_the_ttl_it_is_judged_against(fleet, settle_turns):
    wo = _cold_big_wo(fleet, settle_turns, usage.WRITE_TTL_SECONDS + 60)
    store = fleet["store"]
    assert worker_session.compaction_due(store, wo, BIG, ttl_seconds=300) is not None
    assert worker_session.compaction_due(store, wo, BIG, ttl_seconds=3600) is None
