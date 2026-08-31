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
    for label, path in [("neo", "/neo"), ("gates", "/gates"), ("alarms", "/alarms"),
                        ("inbox", "/inbox"), ("backlog", "/backlog"),
                        ("knowledge", "/knowledge"), ("dashboard", "/")]:
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


def _neo_queue(daemon, escalate: bool = True, review: bool = True):
    """Whatever mix of the two asks a test needs, through the real drain."""
    wo = ops.create_work_order("proj_a", "neo tabbed task")
    daemon.tick()
    if review:
        ops.ask_question(wo["id"], "CSV or JSON for the export default?")
    if escalate:
        ops.ask_question(wo["id"], "FORCE_ESCALATE: may I rotate the production key?")
    daemon._neo_drain()
    return wo


def test_neo_tabs_show_one_block_at_a_time(page, server, daemon, project):
    """The three blocks the reader came for used to be one long scroll."""
    _neo_queue(daemon)
    page.goto(f"{server}/neo")

    assert page.locator("#tab-escalated").is_visible()
    for other in ("#tab-review", "#tab-history", "#tab-learnings"):
        assert not page.locator(other).is_visible()

    page.click(".tabs button:has-text('Awaiting review')")
    assert page.locator("#tab-review").is_visible()
    assert not page.locator("#tab-escalated").is_visible()
    assert "CSV or JSON" in page.locator("#tab-review").inner_text()

    page.click(".tabs button:has-text('Learnings')")
    assert page.locator("#tab-learnings").is_visible()
    assert page.locator("form[action='/neo/learn']").is_visible()


def test_neo_opens_on_the_ask_that_is_owed(page, server, daemon, project):
    """Nothing escalated, an answer awaiting review: the review tab is the one open.
    Landing on Escalated-and-empty would hide the only thing the page owes."""
    _neo_queue(daemon, escalate=False)
    page.goto(f"{server}/neo")

    assert page.locator("#tab-review").is_visible()
    assert not page.locator("#tab-escalated").is_visible()


def test_an_ask_is_counted_in_the_strip_while_its_panel_is_shut(page, server, daemon,
                                                                project):
    """The whole licence for putting an ask in a tab: the count stays above the fold,
    in amber, from whichever tab the reader is on. A silent tab would just be the
    scroll again, one fold higher."""
    _neo_queue(daemon)
    page.goto(f"{server}/neo")
    page.click(".tabs button:has-text('History')")

    assert not page.locator("#tab-escalated").is_visible()
    hot = page.locator(".tabs button:has-text('Escalated') .n.hot")
    assert hot.is_visible()
    assert hot.inner_text().strip() == "1"


def test_answering_an_escalation_lands_back_on_its_tab(page, server, daemon, project):
    """Acting on a tab and being returned to the top of a different one is how a
    reader loses their place — and with four tabs there are three wrong ones."""
    _neo_queue(daemon, review=False)
    page.goto(f"{server}/neo")
    page.fill("#tab-escalated textarea[name='text']", "No — wait for the window")
    page.click("#tab-escalated button:has-text('Send answer to worker')")

    assert page.url.endswith("/neo#tab-escalated")
    assert page.locator("#tab-escalated").is_visible()
    assert not page.locator("#tab-history").is_visible()


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


def test_a_timeline_message_opens_the_conversation_at_that_turn(page, server, daemon,
                                                                project):
    """The timeline says a message happened and points at the words. The words are on
    another tab, so following that pointer has to OPEN that tab — the same `hashchange`
    path `#pending` uses, and the only surface that can prove it."""
    wo = ops.create_work_order("proj_a", "answered task")
    daemon.tick()
    qid = ops.ask_question(wo["id"], "CSV or JSON for the export default?")["question_id"]
    ops.neo_answer_escalated(qid, "CSV, and gzip it — every export, no exceptions")

    page.goto(f"{server}/wo/proj_a/{wo['id']}")
    # The ask and the answer are one exchange, on the tab that opens by default.
    said = page.locator("#tab-conversation").inner_text()
    assert "worker → Neo" in said
    assert "CSV or JSON for the export default?" in said
    assert "CSV, and gzip it — every export, no exceptions" in said

    page.click(".tabs button:has-text('Timeline')")
    story = page.locator("#tab-timeline").inner_text()
    assert "You messaged the worker" in story
    assert "CSV, and gzip it — every export, no exceptions" not in story

    page.click("#tab-timeline a:has-text('in the conversation')")
    # `hashchange` fires after the click's own task, so wait rather than sample.
    page.wait_for_selector("#tab-conversation", state="visible")
    assert not page.locator("#tab-timeline").is_visible()
    assert page.locator("#msg-1").is_visible()


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

    # approve from the browser: the queue the reader is working drains, and the
    # verdict is on the history tab
    page.click("button:has-text(\"That's what I'd say\")")
    assert "nothing awaiting review" in page.locator("#tab-review").inner_text()
    page.click(".tabs button:has-text('History')")
    assert "approved" in page.locator("#tab-history").inner_text()

    # second question — correct it, teaching Neo
    ops.ask_question(wo["id"], "And the delimiter?")
    daemon._neo_drain()
    page.goto(f"{server}/neo")
    page.fill("input[name='feedback']", "Semicolons. Excel-friendly.")
    page.click("button:has-text('Correct')")
    page.click(".tabs button:has-text('Learnings')")
    assert "Semicolons. Excel-friendly." in page.locator("#tab-learnings").inner_text()


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


