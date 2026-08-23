"""Neo: worker questions queue → Neo answers as the user (or escalates) →
answers deliver to workers → the user reviews → corrections become learnings."""

from __future__ import annotations

import json

import pytest

from jarvis import neo as neo_mod
from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import SEATS, NeoStore
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    result = ops.start_os(str(catalog_file), foreground=True)
    assert result["daemon"]["status"] == "foreground"
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def asked(started, project):
    """A dispatched work order with one question queued for Neo."""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    result = ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")
    return daemon, wo, result


def drain(daemon):
    """Run the Neo drain synchronously (the daemon thread pool is async)."""
    daemon._neo_drain()



def _neo_calls(fake_claude) -> list[dict]:
    """Neo's headless calls only.

    Worker turns are `claude -p` invocations too since the transport moved off
    background sessions, so "has -p" no longer identifies Neo on its own: a turn is the
    one carrying a session id.
    """
    return [c for c in fake_claude.calls
            if "-p" in c["argv"]
            and "--session-id" not in c["argv"] and "--resume" not in c["argv"]]


def test_ask_queues_and_parks_worker(asked, project):
    daemon, wo, result = asked
    assert result["question_id"] == 1
    store = ProjectStore(project)
    try:
        fresh = store.get_work_order(wo["id"])
        # parked, but NOT flagged for the user — Neo exists to absorb these
        assert fresh["status"] == "waiting_input"
        assert not fresh["needs_attention"]
        kinds = [e["kind"] for e in store.list_events(wo["id"])]
        assert "question_asked" in kinds
    finally:
        store.close()
    neo = NeoStore()
    try:
        q = neo.get(1)
        assert q["status"] == "queued"
        assert q["project"] == "proj_a"
        assert "build the exporter" in q["context"]
    finally:
        neo.close()


def test_the_work_order_record_points_at_what_was_asked(asked, project, capsys):
    """The record must lead a reader to the question — it no longer reproduces it.

    Before the question id was stored, `question_asked` said nothing at all and the
    record read as an answer to nothing. It then carried the whole question inline,
    which put the same paragraph on the page twice: once as the event, once as the
    answer's message beneath it. What it carries now is a REFERENCE to the one record
    that holds the question and its answer together — a link on the dashboard, and the
    id the CLI's own question command takes.
    """
    import json as _json

    from jarvis import cli

    _, wo, result = asked
    capsys.readouterr()
    cli.main(["wo", "show", wo["id"], "--json"])
    timeline = _json.loads(capsys.readouterr().out)["timeline"]
    entry = next(e for e in timeline if e["kind"] == "question_asked")
    assert entry["detail"] == ""
    assert entry["ref"] == {"kind": "neo_question", "id": result["question_id"],
                            "label": f"question #{result['question_id']}"}


def test_a_question_event_with_no_id_still_says_what_was_asked():
    """The one case with nowhere to point: no id, so the text is all there is."""
    from jarvis.timeline import build_timeline

    events = [{"ts": 1.0, "kind": "question_asked", "payload": {"question": "CSV?"}}]
    entry = build_timeline({}, events, [])[0]
    assert entry["ref"] is None
    assert entry["detail"] == "CSV?"


def test_neo_answers_and_delivers_to_worker(asked, project, fake_claude):
    daemon, wo, _ = asked
    drain(daemon)
    neo = NeoStore()
    try:
        q = neo.get(1)
        assert q["status"] == "answered"
        assert q["answered_by"] == "neo"
        assert q["answer"].startswith("neo-decision")
        assert q["review_status"] == "unreviewed"
    finally:
        neo.close()
    # the answer is queued to the worker through the normal delivery path
    store = ProjectStore(project)
    try:
        msgs = store.queued_messages(wo["id"])
        assert len(msgs) == 1
        assert msgs[0]["content"].startswith(neo_mod.ANSWER_PREFIX)
        assert msgs[0]["source"] == "neo"
    finally:
        store.close()
    # the headless call carried the persona as a byte-stable system prompt
    calls = _neo_calls(fake_claude)
    assert len(calls) == 1
    argv = calls[0]["argv"]
    system = argv[argv.index("--append-system-prompt") + 1]
    assert "You are Neo" in system
    assert argv[argv.index("--model") + 1] == "opus"  # catalog default


