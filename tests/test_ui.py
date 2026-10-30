"""Web UI tests: pages render, actions call the same ops as the CLI"""

from __future__ import annotations

import html
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import gates, ops  # noqa: E402
from jarvis.catalog import load_catalog  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.daemon import Daemon  # noqa: E402
from jarvis.invariants import IDLE_NO_FINISH_BLOCKER, check_project  # noqa: E402
from jarvis.project_store import VALIDATION_CI_CAUSE, ProjectStore  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file, live_daemon):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture()
def daemon(catalog_file):
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def gated_catalog(tmp_path, project):
    """A catalog whose only project gates every privileged action"""
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
    """A started OS with a gated project, a dispatched work order, and a browser"""
    ops.start_os(str(gated_catalog), foreground=True)
    daemon = Daemon(load_catalog(gated_catalog))
    wo = ops.create_work_order("proj_a", "ship version 1.2.3")
    daemon.tick()

    class Handle:
        gated_command = "./scripts/shipit.sh"

        def __init__(self):
            self.daemon = daemon
            self.wo_id = wo["id"]
            self.project = project
            self.client = TestClient(create_app(), follow_redirects=False)

        def request(self, command="./scripts/shipit.sh", why="please"):
            """File a gate request and return the approval row"""
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
    # the sidebar lists projects; the dashboard has no projects column (wo-27a8661c)
    assert "<h2>Projects</h2>" not in r.text


def test_dashboard_auto_refresh_is_scoped_to_the_live_regions(client):
    """A whole-page refresh wipes a half-typed work order"""
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
    # detail page renders the project and who launched it (wo-776ac037)
    page = client.get(r.headers["location"])
    assert "from the ui" in page.text
    assert '<span class="badge framework">proj_a</span>' in page.text
    assert ">manual</span>" in page.text


def test_waiting_input_wo_shows_resume_hint_and_auto_action(client, daemon, project):
    """A worker blocked on a permission prompt can't be approved from the web UI"""
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


def test_wo_page_shows_the_whole_session_id(client, daemon, project):
    """The session id is a thing to COPY, not to recognise"""
    sid = "8a53d05e-6f2b-4c19-9a7d-3e51b0c4f7d2"
    wo = ops.create_work_order("proj_a", "long-running task")
    daemon.tick()
    store = ProjectStore(project)
    store.update_work_order(wo["id"], session_id=sid)

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    assert sid in page
    assert "8a53d05e…" not in page


def test_resume_command_is_offered_only_when_no_turn_can_be_in_flight(
        client, daemon, project):
    """Settled work orders get the whole `claude --resume` line, ready to run — that is what the id is FOR"""
    sid = "8a53d05e-6f2b-4c19-9a7d-3e51b0c4f7d2"
    wo = ops.create_work_order("proj_a", "some task")
    daemon.tick()
    store = ProjectStore(project)
    store.update_work_order(wo["id"], session_id=sid)

    for status in ("dispatching", "running"):
        store.set_status(wo["id"], status)
        page = client.get(f"/wo/proj_a/{wo['id']}").text
        assert sid in page, f"the id itself is always shown ({status})"
        assert f"claude --resume {sid}" not in page, f"no invitation while {status}"

    for status in ("needs_review", "waiting_pr_merge", "completed", "failed",
                   "cancelled"):
        store.set_status(wo["id"], status)
        page = client.get(f"/wo/proj_a/{wo['id']}").text
        assert f"claude --resume {sid}" in page, f"ready to run once {status}"


def test_a_work_order_parked_on_neo_is_not_called_blocked(client, daemon, project):
    """The same page, the same status, a different wait — GitHub issue 100"""
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")

    detail = client.get(f"/wo/proj_a/{wo['id']}")

    assert detail.status_code == 200
    assert "Blocked on a permission prompt" not in detail.text
    assert f"/wo/proj_a/{wo['id']}/resume-auto" not in detail.text
    assert "Neo is answering question 1" in detail.text


def test_wo_page_anchors_pending_at_what_needs_the_user(client, daemon):
    """Notifications deep-link to #pending"""
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


def test_a_pending_assumption_says_who_it_waits_on_and_links_its_turn(client, project):
    """Issue #1265: the card carried the text and an age, and nothing else"""
    wo = ops.create_work_order("proj_a", "risky change")
    store = ProjectStore(project)
    try:
        ops.assume(wo["id"], "assumed the API is v2")
        raised_in = store.record_agent_reply(wo["id"], "recorded assumption #1 — why")
    finally:
        store.close()
    ops.finish(wo["id"], "done-ish")

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    card = page.split("Assumptions pending your review", 1)[1].split("</div></div>", 1)[0]
    flat = html.unescape(" ".join(card.split()))

    # WHY it is with the user, naming the setting that put it there
    assert "`validation.enabled` is off" in flat
    assert "nobody judged it before you" in flat
    # WHERE it was raised, as the anchor the Conversation tab already resolves
    assert f'href="#msg-{raised_in}"' in card
    assert f'id="msg-{raised_in}"' in page
    # And the placeholder no longer implies Neo ruled on it first
    assert "teaches Neo" not in page
    assert "recorded as a rule for Neo" in html.unescape(page)


# -- the work order page reads as three alternatives, not one long scroll


def test_the_three_readings_are_tabs_and_the_asks_are_not(client, daemon, project):
    """Conversation, spend and timeline are alternatives — one tab strip, one panel open"""
    wo = ops.create_work_order("proj_a", "risky change")
    daemon.tick()
    ops.assume(wo["id"], "assumed the API is v2")
    ops.finish(wo["id"], "done-ish")

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    head, tabs = page.split('<div class="tabbed"', 1)
    assert 'id="tab-conversation"' in tabs and 'id="tab-timeline"' in tabs
    # Exactly one panel starts open, and it is the conversation
    assert tabs.count('class="tabpanel on"') == 1
    assert 'class="tabpanel on" id="tab-conversation"' in tabs
    # The pending-assumptions panel is above the strip, where it cannot be hidden
    assert "Assumptions pending your review" in head


def test_the_page_still_reads_without_the_tab_script(client, daemon):
    """Nothing is hidden until the script marks the container `.js`"""
    wo = ops.create_work_order("proj_a", "risky change")
    daemon.tick()
    page = client.get(f"/wo/proj_a/{wo['id']}").text
    assert "<h2>Conversation</h2>" in page
    assert "<h2>Timeline</h2>" in page
    assert "root.classList.add('js')" in page


def test_a_neo_answer_is_one_line_on_the_page_and_credited_to_neo(client, daemon,
                                                                  project):
    """It used to be two — the "Neo answered the worker" event and the message under it"""
    from jarvis.project_store import ProjectStore as _PS

    wo = ops.create_work_order("proj_a", "risky change")
    daemon.tick()
    store = _PS(project)
    try:
        store.queue_message(wo["id"], "[Neo] go with CSV", source="neo")
        store.add_event(wo["id"], "neo_answered", {"neo_question_id": 1})
    finally:
        store.close()

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    said, story = page.split('id="tab-timeline"', 1)
    # The conversation credits it correctly, and is the only place the words appear
    assert "neo → worker" in said
    assert said.count("[Neo] go with CSV") == 1
    # In the timeline the answer is ONE entry
    assert story.count("Neo answered the worker") == 1
    assert "[Neo] go with CSV" not in story
    assert "in the conversation" in story


def test_a_question_on_the_timeline_is_a_link_to_the_question(client, daemon, project):
    wo = ops.create_work_order("proj_a", "risky change")
    daemon.tick()
    result = ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    said, story = page.split('id="tab-timeline"', 1)
    assert f'/neo/question/{result["question_id"]}' in story
    # ...and the question itself is NOT reprinted beside the link
    assert "Should the export default to CSV or JSON?" not in story
    assert "Should the export default to CSV or JSON?" in said
    assert "worker → Neo" in said


def test_an_alarm_on_the_timeline_is_a_link_to_that_alarm(client, daemon, project):
    """§4 of docs/superpowers/specs/2026-08-31-the-supervisor.md"""
    wo = ops.create_work_order("proj_a", "export citations")
    daemon.tick()
    _, path, _ = ops.find_work_order(wo["id"], "proj_a")
    store = ProjectStore(path)
    alarm = store.add_alarm(wo["id"], "turn_minutes", 1, "running 94 minutes")
    store.add_event(wo["id"], "cost_alarm",
                    {"kind": "turn_minutes", "seq": 1, "reason": "running 94 minutes",
                     "alarm_id": alarm["id"]})
    store.add_event(wo["id"], "alarm_reviewed",
                    {"alarm_id": alarm["id"], "verdict": "ack",
                     "reason": "a long test run, not a stuck turn",
                     "note": "it is re-running the suite; nothing is stuck"})
    store.close()

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    said, story = page.split('id="tab-timeline"', 1)
    assert f'href="/alarms/proj_a/{alarm["id"]}"' in story
    # The note is speech and lives in the conversation, once
    assert said.count("it is re-running the suite; nothing is stuck") == 1
    assert "it is re-running the suite; nothing is stuck" not in story
    assert "supervisor → you" in said
    assert story.count("a long test run, not a stuck turn") == 1
    assert "a long test run, not a stuck turn" not in said


def test_the_question_page_holds_the_question_and_its_answer(client, daemon, project):
    wo = ops.create_work_order("proj_a", "risky change")
    daemon.tick()
    qid = ops.ask_question(wo["id"], "CSV or JSON for the export default?")["question_id"]
    ops.neo_answer_escalated(qid, "CSV, and gzip it")

    page = client.get(f"/neo/question/{qid}").text
    assert "CSV or JSON for the export default?" in page
    assert "CSV, and gzip it" in page
    assert f"/wo/proj_a/{wo['id']}" in page  # back to the work order that asked


def test_a_triage_question_sends_the_reader_to_the_issue_not_the_backlog(client):
    """No worker exists to answer a `triage` question (issue #240), so the page offers no reply box"""
    import json as _json

    from jarvis.neo_store import NeoStore

    url = "https://github.com/someone/repo/issues/41"
    neo = NeoStore()
    try:
        q = neo.ask("proj_a", "", "is this really a blocker?", kind="triage",
                    context=_json.dumps({"issue_url": url, "priority": "blocker"}))
        neo.mark(q["id"], "escalated", reason="not enough evidence")
    finally:
        neo.close()

    page = client.get(f"/neo/question/{q['id']}").text
    assert "jarvis issues start" in page and url in page
    # The nav bar still links the backlog page
    assert "from the backlog" not in page and "backlog promote" not in page
    assert 'action="/neo/' not in page, "there is nobody to reply to"


def test_a_question_that_does_not_exist_says_so(client):
    r = client.get("/neo/question/9999")
    assert r.status_code == 404
    assert "not found" in r.text


def test_the_neo_stats_page_renders_an_empty_fleet_without_a_fabricated_zero(client):
    """§6 of docs/superpowers/specs/2026-10-01-neo-observability.md: its own page"""
    page = client.get("/neo/stats")
    assert page.status_code == 200
    assert '<span class="sub">not recorded</span>' in page.text
    # past the stylesheet, whose own percentages are not figures
    body = page.text.split("</style>")[-1]
    assert "0%" not in body, "an absent rate must never render as a measured zero"
    # a measured zero IS printed: the question census has a row for everything
    assert "0 question" in page.text


def test_the_neo_page_links_to_the_report_and_is_otherwise_unchanged(client):
    page = client.get("/neo")
    assert page.status_code == 200
    assert 'href="/neo/stats"' in page.text
    # no statistics block among the review forms — the reason the report is its own page
    assert "escalation rate" not in page.text


def test_the_neo_stats_page_prints_the_counts_and_the_cause_split(client, daemon,
                                                                 project):
    from jarvis.neo_store import NeoStore

    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_ESCALATE: may I rotate the production key?")
    daemon._neo_drain()
    neo = NeoStore()
    try:
        neo.mark(1, "escalated", cause="high-stakes")
    finally:
        neo.close()

    page = client.get("/neo/stats").text
    assert "high-stakes" in page
    assert "1 question" in page
    assert "Neo chose this label" in page


def test_the_stats_page_reads_the_causes_as_three_answers_to_who_decided(client, daemon,
                                                                        project):
    """Neo, question 1170: an override is a THIRD answer to "who decided\""""
    from jarvis import ops as ops_mod
    from jarvis.neo_store import NeoStore

    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_ESCALATE: may I rotate the production key?")
    daemon._neo_drain()
    neo = NeoStore()
    try:
        neo.mark(1, "escalated", cause="stakes-unclassified")
    finally:
        neo.close()

    page = client.get("/neo/stats").text
    assert "stakes-unclassified" in page
    assert "Neo answered and the OS overrode it" in page
    assert "Neo chose to hand it back" in page
    assert "Neo never answered" in page
    assert ops_mod.NEO_ESCALATION_INVISIBLE_NOTE in page


