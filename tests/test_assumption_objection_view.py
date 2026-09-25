"""The assumptions view: one renderer, both surfaces, four objection end states.

§8 of docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md.

Three post-conditions outrank everything else here:

1. **The historical row renders exactly as it does today.** Every assumption written
   before the early pass shipped has all the new columns empty, and an empty column must
   never read as an objection that was never delivered (kn-c712a5d6: a column is untested
   until a test reads a row written before it).
2. **The four end states are four different things** — in flight, delivered, withdrawn
   because the order stopped first, undeliverable — and rendering any two of them the
   same is the defect.
3. **One renderer serves both surfaces.** `ops.assumption_line`'s strings are the page's
   strings; a second formatter in `cli.py` or in the template is how a machine verdict
   gets credited to the user on one surface.
"""

from __future__ import annotations

import html
import sqlite3

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import cli, ops  # noqa: E402
from jarvis.project_store import (ADDED_COLUMNS, ASSUMPTION_DECIDER_OS,  # noqa: E402
                                  ProjectStore)
from jarvis.ui.app import create_app  # noqa: E402

REASON = "the helper is not idempotent on retry, so the second call double-counts"
NOW = 1_700_000_000.0


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


def page_of(client, wo_id: str) -> str:
    """The work order page, unescaped and whitespace-collapsed (test_ui.py:585)."""
    return html.unescape(" ".join(client.get(f"/wo/proj_a/{wo_id}").text.split()))


def objecting_order(store, *, delivered: float | None = None,
                    withdrawn: float | None = None, transport: str = "queue",
                    envelope_state: str = "queued", msg_status: str | None = None,
                    verdict: str = "object"):
    """An order with one assumption whose objection reached `envelope_state`.

    Returns `(wo, assumption row with every derivation attached)` — the row both
    surfaces read, which is `ops.assumptions_with_rulings`' and not the store's.
    """
    wo = store.create_work_order(title="an order mid-flight", description="d")
    aid = store.add_assumption(wo["id"], "the API is idempotent")
    store.record_provisional(aid, verdict=verdict, reason=REASON, model="opus",
                             stakes="routine")
    store.set_status(wo["id"], "running")
    env_id = store.post_envelope(from_role="reviewer", to_role="implementor",
                                 kind="assumption_objection",
                                 payload={"reason": REASON},
                                 subject_wo_id=wo["id"])
    store.record_objection(aid, envelope_id=env_id, transport=transport, sent_ts=NOW)
    if msg_status is not None:
        msg_id = store.queue_message(wo["id"], f"the OS objects: {REASON}", source="bus")
        store.mark_message(msg_id, msg_status)
        store.conn.execute("UPDATE envelopes SET delivered_msg_id=? WHERE id=?",
                           (msg_id, env_id))
    if envelope_state != "queued":
        store.mark_envelope(env_id, envelope_state)
    if delivered:
        store.mark_objection_delivered(aid, delivered)
    if withdrawn:
        store.withdraw_objection(aid, withdrawn)
    return store.get_work_order(wo["id"]), row_of(store, wo["id"], aid)


def row_of(store, wo_id: str, aid: int) -> dict:
    return next(a for a in ops.assumptions_with_rulings(store, wo_id)
                if a["id"] == aid)


# -- the historical row, which is the test that matters --------------------------------


def test_a_row_written_before_the_columns_renders_exactly_as_before(client, project):
    """kn-c712a5d6. Every new column absent from the table, not merely empty: the row is
    read back after the upgrade added them, which is the only shape live data has."""
    wo = ops.create_work_order("proj_a", "task with a judgement call")
    store = ProjectStore(project)
    aid = store.add_assumption(wo["id"], "reused the existing exporter")
    store.set_status(wo["id"], "needs_review")
    store.close()

    conn = sqlite3.connect(project / ".jarvis" / "jarvis.db")
    for col in ADDED_COLUMNS["assumptions"]:
        conn.execute(f"ALTER TABLE assumptions DROP COLUMN {col}")
    conn.commit()
    conn.close()

    store = ProjectStore(project)                              # the upgrade
    row = row_of(store, wo["id"], aid)
    line = ops.assumption_line(row)

    assert line == "#1 pending your review: reused the existing exporter"
    assert ops.provisional_line(row) == ""
    assert ops.objection_line(row) == ""
    assert ops.objection_response_line(row) == ""
    page = page_of(client, wo["id"])
    assert "reused the existing exporter" in page
    for absent in ("objection", "waiting on the worker", "provisionally"):
        assert absent not in page