def test_fifo_order_and_backtoback_drain(started, fake_claude):
    daemon = started
    wo1 = ops.create_work_order("proj_a", "task one")
    wo2 = ops.create_work_order("proj_a", "task two")
    daemon.tick()
    ops.ask_question(wo1["id"], "first question")
    ops.ask_question(wo2["id"], "second question")
    ops.ask_question(wo1["id"], "third question")
    drain(daemon)
    calls = _neo_calls(fake_claude)
    prompts = [c["argv"][c["argv"].index("-p") + 1] for c in calls]
    assert [p.splitlines()[-1] for p in prompts] == [
        "first question", "second question", "third question"]
    # cache economics: identical system prompt bytes across the whole drain
    systems = {c["argv"][c["argv"].index("--append-system-prompt") + 1] for c in calls}
    assert len(systems) == 1


def test_escalation_reaches_the_user(asked, project):
    daemon, wo, _ = asked
    ops.ask_question(wo["id"], "FORCE_ESCALATE: may I rotate the production key?")
    drain(daemon)
    neo = NeoStore()
    try:
        q2 = neo.get(2)
        assert q2["status"] == "escalated"
    finally:
        neo.close()
    # escalations DO demand the user: inbox item + wo attention + status listing
    central = CentralStore()
    try:
        items = central.unacked_inbox()
        assert any("Neo escalated" in i["title"] for i in items)
    finally:
        central.close()
    store = ProjectStore(project)
    try:
        assert store.get_work_order(wo["id"])["needs_attention"]
    finally:
        store.close()
    st = ops.os_status()
    assert any(a["status"] == "neo_escalated" for a in st["attention"])
    assert st["neo"]["escalated"] == 1


def test_garbage_output_escalates_not_delivers(asked, project):
    """Unparseable model output must never reach a worker as an answer."""
    daemon, wo, _ = asked
    ops.ask_question(wo["id"], "FORCE_GARBAGE: what about the schema?")
    drain(daemon)
    neo = NeoStore()
    try:
        q2 = neo.get(2)
        assert q2["status"] == "escalated"
        assert "unparseable" in q2["answer_reason"]
    finally:
        neo.close()


def test_user_answers_escalated_question(asked, project):
    daemon, wo, _ = asked
    ops.ask_question(wo["id"], "FORCE_ESCALATE: prod decision")
    drain(daemon)
    result = ops.neo_answer_escalated(2, "Yes, rotate it during the maintenance window")
    assert result["delivery"]["wo_id"] == wo["id"]
    neo = NeoStore()
    try:
        q = neo.get(2)
        assert q["status"] == "answered"
        assert q["answered_by"] == "user"
        assert q["review_status"] == "approved"  # user-authored, nothing to review
    finally:
        neo.close()
    store = ProjectStore(project)
    try:
        contents = [m["content"] for m in store.queued_messages(wo["id"])]
        assert any("[Answer from the user]" in c for c in contents)
    finally:
        store.close()
    # answering resolves the escalation: it leaves the attention list
    st = ops.os_status()
    assert not any(a["status"] == "neo_escalated" for a in st["attention"])


def test_review_approve(asked):
    daemon, wo, _ = asked
    drain(daemon)
    result = ops.neo_review(1, approved=True)
    assert result["review"] == "approved"
    assert not result["learning_recorded"]
    neo = NeoStore()
    try:
        assert neo.get(1)["review_status"] == "approved"
        assert neo.counts()["unreviewed"] == 0
    finally:
        neo.close()


def test_correction_becomes_learning_and_reaches_worker(asked, project):
    daemon, wo, _ = asked
    drain(daemon)
    result = ops.neo_review(1, approved=False,
                            feedback="Always default to CSV; JSON only behind a flag")
    assert result["review"] == "corrected"
    assert result["learning_recorded"]
    assert result["forwarded_to_worker"]
    neo = NeoStore()
    try:
        learnings = neo.learnings("proj_a")
        assert len(learnings) == 1
        assert "Always default to CSV" in learnings[0]["content"]
        assert learnings[0]["source"] == "review"
    finally:
        neo.close()
    store = ProjectStore(project)
    try:
        contents = [m["content"] for m in store.queued_messages(wo["id"])]
        assert any("Correction from the user" in c for c in contents)
    finally:
        store.close()