def test_the_stats_page_renders_spend_and_latency_over_several_neo_kinds(client, project):
    """Two kinds is the case the page is FOR"""
    central = CentralStore()
    try:
        central.upsert_project("proj_a", str(project))
        for kind, model, latency in (("neo_answer", "claude-sonnet-4-5", 240),
                                     ("panel_seat", "claude-opus-5", None)):
            central.add_agent_call(
                kind, project="proj_a", wo_id="wo-1", model=model, latency_ms=latency,
                usage={"input": 10, "cache_write": 20, "cache_read": 30, "output": 40,
                       "total_cost_usd": 0.01})
        central.conn.commit()
    finally:
        central.close()

    page = client.get("/neo/stats")
    assert page.status_code == 200
    assert "neo_answer" in page.text and "panel_seat" in page.text
    # the latency block's "measured/calls" rendering, with the untimed kind at 0/1
    assert "0/1 timed" in page.text and "1/1 timed" in page.text


def test_the_stats_page_trend_counts_stay_on_one_line(client, daemon, project):
    """wo-327f211c: at 1280px "10 asked · 1 escalated" wrapped inside a 150px span"""
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "which format?")
    daemon._neo_drain()

    page = client.get("/neo/stats").text
    trend = [ln for ln in page.splitlines() if "asked ·" in ln and "style" in ln]
    assert trend, "no inline-styled trend row found"
    for line in trend:
        assert "white-space: nowrap" in line, line


def test_the_stats_page_trend_row_carries_the_unreachable_count_and_rate(client, daemon,
                                                                        project):
    """Spec §4: a bar may never contradict the counts beside it"""
    from jarvis.neo_store import NeoStore

    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "which format?")
    daemon._neo_drain()
    neo = NeoStore()
    try:
        neo.mark(1, "failed", cause="transport-unreachable")
    finally:
        neo.close()

    page = client.get("/neo/stats").text
    assert "never reached" in page
    block = page.split("The trend")[1].split("<h2>")[0]
    assert "never reached" in block
    assert "unreachable" in block
    # the bar stays driven by the ESCALATION rate, which is 0 here, so none is drawn
    assert "0%" in block


def test_an_unregistered_project_on_the_stats_page_is_an_error_not_a_crash(client):
    page = client.get("/neo/stats?project=nope")
    assert page.status_code == 200
    assert "not registered" in page.text


def test_the_question_page_reviews_the_answer_and_stays_put(client, daemon, project):
    """The timeline sends the reader here; the decision has to be here too"""
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "CSV or JSON?")
    daemon._neo_drain()

    page = client.get("/neo/question/1").text
    assert 'action="/neo/1/review"' in page
    assert 'href="/neo"' in page  # the breadcrumb stays; the "go elsewhere" link doesn't
    assert "review it →" not in page

    r = client.post("/neo/1/review", data={"decision": "correct",
                                           "feedback": "CSV. Always CSV.",
                                           "next": "/neo/question/1"})
    assert r.status_code == 303
    assert r.headers["location"] == "/neo/question/1"
    from jarvis.neo_store import NeoStore
    neo = NeoStore()
    try:
        assert neo.get(1)["review_status"] == "corrected"
    finally:
        neo.close()
    assert "you corrected this" in client.get("/neo/question/1").text


def test_the_question_page_answers_an_escalation_and_stays_put(client, daemon, project):
    wo = ops.create_work_order("proj_a", "prod thing")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_ESCALATE: touch prod?")
    daemon._neo_drain()

    page = client.get("/neo/question/1").text
    assert 'action="/neo/1/answer"' in page
    assert "answer it →" not in page

    r = client.post("/neo/1/answer", data={"text": "No. Wait for the window.",
                                           "next": "/neo/question/1"})
    assert r.status_code == 303
    assert r.headers["location"] == "/neo/question/1"
    store = ProjectStore(project)
    try:
        contents = [m["content"] for m in store.queued_messages(wo["id"])]
        assert any("Wait for the window" in c for c in contents)
    finally:
        store.close()


def test_a_neo_decision_will_not_redirect_off_site(client, daemon, project):
    """`next` is attacker-settable, exactly as on the gates form"""
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "CSV or JSON?")
    daemon._neo_drain()

    r = client.post("/neo/1/review", data={"decision": "approve",
                                           "next": "//evil.example/steal"})
    assert r.headers["location"] == "/neo#tab-review"


def test_a_refused_neo_decision_flashes_on_the_tab_it_came_from(client, daemon, project):
    """`?error=` has to precede `#tab-review`"""
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "CSV or JSON?")
    daemon._neo_drain()

    r = client.post("/neo/1/review", data={"decision": "correct", "feedback": ""})
    assert r.headers["location"].startswith("/neo?error=")
    assert r.headers["location"].endswith("#tab-review")


def test_the_question_page_sends_a_gate_request_to_the_gates_tab(gated):
    """An approval question carries a gate the textarea cannot open"""
    gated.request()
    gated.daemon._neo_drain()  # the fake model escalates by default

    page = gated.client.get("/neo/question/1").text
    assert 'action="/neo/1/answer"' not in page
    assert "decide it on the gates tab" in page


def test_an_escalated_assumption_sends_the_user_to_the_work_order_not_the_worker(
        client, daemon, project):
    """Auto-review's half of the same rule"""
    from jarvis.neo_store import NeoStore

    wo = ops.create_work_order("proj_a", "export the schedule")
    store = ProjectStore(project)
    a = store.add_assumption(wo["id"], "dropped the `--since` flag from the exporter")
    store.set_status(wo["id"], "needs_review")
    neo = NeoStore()
    q = neo.ask("proj_a", wo["id"],
                f"ASSUMPTION REVIEW — rule on assumption #1 of {wo['id']} in proj_a, "
                f"and on nothing else.", kind="assumption")
    neo.mark(q["id"], "escalated",
             reason="dropping a flag changes the CLI surface, which is yours to decide")
    store.link_assumption_question(a, q["id"])

    for page in (client.get("/neo").text,
                 client.get(f"/neo/question/{q['id']}").text):
        assert f'action="/neo/{q["id"]}/answer"' not in page
        assert "would reopen its work order" in page
        assert f'href="/wo/proj_a/{wo["id"]}#pending"' in page


def test_assumptions_are_numbered_to_match_the_timeline(client, daemon, project):
    """The timeline says "Assumption #2 recorded" and never the text"""
    wo = ops.create_work_order("proj_a", "risky change")
    daemon.tick()
    ops.assume(wo["id"], "assumed the API is v2")
    ops.assume(wo["id"], "assumed UTF-8 throughout")
    ops.finish(wo["id"], "done-ish")

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    assert "#1" in page and "#2" in page
    assert "Assumption #1 recorded" in page and "Assumption #2 recorded" in page
    # The text is written once, in the panel that asks for a decision on it
    assert page.count("assumed the API is v2") == 1


def test_a_reviewed_assumption_is_still_readable_by_its_number(client, daemon, project):
    """The timeline names an assumption and says nothing else about it"""
    wo = ops.create_work_order("proj_a", "risky change")
    daemon.tick()
    ops.assume(wo["id"], "assumed the API is v2")
    ops.finish(wo["id"], "done-ish")
    client.post(f"/wo/proj_a/{wo['id']}/review", data={"decision": "accept"})

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    assert "Assumption #1 recorded" in page
    assert "assumed the API is v2" in page
    assert "accepted" in page
    # ...and the decision is no longer being asked for
    assert "Accept all" not in page
    assert "Assumptions the worker recorded" in page


def test_the_page_says_which_assumptions_the_os_decided_and_which_you_did(client,
                                                                          project):
    """THE post-condition of auto-review, on the surface somebody actually reads"""
    from jarvis.project_store import ASSUMPTION_DECIDER_OS, ASSUMPTION_DECIDER_USER

    wo = ops.create_work_order("proj_a", "task with two judgement calls")
    store = ProjectStore(project)
    mine = store.add_assumption(wo["id"], "picked postgres over sqlite")
    theirs = store.add_assumption(wo["id"], "named the helper `_render_row`")
    store.review_assumption(theirs, "accepted", decided_by=ASSUMPTION_DECIDER_OS,
                            reason="a naming convention, not a decision",
                            model="claude-opus-5", config_version="cfg-0007")
    store.review_assumption(mine, "accepted", decided_by=ASSUMPTION_DECIDER_USER,
                            reason="fine")
    store.set_status(wo["id"], "needs_review")

    # Whitespace-normalised: the claim is about the WORDS on the badge
    page = " ".join(client.get(f"/wo/proj_a/{wo['id']}").text.split())
    assert "accepted by the OS (neo, claude-opus-5)" in page
    assert "accepted by you" in page
    # Not just "both phrases appear somewhere": the model is named on the OS's row
    assert page.count("the OS (neo, claude-opus-5)") == 1
    assert page.count("claude-opus-5") == 1
    # ...and the reason the OS gave is on the row it decided (#712)
    assert "a naming convention, not a decision" in page
    assert 'title="a naming convention, not a decision"' not in page


def test_a_pending_assumption_says_what_the_os_did_with_it(client, project):
    """THE DEFECT of issue #712: an assumption Neo had escalated and one the keyword net held both..."""
    wo = ops.create_work_order("proj_a", "task with three judgement calls")
    store = ProjectStore(project)
    escalated = store.add_assumption(wo["id"], "changed the CLI's default output")
    held = store.add_assumption(wo["id"], "reused the production api key")
    store.add_assumption(wo["id"], "nobody has looked at this one")
    store.add_event(wo["id"], "autoreview_escalated", {
        "assumption_id": escalated, "n": 1, "neo_question_id": 47,
        "reason": "this is a surface others call"})
    store.add_event(wo["id"], "autoreview_held", {
        "assumption_id": held, "n": 2, "code": "high_stakes",
        "reason": "assumption #2 mentions 'production' — the OS does not decide those"})
    store.set_status(wo["id"], "needs_review")

    # Unescaped: the quoted marker is the readable half of a hold and Jinja spells it `&#39;`
    page = html.unescape(" ".join(client.get(f"/wo/proj_a/{wo['id']}").text.split()))

    assert "Neo escalated (question 47): this is a surface others call" in page
    assert '/neo/question/47' in page
    assert "Held by the OS — mentions 'production'" in page
    # The third assumption grows nothing: silence and a hold are different facts
    assert page.count("Held by the OS") == 1 and page.count("Neo escalated") == 1


def test_the_page_and_jarvis_wo_show_cannot_disagree_about_who_decided(client, project):
    """One renderer, asserted as one renderer"""
    from jarvis.project_store import ASSUMPTION_DECIDER_OS

    wo = ops.create_work_order("proj_a", "task with a judgement call")
    store = ProjectStore(project)
    a = store.add_assumption(wo["id"], "reused the existing exporter")
    store.review_assumption(a, "accepted", decided_by=ASSUMPTION_DECIDER_OS,
                            reason="no new surface", model="claude-opus-5")
    store.set_status(wo["id"], "needs_review")

    row = store.all_assumptions(wo["id"])[0]
    phrase = ops.assumption_decider(row)
    assert phrase == "the OS (neo, claude-opus-5)"
    assert phrase in client.get(f"/wo/proj_a/{wo['id']}").text
    assert phrase in ops.assumption_line(row)


def test_a_model_the_os_did_not_record_is_said_so_rather_than_left_blank(client,
                                                                        project):
    """A row written by an older OS, or by a path that forgot to pass the model"""
    from jarvis.project_store import ASSUMPTION_DECIDER_OS

    wo = ops.create_work_order("proj_a", "task with a judgement call")
    store = ProjectStore(project)
    a = store.add_assumption(wo["id"], "kept the default timeout")
    store.review_assumption(a, "accepted", decided_by=ASSUMPTION_DECIDER_OS,
                            reason="unchanged behaviour")
    store.set_status(wo["id"], "needs_review")

    assert "the OS (neo, model not recorded)" in client.get(
        f"/wo/proj_a/{wo['id']}").text


def test_the_auto_review_line_shows_before_there_is_a_pull_request(client, project):
    """Auto-review is not a fact about a pull request: it fires on `needs_review`"""
    wo = ops.create_work_order("proj_a", "task with a risky judgement call")
    store = ProjectStore(project)
    store.add_assumption(wo["id"], "reused the production API key")
    store.set_status(wo["id"], "needs_review")
    store.add_event(wo["id"], "autoreview_held",
                    {"assumption_id": 1, "n": 1, "code": "high_stakes",
                     "reason": "assumption #1 mentions 'production API key' — the OS "
                               "does not decide those for you"})

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    assert not store.get_work_order(wo["id"])["pr_url"]
    assert "auto-review" in page
    assert "does not decide those for you" in page


def test_the_banner_stops_claiming_a_decision_is_pending(client, project):
    """docs/superpowers/specs/2026-09-25-a-decided-assumption-is-not-left-with-you.md: the escalation..."""
    wo = ops.create_work_order("proj_a", "task with a judgement call")
    store = ProjectStore(project)
    aid = store.add_assumption(wo["id"], "changed the CLI's default output")
    store.add_event(wo["id"], "autoreview_escalated", {
        "assumption_id": aid, "n": 1, "reason": "this is a surface others call"})
    store.set_status(wo["id"], "needs_review")

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    assert "left with you" in page
    assert "since accepted" not in page

    store.review_assumption(aid, "accepted", reason="fine")

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    assert "left with you" in page
    assert "since accepted by you" in page


