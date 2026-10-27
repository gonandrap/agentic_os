import ast
import inspect
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from jarvis import db
from jarvis.central_store import CentralStore
from jarvis.project_store import (
    ACTIVE_STATUSES,
    FO_OPEN_STATUSES,
    FO_STATUSES,
    FO_TERMINAL_STATUSES,
    OPEN_STATUSES,
    SLOT_STATUSES,
    TERMINAL_STATUSES,
    WO_KINDS,
    WO_STATUSES,
    ProjectStore,
)


def test_work_order_lifecycle(project):
    store = ProjectStore(project)
    wo = store.create_work_order("do a thing", origin="jarvis")
    assert wo["status"] == "pending"

    claimed = store.claim_next_pending()
    assert claimed["id"] == wo["id"]
    assert claimed["status"] == "dispatching"
    assert store.claim_next_pending() is None  # nothing else pending

    store.set_status(wo["id"], "running", session_id="s-1")
    assert store.count_active() == 1
    assert store.find_by_session("s-1")["id"] == wo["id"]

    events = store.list_events(wo["id"])
    assert [e["kind"] for e in events][:2] == ["created", "status"]


def test_find_by_session(project):
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    store.update_work_order(wo["id"], session_id="sess-42")
    assert store.find_by_session("sess-42")["id"] == wo["id"]
    assert store.find_by_session("nope") is None


def test_messages_queue(project):
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    mid = store.queue_message(wo["id"], "hello", source="ui")
    assert [m["id"] for m in store.queued_messages()] == [mid]
    store.mark_message(mid, "delivered")
    assert store.queued_messages() == []
    msgs = store.list_messages(wo["id"])
    assert msgs[0]["status"] == "delivered"


def test_assumptions_flow(project):
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    aid = store.add_assumption(wo["id"], "assumed sqlite")
    assert len(store.pending_assumptions(wo["id"])) == 1
    store.review_assumption(aid, "accepted")
    assert store.pending_assumptions(wo["id"]) == []


def test_add_assumption_still_takes_two_positional_arguments(project):
    """The cited-section columns are OPTIONAL keywords: every existing caller"""
    store = ProjectStore(project)
    wo = store.create_work_order("x")

    store.add_assumption(wo["id"], "assumed sqlite")
    store.add_assumption(wo["id"], "errors raise", spec_path="docs/x.md",
                         spec_which="3", spec_text="## 3. Errors\n\nThey raise.")

    plain, cited = store.all_assumptions(wo["id"])
    assert (plain["spec_path"], plain["spec_which"], plain["spec_text"]) == (None,) * 3
    assert cited["spec_path"] == "docs/x.md" and cited["spec_which"] == "3"
    assert "They raise." in cited["spec_text"]
    store.close()


