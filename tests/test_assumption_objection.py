"""The objection: recorded first, sent second, and withdrawn when the order stops (docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md)"""

from __future__ import annotations

import json
import sqlite3

import pytest

from jarvis import autoreview, bus, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ADDED_COLUMNS, ProjectStore

REASON = "the helper is not idempotent on retry, so the second call double-counts"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def spec(daemon):
    s = daemon.catalog.project("proj_a")
    s.validation.enabled = True
    s.validation.auto_review = True
    return s


def running_order(store, *, verdict: str = "object", status: str = "running",
                  text: str = "the API is idempotent"):
    """A work order in `status` with one assumption carrying `verdict`"""
    wo = store.create_work_order(title="an order mid-flight", description="d")
    aid = store.add_assumption(wo["id"], text)
    if verdict:
        store.record_provisional(aid, verdict=verdict, reason=REASON, model="opus",
                                 stakes="routine")
    store.set_status(wo["id"], status)
    # `all_assumptions`, not `get_assumption`: `n` is computed there and it is the number every event
    row = next(a for a in store.all_assumptions(wo["id"]) if a["id"] == aid)
    return store.get_work_order(wo["id"]), row


def events(store, wo_id: str, kind: str) -> list[dict]:
    return [json.loads(e["payload"]) for e in store.events_of_kind(wo_id, kind)]


# -- the filer


def test_the_filer_records_everything_before_anything_reaches_a_wire(started):
    """§6.1"""
    store = ProjectStore(spec(started).path)
    wo, a = running_order(store)
    store.flag_attention(wo["id"], "assumptions pending review")

    out = ops.file_assumption_objection(store, spec(started).path, wo, a,
                                        reason=REASON, model="opus", question_id=11)

    envelopes = store.envelopes(subject_wo_id=wo["id"])
    assert [e["kind"] for e in envelopes] == ["assumption_objection"]
    assert envelopes[0]["state"] == "queued"
    assert out["envelope_id"] == envelopes[0]["id"]
    assert out["transport"] == "queue" and out["sent_ts"]
    # nothing is on the wire yet: the bus has not run, so there is no message at all
    assert store.list_messages(wo["id"]) == []

    row = store.get_assumption(a["id"])
    assert row["objection_envelope_id"] == envelopes[0]["id"]
    assert row["objection_transport"] == "queue"
    assert row["objection_sent_ts"] == out["sent_ts"]
    assert row["status"] == "pending"          # an objection settles nothing (§2)

    [ev] = events(store, wo["id"], "autoreview_objected")
    assert ev["n"] == a["n"] and ev["transport"] == "queue"
    assert ev["reason"] == REASON and ev["model"] == "opus"
    assert ev["question_id"] == 11


def test_the_message_is_never_stamped_as_the_user_and_clears_no_attention(started):
    """§6.3, both side effects that must not be inherited"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    store.flag_attention(wo["id"], "assumptions pending review")

    ops.file_assumption_objection(store, project.path, wo, a, reason=REASON,
                                  model="opus", question_id=None)
    started.deliver_envelopes(project, store)

    [msg] = [m for m in store.list_messages(wo["id"])
             if m["direction"] == "user_to_agent"]
    assert msg["authored_by"] == ""
    assert store.get_work_order(wo["id"])["needs_attention"] == 1
    assert f"#{a['n']}" in msg["content"]
    assert REASON in msg["content"]


def test_the_rendered_objection_says_whose_reading_it_is():
    text = bus.render(bus.AssumptionObjection(assumption_n=3, reason=REASON,
                                              question_id=12))
    assert "#3" in text and REASON in text
    assert "settles nothing" in text
    assert "not the user" in text


# -- the drain


def test_the_drain_files_once_per_assumption(started):
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)

    started.auto_review(project, store)
    started.auto_review(project, store)

    assert len(store.envelopes(subject_wo_id=wo["id"])) == 1
    assert len(events(store, wo["id"], "autoreview_objected")) == 1


@pytest.mark.parametrize("status", ["needs_review", "completed", "pending"])
def test_the_drain_never_files_against_an_order_that_is_not_running(started, status):
    """§2: the licence for this feature evaporates the moment the order stops"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, _ = running_order(store, status=status)

    started.auto_review(project, store)

    assert store.envelopes(subject_wo_id=wo["id"]) == []


