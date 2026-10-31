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
    store = ProjectStore(project)
    parked = store.create_work_order("parked on its PR", description="the ask")
    store.update_work_order(parked["id"], status="waiting_pr_merge")
    stuck = store.create_work_order("stuck one", description="the ask")
    store.update_work_order(stuck["id"], status="validating")
    fo = ops.create_feature_order("proj_a", "big feature", "split it up")
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description="three orders re-read the same files",
                                      refs=("wo-11111111",))
    inv = ops.create_investigation_order("proj_a", stuck["id"], "validating for hours")
    central = CentralStore()
    bl = central.add_backlog("proj_a", "someday")
    mine = central.add_inbox("proj_a", "look at me")
    other = central.add_inbox("proj_b", "not offboarded")
    central.close()
    orders = {wo["id"]: "work_order", parked["id"]: "work_order", fo["id"]: "feature",
              io["id"]: "improvement", inv["id"]: "investigation"}

    report = ops.quiesce_project("proj_a")

    assert {(o["id"], o["kind"]) for o in report["open"]} >= set(orders.items())
    assert report["cancelled"] == [] and report["inbox_acked"] == 0
    assert store.get_work_order(parked["id"])["status"] == "waiting_pr_merge"

    out = ops.quiesce_project("proj_a", apply=True)

    assert set(orders) <= set(out["cancelled"])
    assert out["sessions_stopped"] >= 1
    assert out["backlog_dropped"] == 1 and out["inbox_acked"] == 1
    for wo_id in (wo["id"], parked["id"]):
        assert store.get_work_order(wo_id)["status"] == "cancelled"
        assert ops.OFFBOARDED_EVENT in [e["kind"] for e in store.list_events(wo_id)]
    for fo_id in (fo["id"], io["id"], inv["id"]):
        assert store.get_feature_order(fo_id)["status"] == "cancelled"
    central = CentralStore()
    assert central.get_backlog(bl["id"])["status"] == "dropped"
    assert central.get_inbox(mine)["status"] == "acked"
    assert central.get_inbox(other)["status"] != "acked"
    assert ops.quiesce_project("proj_a")["open"] == []


def test_open_decision_order_refuses_before_any_cancel_inside_a_work_order(
        started, project, monkeypatch):
    wo = ops.create_work_order("proj_a", "open task")
    do = ops.create_decision_order("proj_a", "pick a vendor", "compare prices")
    store = ProjectStore(project)

    monkeypatch.setenv("JARVIS_WO_ID", "wo-deadbeef")
    with pytest.raises(ops.OpsError, match=do["id"]):
        ops.quiesce_project("proj_a", apply=True)
    assert store.get_work_order(wo["id"])["status"] != "cancelled"
    assert store.get_feature_order(do["id"])["status"] != "cancelled"

    monkeypatch.delenv("JARVIS_WO_ID")
    out = ops.quiesce_project("proj_a", apply=True)
    assert {wo["id"], do["id"]} <= set(out["cancelled"])
    assert store.get_work_order(wo["id"])["status"] == "cancelled"
    assert store.get_feature_order(do["id"])["status"] == "cancelled"
