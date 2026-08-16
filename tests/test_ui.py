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


def test_waiting_input_wo_shows_resume_hint_and_auto_action(client, daemon, project):
    """A worker blocked on a permission prompt can't be approved from the web UI, so the
    page surfaces the native escape hatch plus a resume-in-auto action.

    `claude --resume`, not `claude attach`: attaching is a background-agent verb, and
    worker turns are headless — between turns nothing owns the session, so it opens
    directly."""
    wo = ops.create_work_order("proj_a", "blocked task")
    daemon.tick()
    store = ProjectStore(project)
    store.update_work_order(wo["id"], session_id="sess-abc123")
    store.set_status(wo["id"], "waiting_input")
    store.flag_attention(wo["id"], "Claude needs your permission")

    detail = client.get(f"/wo/proj_a/{wo['id']}")
    assert detail.status_code == 200
    assert "claude --resume sess-abc123" in detail.text
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


def _add_session(fake_claude, project, sid="adhoc-9", name="manual poking",
                 state="working"):
    """A Claude session the user started in the project directory."""
    import json
    sessions = fake_claude.sessions
    sessions.append({"id": "zz", "sessionId": sid, "cwd": str(project),
                     "kind": "background", "name": name,
                     "state": state, "startedAt": 0})
    (fake_claude.dir / "sessions.json").write_text(json.dumps(sessions))


def test_your_own_sessions_are_offered_for_injection(client, daemon, fake_claude,
                                                     project):
    """The dashboard affordance for GitHub issue 47: the project page offers to take a
    session the user started, and until they say so it stays off the books."""
    _add_session(fake_claude, project)
    daemon.tick_count = 0
    daemon.tick()

    assert "manual poking" not in client.get("/project/proj_a").text

    panel = client.get("/project/proj_a/sessions").text
    assert "manual poking" in panel and "adhoc-9" in panel
    assert "Inject" in panel

    r = client.post("/project/proj_a/inject", data={"session_id": "adhoc-9"},
                    follow_redirects=True)
    assert "manual poking" in r.text and "injected" in r.text


def test_the_session_panel_never_breaks_the_page(client, fake_claude, project,
                                                 monkeypatch):
    """It shells out to `claude agents --json` — the one thing on any page that does.
    A CLI that is slow, broken or missing costs the panel, never the page."""
    from jarvis import claude_cli

    def boom(*a, **k):
        raise claude_cli.ClaudeCliError("timed out after 5s")

    monkeypatch.setattr(claude_cli, "list_background_sessions", boom)

    r = client.get("/project/proj_a")
    assert r.status_code == 200

    panel = client.get("/project/proj_a/sessions")
    assert panel.status_code == 200
    assert "could not list sessions" in panel.text


def test_injected_badge_visible(client, daemon, fake_claude, project):
    _add_session(fake_claude, project)
    client.post("/project/proj_a/inject", data={"session_id": "adhoc-9"})
    r = client.get("/")
    assert "injected" in r.text


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


#: A question of the shape that made this necessary — Neo question #53 was ~7,000
#: characters of feature-order brief, rendered inline, and the user could not keep up.
LONG_QUESTION = ("Should the exporter emit CSV or JSON, given the constraints below? "
                 * 30)


def test_neo_tab_shortens_an_over_long_question_and_keeps_the_full_text_one_click_away(
        client, daemon, project):
    """The whole feature, end to end, with its control.

    The page must show the SHORTENED question — and the verbatim text must still be
    reachable from it. Asserting only the first half cannot tell "correctly shortened"
    from "the question was lost", which is the failure that would actually hurt: the
    user reviews Neo's answers from this page, and an answer to a question they can no
    longer read is not reviewable.
    """
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], LONG_QUESTION)
    daemon._neo_drain()                 # answered, awaiting the user's review
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)

    page = client.get("/neo").text
    assert "digest of:" in page                       # the shortened rendering
    assert "cut down to the decision" in page            # ...and it says that it is one
    assert "i-have-adhd" in page                      # attribution for the style
    # THE CONTROL: the verbatim question is on the page, inside the disclosure.
    assert "Full question context sent to Neo" in page
    body = page.split("Full question context sent to Neo", 1)[1]
    assert LONG_QUESTION.strip() in body
    assert f"Work order: {wo['id']}" in body          # the prompt Neo got, not just the text


