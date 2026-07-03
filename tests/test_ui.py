"""Web UI tests: pages render, actions call the same ops as the CLI."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import ops  # noqa: E402
from jarvis.catalog import load_catalog  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.daemon import Daemon  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture()
def daemon(catalog_file):
    return Daemon(load_catalog(catalog_file))


def test_dashboard_renders_quiet(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "all quiet" in r.text
    assert "proj_a" in r.text


def test_create_wo_via_ui_marks_origin(client, project):
    r = client.post("/wo/create", data={"project": "proj_a", "title": "from the ui"})
    assert r.status_code == 303
    store = ProjectStore(project)
    wo = store.list_work_orders()[0]
    assert wo["origin"] == "ui"
    assert wo["title"] == "from the ui"
    # detail page renders with framework badge
    page = client.get(r.headers["location"])
    assert "from the ui" in page.text
    assert "⚙" in page.text


def test_attention_strip_shows_review_items(client, daemon, project):
    wo = ops.create_work_order("proj_a", "risky change")
    daemon.tick()
    ops.assume(wo["id"], "assumed the API is v2")
    ops.finish(wo["id"], "done-ish")

    r = client.get("/")
    assert "NEEDS YOU" in r.text
    assert "assumptions pending review" in r.text

    detail = client.get(f"/wo/proj_a/{wo['id']}")
    assert "assumed the API is v2" in detail.text

    client.post(f"/wo/proj_a/{wo['id']}/review", data={"decision": "accept"})
    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["status"] == "completed"
    assert "all quiet" in client.get("/").text


def test_send_message_via_ui(client, project):
    wo = ops.create_work_order("proj_a", "task")
    client.post(f"/wo/proj_a/{wo['id']}/send", data={"message": "check the docs too"})
    store = ProjectStore(project)
    msgs = store.list_messages(wo["id"])
    assert msgs[0]["content"] == "check the docs too"
    assert msgs[0]["source"] == "ui"
    assert msgs[0]["status"] == "queued"


def test_adhoc_badge_visible(client, daemon, fake_claude, project):
    import json
    sessions = fake_claude.sessions
    sessions.append({"id": "zz", "sessionId": "adhoc-9", "cwd": str(project),
                     "kind": "background", "name": "manual poking",
                     "state": "running", "startedAt": 0})
    (fake_claude.dir / "sessions.json").write_text(json.dumps(sessions))
    daemon.tick_count = 0
    daemon.tick()
    r = client.get("/")
    assert "ad-hoc" in r.text and "⚠" in r.text


def test_inbox_page_and_ack(client, daemon, project):
    store = ProjectStore(project)
    store.add_notification("disk almost full", level="critical")
    daemon.tick()
    r = client.get("/inbox")
    assert "disk almost full" in r.text and "critical" in r.text
    client.post("/inbox/ack", data={})
    assert "inbox empty" in client.get("/inbox").text


def test_backlog_page_promote_blocked_then_forced(client, project):
    central = CentralStore()
    a = central.add_backlog("proj_a", "foundation")
    b = central.add_backlog("proj_a", "tower", depends_on=[a["id"]])

    r = client.get("/backlog")
    assert "blocked by" in r.text and a["id"] in r.text

    r = client.post(f"/backlog/promote/{b['id']}", data={})
    from urllib.parse import unquote
    assert "unfinished dependencies" in unquote(r.headers["location"])

    r = client.post(f"/backlog/promote/{b['id']}", data={"force": "1"})
    assert r.headers["location"].startswith("/wo/proj_a/")
    assert central.get_backlog(b["id"])["status"] == "promoted"


def test_knowledge_page(client):
    central = CentralStore()
    central.add_knowledge("prefer uv for python installs", project="", topic="tooling")
    r = client.get("/knowledge")
    assert "prefer uv" in r.text and "global" in r.text


def test_api_status(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["projects"][0]["name"] == "proj_a"


def test_unknown_project_and_wo(client):
    assert "unknown project" in client.get("/project/nope").text
    assert "not found" in client.get("/wo/proj_a/wo-nope").text
