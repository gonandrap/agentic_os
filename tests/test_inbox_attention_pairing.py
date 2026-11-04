"""A notification raised WITH an attention flag is one incident: acking either settles both..."""

from __future__ import annotations

import pytest

from jarvis import autoreview, cli, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore


REASON = "worker failed — review and retry"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def flagged(store: ProjectStore, *, paired: bool = True) -> dict:
    """A failed order, flagged, with the notification the settler raises beside the flag"""
    wo = store.create_work_order(title="t", description="d", origin="jarvis")
    store.set_status(wo["id"], "failed")
    store.flag_attention(wo["id"], REASON)
    store.add_notification(title=f"{wo['id']} worker turn failed", body="boom",
                           level="warning", wo_id=wo["id"], source="reconciler",
                           paired=paired)
    return wo


def route(started, store) -> list[dict]:
    spec = next(p for p in started.catalog.projects if p.name == "proj_a")
    started.route_outbox(spec, store)
    started.central.conn.commit()
    central = CentralStore()
    try:
        return central.unacked_inbox()
    finally:
        central.close()


def test_wo_ack_marks_the_paired_notification_read(started, store):
    wo = flagged(store)
    assert [i["wo_id"] for i in route(started, store)] == [wo["id"]]

    out = ops.ack_attention(wo["id"])

    assert out["acknowledged"] == [wo["id"]]
    assert out["inbox_read"] == 1
    assert route(started, store) == []


def test_inbox_ack_lowers_the_paired_flag(started, store):
    wo = flagged(store)
    item = route(started, store)[0]

    out = ops.ack_inbox(item["id"])

    assert out["attention_lowered"] == [wo["id"]]
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_the_cli_inbox_ack_reaches_ops(started, store):
    wo = flagged(store)
    item = route(started, store)[0]

    assert cli.main(["inbox", "ack", str(item["id"])]) == 0
    assert cli.main(["inbox", "ack"]) == 0

    assert route(started, store) == []
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_inbox_ack_all_lowers_every_paired_flag(started, store):
    a, b = flagged(store), flagged(store)
    route(started, store)

    out = ops.ack_inbox(None)

    assert sorted(out["attention_lowered"]) == sorted([a["id"], b["id"]])


def test_a_pending_assumption_is_never_acked_away_by_the_inbox(started, store):
    """The refusal `jarvis wo ack` keeps, kept here too"""
    wo = store.create_work_order(title="t", description="d", origin="jarvis")
    store.set_status(wo["id"], "needs_review")
    store.add_assumption(wo["id"], "assumed the schema is stable")
    store.flag_attention(wo["id"], "1 assumption pending your review")
    store.add_notification(title="review it", wo_id=wo["id"], level="warning",
                           paired=True)
    item = route(started, store)[0]

    out = ops.ack_inbox(item["id"])

    assert out["attention_lowered"] == []
    assert [k["wo_id"] for k in out["attention_kept"]] == [wo["id"]]
    assert store.get_work_order(wo["id"])["needs_attention"]


def test_a_standalone_notification_keeps_its_own_ack(started, store):
    """Unpaired: acking it touches no flag, and `wo ack` does not read it"""
    wo = flagged(store, paired=False)
    route(started, store)

    ops.ack_attention(wo["id"])
    assert len(route(started, store)) == 1

    store.flag_attention(wo["id"], REASON)
    out = ops.ack_inbox(None)
    assert out["attention_lowered"] == []
    assert store.get_work_order(wo["id"])["needs_attention"]


def test_a_flag_that_moved_on_is_not_lowered_by_an_old_notification(started, store):
    """Only the SAME incident: a newer reason is a new thing to see"""
    wo = flagged(store)
    item = route(started, store)[0]
    store.flag_attention(wo["id"], "something else entirely")

    assert ops.ack_inbox(item["id"])["attention_lowered"] == []
    assert store.get_work_order(wo["id"])["needs_attention"]


def test_status_says_what_each_list_is_and_which_command_clears_it(started, store,
                                                                   capsys):
    flagged(store)
    route(started, store)

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert cli.ATTENTION_LIST_NOTE in out
    assert cli.INBOX_LIST_NOTE in out
    assert "jarvis wo ack" in cli.ATTENTION_LIST_NOTE
    assert "jarvis inbox ack" in cli.INBOX_LIST_NOTE


def test_the_dashboard_says_the_same(started, store):
    """The user met this on the dashboard first: both panels name their list and their command"""
    import html

    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    flagged(store)
    item = route(started, store)[0]
    c = TestClient(create_app(), follow_redirects=False)

    page = html.unescape(c.get("/").text)
    assert cli.ATTENTION_LIST_NOTE in page
    assert cli.INBOX_LIST_NOTE in page
    assert cli.INBOX_LIST_NOTE in html.unescape(c.get("/inbox").text)
    assert c.post("/inbox/ack", data={"inbox_id": str(item["id"])}).status_code == 303
    assert not store.get_work_order(item["wo_id"])["needs_attention"]