def test_neo_tab_shows_a_short_question_in_full_and_still_offers_the_disclosure(
        client, daemon, project):
    """No digest, no loss: a question under the threshold renders exactly as it did
    before this existed. The disclosure is there anyway — it carries the work-order
    context, which the page never shows otherwise, and a control that appears only
    sometimes is not one the reader learns to trust."""
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "CSV or JSON?")
    daemon._neo_drain()
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)

    page = client.get("/neo").text
    assert "CSV or JSON?" in page
    assert "digest of:" not in page
    assert "cut down to the decision" not in page   # nothing was, so the page says nothing
    assert "Full question context sent to Neo" in page


def test_neo_tab_falls_back_to_the_full_question_when_the_digest_failed(
        client, daemon, project):
    """A recorded digest failure must render as today's page, not as a blank box."""
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], LONG_QUESTION + " FORCE_DIGEST_FAIL")
    daemon._neo_drain()
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)

    page = client.get("/neo").text
    assert LONG_QUESTION.strip() in page
    assert "digest of:" not in page
    assert "cut down to the decision" not in page


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


def test_neo_tab_shows_panel_deliberation_collapsed(client, daemon, project):
    """The deliberation is on the page the user already reads, folded shut.

    A tab of its own would be a new place to spend exactly the attention the panel
    exists to protect, so this is a `<details>` on `/neo` and nothing more.

    The marker is "Panel deliberation", NOT the bare word "panel": `neo.html` has used
    a CSS class named `panel` for its generic box since long before this existed, so a
    test keyed on that string would pass against an unchanged template.
    """
    from jarvis.neo_store import NeoStore

    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "CSV or JSON?")
    daemon._neo_drain()

    before = client.get("/neo").text
    assert "Panel deliberation" not in before      # no opinions ⇒ no block at all

    neo = NeoStore()
    try:
        neo.record_opinion(1, "premise", reply="the question is real", verdict="",
                           route="panel", model="sonnet", latency_ms=311)
        neo.record_opinion(1, "blast", reply="CSV is reversible", verdict="answer",
                           status="abstained", model="opus", latency_ms=812)
    finally:
        neo.close()

    page = client.get("/neo").text
    assert "Panel deliberation" in page
    assert "<details" in page
    for seat in ("premise", "blast"):
        assert seat in page
    assert "311ms" in page and "812ms" in page
    assert "abstained" in page
    assert "the question is real" in page          # the seat's raw reply, on demand


def test_neo_tab_labels_a_seat_scoped_learning(client):
    """Unlabelled, a learning only one seat reads would display as a rule the whole of
    Neo follows — the opposite of what it is."""
    from jarvis.neo_store import NeoStore

    neo = NeoStore()
    try:
        neo.add_learning("A grep naming shipit ships nothing", seat="blast")
        neo.add_learning("Prefer uv over pip")
    finally:
        neo.close()
    page = client.get("/neo").text
    assert "blast seat" in page
    # the control: the global row is on the same page and carries no seat label
    assert "Prefer uv over pip" in page


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


def test_dismiss_from_the_dashboard_clears_the_command_without_approving_it(gated):
    """The third button. It has to do what approve does to the worker and none of what
    approve does to the record."""
    from jarvis.hooks import preflight_decision

    command = "uv run pytest tests/test_release_staging.py -k shipit"
    approval = gated.request(command=command, why="this runs a test; the literal is a -k selector")

    r = gated.client.post(f"/gates/{approval['id']}/decide",
                          data={"decision": "dismiss",
                                "reason": "the literal is a -k test selector",
                                "project": "proj_a", "next": "/gates"})
    assert r.status_code == 303

    fresh = gated.approval()
    assert fresh["status"] == "dismissed"
    assert fresh["decided_by"] == "user"

    settings = json.loads(
        (gated.project / ".jarvis" / "worker-settings"
         / f"{gated.wo_id}.json").read_text())
    result = preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": command},
         "cwd": str(gated.project)}, settings["env"])
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    page = gated.client.get("/gates").text
    # Listed apart from the verdicts, with the rate, and NOT counted as approved.
    assert "Not gated actions — the OS got these wrong" in page
    assert "false-positive rate: 1 of 1 (100%)" in page
    assert "no gate has been decided yet" in page


