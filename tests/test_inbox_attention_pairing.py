"""A notification raised WITH an attention flag is one incident: acking either settles both..."""

from __future__ import annotations

import pytest

from jarvis import cli, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
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