def test_assumptions_pending_review_reach_the_inbox_once(started, store):
    """Issue #244: the flag alone reaches nobody; one paired notice per raise"""
    wo = store.create_work_order(title="t", description="d", origin="jarvis")
    store.set_status(wo["id"], "running")
    store.add_assumption(wo["id"], "assumed the schema is stable")

    assert ops.land_when_cleared(store, store.get_work_order(wo["id"])) == "needs_review"
    ops.flag_assumptions_pending(store, wo["id"])

    items = route(started, store)
    assert [i["wo_id"] for i in items] == [wo["id"]]
    assert "1 assumption" in items[0]["title"]
    assert store.get_work_order(wo["id"])["attention_reason"] == ops.ASSUMPTIONS_PENDING_REASON
    # What Daemon.settle_review_pings matches on: a ping raised under another reason never settles
    assert items[0]["paired_reason"] == ops.ASSUMPTIONS_PENDING_REASON


def pinged(store: ProjectStore, n: int = 2, status: str = "needs_review") -> tuple[dict, list[int]]:
    """An order holding n pending assumptions and the review ping raised for them"""
    wo = store.create_work_order(title="t", description="d", origin="jarvis")
    store.set_status(wo["id"], status)
    ids = [store.add_assumption(wo["id"], f"assumed {i}") for i in range(n)]
    store.flag_attention(wo["id"], ops.ASSUMPTIONS_PENDING_REASON)
    store.add_notification(title=f"{wo['id']} is waiting on your review", wo_id=wo["id"],
                           level="warning", source="assumptions", paired=True)
    return wo, ids


def settle(started, store) -> list[dict]:
    spec = next(p for p in started.catalog.projects if p.name == "proj_a")
    route(started, store)
    started.settle_review_pings(spec, store)
    return route(started, store)


def test_neo_settling_every_assumption_acks_the_ping_on_the_next_pass(started, store):
    wo, ids = pinged(store)
    for aid in ids:
        store.review_assumption(aid, "accepted")

    assert settle(started, store) == []


def test_the_daemon_tick_runs_the_settling_pass(started, store):
    wo, ids = pinged(store, n=1)
    store.review_assumption(ids[0], "accepted")

    started.tick()

    assert "is waiting on your review" not in " ".join(i["title"] for i in route(started, store))


def test_one_assumption_still_pending_keeps_the_ping(started, store):
    wo, ids = pinged(store)
    store.review_assumption(ids[0], "accepted")

    assert [i["wo_id"] for i in settle(started, store)] == [wo["id"]]


def test_a_failed_order_acks_its_ping_but_keeps_its_failed_turn_row(started, store):
    """Terminal includes `failed`, pending assumptions or not; the turn's own row stays"""
    wo, _ = pinged(store, status="failed")
    store.flag_attention(wo["id"], REASON)  # the failure path moves the flag before its row
    store.add_notification(title=f"{wo['id']} worker turn failed", wo_id=wo["id"],
                           level="warning", source="reconciler", paired=True)

    assert [i["title"] for i in settle(started, store)] == [f"{wo['id']} worker turn failed"]


def test_a_failed_turn_row_survives_on_the_same_settled_order(started, store):
    wo, ids = pinged(store)
    for aid in ids:
        store.review_assumption(aid, "accepted")
    store.flag_attention(wo["id"], REASON)
    store.add_notification(title=f"{wo['id']} worker turn failed", wo_id=wo["id"],
                           level="warning", source="reconciler", paired=True)

    assert [i["title"] for i in settle(started, store)] == [f"{wo['id']} worker turn failed"]


def test_a_stale_ping_on_a_completed_order_heals_with_assumptions_still_pending(started, store):
    """Today's backlog: terminal is enough, whatever the rows say"""
    wo, _ = pinged(store, status="completed")

    assert settle(started, store) == []


# -- a row is the user's only once Neo cannot take it further (io-ed253c2b)


ACCEPT = {"escalate": False, "verdict": "approved", "stakes": "routine",
          "reason": "fine", "answer": "APPROVED"}
ESCALATE = {"escalate": True, "stakes": "routine", "reason": "yours", "answer": ""}


@pytest.fixture()
def reviewing(started, catalog_file):
    """Auto-review ON in the file `ops.auto_review_at` reads AND in the daemon's catalog"""
    for key in ("validation.enabled", "validation.auto_review"):
        ops.set_config(key, True, project="proj_a", reason="testing io-ed253c2b",
                       catalog_path=str(catalog_file))
    daemon = Daemon(load_catalog(catalog_file))
    daemon.catalog.project("proj_a").validation.stakes_classifier = "regex"
    return daemon


def delivered(store, *assumptions: str) -> dict:
    wo = store.create_work_order(title="t", description="d", origin="jarvis")
    for text in assumptions:
        store.add_assumption(wo["id"], text)
    store.set_status(wo["id"], "running")
    assert ops.land_when_cleared(store, store.get_work_order(wo["id"])) == "needs_review"
    return store.get_work_order(wo["id"])


