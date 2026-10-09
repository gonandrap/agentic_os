"""`jarvis wo retry` — the named way to relaunch a `failed` work order.

docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md. The revive existed as an
unnamed side effect of `wo send`; these tests are about the NAME having a refusal, a
record and a surface, and about the two things the spec forbids: no status write, and no
user stamp on the OS's own relaunch note.
"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def daemon(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def a_failed_order(daemon, project, title: str = "build the exporter") -> dict:
    """A dispatched order whose worker died without delivering — the issue's own shape."""
    wo = ops.create_work_order("proj_a", title)
    daemon.tick()
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "failed")
        store.flag_attention(wo["id"], "worker failed — review and retry")
        return store.get_work_order(wo["id"])
    finally:
        store.close()


def test_retry_queues_one_message_and_records_who_asked(daemon, project):
    wo = a_failed_order(daemon, project)

    out = ops.retry(wo["id"], project_name="proj_a")

    store = ProjectStore(project)
    try:
        msgs = store.list_messages(wo["id"])
        kinds = [e["kind"] for e in store.list_events(wo["id"])]
    finally:
        store.close()
    assert len(msgs) == 1 and msgs[0]["id"] == out["msg_id"]
    assert "retry_requested" in kinds
    assert out["note"] == ops.RETRY_QUEUED.format(msg=out["msg_id"])
    assert out["status"] == "failed"


def test_a_message_less_retry_is_the_os_speaking_not_the_user(daemon, project):
    """§3: `RETRY_NOTE` verbatim, and `user_messages` — the reader that feeds a gate
    request — must NOT return it. An OS literal stamped `user` would let a worker quote
    the OS back to the gate panel as the user authorising something."""
    wo = a_failed_order(daemon, project)

    out = ops.retry(wo["id"], project_name="proj_a", relay=True)

    store = ProjectStore(project)
    try:
        msgs = store.list_messages(wo["id"])
        attributed = store.user_messages(wo["id"])
    finally:
        store.close()
    assert msgs[0]["content"] == ops.RETRY_NOTE
    assert msgs[0]["source"] == "retry"
    assert msgs[0]["authored_by"] == ""
    assert attributed == []
    assert out["authored"] is False


def test_a_retry_with_a_message_is_attributed_to_the_user(daemon, project, monkeypatch):
    # The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID` — and
    # `user_authorship` refuses a stamp to one, which is the point of that check.
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    wo = a_failed_order(daemon, project)

    out = ops.retry(wo["id"], message="carry on from the exporter", project_name="proj_a",
                    relay=True)

    store = ProjectStore(project)
    try:
        attributed = store.user_messages(wo["id"])
    finally:
        store.close()
    assert [m["content"] for m in attributed] == ["carry on from the exporter"]
    assert out["authored"] is True


@pytest.mark.parametrize("status", ["running", "needs_review", "waiting_pr_merge",
                                    "completed", "cancelled", "pending"])
def test_every_other_status_is_refused_in_one_sentence(daemon, project, status):
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], status)
    finally:
        store.close()

    with pytest.raises(ops.OpsError) as e:
        ops.retry(wo["id"], project_name="proj_a")

    assert str(e.value) == (
        f"{wo['id']} is {status}, not failed — `wo retry` relaunches an order whose "
        f"worker died without delivering. Carry on a {status} one with `jarvis wo send "
        f"{wo['id']} \"…\"`."
    )


def test_an_order_that_never_opened_a_conversation_is_refused(daemon, project):
    """§4 refusal 2, reachable through `release_dispatch_claim`: `wo send` queues a
    message `delivery_hold` holds for ever; `retry` refuses instead."""
    wo = ops.create_work_order("proj_a", "task")
    store = ProjectStore(project)
    try:
        assert store.release_dispatch_claim(
            wo["id"], "no worker turn ever started", max_attempts=1) == "failed"
        assert not store.get_work_order(wo["id"])["session_id"]
    finally:
        store.close()

    with pytest.raises(ops.OpsError) as e:
        ops.retry(wo["id"], project_name="proj_a")

    assert str(e.value) == (
        f"{wo['id']} failed before it ever opened a conversation, so there is no session "
        f"to relaunch — a message queued here would sit undelivered "
        f"(`worker_session.delivery_hold` holds it: \"it has no session to resume\"). "
        f"Nothing here can be retried; file the work again."
    )


def test_the_revive_actually_happens_on_the_next_tick(daemon, project):
    """End to end — a row written is not a relaunch. §5: the order stays `failed` and
    stays flagged until the turn goes out."""
    wo = a_failed_order(daemon, project)

    ops.retry(wo["id"], project_name="proj_a")

    store = ProjectStore(project)
    try:
        # §5: the flag is down already and that is cosmetic — `true_blockers`
        # re-derives it every tick until the turn actually goes out.
        assert store.get_work_order(wo["id"])["status"] == "failed"
    finally:
        store.close()
    daemon.tick()
    store = ProjectStore(project)
    try:
        after = store.get_work_order(wo["id"])
    finally:
        store.close()
    assert after["status"] == "running"
    assert not after["needs_attention"]


def test_a_pending_assumption_is_not_buried_by_a_retry(daemon, project):
    """§5: `wo ack` and `wo done` refuse on one; a retry buries nothing, so it is told
    rather than blocked."""
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    ops.assume(wo["id"], "the exporter writes CSV")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "failed")
    finally:
        store.close()

    out = ops.retry(wo["id"], project_name="proj_a")

    store = ProjectStore(project)
    try:
        assert len(store.pending_assumptions(wo["id"])) == 1
    finally:
        store.close()
    assert out["assumptions"] == 1