def test_an_assumption_in_an_auto_review_off_project_grows_no_lines(client, project):
    """Most of the fleet. The columns exist and are empty, which must read as silence."""
    wo = ops.create_work_order("proj_a", "task with a judgement call")
    store = ProjectStore(project)
    aid = store.add_assumption(wo["id"], "kept the default timeout")
    store.set_status(wo["id"], "needs_review")

    row = row_of(store, wo["id"], aid)
    assert ops.assumption_line(row) == "#1 pending your review: kept the default timeout"
    assert row["objection_undeliverable"] is False
    assert row["objection_response"] is None
    page = page_of(client, wo["id"])
    assert "kept the default timeout" in page
    assert "waiting on the worker" not in page and "objection sent" not in page


# -- the four end states ---------------------------------------------------------------


def test_the_four_end_states_each_read_differently(client, project):
    """§8 item 3: four facts, four sentences. Any two spelled the same is the defect."""
    store = ProjectStore(project)
    lines = {}
    _, flight = objecting_order(store)
    lines["flight"] = ops.objection_line(flight)
    _, delivered = objecting_order(store, delivered=NOW + 60)
    lines["delivered"] = ops.objection_line(delivered)
    _, withdrawn = objecting_order(store, withdrawn=NOW + 90,
                                   envelope_state="withdrawn")
    lines["withdrawn"] = ops.objection_line(withdrawn)
    _, dead = objecting_order(store, msg_status="failed")
    lines["dead"] = ops.objection_line(dead)

    assert len(set(lines.values())) == 4, lines
    assert lines["flight"].endswith("in flight")
    assert "delivered at " in lines["delivered"]
    assert "withdrawn at " in lines["withdrawn"]
    assert "the order stopped before it could be delivered" in lines["withdrawn"]
    assert "UNDELIVERABLE" in lines["dead"]
    for line in lines.values():
        assert "objection sent to the worker over the queue at " in line


@pytest.mark.parametrize("kwargs,expected", [
    ({"msg_status": "failed"}, True),                      # the carrier gave up
    ({"envelope_state": "undeliverable"}, True),           # terminal, not delivered
    ({"envelope_state": "handled_by_router"}, True),       # terminal, not delivered
    ({}, False),                                           # still queued: normal
    ({"msg_status": "queued"}, False),                     # in the worker's queue
    ({"envelope_state": "withdrawn"}, False),              # §6.6, nothing failed
    ({"envelope_state": "delivered", "msg_status": "delivered"}, False),
])
def test_undeliverable_is_a_carrier_state_and_only_those(project, kwargs, expected):
    """§9's trigger, verbatim: the message row `failed`, or the envelope terminal and
    not `delivered`. A queued objection is the normal case for the length of a turn."""
    store = ProjectStore(project)
    _, row = objecting_order(store, **kwargs)

    assert ops.objection_undeliverable(store, row) is expected
    assert row["objection_undeliverable"] is expected


def test_a_row_with_no_objection_is_never_undeliverable(project):
    """No `objection_envelope_id` is no objection, not a failed one."""
    store = ProjectStore(project)
    wo = store.create_work_order(title="an order with no objection", description="d")
    aid = store.add_assumption(wo["id"], "nothing was ever objected to")

    assert ops.objection_undeliverable(store, row_of(store, wo["id"], aid)) is False


# -- what the worker did, and the waiting line ------------------------------------------