def test_dismiss_from_the_dashboard_needs_a_reason(gated):
    """The reason is the defect report on the recogniser — the only thing attached to
    the false-positive count that says what actually went wrong."""
    approval = gated.request(command="uv run pytest tests/test_release_staging.py -k shipit")

    r = gated.client.post(f"/gates/{approval['id']}/decide",
                          data={"decision": "dismiss", "reason": " ",
                                "project": "proj_a", "next": "/gates"})
    from urllib.parse import unquote
    assert "needs a reason" in unquote(r.headers["location"])
    assert gated.approval()["status"] == "pending"


def test_a_mangled_decision_value_cannot_open_a_gate(gated):
    """`decision` is an attacker-settable form field, so an unknown value must fail
    closed rather than falling through to the permissive branch."""
    approval = gated.request()

    gated.client.post(f"/gates/{approval['id']}/decide",
                      data={"decision": "approve​", "reason": "nice try",
                            "project": "proj_a", "next": "/gates"})

    assert gated.approval()["status"] == "denied"


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
    # running, so it is one of the statuses the project page gives a row of its own
    store = ProjectStore(project)
    store.set_status(wo["id"], "running")
    store.close()

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


def test_mark_done_from_the_work_order_page(client, project):
    wo = ops.create_work_order("proj_a", "finished by hand")

    detail = client.get(f"/wo/proj_a/{wo['id']}")
    assert "Mark done" in detail.text

    r = client.post(f"/wo/proj_a/{wo['id']}/done")
    assert r.status_code == 303
    assert r.headers["location"] == f"/wo/proj_a/{wo['id']}"

    store = ProjectStore(project)
    try:
        assert store.get_work_order(wo["id"])["status"] == "completed"
    finally:
        store.close()
    detail = client.get(f"/wo/proj_a/{wo['id']}")
    assert "Marked done by you" in detail.text
    assert "Mark done" not in detail.text  # nothing left to close


def test_mark_done_is_not_offered_while_assumptions_are_pending(client, project):
    """It would accept the assumptions silently. The panel that decides them is on the
    same page, so the user is one click from being allowed to close it."""
    wo = ops.create_work_order("proj_a", "has an open question")
    ops.assume(wo["id"], "used the sqlite backend")

    detail = client.get(f"/wo/proj_a/{wo['id']}")
    assert "Mark done" not in detail.text
    assert "Accept all" in detail.text

    r = client.post(f"/wo/proj_a/{wo['id']}/done")  # forced by hand anyway
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    store = ProjectStore(project)
    try:
        assert store.get_work_order(wo["id"])["status"] != "completed"
    finally:
        store.close()

    ops.review_work_order(wo["id"], accept=True)
    assert "Mark done" in client.get(f"/wo/proj_a/{wo['id']}").text


def test_settled_work_orders_collapse_into_a_count(client, project):
    """The complaint: a project page that lists every work order ever created is
    unreadable. Settled ones are the bulk of it and none is asking for anything."""
    open_wo = ops.create_work_order("proj_a", "still going")
    done = ops.create_work_order("proj_a", "old and finished")
    killed = ops.create_work_order("proj_a", "old and cancelled")
    ops.mark_done(done["id"])
    ops.cancel(killed["id"])
    store = ProjectStore(project)
    store.set_status(open_wo["id"], "running")
    store.close()

    page = client.get("/project/proj_a")
    assert "still going" in page.text
    assert "old and finished" not in page.text
    assert "old and cancelled" not in page.text
    assert "1 completed" in page.text and "1 cancelled" in page.text
    assert "failed" not in page.text  # zero of them: no count, no link

    just_completed = client.get("/project/proj_a?show=completed")
    assert "old and finished" in just_completed.text
    assert "old and cancelled" not in just_completed.text  # just that group
    assert "still going" in just_completed.text  # the open ones never leave

    everything = client.get("/project/proj_a?show=all")
    for title in ("still going", "old and finished", "old and cancelled"):
        assert title in everything.text


