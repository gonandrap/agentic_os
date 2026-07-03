import pytest

from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore


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
