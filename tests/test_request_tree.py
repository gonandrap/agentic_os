"""User roots and the request-tree walk (docs/superpowers/specs/2026-11-09-yours-requestor-stamp-and-tree-notice.md §3)"""

from __future__ import annotations

import json

import pytest

from jarvis import ops
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore, db
from jarvis.testing import make_git_project


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)


@pytest.fixture()
def store(started, project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def proj_b(started, tmp_path):
    path = make_git_project(tmp_path, "proj_b")
    central = CentralStore()
    try:
        central.upsert_project("proj_b", str(path), "the other project")
        central.conn.commit()
    finally:
        central.close()
    s = ProjectStore(path)
    yield s
    s.close()


def ids(tree):
    return [n.id for n in tree.nodes()]


def test_a_plain_user_wo_is_a_one_node_settled_tree(store):
    wo = store.create_work_order("fix it", requestor="user", status="completed")
    tree = ops.request_tree("proj_a", wo["id"])
    assert ids(tree) == [wo["id"]] and tree.settled
    assert tree.root.terminal and tree.root.children == []


def test_waiting_pr_merge_is_not_terminal(store):
    wo = store.create_work_order("fix it", requestor="user", status="waiting_pr_merge")
    tree = ops.request_tree("proj_a", wo["id"])
    assert not tree.root.terminal and not tree.settled


def test_a_user_fo_settles_only_when_planner_and_every_child_is_terminal(store):
    fo = store.create_feature_order("big ask", requestor="user")
    planner = store.create_work_order("plan", kind="planner", parent_id=fo["id"],
                                      status="completed")
    a = store.create_work_order("a", parent_id=fo["id"], status="completed")
    b = store.create_work_order("b", parent_id=fo["id"], status="running")
    store.set_feature_status(fo["id"], "completed")
    tree = ops.request_tree("proj_a", fo["id"])
    assert ids(tree) == [fo["id"], planner["id"], a["id"], b["id"]]
    assert tree.root.kind == "feature" and not tree.settled
    store.set_status(b["id"], "failed")
    assert ops.request_tree("proj_a", fo["id"]).settled  # failed is terminal


def _io_with_findings(store, created_orders):
    io = store.create_feature_order("slow cache", kind="improvement", requestor="user")
    report = {"findings": [{"key": "f1", "created_orders": created_orders}]}
    store.update_feature_order(io["id"], plan=db.to_json(report))
    store.set_feature_status(io["id"], "completed")
    return io


def test_a_user_io_follows_finding_orders_into_another_project(store, proj_b):
    far = proj_b.create_work_order("fix there", metadata={ops.ORIGIN_IO_KEY: "x"},
                                   requestor="user", status="running")
    io = _io_with_findings(store, [{"index": 0, "id": far["id"], "type": "work",
                                    "title": "fix there", "project": "proj_b"}])
    tree = ops.request_tree("proj_a", io["id"])
    [child] = tree.root.children
    assert (child.id, child.project, child.status) == (far["id"], "proj_b", "running")
    assert not tree.settled
    proj_b.set_status(far["id"], "completed")
    assert ops.request_tree("proj_a", io["id"]).settled


def test_a_finding_order_is_found_by_origin_io_when_unrecorded(store):
    io = _io_with_findings(store, [])
    filed = store.create_work_order("fix", metadata={ops.ORIGIN_IO_KEY: io["id"]},
                                    requestor="user", status="running")
    tree = ops.request_tree("proj_a", io["id"])
    assert ids(tree) == [io["id"], filed["id"]] and not tree.settled


def test_a_missing_child_is_terminal_and_marked_missing(store):
    io = _io_with_findings(store, [{"index": 0, "id": "wo-gone0000", "type": "work",
                                    "project": "proj_a"},
                                   {"index": 1, "id": "wo-gone0001", "type": "work",
                                    "project": "unregistered"}])
    tree = ops.request_tree("proj_a", io["id"])
    assert [(n.id, n.status, n.terminal) for n in tree.root.children] == [
        ("wo-gone0000", "missing", True), ("wo-gone0001", "missing", True)]
    assert tree.settled


def test_a_cycle_terminates(store):
    # parent_id has a FK, so a loop can only come through findings' recorded orders
    a = _io_with_findings(store, [])
    b = _io_with_findings(store, [{"id": a["id"], "type": "feature", "project": "proj_a"}])
    store.update_feature_order(a["id"], plan=db.to_json({"findings": [{"created_orders": [
        {"id": b["id"], "type": "feature", "project": "proj_a"}]}]}))
    assert ids(ops.request_tree("proj_a", a["id"])) == [a["id"], b["id"]]


def test_an_unknown_root_is_refused(store):
    with pytest.raises(ops.OpsError, match="wo-nothere0"):
        ops.request_tree("proj_a", "wo-nothere0")


REPORT = {"proposed_orders": [
    {"key": "p1", "as": "wo", "project": "", "title": "migrate", "description": "move"},
    {"key": "p2", "as": "wo", "project": "proj_b", "title": "there", "description": "far"},
]}


def test_a_user_do_follow_up_is_in_the_do_tree_and_not_a_root(store, proj_b):
    do = ops.create_decision_order("proj_a", "which cache", "compare the two caches")
    researcher = store.create_work_order("research", kind="researcher", parent_id=do["id"],
                                         status="completed")
    store.update_feature_order(do["id"], plan_wo_id=researcher["id"],
                               plan=db.to_json(REPORT))
    store.set_feature_status(do["id"], "awaiting_decision")
    filed = {f["key"]: f for f in ops.file_decision_followups(do["id"], all_=True)["filed"]}
    marks = [p["filed"][0] for p in
             json.loads(store.get_feature_order(do["id"])["plan"])["proposed_orders"]]
    assert [m["project"] for m in marks] == ["proj_a", "proj_b"]
    tree = ops.request_tree("proj_a", do["id"])
    assert set(ids(tree)) == {do["id"], researcher["id"], filed["p1"]["id"], filed["p2"]["id"]}
    assert len(ids(tree)) == 4  # recorded mark and origin_do fallback do not duplicate
    assert [r["id"] for r in store.user_roots()] == [do["id"]]
    assert proj_b.user_roots() == []


def test_a_legacy_mark_without_project_is_read_in_the_do_project(store):
    wo = store.create_work_order("old follow-up", status="completed")
    report = {"proposed_orders": [{"key": "p1", "as": "wo",
                                   "filed": [{"id": wo["id"], "as": "wo", "at": 1.0}]}]}
    do = store.create_feature_order("old do", kind="decision", requestor="user")
    store.update_feature_order(do["id"], plan=db.to_json(report))
    assert ids(ops.request_tree("proj_a", do["id"])) == [do["id"], wo["id"]]


def test_user_roots_exclude_children_finding_orders_os_and_null_rows(store):
    root_wo = store.create_work_order("mine", requestor="user")
    root_fo = store.create_feature_order("mine too", requestor="user")
    store.create_work_order("child", parent_id=root_fo["id"])  # inherits user
    store.create_work_order("finding", requestor="user",
                            metadata={ops.ORIGIN_IO_KEY: "fo-someio00"})
    store.create_feature_order("follow-up", requestor="user",
                               metadata={ops.ORIGIN_DO_KEY: "fo-somedo00"})
    store.create_work_order("os work", requestor="os")
    store.create_work_order("legacy")  # NULL: never a root, it would flood the inbox
    assert [r["id"] for r in store.user_roots()] == [root_wo["id"], root_fo["id"]]


def test_user_roots_skip_settled_roots_unless_asked(store):
    wo = store.create_work_order("mine", requestor="user")
    fo = store.create_feature_order("mine too", requestor="user")
    planner = store.create_work_order("plan", kind="planner", parent_id=fo["id"])
    store.add_event(wo["id"], "request_settled", {"root_id": wo["id"]})
    # A feature row cannot hold an event (wo_events FK): its payload names the root
    store.add_event(planner["id"], "request_settled", {"root_id": fo["id"]})
    assert store.user_roots() == []
    assert [r["id"] for r in store.user_roots(include_settled=True)] == [wo["id"], fo["id"]]