def test_delivered_with_nothing_back_is_waiting_on_the_worker(client, project):
    """§8 owns this line — it is what the order is waiting on, and it is not the user."""
    store = ProjectStore(project)
    wo, row = objecting_order(store, delivered=NOW + 60)

    line = ops.objection_response_line(row)
    assert line == f"waiting on the worker since {ops._stamp(NOW + 60)}"
    assert line in ops.assumption_line(row)
    assert line in page_of(client, wo["id"])


def test_an_undelivered_objection_is_never_waiting_on_the_worker(project):
    """Not delivered is the ambiguous fact: the worker was never told, so it owes
    nothing. Both the queued case and the withdrawn one."""
    store = ProjectStore(project)
    _, flight = objecting_order(store)
    _, withdrawn = objecting_order(store, withdrawn=NOW + 90)

    assert ops.objection_response_line(flight) == ""
    assert ops.objection_response_line(withdrawn) == ""


def test_a_later_assumption_is_the_workers_answer(project):
    """§6.5: read forward from the delivery. A new assumption IS an act of the worker."""
    store = ProjectStore(project)
    wo, row = objecting_order(store, delivered=NOW + 60)
    store.add_assumption(wo["id"], "so I made the helper idempotent instead")

    line = ops.objection_response_line(row_of(store, wo["id"], row["id"]))
    assert "the worker recorded another assumption" in line
    assert "so I made the helper idempotent instead" in line
    assert "waiting on the worker" not in line


def test_a_worker_message_is_the_workers_answer(project):
    """`wo_messages` direction `agent_to_user` — the worker talking, not the OS."""
    store = ProjectStore(project)
    wo, row = objecting_order(store, delivered=NOW + 60)
    store.record_agent_reply(wo["id"], "fair — retry safety added in b3c1f0a")

    line = ops.objection_response_line(row_of(store, wo["id"], row["id"]))
    assert "the worker replied" in line
    assert "fair — retry safety added in b3c1f0a" in line
    assert "waiting on the worker" not in line


def test_a_question_to_neo_is_the_workers_answer(project):
    """It acted on the objection by asking, which is an answer to "did it read it"."""
    store = ProjectStore(project)
    wo, row = objecting_order(store, delivered=NOW + 60)
    store.add_event(wo["id"], "question_asked",
                    {"neo_question_id": 91, "question": "retry-safe how?"})

    line = ops.objection_response_line(row_of(store, wo["id"], row["id"]))
    assert "the worker asked Neo" in line and "91" in line
    assert "waiting on the worker" not in line


def test_finishing_is_the_workers_answer(project):
    """The worker stopped. It owes nothing further and the line must not say it does."""
    store = ProjectStore(project)
    wo, row = objecting_order(store, delivered=NOW + 60)
    store.add_event(wo["id"], "finished", {"summary": "shipped it unchanged"})

    line = ops.objection_response_line(row_of(store, wo["id"], row["id"]))
    assert "the worker finished" in line
    assert "waiting on the worker" not in line


def test_a_daemon_written_event_is_not_the_workers_answer(project):
    """Only the four acts count. An OS event after delivery proves nothing about the
    worker, and reading one as an answer would retire the waiting line silently."""
    store = ProjectStore(project)
    wo, row = objecting_order(store, delivered=NOW + 60)
    store.add_event(wo["id"], "autoreview_held", {"assumption_id": row["id"], "n": 1,
                                                  "reason": "mentions 'production'"})
    store.add_event(wo["id"], "status", {"from": "running", "to": "waiting_input"})

    assert "waiting on the worker since" in ops.objection_response_line(
        row_of(store, wo["id"], row["id"]))


def test_an_act_before_the_delivery_is_not_a_response(project):
    """Read FORWARD from `objection_delivered_ts`, never backwards: the worker cannot
    have answered a message it had not yet received.

    The delivery is stamped AHEAD of the clock, so the reply the store writes at `now()`
    is genuinely earlier than it — the shape a real record has when the worker was mid-
    sentence when the objection landed."""
    from jarvis import db

    store = ProjectStore(project)
    wo, row = objecting_order(store, delivered=db.now() + 3600)
    store.record_agent_reply(wo["id"], "progress note written before the objection")

    assert "waiting on the worker since" in ops.objection_response_line(
        row_of(store, wo["id"], row["id"]))


