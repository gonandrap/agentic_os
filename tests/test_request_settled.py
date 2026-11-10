"""One notice per user ask once its whole tree settles (docs/superpowers/specs/2026-11-09-yours-requestor-stamp-and-tree-notice.md §4)"""

from __future__ import annotations

import json

import pytest

from jarvis import ops, timeline
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(started, project):
    s = ProjectStore(project)
    yield s
    s.close()


def sweep(daemon: Daemon, times: int = 3) -> None:
    for _ in range(times):
        daemon.settle_request_trees()


def notices(store: ProjectStore, source: str = "request") -> list[dict]:
    return [n for n in store.unrouted_notifications() if n["source"] == source]


def settled_events(store: ProjectStore) -> list[dict]:
    rows = store.conn.execute(
        "SELECT * FROM wo_events WHERE kind='request_settled'").fetchall()
    return [dict(r) for r in rows]


def test_a_plain_user_wo_gets_one_notice_with_its_final_message(started, store):
    wo = store.create_work_order("fix it", requestor="user")
    store.update_work_order(wo["id"], result_summary="fixed", pr_url=PR)
    store.record_agent_reply(wo["id"], "Done. Want me to file the gap?")
    ops.complete_merged(store, store.get_work_order(wo["id"]))

    sweep(started)

    [note] = notices(store)
    assert note["wo_id"] == wo["id"] and note["level"] == "info"
    assert note["title"] == f"{wo['id']} done: fix it"
    assert "Want me to file the gap?" in note["body"]
    assert notices(store, "completion") == []
    [event] = settled_events(store)
    payload = json.loads(event["payload"])
    assert payload == {"root_id": wo["id"], "statuses": {wo["id"]: "completed"},
                       "inbox_id": note["id"]}


def test_a_user_fo_notifies_once_after_its_last_child_across_sweeps(started, store):
    fo = store.create_feature_order("big ask", requestor="user")
    planner = store.create_work_order("plan", kind="planner", parent_id=fo["id"],
                                      status="completed")
    child = store.create_work_order("part", parent_id=fo["id"], status="waiting_pr_merge")
    store.set_feature_status(fo["id"], "completed")

    sweep(started)
    assert notices(store) == []  # waiting_pr_merge is not terminal

    store.update_work_order(child["id"], pr_url=PR)
    ops.complete_merged(store, store.get_work_order(child["id"]))
    assert notices(store, "completion") == []  # a child is never told separately
    sweep(started)
    sweep(started)

    [note] = notices(store)
    assert note["wo_id"] == fo["id"]
    [event] = settled_events(store)
    assert event["wo_id"] == planner["id"]  # first work order in pre-order holds it
    assert json.loads(event["payload"])["root_id"] == fo["id"]


def test_every_block_of_the_notice(started, store, monkeypatch):
    monkeypatch.setattr(ops, "cost_report",
                        lambda project=None, target=None, **kw: {"totals": {"total_cost_usd": 1.25}})
    fo = store.create_feature_order("big ask", requestor="user")
    planner = store.create_work_order("plan", kind="planner", parent_id=fo["id"],
                                      status="completed")
    child = store.create_work_order("part", parent_id=fo["id"], status="failed")
    store.update_work_order(child["id"], pr_url=PR)
    aid = store.add_assumption(child["id"], "kept the old flag")
    store.review_assumption(aid, "accepted", decided_by="neo")
    mine = store.add_assumption(child["id"], "the user ruled this")
    store.review_assumption(mine, "accepted", decided_by="user")
    store.set_feature_status(fo["id"], "failed")

    sweep(started)

    [note] = notices(store)
    assert note["title"] == f"{fo['id']} done: big ask"
    assert note["level"] == "warning"
    blocks = note["body"].split("\n\n")
    assert blocks[0] == f"Outcome: {fo['id']} ended failed; {child['id']} ended failed"
    assert blocks[1] == "No report on record."
    assert blocks[2] == (f"Orders run:\n{planner['id']} completed plan\n"
                         f"{child['id']} failed part")
    assert blocks[3] == f"Decided for you:\n{child['id']} #1 accepted by neo: kept the old flag"
    assert blocks[4] == f"Pull requests:\n{PR}"
    assert blocks[5] == "Cost: $2.50"
    assert blocks[6] == f"http://127.0.0.1:{started.catalog.os.ui_port}/yours#{fo['id']}"


def test_a_failed_body_writes_neither_event_nor_notice_and_retries(started, store,
                                                                   monkeypatch):
    wo = store.create_work_order("fix it", requestor="user", status="completed")

    def boom(*a, **kw):
        raise RuntimeError("no body")

    with monkeypatch.context() as m:
        m.setattr(ops, "completion_lines", boom)
        sweep(started)
        assert notices(store) == [] and settled_events(store) == []

    sweep(started)
    assert [n["wo_id"] for n in notices(store)] == [wo["id"]]


def test_a_user_do_with_no_follow_ups_writes_the_event_and_no_notice(started, store):
    do = store.create_feature_order("which db?", kind="decision", requestor="user")
    researcher = store.create_work_order("research", kind="researcher",
                                         parent_id=do["id"], status="completed")
    store.set_feature_status(do["id"], "completed", decided_by_user=True)

    sweep(started)

    assert notices(store) == []
    [event] = settled_events(store)
    assert event["wo_id"] == researcher["id"]
    assert json.loads(event["payload"])["inbox_id"] is None


def test_a_user_do_with_follow_ups_gets_the_tree_notice(started, store):
    do = store.create_feature_order("which db?", kind="decision", requestor="user")
    store.create_work_order("research", kind="researcher", parent_id=do["id"],
                            status="completed")
    store.create_work_order("migrate", status="completed",
                            metadata={ops.ORIGIN_DO_KEY: do["id"]}, requestor="user")
    store.set_feature_status(do["id"], "completed", decided_by_user=True)

    sweep(started)

    assert [n["wo_id"] for n in notices(store)] == [do["id"]]


def test_a_tree_with_no_work_order_is_marked_by_its_notice(started, store):
    fo = store.create_feature_order("dropped", requestor="user")
    store.set_feature_status(fo["id"], "cancelled")

    sweep(started)

    assert [n["wo_id"] for n in notices(store)] == [fo["id"]]
    assert settled_events(store) == []
    assert fo["id"] in store.settled_request_roots()


def test_a_legacy_wo_keeps_the_completion_notice_and_gets_no_tree_notice(started, store):
    wo = store.create_work_order("old", origin="jarvis")
    assert wo["requestor"] is None
    store.set_status(wo["id"], "completed")

    assert ops.notify_completed(store, store.get_work_order(wo["id"]), "finished")
    sweep(started)
    assert notices(store) == []


def test_an_os_root_gets_no_notice(started, store):
    wo = store.create_work_order("self fix", requestor="os", status="completed")
    assert ops.notify_completed(store, wo, "finished") is None
    sweep(started)
    assert notices(store) == [] and settled_events(store) == []


def test_the_timeline_labels_the_event():
    title, detail = timeline._describe("request_settled",
                                       {"root_id": "fo-1", "inbox_id": 4})
    assert "fo-1" in title and "#4" in detail