def test_correction_requires_feedback(asked):
    daemon, wo, _ = asked
    drain(daemon)
    with pytest.raises(ops.OpsError):
        ops.neo_review(1, approved=False, feedback="   ")


def test_learnings_shape_future_answers(asked, fake_claude):
    """The feedback loop: a correction appears in Neo's next system prompt, and the
    prompt grows append-only so the previously cached prefix stays valid."""
    daemon, wo, _ = asked
    drain(daemon)
    calls = _neo_calls(fake_claude)
    system_before = calls[-1]["argv"][calls[-1]["argv"].index("--append-system-prompt") + 1]

    ops.neo_review(1, approved=False, feedback="Prefer CSV, always")
    ops.ask_question(wo["id"], "And what delimiter?")
    drain(daemon)
    calls = _neo_calls(fake_claude)
    system_after = calls[-1]["argv"][calls[-1]["argv"].index("--append-system-prompt") + 1]
    assert "Prefer CSV, always" in system_after
    # append-only: the old prefix (minus the placeholder line) survives verbatim
    head = system_before.replace("(none yet — escalate when unsure)\n", "").rstrip()
    assert system_after.startswith(head.split("# Learnings")[0])
    assert "# Learnings" in system_after


def test_a_seat_scoped_learning_does_not_move_neos_cached_prefix(jarvis_home):
    """A learning routed to one panel seat must be invisible to the single-agent path.

    The panel ships disabled, so the default-configured OS must behave byte-identically
    to today — and `build_system_prompt` calls `store.learnings(project)` with no seat.
    A seat-scoped learning leaking into that query would rewrite the cached prompt
    prefix every time someone taught a seat something, for no behavioural gain.

    The second half is what stops the first from passing vacuously: a filter that
    returned nothing at all would satisfy "the prompt did not move" perfectly.
    """
    store = NeoStore()
    try:
        store.add_learning("Always default to CSV", project="proj_a")
        before = neo_mod.build_system_prompt(store, "proj_a")

        store.add_learning("A grep naming shipit ships nothing",
                           project="proj_a", seat="blast")
        assert neo_mod.build_system_prompt(store, "proj_a") == before

        store.add_learning("Never bundle two decisions in one PR", project="proj_a")
        after = neo_mod.build_system_prompt(store, "proj_a")
        assert after != before
        assert after.startswith(before)          # append-only: the prefix still holds
        assert "A grep naming shipit" not in after
    finally:
        store.close()


def test_with_the_panel_disabled_neo_answers_exactly_as_before(started, project,
                                                               fake_claude):
    """THE REGRESSION PIN FOR THE WHOLE PANEL FEATURE.

    Every work order in this feature ships the panel DISABLED, and the promise at that
    default is not "roughly the same" — it is byte-identical behaviour: the same number of
    Claude calls, the same system prompt bytes, the same message to the worker, and
    nothing at all in `panel_opinions`. If a change alters what a default-configured Neo
    does, it is wrong, and this is where that is caught.

    Asserted on COUNTS and on STORED ROWS rather than on "a verdict came back": the fake
    returns a valid non-escalating verdict for any prompt at all, so "Neo answered" is
    true of a panel run, a single-agent run and very nearly anything else.
    """
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    for question in ("Which delimiter?", "Which encoding?", "Which line ending?"):
        ops.ask_question(wo["id"], question)

    drain(daemon)

    calls = _neo_calls(fake_claude)
    assert len(calls) == 3, "one call per question — no seat, no chair"
    neo = NeoStore()
    try:
        systems = {c["argv"][c["argv"].index("--append-system-prompt") + 1]
                   for c in calls}
        assert systems == {neo_mod.build_system_prompt(neo, "proj_a")}
        assert {c["argv"][c["argv"].index("--model") + 1] for c in calls} == {"opus"}
        for qid in (1, 2, 3):
            assert neo.opinions(qid) == [], "nothing deliberated"
        answers = [neo.get(qid)["answer"] for qid in (1, 2, 3)]
    finally:
        neo.close()

    store = ProjectStore(project)
    try:
        contents = [m["content"] for m in store.queued_messages(wo["id"])]
    finally:
        store.close()
    assert contents == [f"{neo_mod.ANSWER_PREFIX} {a}" for a in answers]
    assert not [s for s in SEATS for c in contents if s in c]