def test_the_cited_section_columns_migrate_onto_a_pre_existing_database(project):
    """Spec test 8: the three columns ship in `ADDED_COLUMNS`"""
    store = ProjectStore(project)
    wo_id = store.create_work_order("filed before the columns existed")["id"]
    db_path = store.db_path
    store.close()

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        DROP TABLE assumptions;
        CREATE TABLE assumptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wo_id TEXT NOT NULL REFERENCES work_orders(id),
            ts REAL NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        );
    """)
    conn.execute("INSERT INTO assumptions (wo_id, ts, content) VALUES (?, 0.0, ?)",
                 (wo_id, "assumed sqlite"))
    conn.commit()
    conn.close()

    store = ProjectStore(project)
    try:
        store.add_assumption(wo_id, "errors raise", spec_path="docs/x.md",
                             spec_which="3", spec_text="docs/x.md § 3:\n## 3. Errors")
        old, cited = store.all_assumptions(wo_id)
    finally:
        store.close()
    assert (old["spec_path"], old["spec_which"], old["spec_text"]) == (None,) * 3
    assert (cited["spec_path"], cited["spec_which"]) == ("docs/x.md", "3")
    assert "## 3. Errors" in cited["spec_text"]


def test_assumptions_are_numbered_from_one_per_work_order(project):
    """`n` is what the timeline says instead of repeating the text"""
    store = ProjectStore(project)
    a, b = store.create_work_order("a"), store.create_work_order("b")
    first = store.add_assumption(a["id"], "assumed sqlite")
    store.add_assumption(a["id"], "assumed UTF-8")
    store.add_assumption(b["id"], "assumed the other thing")

    assert [x["n"] for x in store.all_assumptions(a["id"])] == [1, 2]
    assert [x["n"] for x in store.all_assumptions(b["id"])] == [1]
    # The event says the same number, so the timeline and the listing agree
    events = [e for e in store.list_events(a["id"]) if e["kind"] == "assumption"]
    assert [json.loads(e["payload"])["n"] for e in events] == [1, 2]

    # Reviewing #1 leaves #2 called #2
    store.review_assumption(first, "accepted")
    assert [x["n"] for x in store.pending_assumptions(a["id"])] == [2]
    store.close()


def test_notifications_outbox(project):
    store = ProjectStore(project)
    store.add_notification("t1", "b1", level="warning")
    items = store.unrouted_notifications()
    assert len(items) == 1
    store.mark_notification_routed(items[0]["id"])
    assert store.unrouted_notifications() == []


def test_summary(project):
    store = ProjectStore(project)
    a = store.create_work_order("a")
    store.create_work_order("b")
    store.set_status(a["id"], "completed")
    store.flag_attention(a["id"], "check me")
    s = store.summary()
    assert s["by_status"] == {"completed": 1, "pending": 1}
    assert s["needs_attention"] == 1


def test_backlog_dependencies(jarvis_home):
    central = CentralStore()
    a = central.add_backlog("p", "first")
    b = central.add_backlog("p", "second", depends_on=[a["id"]])
    with pytest.raises(KeyError):
        central.add_backlog("p", "bad", depends_on=["bl-missing1"])

    blockers = central.unfinished_dependencies(b["id"])
    assert [x["id"] for x in blockers] == [a["id"]]
    central.mark_backlog(a["id"], "done")
    assert central.unfinished_dependencies(b["id"]) == []


def test_inbox_ack(jarvis_home):
    central = CentralStore()
    i1 = central.add_inbox("p", "alert 1", level="critical")
    central.add_inbox("p", "alert 2")
    assert len(central.unacked_inbox()) == 2
    central.ack_inbox(i1)
    assert len(central.unacked_inbox()) == 1
    assert central.ack_inbox() == 1  # ack all
    assert central.unacked_inbox() == []


def test_knowledge_relevance(jarvis_home):
    central = CentralStore()
    central.add_knowledge("global tip", project="")
    central.add_knowledge("proj tip", project="p1")
    central.add_knowledge("other proj tip", project="p2")
    got = [k["content"] for k in central.relevant_knowledge("p1")]
    assert "proj tip" in got and "global tip" in got and "other proj tip" not in got
    assert central.search_knowledge("global")[0]["content"] == "global tip"


def test_status_counts_are_not_capped_by_the_listing_limit(project):
    """Counted in SQL, so a project with more history than one page still adds up"""
    store = ProjectStore(project)
    for i in range(5):
        store.set_status(store.create_work_order(f"done {i}")["id"], "completed")
    store.create_work_order("open one")
    hidden = store.create_work_order("hidden and cancelled")
    store.set_status(hidden["id"], "cancelled")
    store.set_hidden(hidden["id"], True)

    assert store.status_counts() == {"completed": 5, "pending": 1}
    assert store.status_counts(include_hidden=True) == {
        "completed": 5, "pending": 1, "cancelled": 1,
    }
    assert len(store.list_work_orders(limit=2)) == 2  # the listing still pages


# -- the validation panel's storage


def test_validating_sits_between_the_worker_and_the_review(project):
    """The INDEX, not mere membership"""
    assert WO_STATUSES.index("validating") == WO_STATUSES.index("needs_review") - 1
    assert FO_STATUSES.index("validating") == FO_STATUSES.index("executing") + 1
    assert FO_STATUSES.index("validating") == FO_STATUSES.index("completed") - 1
    # A unit under validation holds a live session (#134)
    assert "validating" in OPEN_STATUSES and "validating" in ACTIVE_STATUSES
    assert "validating" not in SLOT_STATUSES
    assert "validating" in FO_OPEN_STATUSES
    assert "validating" not in TERMINAL_STATUSES
    assert "validating" not in FO_TERMINAL_STATUSES


def test_a_round_number_is_unique_per_subject_but_the_two_subjects_are_independent(
        project):
    """The partial indexes have to actually bite"""
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    fo = store.create_feature_order("f")

    # round 1 on each subject, side by side: independent counters, not one sequence
    first = store.open_validation_round(wo_id=wo["id"], fingerprint="a")
    other = store.open_validation_round(fo_id=fo["id"], fingerprint="b")
    assert first["round"] == other["round"] == 1

    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO validation_rounds (wo_id, round, ts, fingerprint)"
            " VALUES (?,1,1.0,'dup')", (wo["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO validation_rounds (fo_id, round, ts, fingerprint)"
            " VALUES (?,1,1.0,'dup')", (fo["id"],))


def test_a_round_belongs_to_exactly_one_subject(project):
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    fo = store.create_feature_order("f")

    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO validation_rounds (wo_id, fo_id, round, ts, fingerprint)"
            " VALUES (?,?,1,1.0,'both')", (wo["id"], fo["id"]))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO validation_rounds (round, ts, fingerprint)"
            " VALUES (1,1.0,'neither')")
    # ...and the store's own methods refuse the same two before SQL ever sees them
    with pytest.raises(ValueError):
        store.open_validation_round(wo_id=wo["id"], fo_id=fo["id"], fingerprint="both")
    with pytest.raises(ValueError):
        store.validation_rounds()


def test_deleting_a_work_order_takes_its_rounds_and_leaves_its_siblings(project):
    store = ProjectStore(project)
    doomed = store.create_work_order("doomed")
    sibling = store.create_work_order("sibling")
    for wo_id in (doomed["id"], sibling["id"]):
        rnd = store.open_validation_round(wo_id=wo_id, fingerprint="f")
        store.record_validation_opinion(rnd["id"], "tester", verdict="pass")

    store.delete_work_order(doomed["id"])

    assert store.validation_rounds(wo_id=doomed["id"]) == []
    kept = store.validation_rounds(wo_id=sibling["id"])
    assert len(kept) == 1
    assert len(store.validation_opinions(kept[0]["id"])) == 1
    assert store.conn.execute(
        "SELECT COUNT(*) c FROM validation_opinions").fetchone()["c"] == 1


def test_deleting_a_feature_order_takes_its_rounds_and_leaves_its_siblings(project):
    store = ProjectStore(project)
    doomed = store.create_feature_order("doomed")
    sibling = store.create_feature_order("sibling")
    for fo_id in (doomed["id"], sibling["id"]):
        rnd = store.open_validation_round(fo_id=fo_id, fingerprint="f")
        store.record_validation_opinion(rnd["id"], "chair", verdict="pass")

    store.conn.execute("DELETE FROM feature_orders WHERE id=?", (doomed["id"],))

    assert store.validation_rounds(fo_id=doomed["id"]) == []
    kept = store.validation_rounds(fo_id=sibling["id"])
    assert len(kept) == 1
    assert len(store.validation_opinions(kept[0]["id"])) == 1


def test_a_seat_speaking_twice_replaces_its_opinion(project):
    """A retried seat must leave one row, not two: the arbiter counts verdicts"""
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="f")

    store.record_validation_opinion(rnd["id"], "tester", reply="timed out",
                                    status="failed")
    store.record_validation_opinion(rnd["id"], "tester", reply="looks covered",
                                    verdict="pass", model="opus", latency_ms=1200)
    store.record_validation_opinion(rnd["id"], "security", verdict="reject")

    opinions = store.validation_opinions(rnd["id"])
    assert [o["seat"] for o in opinions] == ["tester", "security"]
    assert opinions[0]["verdict"] == "pass" and opinions[0]["status"] == "ok"
    assert opinions[0]["model"] == "opus" and opinions[0]["latency_ms"] == 1200


def test_rounds_come_back_oldest_first_and_the_latest_is_the_highest(project):
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    never_judged = store.create_work_order("untouched")
    for n in range(1, 4):
        store.open_validation_round(wo_id=wo["id"], fingerprint=f"f{n}")

    assert [r["round"] for r in store.validation_rounds(wo_id=wo["id"])] == [1, 2, 3]
    assert store.latest_validation_round(wo_id=wo["id"])["fingerprint"] == "f3"
    # paired: a subject nobody has judged has no latest round rather than a stale one
    assert store.latest_validation_round(wo_id=never_judged["id"]) is None


def test_only_the_documented_outcomes_and_statuses_are_accepted(project):
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="f")
    assert rnd["outcome"] == "pending"

    store.close_validation_round(rnd["id"], "escalated", reason="three rounds, no deal")
    closed = store.latest_validation_round(wo_id=wo["id"])
    assert closed["outcome"] == "escalated"
    assert closed["reason"] == "three rounds, no deal"
    with pytest.raises(AssertionError):
        store.close_validation_round(rnd["id"], "approved")   # a Neo word, not ours

    store.record_validation_opinion(rnd["id"], "architect", status="abstained")
    with pytest.raises(AssertionError):
        store.record_validation_opinion(rnd["id"], "architect", status="timeout")
    with pytest.raises(AssertionError):
        store.record_validation_opinion(rnd["id"], "architect", verdict="approve")


def test_the_manager_the_analyst_and_the_investigator_are_the_other_kinds(project):
    store = ProjectStore(project)
    assert WO_KINDS == ("worker", "planner", "manager", "analyst", "investigator")

    manager = store.create_work_order("own the feature", kind="manager")
    analyst = store.create_work_order("plan the improvement", kind="analyst")
    investigator = store.create_work_order("diagnose wo-1", kind="investigator")

    assert store.get_work_order(manager["id"])["kind"] == "manager"
    assert store.get_work_order(analyst["id"])["kind"] == "analyst"
    assert store.get_work_order(investigator["id"])["kind"] == "investigator"
    with pytest.raises(AssertionError):
        store.create_work_order("x", kind="supervisor")


# -- write transactions and a second connection


def _writes_from_another_thread(project) -> tuple[threading.Thread, threading.Event,
                                                  threading.Event]:
    """A second connection committing one row, on the daemon's side of the fence"""
    go, done = threading.Event(), threading.Event()

    def interloper() -> None:
        other = ProjectStore(project)
        try:
            assert go.wait(timeout=15)
            other.create_work_order("what the validate thread was writing")
            done.set()
        finally:
            other.close()

    return threading.Thread(target=interloper, daemon=True), go, done