@pytest.mark.parametrize("verdict", ["accept", ""])
def test_only_an_objection_is_objected_to(started, verdict):
    project = spec(started)
    store = ProjectStore(project.path)
    wo, _ = running_order(store, verdict=verdict)

    started.auto_review(project, store)

    assert store.envelopes(subject_wo_id=wo["id"]) == []


def test_a_row_that_predates_the_columns_is_left_alone(started):
    """kn-c712a5d6: a column is untested until a test reads a row written before it"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo = store.create_work_order(title="an order from before", description="")
    aid = store.add_assumption(wo["id"], "the old assumption")
    store.set_status(wo["id"], "running")
    store.close()

    conn = sqlite3.connect(project.path / ".jarvis" / "jarvis.db")
    for col in ADDED_COLUMNS["assumptions"]:
        conn.execute(f"ALTER TABLE assumptions DROP COLUMN {col}")
    conn.commit()
    conn.close()

    store = ProjectStore(project.path)                    # the upgrade
    started.auto_review(project, store)

    assert store.envelopes(subject_wo_id=wo["id"]) == []
    assert events(store, wo["id"], "autoreview_objected") == []
    assert store.get_assumption(aid)["status"] == "pending"


# -- §6.6 withdrawal


@pytest.mark.parametrize("status", ["cancelled", "completed"])
def test_an_order_that_stops_with_an_objection_queued_ends_withdrawn(started, status):
    """Half one of the pair that matters: no turn is ever sent to the worker"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    started.deliver_envelopes(project, store)
    store.set_status(wo["id"], status)

    started.withdraw_stale_objections(project, store)

    row = store.get_assumption(a["id"])
    assert row["objection_withdrawn_ts"] is not None
    env = store.get_envelope(int(row["objection_envelope_id"]))
    msg = [m for m in store.list_messages(wo["id"])
           if m["id"] == env["delivered_msg_id"]][0]
    assert msg["status"] == "withdrawn"
    assert store.queued_messages(wo["id"]) == []     # nothing left to become a turn
    [ev] = events(store, wo["id"], "autoreview_objection_withdrawn")
    assert ev["n"] == a["n"]
    assert ev["reason"] == "the order stopped before it could be delivered"
    # the objection itself is not erased: withdrawal stops a delivery, it does not unsay
    assert REASON in msg["content"]


def test_an_undelivered_envelope_is_withdrawn_too(started):
    """The envelope never reached the router, so IT is the carrier to disarm"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    store.set_status(wo["id"], "cancelled")

    started.withdraw_stale_objections(project, store)

    env = store.envelopes(subject_wo_id=wo["id"])[0]
    assert env["state"] == "withdrawn"
    assert store.get_assumption(a["id"])["objection_withdrawn_ts"] is not None


@pytest.mark.parametrize("status", ["running", "validating", "waiting_pr_merge",
                                    "budget_exhausted", "waiting_input"])
def test_an_order_still_running_keeps_its_objection_however_long_it_waited(started,
                                                                          status):
    """Half two of the pair"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    store.conn.execute("UPDATE assumptions SET objection_sent_ts=0.0 WHERE id=?",
                       (a["id"],))
    store.set_status(wo["id"], status)

    started.withdraw_stale_objections(project, store)

    assert store.get_assumption(a["id"])["objection_withdrawn_ts"] is None
    assert [x["id"] for x in store.outstanding_objections(wo["id"])] == [a["id"]]
    assert events(store, wo["id"], "autoreview_objection_withdrawn") == []


def test_a_project_with_no_objections_costs_the_pass_one_query(started):
    """The candidate filter, and the common case: no order in the fleet ever objected"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store, verdict="accept")
    store.set_status(wo["id"], "needs_review")

    assert store.work_orders_with_objections() == []

    started.withdraw_stale_objections(project, store)

    assert store.get_assumption(a["id"])["objection_withdrawn_ts"] is None
    assert events(store, wo["id"], "autoreview_objection_withdrawn") == []


def test_the_candidate_filter_is_a_superset_of_outstanding(started):
    """COARSER ON PURPOSE: a delivered objection is still a candidate"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    store.mark_objection_delivered(a["id"])

    assert store.work_orders_with_objections() == [wo["id"]]
    assert store.outstanding_objections(wo["id"]) == []


