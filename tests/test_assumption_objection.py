"""The objection: recorded first, sent second, and withdrawn when the order stops.

§6 of docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md.

Three post-conditions outrank everything else here:

1. **The record exists before the wire does.** The envelope, the assumption row and the
   timeline event are all written by the filer; the message a worker reads is created
   later, by the bus, on a tick.
2. **An objection is never the user talking.** `wo_messages.authored_by` stays `''` and
   the work order's attention is not cleared.
3. **A stopped order's undelivered objection is withdrawn, never delivered late** —
   the pair: stopped ends `withdrawn` with no turn sent, running stays outstanding.
4. **§6.6 and §7 agree on one order**: the withdrawal is what unblocks the confirmation.
"""

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
    """A work order in `status` with one assumption carrying `verdict`."""
    wo = store.create_work_order(title="an order mid-flight", description="d")
    aid = store.add_assumption(wo["id"], text)
    if verdict:
        store.record_provisional(aid, verdict=verdict, reason=REASON, model="opus",
                                 stakes="routine")
    store.set_status(wo["id"], status)
    # `all_assumptions`, not `get_assumption`: `n` is computed there and it is the number
    # every event, every rendered message and every surface names the assumption by.
    row = next(a for a in store.all_assumptions(wo["id"]) if a["id"] == aid)
    return store.get_work_order(wo["id"]), row


def events(store, wo_id: str, kind: str) -> list[dict]:
    return [json.loads(e["payload"]) for e in store.events_of_kind(wo_id, kind)]


# -- the filer -------------------------------------------------------------------------


def test_the_filer_records_everything_before_anything_reaches_a_wire(started):
    """§6.1. The envelope and the event are in the database; the message is not yet."""
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
    """§6.3, both side effects that must not be inherited."""
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


# -- the drain -------------------------------------------------------------------------


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
    """§2: the licence for this feature evaporates the moment the order stops."""
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
    """kn-c712a5d6: a column is untested until a test reads a row written before it."""
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


# -- §6.6 withdrawal -------------------------------------------------------------------


def test_an_order_that_stops_with_an_objection_queued_ends_withdrawn(started):
    """Half one of the pair that matters: no turn is ever sent to the worker."""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    started.deliver_envelopes(project, store)
    store.set_status(wo["id"], "needs_review")

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
    """The envelope never reached the router, so IT is the carrier to disarm."""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    store.set_status(wo["id"], "cancelled")

    started.withdraw_stale_objections(project, store)

    env = store.envelopes(subject_wo_id=wo["id"])[0]
    assert env["state"] == "withdrawn"
    assert store.get_assumption(a["id"])["objection_withdrawn_ts"] is not None


def test_an_order_still_running_keeps_its_objection_however_long_it_waited(started):
    """Half two of the pair. Age is not the trigger; leaving `running` is."""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    store.conn.execute("UPDATE assumptions SET objection_sent_ts=0.0 WHERE id=?",
                       (a["id"],))

    started.withdraw_stale_objections(project, store)

    assert store.get_assumption(a["id"])["objection_withdrawn_ts"] is None
    assert [x["id"] for x in store.outstanding_objections(wo["id"])] == [a["id"]]
    assert events(store, wo["id"], "autoreview_objection_withdrawn") == []


def test_a_project_with_no_objections_costs_the_pass_one_query(started):
    """The candidate filter, and the common case: no order in the fleet ever objected.

    Asserting the candidate list is empty is the whole test — an empty list is an empty
    loop, so nothing reads an assumption and nothing is touched.
    """
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store, verdict="accept")
    store.set_status(wo["id"], "needs_review")

    assert store.work_orders_with_objections() == []

    started.withdraw_stale_objections(project, store)

    assert store.get_assumption(a["id"])["objection_withdrawn_ts"] is None
    assert events(store, wo["id"], "autoreview_objection_withdrawn") == []


def test_the_candidate_filter_is_a_superset_of_outstanding(started):
    """COARSER ON PURPOSE: a delivered objection is still a candidate, and
    `outstanding_objections` is what refuses it. Two spellings of "outstanding" is the
    thing this must not become."""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    store.mark_objection_delivered(a["id"])

    assert store.work_orders_with_objections() == [wo["id"]]
    assert store.outstanding_objections(wo["id"]) == []


def test_a_delivered_objection_is_never_withdrawn(started):
    """§6.6, driven end to end: NOTHING here stamps delivery by hand.

    A hand call to `mark_objection_delivered` tests the withdrawal pass against a state
    the real path never produced — the gap the panel called out. So the turn goes out
    through `deliver_messages` while the order runs, and only then does it stop.
    """
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
    """The `except` exists to keep one bad order cheap — so it must not itself raise."""
    project = spec(started)
    store = ProjectStore(project.path)
    gone, _ = running_order(store)
    started.auto_review(project, store)
    live, a = running_order(store)
    started.auto_review(project, store)
    store.set_status(live["id"], "needs_review")
    # the first candidate's work order no longer exists: `get_work_order` raises KeyError.
    # The keys go off for the one statement — the orphan assumption row IS the state
    # under test, and the cascade would take it with the order.
    store.conn.commit()
    store.conn.execute("PRAGMA foreign_keys=OFF")
    store.conn.execute("DELETE FROM work_orders WHERE id=?", (gone["id"],))
    store.conn.commit()
    store.conn.execute("PRAGMA foreign_keys=ON")

    started.withdraw_stale_objections(project, store)

    assert store.get_assumption(a["id"])["objection_withdrawn_ts"] is not None