def test_wo_show_carries_every_assumption_with_its_number(jarvis_home, fake_claude,
                                                          catalog_file, capsys):
    """`jarvis wo show` speaks the same record as the page, for the same reason"""
    import json as _json

    from jarvis import cli

    ops.start_os(str(catalog_file), foreground=True)
    wo = ops.create_work_order("proj_a", "risky change")
    ops.assume(wo["id"], "assumed the API is v2")
    ops.assume(wo["id"], "assumed UTF-8 throughout")
    ops.review_work_order(wo["id"], accept=True)

    capsys.readouterr()
    cli.main(["wo", "show", wo["id"], "--json"])
    detail = _json.loads(capsys.readouterr().out)
    assert [(a["n"], a["status"]) for a in detail["assumptions"]] == [
        (1, "accepted"), (2, "accepted")]


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
    """A Claude session the user started in the project directory"""
    import json
    sessions = fake_claude.sessions
    sessions.append({"id": "zz", "sessionId": sid, "cwd": str(project),
                     "kind": "background", "name": name,
                     "state": state, "startedAt": 0})
    (fake_claude.dir / "sessions.json").write_text(json.dumps(sessions))


def test_your_own_sessions_are_offered_for_injection(client, daemon, fake_claude,
                                                     project):
    """The dashboard affordance for GitHub issue 47: the project page offers to take a session the user..."""
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
    """It shells out to `claude agents --json` — the one thing on any page that does"""
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
    # The user handed it over: `manual`, under its own project (wo-776ac037)
    assert '<span class="badge framework">proj_a</span>' in r.text
    assert ">manual</span>" in r.text


def test_inbox_page_and_ack(client, daemon, project):
    store = ProjectStore(project)
    store.add_notification("disk almost full", level="critical")
    daemon.tick()
    r = client.get("/inbox")
    assert "disk almost full" in r.text and "critical" in r.text
    client.post("/inbox/ack", data={})
    assert "inbox empty" in client.get("/inbox").text


def test_inbox_filters_sort_and_ack_shown(client, daemon, project):
    store = ProjectStore(project)
    store.add_notification("low news", level="info")
    store.add_notification("disk almost full", level="critical")
    store.add_notification("quota near", level="warning")
    daemon.tick()
    text = client.get("/inbox").text
    assert text.index("disk almost full") < text.index("quota near") < text.index("low news")
    text = client.get("/inbox?level=warning").text
    assert "quota near" in text and "disk almost full" not in text and "1 of 3 shown" in text
    text = client.get("/inbox?q=DISK").text
    assert "disk almost full" in text and "low news" not in text
    assert "nothing matches" in client.get("/inbox?project=nope").text
    central = CentralStore()
    shown = [it["id"] for it in ops.select_inbox(central.unacked_inbox(), level="critical")]
    central.close()
    r = client.post("/inbox/ack", data={"shown": ",".join(map(str, shown)),
                                        "next": "/inbox?level=critical"})
    assert r.headers["location"] == "/inbox?level=critical"
    text = client.get("/inbox").text
    assert "disk almost full" not in text and "low news" in text


def test_the_list_page_primitives_filter_and_order_rows():
    from starlette.requests import Request

    from jarvis.ui.app import attention_first, filter_options, filter_rows, query_filters
    q = "level=bogus&q=%20" + "x" * 300 + "&project=&other=1"
    req = Request({"type": "http", "query_string": q.encode()})
    got = query_filters(req, {"level": ["info"], "q": [], "project": ["a"]})
    # an unknown value is kept so it matches nothing; free text is capped; empty is absent
    assert got == {"level": "bogus", "q": "x" * 100}
    rows = [{"id": 1, "level": "info", "t": "Disk full"}, {"id": 2, "level": "crit", "t": "ok"},
            {"id": 3, "level": "info", "t": None}, {"id": 4, "level": "crit", "t": "disk"}]
    assert filter_rows(rows, {"level": "bogus"}) == []
    assert [r["id"] for r in filter_rows(rows, {"level": "info", "q": "DISK"}, ("t",))] == [1]
    assert filter_options(rows + [{"level": ""}], "level") == ["crit", "info"]
    assert [r["id"] for r in attention_first(rows, lambda r: r["level"] == "crit")] == [2, 4, 1, 3]


def test_dashboard_clears_unacked_banner_after_ack(client, daemon, project):
    """Acking every inbox item must clear the "unacked notification" banner on the main dashboard"""
    store = ProjectStore(project)
    store.add_notification("disk almost full", level="critical")
    daemon.tick()

    dash = client.get("/").text
    assert "unacked notification" in dash

    client.post("/inbox/ack", data={})

    dash_after = client.get("/").text
    assert "unacked notification" not in dash_after


def test_dashboard_is_never_cached(client):
    """The dashboard state (unacked count"""
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


def test_work_order_page_lists_its_learnings(client):
    """wo-9f943f10: the knowledge an order wrote or retired, as a rail section"""
    from jarvis.project_store import ProjectStore
    ps = ProjectStore(ops.registered_project_paths()["proj_a"])
    try:
        wo = ps.create_work_order("learn things")
        other = ps.create_work_order("another learner")
        quiet = ps.create_work_order("quiet one")
    finally:
        ps.close()
    central = CentralStore()
    wrote = central.add_knowledge("held panels hide stuck orders", project="proj_a",
                                  topic="ui", wo_id=wo["id"])
    old = central.add_knowledge("stale lesson", project="proj_a")
    central.retract_knowledge(old["id"], "superseded", wo_id=wo["id"])
    central.add_knowledge("someone else's lesson", project="proj_a", wo_id=other["id"])

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    assert '<h2 id="learnings">Learnings</h2>' in page
    assert f'href="/knowledge/{wrote["id"]}"' in page and "held panels" in page
    assert "retired by this order: superseded" in page
    assert "someone else" not in page

    assert "This order wrote no learnings." not in page
    assert "This order wrote no learnings." in client.get(f"/wo/proj_a/{quiet['id']}").text


def test_knowledge_page_filters_by_topic_and_words(client):
    """Topics were a flat run of text: no link, no way to narrow 800 entries to one topic"""
    central = CentralStore()
    central.add_knowledge("prefer uv for python installs", project="", topic="tooling")
    central.add_knowledge("gates block pr merges", project="", topic="gates")
    central.add_knowledge("untopicked lesson", project="")

    page = client.get("/knowledge").text
    assert 'href="/knowledge?topic=tooling"' in page
    assert '<select name="topic"' in page and '<option value="tooling"' in page

    page = client.get("/knowledge?topic=tooling").text
    assert "prefer uv" in page and "gates block" not in page
    assert "untopicked" in client.get("/knowledge?topic=").text   # empty form field: every topic
    page = client.get("/knowledge?topic=(none)").text
    assert "untopicked" in page and "prefer uv" not in page

    page = client.get("/knowledge?q=merges").text
    assert "gates block" in page and "prefer uv" not in page


def test_knowledge_page_reports_what_the_base_costs_and_who_reads_it(client):
    """The page used to answer "what do we know" and nothing about what that is worth: no cost"""
    central = CentralStore()
    row = central.add_knowledge("A LESSON WITH A FIRST LINE LONG ENOUGH TO BE CUT. "
                                + "padding " * 60, project="proj_a", topic="tooling")
    central.record_knowledge_read("show", [row], project="proj_a", wo_id="wo-x")

    page = client.get("/knowledge").text
    assert "what it costs a prompt" in page.lower() and "who reads it" in page.lower()
    assert "…" in page                       # the index line, truncated as a worker sees it
    assert "1 reads" in page or ">1<" in page  # the read just recorded is counted


def test_api_status(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["projects"][0]["name"] == "proj_a"


def test_unknown_project_and_wo(client):
    assert "unknown project" in client.get("/project/nope").text
    assert "not found" in client.get("/wo/proj_a/wo-nope").text


def test_neo_tab_lists_questions_still_with_neo(client, daemon, project):
    """A queued question used to appear only as a number in the counts line"""
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], "CSV or JSON?")

    r = client.get("/neo")
    assert r.status_code == 200
    assert "With Neo right now" in r.text  # the block, not the count line it replaced
    assert "CSV or JSON?" in r.text          # the question itself, not just a count
    assert f"/wo/proj_a/{wo['id']}" in r.text  # traceable back to the parked worker


def test_neo_tab_shows_a_question_stuck_mid_answer(client, daemon, project):
    """`claim_next` flips a question to `answering` and only a completed drain moves it on"""
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


#: A question of the shape that made this necessary (#53)
LONG_QUESTION = ("Should the exporter emit CSV or JSON, given the constraints below? "
                 * 25)


def test_neo_tab_shortens_an_over_long_question_and_keeps_the_full_text_one_click_away(
        client, daemon, project):
    """The whole feature, end to end, with its control"""
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
    # THE CONTROL: the verbatim question is on the page, inside the disclosure
    assert "Full question context sent to Neo" in page
    body = page.split("Full question context sent to Neo", 1)[1]
    assert LONG_QUESTION.strip() in body
    assert f"Work order: {wo['id']}" in body          # the prompt Neo got, not just the text


def test_neo_tab_shows_a_short_question_in_full_and_still_offers_the_disclosure(
        client, daemon, project):
    """No digest, no loss: a question under the threshold renders exactly as it did before this existed"""
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
    """A recorded digest failure must render as today's page, not as a blank box"""
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
    """The deliberation is on the page the user already reads, folded shut"""
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
    """Unlabelled, a learning only one seat reads would display as a rule the whole of Neo follows"""
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


def test_neo_tab_shows_where_a_learning_came_from(client):
    """A ruling with no visible origin cannot be judged stale before it is retracted"""
    from jarvis.neo_store import NeoStore

    neo = NeoStore()
    try:
        neo.add_learning("RULE: squash merges only", project="proj_a", source="review",
                         wo_id="wo-1234abcd", origin_kind="assumption", origin_id="7")
        neo.add_learning("Prefer uv over pip")
    finally:
        neo.close()
    page = client.get("/neo").text
    assert "from wo-1234abcd assumption 7" in page
    # the control: a row with no origin shows no 'from' label
    assert page.count("from wo-") == 1


def test_neo_teach_directly(client):
    r = client.post("/neo/learn", data={"content": "prefer uv over pip", "project": ""})
    assert r.status_code == 303
    page = client.get("/neo")
    assert "prefer uv over pip" in page.text


def test_gates_tab_is_empty_and_unbadged_when_nothing_is_gated(client):
    r = client.get("/gates")
    assert r.status_code == 200
    assert "no privileged action has been gated yet" in r.text
    assert 'class="attention hot needs-you"' not in r.text
    # an ungated fleet must not grow a nav badge asking for attention it doesn't need
    assert 'href="/gates"' in r.text
    assert '<span class="nav-badge">' not in r.text  # the CSS rule always matches


def test_gates_tab_shows_the_request_the_reviewer_saw(gated):
    """Approving a bare command string is not a review"""
    approval = gated.request(why="all tests pass, PR is merged")
    gated.daemon._neo_drain()  # the fake model escalates by default

    r = gated.client.get("/gates")
    assert r.status_code == 200
    assert "waiting on you — Neo escalated" in r.text
    assert "./scripts/shipit.sh" in r.text
    assert f'href="/gates/{approval["id"]}?project=proj_a"' in r.text
    assert '<span class="nav-badge">1</span>' in r.text  # escalated ⇒ the tab is badged

    # the case and the request as Neo read it live on the gate's own page
    detail = gated.client.get(f"/gates/{approval['id']}").text
    assert "all tests pass, PR is merged" in detail
    assert "the request exactly as the reviewer saw it" in detail
    assert "PRIVILEGED ACTION REQUEST" in detail or "cut a release" in detail
    assert 'value="approve"' in detail


def test_gates_tab_explains_a_request_held_for_the_worker_s_case(gated):
    """`awaiting_case` used to render as a badge word under "Decided" and nothing else"""
    from jarvis.hooks import preflight_decision

    # The attempt alone is what holds it: no case, no reviewer (gates.AWAITING_CASE)
    settings = json.loads(
        (gated.project / ".jarvis" / "worker-settings"
         / f"{gated.wo_id}.json").read_text())
    preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": gated.gated_command},
         "cwd": str(gated.project)}, settings["env"])
    assert gated.approval()["status"] == gates.AWAITING_CASE

    page = gated.client.get("/gates").text

    assert "Awaiting the worker's case" in page
    assert "jarvis gate request" in page
    # BOTH exits, here as everywhere a blocked worker's row is explained (spec §3)
    assert "jarvis gate contest" in page
    assert "gates.case_ttl_seconds" in page
    assert "awaiting case" in page and "abandoned in" in page
    # not filed among the verdicts
    assert ">approved<" not in page and ">denied<" not in page
    # and it asks the user for nothing
    assert '<span class="nav-badge">' not in page

    detail = gated.client.get(f"/gates/{gated.approval()['id']}").text
    assert "no reviewer sees it until the worker makes its case" in detail
    # Not "Refused": the TTL abandons, and saying refused would promise a reviewer who never looked
    assert "abandoned automatically in" in detail
    assert "efused automatically" not in detail
    assert 'name="decision"' not in detail


def test_gate_pending_with_neo_costs_no_badge(gated):
    """A request Neo is still reviewing is free by design"""
    gated.request()
    r = gated.client.get("/gates")
    assert "with neo" in r.text
    assert "./scripts/shipit.sh" in r.text
    assert 'class="attention hot needs-you"' not in r.text
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

    assert "approved" in gated.client.get("/gates").text
    page = gated.client.get(f"/gates/{approval['id']}").text
    assert "approved by" in page and "I checked it myself" in page