def test_a_delivered_objection_is_never_withdrawn(started):
    """§6.6, driven end to end: NOTHING here stamps delivery by hand"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    store.set_status(wo["id"], "running", session_id="s-objection")  # else: no session
    started.auto_review(project, store)
    started.deliver_envelopes(project, store)
    started.deliver_messages(project, store)      # the worker reads it, for real
    store.set_status(wo["id"], "completed")

    started.withdraw_stale_objections(project, store)

    row = store.get_assumption(a["id"])
    assert row["objection_delivered_ts"] is not None
    assert row["objection_withdrawn_ts"] is None
    env = store.get_envelope(int(row["objection_envelope_id"]))
    [msg] = [m for m in store.list_messages(wo["id"])
             if m["id"] == env["delivered_msg_id"]]
    assert msg["status"] == "delivered"           # never rewritten to `withdrawn`
    assert events(store, wo["id"], "autoreview_objection_withdrawn") == []


def test_one_broken_candidate_never_costs_the_pass_the_rest(started):
    """The `except` exists to keep one bad order cheap — so it must not itself raise"""
    project = spec(started)
    store = ProjectStore(project.path)
    gone, _ = running_order(store)
    started.auto_review(project, store)
    live, a = running_order(store)
    started.auto_review(project, store)
    store.set_status(live["id"], "needs_review")
    # the first candidate's work order no longer exists: `get_work_order` raises KeyError
    store.conn.commit()
    store.conn.execute("PRAGMA foreign_keys=OFF")
    store.conn.execute("DELETE FROM work_orders WHERE id=?", (gone["id"],))
    store.conn.commit()
    store.conn.execute("PRAGMA foreign_keys=ON")

    started.withdraw_stale_objections(project, store)

    assert store.get_assumption(a["id"])["objection_withdrawn_ts"] is not None


def test_withdrawal_never_rewrites_a_message_the_worker_already_read(started):
    """The carrier half of §6.6, forced: a `delivered` row stays `delivered`"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    started.deliver_envelopes(project, store)
    env = store.envelopes(subject_wo_id=wo["id"])[0]
    store.mark_message(int(env["delivered_msg_id"]), "delivered")

    started._withdraw_objection(store, wo["id"], envelope=env)

    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "delivered"


@pytest.mark.parametrize("status,reason", [
    ("cancelled", "the order stopped before it could be delivered"),
    ("completed", "the order stopped before it could be delivered"),
    ("needs_review", "the order delivered its result and the decision is yours"),
])
def test_delivery_refuses_an_objection_to_an_order_that_stopped(started, status, reason):
    """Belt as well as braces: the delivery side must lose the race (§6.6)"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    started.deliver_envelopes(project, store)
    store.set_status(wo["id"], status)

    started.deliver_messages(project, store)

    assert store.queued_messages(wo["id"]) == []
    assert store.get_assumption(a["id"])["objection_withdrawn_ts"] is not None
    [ev] = events(store, wo["id"], "autoreview_objection_withdrawn")
    assert ev["reason"] == reason
    assert store.get_work_order(wo["id"])["status"] == status


@pytest.mark.parametrize("status", ["validating", "waiting_pr_merge",
                                    "budget_exhausted", "waiting_input"])
def test_delivery_holds_an_objection_to_an_order_that_will_run_again(started, status):
    """THE THIRD OUTCOME (spec 2026-10-20 §1): nothing is written and nothing is sent"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    store.set_status(wo["id"], "running", session_id="s-held")
    started.auto_review(project, store)
    started.deliver_envelopes(project, store)
    store.set_status(wo["id"], status)

    started.deliver_messages(project, store)

    assert len(store.queued_messages(wo["id"])) == 1        # still queued
    assert store.get_assumption(a["id"])["objection_withdrawn_ts"] is None
    assert events(store, wo["id"], "autoreview_objection_withdrawn") == []
    assert store.list_turns(wo["id"]) == []                 # and no turn went out


# -- the joint: §6.6's withdrawal and §7's gate