def tick(daemon, store) -> None:
    daemon.auto_review(daemon.catalog.project("proj_a"), store)


def asked(wo: dict) -> list[dict]:
    neo = NeoStore()
    try:
        return [q for q in neo.list_questions()
                if q["kind"] == "assumption" and q["wo_id"] == wo["id"]]
    finally:
        neo.close()


def rule(daemon, store, q: dict, verdict: dict) -> None:
    """What `neo.drain_queue` does to one question, then its delivery"""
    neo, central = NeoStore(), CentralStore()
    try:
        if verdict["escalate"]:
            neo.mark(q["id"], "escalated", reason=verdict["reason"])
        else:
            neo.record_answer(q["id"], verdict["answer"], answered_by="neo",
                              reason=verdict["reason"])
        daemon._deliver_assumption_verdict(central, neo, store, q, verdict)
        central.conn.commit()
    finally:
        neo.close()
        central.close()


def pings(daemon, store, wo: dict) -> list[str]:
    return [i["title"] for i in route(daemon, store)
            if i["wo_id"] == wo["id"] and "waiting on your review" in i["title"]]


def test_delivery_alone_pings_nobody_when_neo_will_rule(reviewing, store):
    wo = delivered(store, "reused the helper", "kept the default")

    assert pings(reviewing, store, wo) == []
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_neo_accepting_every_row_never_warns(reviewing, store):
    wo = delivered(store, "reused the helper", "kept the default")
    tick(reviewing, store)

    for q in asked(wo):
        rule(reviewing, store, q, ACCEPT)

    assert store.pending_assumptions(wo["id"]) == []
    assert pings(reviewing, store, wo) == []


def test_escalating_one_of_two_warns_once_and_counts_one(reviewing, store):
    wo = delivered(store, "reused the helper", "kept the default")
    tick(reviewing, store)
    first, second = asked(wo)

    rule(reviewing, store, first, ESCALATE)

    [title] = pings(reviewing, store, wo)
    assert "of 1 assumption" in title
    fresh = store.get_work_order(wo["id"])
    assert fresh["attention_reason"] == ops.ASSUMPTIONS_PENDING_REASON
    assert invariants.true_blockers(store, fresh)[0] == "1 assumption pending your review"

    rule(reviewing, store, second, ACCEPT)
    assert len(pings(reviewing, store, wo)) == 1


def test_a_high_stakes_hold_pings_once_across_ticks(reviewing, store):
    wo = delivered(store, "this will drop the users table")

    tick(reviewing, store)
    tick(reviewing, store)

    [title] = pings(reviewing, store, wo)
    assert "of 1 assumption" in title


def test_a_hold_the_next_pass_retries_stays_silent(reviewing, store):
    wo = delivered(store, "reused the helper")
    [a] = store.pending_assumptions(wo["id"])
    store.add_event(wo["id"], "autoreview_held", {
        "code": autoreview.HELD_ROUND_OPEN, "assumption_id": a["id"], "n": 1, "round": 1})

    ops.flag_assumptions_pending(store, wo["id"])

    assert pings(reviewing, store, wo) == []


def test_neo_disabled_pings_at_delivery(reviewing, store, monkeypatch):
    monkeypatch.setattr(ops, "autoreview_off_because",
                        lambda path: "`os.neo.enabled` is off")

    wo = delivered(store, "reused the helper")

    assert len(pings(reviewing, store, wo)) == 1


def test_an_unreachable_question_pings_the_row_once(reviewing, store):
    wo = delivered(store, "reused the helper")
    tick(reviewing, store)
    [q] = asked(wo)
    neo, central = NeoStore(), CentralStore()
    neo.mark(q["id"], "failed", reason="unreachable")

    reviewing._note_question_unreachable(central, q, "down")
    reviewing._note_question_unreachable(central, q, "down")
    central.conn.commit()
    neo.close()
    central.close()

    [item] = [i for i in route(reviewing, store) if i["wo_id"] == wo["id"]]
    assert "of 1 assumption" in item["title"]


def test_redelivery_raises_the_flag_again_without_a_second_ping(reviewing, store):
    wo = delivered(store, "reused the helper")
    tick(reviewing, store)
    rule(reviewing, store, asked(wo)[0], ESCALATE)
    assert len(pings(reviewing, store, wo)) == 1

    store.set_status(wo["id"], "validating")
    store.clear_attention(wo["id"])
    assert ops.land_when_cleared(store, store.get_work_order(wo["id"])) == "needs_review"

    assert store.get_work_order(wo["id"])["needs_attention"]
    assert len(pings(reviewing, store, wo)) == 1


def test_auto_review_off_still_pings_every_row_at_delivery(started, store):
    wo = delivered(store, "reused the helper", "kept the default")
    ops.flag_assumptions_pending(store, wo["id"])

    [title] = pings(started, store, wo)
    assert "of 2 assumptions" in title