def test_neo_disabled_via_catalog(jarvis_home, fake_claude, tmp_path, project, claude_json):
    data = {
        "os": {"neo": {"enabled": False}},
        "projects": [{"name": "proj_a", "path": str(project)}],
    }
    path = tmp_path / "catalog-noneo.json"
    path.write_text(json.dumps(data))
    ops.start_os(str(path), foreground=True)
    daemon = Daemon(load_catalog(path))
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    ops.ask_question(wo["id"], "anyone home?")
    daemon.neo_tick()
    assert not daemon.neo_draining
    neo = NeoStore()
    try:
        assert neo.get(1)["status"] == "queued"  # untouched — Neo is off
    finally:
        neo.close()


def test_daemon_tick_triggers_drain(asked):
    """The real path: tick() notices queued questions and drains on the neo thread."""
    daemon, wo, _ = asked
    daemon.neo_tick()
    daemon.neo_pool.shutdown(wait=True)  # join the drain thread
    neo = NeoStore()
    try:
        assert neo.get(1)["status"] == "answered"
    finally:
        neo.close()


# -- inspecting the panel's deliberation ------------------------------------------------
#
# The panel itself is another work order's (`src/jarvis/panel.py`); everything below
# drives the SURFACES over the rows it writes, so the opinions are seeded with
# `NeoStore.record_opinion` directly. That is not a shortcut around a real panel run —
# there is no panel to run yet — and every assertion here is about what the CLI and the
# dashboard do with rows that exist, which is exactly what these surfaces own.


def seed_opinions(question_id: int = 1, seats=("premise", "record", "blast")) -> None:
    store = NeoStore()
    try:
        for i, seat in enumerate(seats):
            store.record_opinion(
                question_id, seat,
                reply=f"{seat} says the export should be CSV",
                verdict="answer" if seat != "blast" else "escalate",
                route="panel" if seat == "premise" else "",
                status="ok", model="opus", latency_ms=400 + i,
            )
    finally:
        store.close()


def test_show_panel_lists_every_seat_with_its_status_verdict_and_latency(asked, capsys):
    daemon, _, _ = asked
    drain(daemon)
    seed_opinions()

    from jarvis import cli

    capsys.readouterr()
    assert cli.main(["neo", "show", "1", "--panel"]) == 0
    out = capsys.readouterr().out
    assert "Panel deliberation" in out
    for seat, latency in (("premise", 400), ("record", 401), ("blast", 402)):
        line = next(ln for ln in out.splitlines() if ln.strip().startswith(seat))
        assert "ok" in line
        assert f"{latency}ms" in line
    assert "verdict=escalate" in out          # blast's proposal, not the chair's answer
    assert "verdict=answer" in out


def test_show_without_panel_carries_no_deliberation_into_its_json(asked, capsys):
    """The default document is exactly the question record.

    Anything consuming `jarvis neo show <id> --json` must not silently start carrying
    deliberation it never asked to see — "inspectable on demand" is a promise about the
    default, not only about the dashboard.
    """
    import json as _json

    from jarvis import cli

    daemon, _, _ = asked
    drain(daemon)
    seed_opinions()
    neo = NeoStore()
    try:
        expected_keys = set(neo.get(1))
    finally:
        neo.close()

    capsys.readouterr()
    cli.main(["neo", "show", "1", "--json"])
    doc = _json.loads(capsys.readouterr().out)
    assert isinstance(doc, dict)
    assert set(doc) == expected_keys
    assert "panel_opinions" not in doc

    # …and the same command WITH --panel does carry them, so the assertion above is
    # about suppression rather than about the rows being missing.
    cli.main(["neo", "show", "1", "--panel", "--json"])
    panelled = _json.loads(capsys.readouterr().out)
    assert set(panelled) == expected_keys | {"panel_opinions"}
    assert [o["seat"] for o in panelled["panel_opinions"]] == ["premise", "record", "blast"]


def test_show_panel_on_a_question_no_panel_deliberated_says_so(asked, capsys):
    from jarvis import cli

    daemon, _, _ = asked
    drain(daemon)
    capsys.readouterr()
    assert cli.main(["neo", "show", "1", "--panel"]) == 0   # inspection, not an error
    assert "no panel ran" in capsys.readouterr().out