def test_pr_merges_and_running_work_get_the_rows(client, project):
    """The listing question the user actually asks the dashboard: what is moving, and
    what is waiting for me to merge it.

    `pending` is the collapsed case here because it is one of the two statuses that
    genuinely need nobody — see the test below for the ones that do.
    """
    running = ops.create_work_order("proj_a", "still going")
    merging = ops.create_work_order("proj_a", "waiting on a merge")
    ops.create_work_order("proj_a", "not started yet")
    ops.finish(merging["id"], "PR is up", pr_url="https://github.com/a/b/pull/9")
    store = ProjectStore(project)
    store.set_status(running["id"], "running")
    store.close()

    for url in ("/", "/project/proj_a"):
        page = client.get(url)
        assert "still going" in page.text, url
        assert "waiting on a merge" in page.text, url
        assert "https://github.com/a/b/pull/9" in page.text, url  # one click to merge
        assert "not started yet" not in page.text, url
        assert "1 pending" in page.text, url
        # running is listed before the merge queue, and both before anything else
        assert page.text.index("still going") < page.text.index("waiting on a merge")

    for url in ("/?show=pending", "/project/proj_a?show=pending"):
        assert "not started yet" in client.get(url).text, url


def test_a_decision_you_owe_gets_a_row_not_a_count(client, daemon, project):
    """The statuses invariants.true_blockers calls real blockers have to be rows.

    They were counts, which is how a project page came to print "3 needs review" in the
    header and "nothing running and nothing to merge" in the panel underneath it. A
    listing that disagrees with true_blockers about what needs the user is a bug in the
    listing.
    """
    reviewing = ops.create_work_order("proj_a", "decide this please")
    blocked = ops.create_work_order("proj_a", "worker is stuck")
    daemon.tick()
    ops.assume(reviewing["id"], "assumed the API is v2")
    ops.finish(reviewing["id"], "done-ish")
    store = ProjectStore(project)
    store.set_status(blocked["id"], "waiting_input")
    assert store.get_work_order(reviewing["id"])["status"] == "needs_review"
    store.close()
    ops.create_work_order("proj_a", "not started yet")  # after the tick: stays pending

    for url in ("/", "/project/proj_a"):
        page = client.get(url).text
        assert "decide this please" in page, url
        assert "worker is stuck" in page, url
        assert "not started yet" not in page, url  # started by nobody, needs nobody
        assert "show=pending" in page, url  # a count line, expandable
        assert "show=needs_review" not in page, url  # a row, so never a count line
        # a review the user owes comes before a worker merely blocked on them
        assert page.index("decide this please") < page.index("worker is stuck"), url

    for url in ("/?show=pending", "/project/proj_a?show=pending"):
        assert "not started yet" in client.get(url).text, url


def test_a_project_whose_only_open_work_needs_review_is_not_an_empty_panel(client,
                                                                          daemon):
    """The reported bug, exactly: counts and panel contradicting each other."""
    for n in range(3):
        wo = ops.create_work_order("proj_a", f"needs a decision {n}")
        daemon.tick()
        ops.assume(wo["id"], "assumed something")
        ops.finish(wo["id"], "done-ish")

    for url in ("/", "/project/proj_a"):
        page = client.get(url).text
        assert "nothing running and nothing waiting on you" not in page, url
        # the count line's own signature: the link that expands the collapsed group
        assert "show=needs_review" not in page, url
        for n in range(3):
            assert f"needs a decision {n}" in page, url


def test_a_blocked_work_order_says_what_it_is_waiting_for(client, project):
    """Both listings, because they derive it by different routes — the dashboard off
    `ops.os_status`, the project page off the store directly. A surface that worked
    while its twin lied is exactly how the FEATURED_STATUSES bug survived."""
    dep = ops.create_work_order("proj_a", "the schema change")
    ops.create_work_order("proj_a", "needs the schema", depends_on=[dep["id"]])

    # Pending work still collapses by default — a work order waiting its turn needs
    # nobody — so the line is asserted where the user actually expands to see it.
    for url in ("/?show=pending", "/project/proj_a?show=pending"):
        page = client.get(url)
        assert "blocked by" in page.text, url
        assert dep["id"] in page.text, url


def test_a_stranded_work_order_reaches_the_attention_strip(client, project):
    """The one dependency case that is not routine: it will never start on its own."""
    dep = ops.create_work_order("proj_a", "the schema change")
    child = ops.create_work_order("proj_a", "needs the schema", depends_on=[dep["id"]])
    store = ProjectStore(project)
    store.set_status(dep["id"], "cancelled")
    check_project(store, repair=True)
    store.close()

    body = client.get("/").text
    assert child["id"] in body
    assert "can never complete" in body


