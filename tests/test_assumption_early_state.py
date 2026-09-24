"""The state an assumption judged mid-run is recorded in, and how it renders.

§4 of docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md.
Columns, store verbs, event kinds and the two shared renderers — delivered with no caller,
so these tests are the only thing holding the names the five sections behind it call.

TWO POST-CONDITIONS THIS FILE EXISTS FOR, and they outrank every other assertion here:

1. `status` NEVER LEAVES `pending` on any of these paths (§4.1). Eight-plus call sites
   read `pending` as "the user owes a decision", so a provisional verdict that moved it
   would silently open the user's gate — the feature's stated failure mode.
2. A ROW THAT PREDATES THE COLUMNS renders exactly as it did before (kn-c712a5d6). That
   is most of the fleet: every historical assumption, and every project with early review
   off.
"""

from __future__ import annotations

import sqlite3

from jarvis import ops, timeline
from jarvis.project_store import ADDED_COLUMNS, ProjectStore

NEW_COLUMNS = tuple(ADDED_COLUMNS["assumptions"])


def _aged_store(tmp_path):
    """Today's schema with the new columns dropped — a database from the live release.

    Built by dropping rather than from a frozen asset, for `test_schema_upgrade`'s
    reason: an asset that predates the whole table would exercise `CREATE TABLE` and pass
    with `ADDED_COLUMNS` forgotten, which is the failure this shape catches.
    """
    proj = tmp_path / "aged"
    (proj / ".jarvis").mkdir(parents=True)
    store = ProjectStore(proj)
    wo = store.create_work_order(title="an order from before", description="")
    aid = store.add_assumption(wo["id"], "the old assumption")
    store.close()

    conn = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    for col in NEW_COLUMNS:
        conn.execute(f"ALTER TABLE assumptions DROP COLUMN {col}")
    conn.execute("ALTER TABLE envelopes DROP COLUMN delivered_msg_id")
    conn.commit()
    conn.close()
    return proj, wo["id"], aid


def test_the_columns_reach_a_database_that_predates_them(tmp_path):
    proj, wo_id, aid = _aged_store(tmp_path)

    store = ProjectStore(proj)                       # the upgrade
    try:
        have = {r["name"] for r in
                store.conn.execute("PRAGMA table_info(assumptions)").fetchall()}
        assert set(NEW_COLUMNS) <= have
        # ...and the row written before them reads as "nothing ever judged this early",
        # which is the only honest answer and the one every surface keys off.
        old = store.get_assumption(aid)
        assert old["provisional_verdict"] == ""
        assert old["objection_transport"] == ""
        assert old["provisional_ts"] is None
        assert old["objection_envelope_id"] is None
        assert old["confirm_question_id"] is None
        # and the upgraded database still takes a write to every one of them
        store.record_provisional(aid, verdict="accept", reason="fine", model="m",
                                 stakes="low", config_version="cv-1")
        store.record_objection(aid, envelope_id=7, transport="queue", sent_ts=100.0)
        store.link_assumption_confirmation(aid, 42)
        back = store.get_assumption(aid)
        assert back["provisional_verdict"] == "accept"
        assert back["objection_envelope_id"] == 7
        assert back["confirm_question_id"] == 42
        assert back["status"] == "pending"
    finally:
        store.close()


def test_a_provisional_verdict_does_not_settle_the_assumption(project):
    """§4.1, the one edit this feature forbids. The negative control is the whole test."""
    store = ProjectStore(project)
    wo = store.create_work_order(title="running order", description="")
    aid = store.add_assumption(wo["id"], "the API is idempotent")

    store.record_provisional(aid, verdict="accept", reason="standard", model="opus",
                             stakes="low", config_version="cv-9")

    assert store.get_assumption(aid)["status"] == "pending"
    assert [a["id"] for a in store.pending_assumptions(wo["id"])] == [aid]
    row = store.get_assumption(aid)
    assert row["provisional_reason"] == "standard"
    assert row["provisional_model"] == "opus"
    assert row["provisional_stakes"] == "low"
    assert row["provisional_config_version"] == "cv-9"
    assert row["provisional_ts"] is not None
    # nothing was stamped on the SETTLEMENT columns: an early reading is not a decider
    assert row["decided_by"] == "" and row["decided_model"] == ""


