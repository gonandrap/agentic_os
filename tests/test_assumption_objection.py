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
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from jarvis import bus, ops
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