#: Routine text, and the FORCE_ACCEPT marker sits PAST character 200 on purpose
ACCEPTED_TEXT = (
    "named the helper `_render_row`, matching the two beside it, and kept its arguments "
    "in the order the three callers already pass them, so a reader of any one call site "
    "sees the same shape as the other two and nothing has to be re-checked when a fourth "
    "caller arrives — FORCE_ACCEPT")
OBJECTED_TEXT = "FORCE_DENY — put the helper at the bottom of the module"


def tick(daemon, project, store, *, poll=True, reconcile=True, drain=True):
    """One daemon tick, in `Daemon.run`'s own order (daemon.py ~715 and ~782)"""
    if poll:
        daemon.deliver_envelopes(project, store)
        daemon.deliver_messages(project, store)
    if reconcile:
        daemon.auto_review(project, store)
        daemon.withdraw_stale_objections(project, store)
    if drain:
        daemon._neo_drain()


def objection_in_flight(daemon, project, store, *, session="s-objection",
                        title="an order with an objection"):
    """A RUNNING order, mid-turn, whose objection is queued as a message (docs/superpowers/specs/2026-10-20-an-objection-is-withdrawn-before-the-order-stops.md)"""
    wo = ops.create_work_order("proj_a", title, description="d")
    wo_id = wo["id"]
    store.set_status(wo_id, "running", session_id=session)
    turn = store.create_turn(wo_id, "dispatch", "the dispatch prompt")
    ops.assume(wo_id, OBJECTED_TEXT)
    tick(daemon, project, store)                                  # the early reading
    tick(daemon, project, store)                                  # §6.1 files it
    tick(daemon, project, store, reconcile=False, drain=False)    # the bus routes it
    a = next(a for a in store.all_assumptions(wo_id) if a["objection_envelope_id"])
    env = store.get_envelope(int(a["objection_envelope_id"]))
    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "queued"
    return wo_id, turn, env


def objection_row(store, wo_id: str) -> dict:
    return next(a for a in store.all_assumptions(wo_id) if a["objection_envelope_id"])


def test_an_order_that_leaves_running_for_needs_review_keeps_its_objection(started):
    """CHANGE 1 AND CHANGE 2, the pair that matters: `needs_review` is not a stop"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo_id, turn, env = objection_in_flight(started, project, store)

    assert ops.finish(wo_id, "two readings recorded")["status"] == "needs_review"
    store.finish_turn(turn["id"], "done")

    # ...and before the reconcile half: the objection is still the OS's to resolve
    row = objection_row(store, wo_id)
    assert row["objection_withdrawn_ts"] is None
    assert [a["id"] for a in store.outstanding_objections(wo_id)] == [row["id"]]
    assert row["objection_delivered_ts"] is None
    assert events(store, wo_id, "autoreview_objection_withdrawn") == []

    tick(started, project, store, poll=False, drain=False)        # the disarm

    row = objection_row(store, wo_id)
    assert row["objection_withdrawn_ts"] is not None
    assert row["objection_delivered_ts"] is None
    [ev] = events(store, wo_id, "autoreview_objection_withdrawn")
    assert ev["reason"] == "the order delivered its result and the decision is yours"
    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "withdrawn"
    assert store.queued_messages(wo_id) == []
    assert store.get_work_order(wo_id)["status"] == "needs_review"
    assert [t["seq"] for t in store.list_turns(wo_id)] == [1]     # no turn, ever


def test_an_order_that_goes_terminal_with_an_objection_queued_still_gets_the_old_reason(
        started):
    """The negative control for the reason: a CANCELLED order really did stop first"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo_id, turn, env = objection_in_flight(started, project, store)
    store.finish_turn(turn["id"], "done")

    ops.cancel(wo_id)
    tick(started, project, store, drain=False)

    row = objection_row(store, wo_id)
    assert row["objection_withdrawn_ts"] is not None
    [ev] = events(store, wo_id, "autoreview_objection_withdrawn")
    assert ev["reason"] == "the order stopped before it could be delivered"
    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "withdrawn"


