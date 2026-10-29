"""Quiescing a project: report its open orders, and with `apply` stop them through the OS's own paths"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def test_report_writes_nothing_and_apply_stops_everything(started, fake_claude, project):
    fake_claude.hold_turns()
    wo = ops.create_work_order("proj_a", "running task")
    started.tick()
    fo = ops.create_feature_order("proj_a", "big feature", "split it up")
    central = CentralStore()
    bl = central.add_backlog("proj_a", "someday")
    central.add_inbox("proj_a", "look at me")
    other = central.add_inbox("proj_b", "not offboarded")
    central.close()

    report = ops.quiesce_project("proj_a")

    assert {(o["id"], o["kind"]) for o in report["open"]} >= {
        (wo["id"], "work_order"), (fo["id"], "feature")}
    assert report["cancelled"] == [] and report["inbox_acked"] == 0
    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["status"] in ("running", "pending")

    out = ops.quiesce_project("proj_a", apply=True)

    assert wo["id"] in out["cancelled"] and fo["id"] in out["cancelled"]
    assert out["sessions_stopped"] >= 1
    assert out["backlog_dropped"] == 1 and out["inbox_acked"] == 1
    assert store.get_work_order(wo["id"])["status"] == "cancelled"
    assert store.get_feature_order(fo["id"])["status"] == "cancelled"
    assert ops.OFFBOARDED_EVENT in [e["kind"] for e in store.list_events(wo["id"])]
    central = CentralStore()
    assert central.get_backlog(bl["id"])["status"] == "dropped"
    assert central.get_inbox(other)["status"] != "acked"
    assert ops.quiesce_project("proj_a")["open"] == []