def test_deliberation_is_never_pushed_yet_is_always_reachable(asked, project, capsys):
    """The design's hard line, as one test with both halves.

    (a) The answer that reaches the worker is the chair's and nothing else: no seat name
    appears in the queued message, and none appears in the inbox either.
    (b) Those very opinions ARE readable through `jarvis neo show --panel`.

    Half (b) is what makes half (a) mean something. Without it the test would pass just
    as well against a database that stored no deliberation at all — it would be
    asserting that absent rows are absent. Together they say the deliberation was
    present and reachable at the moment it was absent from everything pushed.
    """
    from jarvis import cli

    daemon, wo, _ = asked
    seats = ("premise", "record", "blast", "taste")
    drain(daemon)                      # the ordinary delivery path, unchanged
    seed_opinions(1, seats)

    # (a) what was PUSHED names no seat
    store = ProjectStore(project)
    try:
        msgs = store.queued_messages(wo["id"])
        assert len(msgs) == 1
        neo = NeoStore()
        try:
            answer = neo.get(1)["answer"]
        finally:
            neo.close()
        assert msgs[0]["content"] == f"{neo_mod.ANSWER_PREFIX} {answer}"
        for seat in seats:
            assert seat not in msgs[0]["content"]
    finally:
        store.close()
    central = CentralStore()
    try:
        blob = json.dumps(central.unacked_inbox(), default=str)
        for seat in seats:
            assert seat not in blob
    finally:
        central.close()

    # (b) and yet every one of them is there on demand
    capsys.readouterr()
    assert cli.main(["neo", "show", "1", "--panel"]) == 0
    shown = capsys.readouterr().out
    for seat in seats:
        assert seat in shown


# -- seat-scoped corrections ------------------------------------------------------------


def test_a_seat_scoped_correction_teaches_only_that_seat(asked, capsys):
    from jarvis import cli

    daemon, _, _ = asked
    drain(daemon)
    seed_opinions()

    assert cli.main(["neo", "review", "1", "--correct",
                     "A grep naming shipit ships nothing", "--seat", "blast"]) == 0
    neo = NeoStore()
    try:
        globals_only = neo.learnings("proj_a")
        blasts = neo.learnings("proj_a", seat="blast")
        # Naming the row in BOTH calls is the point: an empty list would satisfy "not in
        # the default scope" perfectly while meaning the learning was never written.
        assert not any("shipit" in learning["content"] for learning in globals_only)
        assert any("shipit" in learning["content"] for learning in blasts)
        assert [learning["seat"] for learning in blasts
                if "shipit" in learning["content"]] == ["blast"]
    finally:
        neo.close()


def test_an_unscoped_correction_still_behaves_exactly_as_before(asked, project):
    """The negative control for the whole feature: no `--seat`, no change.

    Mirrors `test_correction_becomes_learning_and_reaches_worker` — a global learning,
    visible in the default scope, forwarded to the worker.
    """
    from jarvis import cli

    daemon, wo, _ = asked
    drain(daemon)
    seed_opinions()          # a panel DID run; without --seat that must not matter

    assert cli.main(["neo", "review", "1", "--correct",
                     "Always default to CSV; JSON only behind a flag"]) == 0
    neo = NeoStore()
    try:
        learnings = neo.learnings("proj_a")
        assert len(learnings) == 1
        assert "Always default to CSV" in learnings[0]["content"]
        assert learnings[0]["seat"] == ""
    finally:
        neo.close()
    store = ProjectStore(project)
    try:
        contents = [m["content"] for m in store.queued_messages(wo["id"])]
        assert any("Correction from the user" in c for c in contents)
    finally:
        store.close()


def test_a_seat_correction_on_a_question_no_panel_answered_is_refused(asked, capsys):
    """No panel ran ⇒ there is no seat that got it wrong, and NOTHING is written."""
    from jarvis import cli

    daemon, _, _ = asked
    drain(daemon)                      # answered single-agent: no opinions at all

    capsys.readouterr()
    assert cli.main(["neo", "review", "1", "--correct", "Prefer CSV",
                     "--seat", "blast"]) == 1
    err = capsys.readouterr().err
    assert "1" in err and "no panel ran" in err
    neo = NeoStore()
    try:
        assert neo.all_learnings() == []                    # no ledger row
        assert neo.get(1)["review_status"] == "unreviewed"  # not half-applied either
    finally:
        neo.close()