def test_an_objection_rides_the_next_turn_after_a_rejected_round(started):
    """THE REGRESSION TEST FOR `wo-a1d7b87c`: `validating` is a state it comes back from"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo_id, turn, env = objection_in_flight(started, project, store)
    store.finish_turn(turn["id"], "done")
    wo = store.get_work_order(wo_id)
    round_row = store.open_validation_round(wo_id=wo_id, fingerprint="fp1")
    store.set_status(wo_id, "validating")

    # a round in flight: the objection is HELD, not withdrawn
    tick(started, project, store, drain=False)
    row = objection_row(store, wo_id)
    assert row["objection_withdrawn_ts"] is None
    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "queued"

    started._reject(store, wo, int(round_row["id"]), 1, 3,
                    "the exporter still double-counts")
    tick(started, project, store, drain=False)       # the feedback turn goes out
    assert store.get_work_order(wo_id)["status"] == "running"
    for t in store.list_turns(wo_id):
        if t["state"] == "running":
            store.finish_turn(t["id"], "done")

    tick(started, project, store, drain=False)       # ...and the objection rides next

    row = objection_row(store, wo_id)
    assert row["objection_delivered_ts"] is not None
    assert row["objection_withdrawn_ts"] is None
    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "delivered"


def test_a_held_objection_is_never_a_stuck_message(started):
    """§1.1: an accounted hold, or INV-MESSAGE-STUCK reports a defect that is not one"""
    from jarvis import invariants

    project = spec(started)
    store = ProjectStore(project.path)
    wo_id, turn, env = objection_in_flight(started, project, store)
    store.finish_turn(turn["id"], "done")
    # Not a finish, so kn-13e71e04 does not apply: the worker going quiet mid-task is exactly what this...
    store.set_status(wo_id, "waiting_input")
    store.conn.execute("UPDATE wo_messages SET ts=ts-? WHERE wo_id=?", (90 * 60, wo_id))
    store.conn.commit()

    tick(started, project, store, drain=False)

    wo = store.get_work_order(wo_id)
    assert objection_row(store, wo_id)["objection_withdrawn_ts"] is None
    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "queued"
    assert invariants.stuck_message(store, wo) is None
    assert invariants.MESSAGE_STUCK_BLOCKER not in invariants.true_blockers(store, wo)
    assert [v.invariant for v in invariants.check_project(store)
            if v.invariant == "INV-MESSAGE-STUCK"] == []


def test_a_user_message_is_still_delivered_past_a_held_objection(started):
    """One rule: the objection reaches a running conversation or it reaches nobody"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo_id, turn, env = objection_in_flight(started, project, store)
    store.finish_turn(turn["id"], "done")
    store.set_status(wo_id, "waiting_input")

    ops.send_message(wo_id, "please also check the retry path")
    assert daemon_delivers(started, store, wo_id) is True

    tick(started, project, store, drain=False)

    sent = [t for t in store.list_turns(wo_id) if t["seq"] > 1]
    assert len(sent) == 1
    assert REASON not in str(sent[0]["prompt"] or "")
    assert "please also check the retry path" in str(sent[0]["prompt"] or "")
    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "queued"
    assert objection_row(store, wo_id)["objection_delivered_ts"] is None


def daemon_delivers(daemon, store, wo_id: str) -> bool:
    from jarvis import daemon as daemon_mod

    return daemon_mod.delivers_message(store, store.get_work_order(wo_id))