def test_withdrawal_never_rewrites_a_message_the_worker_already_read(started):
    """The carrier half of §6.6, forced: a `delivered` row stays `delivered`.

    Same rule as the envelope, which only ever moves from `queued`.
    """
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    started.deliver_envelopes(project, store)
    env = store.envelopes(subject_wo_id=wo["id"])[0]
    store.mark_message(int(env["delivered_msg_id"]), "delivered")

    started._withdraw_objection(store, wo["id"], envelope=env)

    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "delivered"


def test_delivery_refuses_an_objection_to_an_order_that_stopped(started):
    """Belt as well as braces: the delivery side must lose the race (§6.6)."""
    project = spec(started)
    store = ProjectStore(project.path)
    wo, a = running_order(store)
    started.auto_review(project, store)
    started.deliver_envelopes(project, store)
    store.set_status(wo["id"], "needs_review")

    started.deliver_messages(project, store)

    assert store.queued_messages(wo["id"]) == []
    assert store.get_assumption(a["id"])["objection_withdrawn_ts"] is not None
    assert events(store, wo["id"], "autoreview_objection_withdrawn")
    assert store.get_work_order(wo["id"])["status"] == "needs_review"


# -- the joint: §6.6's withdrawal and §7's gate ----------------------------------------

#: Routine text, and the FORCE_ACCEPT marker sits PAST character 200 on purpose:
#: `autoreview.sibling_line` truncates there, so the marker never leaks into the prompt
#: for the assumption beside this one. Otherwise the fake reads `FORCE_ACCEPT` first
#: (src/jarvis/testing.py) and both rows come back accepted.
ACCEPTED_TEXT = (
    "named the helper `_render_row`, matching the two beside it, and kept its arguments "
    "in the order the three callers already pass them, so a reader of any one call site "
    "sees the same shape as the other two and nothing has to be re-checked when a fourth "
    "caller arrives — FORCE_ACCEPT")
OBJECTED_TEXT = "FORCE_DENY — put the helper at the bottom of the module"


def tick(daemon, project, store, *, poll=True, reconcile=True, drain=True):
    """One daemon tick, in `Daemon.run`'s own order (daemon.py ~715 and ~782).

    The halves are separable because the race §7's gate exists for happens BETWEEN them.
    """
    if poll:
        daemon.deliver_envelopes(project, store)
        daemon.deliver_messages(project, store)
    if reconcile:
        daemon.auto_review(project, store)
        daemon.withdraw_stale_objections(project, store)
    if drain:
        daemon._neo_drain()


def test_the_withdrawal_is_what_lets_the_confirmation_pass_run(started):
    """§6.6 and §7 were written in separate sessions and only work if they agree.

    Every other test in the feature proves ONE section. This is the joint: an objection in
    flight HOLDS the confirmation (§7), and §6.6's withdrawal is the only thing that ever
    releases it on an order that stopped first. Real passes only — the verdicts come from
    the fake through the markers in the assumption text.
    """
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
    # NO session_id: `worker_session.delivery_hold` then holds the objection for ever, so
    # no turn can start and the objection stays outstanding until §6.6 takes it back.
    store.set_status(wo_row["id"], "running")
    wo_id = wo_row["id"]
    ops.assume(wo_id, OBJECTED_TEXT)
    ops.assume(wo_id, ACCEPTED_TEXT)

    def rows():
        by = {("object" if "FORCE_DENY" in a["content"] else "accept"): a
              for a in store.all_assumptions(wo_id)}
        return by["object"], by["accept"]

    # tick 1: the early pass asks, the drain rules, and NOTHING settles (§5).
    tick(started, project, store)
    objected, accepted = rows()
    assert objected["provisional_verdict"] == "object" and objected["provisional_reason"]
    assert accepted["provisional_verdict"] == "accept"
    for row in (objected, accepted):
        assert row["status"] == "pending" and row["decided_by"] == ""
    assert len(store.pending_assumptions(wo_id)) == 2
    assert len(asked()) == 2

    # tick 2: §6.1 files the objection, off the recorded verdict and nothing else.
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

    # tick 3, delivery half only: the envelope becomes a message and the hold keeps it.
    tick(started, project, store, reconcile=False, drain=False)
    env = store.get_envelope(int(env["id"]))       # `delivered_msg_id` is set by routing
    [msg] = [m for m in store.list_messages(wo_id) if m["id"] == env["delivered_msg_id"]]
    assert msg["status"] == "queued"
    assert store.latest_turn(wo_id) is None

    # The worker finishes MID-TICK. `ops.finish` is its own process writing, so the
    # status really can flip between the delivery half of a tick and its reconcile half —
    # and that race is what §7's gate exists for.
    store.set_status(wo_id, "needs_review")

    # tick 3, reconcile half: the gate holds, THEN §6.6 withdraws.
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
    assert withdrawn["reason"] == "the order stopped before it could be delivered"
    assert store.get_message(int(env["delivered_msg_id"]))["status"] == "withdrawn"
    assert store.queued_messages(wo_id) == []
    assert store.latest_turn(wo_id) is None        # no turn, first assertion

    # tick 4: the gate is open, so the accepted row alone is confirmed and settled.
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
    assert store.latest_turn(wo_id) is None        # no turn, second assertion

    # kn-640e7f6c: a WITHDRAWN objection is not an undeliverable one — nothing failed.
    blockers = invariants.true_blockers(store, store.get_work_order(wo_id))
    assert invariants.OBJECTION_UNDELIVERABLE_BLOCKER not in blockers
    # THE POSITIVE CONTROL, or an empty list would pass the line above. A provisional
    # OBJECT is nobody's confirmation in flight (`invariants._os_is_confirming`) — §7
    # asks nothing on one, so the objected row is the user's and says so.
    assert blockers == ["1 assumption pending your review"]