def test_deny_from_the_dashboard_needs_a_reason(gated):
    """ops.decide_gate refuses a reasonless denial — the worker acts on that text"""
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
    """Deciding from a work order should land back on that work order"""
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
    """The third button"""
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
    # Listed apart from the verdicts, with the rate, and NOT counted as approved
    assert "Not gated actions — the OS got these wrong" in page
    assert "false-positive rate: 1 of 1 (100%)" in page
    assert "not a gate" in page and ">approved<" not in page


def test_dismiss_from_the_dashboard_needs_a_reason(gated):
    """The reason is the defect report on the recogniser"""
    approval = gated.request(command="uv run pytest tests/test_release_staging.py -k shipit")

    r = gated.client.post(f"/gates/{approval['id']}/decide",
                          data={"decision": "dismiss", "reason": " ",
                                "project": "proj_a", "next": "/gates"})
    from urllib.parse import unquote
    assert "needs a reason" in unquote(r.headers["location"])
    assert gated.approval()["status"] == "pending"


def test_a_mangled_decision_value_cannot_open_a_gate(gated):
    """`decision` is an attacker-settable form field"""
    approval = gated.request()

    gated.client.post(f"/gates/{approval['id']}/decide",
                      data={"decision": "approve​", "reason": "nice try",
                            "project": "proj_a", "next": "/gates"})

    assert gated.approval()["status"] == "denied"


def test_neo_tab_sends_gate_escalations_to_the_gates_tab(gated):
    """A gate escalation is a Neo question, so it also lands on the neo tab"""
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
    assert f'href="/gates/{approval["id"]}"' in r.text


def test_timeline_hides_plumbing_until_debug_is_requested(client, daemon, project):
    """The default timeline reads as a story"""
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
    """It would accept the assumptions silently"""
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
    """The complaint: a project page that lists every work order ever created is unreadable"""
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
    """The listing question the user actually asks the dashboard: what is moving"""
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
    """The statuses invariants.true_blockers calls real blockers have to be rows"""
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
    """The reported bug, exactly: counts and panel contradicting each other"""
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
    """Both listings, because they derive it by different routes"""
    dep = ops.create_work_order("proj_a", "the schema change")
    ops.create_work_order("proj_a", "needs the schema", depends_on=[dep["id"]])

    # Pending work still collapses by default
    for url in ("/?show=pending", "/project/proj_a?show=pending"):
        page = client.get(url)
        assert "blocked by" in page.text, url
        assert dep["id"] in page.text, url


def test_a_stranded_work_order_reaches_the_attention_strip(client, project):
    """The one dependency case that is not routine: it will never start on its own"""
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
    """It is a merge queue, not an interrupt: NEEDS YOU has to stay for real blockers"""
    wo = ops.create_work_order("proj_a", "waiting on a merge")
    ops.finish(wo["id"], "PR is up", pr_url="https://github.com/a/b/pull/9")

    page = client.get("/")
    assert "all quiet" in page.text
    assert "NEEDS YOU" not in page.text


def test_expanding_a_settled_group_keeps_hidden_work_orders_showing(client, project):
    """Two independent toggles: expanding one must not silently undo the other"""
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
    """The dashboard complaint in one test: acking has to survive the daemon"""
    wo = ops.create_work_order("proj_a", "noisy task")
    store = ProjectStore(project)
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], IDLE_NO_FINISH_BLOCKER)
    assert "NEEDS YOU" in client.get("/").text

    detail = client.get(f"/wo/proj_a/{wo['id']}")
    assert "Got it" in detail.text
    r = client.post(f"/wo/proj_a/{wo['id']}/ack")
    assert r.status_code == 303

    check_project(ProjectStore(project), repair=True)  # the daemon's next tick
    assert "all quiet" in client.get("/").text


def test_got_it_is_not_offered_when_a_decision_is_owed(client, project):
    """Assumptions need accept/reject — dismissing one would drop the work silently"""
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
    """Notifications embed a deep link built from whatever project name the emitter passed"""
    r = client.get("/wo/proj_gone/wo-4fdb20ba")
    # 404, not 200: there is genuinely no such page
    assert r.status_code == 404
    assert "Internal Server Error" not in r.text
    assert "proj_gone" in r.text and "not registered" in r.text
    assert "never registered, or it has since been removed" in r.text


def test_stale_deep_link_to_a_deleted_work_order_says_which_half_is_gone(client):
    """The other stale-link shape: the project is fine"""
    r = client.get("/wo/proj_a/wo-neverexisted")
    assert r.status_code == 404
    assert "is registered, but it has no work order" in r.text


def test_unexpected_error_renders_a_page_and_lands_in_the_os_log(
        jarvis_home, fake_claude, catalog_file, monkeypatch):
    """Defence in depth: whatever else breaks"""
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


# -- feature orders


@pytest.fixture()
def feature(client, daemon, project):
    """A feature order whose plan is submitted and escalated to the user"""
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
    """The planner sees only the reason, so a bare rejection is a guess"""
    client, fo = feature

    res = client.post(f"/fo/proj_a/{fo['id']}/review", data={"decision": "reject"})

    assert res.status_code == 303
    assert "error=" in res.headers["location"]
    assert ops.show_feature_order(fo["id"])["status"] == "plan_review"


def test_a_plan_neo_can_decide_never_reaches_the_users_page(client, daemon, project):
    """The control for the panel above: routine plans must not put a decision on screen"""
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
    # The children belong to the listing below as ordinary work orders
    assert page.count(f"/fo/proj_a/{fo['id']}") == 1


# -- the spec page


@pytest.fixture()
def released_feature(client, daemon, project):
    """A feature whose plan was released, so planner, feature and children all exist"""
    from tests.test_feature_orders import ASK, a_plan, child

    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE"),
                                     child("api", needs=["schema"])))
    daemon._neo_drain()
    return client, ops.show_feature_order(fo["id"])


def test_the_spec_page_renders_a_held_document_with_no_pull_request(released_feature):
    """Spec test 1 — wo-1170d758's shape, the one that must not regress"""
    client, fo = released_feature
    assert not any(c["pr_url"] for c in fo["children"])

    page = client.get(f"/spec/proj_a/{fo['id']}")

    assert page.status_code == 200
    assert "Failure handling" in page.text          # a heading from the document
    assert 'id="3-failure-handling"' in page.text   # rendered, not printed as a path


def test_the_spec_page_prints_the_revision_it_holds_and_never_invents_one(
        released_feature, project):
    """Spec test 7, second half"""
    client, fo = released_feature

    page = client.get(f"/spec/proj_a/{fo['id']}").text
    assert html.escape(fo["plan"]["design_doc_source"]) in page

    store = ProjectStore(project)
    try:
        plan = dict(fo["plan"])
        plan.pop("design_doc_source")
        store.update_feature_order(fo["id"], plan=json.dumps(plan))
    finally:
        store.close()

    older = client.get(f"/spec/proj_a/{fo['id']}").text
    assert "the revision this text came from was not recorded" in older
    assert "origin/main" not in older


def test_a_feature_with_no_plan_is_200_and_names_its_status(client, daemon):
    """Spec test 7, first half: three outcomes, none of them a 500"""
    from tests.test_feature_orders import ASK

    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()

    page = client.get(f"/spec/proj_a/{fo['id']}")

    assert page.status_code == 200
    assert "planning" in page.text
    assert f"/fo/proj_a/{fo['id']}" in page.text


def test_an_unknown_project_spec_is_the_ordinary_error_page(client):
    page = client.get("/spec/no_such_project/fo-1234")

    assert page.status_code == 200
    assert "no_such_project" in page.text


def test_three_pages_link_to_the_one_spec_page(released_feature):
    """Spec test 3: the planner's page, the feature page and a child's page"""
    client, fo = released_feature
    link = f"/spec/proj_a/{fo['id']}"

    assert link in client.get(f"/fo/proj_a/{fo['id']}").text
    assert link in client.get(f"/wo/proj_a/{fo['planner']['id']}").text
    kid = fo["children"][0]["id"]
    assert link in client.get(f"/wo/proj_a/{kid}").text


def test_a_childs_link_carries_its_sections_anchor_and_that_id_exists(released_feature,
                                                                      project):
    """Spec test 4 — assert the PAIR"""
    from jarvis import specs

    client, fo = released_feature
    store = ProjectStore(project)
    try:
        kid = store.feature_children(fo["id"], kinds=("worker",))[0]
        anchor = specs.spec_link(store, kid)["anchor"]
    finally:
        store.close()
    assert anchor

    fragment = f"/spec/proj_a/{fo['id']}#{anchor}"
    assert fragment in client.get(f"/wo/proj_a/{kid['id']}").text
    # And the plan's child row on the feature page links to the same fragment
    assert fragment in client.get(f"/fo/proj_a/{fo['id']}").text
    assert f'id="{anchor}"' in client.get(f"/spec/proj_a/{fo['id']}").text


# -- improvement orders


@pytest.fixture()
def improvement(client, project, improvement_order, user_decides_findings):
    """An improvement order in `plan_review` with the shared findings report on it, the user's to decide"""
    return _reported(client, project, improvement_order)


def _reported(client, project, improvement_order):
    from jarvis.testing import a_report

    store = ProjectStore(project)
    try:
        analyst = store.create_work_order("analyse it", description="the observation",
                                          kind="planner", status="running")
        store.update_feature_order(improvement_order["id"], plan_wo_id=analyst["id"],
                                   status="planning")
    finally:
        store.close()
    ops.submit_findings(improvement_order["id"], a_report())
    return client, ops.show_improvement_order(improvement_order["id"]), analyst


def test_the_improvement_page_opens_on_counts_then_evidence_and_the_analyst(improvement):
    client, io, analyst = improvement

    page = client.get(f"/io/proj_a/{io['id']}").text

    assert "2 findings" in page and "2 pending" in page
    assert "Three work orders in a row spent their first turn" in page  # observation
    for ref in ("wo-11111111", "#42", "https://example.invalid/x"):
        assert html.escape(ref) in page
    assert f"/wo/proj_a/{analyst['id']}" in page
    # Its LIVE status, not the one it was created with: `submit_findings` settles the analyst
    assert io["analyst"]["status"] in page


def test_a_finding_card_shows_all_five_fields_and_its_proposed_orders(improvement):
    client, io, _ = improvement

    page = client.get(f"/io/proj_a/{io['id']}").text

    assert "first-turn-reads" in page
    assert "before touching the dispatch path" in page             # symptom
    assert "so every worker rediscovers the module layout" in page  # root_cause
    assert "leaves every worker still reading the same files" in page  # why_insufficient
    assert "generated from the committed code map" in page         # recommendation
    assert "41 tool calls, 38 of them Read" in page                # evidence
    assert "name the entry point in every dispatch brief" in page  # proposed order


def test_an_accepted_finding_lists_the_orders_it_filed(improvement):
    client, io, _ = improvement
    ops.review_findings(io["id"], accept=("first-turn-reads",), project_name="proj_a")

    detail = ops.show_improvement_order(io["id"])
    filed = detail["filed_orders"]["first-turn-reads"]
    page = client.get(f"/io/proj_a/{io['id']}").text

    assert filed, "the accepted finding filed nothing to link to"
    for order in filed:
        link = f"/wo/proj_a/{order['id']}"
        assert link in page
        # The status has to ride on the filed order's OWN row: an accepted finding that says what was...
        row = page[page.index(link):page.index(link) + 400]
        assert order["status"] in row


def test_dispatched_orders_lead_the_io_page_and_the_analyst_page(improvement, project):
    """Issue 1323: what the findings dispatched, up front, on the io AND its analyst"""
    client, io, _ = improvement
    ops.review_findings(io["id"], accept=("first-turn-reads",), project_name="proj_a")
    filed = ops.show_improvement_order(io["id"])["dispatched"]["orders"]
    store = ProjectStore(project)
    try:
        store.update_work_order(filed[0]["id"], pr_url="https://github.com/o/r/pull/1306")
        analyst = store.create_work_order("analyse it", kind="analyst",
                                          parent_id=io["id"], status="completed")
    finally:
        store.close()

    page = client.get(f"/io/proj_a/{io['id']}").text
    head = page[:page.index('id="findings"')]
    assert f"{len(filed)} dispatched, 0 merged" in head
    assert f"/wo/proj_a/{filed[0]['id']}" in head and "PR #1306" in head

    sibling = client.get(f"/wo/proj_a/{analyst['id']}").text
    assert f"its findings dispatched ({io['id']})" in sibling
    assert f"/wo/proj_a/{filed[0]['id']}" in sibling


def test_a_finding_with_neo_says_so_and_a_decided_one_shows_neos_reading(
        client, project, improvement_order):
    client, io, _ = _reported(client, project, improvement_order)

    page = client.get(f"/io/proj_a/{io['id']}").text
    assert "2 with Neo" in page and ">with Neo<" in page

    report = io["report"]
    q = {"id": report["findings"][0]["neo_question_id"], "project": "proj_a",
         "wo_id": io["plan_wo_id"]}
    ops.apply_findings_verdict(q, {"escalate": False, "approve": False,
                                   "severity": "medium",
                                   "reason": "the evidence shows no mechanism"})
    page = client.get(f"/io/proj_a/{io['id']}").text
    assert "rejected by neo" in page
    assert "the evidence shows no mechanism" in page and "rated medium" in page


