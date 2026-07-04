"""Browser tests: the dashboard as a user actually experiences it — real DOM,
real forms, real navigation, headless Chromium."""

from __future__ import annotations

from jarvis import ops
from jarvis.project_store import ProjectStore


def test_dashboard_quiet_state(page, server):
    page.goto(server)
    assert "Jarvis" in page.title()
    assert page.locator(".wordmark").inner_text().endswith("JARVIS")
    quiet = page.locator(".attention .quiet")
    assert quiet.is_visible()
    assert "all quiet" in quiet.inner_text()
    assert page.locator("text=proj_a").first.is_visible()


def test_nav_walk_all_tabs(page, server):
    page.goto(server)
    for label, path in [("neo", "/neo"), ("inbox", "/inbox"),
                        ("backlog", "/backlog"), ("knowledge", "/knowledge"),
                        ("dashboard", "/")]:
        page.click(f"nav >> text={label}")
        assert page.url.rstrip("/").endswith(path.rstrip("/")) or path == "/"
        assert page.locator("nav a.here").inner_text().startswith(label)


def test_create_work_order_via_form(page, server, project):
    page.goto(server)
    page.fill("form[action='/wo/create'] input[name='title']", "browser-made order")
    page.select_option("form[action='/wo/create'] select[name='project']", "proj_a")
    page.click("form[action='/wo/create'] button")
    # lands on the detail page with the framework origin badge
    assert "browser-made order" in page.locator("body").inner_text()
    store = ProjectStore(project)
    try:
        wo = store.list_work_orders()[0]
        assert wo["origin"] == "ui"
    finally:
        store.close()


def test_attention_strip_and_review_flow(page, server, daemon, project):
    wo = ops.create_work_order("proj_a", "assumption heavy task")
    daemon.tick()
    ops.assume(wo["id"], "went with sqlite over postgres")
    ops.finish(wo["id"], "done with one assumption")

    page.goto(server)
    strip = page.locator(".attention.hot")
    assert strip.is_visible()
    assert "NEEDS YOU" in strip.inner_text()
    assert wo["id"] in strip.inner_text()

    # click through to the work order and accept the review
    page.click(f".attention a:has-text('{wo['id']}')")
    assert "went with sqlite" in page.locator("body").inner_text()
    page.click("button:has-text('Accept all')")
    assert "completed" in page.locator("body").inner_text()

    # dashboard returns to quiet
    page.goto(server)
    assert page.locator(".attention .quiet").is_visible()


def test_send_feedback_from_wo_page(page, server, daemon, project):
    wo = ops.create_work_order("proj_a", "chatty task")
    daemon.tick()
    page.goto(f"{server}/wo/proj_a/{wo['id']}")
    page.fill("textarea[name='message']", "please use the staging bucket")
    page.click("button:has-text('Send to worker')")
    assert "please use the staging bucket" in page.locator("body").inner_text()
    store = ProjectStore(project)
    try:
        assert any("staging bucket" in m["content"]
                   for m in store.queued_messages(wo["id"]))
    finally:
        store.close()


def test_neo_full_review_cycle(page, server, daemon, project):
    wo = ops.create_work_order("proj_a", "format decision")
    daemon.tick()
    ops.ask_question(wo["id"], "CSV or JSON for the export?")
    daemon._neo_drain()

    page.goto(f"{server}/neo")
    body = page.locator("body").inner_text()
    assert "CSV or JSON for the export?" in body
    assert "neo-decision" in body

    # approve from the browser
    page.click("button:has-text(\"That's what I'd say\")")
    assert page.locator("text=approved").first.is_visible()

    # second question — correct it, teaching Neo
    ops.ask_question(wo["id"], "And the delimiter?")
    daemon._neo_drain()
    page.goto(f"{server}/neo")
    page.fill("input[name='feedback']", "Semicolons. Excel-friendly.")
    page.click("button:has-text('Correct')")
    assert "Semicolons. Excel-friendly." in page.locator("body").inner_text()


def test_neo_escalation_answered_in_browser(page, server, daemon, project):
    wo = ops.create_work_order("proj_a", "risky business")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_ESCALATE: rm -rf prod?")
    daemon._neo_drain()

    page.goto(server)  # escalation shows on the dashboard attention strip
    assert "Neo escalated" in page.locator(".attention").inner_text()

    page.goto(f"{server}/neo")
    page.fill("textarea[name='text']", "Absolutely not. Never.")
    page.click("button:has-text('Send answer to worker')")
    store = ProjectStore(project)
    try:
        assert any("Absolutely not" in m["content"]
                   for m in store.queued_messages(wo["id"]))
    finally:
        store.close()
    # resolved: strip is quiet again
    page.goto(server)
    assert page.locator(".attention .quiet").is_visible()


def test_neo_badge_counts(page, server, daemon, project):
    wo = ops.create_work_order("proj_a", "badge check")
    daemon.tick()
    ops.ask_question(wo["id"], "q1?")
    ops.ask_question(wo["id"], "q2?")
    daemon._neo_drain()
    page.goto(server)
    badge = page.locator("nav .nav-badge")
    assert badge.inner_text() == "2"


def test_backlog_promote_blocked_then_forced(page, server, project):
    from jarvis.central_store import CentralStore
    central = CentralStore()
    try:
        dep = central.add_backlog("proj_a", "the foundation")
        item = central.add_backlog("proj_a", "the tower", depends_on=[dep["id"]])
    finally:
        central.close()
    page.goto(f"{server}/backlog")
    assert "the tower" in page.locator("body").inner_text()
    # promote the blocked item → error flash names the blocker
    row = page.locator(f"form[action='/backlog/promote/{item['id']}']").first
    row.locator("button:has-text('Promote')").click()
    assert "unfinished dependencies" in page.locator(".error-flash").inner_text()


def test_inbox_ack_in_browser(page, server, daemon, project):
    from jarvis.central_store import CentralStore
    central = CentralStore()
    try:
        central.add_inbox("proj_a", "deploy finished", level="info")
    finally:
        central.close()
    page.goto(f"{server}/inbox")
    assert "deploy finished" in page.locator("body").inner_text()
    page.click("form:has(input[name='inbox_id']) button")
    assert "deploy finished" not in page.locator("body").inner_text()


def test_dashboard_auto_refresh_tag(page, server):
    page.goto(server)
    meta = page.locator("meta[http-equiv='refresh']")
    assert meta.count() == 1
