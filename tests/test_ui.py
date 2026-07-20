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


def test_waiting_input_wo_shows_attach_hint_and_resume(client, daemon, project):
    """A worker blocked on a permission prompt can't be approved from the web UI
    (bg sessions take no programmatic approval), so the page surfaces the native
    `claude attach <session-id>` escape hatch plus a resume-in-auto action."""
    wo = ops.create_work_order("proj_a", "blocked task")
    daemon.tick()
    store = ProjectStore(project)
    store.update_work_order(wo["id"], session_id="sess-abc123")
    store.set_status(wo["id"], "waiting_input")
    store.flag_attention(wo["id"], "Claude needs your permission")

    detail = client.get(f"/wo/proj_a/{wo['id']}")
    assert detail.status_code == 200
    assert "claude attach sess-abc123" in detail.text
    assert f"/wo/proj_a/{wo['id']}/resume-auto" in detail.text

    r = client.post(f"/wo/proj_a/{wo['id']}/resume-auto")
    assert r.status_code == 303
    fresh = store.get_work_order(wo["id"])
    assert fresh["permission_mode"] == "auto"
    assert fresh["needs_attention"] == 0


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


def test_neo_tab_review_flow(client, daemon, project):
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "CSV or JSON?")
    r = client.get("/neo")
    assert r.status_code == 200  # queued question renders in counts
    assert "1 queued" in r.text

    daemon._neo_drain()
    r = client.get("/neo")
    assert "CSV or JSON?" in r.text
    assert "neo-decision" in r.text
    assert "nav-badge" in r.text  # unreviewed answer badges the tab

    # correct the answer from the UI → learning recorded
    r = client.post("/neo/1/review", data={"decision": "correct",
                                           "feedback": "CSV. Always CSV."})
    assert r.status_code == 303
    from jarvis.neo_store import NeoStore
    neo = NeoStore()
    try:
        assert neo.get(1)["review_status"] == "corrected"
        assert any("Always CSV" in l["content"] for l in neo.learnings("proj_a"))
    finally:
        neo.close()
    page = client.get("/neo")
    assert "corrected" in page.text
    assert "Always CSV" in page.text


def test_neo_tab_escalation_answer_flow(client, daemon, project):
    wo = ops.create_work_order("proj_a", "prod thing")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_ESCALATE: touch prod?")
    daemon._neo_drain()
    r = client.get("/neo")
    assert "Escalated" in r.text and "touch prod?" in r.text
    r = client.post("/neo/1/answer", data={"text": "No. Wait for the window."})
    assert r.status_code == 303
    store = ProjectStore(project)
    try:
        contents = [m["content"] for m in store.queued_messages(wo["id"])]
        assert any("Wait for the window" in c for c in contents)
    finally:
        store.close()


def test_neo_teach_directly(client):
    r = client.post("/neo/learn", data={"content": "prefer uv over pip", "project": ""})
    assert r.status_code == 303
    page = client.get("/neo")
    assert "prefer uv over pip" in page.text


def test_timeline_hides_plumbing_until_debug_is_requested(client, daemon, project):
    """The default timeline reads as a story; delivery receipts and session hooks
    only appear behind the debug toggle."""
    wo = ops.create_work_order("proj_a", "export citations",
                               description="BibTeX drops DOIs")
    daemon.tick()
    store = ProjectStore(project)
    store.add_event(wo["id"], "turn_ended")
    store.add_event(wo["id"], "hook:Stop", {"session_id": "a768", "cwd": "/x"})
    store.add_event(wo["id"], "message_delivered", {"msg_id": 1, "via": "bg-resume"})
    store.queue_message(wo["id"], "also cover EndNote", source="ui")
    ops.finish(wo["id"], "exporter fixed")

    plain = client.get(f"/wo/proj_a/{wo['id']}")
    assert plain.status_code == 200
    assert "Work order created" in plain.text
    assert "Finished" in plain.text and "exporter fixed" in plain.text
    assert "also cover EndNote" in plain.text
    for noise in ("turn_ended", "hook:Stop", "message_delivered", "bg-resume"):
        assert noise not in plain.text
    assert "Show debug logs" in plain.text

    debug = client.get(f"/wo/proj_a/{wo['id']}?debug=1")
    assert debug.status_code == 200
    for noise in ("turn_ended", "hook:Stop", "message_delivered"):
        assert noise in debug.text
    assert "Hide debug logs" in debug.text