def test_unclassified_is_what_no_stakes_means(project):
    store = ProjectStore(project)
    wo = store.create_work_order(title="o", description="")
    aid = store.add_assumption(wo["id"], "a")
    store.record_provisional(aid, verdict="object", reason="r", model="m")
    assert store.get_assumption(aid)["provisional_stakes"] == "unclassified"


def test_assumption_for_question_resolves_both_question_columns(project):
    """The early question AND the confirmation question, §4.3.

    A single-column lookup would resolve the confirmation question to nothing, and §7's
    verdict would be dropped on the floor with the assumption left pending for ever.
    """
    store = ProjectStore(project)
    wo = store.create_work_order(title="o", description="")
    aid = store.add_assumption(wo["id"], "a")
    store.link_assumption_question(aid, 11)
    store.link_assumption_confirmation(aid, 12)

    assert store.assumption_for_question(11)["id"] == aid
    assert store.assumption_for_question(12)["id"] == aid
    assert store.assumption_for_question(13) is None
    # the early link is not overwritten by the second pass: both readings survive
    assert store.get_assumption(aid)["neo_question_id"] == 11


def test_outstanding_objections_is_neither_delivered_nor_withdrawn(project):
    """One query, two readers — §6.6 withdraws from this list and §7 waits on it."""
    store = ProjectStore(project)
    wo = store.create_work_order(title="o", description="")
    plain = store.add_assumption(wo["id"], "no objection at all")
    flight = store.add_assumption(wo["id"], "objection in flight")
    landed = store.add_assumption(wo["id"], "objection delivered")
    gone = store.add_assumption(wo["id"], "objection withdrawn")
    for aid in (flight, landed, gone):
        store.record_objection(aid, envelope_id=aid, transport="queue", sent_ts=1.0)
    store.mark_objection_delivered(landed, 2.0)
    store.withdraw_objection(gone, 3.0)

    assert [a["id"] for a in store.outstanding_objections(wo["id"])] == [flight]
    assert plain not in [a["id"] for a in store.outstanding_objections(wo["id"])]
    # every row carries its number, which is what the withdrawal event names it by
    assert store.outstanding_objections(wo["id"])[0]["n"] == 2
    assert store.get_assumption(landed)["objection_delivered_ts"] == 2.0
    assert store.get_assumption(gone)["objection_withdrawn_ts"] == 3.0
    # and none of it settled anything
    assert {store.get_assumption(a)["status"] for a in (flight, landed, gone)} == {
        "pending"}


def test_an_envelope_says_which_message_it_became(project):
    """`envelopes.delivered_msg_id`, §4.2 — the link §8 and §9 both have to follow."""
    store = ProjectStore(project)
    wo = store.create_work_order(title="o", description="")
    # `review_feedback` and not the objection kind §6.2 adds: the link is the BUS's, not
    # this feature's, and every envelope must carry it.
    env_id = store.post_envelope(from_role="reviewer", to_role="implementor",
                                 kind="review_feedback", subject_wo_id=wo["id"])
    assert store.envelopes(subject_wo_id=wo["id"])[0]["delivered_msg_id"] is None

    msg_id = store.deliver_envelope(env_id, wo["id"], "Neo objects: ...")
    env = store.envelopes(subject_wo_id=wo["id"])[0]
    assert env["delivered_msg_id"] == msg_id
    assert env["state"] == "delivered"


# -- the renderers ------------------------------------------------------------


def test_a_historical_assumption_renders_exactly_as_it_did(project):
    """The test that matters (§8): empty columns must read as "nothing happened"."""
    pending = {"n": 1, "status": "pending", "content": "the API is idempotent",
               "provisional_verdict": "", "objection_transport": ""}
    assert ops.assumption_line(pending) == (
        "#1 pending your review: the API is idempotent")

    settled = {"n": 2, "status": "accepted", "content": "retries are safe",
               "decided_by": "", "decided_reason": "agreed"}
    assert ops.assumption_line(settled) == (
        "#2 accepted by you — agreed: retries are safe")
    assert ops.assumption_decider(settled) == "you"