def test_releasing_a_plan_survives_a_commit_from_another_connection(project):
    """The race that flaked CI: `review_plan` creating a feature's children while the daemon's validate..."""
    store = ProjectStore(project)
    fo = store.create_feature_order("a feature", description="the ask")
    plan = [{"key": "one", "title": "Build one", "description": "d", "needs": []},
            {"key": "two", "title": "Build two", "description": "d", "needs": ["one"]}]
    thread, go, done = _writes_from_another_thread(project)
    thread.start()

    read = False

    def let_the_other_thread_in(sql: str) -> None:
        """Fires as each statement starts"""
        nonlocal read
        head = sql.strip().split()[0].upper()
        if head == "SELECT":
            read = True
        elif read and head == "INSERT" and not go.is_set():
            go.set()
            done.wait(timeout=0.5)  # times out once the write lock is taken up front

    store.conn.set_trace_callback(let_the_other_thread_in)
    try:
        created = store.create_plan_children(fo["id"], plan, manager=True)
    finally:
        store.conn.set_trace_callback(None)
        thread.join(timeout=15)

    assert [wo["title"] for wo in created] == ["Build one", "Build two"]
    assert store.manager_work_order(fo["id"]) is not None
    assert done.is_set(), "the other connection never got its write in"


def test_only_db_write_transaction_opens_a_transaction():
    """THE GUARD"""
    body, first = inspect.getsourcelines(db.write_transaction)
    allowed = range(first, first + len(body))
    offenders = []

    for path in sorted(Path(db.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute" and node.args):
                continue
            sql = node.args[0]
            if not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)):
                continue
            head = (sql.value.strip().split() or [""])[0].upper()
            if head not in ("BEGIN", "COMMIT", "ROLLBACK"):
                continue
            if path.name == "db.py" and node.lineno in allowed:
                # The one it is allowed to run, and only in its IMMEDIATE form: a deferred BEGIN here would put the...
                if head == "BEGIN" and sql.value.strip().upper() != "BEGIN IMMEDIATE":
                    offenders.append(f"{path.name}:{node.lineno} deferred BEGIN")
                continue
            offenders.append(f"{path.name}:{node.lineno} {head}")

    assert offenders == [], (
        "open transactions with `db.write_transaction`, not a bare BEGIN — its "
        "docstring says what a deferred one costs")