def _shot(page, name):
    """Save a screenshot only when someone asked for one.

    A normal run writes nothing into the tree; `JARVIS_UI_SHOTS=<dir> pytest
    tests_browser` collects them for a review. The alternative — always writing — puts
    binaries in the working tree of everyone who runs the suite.
    """
    import os
    import pathlib

    where = os.environ.get("JARVIS_UI_SHOTS")
    if not where:
        return
    out = pathlib.Path(where)
    out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / f"{name}.png"), full_page=True)


def test_a_burning_turn_is_reviewed_and_acked_in_the_browser(
        page, server, daemon, project, monkeypatch, tmp_path):
    """The review surface end to end: the daemon raises it, the page shows it, one
    button answers it, and the record survives the answer.

    The alarm is produced by running the real `check_burning_turns` against a real
    transcript rather than by writing the event by hand — the page's job is to render
    what the daemon actually produces, and a hand-written row would not test that.
    """
    import json
    import time as _time

    from jarvis import ops, usage
    from jarvis.project_store import ProjectStore

    root = tmp_path / "transcripts"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    wo = ops.create_work_order("proj_a", "plan the observability console")
    store = ProjectStore(project)
    try:
        # The turn genuinely started two hours ago, rather than the daemon being told a
        # lie about the time when it judges: the alarm event then carries a REAL
        # timestamp, which is what the page ages against.
        at = _time.time() - 2 * 3600
        with monkeypatch.context() as clock:
            clock.setattr("jarvis.db.now", lambda: at)
            turn = store.create_turn(wo["id"], "dispatch", "go")
        assert turn["started_at"] == at
        def stamp(t):
            return _time.strftime("%Y-%m-%dT%H:%M:%S.000Z", _time.gmtime(t))
        (root / "-proj" / f"{wo['id']}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in [
                {"type": "user", "timestamp": stamp(at), "promptSource": "sdk",
                 "message": {"content": "You are the worker agent for " + wo["id"]}},
                {"type": "assistant", "timestamp": stamp(at + 5),
                 "message": {"id": "m1", "model": "claude-opus-5",
                             "usage": {"input_tokens": 0,
                                       "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0,
                                       "output_tokens": 1},
                             "content": [{"type": "text", "text": "ok"}]}},
            ]))
        store.update_work_order(wo["id"], status="running", session_id=wo["id"])
        daemon.check_burning_turns(daemon.catalog.projects[0], store)
    finally:
        store.close()

    page.goto(f"{server}/alarms")
    body = page.locator("body").inner_text()
    assert "plan the observability console" in body
    assert "still being billed" in body
    assert page.locator("nav a:has-text('alarms') .nav-badge").is_visible()
    _shot(page, "alarms-asking-for-you")

    page.click("form[action$='/ack'] button")
    after = page.locator("body").inner_text()
    assert "nothing is burning" in after, "the ask is answered"
    assert wo["id"] in after, "and the alarm is still on the record"
    _shot(page, "alarms-after-ack")


def test_the_three_halves_of_the_alarms_page_and_the_alarm_it_links_to(
        page, server, project):
    """§5 of docs/superpowers/specs/2026-08-31-the-supervisor.md, in a browser.

    The supervisor is NOT run: every column it writes is set with `update_alarm`, so
    this proves the page renders a verdict with `supervisor.py` absent — and a
    screenshot does not cost a model call.
    """
    import time as _time

    from jarvis import ops
    from jarvis.project_store import ProjectStore

    asking = ops.create_work_order("proj_a", "port the ingest job to the new schema")
    answered = ops.create_work_order("proj_a", "write the observability design doc")
    settled = ops.create_work_order("proj_a", "rebuild the nightly index")
    store = ProjectStore(project)
    try:
        store.add_alarm(asking["id"], "long-turn", 3,
                        "turn 3 has been running 1h12m and is still being billed")
        store.flag_attention(asking["id"], "a turn is still being billed")

        awaiting = store.add_alarm(
            answered["id"], "long-turn", 1,
            "turn 1 has been running 1h48m and is still being billed")
        store.update_alarm(
            awaiting["id"], status="acked", verdict="ack",
            verdict_reason="the session is reading a 4,000-line spec end to end; the "
                           "hour is the shape of the work, not a stall",
            note="the design doc is long on purpose — nothing is stuck",
            decided_at=_time.time() - 240)

        old = store.add_alarm(settled["id"], "cache-write", 6,
                              "turn 6 re-sent 312k tokens with the cache still warm")
        store.update_alarm(old["id"], status="acked", verdict="ack",
                           verdict_reason="a prefix miss, and the turn recovered",
                           note="one expensive re-write; it did not repeat",
                           review_status="approved", reviewed_at=_time.time() - 60)
    finally:
        store.close()

    page.goto(f"{server}/alarms")
    body = page.locator("body").inner_text()
    assert "port the ingest job to the new schema" in body       # asking for you
    assert "the design doc is long on purpose" in body           # awaiting feedback
    assert "rebuild the nightly index" in body                   # on the record
    _shot(page, "alarms-three-halves")

    page.click(f"a[href='/alarms/proj_a/{awaiting['id']}']")
    one = page.locator("body").inner_text()
    assert "the hour is the shape of the work" in one
    assert "That's the right call" in one
    _shot(page, "alarm-one-in-full")

    page.click("form button.accept")
    assert "you approved this" in page.locator("body").inner_text()
    _shot(page, "alarm-after-review")