def test_a_pr_waiting_to_be_merged_never_enters_the_attention_strip(client, project):
    """It is a merge queue, not an interrupt: NEEDS YOU has to stay for real blockers."""
    wo = ops.create_work_order("proj_a", "waiting on a merge")
    ops.finish(wo["id"], "PR is up", pr_url="https://github.com/a/b/pull/9")

    page = client.get("/")
    assert "all quiet" in page.text
    assert "NEEDS YOU" not in page.text


def test_expanding_a_settled_group_keeps_hidden_work_orders_showing(client, project):
    """Two independent toggles: expanding one must not silently undo the other."""
    shy = ops.create_work_order("proj_a", "hidden and finished")
    ops.mark_done(shy["id"])
    ops.hide_work_order(shy["id"], hidden=True)

    assert "hidden and finished" not in client.get("/project/proj_a?show=completed").text
    both = client.get("/project/proj_a?show=completed&hidden=1")
    assert "hidden and finished" in both.text
    assert "show=completed" in both.text  # the "hide them again" link keeps the group


def test_a_bogus_show_value_falls_back_to_the_open_list(client, project):
    live = ops.create_work_order("proj_a", "still going")
    done = ops.create_work_order("proj_a", "old and finished")
    ops.mark_done(done["id"])
    store = ProjectStore(project)
    store.set_status(live["id"], "running")
    store.close()

    page = client.get("/project/proj_a?show=../../etc/passwd")
    assert page.status_code == 200
    assert "still going" in page.text
    assert "old and finished" not in page.text


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
    # 404, not 200: there is genuinely no such page. What matters is that it is not a
    # 500 and that the page names which half of the link went stale.
    assert r.status_code == 404
    assert "Internal Server Error" not in r.text
    assert "proj_gone" in r.text and "not registered" in r.text
    assert "never registered, or it has since been removed" in r.text


def test_stale_deep_link_to_a_deleted_work_order_says_which_half_is_gone(client):
    """The other stale-link shape: the project is fine, the work order is not (a
    notification that outlived `jarvis wo delete`). Blaming the project here would send
    the user to check the catalog for no reason."""
    r = client.get("/wo/proj_a/wo-neverexisted")
    assert r.status_code == 404
    assert "is registered, but it has no work order" in r.text


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


# -- feature orders ------------------------------------------------------------------
#
# The feature page is the one view where the whole feature is visible at once — the ask,
# the plan as submitted, and each child's live status against it. It is also where an
# escalated plan is decided, because deciding needs all three on one screen.


@pytest.fixture()
def feature(client, daemon, project):
    """A feature order whose plan is submitted and escalated to the user."""
    from tests.test_feature_orders import ASK, a_plan, child

    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    ops.submit_plan(fo["id"], a_plan(child("schema"), child("api", needs=["schema"])))
    daemon._neo_drain()  # the fake escalates plan reviews unless forced
    return client, ops.show_feature_order(fo["id"])


def test_the_feature_page_shows_the_ask_the_plan_and_the_children(feature):
    client, fo = feature

    page = client.get(f"/fo/proj_a/{fo['id']}").text

    assert "CSV export" in page
    assert "Add a CSV exporter" in page          # the ask, verbatim
    assert "Build schema" in page and "Build api" in page   # the plan
    assert "nothing created yet" in page         # a plan is a proposal until released


def test_the_feature_page_is_where_an_escalated_plan_is_decided(feature):
    client, fo = feature

    page = client.get(f"/fo/proj_a/{fo['id']}").text

    assert "Release this plan?" in page
    assert f"/fo/proj_a/{fo['id']}/review" in page


def test_releasing_a_plan_from_the_browser_creates_the_children(feature):
    client, fo = feature

    res = client.post(f"/fo/proj_a/{fo['id']}/review",
                      data={"decision": "accept", "feedback": "looks right"})

    assert res.status_code == 303
    detail = ops.show_feature_order(fo["id"])
    assert detail["status"] == "executing"
    assert [c["title"] for c in detail["children"]] == ["Build schema", "Build api"]
    page = client.get(f"/fo/proj_a/{fo['id']}").text
    assert "⊘ after" in page      # the tree, with the edge drawn


