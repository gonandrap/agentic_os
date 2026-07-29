"""Web UI tests: pages render, actions call the same ops as the CLI."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import gates, ops  # noqa: E402
from jarvis.catalog import load_catalog  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.daemon import Daemon  # noqa: E402
from jarvis.invariants import check_project  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture()
def daemon(catalog_file):
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def gated_catalog(tmp_path, project):
    """A catalog whose only project gates every privileged action."""
    data = {
        "os": {"defaults": {"model": "sonnet"},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project",
                      "gates": {"enabled": list(gates.KIND_NAMES)}}],
    }
    path = tmp_path / "catalog-gated.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def gated(jarvis_home, fake_claude, gated_catalog, project):
    """A started OS with a gated project, a dispatched work order, and a browser."""
    ops.start_os(str(gated_catalog), foreground=True)
    daemon = Daemon(load_catalog(gated_catalog))
    wo = ops.create_work_order("proj_a", "ship version 1.2.3")
    daemon.tick()

    class Handle:
        def __init__(self):
            self.daemon = daemon
            self.wo_id = wo["id"]
            self.project = project
            self.client = TestClient(create_app(), follow_redirects=False)

        def request(self, command="./scripts/shipit.sh", why="please"):
            """File a gate request and return the approval row."""
            ops.request_gate_approval(self.wo_id, command, why=why)
            store = ProjectStore(project)
            try:
                return store.list_approvals(self.wo_id)[0]
            finally:
                store.close()

        def approval(self):
            store = ProjectStore(project)
            try:
                return store.list_approvals(self.wo_id)[0]
            finally:
                store.close()

    return Handle()


def test_dashboard_renders_quiet(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "all quiet" in r.text
    assert "proj_a" in r.text


def test_dashboard_auto_refresh_is_scoped_to_the_live_regions(client):
    """A whole-page refresh wipes a half-typed work order, so the meta tag is
    noscript-only and JS swaps just the live regions."""
    r = client.get("/")
    assert '<noscript><meta http-equiv="refresh"' in r.text
    assert r.text.count('http-equiv="refresh"') == 1
    for region in ('id="live-chrome"', 'id="live-top"', 'id="live-bottom"'):
        assert region in r.text
    # the create form must sit outside the swapped regions
    assert (r.text.index("<!-- /live-top -->")
            < r.text.index('action="/wo/create"')
            < r.text.index('id="live-bottom"'))


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


def test_wo_page_anchors_pending_at_what_needs_the_user(client, daemon):
    """Notifications deep-link to #pending; it must land on the live ask, and the
    page must never emit two of them."""
    wo = ops.create_work_order("proj_a", "risky change")
    daemon.tick()

    quiet = client.get(f"/wo/proj_a/{wo['id']}").text
    assert quiet.count('id="pending"') == 1  # falls back to the reply box
    assert 'id="pending"' in quiet.split("Send to worker")[0].split("<h2>Conversation</h2>")[1]

    ops.assume(wo["id"], "assumed the API is v2")
    ops.finish(wo["id"], "done-ish")
    review = client.get(f"/wo/proj_a/{wo['id']}").text
    assert review.count('id="pending"') == 1
    assert '<h2 id="pending">Assumptions pending your review</h2>' in review


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
                     "state": "working", "startedAt": 0})
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


def test_dashboard_clears_unacked_banner_after_ack(client, daemon, project):
    """Acking every inbox item must clear the "unacked notification" banner on
    the main dashboard, not just the /inbox page."""
    store = ProjectStore(project)
    store.add_notification("disk almost full", level="critical")
    daemon.tick()

    dash = client.get("/").text
    assert "unacked notification" in dash

    client.post("/inbox/ack", data={})

    dash_after = client.get("/").text
    assert "unacked notification" not in dash_after


def test_dashboard_is_never_cached(client):
    """The dashboard state (unacked count, attention items) changes on every
    action, so browsers must always revalidate it — never serve a stale copy
    from disk cache or history (bfcache) after the user navigates back."""
    r = client.get("/")
    assert r.headers.get("cache-control", "") == "no-store"


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


def test_neo_tab_lists_questions_still_with_neo(client, daemon, project):
    """A queued question used to appear only as a number in the counts line, so a
    Neo that had stopped draining was indistinguishable from a Neo with nothing to
    do — the page said "Neo hasn't handled anything yet" while a worker sat parked."""
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "CSV or JSON?")

    r = client.get("/neo")
    assert r.status_code == 200
    assert "1 queued" in r.text
    assert "With Neo right now" in r.text
    assert "CSV or JSON?" in r.text          # the question itself, not just a count
    assert f"/wo/proj_a/{wo['id']}" in r.text  # traceable back to the parked worker


def test_neo_tab_shows_a_question_stuck_mid_answer(client, daemon, project):
    """`claim_next` flips a question to `answering` and only a completed drain moves
    it on; a crash in between strands it, since neo_tick only ever looks at `queued`.
    Stranded is exactly when the user needs to see it."""
    from jarvis.neo_store import NeoStore

    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "CSV or JSON?")
    neo = NeoStore()
    try:
        assert neo.claim_next()["status"] == "answering"  # drain dies right here
    finally:
        neo.close()

    r = client.get("/neo")
    assert "CSV or JSON?" in r.text
    assert "answering" in r.text


def test_neo_tab_review_flow(client, daemon, project):
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "CSV or JSON?")

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


def test_gates_tab_is_empty_and_unbadged_when_nothing_is_gated(client):
    r = client.get("/gates")
    assert r.status_code == 200
    assert "nothing under review" in r.text
    assert "no gate has been decided yet" in r.text
    # an ungated fleet must not grow a nav badge asking for attention it doesn't need
    assert 'href="/gates"' in r.text
    assert '<span class="nav-badge">' not in r.text  # the CSS rule always matches


def test_gates_tab_shows_the_request_the_reviewer_saw(gated):
    """Approving a bare command string is not a review. The page has to carry the
    same case Neo read, or the user is rubber-stamping."""
    approval = gated.request(why="all tests pass, PR is merged")
    gated.daemon._neo_drain()  # the fake model escalates by default

    r = gated.client.get("/gates")
    assert r.status_code == 200
    assert "Waiting on you" in r.text
    assert "./scripts/shipit.sh" in r.text
    assert "all tests pass, PR is merged" in r.text
    assert "the request exactly as the reviewer saw it" in r.text
    # the full request text Neo was given, verbatim
    assert "PRIVILEGED ACTION REQUEST" in r.text or "cut a release" in r.text
    assert f'id="gate-{approval["id"]}"' in r.text
    assert '<span class="nav-badge">1</span>' in r.text  # escalated ⇒ the tab is badged


def test_gate_pending_with_neo_costs_no_badge(gated):
    """A request Neo is still reviewing is free by design — badging it would undo
    the entire point of having Neo review it."""
    gated.request()
    r = gated.client.get("/gates")
    assert "With Neo" in r.text
    assert "./scripts/shipit.sh" in r.text
    assert "Waiting on you" not in r.text
    assert '<span class="nav-badge">' not in r.text


def test_approve_from_the_dashboard_opens_the_gate(gated):
    from jarvis.hooks import preflight_decision

    approval = gated.request()
    gated.daemon._neo_drain()

    r = gated.client.post(f"/gates/{approval['id']}/decide",
                          data={"decision": "approve", "reason": "I checked it myself",
                                "project": "proj_a", "next": "/gates"})
    assert r.status_code == 303
    assert r.headers["location"] == "/gates"

    fresh = gated.approval()
    assert fresh["status"] == "approved"
    assert fresh["decided_by"] == "user"
    assert fresh["decision_reason"] == "I checked it myself"

    # the point of approving: the worker's retry now goes through
    settings = json.loads(
        (gated.project / ".jarvis" / "worker-settings"
         / f"{gated.wo_id}.json").read_text())
    result = preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": "./scripts/shipit.sh"},
         "cwd": str(gated.project)}, settings["env"])
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    page = gated.client.get("/gates").text
    assert "Decided" in page and "approved" in page and "I checked it myself" in page


def test_deny_from_the_dashboard_needs_a_reason(gated):
    """ops.decide_gate refuses a reasonless denial — the worker acts on that text.
    The dashboard must surface the refusal rather than silently doing nothing."""
    approval = gated.request()
    gated.daemon._neo_drain()

    r = gated.client.post(f"/gates/{approval['id']}/decide",
                          data={"decision": "deny", "reason": "  ",
                                "project": "proj_a", "next": "/gates"})
    assert r.status_code == 303
    from urllib.parse import unquote
    assert "needs a reason" in unquote(r.headers["location"])
    assert gated.approval()["status"] == "pending"

    r = gated.client.post(f"/gates/{approval['id']}/decide",
                          data={"decision": "deny", "reason": "not this week",
                                "project": "proj_a", "next": "/gates"})
    assert r.status_code == 303
    assert gated.approval()["status"] == "denied"


def test_gate_decision_returns_to_the_page_it_was_made_from(gated):
    """Deciding from a work order should land back on that work order, and `next`
    must not become an open redirect out of the dashboard."""
    approval = gated.request()
    back = f"/wo/proj_a/{gated.wo_id}"

    detail = gated.client.get(back)
    assert "Privileged actions" in detail.text
    assert "./scripts/shipit.sh" in detail.text
    assert f'value="{back}"' in detail.text

    r = gated.client.post(f"/gates/{approval['id']}/decide",
                          data={"decision": "approve", "reason": "go",
                                "project": "proj_a", "next": back})
    assert r.headers["location"] == back

    other = gated.request(command="gh pr merge 7 --squash")
    r = gated.client.post(f"/gates/{other['id']}/decide",
                          data={"decision": "approve", "reason": "go",
                                "project": "proj_a", "next": "//evil.example.com"})
    assert r.headers["location"] == "/gates"


def test_neo_tab_sends_gate_escalations_to_the_gates_tab(gated):
    """A gate escalation is a Neo question, so it also lands on the neo tab. Answering
    it there queues a message and leaves the gate shut — the reply box has to be
    replaced by a pointer at the thing that actually decides it."""
    gated.request()
    gated.daemon._neo_drain()

    r = gated.client.get("/neo")
    assert "PRIVILEGED ACTION REQUEST" in r.text
    assert "decide it on the gates tab" in r.text
    assert "Send answer to worker" not in r.text


def test_dashboard_attention_deep_links_an_escalated_gate(gated):
    approval = gated.request()
    gated.daemon._neo_drain()
    r = gated.client.get("/")
    assert "NEEDS YOU" in r.text
    assert f'href="/gates#gate-{approval["id"]}"' in r.text


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


def test_hide_and_unhide_from_the_work_order_page(client, project):
    wo = ops.create_work_order("proj_a", "shy task")

    r = client.post(f"/wo/proj_a/{wo['id']}/hide")
    assert r.status_code == 303
    assert r.headers["location"] == "/project/proj_a"
    assert wo["id"] not in client.get("/project/proj_a").text
    assert wo["id"] in client.get("/project/proj_a?hidden=1").text

    detail = client.get(f"/wo/proj_a/{wo['id']}")
    assert "Unhide" in detail.text
    r = client.post(f"/wo/proj_a/{wo['id']}/unhide")
    assert r.status_code == 303
    assert wo["id"] in client.get("/project/proj_a").text


def test_delete_from_the_work_order_page_removes_it(client, project):
    wo = ops.create_work_order("proj_a", "doomed task")
    store = ProjectStore(project)
    store.queue_message(wo["id"], "some feedback")
    store.close()

    r = client.post(f"/wo/proj_a/{wo['id']}/delete")
    assert r.status_code == 303
    assert r.headers["location"] == "/project/proj_a"

    store = ProjectStore(project)
    try:
        assert store.list_work_orders(include_hidden=True) == []
        assert store.list_messages(wo["id"]) == []
    finally:
        store.close()
    assert "doomed task" not in client.get("/project/proj_a").text


def test_got_it_button_puts_the_flag_down(client, project):
    """The dashboard complaint in one test: acking has to survive the daemon.

    The attention strip is what the user actually reads, and before this there was no
    control on the page that could clear it — `jarvis inbox ack` acks notifications,
    which is a different store entirely.
    """
    wo = ops.create_work_order("proj_a", "noisy task")
    store = ProjectStore(project)
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], "finished without a completion signal — review the session")
    assert "NEEDS YOU" in client.get("/").text

    detail = client.get(f"/wo/proj_a/{wo['id']}")
    assert "Got it" in detail.text
    r = client.post(f"/wo/proj_a/{wo['id']}/ack")
    assert r.status_code == 303

    check_project(ProjectStore(project), repair=True)  # the daemon's next tick
    assert "all quiet" in client.get("/").text


def test_got_it_is_not_offered_when_a_decision_is_owed(client, project):
    """Assumptions need accept/reject — dismissing one would drop the work silently."""
    wo = ops.create_work_order("proj_a", "task with a judgement call")
    store = ProjectStore(project)
    store.add_assumption(wo["id"], "picked postgres over sqlite")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], "1 assumption pending your review")

    detail = client.get(f"/wo/proj_a/{wo['id']}")
    assert "Got it" not in detail.text

    r = client.post(f"/wo/proj_a/{wo['id']}/ack")  # forced by hand anyway
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert store.get_work_order(wo["id"])["needs_attention"]


def test_stale_deep_link_to_unregistered_project_is_not_a_500(client):
    """Notifications embed a deep link built from whatever project name the emitter
    passed. When that project is not (or is no longer) registered the dashboard has
    to say so — a bare "Internal Server Error" tells the user nothing and leaves
    them unable to tell a Jarvis bug from a vanished work order.
    """
    r = client.get("/wo/proj_gone/wo-4fdb20ba")
    assert r.status_code == 200
    assert "Internal Server Error" not in r.text
    assert "proj_gone" in r.text and "not registered" in r.text


def test_unexpected_error_renders_a_page_and_lands_in_the_os_log(
        jarvis_home, fake_claude, catalog_file, monkeypatch):
    """Defence in depth: whatever else breaks, the dashboard owes the user a page
    and the OS owes itself a trace on disk. Before this, UI tracebacks went to the
    systemd journal only — invisible to `$JARVIS_HOME/logs`, to Jarvis and to Neo.

    Starlette always re-raises after sending the response so the server can log it,
    hence `raise_server_exceptions=False` — a real browser still gets the page.
    """
    ops.start_os(str(catalog_file), foreground=True)
    c = TestClient(create_app(), follow_redirects=False, raise_server_exceptions=False)

    def boom():
        raise RuntimeError("kaboom in os_status")

    monkeypatch.setattr(ops, "os_status", boom)
    r = c.get("/")
    assert r.status_code == 500
    assert "something went wrong" in r.text.lower()
    assert "kaboom in os_status" in r.text

    log = (jarvis_home / "logs" / "ui.log").read_text()
    assert "kaboom in os_status" in log
    assert "GET /" in log
    assert "RuntimeError" in log