def test_a_provisional_reading_is_never_credited_to_the_user():
    """One renderer, and the phrase it builds names the machine (§4.4)."""
    row = {"n": 1, "status": "pending", "content": "c",
           "provisional_verdict": "accept", "provisional_model": "opus-5"}
    phrase = ops.assumption_decider(row)
    assert "you" != phrase and "opus-5" in phrase and "provisional" in phrase
    line = ops.assumption_line(row)
    assert "pending your review" in line
    assert "provisionally accepted by the OS" in line and "opus-5" in line


def test_the_objection_line_tells_four_states_apart():
    """In flight, delivered, withdrawn, undeliverable — rendering two the same is §8's
    named defect."""
    base = {"objection_envelope_id": 4, "objection_transport": "peer",
            "objection_sent_ts": 1_700_000_000.0}
    flight = ops.objection_line(base)
    delivered = ops.objection_line({**base, "objection_delivered_ts": 1_700_000_060.0})
    withdrawn = ops.objection_line({**base, "objection_withdrawn_ts": 1_700_000_060.0})
    undeliverable = ops.objection_line({**base, "objection_undeliverable": True})

    assert len({flight, delivered, withdrawn, undeliverable}) == 4
    assert "in flight" in flight
    assert "delivered at" in delivered
    assert "withdrawn at" in withdrawn and "stopped" in withdrawn
    assert "UNDELIVERABLE" in undeliverable
    assert all("peer" in line
               for line in (flight, delivered, withdrawn, undeliverable))
    # no objection at all is silence, not "in flight"
    assert ops.objection_line({"n": 1, "status": "pending"}) == ""


def test_the_early_reading_survives_on_a_settled_row():
    """§7 hands the user both readings when they disagree, so the line carries both."""
    row = {"n": 1, "status": "accepted", "content": "c", "decided_by": "neo",
           "decided_model": "opus-5", "decided_reason": "confirmed against the diff",
           "provisional_verdict": "accept", "provisional_model": "opus-5",
           "provisional_reason": "looks standard"}
    line = ops.assumption_line(row)
    assert "accepted by the OS (neo, opus-5)" in line
    assert "looks standard" in line


# -- the event kinds ----------------------------------------------------------


EARLY_KINDS = ("autoreview_provisional", "autoreview_objected",
               "autoreview_objection_withdrawn", "autoreview_confirmed",
               "autoreview_unconfirmed")


def test_the_five_event_kinds_are_enumerated_once():
    assert set(EARLY_KINDS) <= set(ops.AUTOREVIEW_EVENTS)


def test_every_autoreview_kind_has_a_rank():
    """`assumptions_with_rulings` indexes `_RULING_RANK` directly — a missing entry is a
    KeyError on the work order page, not a cosmetic gap."""
    assert set(ops.AUTOREVIEW_EVENTS) == set(ops._RULING_RANK)
    # the delivery pass outranks every early reading: only it settles anything
    assert ops._RULING_RANK["autoreview_confirmed"] > ops._RULING_RANK[
        "autoreview_provisional"]


def test_every_autoreview_kind_has_its_own_summary_line():
    """A kind with no branch falls through to "held —", which claims the OS refused to
    judge an assumption it did judge."""
    lines = {kind: ops._autoreview_line({"kind": kind, "n": 3, "verdict": "accept",
                                         "model": "opus-5", "reason": "because"})
             for kind in ops.AUTOREVIEW_EVENTS}
    assert len(set(lines.values())) == len(ops.AUTOREVIEW_EVENTS)
    for kind, line in lines.items():
        if kind != "autoreview_held":
            assert not line.startswith("held —"), kind
    assert "settles nothing" in lines["autoreview_provisional"]


def test_every_autoreview_kind_has_a_timeline_label():
    """kn-3f133363: a kind with no branch renders as generic prose that says nothing."""
    for kind in ops.AUTOREVIEW_EVENTS:
        label, _detail = timeline._describe(
            kind, {"n": 3, "verdict": "accept", "model": "opus-5", "reason": "because",
                   "transport": "peer"})
        assert label != kind, kind
    provisional, _ = timeline._describe("autoreview_provisional",
                                        {"n": 1, "verdict": "accept", "model": "m"})
    assert "nothing settled" in provisional
    withdrawn, _ = timeline._describe("autoreview_objection_withdrawn", {"n": 1})
    assert "never told" in withdrawn
    # none of them is plumbing: every one is a thing that happened to the user's work
    assert all(timeline.event_level(k) == "signal" for k in ops.AUTOREVIEW_EVENTS)