def test_a_seat_that_did_not_opine_is_refused_even_though_a_panel_ran(asked, capsys):
    """The fast route runs `premise` alone, so "a panel ran" does not mean this seat saw
    the question. Correcting a seat that never read it teaches the wrong reader."""
    from jarvis import cli

    daemon, _, _ = asked
    drain(daemon)
    seed_opinions(1, ("premise",))

    capsys.readouterr()
    assert cli.main(["neo", "review", "1", "--correct", "Prefer CSV",
                     "--seat", "taste"]) == 1
    err = capsys.readouterr().err
    assert "taste" in err and "premise" in err
    neo = NeoStore()
    try:
        assert neo.all_learnings() == []
    finally:
        neo.close()

    # the control: the seat that DID opine is accepted on the same question
    assert cli.main(["neo", "review", "1", "--correct", "Prefer CSV",
                     "--seat", "premise"]) == 0
    neo = NeoStore()
    try:
        assert [learning["seat"] for learning in neo.all_learnings()] == ["premise"]
    finally:
        neo.close()


def test_an_unknown_seat_is_refused_before_anything_is_written(asked, capsys):
    from jarvis import cli

    daemon, _, _ = asked
    drain(daemon)
    seed_opinions()

    capsys.readouterr()
    assert cli.main(["neo", "review", "1", "--correct", "Prefer CSV",
                     "--seat", "blastradius"]) == 1
    err = capsys.readouterr().err
    assert "blastradius" in err
    assert "blast" in err                       # the error names the real seats
    neo = NeoStore()
    try:
        assert neo.all_learnings() == []
        assert neo.get(1)["review_status"] == "unreviewed"
    finally:
        neo.close()


# -- the ledger commands ----------------------------------------------------------------


def test_export_of_an_empty_home_is_empty_collections_not_an_error(jarvis_home, capsys):
    import json as _json

    from jarvis import cli

    capsys.readouterr()
    assert cli.main(["neo", "export", "--json"]) == 0
    doc = _json.loads(capsys.readouterr().out)
    assert doc == {"questions": [], "learnings": [], "panel_opinions": []}


def test_export_round_trips_every_row_and_every_column(asked, capsys):
    import json as _json

    from jarvis import cli

    daemon, _, _ = asked
    drain(daemon)
    seed_opinions()
    neo = NeoStore()
    try:
        neo.add_learning("Prefer CSV", project="proj_a")
        neo.add_learning("A grep naming shipit ships nothing", seat="blast")
        expected = neo.export()
    finally:
        neo.close()

    capsys.readouterr()
    assert cli.main(["neo", "export", "--json"]) == 0
    doc = _json.loads(capsys.readouterr().out)
    assert doc == expected
    assert len(doc["learnings"]) == 2
    assert len(doc["panel_opinions"]) == 3
    # every column, not a whitelist: a seat-scoped learning is in there WITH its scope,
    # and every row carries the `ts` a replay needs to order the corpus by.
    assert {learning["seat"] for learning in doc["learnings"]} == {"", "blast"}
    for table in doc.values():
        for row in table:
            assert row["ts"] and row["id"]