def test_a_pending_finding_carries_both_decisions_and_a_required_reason(improvement):
    client, io, _ = improvement

    page = client.get(f"/io/proj_a/{io['id']}").text

    assert f"/io/proj_a/{io['id']}/review" in page
    assert 'value="accept"' in page and 'value="reject"' in page
    textarea = page[page.index("<textarea"):page.index("</textarea>")]
    assert "required" in textarea


def test_accepting_a_finding_from_the_browser_decides_it(improvement):
    client, io, _ = improvement

    res = client.post(f"/io/proj_a/{io['id']}/review",
                      data={"key": "first-turn-reads", "decision": "accept",
                            "reason": ""})

    assert res.status_code == 303
    assert ops.show_improvement_order(io["id"])["by_decision"]["accepted"] == 1


def test_rejecting_a_finding_with_no_reason_says_so_instead_of_failing(improvement):
    """The reason is what the knowledge entry and the analyst read"""
    client, io, _ = improvement

    res = client.post(f"/io/proj_a/{io['id']}/review",
                      data={"key": "first-turn-reads", "decision": "reject",
                            "reason": ""})

    assert res.status_code == 303
    assert "error=" in res.headers["location"]
    assert ops.show_improvement_order(io["id"])["by_decision"]["pending"] == 2
    page = client.get(res.headers["location"]).text
    assert "the entire teaching signal" in page   # ops' own refusal, on the page


def test_the_project_page_lists_improvement_orders_without_expanding_findings(improvement):
    client, io, _ = improvement

    page = client.get("/project/proj_a").text

    assert page.count(f"/io/proj_a/{io['id']}") == 1
    assert "Improvement orders" in page
    assert "findings awaiting you" in page   # the kind-aware label, not `plan_review`
    # Counts, not trees: the findings are what the order's own page is for
    assert "before touching the dispatch path" not in page


# -- investigation orders


@pytest.fixture()
def investigation(client, project):
    """A settled investigation with a WAITING_ON_USER verdict on it"""
    from jarvis.testing import a_verdict

    store = ProjectStore(project)
    try:
        subject = store.create_work_order("ship the CSV export")["id"]
    finally:
        store.close()
    inv = ops.create_investigation_order(
        "proj_a", subject,
        "wo-x has been `validating` for six hours with no turn in flight.")
    store = ProjectStore(project)
    try:
        investigator = store.create_work_order(f"investigate {subject}",
                                               kind="investigator",
                                               parent_id=inv["id"], status="running")
        store.update_feature_order(inv["id"], plan_wo_id=investigator["id"])
        store.set_feature_status(inv["id"], "planning")
    finally:
        store.close()
    ops.submit_verdict(inv["id"], a_verdict("WAITING_ON_USER", subject=subject))
    return client, ops.show_investigation_order(inv["id"]), investigator, subject


def test_the_investigation_page_leads_with_the_classification_and_the_subject(
        investigation):
    client, inv, investigator, subject = investigation

    page = client.get(f"/inv/proj_a/{inv['id']}").text

    assert "WAITING_ON_USER" in page
    assert subject in page
    assert "is written once and never" in page          # the root cause
    assert "the panel gave up after 3 rounds" in page   # the evidence quote
    assert html.escape(f"jarvis wo show {subject}") in page   # its source
    assert "as-4" in page                               # what the user owes
    assert f"/wo/proj_a/{investigator['id']}" in page
    # Nothing to decide: the difference from the improvement-order page
    assert 'method="post"' not in page


def test_the_investigation_page_says_what_was_filed(client, project):
    from jarvis.testing import a_verdict

    store = ProjectStore(project)
    try:
        subject = store.create_work_order("ship the CSV export")["id"]
    finally:
        store.close()
    inv = ops.create_investigation_order("proj_a", subject, "it has not moved in 6h.")
    store = ProjectStore(project)
    try:
        child = store.create_work_order("investigate it", kind="investigator",
                                        parent_id=inv["id"])
        store.update_feature_order(inv["id"], plan_wo_id=child["id"])
        store.set_feature_status(inv["id"], "planning")
    finally:
        store.close()
    out = ops.submit_verdict(inv["id"], a_verdict("ALREADY_TRACKED", subject=subject))

    page = client.get(f"/inv/proj_a/{inv['id']}").text
    assert "ALREADY_TRACKED" in page
    assert "#790" in page          # the duplicate it matched
    assert out["filed"] is None


def test_an_unknown_investigation_says_so_instead_of_failing(client):
    page = client.get("/inv/proj_a/inv-nosuchid")
    assert page.status_code == 200
    assert "inv-nosuchid" in page.text


def test_the_project_page_lists_live_investigations_beside_the_other_kinds(client,
                                                                          project):
    store = ProjectStore(project)
    try:
        subject = store.create_work_order("ship the CSV export")["id"]
    finally:
        store.close()
    inv = ops.create_investigation_order("proj_a", subject, "no turn in six hours.")

    page = client.get("/project/proj_a").text

    assert page.count(f"/inv/proj_a/{inv['id']}") == 1
    assert "Investigations" in page
    assert subject in page   # the subject is what the line is about


def _settle_feature(project, title, status):
    """A feature order in a terminal status, with no ceremony about how it got there"""
    fo = ops.create_feature_order("proj_a", title, description="the whole ask, at "
                                  "length, with enough detail to plan it properly")
    store = ProjectStore(project)
    try:
        store.update_feature_order(fo["id"], status=status)
    finally:
        store.close()
    return fo


def test_settled_feature_orders_collapse_into_a_count(client, project):
    """The complaint: a completed feature order left the project page and there was no way back to it"""
    done = _settle_feature(project, "CSV export", "completed")
    killed = _settle_feature(project, "PDF export", "cancelled")
    still_going = ops.create_feature_order("proj_a", "XLS export",
                                           description="the whole ask, at length, "
                                                       "with enough detail to plan it")

    page = client.get("/project/proj_a").text
    assert "XLS export" in page
    assert "CSV export" not in page and "PDF export" not in page
    # The gap was discoverability, not just filtering: the count says they exist
    assert "1 completed" in page and "1 cancelled" in page
    assert "fo=completed" in page
    assert "fo=failed" not in page  # zero of them: no count, no link

    just_completed = client.get("/project/proj_a?fo=completed").text
    assert "CSV export" in just_completed
    assert "PDF export" not in just_completed  # just that group
    assert "XLS export" in just_completed  # the open ones never leave

    everything = client.get("/project/proj_a?fo=all").text
    for fo in (done, killed, still_going):
        assert f"/fo/proj_a/{fo['id']}" in everything


def test_a_settled_feature_is_still_told_apart_from_an_open_one(client, project):
    """Revealed, it is a row like any other — wearing its own status, not an open one's"""
    _settle_feature(project, "CSV export", "failed")

    page = client.get("/project/proj_a?fo=all").text
    assert "Feature orders" in page
    assert "tone-bad" in page and "✗" in page


def test_the_work_order_reveal_and_the_feature_reveal_do_not_undo_each_other(client,
                                                                            project):
    """Two independent reveals on one page: every link must carry the other"""
    _settle_feature(project, "CSV export", "completed")
    finished = ops.create_work_order("proj_a", "old and finished")
    ops.mark_done(finished["id"])

    both = client.get("/project/proj_a?fo=all&show=all").text
    assert "CSV export" in both and "old and finished" in both
    assert "?fo=all&amp;show=completed" in both  # the work-order reveal keeps `fo`
    assert "?show=all&amp;fo=completed" in both  # and the feature reveal keeps `show`


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
    """`max_parallel` is invisible everywhere else"""
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
    # ...and the child that is waiting says so, rather than reading as about-to-start
    assert "waiting for a slot" in page


def test_an_uncapped_feature_page_says_nothing_about_slots(feature):
    """The control: a feature with no cap has nothing to report"""
    client, fo = feature

    assert "slots in use" not in client.get(f"/fo/proj_a/{fo['id']}").text


def test_the_attention_strip_links_a_rolled_up_feature_to_its_page(client, daemon,
                                                                   project):
    """The rollup collapses the children's lines"""
    from jarvis.project_store import ProjectStore
    from tests.test_feature_orders import ASK, a_plan, child

    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    ops.submit_plan(fo["id"], a_plan(child("one", extra="FORCE_APPROVE"),
                                     child("two", extra="FORCE_APPROVE")))
    daemon._neo_drain()
    store = ProjectStore(project)
    try:
        kid = store.feature_children(fo["id"], kinds=("worker",))[0]
        store.update_work_order(kid["id"], needs_attention=1,
                                attention_reason="assumption needs a decision")
    finally:
        store.close()

    page = client.get("/").text

    assert f'/fo/proj_a/{fo["id"]}' in page
    assert "1 of its work orders need you" in page
def test_knowledge_page_pin_toggle(client):
    """Pinning is the switch between 'in every worker prompt verbatim' and 'an index line the worker looks..."""
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


# -- which instance is this?

def _chrome(jarvis_home, catalog_file, path="/"):
    """The rendered header, from a freshly built app (the badge is read once)"""
    ops.start_os(str(catalog_file), foreground=True)
    c = TestClient(create_app(), follow_redirects=False)
    return c.get(path).text


def test_header_names_a_dev_instance_and_its_version(
        jarvis_home, fake_claude, catalog_file, monkeypatch):
    """Two checkouts of the same code on one machine render identical dashboards"""
    from jarvis import bugreport

    monkeypatch.delenv("JARVIS_ENV", raising=False)
    monkeypatch.setenv("PRODUCTION_CODE", str(jarvis_home / "not-production"))
    monkeypatch.setattr(bugreport, "jarvis_version", lambda: "dev-a1b2c3d")
    html = _chrome(jarvis_home, catalog_file)

    # a dev build has no release number — the sha is the version, and the badge does not say "dev" twice
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
    """`-dirty` means the production checkout is no longer the release it claims to be"""
    from jarvis import bugreport

    monkeypatch.setenv("JARVIS_ENV", "production")
    monkeypatch.setattr(bugreport, "jarvis_version", lambda: "0.5.0-dirty")
    html = _chrome(jarvis_home, catalog_file)

    assert "prod · v0.5.0-dirty" in html


def test_instance_badge_is_on_every_page_with_a_diagnosable_tooltip(
        jarvis_home, fake_claude, catalog_file, monkeypatch):
    """It lives in the base template, so it cannot go missing on the page someone happens to deep-link..."""
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


# -- units under validation


def test_a_validating_work_order_renders_everywhere_it_appears(client, project):
    store = ProjectStore(project)
    wo = store.create_work_order("ship the exporter")
    store.set_status(wo["id"], "validating")
    store.open_validation_round(wo_id=wo["id"], fingerprint="abc")

    for url in ("/", "/project/proj_a", f"/wo/proj_a/{wo['id']}"):
        res = client.get(url)
        assert res.status_code == 200, (url, res.status_code)
    assert "under review" in client.get(f"/wo/proj_a/{wo['id']}").text


def test_an_idle_manager_renders_everywhere_it_appears(client, project):
    """The third status added to the vocabulary, under the rule above (#264)"""
    store = ProjectStore(project)
    wo = store.create_work_order("coordinate the exporter", kind="manager")
    store.set_status(wo["id"], "idle")

    for url in ("/", "/project/proj_a", f"/wo/proj_a/{wo['id']}"):
        res = client.get(url)
        assert res.status_code == 200, (url, res.status_code)
    page = client.get(f"/wo/proj_a/{wo['id']}").text
    assert "waiting on you" not in page.lower()
    assert "idle until its feature sends it something" in page


def test_a_validating_feature_order_renders_everywhere_it_appears(client, project):
    store = ProjectStore(project)
    fo = store.create_feature_order("CSV export", description="the whole ask")
    store.set_feature_status(fo["id"], "validating")
    store.open_validation_round(fo_id=fo["id"], fingerprint="abc")

    for url in ("/", "/project/proj_a", f"/fo/proj_a/{fo['id']}"):
        res = client.get(url)
        assert res.status_code == 200, (url, res.status_code)
    assert "under review" in client.get(f"/fo/proj_a/{fo['id']}").text


# -- the validation section


def _validated(store, *, wo_id=None, fo_id=None):
    """A rejection and a pass, with the seats that produced them"""
    subject = {"wo_id": wo_id} if wo_id else {"fo_id": fo_id}
    first = store.open_validation_round(**subject, fingerprint="aaaa1111")
    store.close_validation_round(first["id"], "rejected", "no test touches the branch")
    store.record_validation_opinion(first["id"], "tester", verdict="reject",
                                    reply="no file under tests/ is in the diff",
                                    model="sonnet", latency_ms=1200)
    store.record_validation_opinion(first["id"], "security", verdict="pass",
                                    reply="nothing exposed", model="sonnet",
                                    latency_ms=900)
    second = store.open_validation_round(**subject, fingerprint="bbbb2222")
    store.close_validation_round(second["id"], "passed", "the new case covers it")