def test_a_budget_exhausted_order_keeps_its_objection(started):
    """`budget_exhausted` is OPEN, not terminal: the user raises it and the work goes on"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo_id, turn, env = objection_in_flight(started, project, store)
    store.finish_turn(turn["id"], "done")
    store.set_status(wo_id, "budget_exhausted")

    tick(started, project, store, drain=False)
    assert objection_row(store, wo_id)["objection_withdrawn_ts"] is None
    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "queued"

    ops.set_work_order_budget(wo_id, 50.0)
    for t in store.list_turns(wo_id):
        if t["state"] == "running":
            store.finish_turn(t["id"], "done")
    tick(started, project, store, drain=False)

    assert objection_row(store, wo_id)["objection_delivered_ts"] is not None
    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "delivered"


def test_the_disarm_is_what_clears_the_confirm_gate_for_the_siblings(started):
    """§2's second argument: `objections_outstanding` is per ORDER"""
    project = spec(started)
    store = ProjectStore(project.path)
    wo = ops.create_work_order("proj_a", "an order with two readings", description="d")
    wo_id = wo["id"]
    store.set_status(wo_id, "running", session_id="s-siblings")
    turn = store.create_turn(wo_id, "dispatch", "the dispatch prompt")
    ops.assume(wo_id, OBJECTED_TEXT)
    ops.assume(wo_id, ACCEPTED_TEXT)
    tick(started, project, store)
    tick(started, project, store)
    tick(started, project, store, reconcile=False, drain=False)

    assert ops.finish(wo_id, "two readings recorded")["status"] == "needs_review"
    store.finish_turn(turn["id"], "done")

    def accepted():
        return next(a for a in store.all_assumptions(wo_id)
                    if "FORCE_ACCEPT" in a["content"])

    # the disarm tick: the gate still held while the objection was outstanding
    tick(started, project, store, poll=False, drain=False)
    assert autoreview.HELD_OBJECTION_IN_FLIGHT in [
        h["code"] for h in events(store, wo_id, "autoreview_held")]
    assert accepted()["confirm_question_id"] is None
    assert objection_row(store, wo_id)["objection_withdrawn_ts"] is not None

    tick(started, project, store, poll=False, drain=False)
    assert accepted()["confirm_question_id"] is not None


def test_objection_counts_split_withdrawals_by_reason(started):
    """CHANGE 5: a derived read over the three ts columns, no table and no write path"""
    project = spec(started)
    store = ProjectStore(project.path)

    handed, turn_a, _ = objection_in_flight(started, project, store, session="s-a",
                                            title="an order that delivered")
    assert ops.finish(handed, "delivered")["status"] == "needs_review"
    store.finish_turn(turn_a["id"], "done")
    tick(started, project, store, poll=False, drain=False)

    stopped, turn_b, _ = objection_in_flight(started, project, store, session="s-b",
                                             title="an order that stopped")
    store.finish_turn(turn_b["id"], "done")
    ops.cancel(stopped)
    tick(started, project, store, drain=False)

    counts = store.objection_counts()
    assert counts["sent"] == 2
    assert counts["delivered"] == 0
    assert counts["withdrawn"] == 2
    assert counts["by_reason"] == {
        "the order stopped before it could be delivered": 1,
        "the order delivered its result and the decision is yours": 1,
        "other": 0}

    report = ops.neo_stats_report(project="proj_a")
    assert report["objections"]["by_reason"] == counts["by_reason"]
    assert (report["objections"]["stopped"], report["objections"]["handed_back"],
            report["objections"]["other"]) == (1, 1, 0)
    assert report["objections"]["sent"] == 2