# -- one renderer, both surfaces --------------------------------------------------------


def test_the_page_and_jarvis_wo_show_render_the_same_strings(client, project):
    """test_ui.py:595's claim, one section along: the page's lines ARE
    `ops.assumption_line`'s, so no surface can spell an objection its own way."""
    store = ProjectStore(project)
    wo, row = objecting_order(store, delivered=NOW + 60, transport="peer")
    store.record_agent_reply(wo["id"], "fair — retry safety added in b3c1f0a")
    row = row_of(store, wo["id"], row["id"])

    provisional = ops.provisional_line(row)
    objection = ops.objection_line(row)
    response = ops.objection_response_line(row)
    line = ops.assumption_line(row)
    readable = cli._readable_autoreview({"assumptions": [row]})["assumptions"]

    page = page_of(client, wo["id"])
    for part in (provisional, objection, response):
        assert part, row
        assert part in line
        assert part in page
    assert readable == [line]


def test_the_page_shows_the_objection_in_the_specs_order(client, project):
    """§8: text and status, then Neo's provisional reading, then the objection, then the
    response. One ordering, because the user asked for it in that order."""
    store = ProjectStore(project)
    wo, row = objecting_order(store, delivered=NOW + 60)
    page = page_of(client, wo["id"])
    row = row_of(store, wo["id"], row["id"])

    at = [page.index(p) for p in ("the API is idempotent", ops.provisional_line(row),
                                  ops.objection_line(row),
                                  ops.objection_response_line(row))]
    assert at == sorted(at), at


# -- §7 on a settled row ----------------------------------------------------------------


def test_a_confirmed_provisional_accept_reads_correctly_once_settled(client, project):
    """§7: what the OS thought while the work ran AND what it thought once it saw the
    result. Both readings on the row, with the decider naming the machine."""
    store = ProjectStore(project)
    wo = store.create_work_order(title="an order that landed", description="d")
    aid = store.add_assumption(wo["id"], "the exporter handles the empty case")
    store.record_provisional(aid, verdict="accept", reason="no new surface",
                             model="opus", stakes="routine")
    store.review_assumption(aid, "accepted", decided_by=ASSUMPTION_DECIDER_OS,
                            reason="the diff matches the reading", model="opus")
    store.add_event(wo["id"], "autoreview_confirmed",
                    {"assumption_id": aid, "n": 1, "model": "opus",
                     "reason": "the diff matches the reading"})
    store.set_status(wo["id"], "needs_review")

    row = row_of(store, wo["id"], aid)
    line = ops.assumption_line(row)
    assert "accepted by the OS (neo, opus)" in line
    assert "provisionally accepted by the OS (Neo, opus)" in line
    assert ops.objection_response_line(row) == ""
    assert "provisionally accepted by the OS (Neo, opus)" in page_of(client, wo["id"])


def test_an_unconfirmed_provisional_accept_still_says_both_readings(client, project):
    """The two readings disagreed, so the assumption is the user's and they get both."""
    store = ProjectStore(project)
    wo = store.create_work_order(title="an order that landed", description="d")
    aid = store.add_assumption(wo["id"], "the exporter handles the empty case")
    store.record_provisional(aid, verdict="accept", reason="no new surface",
                             model="opus", stakes="routine")
    store.add_event(wo["id"], "autoreview_unconfirmed",
                    {"assumption_id": aid, "n": 1, "neo_question_id": 77,
                     "reason": "the diff changes a public default"})
    store.set_status(wo["id"], "needs_review")

    row = row_of(store, wo["id"], aid)
    line = ops.assumption_line(row)
    assert "pending your review" in line
    assert "Neo did not confirm its early reading (question 77)" in line
    assert "provisionally accepted by the OS (Neo, opus)" in line
    page = page_of(client, wo["id"])
    assert "Neo did not confirm its early reading (question 77)" in page
    assert "provisionally accepted by the OS (Neo, opus)" in page