def test_a_settling_round_reaches_another_connection_with_its_event(project):
    """Issue #1203: the invariant sweep read a settled round whose event was not written yet"""
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="f")
    go, done, ready = threading.Event(), threading.Event(), threading.Event()
    seen: dict = {}

    def sweeper() -> None:
        other = ProjectStore(project)   # opened BEFORE the write: construction writes too
        ready.set()
        try:
            assert go.wait(timeout=15)
            seen["outcome"] = other.latest_validation_round(wo_id=wo["id"])["outcome"]
            seen["kinds"] = [e["kind"] for e in other.list_events(wo["id"])]
        finally:
            other.close()
            done.set()

    thread = threading.Thread(target=sweeper, daemon=True)
    thread.start()
    assert ready.wait(timeout=15)

    def let_the_sweep_read_between_the_two_writes(sql: str) -> None:
        if sql.strip().upper().startswith("INSERT INTO WO_EVENTS") and not go.is_set():
            go.set()
            done.wait(timeout=15)

    store.conn.set_trace_callback(let_the_sweep_read_between_the_two_writes)
    try:
        store.settle_validation_round(rnd["id"], "escalated",
                                      {"round": 1, "round_id": rnd["id"]},
                                      wo_id=wo["id"], reason="three rounds, no deal")
    finally:
        store.conn.set_trace_callback(None)
        thread.join(timeout=15)

    assert done.is_set(), "the other connection never got its read in"
    assert (seen["outcome"] == "pending") or ("validation_escalated" in seen["kinds"]), (
        "a second connection saw the round settled with no event on the timeline")
    assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "escalated"
    assert "validation_escalated" in [e["kind"] for e in store.list_events(wo["id"])]


