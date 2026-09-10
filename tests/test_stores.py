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


def test_assumptions_are_numbered_from_one_per_work_order(project):
    """`n` is what the timeline says instead of repeating the text, so it has to be a
    POSITION within this work order — a row id would start the second work order's list
    at 3 — and it has to survive a review, which is what turns the list into a subset."""
    store = ProjectStore(project)
    a, b = store.create_work_order("a"), store.create_work_order("b")
    first = store.add_assumption(a["id"], "assumed sqlite")
    store.add_assumption(a["id"], "assumed UTF-8")
    store.add_assumption(b["id"], "assumed the other thing")

    assert [x["n"] for x in store.all_assumptions(a["id"])] == [1, 2]
    assert [x["n"] for x in store.all_assumptions(b["id"])] == [1]
    # The event says the same number, so the timeline and the listing agree.
    events = [e for e in store.list_events(a["id"]) if e["kind"] == "assumption"]
    assert [json.loads(e["payload"])["n"] for e in events] == [1, 2]

    # Reviewing #1 leaves #2 called #2. Renumbering the remainder would rewrite what the
    # timeline already said about an assumption nobody touched.
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
    """Counted in SQL, so a project with more history than one page still adds up.

    `list_work_orders` stops at `limit`; counting its result would under-report exactly
    where the count matters most.
    """
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


# -- the validation panel's storage ---------------------------------------------------
#
# Rounds hang off EITHER a work order or a feature order. That polymorphism is where
# every trap in this table lives, and each test below pairs the thing that must fail
# with the thing that must still work — because the two obvious wrong schemas each pass
# one half cleanly.


def test_validating_sits_between_the_worker_and_the_review(project):
    """The INDEX, not mere membership.

    WO_STATUSES is the order the dashboard renders status counts in, so a status
    appended at the end passes every `in` test and still reads wrong on the page.
    """
    assert WO_STATUSES.index("validating") == WO_STATUSES.index("needs_review") - 1
    assert FO_STATUSES.index("validating") == FO_STATUSES.index("executing") + 1
    assert FO_STATUSES.index("validating") == FO_STATUSES.index("completed") - 1
    # A unit under validation holds a live session and must spend a concurrency slot,
    # but it is not settled.
    assert "validating" in OPEN_STATUSES and "validating" in ACTIVE_STATUSES
    assert "validating" in FO_OPEN_STATUSES
    assert "validating" not in TERMINAL_STATUSES
    assert "validating" not in FO_TERMINAL_STATUSES


def test_a_round_number_is_unique_per_subject_but_the_two_subjects_are_independent(
        project):
    """The partial indexes have to actually bite.

    `UNIQUE (wo_id, fo_id, round)` is the natural-looking schema and it enforces
    NOTHING: every row has a NULL in one id column and SQLite treats NULLs as distinct.
    It would pass the coexistence half below — which is the half that feels interesting
    — and fail both duplicate halves, so the three have to be asserted together.
    """
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
    """A retried seat must leave one row, not two: the arbiter counts verdicts."""
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


def test_the_manager_is_the_third_and_only_other_work_order_kind(project):
    store = ProjectStore(project)
    assert WO_KINDS == ("worker", "planner", "manager")

    manager = store.create_work_order("own the feature", kind="manager")

    assert store.get_work_order(manager["id"])["kind"] == "manager"
    with pytest.raises(AssertionError):
        store.create_work_order("x", kind="supervisor")


# -- write transactions and a second connection ---------------------------------------
#
# The daemon reaches one project database from several threads at once, each with its
# OWN connection (`Daemon._validate_feature`). WAL lets a reader and a writer coexist,
# with ONE exception: a transaction that READS before it WRITES holds a snapshot, and
# the upgrade to a write fails outright if anyone committed after that snapshot was
# taken. The failure is SQLITE_BUSY_SNAPSHOT — `database is locked`, raised WITHOUT
# consulting `busy_timeout`, so nothing configured in `db.connect` can wait it out.


def _writes_from_another_thread(project) -> tuple[threading.Thread, threading.Event,
                                                  threading.Event]:
    """A second connection committing one row, on the daemon's side of the fence.

    Opened INSIDE the thread: a sqlite connection belongs to the thread that made it,
    which is why the daemon's validate pool opens its own (`Daemon._validate_feature`).
    """
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
    """The race that flaked CI: `review_plan` creating a feature's children while the
    daemon's validate thread commits to the same database. The interloper is let in
    between the transaction's first read and its first write — the one window in which
    a snapshot exists to go stale."""
    store = ProjectStore(project)
    fo = store.create_feature_order("a feature", description="the ask")
    plan = [{"key": "one", "title": "Build one", "description": "d", "needs": []},
            {"key": "two", "title": "Build two", "description": "d", "needs": ["one"]}]
    thread, go, done = _writes_from_another_thread(project)
    thread.start()

    read = False

    def let_the_other_thread_in(sql: str) -> None:
        """Fires as each statement starts, so arming on the INSERT puts the interloper
        after the SELECT has taken the snapshot and before the write is attempted."""
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
    """THE GUARD. The hazard above is invisible at the call site — a bare `BEGIN` reads
    exactly like the safe thing — so the rule is checked on the source rather than left
    to review. Every `BEGIN`/`COMMIT`/`ROLLBACK` in `src/jarvis` must be inside
    `db.write_transaction`, which is the one place that knows to make it IMMEDIATE."""
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
                # The one it is allowed to run, and only in its IMMEDIATE form: a
                # deferred BEGIN here would put the hazard back in every call site at
                # once, and it reads identically.
                if head == "BEGIN" and sql.value.strip().upper() != "BEGIN IMMEDIATE":
                    offenders.append(f"{path.name}:{node.lineno} deferred BEGIN")
                continue
            offenders.append(f"{path.name}:{node.lineno} {head}")

    assert offenders == [], (
        "open transactions with `db.write_transaction`, not a bare BEGIN — its "
        "docstring says what a deferred one costs")