def test_sending_a_plan_back_with_no_reason_says_so_instead_of_failing(feature):
    """The planner sees only the reason, so a bare rejection is a guess. The refusal has
    to reach the page rather than becoming a 500."""
    client, fo = feature

    res = client.post(f"/fo/proj_a/{fo['id']}/review", data={"decision": "reject"})

    assert res.status_code == 303
    assert "error=" in res.headers["location"]
    assert ops.show_feature_order(fo["id"])["status"] == "plan_review"


def test_a_plan_neo_can_decide_never_reaches_the_users_page(client, daemon, project):
    """The control for the panel above: routine plans must not put a decision on screen,
    because a feature order that costs an interactive review every time costs more
    attention than typing the work orders by hand."""
    from tests.test_feature_orders import ASK, a_plan, child

    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE")))
    daemon._neo_drain()

    page = client.get(f"/fo/proj_a/{fo['id']}").text

    assert "Release this plan?" not in page
    assert "executing" in page


def test_the_project_page_lists_feature_orders_without_repeating_the_tree(feature):
    client, fo = feature

    page = client.get("/project/proj_a").text

    assert f"/fo/proj_a/{fo['id']}" in page
    assert "Feature orders" in page
    # The children belong to the listing below as ordinary work orders, and to the
    # feature's own page as a tree. Not to both, on one page.
    assert page.count(f"/fo/proj_a/{fo['id']}") == 1


def test_the_dashboard_takes_a_feature_order(client):
    res = client.post("/fo/create", data={"project": "proj_a", "title": "CSV export",
                                          "description": "the whole ask, at length, "
                                                         "with enough detail to plan"})

    assert res.status_code == 303
    assert res.headers["location"].startswith("/fo/proj_a/fo-")


def test_a_feature_order_with_no_description_is_refused_by_the_browser_too(client):
    res = client.post("/fo/create", data={"project": "proj_a", "title": "CSV export"})

    assert res.status_code == 303
    assert res.headers["location"].startswith("/project/proj_a?error=")


def test_cancelling_a_feature_order_from_the_browser(feature):
    client, fo = feature

    res = client.post(f"/fo/proj_a/{fo['id']}/cancel")

    assert res.status_code == 303
    assert ops.show_feature_order(fo["id"])["status"] == "cancelled"


def test_an_unknown_feature_order_is_a_page_not_a_traceback(client):
    page = client.get("/fo/proj_a/fo-nope")

    assert page.status_code == 200
    assert "not found in any registered project" in page.text


def test_the_feature_page_shows_its_slot_budget(client, daemon, project):
    """`max_parallel` is invisible everywhere else, so the one page that holds the whole
    feature is where the user finds out why three children are showing two running."""
    from tests.test_feature_orders import ASK, a_plan, child

    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK,
                                  max_parallel=2)
    daemon.tick()
    ops.submit_plan(fo["id"], a_plan(child("one", extra="FORCE_APPROVE"),
                                     child("two", extra="FORCE_APPROVE"),
                                     child("three", extra="FORCE_APPROVE")))
    daemon._neo_drain()
    daemon.tick()

    page = client.get(f"/fo/proj_a/{fo['id']}").text

    assert "2/2 slots in use" in page
    # ...and the child that is waiting says so, rather than reading as about-to-start.
    assert "waiting for a slot" in page


def test_an_uncapped_feature_page_says_nothing_about_slots(feature):
    """The control: a feature with no cap has nothing to report, and the project's own
    max_concurrent is not this page's business."""
    client, fo = feature

    assert "slots in use" not in client.get(f"/fo/proj_a/{fo['id']}").text


def test_the_attention_strip_links_a_rolled_up_feature_to_its_page(client, daemon,
                                                                   project):
    """The rollup collapses the children's lines, so the one link it leaves has to reach
    the page where they are individually visible — otherwise collapsing them hides them."""
    from jarvis.project_store import ProjectStore
    from tests.test_feature_orders import ASK, a_plan, child

    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    ops.submit_plan(fo["id"], a_plan(child("one", extra="FORCE_APPROVE"),
                                     child("two", extra="FORCE_APPROVE")))
    daemon._neo_drain()
    store = ProjectStore(project)
    try:
        kid = store.feature_children(fo["id"])[0]
        store.update_work_order(kid["id"], needs_attention=1,
                                attention_reason="assumption needs a decision")
    finally:
        store.close()

    page = client.get("/").text

    assert f'/fo/proj_a/{fo["id"]}' in page
    assert "1 of its work orders need you" in page