def test_the_work_order_page_shows_its_rounds_and_folds_the_seats_away(client, project):
    store = ProjectStore(project)
    wo = store.create_work_order("ship the exporter")
    _validated(store, wo_id=wo["id"])

    page = client.get(f"/wo/proj_a/{wo['id']}").text

    assert 'id="validation"' in page
    assert "aaaa1111" in page and "bbbb2222" in page
    assert "rejected" in page and "passed" in page
    assert "no test touches the branch" in page
    assert "tester" in page and "security" in page
    # Folded, not hidden: the seats are one click away and cost nobody a page of reading they did not ask...
    assert "Panel deliberation" in page
    assert "<details" in page
    assert "no file under tests/ is in the diff" in page


def test_the_feature_page_shows_its_rounds_and_folds_the_seats_away(client, project):
    store = ProjectStore(project)
    fo = store.create_feature_order("CSV export", description="the whole ask")
    _validated(store, fo_id=fo["id"])

    page = client.get(f"/fo/proj_a/{fo['id']}").text

    assert 'id="validation"' in page
    assert "aaaa1111" in page and "bbbb2222" in page
    assert "rejected" in page and "passed" in page
    assert "the new case covers it" in page
    assert "tester" in page and "security" in page
    assert "Panel deliberation" in page


def test_a_voided_round_reads_as_voided_and_not_as_the_raw_word(client, project):
    """Every outcome the machine can write needs a badge here"""
    store = ProjectStore(project)
    wo = store.create_work_order("ship 0.10.5")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="cccc3333")
    store.close_validation_round(rnd["id"], "void", "the OS verifies it itself")

    page = client.get(f"/wo/proj_a/{wo['id']}").text

    assert 'id="validation"' in page
    assert "voided</span>" in page
    assert "the OS verifies it itself" in page
    # The fall-through badge prints the raw outcome and colours it "in flight"
    assert ">void</span>" not in page


def test_a_round_waiting_for_ci_is_not_painted_as_a_failure(client, project):
    """GitHub issue #581"""
    store = ProjectStore(project)
    wo = store.create_work_order("add the export")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="dddd4444")
    store.close_validation_round(rnd["id"], "failed", "waiting for GitHub: unit (3.13)",
                                 hold_cause=VALIDATION_CI_CAUSE)

    page = client.get(f"/wo/proj_a/{wo['id']}").text

    assert 'class="st tone-active"><span class="i">◑</span>waiting for CI</span>' in page
    assert ">failed</span>" not in page
    # Not a bare `"tone-bad" not in page`: the stylesheet names every tone
    assert 'class="st tone-bad"' not in page, "a wait was toned as a failure"


def test_a_round_held_for_authentication_is_not_painted_as_a_failure(client, project):
    """GitHub issue #778"""
    from jarvis.project_store import VALIDATION_AUTH_CAUSE

    store = ProjectStore(project)
    wo = store.create_work_order("add the export")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="ffff6666")
    store.close_validation_round(rnd["id"], "failed",
                                 "waiting for Claude Code authentication",
                                 hold_cause=VALIDATION_AUTH_CAUSE)

    page = client.get(f"/wo/proj_a/{wo['id']}").text

    assert ('class="st tone-active"><span class="i">◑</span>held for authentication'
            '</span>') in page
    assert ">failed</span>" not in page
    assert 'class="st tone-bad"' not in page, "a wait was toned as a failure"


def test_a_round_that_really_failed_keeps_the_red_badge(client, project):
    """The other half, and the one a rendering fix breaks by accident: an outage and an unconfigured panel..."""
    store = ProjectStore(project)
    wo = store.create_work_order("add the import")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="eeee5555")
    store.close_validation_round(rnd["id"], "failed", "the validator could not be reached")

    page = client.get(f"/wo/proj_a/{wo['id']}").text

    assert 'class="st tone-bad"><span class="i">✗</span>failed</span>' in page
    assert "waiting for CI" not in page


def test_a_unit_that_was_never_validated_gets_no_validation_section(client, project):
    """No empty box"""
    store = ProjectStore(project)
    wo = store.create_work_order("ship the exporter")
    fo = store.create_feature_order("CSV export", description="the whole ask")

    assert 'id="validation"' not in client.get(f"/wo/proj_a/{wo['id']}").text
    assert 'id="validation"' not in client.get(f"/fo/proj_a/{fo['id']}").text


def test_the_work_order_page_reads_its_label_off_the_os_not_the_template(client,
                                                                        project):
    """`invariants.status_label` is where every surface gets its wording"""
    from jarvis import invariants

    store = ProjectStore(project)
    wo = store.create_work_order("ship the exporter")
    store.set_status(wo["id"], "validating")
    store.open_validation_round(wo_id=wo["id"], fingerprint="aaaa1111", round=1)
    store.open_validation_round(wo_id=wo["id"], fingerprint="bbbb2222", round=2)
    expected = invariants.status_label(store, store.get_work_order(wo["id"]))

    page = client.get(f"/wo/proj_a/{wo['id']}").text

    assert expected in page
    assert "review round 2 of 3" in expected      # the label really did say something


def test_an_undeliverable_envelope_is_visible_on_the_page_and_a_delivered_one_is_not(
        client, project):
    """Feedback that reached nobody must never read like feedback that was acted on"""
    store = ProjectStore(project)
    wo = store.create_work_order("ship the exporter")
    lost = store.post_envelope(from_role="reviewer", to_role="implementor",
                               kind="review_feedback", subject_wo_id=wo["id"])
    fine = store.post_envelope(from_role="reviewer", to_role="implementor",
                               kind="review_feedback", subject_wo_id=wo["id"])
    store.mark_envelope(lost, "undeliverable", note="nobody fills role implementor")
    store.mark_envelope(fine, "delivered", delivered_wo_id=wo["id"])

    page = client.get(f"/wo/proj_a/{wo['id']}").text

    assert 'id="validation"' in page          # a section with no rounds, but a failure
    assert "reached nobody" in page
    assert "nobody fills role implementor" in page
    # ONE envelope on the page, out of two on the record
    assert page.count("review_feedback") == 1


def test_the_feature_page_names_its_manager_order(client, project):
    """Paired with a feature that has none"""
    store = ProjectStore(project)
    with_mgr = store.create_feature_order("CSV export", description="the whole ask")
    mgr = store.create_work_order("own the follow-through", kind="manager",
                                  parent_id=with_mgr["id"])
    without = store.create_feature_order("PDF export", description="the other ask")

    page = client.get(f"/fo/proj_a/{with_mgr['id']}").text
    assert "manager:" in page
    assert mgr["id"] in page
    # ...and it is not counted as a piece of the work
    assert "no children" in page

    plain = client.get(f"/fo/proj_a/{without['id']}")
    assert plain.status_code == 200
    assert "manager:" not in plain.text


# -- a contested match on the page (spec 2026-09-12)


def test_a_contested_gate_offers_no_approve_button(gated):
    """The page's verbs are the user's options"""
    ops.contest_gate_match(gated.wo_id, "./scripts/shipit.sh",
                           why="the literal is inside a message body")
    gated.daemon._neo_drain()          # the fake escalates by default

    [approval] = [g for g in ops.list_gates() if g["wo_id"] == gated.wo_id]
    page = gated.client.get(f"/gates/{approval['id']}").text
    assert "CONTESTED" in page
    assert "the literal is inside a message body" in page
    assert 'value="dismiss"' in page and 'value="deny"' in page
    assert 'value="approve"' not in page


def test_an_abandoned_request_is_not_shown_as_expired(gated):
    """`expired` is three different outcomes and only one of them is evidence about the classifier"""
    from jarvis.hooks import preflight_decision

    settings = json.loads(
        (gated.project / ".jarvis" / "worker-settings"
         / f"{gated.wo_id}.json").read_text())
    preflight_decision({"tool_name": "Bash",
                        "tool_input": {"command": "./scripts/shipit.sh"},
                        "cwd": str(gated.project)}, settings["env"])
    store = ProjectStore(gated.project)
    try:
        store.conn.execute("UPDATE approvals SET ts = ts - 7200")
        gates.sweep_unargued(store, gates.DEFAULT_CASE_TTL_SECONDS)
    finally:
        store.close()

    page = gated.client.get("/gates").text
    assert "abandoned, never reviewed" in page
    # ...and counted beside the false-positive rate rather than inside it
    assert "1 more were abandoned" in page


def test_a_spent_grant_is_not_shown_as_expired(gated):
    """Issue 491: 23 of 26 production auto_merge gates were approved, used, and then badged `– expired`"""
    from jarvis.hooks import preflight_decision

    settings = json.loads(
        (gated.project / ".jarvis" / "worker-settings"
         / f"{gated.wo_id}.json").read_text())
    payload = {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 31 --squash"},
               "cwd": str(gated.project)}
    preflight_decision(payload, settings["env"])
    store = ProjectStore(gated.project)
    try:
        approval = store.list_approvals(gated.wo_id)[0]
        gates.apply_decision(store, approval["id"], verdict="approved", reason="green",
                             decided_by="neo")
    finally:
        store.close()
    for _ in range(gates.GRANT_MAX_USES):
        preflight_decision(payload, settings["env"])

    page = gated.client.get("/gates").text           # the view sweeps as it renders

    assert "spent" in page
    assert f"{gates.GRANT_MAX_USES}/{gates.GRANT_MAX_USES} used" in page
    # ...and NOT as the outcome it is the opposite of
    assert ">expired<" not in page


def test_the_work_order_page_splits_expired_too(gated):
    """The other renderer of `GATE_META`"""
    from jarvis.hooks import preflight_decision

    settings = json.loads(
        (gated.project / ".jarvis" / "worker-settings"
         / f"{gated.wo_id}.json").read_text())
    payload = {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 31 --squash"},
               "cwd": str(gated.project)}
    preflight_decision(payload, settings["env"])
    store = ProjectStore(gated.project)
    try:
        gates.apply_decision(store, store.list_approvals(gated.wo_id)[0]["id"],
                             verdict="approved", reason="green", decided_by="neo")
    finally:
        store.close()
    for _ in range(gates.GRANT_MAX_USES):
        preflight_decision(payload, settings["env"])

    # ...and beside it the outcome this page has never been able to say either: blocked
    preflight_decision({"tool_name": "Bash",
                        "tool_input": {"command": gated.gated_command},
                        "cwd": str(gated.project)}, settings["env"])
    store = ProjectStore(gated.project)
    try:
        held = [a for a in store.list_approvals(gated.wo_id)
                if a["command"] == gated.gated_command][0]
        store.abandon_approval(held["id"], "no case was made for it within 30 minutes")
    finally:
        store.close()

    page = gated.client.get(f"/wo/proj_a/{gated.wo_id}").text   # sweeps as it renders

    assert ">spent</span>" in page
    assert ">abandoned, never reviewed</span>" in page
    assert ">expired</span>" not in page


# -- budgets


def test_the_work_order_page_offers_a_budget_even_when_there_is_none(client):
    """The box is always offered: setting a budget on an order that has none is as much a thing the reader..."""
    wo = ops.create_work_order("proj_a", "uncapped", description="do it")
    page = client.get(f"/wo/proj_a/{wo['id']}").text
    assert "No budget" in page
    assert f"/wo/proj_a/{wo['id']}/budget" in page
    assert "Set budget" in page


def test_setting_a_budget_from_the_dashboard_calls_the_same_op(client):
    wo = ops.create_work_order("proj_a", "capped", description="do it")
    r = client.post(f"/wo/proj_a/{wo['id']}/budget", data={"amount": "$12.50"})
    assert r.status_code == 303
    assert ops.work_order_budget(wo["id"])["budget_usd"] == 12.5
    assert "$0.00 of $12.50" in client.get(f"/wo/proj_a/{wo['id']}").text


def test_an_empty_box_is_not_a_clear(client):
    """A form submitted by accident must not silently remove a ceiling"""
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=9.0)
    r = client.post(f"/wo/proj_a/{wo['id']}/budget", data={"amount": "  "})
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert ops.work_order_budget(wo["id"])["budget_usd"] == 9.0

    r = client.post(f"/wo/proj_a/{wo['id']}/budget", data={"clear": "1"})
    assert ops.work_order_budget(wo["id"])["budget_usd"] is None


def test_a_bad_amount_comes_back_as_a_flash_not_a_traceback(client):
    wo = ops.create_work_order("proj_a", "capped", description="do it")
    r = client.post(f"/wo/proj_a/{wo['id']}/budget", data={"amount": "lots"})
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert ops.work_order_budget(wo["id"])["budget_usd"] is None


def test_the_budget_box_refuses_nan_rather_than_accepting_an_uncappable_cap(client):
    """The dashboard POST is an entry point of its own"""
    wo = ops.create_work_order("proj_a", "capped", description="do it")
    for bad in ("nan", "inf", "-inf"):
        r = client.post(f"/wo/proj_a/{wo['id']}/budget", data={"amount": bad})
        assert r.status_code == 303
        assert "error=" in r.headers["location"]
        assert ops.work_order_budget(wo["id"])["budget_usd"] is None