def test_export_emits_every_column_the_tables_actually_have(asked, capsys):
    """No hand-written whitelist: the columns come from the tables themselves.

    `test_export_round_trips_every_row_and_every_column` compares the CLI against
    `NeoStore.export()`, so it cannot catch a whitelist — both sides would drop the same
    column. This asks SQLite what the columns ARE and demands all of them, which is what
    makes the guarantee survive the next migration.

    It is not hypothetical: `retired_at`/`retired_reason` were added to `learnings` by a
    concurrent work order after this export was written, and reached the document with
    no change here.
    """
    import json as _json

    from jarvis import cli
    from jarvis.neo_store import NeoStore as _NS

    daemon, _, _ = asked
    drain(daemon)
    seed_opinions()
    neo = NeoStore()
    try:
        neo.add_learning("Prefer CSV", project="proj_a")
        actual = {
            table: {r["name"] for r in
                    neo.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for table in _NS.EXPORT_TABLES
        }
    finally:
        neo.close()

    capsys.readouterr()
    cli.main(["neo", "export", "--json"])
    doc = _json.loads(capsys.readouterr().out)
    for table, columns in actual.items():
        assert doc[table], f"{table} seeded no rows, so this asserts nothing"
        for row in doc[table]:
            assert set(row) == columns, table
    # the concrete columns that made this rule worth writing down
    assert {"retired_at", "retired_reason"} <= actual["learnings"]


def test_two_consecutive_exports_are_byte_identical(asked, capsys):
    """No wall-clock field anywhere in the document — a diff of two exports has to show
    what changed in the ledger, not that time passed."""
    from jarvis import cli

    daemon, _, _ = asked
    drain(daemon)
    seed_opinions()

    capsys.readouterr()
    cli.main(["neo", "export", "--json"])
    first = capsys.readouterr().out
    cli.main(["neo", "export", "--json"])
    assert capsys.readouterr().out == first
    assert first.strip()


def test_learnings_print_when_they_were_recorded(jarvis_home, capsys):
    """bl-8427e451: the human listing carried no timestamps, so a learning could not be
    lined up against the decision that produced it."""
    import time as _time

    from jarvis import cli

    pinned = _time.mktime((2026, 3, 5, 14, 30, 0, 0, 0, -1))
    store = NeoStore()
    try:
        row = store.add_learning("Prefer CSV, always", project="proj_a")
        store.conn.execute("UPDATE learnings SET ts=? WHERE id=?", (pinned, row["id"]))
    finally:
        store.close()

    capsys.readouterr()
    assert cli.main(["neo", "learnings", "--project", "proj_a"]) == 0
    out = capsys.readouterr().out
    assert "2026-03-05" in out
    assert "Prefer CSV, always" in out


def test_learnings_json_is_documented_on_the_subcommand_and_round_trips(jarvis_home, capsys):
    """`--json` is accepted in either position ALREADY — `cli.main` strips it from argv
    wherever it appears — so the behavioural half of bl-8427e451 was true before this
    work order. What was missing is that `jarvis neo learnings --help` never said so,
    which is why the backlog item was filed at all. Assert the help text, or this test
    passes against an unchanged parser.
    """
    import json as _json

    from jarvis import cli

    store = NeoStore()
    try:
        store.add_learning("Prefer CSV, always", project="proj_a")
    finally:
        store.close()

    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["neo", "learnings", "--help"])
    assert "--json" in capsys.readouterr().out

    assert cli.main(["neo", "learnings", "--project", "proj_a", "--json"]) == 0
    rows = _json.loads(capsys.readouterr().out)
    assert [r["content"] for r in rows] == ["Prefer CSV, always"]
    for r in rows:
        assert r["ts"] and r["id"]


def test_learnings_seat_filter_shows_a_seat_scoped_row_the_default_hides(jarvis_home, capsys):
    """The sibling of the seat-scoped correction: a ledger command that could never
    display the row `neo review --seat` had just written would be a dead end."""
    import json as _json

    from jarvis import cli

    store = NeoStore()
    try:
        store.add_learning("Prefer CSV, always", project="proj_a")
        store.add_learning("A grep naming shipit ships nothing",
                           project="proj_a", seat="blast")
    finally:
        store.close()

    capsys.readouterr()
    cli.main(["neo", "learnings", "--project", "proj_a", "--json"])
    default = [r["content"] for r in _json.loads(capsys.readouterr().out)]
    assert default == ["Prefer CSV, always"]          # the narrow default is preserved

    cli.main(["neo", "learnings", "--project", "proj_a", "--seat", "blast", "--json"])
    scoped = [r["content"] for r in _json.loads(capsys.readouterr().out)]
    assert "A grep naming shipit ships nothing" in scoped
    assert "Prefer CSV, always" in scoped             # additive, not a replacement

    # and the human line labels the seat, so a scoped row never reads as a global rule
    cli.main(["neo", "learnings", "--project", "proj_a", "--seat", "blast"])
    assert "blast seat" in capsys.readouterr().out


def test_learnings_with_an_unknown_seat_is_refused_rather_than_silently_narrowed(
        jarvis_home, capsys):
    """`learnings(project, seat="typo")` returns the global rows and looks like a
    working filter. Refuse instead."""
    from jarvis import cli

    capsys.readouterr()
    assert cli.main(["neo", "learnings", "--seat", "blastradius"]) == 1
    assert "blastradius" in capsys.readouterr().err


