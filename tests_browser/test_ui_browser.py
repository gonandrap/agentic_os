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
    for label, path in [("neo", "/neo"), ("gates", "/gates"), ("inbox", "/inbox"),
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


def test_work_order_tabs_show_one_reading_at_a_time(page, server, daemon, project):
    """The tab strip is JavaScript, so only a real browser proves it: three panels, one
    open, and switching swaps which."""
    wo = ops.create_work_order("proj_a", "tabbed task")
    daemon.tick()
    page.goto(f"{server}/wo/proj_a/{wo['id']}")

    conversation = page.locator("#tab-conversation")
    timeline = page.locator("#tab-timeline")
    assert conversation.is_visible()
    assert not timeline.is_visible()

    page.click(".tabs button:has-text('Timeline')")
    assert timeline.is_visible()
    assert not conversation.is_visible()
    assert "Work order created" in timeline.inner_text()


def test_a_deep_link_opens_the_tab_its_target_is_inside(page, server, daemon, project):
    """Every notification links to `#pending`, and with no work owed it resolves to the
    reply box — which now lives inside a tab. A deep link that landed on a closed panel
    would show the user nothing at all."""
    wo = ops.create_work_order("proj_a", "deep linked task")
    daemon.tick()
    page.goto(f"{server}/wo/proj_a/{wo['id']}#pending")

    assert page.locator("#tab-conversation").is_visible()
    assert page.locator("#pending").is_visible()

    # ...and the hash arriving LATER works too, which is the case a load-time-only
    # handler would miss: the reader is on another tab when the link is followed.
    page.click(".tabs button:has-text('Timeline')")
    assert not page.locator("#tab-conversation").is_visible()
    page.evaluate("location.hash = ''")
    page.evaluate("location.hash = 'pending'")
    assert page.locator("#tab-conversation").is_visible()
    assert page.locator("#pending").is_visible()


def test_a_timeline_question_opens_the_question_and_its_answer(page, server, daemon,
                                                               project):
    """The timeline names the question and points at it. Following that link must land
    on the one record holding the question and the answer together."""
    wo = ops.create_work_order("proj_a", "asking task")
    daemon.tick()
    qid = ops.ask_question(wo["id"], "CSV or JSON for the export default?")["question_id"]
    ops.neo_answer_escalated(qid, "CSV, and gzip it")

    page.goto(f"{server}/wo/proj_a/{wo['id']}")
    page.click(".tabs button:has-text('Timeline')")
    story = page.locator("#tab-timeline").inner_text()
    assert "Worker asked a question" in story
    assert "CSV or JSON for the export default?" not in story  # pointed at, not printed

    page.click(f"#tab-timeline a:has-text('question #{qid}')")
    body = page.locator("body").inner_text()
    assert "CSV or JSON for the export default?" in body
    assert "CSV, and gzip it" in body


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


def test_gate_escalation_decided_in_the_browser(page, gated_server, gated_daemon,
                                                project):
    """The whole point of the tab: a worker's release attempt reaches the user as a
    decision they can make from the dashboard, with the reviewer's own text in front
    of them, and approving it actually opens the gate for the worker."""
    wo = ops.create_work_order("proj_a", "ship 1.2.3")
    gated_daemon.tick()
    ops.request_gate_approval(wo["id"], "./scripts/shipit.sh",
                              why="green build, changelog written")
    gated_daemon._neo_drain()  # the fake model escalates unless forced

    page.goto(gated_server)  # the attention strip routes to the tab
    assert "approve release" in page.locator(".attention").inner_text()
    page.click(".attention a:has-text('decide it')")
    assert "/gates" in page.url

    # inner_text() is rendered text, and the section headings are uppercased by CSS
    body = page.locator("body").inner_text().lower()
    assert "waiting on you" in body
    assert "./scripts/shipit.sh" in body
    assert "green build, changelog written" in body

    # the request as the reviewer read it is one click away, not a separate command
    page.click("summary:has-text('the request exactly as the reviewer saw it')")
    assert "shipit" in page.locator("pre.request-text").first.inner_text()

    page.fill("input[name='reason']", "checked the diff myself")
    page.click("button:has-text('Approve')")

    store = ProjectStore(project)
    try:
        approval = store.list_approvals(wo["id"])[0]
        assert approval["status"] == "approved"
        assert approval["decided_by"] == "user"
    finally:
        store.close()
    assert "checked the diff myself" in page.locator("body").inner_text()
    page.goto(gated_server)
    assert page.locator(".attention .quiet").is_visible()  # and the strip goes quiet


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


def test_dashboard_refreshes_without_reloading_the_page(page, server):
    """The only meta-refresh left is the noscript fallback — a live page never
    navigates, because navigating wipes whatever is half-typed in the form."""
    page.goto(server)
    # the fallback is served, but a scripting browser parses <noscript> as inert
    # text — so the live page has no meta-refresh element at all
    assert 'http-equiv="refresh"' in page.content()
    assert page.evaluate(
        "document.querySelectorAll('meta[http-equiv=\"refresh\"]').length"
    ) == 0
    for region in ("live-chrome", "live-top", "live-bottom"):
        assert page.locator(f"#{region}").count() == 1
    # the form is deliberately outside every live region
    assert page.evaluate(
        "!!document.querySelector('form[action=\"/wo/create\"]')"
        " && !document.querySelector('#live-top form[action=\"/wo/create\"]')"
        " && !document.querySelector('#live-bottom form[action=\"/wo/create\"]')"
    )


def test_live_sync_updates_state_but_keeps_the_half_typed_order(page, server, project):
    page.goto(server)
    form = "form[action='/wo/create']"
    page.select_option(f"{form} select[name='project']", "proj_a")
    page.fill(f"{form} input[name='title']", "half-typed order")
    page.fill(f"{form} textarea[name='description']", "context I am still writing")

    # state changes underneath the open page. A running work order, so it lands as a
    # row: the dashboard collapses everything open that is not running or waiting on a
    # merge, and a brand-new one is neither.
    wo = ops.create_work_order("proj_a", "landed while you were typing")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "running")
    finally:
        store.close()
    page.evaluate("window.jarvisLiveSync()")

    # the live region picked the new work order up...
    assert wo["id"] in page.locator("#live-top").inner_text()
    assert "landed while you were typing" in page.locator("#live-top").inner_text()
    # ...and the form is exactly as the user left it
    assert page.input_value(f"{form} select[name='project']") == "proj_a"
    assert page.input_value(f"{form} input[name='title']") == "half-typed order"
    assert page.input_value(f"{form} textarea[name='description']") == (
        "context I am still writing"
    )