def test_a_status_move_reaches_another_connection_with_its_event(project):
    """Issue #1205: the sweep read a span whose `status` event was not written yet"""
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    go, done, ready = threading.Event(), threading.Event(), threading.Event()
    seen: dict = {}

    def sweeper() -> None:
        other = ProjectStore(project)   # opened BEFORE the write: construction writes too
        ready.set()
        try:
            assert go.wait(timeout=15)
            seen["spans"] = [s["to_status"] for s in other.state_spans(wo["id"])]
            seen["events"] = [(db.from_json(e["payload"], {}) or {}).get("status")
                              for e in other.events_of_kind(wo["id"], "status")]
        finally:
            other.close()
            done.set()

    thread = threading.Thread(target=sweeper, daemon=True)
    thread.start()
    assert ready.wait(timeout=15)

    def let_the_sweep_read_between_the_span_and_its_event(sql: str) -> None:
        # The callback runs BEFORE its statement: the span is written by now, the event is not
        if sql.strip().upper().startswith("INSERT INTO WO_EVENTS") and not go.is_set():
            go.set()
            done.wait(timeout=15)

    store.conn.set_trace_callback(let_the_sweep_read_between_the_span_and_its_event)
    try:
        store.set_status(wo["id"], "needs_review")
    finally:
        store.conn.set_trace_callback(None)
        thread.join(timeout=15)

    assert done.is_set(), "the other connection never got its read in"
    assert "needs_review" not in seen["spans"] or "needs_review" in seen["events"], (
        "a second connection saw a span with no `status` event beside it")
    assert "needs_review" in [s["to_status"] for s in store.state_spans(wo["id"])]


def test_a_repair_is_dropped_when_the_row_left_the_status_it_derived_from(project):
    """Issue #1205, second half: a repair writes only against the row it derived from"""
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    store.set_status(wo["id"], "validating")
    store.flag_attention(wo["id"], "something")

    def retire() -> None:
        store.set_status(wo["id"], "completed")
        store.clear_attention(wo["id"])

    assert store.repair_while(wo["id"], "validating", retire)
    assert store.get_work_order(wo["id"])["status"] == "completed"
    assert not store.get_work_order(wo["id"])["needs_attention"]

    assert not store.repair_while(wo["id"], "validating",
                                  lambda: store.flag_attention(wo["id"], "stale"))
    assert not store.get_work_order(wo["id"])["needs_attention"]

    fo = store.create_feature_order("a feature", description="all of it")
    store.set_feature_status(fo["id"], "failed")
    assert not store.repair_while(
        fo["id"], "executing",
        lambda: store.flag_feature_attention(fo["id"], "stale"), kind="fo")
    assert not store.get_feature_order(fo["id"])["needs_attention"]