def test_the_withdrawal_is_what_lets_the_confirmation_pass_run(started):
    """§6.6 and §7 were written in separate sessions and only work if they agree"""
    from jarvis import invariants
    from jarvis.neo_store import NeoStore

    def asked(kind="assumption"):
        neo = NeoStore()
        try:
            return [q for q in neo.list_questions() if q["kind"] == kind]
        finally:
            neo.close()

    project = spec(started)
    wo_row = ops.create_work_order("proj_a", "an order with two readings",
                                   description="d")
    store = ProjectStore(project.path)
    store.set_status(wo_row["id"], "running", session_id="s-joint")
    # The worker is MID-TURN: that is when it records an assumption
    turn = store.create_turn(wo_row["id"], "dispatch", "the dispatch prompt")
    wo_id = wo_row["id"]
    ops.assume(wo_id, OBJECTED_TEXT)
    ops.assume(wo_id, ACCEPTED_TEXT)

    def rows():
        by = {("object" if "FORCE_DENY" in a["content"] else "accept"): a
              for a in store.all_assumptions(wo_id)}
        return by["object"], by["accept"]

    # ACCEPTED_TEXT only works while `sibling_line`'s 200-char cut hides its marker: if the cut moves
    assert "FORCE_ACCEPT" not in autoreview.sibling_line(rows()[1])

    # tick 1: the early pass asks, the drain rules, and NOTHING settles (§5)
    tick(started, project, store)
    objected, accepted = rows()
    assert objected["provisional_verdict"] == "object" and objected["provisional_reason"]
    assert accepted["provisional_verdict"] == "accept"
    for row in (objected, accepted):
        assert row["status"] == "pending" and row["decided_by"] == ""
    assert len(store.pending_assumptions(wo_id)) == 2
    assert len(asked()) == 2

    # tick 2: §6.1 files the objection, off the recorded verdict and nothing else
    tick(started, project, store)
    objected, accepted = rows()
    [ev] = events(store, wo_id, "autoreview_objected")
    assert ev["n"] == objected["n"]
    [env] = store.envelopes(subject_wo_id=wo_id)
    assert objected["objection_envelope_id"] == env["id"]
    assert objected["objection_transport"] == "queue"
    assert accepted["objection_envelope_id"] is None
    assert [a["id"] for a in store.outstanding_objections(wo_id)] == [objected["id"]]
    assert objected["status"] == "pending" and accepted["status"] == "pending"

    # tick 3, delivery half only: the envelope becomes a message and the hold keeps it
    tick(started, project, store, reconcile=False, drain=False)
    env = store.get_envelope(int(env["id"]))       # `delivered_msg_id` is set by routing
    [msg] = [m for m in store.list_messages(wo_id) if m["id"] == env["delivered_msg_id"]]
    assert msg["status"] == "queued"
    assert [t["seq"] for t in store.list_turns(wo_id)] == [1]

    # The worker finishes MID-TICK — its own process writing
    assert ops.finish(wo_id, "two readings recorded")["status"] == "needs_review"
    store.finish_turn(turn["id"], "done")           # ...and its process exits

    # tick 3, reconcile half: the gate holds, THEN §6.6 withdraws
    tick(started, project, store, poll=False, drain=False)
    objected, accepted = rows()
    assert len(asked()) == 2                       # no confirmation question yet
    assert objected["confirm_question_id"] is None
    assert accepted["confirm_question_id"] is None
    assert autoreview.HELD_OBJECTION_IN_FLIGHT in [
        h["code"] for h in events(store, wo_id, "autoreview_held")]
    assert objected["objection_withdrawn_ts"] is not None
    assert objected["objection_delivered_ts"] is None
    [withdrawn] = events(store, wo_id, "autoreview_objection_withdrawn")
    assert withdrawn["n"] == objected["n"]
    # THE DISARM, not the old withdrawal: this order reached `needs_review`
    assert withdrawn["reason"] == ("the order delivered its result and the decision "
                                   "is yours")
    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "withdrawn"
    assert store.queued_messages(wo_id) == []
    assert [t["seq"] for t in store.list_turns(wo_id)] == [1]   # no second turn, ever
    assert [t for t in store.list_turns(wo_id) if t["msg_id"]] == []

    # The hold was belt and the withdrawal is braces: the turn has ENDED
    tick(started, project, store, reconcile=False, drain=False)
    assert [t["seq"] for t in store.list_turns(wo_id)] == [1]
    assert store.deliverable_messages(wo_id) == []

    # tick 4: the gate is open, so the accepted row alone is confirmed and settled
    tick(started, project, store, drain=False)
    objected, accepted = rows()
    confirming = [q for q in asked() if q["id"] == accepted["confirm_question_id"]]
    assert len(asked()) == 3 and len(confirming) == 1
    assert objected["confirm_question_id"] is None

    started._neo_drain()
    objected, accepted = rows()
    assert accepted["status"] == "accepted" and accepted["decided_by"] == "neo"
    [confirmed] = events(store, wo_id, "autoreview_confirmed")
    assert confirmed["neo_question_id"] == confirming[0]["id"]
    assert objected["status"] == "pending" and objected["decided_by"] == ""
    assert [t["seq"] for t in store.list_turns(wo_id)] == [1]   # still no second turn

    # kn-640e7f6c: a WITHDRAWN objection is not an undeliverable one — nothing failed
    blockers = invariants.true_blockers(store, store.get_work_order(wo_id))
    assert invariants.OBJECTION_UNDELIVERABLE_BLOCKER not in blockers
    # THE POSITIVE CONTROL, or an empty list would pass the line above
    assert blockers == ["1 assumption pending your review"]