def _broke_family(project, *, budget_usd: float = 3.0,
                  kind: str = "investigation") -> tuple[str, str]:
    """A family whose money is gone and one child parked on its slice"""
    store = ProjectStore(project)
    try:
        fo = store.create_feature_order("why is it stuck", description="it is stuck",
                                        kind=kind, budget_usd=budget_usd)
        store.set_feature_status(fo["id"], "planning")
        # The child's kind is the family's
        child = store.create_work_order(
            "diagnose it", description="look", parent_id=fo["id"],
            kind="worker" if kind == "feature" else "investigator")
        if kind != "feature":
            store.update_feature_order(fo["id"], plan_wo_id=child["id"])
        store.update_work_order(child["id"], budget_reserved_usd=budget_usd,
                                status="budget_exhausted")
        turn = store.create_turn(child["id"], kind="message", prompt="work")
        store.finish_turn(turn["id"], "done", result="done", cost_usd=9.0,
                          usage_json=json.dumps({"total_cost_usd": 9.0}))
        return fo["id"], child["id"]
    finally:
        store.close()


def test_raising_a_family_capped_child_says_so_and_calls_it_no_error(client, project):
    """Obligation 8 of docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md"""
    _fo_id, child = _broke_family(project)
    r = client.post(f"/wo/proj_a/{child}/budget", data={"amount": "$10"})
    assert r.status_code == 303
    location = r.headers["location"]
    assert "note=" in location
    assert "error=" not in location
    assert "jarvis+investigate+budget" in location or \
        "jarvis%20investigate%20budget" in location

    page = client.get(f"/wo/proj_a/{child}{location[location.index('?'):]}").text
    assert "jarvis investigate budget" in html.unescape(page)
    # The rendered DIV, not the class name: base.html's stylesheet defines `.error-flash` on every page
    assert '<div class="error-flash">' not in page
    assert '<div class="note-flash"' in page


def test_the_budget_card_calls_the_family_by_its_kind(client, project):
    """The card beside the flash said "its feature's slice" for an investigation"""
    _fo_id, child = _broke_family(project)
    page = html.unescape(client.get(f"/wo/proj_a/{child}").text)
    assert "its investigation's slice" in page
    assert "its feature's slice" not in page
    assert "its investigation has $0.00 unreserved" in page


def test_raising_a_family_with_parked_children_names_them(client, project):
    """Obligation 8, the sibling route: `exhausted_children` is the whole instruction to the user"""
    fo_id, child = _broke_family(project, kind="feature")
    r = client.post(f"/fo/proj_a/{fo_id}/budget", data={"amount": "$50"})
    assert r.status_code == 303
    location = r.headers["location"]
    assert "note=" in location and "error=" not in location

    page = html.unescape(client.get(f"/fo/proj_a/{fo_id}"
                                    f"{location[location.index('?'):]}").text)
    assert child in page
    assert "jarvis wo budget" in page
    # The WHOLE clause agrees, not just the noun: one order is parked on ITS OWN ceiling
    assert "1 work order still parked on its own ceiling" in page


def test_api_status_carries_every_feature_order_kind(client, project):
    """Obligation 7 at the surface that reads the payload: `/api/status` and `jarvis status --json` are..."""
    store = ProjectStore(project)
    try:
        io = store.create_feature_order("slow first turns", description="three in a row",
                                        kind="improvement")
        store.set_feature_status(io["id"], "planning")
    finally:
        store.close()
    rows = client.get("/api/status").json()["projects"][0]["feature_orders"]
    row = next(r for r in rows if r["id"] == io["id"])
    assert row["kind"] == "improvement"
    assert row["status_label"] == "analysing"


def test_a_spent_order_is_featured_ahead_of_everything_else(client, daemon):
    """It is the only blocker the reader cannot answer by reading: the order is stopped and spending..."""
    from jarvis.ui.app import FEATURED_STATUSES, group_open

    assert FEATURED_STATUSES[0] == "budget_exhausted"
    featured, _ = group_open([{"status": "needs_review", "id": "wo-1"},
                              {"status": "budget_exhausted", "id": "wo-2"}])
    assert [w["id"] for w in featured] == ["wo-2", "wo-1"]


def test_a_validating_order_has_a_row_of_its_own():
    """A pending row's "blocked by" can name it, so it must not hide in a collapsed count"""
    from jarvis.ui.app import group_open

    featured, rest = group_open([{"status": "pending", "id": "wo-1"},
                                 {"status": "validating", "id": "wo-2"}], revealed="pending")
    assert [w["id"] for w in featured] == ["wo-2"]
    assert [w["id"] for w in rest] == ["wo-1"]


# -- a budget at CREATION time


def _form(page: str, action: str) -> str:
    """The one form posting to `action`"""
    start = page.index(f'action="{action}"')
    return page[start:page.index("</form>", start)]


def test_both_create_forms_offer_a_budget(client):
    """The ceiling was settable only after the order existed"""
    page = client.get("/").text
    assert 'name="budget"' in _form(page, "/wo/create")
    assert 'name="budget"' in _form(page, "/fo/create")


def test_creating_a_work_order_from_the_browser_stamps_the_budget(client):
    r = client.post("/wo/create", data={"project": "proj_a", "title": "capped",
                                        "description": "do it", "budget": "$12.50"})
    assert r.status_code == 303
    wo_id = r.headers["location"].rsplit("/", 1)[-1]
    assert ops.work_order_budget(wo_id)["budget_usd"] == 12.5


def test_creating_a_feature_order_from_the_browser_stamps_the_family_budget(client):
    r = client.post("/fo/create", data={"project": "proj_a", "title": "CSV export",
                                        "description": "the whole ask, at length, "
                                                       "with enough detail to plan",
                                        "budget": "50"})
    assert r.status_code == 303
    fo_id = r.headers["location"].rsplit("/", 1)[-1]
    assert ops.feature_order_budget(fo_id)["budget_usd"] == 50.0


def test_an_empty_budget_box_on_creation_is_not_a_zero(client):
    """Empty means "the project's default", which is usually no ceiling"""
    r = client.post("/wo/create", data={"project": "proj_a", "title": "uncapped",
                                        "description": "do it", "budget": "  "})
    wo_id = r.headers["location"].rsplit("/", 1)[-1]
    assert ops.work_order_budget(wo_id)["budget_usd"] is None

    r = client.post("/fo/create", data={"project": "proj_a", "title": "uncapped",
                                        "description": "the whole ask, at length",
                                        "budget": ""})
    fo_id = r.headers["location"].rsplit("/", 1)[-1]
    assert ops.feature_order_budget(fo_id)["budget_usd"] is None


@pytest.mark.parametrize("bad", ["lots", "nan", "inf", "-inf", "0"])
def test_a_bad_budget_refuses_the_order_rather_than_filing_it_uncapped(client, project,
                                                                      bad):
    """`nan` is the one that matters: it survives a `> 0` check"""
    r = client.post("/wo/create", data={"project": "proj_a", "title": "capped",
                                        "description": "do it", "budget": bad})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/?error=")

    r = client.post("/fo/create", data={"project": "proj_a", "title": "capped",
                                        "description": "the whole ask, at length",
                                        "budget": bad})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/project/proj_a?error=")

    store = ProjectStore(project)
    assert store.list_work_orders() == []
    assert ops.list_feature_orders("proj_a") == []


def test_the_browser_side_pattern_never_rejects_what_the_server_accepts(client):
    """The pattern exists so a typo bounces before it costs the description typed beside it (Neo, q418)"""
    import re

    from jarvis import budget

    page = client.get("/").text
    # Per form, so dropping the attribute from ONE of them is not covered by the other
    found = {form: re.findall(r'pattern="([^"]+)"', _form(page, form))
             for form in ("/wo/create", "/fo/create")}
    assert all(len(v) == 1 for v in found.values()), f"one rule per form, got {found}"
    patterns = {v[0] for v in found.values()}
    assert len(patterns) == 1, f"the two forms disagree on the rule: {patterns}"
    pattern = patterns.pop()
    for good in ("5", "5.50", "$5.50", "1,000", "1,000.50", ".5"):
        budget.parse_amount(good)  # the server takes it, so the browser must too
        assert re.fullmatch(pattern, good), f"{pattern!r} rejects {good!r}"
    for bad in ("lots", "nan", "inf", "five dollars", "$"):
        assert not re.fullmatch(pattern, bad), f"{pattern!r} accepts {bad!r}"


def test_the_work_order_page_says_the_pull_request_merged(client, daemon, project):
    """The dashboard half of the 2026-09-25 spec §7"""
    pr = "https://github.com/acme/proj/pull/735"
    wo = ops.create_work_order("proj_a", "the cap")
    daemon.tick()
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], pr_url=pr)
        store.set_status(wo["id"], "completed")
        store.add_event(wo["id"], "pr_merged", {"pr_url": pr, "head_oid": "abc1234",
                                                "merged_at": "2026-09-20T10:00:00Z",
                                                "source": "landing_sweep"})
    finally:
        store.close()

    page = client.get(f"/wo/proj_a/{wo['id']}").text

    assert pr in page
    assert "MERGED" in page


def test_a_work_order_with_no_pull_request_says_nothing_about_merging(client, daemon,
                                                                     project):
    wo = ops.create_work_order("proj_a", "a planner")
    daemon.tick()

    assert "MERGED" not in client.get(f"/wo/proj_a/{wo['id']}").text


# -- the review control an escalated round owes


def _escalate(project, title="the panel gave up on it",
              cause=None):
    """A work order in the live shape of wo-659be188: `needs_review`"""
    store = ProjectStore(project)
    try:
        wo = store.create_work_order(title)
        r = store.open_validation_round(wo_id=wo["id"], fingerprint="cccc3333",
                                        summary="ship it", evidence="ran pytest")
        ops.escalate_validation_round(store, wo, r["id"], r["round"],
                                      "round 2 asked for the same test again",
                                      cause=cause or ops.ESCALATION_ROUNDS_SPENT)
        return store.get_work_order(wo["id"])
    finally:
        store.close()


def test_an_escalated_round_with_no_assumptions_still_offers_the_review(client, project):
    """The whole defect: the only review form was gated on a pending assumption"""
    wo = _escalate(project)

    page = client.get(f"/wo/proj_a/{wo['id']}").text

    assert f'action="/wo/proj_a/{wo["id"]}/review"' in page
    assert 'name="decision" value="accept"' in page
    assert 'name="decision" value="reject"' in page
    assert "over the panel's objection" in html.unescape(" ".join(page.split()))
    # ...and it is where the deep link lands — §7
    assert page.count('id="pending"') == 1


def test_the_review_control_renders_once_and_in_the_assumptions_block(client, project):
    """Neo 838: exactly one form, never two"""
    wo = _escalate(project)
    ops.assume(wo["id"], "assumed the API is v2")

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    action = f'action="/wo/proj_a/{wo["id"]}/review"'

    assert page.count(action) == 1
    # The one form is in the assumptions block, above the rounds — never beside them
    block = page.split("Assumptions pending your review", 1)[1].split(">Validation<", 1)[0]
    assert action in block
    # The line naming the round this one decision now ALSO settles
    assert "also settles round 1" in " ".join(block.split())


def test_rejecting_an_escalated_order_records_the_review_and_guides_the_worker(
        client, project):
    wo = _escalate(project)
    assert wo["needs_attention"]   # the escalation raised it; the review must put it down

    res = client.post(f"/wo/proj_a/{wo['id']}/review",
                      data={"decision": "reject", "feedback": "the panel is right"})

    assert res.status_code == 303
    store = ProjectStore(project)
    try:
        assert store.events_of_kind(wo["id"], "reviewed")
        assert [m["content"] for m in store.list_messages(wo["id"])] \
            == ["the panel is right"]
        # THE REVIEW TOOK EFFECT, not just the delivery
        after = store.get_work_order(wo["id"])
        assert not after["needs_attention"]
        # ...and the status is unchanged by design: `needs_review` is an OPEN status and the worker resumes...
        assert after["status"] == "needs_review"
    finally:
        store.close()


def test_the_page_still_renders_exactly_one_review_form(client, project):
    """Spec 2026-10-14-an-escalated-round-says-why-and-what-to-run-next §5: two more rows beside the..."""
    wo = _escalate(project)

    page = client.get(f"/wo/proj_a/{wo['id']}").text
    flat = html.unescape(" ".join(page.split()))

    assert page.count(f'action="/wo/proj_a/{wo["id"]}/review"') == 1
    assert "the panel rejected this and had no round left" in flat
    assert f"jarvis wo review {wo['id']} --reject" in flat
    # The HEADER above those rows, which contradicted them — issue #1040
    assert f"Round 1 {ops.ESCALATION_VERB['handover']} — your judgement" in flat
    assert "Round 1 gave up" not in flat


def test_the_review_rows_escape_everything_they_are_handed():
    """`review.why` and `review.next` are placeholders"""
    from fastapi.templating import Jinja2Templates

    from jarvis.ui.app import TEMPLATES

    env = Jinja2Templates(directory=str(TEMPLATES)).env
    assert env.autoescape, "the review rows have no escaping of their own"

    hostile = "<script>alert(1)</script>"
    out = env.get_template("_review.html").module.review_form(
        "proj_a", {"id": hostile},
        {"pending": 0, "round": 1, "why": f"the panel said {hostile}",
         "next": [f"jarvis wo review {hostile} --reject"],
         "scope": "one decision", "accept": "lands it", "reject": "sends it back",
         "strands": "no reason strands it"})

    assert "<script>" not in str(out)
    assert "&lt;script&gt;" in str(out)