def test_knowledge_page_pin_toggle(client):
    """Pinning is the switch between 'in every worker prompt verbatim' and 'an index
    line the worker looks up' — so it has to be operable without touching SQLite."""
    from jarvis.central_store import PINNED_TAG, CentralStore, has_tag

    central = CentralStore()
    row = central.add_knowledge("never deploy on a Friday", project="proj_a",
                                topic="deploy")

    page = client.get("/knowledge")
    assert "never deploy on a Friday" in page.text
    assert row["id"] in page.text            # the id the worker would `learn show`
    assert "index" in page.text.lower()      # the page explains the new contract

    r = client.post("/knowledge/pin", data={"kn_id": row["id"], "pinned": "1"})
    assert r.status_code == 303
    fresh = central.get_knowledge(row["id"])
    assert fresh is not None and has_tag(fresh["tags"], PINNED_TAG)
    assert "unpin" in client.get("/knowledge").text

    client.post("/knowledge/pin", data={"kn_id": row["id"], "pinned": "0"})
    fresh = central.get_knowledge(row["id"])
    assert fresh is not None and not has_tag(fresh["tags"], PINNED_TAG)


# -- which instance is this? -----------------------------------------------------

def _chrome(jarvis_home, catalog_file, path="/"):
    """The rendered header, from a freshly built app (the badge is read once)."""
    ops.start_os(str(catalog_file), foreground=True)
    c = TestClient(create_app(), follow_redirects=False)
    return c.get(path).text


def test_header_names_a_dev_instance_and_its_version(
        jarvis_home, fake_claude, catalog_file, monkeypatch):
    """Two checkouts of the same code on one machine render identical dashboards;
    the badge is what stops someone acting on the live fleet from the wrong one."""
    from jarvis import bugreport

    monkeypatch.delenv("JARVIS_ENV", raising=False)
    monkeypatch.setenv("PRODUCTION_CODE", str(jarvis_home / "not-production"))
    monkeypatch.setattr(bugreport, "jarvis_version", lambda: "dev-a1b2c3d")
    html = _chrome(jarvis_home, catalog_file)

    # a dev build has no release number — the sha is the version, and the badge does
    # not say "dev" twice
    assert "dev · a1b2c3d" in html
    assert "vdev-" not in html
    assert 'class="instance"' in html  # muted: dev must not wear the prod colour


def test_header_marks_a_production_instance(
        jarvis_home, fake_claude, catalog_file, monkeypatch):
    from jarvis import bugreport

    monkeypatch.setenv("JARVIS_ENV", "production")
    monkeypatch.setattr(bugreport, "jarvis_version", lambda: "0.5.0")
    html = _chrome(jarvis_home, catalog_file)

    assert "prod · v0.5.0" in html
    assert 'class="instance prod"' in html


def test_a_hand_edited_release_still_says_so_in_the_badge(
        jarvis_home, fake_claude, catalog_file, monkeypatch):
    """`-dirty` means the production checkout is no longer the release it claims to
    be. It reaches bug reports already; the header is where someone would see it."""
    from jarvis import bugreport

    monkeypatch.setenv("JARVIS_ENV", "production")
    monkeypatch.setattr(bugreport, "jarvis_version", lambda: "0.5.0-dirty")
    html = _chrome(jarvis_home, catalog_file)

    assert "prod · v0.5.0-dirty" in html


def test_instance_badge_is_on_every_page_with_a_diagnosable_tooltip(
        jarvis_home, fake_claude, catalog_file, monkeypatch):
    """It lives in the base template, so it cannot go missing on the page someone
    happens to deep-link into out of a Telegram alert."""
    monkeypatch.setenv("JARVIS_ENV", "production")
    for path in ("/", "/inbox", "/backlog", "/knowledge", "/neo", "/gates"):
        html = _chrome(jarvis_home, catalog_file, path)
        assert 'class="instance prod"' in html, path
        assert "title=\"production · JARVIS_ENV=production · version " in html, path


def test_version_lookup_failure_does_not_break_the_header(
        jarvis_home, fake_claude, catalog_file, monkeypatch):
    from jarvis import bugreport

    def boom():
        raise RuntimeError("no dist metadata")

    monkeypatch.setattr(bugreport, "jarvis_version", boom)
    html = _chrome(jarvis_home, catalog_file)
    assert "· unknown" in html
    assert 'class="instance"' in html