#: The shape of question that made the digest necessary — Neo question #53 was ~7,000
#: characters of feature-order brief rendered inline on this page.
LONG_QUESTION = ("Should the exporter emit CSV or JSON, given the constraints below? "
                 * 30)


def test_a_long_neo_question_is_collapsed_and_the_full_text_opens_from_the_page(
        page, server, daemon, project):
    """The disclosure is a zero-JS `<details>`, so whether it actually HIDES the
    verbatim question is a browser fact: it is in the DOM either way, and only a real
    layout engine can say it is not on screen. That is exactly the property the feature
    is for — the user must not have to scroll past 7,000 characters.

    Both halves in one test: hidden to begin with, and one click away — a page that
    dropped the question entirely would pass the first assertion perfectly.
    """
    wo = ops.create_work_order("proj_a", "pick a format")
    daemon.tick()
    ops.ask_question(wo["id"], LONG_QUESTION)
    daemon._neo_drain()
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)

    page.goto(server + "/neo")
    digest_block = page.locator(".digest").first
    assert digest_block.is_visible()
    assert "digest of:" in digest_block.inner_text()
    assert len(digest_block.locator("li").all()) <= 7   # 5 bullets + 2 options, capped

    verbatim = page.locator(".disclosure .request-text").first
    assert not verbatim.is_visible()                    # collapsed: the point of it all
    page.locator(".disclosure summary").first.click()
    assert verbatim.is_visible()
    assert LONG_QUESTION.strip() in verbatim.inner_text()
    assert wo["id"] in verbatim.inner_text()            # the prompt Neo got, not a copy