def test_the_page_of_a_no_pr_order_never_names_validation_force(client, project):
    """The regression's fourth surface — spec §4"""
    wo = _escalate(project, cause=ops.ESCALATION_NO_VERDICT)

    flat = html.unescape(" ".join(client.get(f"/wo/proj_a/{wo['id']}").text.split()))

    assert "validation force" not in flat
    assert "Nobody has judged the work" in flat


def test_the_feature_order_page_never_grows_a_work_order_review_form(client, project):
    """§5: the shared `section()` macro must not carry it"""
    store = ProjectStore(project)
    try:
        fo = store.create_feature_order("CSV export", description="the whole ask")
        r = store.open_validation_round(fo_id=fo["id"], fingerprint="dddd4444",
                                        summary="the plan", evidence="read the tree")
        store.close_validation_round(r["id"], "escalated", "the plan cannot be judged")
    finally:
        store.close()

    page = client.get(f"/fo/proj_a/{fo['id']}").text

    assert "Validation" in page and "the plan cannot be judged" in page
    assert "/wo/proj_a/" not in page.split(">Validation<")[1]
    assert "over the panel's objection" not in html.unescape(" ".join(page.split()))


def test_the_open_span_says_when_it_last_moved(client, daemon, project):
    """Fix 4's span line: "still in it" is true but is not the live fact (docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md)"""
    wo = ops.create_work_order("proj_a", "risky change")
    daemon.tick()
    store = ProjectStore(project)
    try:
        store.add_event(wo["id"], "turn_ended")
    finally:
        store.close()

    page = " ".join(client.get(f"/wo/proj_a/{wo['id']}").text.split())
    assert "still in it · active" in html.unescape(page)


@pytest.mark.usefixtures("self_dispatch_on")
def test_stuck_report_is_the_only_arithmetic(client, project, capsys):
    """§7 of docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets- investigated.md: one..."""
    from test_health_sweep import park_order

    from jarvis import catalog, cli

    wo = ops.create_work_order("proj_a", "the pull request merged hours ago")
    store = ProjectStore(project)
    try:
        park_order(store, wo["id"], "waiting_pr_merge", hours=5)
    finally:
        store.close()

    rows = ops.stuck_report()
    (row,) = [r for r in rows if r["id"] == wo["id"]]
    assert row["stuck"] and row["status"] == "waiting_pr_merge"
    assert row["threshold_seconds"] == \
        catalog.DEFAULT_FLEET_HEALTH_THRESHOLDS["waiting_pr_merge"] * 60
    assert row["excluded"] == ""

    assert cli.main(["stuck", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)

    # The clocks move between the two reads; everything the renderers DECIDE from does not
    moving = ("seconds_in_status", "seconds_since_activity", "active_seconds")
    assert set(printed[0]) == set(row)
    assert [{k: v for k, v in r.items() if k not in moving} for r in printed] == \
           [{k: v for k, v in r.items() if k not in moving} for r in rows]

    page = " ".join(client.get("/stuck").text.split())
    assert wo["id"] in page and "jarvis rules list" in page


@pytest.mark.usefixtures("self_dispatch_on")
def test_the_terminal_names_the_blocker_too(client, project, capsys):
    """Parity fix: `jarvis stuck` prints the blocker `/stuck` already renders"""
    from test_health_sweep import park_order

    from jarvis import cli

    wo = ops.create_work_order("proj_a", "asked a question hours ago")
    store = ProjectStore(project)
    try:
        park_order(store, wo["id"], "waiting_input", hours=5)
    finally:
        store.close()

    assert cli.main(["stuck"]) == 0
    out = capsys.readouterr().out
    assert "what the record says blocks it: worker is waiting on your input" in out


# -- the retry control

#: Wording ONLY the retry section emits
RETRY_HEADING = "Retry this work order"


def _page(client, wo_id: str, query: str = "") -> str:
    return html.unescape(" ".join(client.get(f"/wo/proj_a/{wo_id}{query}").text.split()))


def _died(daemon, project, session: bool = True) -> dict:
    """A work order whose worker died without delivering — with or without a session"""
    wo = ops.create_work_order("proj_a", "died without delivering")
    if session:
        daemon.tick()
    store = ProjectStore(project)
    try:
        if session:
            store.set_status(wo["id"], "failed")
        else:
            store.release_dispatch_claim(wo["id"], "worker turn never started",
                                         max_attempts=1)
        return store.get_work_order(wo["id"])
    finally:
        store.close()


def test_the_retry_control_is_on_a_failed_order_and_on_no_other(client, daemon, project):
    """`ops.retry_state`'s None convention: no control at all where the mechanism does not apply"""
    dead = _died(daemon, project)
    live = ops.create_work_order("proj_a", "still going")
    daemon.tick()

    assert RETRY_HEADING in _page(client, dead["id"])
    assert RETRY_HEADING not in _page(client, live["id"])


def test_the_no_session_refusal_renders_above_a_disabled_box(client, daemon, project):
    wo = _died(daemon, project, session=False)

    page = _page(client, wo["id"])

    assert "no session to relaunch" in page
    # Above the control it governs, and the control is shut
    assert page.index("no session to relaunch") < page.index('name="message"')
    assert page.count("disabled") >= 2


def test_pressing_retry_queues_the_message_and_says_so(client, daemon, project):
    wo = _died(daemon, project)

    response = client.post(f"/wo/proj_a/{wo['id']}/retry", data={"message": ""})

    store = ProjectStore(project)
    try:
        msgs = store.list_messages(wo["id"])
    finally:
        store.close()
    assert msgs[0]["content"] == ops.RETRY_NOTE and msgs[0]["source"] == "retry"
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/wo/proj_a/{wo['id']}?retried={msgs[0]['id']}#retry")
    assert ops.retry_queued_notice(msgs[0]["id"]) in _page(
        client, wo["id"], f"?retried={msgs[0]['id']}")


def test_a_refused_press_flashes_the_refusal_and_claims_nothing(client, daemon, project):
    wo = _died(daemon, project, session=False)

    response = client.post(f"/wo/proj_a/{wo['id']}/retry", data={"message": ""})

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "#retry" in response.headers["location"]
    store = ProjectStore(project)
    try:
        assert store.list_messages(wo["id"]) == []
    finally:
        store.close()


@pytest.mark.parametrize("crafted", ["999999", "nonsense", "-1"])
def test_a_crafted_retried_link_states_no_fact(client, daemon, project, crafted):
    """The notice is rebuilt from an id the record knows, never carried as text — `fix_filed_notice`'s rule"""
    wo = _died(daemon, project)

    page = _page(client, wo["id"], f"?retried={crafted}")

    assert RETRY_HEADING in page
    assert "jarvisd launches the turn" not in page


# -- one order, one page: links are derived from the id PREFIX


def _flagged(project, title, kind, reason):
    """A `feature_orders` row of one kind, flagged for the user"""
    store = ProjectStore(project)
    try:
        row = store.create_feature_order(title, description="the whole ask", kind=kind)
        store.flag_feature_attention(row["id"], reason)
        return row
    finally:
        store.close()


@pytest.mark.parametrize("kind,segment", [("feature", "fo"), ("improvement", "io"),
                                          ("investigation", "inv")])
def test_the_attention_strip_links_each_kind_to_its_own_page(client, project, kind,
                                                             segment):
    row = _flagged(project, "slow first turns", kind, "it needs you")

    page = client.get("/").text

    assert f'/{segment}/proj_a/{row["id"]}' in page
    for other in ("fo", "io", "inv"):
        if other != segment:
            assert f'/{other}/proj_a/{row["id"]}' not in page


@pytest.mark.parametrize("kind,segment", [("improvement", "io"),
                                          ("investigation", "inv")])
def test_an_old_fo_bookmark_for_another_kind_redirects(client, project, kind, segment):
    row = _flagged(project, "slow first turns", kind, "it needs you")

    res = client.get(f"/fo/proj_a/{row['id']}")

    assert res.status_code == 303
    assert res.headers["location"] == f"/{segment}/proj_a/{row['id']}"


def test_order_path_maps_every_prefix_and_falls_back_to_the_feature_page():
    # In `project_store`, beside the prefixes it derives from: `search.py` links through the same...
    from jarvis.project_store import order_path
    from jarvis.ui import app

    assert app.order_path is order_path
    assert order_path("proj_a", "wo-1234") == "/wo/proj_a/wo-1234"
    assert order_path("proj_a", "fo-1234") == "/fo/proj_a/fo-1234"
    assert order_path("proj_a", "io-1234") == "/io/proj_a/io-1234"
    assert order_path("proj_a", "inv-1234") == "/inv/proj_a/inv-1234"
    # Unknown prefix: the feature page, never `/wo/`
    assert order_path("proj_a", "zz-1234") == "/fo/proj_a/zz-1234"


# -- record detail pages (spec 2026-10-29 dashboard §6)


def test_a_backlog_item_page_links_its_dependencies_and_promotes(client, project):
    central = CentralStore()
    dep = central.add_backlog("proj_a", "foundation")
    item = central.add_backlog("proj_a", "tower", description="build it tall",
                               depends_on=[dep["id"]])
    central.close()

    page = client.get(f"/backlog/{item['id']}").text
    assert "build it tall" in page
    assert f'href="/backlog/{dep["id"]}"' in page
    assert f'action="/backlog/promote/{item["id"]}"' in page and 'name="force"' in page
    assert client.get("/backlog/bl-nope").status_code == 404

    shown = ops.show_backlog(item["id"])
    assert [b["id"] for b in shown["blockers"]] == [dep["id"]]


def test_a_knowledge_entry_page_shows_the_body_and_pins_back_to_itself(client):
    central = CentralStore()
    row = central.add_knowledge("FIRST LINE\nthe whole body, past the headline",
                                project="", topic="tooling", wo_id="wo-1234abcd")
    central.close()

    page = client.get(f"/knowledge/{row['id']}").text
    assert "the whole body, past the headline" in page
    assert 'href="/go/wo-1234abcd"' in page
    assert "0 reads" in page

    r = client.post("/knowledge/pin", data={"kn_id": row["id"], "pinned": "1",
                                            "next": f"/knowledge/{row['id']}"})
    assert r.headers["location"] == f"/knowledge/{row['id']}"
    assert ops.knowledge_detail(row["id"])["pinned"]
    # a dashboard view is not an agent read: the read log stays empty
    assert ops.knowledge_detail(row["id"])["reads"] == 0
    assert client.get("/knowledge/kn-nope").status_code == 404
    r = client.post("/knowledge/pin", data={"kn_id": "kn-nope", "pinned": "1"})
    from urllib.parse import unquote
    assert "unknown knowledge id" in unquote(r.headers["location"])


def test_a_gate_page_404s_on_an_unknown_id(client):
    assert client.get("/gates/999").status_code == 404


def test_a_gate_number_in_two_projects_lists_both_candidates(client, project, tmp_path):
    """Approval ids are per project: a bare /gates/<n> must not dead-end"""
    other = tmp_path / "proj_b"
    other.mkdir()
    central = CentralStore()
    central.upsert_project("proj_b", str(other))
    central.close()
    ids = {}
    for name, path in (("proj_a", project), ("proj_b", other)):
        store = ProjectStore(path)
        try:
            wo = store.create_work_order(f"ship {name}")
            ids[name] = store.add_approval(wo["id"], "release", f"./ship-{name}.sh")["id"]
        finally:
            store.close()
    n = ids["proj_a"]
    assert ids["proj_b"] == n

    r = client.get(f"/gates/{n}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/gates?id={n}"

    page = client.get(f"/gates?id={n}").text
    assert f'href="/gates/{n}?project=proj_a"' in page
    assert f'href="/gates/{n}?project=proj_b"' in page
    assert "nothing matches these filters" not in page

    r = client.get(f"/gates/{n}?project=proj_b")
    assert r.status_code == 200
    assert "./ship-proj_b.sh" in r.text and "./ship-proj_a.sh" not in r.text
# -- §7 of docs/superpowers/specs/2026-10-29-dashboard-full-width-failures-first-row-details.md


def test_a_dashboard_status_filter_reveals_a_collapsed_status(client):
    ops.create_work_order("proj_a", "not started yet")
    assert "not started yet" not in client.get("/").text  # pending collapses into a count
    page = client.get("/?status=pending").text
    assert "not started yet" in page and "1 of 1 shown" in page


def test_the_project_filter_counts_only_what_the_view_draws_from(client, project):
    open_wo = ops.create_work_order("proj_a", "still going")
    done = ops.create_work_order("proj_a", "old and finished")
    ops.mark_done(done["id"])
    store = ProjectStore(project)
    try:
        store.set_status(open_wo["id"], "running")
    finally:
        store.close()
    assert "1 of 1 shown" in client.get("/project/proj_a").text  # the settled one is not in M
    assert "1 of 2 shown" in client.get("/project/proj_a?status=completed").text


def test_a_finding_owed_a_decision_sorts_above_a_decided_one(improvement):
    client, io, _ = improvement
    first, second = (f["key"] for f in io["report"]["findings"][:2])
    before = client.get(f"/io/proj_a/{io['id']}").text
    assert before.index(f'id="finding-{first}"') < before.index(f'id="finding-{second}"')
    ops.review_findings(io["id"], accept=(first,), project_name="proj_a")
    page = client.get(f"/io/proj_a/{io['id']}").text
    assert page.index(f'id="finding-{second}"') < page.index(f'id="finding-{first}"')
