"""The Yours page and seen state (docs/superpowers/specs/2026-11-09-yours-requestor-stamp-and-tree-notice.md §6)"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore, db
from jarvis.testing import make_git_project

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis.ui.app import create_app  # noqa: E402


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


def settle(store, root_id, *, ts=None):
    """§4's event, written by hand: the notice is a sibling's"""
    store.add_event(root_id, "request_settled", {"root_id": root_id, "statuses": {}},
                    ts=ts)


def test_each_state_on_a_fixture_tree(store):
    fo = store.create_feature_order("big ask", requestor="user")
    kid = store.create_work_order("child", parent_id=fo["id"], status="running")
    assert ops.request_state("proj_a", fo["id"]) == "running"

    store.update_work_order(kid["id"], needs_attention=1)
    assert ops.request_state("proj_a", fo["id"]) == "needs_you"

    store.update_work_order(kid["id"], needs_attention=0)
    store.set_status(kid["id"], "completed")
    store.set_feature_status(fo["id"], "completed")
    # A feature row holds no events: the planner carries it, matched by root_id
    planner = store.create_work_order("plan", kind="planner", parent_id=fo["id"],
                                      status="completed")
    settle_on = planner["id"]
    store.add_event(settle_on, "request_settled", {"root_id": fo["id"]})
    assert ops.request_state("proj_a", fo["id"]) == "done_unseen"

    ops.mark_request_seen("proj_a", fo["id"])
    assert ops.request_state("proj_a", fo["id"]) == "done"

    # A later settle (the tree reopened and closed) is unseen again
    store.add_event(settle_on, "request_settled", {"root_id": fo["id"]}, ts=db.now() + 60)
    assert ops.request_state("proj_a", fo["id"]) == "done_unseen"


def test_a_pending_user_assumption_or_owed_action_needs_you(store):
    wo = store.create_work_order("fix it", requestor="user", status="running")
    store.add_assumption(wo["id"], "picked X over Y")
    assert ops.request_state("proj_a", wo["id"]) == "needs_you"

    owed = store.create_work_order("deploy", requestor="user", status="needs_review")
    store.add_event(owed["id"], "finished", {"needs_user": "jarvis wo done x"})
    assert ops.request_state("proj_a", owed["id"]) == "needs_you"
    # Carried out and closed: the old declaration owes nothing now
    store.set_status(owed["id"], "completed")
    assert ops.request_state("proj_a", owed["id"]) == "done_unseen"


def test_mark_seen_moves_only_a_user_row(store):
    mine = store.create_work_order("mine", requestor="user")
    theirs = store.create_work_order("theirs", requestor="os")
    for wo in (mine, theirs):
        ops.mark_request_seen("proj_a", wo["id"])
    assert store.get_work_order(mine["id"])["requestor_seen_at"] is not None
    assert store.get_work_order(theirs["id"])["requestor_seen_at"] is None


def test_wo_ack_marks_a_settled_root_seen(store):
    wo = store.create_work_order("fix it", requestor="user", status="completed")
    store.update_work_order(wo["id"], needs_attention=1, attention_reason="done")
    settle(store, wo["id"])
    assert ops.request_state("proj_a", wo["id"]) == "needs_you"
    ops.ack_attention(wo["id"])
    assert ops.request_state("proj_a", wo["id"]) == "done"


def test_viewing_a_settled_root_marks_it_seen(store):
    wo = store.create_work_order("fix it", requestor="user", status="completed")
    settle(store, wo["id"])
    assert ops.request_state("proj_a", wo["id"]) == "done_unseen"
    client = TestClient(create_app(), follow_redirects=False)
    assert client.get(f"/wo/proj_a/{wo['id']}").status_code == 200
    assert ops.request_state("proj_a", wo["id"]) == "done"

    fo = store.create_feature_order("big", requestor="user")
    store.set_feature_status(fo["id"], "completed")
    assert ops.request_state("proj_a", fo["id"]) == "done_unseen"
    assert client.get(f"/fo/proj_a/{fo['id']}").status_code == 200
    assert ops.request_state("proj_a", fo["id"]) == "done"


def test_page_renders_each_state_and_a_cross_project_child(store, proj_b, monkeypatch):
    monkeypatch.setattr(ops, "_unit_row", lambda *a, **k: {"total_cost_usd": 1.25})
    need = store.create_work_order("ask-need", requestor="user", status="running")
    store.update_work_order(need["id"], needs_attention=1)
    store.create_work_order("ask-run", requestor="user", status="running")
    unseen = store.create_work_order("ask-unseen", requestor="user", status="completed")
    settle(store, unseen["id"])
    done = store.create_work_order("ask-done", requestor="user", status="completed")
    ops.mark_request_seen("proj_a", done["id"])
    store.create_work_order("os-own", requestor="os", status="running")
    # A cross-project finding order under a user io
    far = proj_b.create_work_order("far-child", status="running")
    io = store.create_feature_order("ask-io", kind="improvement", requestor="user")
    report = {"findings": [{"key": "f1", "created_orders": [
        {"id": far["id"], "project": "proj_b"}]}]}
    store.update_feature_order(io["id"], plan=db.to_json(report))
    store.set_feature_status(io["id"], "completed")

    text = TestClient(create_app()).get("/yours").text
    for state in ("needs_you", "running", "done_unseen", "done"):
        assert f'id="state-{state}"' in text
    for title in ("ask-need", "ask-run", "ask-unseen", "ask-done", "ask-io", "far-child"):
        assert title in text
    assert "os-own" not in text
    assert f'href="/wo/proj_b/{far["id"]}"' in text
    assert f'id="{unseen["id"]}"' in text and "$1.25" in text
    # Rows land in their state's section
    assert text.index("ask-need") < text.index('id="state-running"') < text.index("ask-run")
    assert text.index('id="state-done_unseen"') < text.index("ask-unseen") \
        < text.index('id="state-done"') < text.index("ask-done")


def test_old_done_roots_hide_behind_show_older(store):
    # The real cost path: no transcript reads as $0.00, never an error
    wo = store.create_work_order("ancient-ask", requestor="user", status="completed")
    ops.mark_request_seen("proj_a", wo["id"])
    store.conn.execute("UPDATE work_orders SET updated_at=? WHERE id=?",
                       (db.now() - 30 * 86400, wo["id"]))
    client = TestClient(create_app())
    page = client.get("/yours").text
    assert "ancient-ask" not in page and "show older" in page
    assert "ancient-ask" in client.get("/yours?older=1").text