def test_parse_verdict_tolerates_fences():
    v = neo_mod.parse_verdict('```json\n{"escalate": false, "answer": "go", "reason": "r"}\n```')
    # `verdict`/`approve` are only meaningful for approval requests, and absent means
    # "not approved" — an answer that never mentions either must never open a gate.
    assert v == {"escalate": False, "answer": "go", "reason": "r",
                 "approve": False, "verdict": "denied", "dispatch": None,
                 # An answer that proposes no gate exemption proposes none: the empty
                 # string must never reach the rule base as a pattern that matches
                 # everything.
                 "exempt_pattern": ""}
    v = neo_mod.parse_verdict("total nonsense")
    assert v["escalate"] is True


def test_parse_verdict_recovers_a_reply_missing_only_its_final_brace():
    """Question 145, reduced: Neo answered clearly, left off one `}`, and the user was
    interrupted for a decision Neo had already made.

    The greedy span stopped at the nested `dispatch` object's brace rather than the
    outer one, so `json.loads` failed and the fail-safe escalated a settled answer. What
    makes this worth a regression test rather than a one-line fix is the direction of
    the damage: the fail-safe is CORRECT to escalate what it cannot read, so nothing
    downstream was ever going to catch this — the only place it can be fixed is at the
    point where the bytes are read.
    """
    v = neo_mod.parse_verdict(
        '{"escalate": false, "answer": "(b) is right", "reason": "agrees with (b)", '
        '"dispatch": {"title": "t", "description": "d"}')
    assert v["escalate"] is False, "a recoverable reply must not reach the user"
    assert v["answer"] == "(b) is right"
    assert v["dispatch"] == {"title": "t", "description": "d"}


def test_parse_verdict_still_escalates_a_reply_cut_off_mid_answer():
    """The other half of the same boundary, and the more important one.

    A reply whose `answer` string was cut short cannot be repaired without inventing the
    end of it, so it keeps failing toward the user. A worker being handed half a ruling
    it cannot tell from a whole one is the failure this fail-safe exists to prevent.
    """
    v = neo_mod.parse_verdict('{"escalate": false, "answer": "(b) is right because it ke')
    assert v["escalate"] is True
    assert v["answer"] == ""
    assert v["reason"].startswith(neo_mod.UNPARSEABLE_PREFIX)


def test_answer_question_logs_the_full_raw_reply_when_it_cannot_be_parsed(
        jarvis_home, monkeypatch, caplog):
    """Every SURFACE clips the raw reply to 120 characters — the escalation reason, the
    daemon log line, the Telegram push — which is right for a notification and useless
    for a diagnosis. Question 145's root cause sat at character 3161 and was only
    reachable by hand-digging a `~/.claude` session transcript, which is not a file the
    OS owns or can promise still exists. One log line, once, on a path that is rare and
    already costs the user an interruption.
    """
    from jarvis import claude_cli

    raw = '{"escalate": false, "answer": "' + "x" * 500 + '", "reason": "cut off here'
    monkeypatch.setattr(claude_cli, "run_headless_result",
                        lambda *a, **kw: claude_cli.HeadlessResult(text=raw, model="m"))
    store = NeoStore()
    q = {"id": 145, "project": "proj_a", "wo_id": "wo-1", "question": "which?",
         "kind": "question"}
    with caplog.at_level("WARNING", logger="neo"):
        verdict = neo_mod.answer_question(store, q, "m", record=lambda *a, **kw: None)

    assert verdict["escalate"] is True
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert raw in logged, "the WHOLE reply must be in the log, not the 120-char clip"
    assert "145" in logged, "and it must say which question it came from"


def test_answer_question_does_not_log_a_reply_it_could_parse(
        jarvis_home, monkeypatch, caplog):
    """The corollary: a working answer is not three kilobytes of log noise per question.
    Paired with the test above so the trigger cannot silently widen to every reply."""
    from jarvis import claude_cli

    raw = '{"escalate": false, "answer": "go", "reason": "r"}'
    monkeypatch.setattr(claude_cli, "run_headless_result",
                        lambda *a, **kw: claude_cli.HeadlessResult(text=raw, model="m"))
    store = NeoStore()
    q = {"id": 146, "project": "proj_a", "wo_id": "wo-1", "question": "which?",
         "kind": "question"}
    with caplog.at_level("WARNING", logger="neo"):
        verdict = neo_mod.answer_question(store, q, "m", record=lambda *a, **kw: None)

    assert verdict["answer"] == "go"
    assert not [r for r in caplog.records if "raw reply" in r.getMessage()]