def test_approvals_of_kind_is_not_bounded_by_the_generic_listings_window(project):
    """`list_approvals` answers the 200 NEWEST rows of every kind"""
    # Fixture commands stay non-commands: a real `gh pr merge …` literal here trips the privileged-action...
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    wanted = store.add_approval(wo["id"], "self_heal", "fix wo-1", status="pending")
    store.decide_approval(wanted["id"], "approved", "go on", "test")
    for i in range(250):
        other = store.add_approval(wo["id"], "other_kind", f"some command {i}",
                                   status="pending")
        store.decide_approval(other["id"], "approved", "go on", "test")

    assert wanted["id"] not in [a["id"] for a in
                               store.list_approvals(statuses=("approved",))]
    found = store.approvals_of_kind("self_heal", "approved")
    assert [a["id"] for a in found] == [wanted["id"]]
    # One status only: the approved list never carries a pending row
    assert store.approvals_of_kind("self_heal", "pending") == []


# -- the per-order observability override


def test_the_observability_column_is_written_at_creation_and_by_the_setter(project):
    store = ProjectStore(project)
    wo = store.create_work_order("stamped", observability="full")
    assert store.get_work_order(wo["id"])["observability"] == "full"

    store.set_observability(wo["id"], "off")
    assert store.get_work_order(wo["id"])["observability"] == "off"

    store.set_observability(wo["id"], None)      # clear: back to "no answer"
    assert store.get_work_order(wo["id"])["observability"] is None

    with pytest.raises(ValueError, match="observability"):
        store.set_observability(wo["id"], "verbose")
    with pytest.raises(ValueError, match="observability"):
        store.create_work_order("bad level", observability="loud")


def test_a_row_that_predates_the_column_has_no_answer_and_is_not_off(project):
    """A pre-existing row: the column was added by `ADDED_COLUMNS`, so it is NULL"""
    from jarvis import observability
    from jarvis.catalog import ObservabilityConfig

    store = ProjectStore(project)
    wo = store.create_work_order("filed before the column existed")
    store.conn.execute("UPDATE work_orders SET observability=NULL WHERE id=?",
                       (wo["id"],))
    store.conn.commit()
    row = store.get_work_order(wo["id"])

    assert row["observability"] is None
    assert observability.level_for(row, ObservabilityConfig(level="full")) == "full"
    assert observability.records_context(row, ObservabilityConfig()) is True


# -- what a fix order carries


def test_the_self_evolution_columns_are_written_at_creation(project):
    """Set STRUCTURALLY and never inferred from prose (kn-5d8a396a): they arrive as keyword arguments"""
    store = ProjectStore(project)
    wo = store.create_work_order(
        "close the stale-hold gap", description="no slug appears in this text",
        gap_class="stale-hold", detector_id="dt-7", remedy_rule_id="rr-3",
        trigger_wo_id="wo-trigger")
    row = store.get_work_order(wo["id"])

    assert row["gap_class"] == "stale-hold"
    assert row["detector_id"] == "dt-7"
    assert row["remedy_rule_id"] == "rr-3"
    assert row["trigger_wo_id"] == "wo-trigger"

    # The ordinary order, which is almost every order: all four NULL
    plain = store.get_work_order(store.create_work_order("ordinary")["id"])
    assert [plain["gap_class"], plain["detector_id"], plain["remedy_rule_id"],
            plain["trigger_wo_id"]] == [None, None, None, None]


def test_an_uncounted_round_records_why_it_is_uncounted(project):
    """Two causes now open a round outside the budget (docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md)"""
    from jarvis import ops

    store = ProjectStore(project)
    wo = store.create_work_order("x")
    ordinary = store.open_validation_round(wo_id=wo["id"], fingerprint="f1")
    rebind = store.open_validation_round(wo_id=wo["id"], fingerprint="f2",
                                         uncounted=True,
                                         uncounted_cause=ops.REBIND_CAUSE)
    rework = store.open_validation_round(wo_id=wo["id"], fingerprint="f3",
                                         uncounted=True,
                                         uncounted_cause=ops.USER_REWORK_CAUSE)

    assert ordinary["uncounted_cause"] == ""
    assert rebind["uncounted_cause"] == ops.REBIND_CAUSE
    assert rework["uncounted_cause"] == ops.USER_REWORK_CAUSE
    assert store.uncounted_validation_rounds(wo_id=wo["id"]) == 2
    assert store.uncounted_validation_rounds(
        wo_id=wo["id"], cause=ops.REBIND_CAUSE) == 1
    assert store.uncounted_validation_rounds(
        wo_id=wo["id"], cause=ops.USER_REWORK_CAUSE) == 1
