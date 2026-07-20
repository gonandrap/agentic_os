"""Hiding and deleting work orders.

Hiding is presentation-only: the record stays, it just stops competing for the
user's attention. Deleting is destructive and cascades — the work order and every
row that hangs off it (events, messages, assumptions, notifications, central inbox
entries) leave the database for good.
"""

from __future__ import annotations

import json as _json

import pytest

from jarvis import cli, ops
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore

# -- store layer -------------------------------------------------------------------


def test_hidden_work_orders_are_out_of_sight_but_still_there(project):
    store = ProjectStore(project)
    keep = store.create_work_order("keep me")
    hide = store.create_work_order("hide me")

    store.set_hidden(hide["id"], True)

    listed = [wo["id"] for wo in store.list_work_orders()]
    assert listed == [keep["id"]]
    assert [wo["id"] for wo in store.list_work_orders(include_hidden=True)] == [
        hide["id"], keep["id"],
    ]
    assert store.get_work_order(hide["id"])["hidden"] == 1
    assert "hidden" in [e["kind"] for e in store.list_events(hide["id"])]


def test_unhide_puts_the_work_order_back(project):
    store = ProjectStore(project)
    wo = store.create_work_order("x")
    store.set_hidden(wo["id"], True)
    store.set_hidden(wo["id"], False)
    assert [w["id"] for w in store.list_work_orders()] == [wo["id"]]
    assert store.get_work_order(wo["id"])["hidden"] == 0


def test_hidden_work_orders_drop_out_of_the_summary(project):
    """Hiding is how the user says 'stop counting this against me'."""
    store = ProjectStore(project)
    wo = store.create_work_order("noisy")
    store.flag_attention(wo["id"], "needs you")
    store.add_assumption(wo["id"], "assumed sqlite")
    assert store.summary()["needs_attention"] == 1

    store.set_hidden(wo["id"], True)
    summary = store.summary()
    assert summary["needs_attention"] == 0
    assert summary["pending_assumptions"] == 0
    assert summary["by_status"] == {}
    assert store.pending_assumptions() == []


def test_claiming_ignores_hidden_work_orders(project):
    """A hidden pending order is not dispatched — hiding parks the work."""
    store = ProjectStore(project)
    hidden = store.create_work_order("hidden one")
    store.set_hidden(hidden["id"], True)
    assert store.claim_next_pending() is None


def test_delete_work_order_cascades(project):
    store = ProjectStore(project)
    wo = store.create_work_order("doomed")
    other = store.create_work_order("survivor")
    store.queue_message(wo["id"], "feedback")
    store.record_agent_reply(wo["id"], "worker said hi")
    store.add_assumption(wo["id"], "assumed a thing")
    store.add_notification("done", wo_id=wo["id"])
    store.queue_message(other["id"], "untouched")
    store.add_event(other["id"], "turn_ended")

    counts = store.delete_work_order(wo["id"])

    assert counts == {"events": 2, "messages": 2, "assumptions": 1, "notifications": 1}
    with pytest.raises(KeyError):
        store.get_work_order(wo["id"])
    assert store.list_events(wo["id"]) == []
    assert store.list_messages(wo["id"]) == []
    assert store.pending_assumptions(wo["id"]) == []
    assert store.unrouted_notifications() == []
    # the neighbour is untouched
    assert store.get_work_order(other["id"])["title"] == "survivor"
    assert len(store.list_messages(other["id"])) == 1
    assert len(store.list_events(other["id"])) == 2


def test_delete_unknown_work_order_raises(project):
    store = ProjectStore(project)
    with pytest.raises(KeyError):
        store.delete_work_order("wo-nope")


# -- ops layer ---------------------------------------------------------------------


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)


def test_ops_hide_and_unhide(started, project):
    wo = ops.create_work_order("proj_a", "shy task")
    assert ops.hide_work_order(wo["id"])["hidden"] is True
    store = ProjectStore(project)
    assert store.list_work_orders() == []
    assert ops.hide_work_order(wo["id"], hidden=False)["hidden"] is False
    assert len(store.list_work_orders()) == 1


def test_ops_hidden_work_order_leaves_the_attention_list(started):
    wo = ops.create_work_order("proj_a", "noisy task")
    _, path, _ = ops.find_work_order(wo["id"])
    store = ProjectStore(path)
    store.flag_attention(wo["id"], "needs you")
    store.close()
    assert any(a["wo_id"] == wo["id"] for a in ops.os_status()["attention"])

    ops.hide_work_order(wo["id"])
    assert not any(a["wo_id"] == wo["id"] for a in ops.os_status()["attention"])


def test_ops_delete_removes_central_inbox_traces(started, project):
    wo = ops.create_work_order("proj_a", "doomed task")
    central = CentralStore()
    central.add_inbox("proj_a", "worker finished", level="info", wo_id=wo["id"])
    central.add_inbox("proj_a", "unrelated", level="info")
    central.close()

    out = ops.delete_work_order(wo["id"])

    assert out["wo_id"] == wo["id"]
    assert out["project"] == "proj_a"
    assert out["deleted"]["inbox"] == 1
    central = CentralStore()
    try:
        assert [i["title"] for i in central.unacked_inbox()] == ["unrelated"]
    finally:
        central.close()
    with pytest.raises(ops.OpsError):
        ops.find_work_order(wo["id"])


def test_ops_delete_releases_the_backlog_item(started):
    """Deleting a promoted work order must not leave the backlog pointing at a ghost."""
    central = CentralStore()
    item = central.add_backlog("proj_a", "some deferred thing")
    central.close()
    promoted = ops.promote_backlog(item["id"])

    ops.delete_work_order(promoted["wo_id"])

    central = CentralStore()
    try:
        row = [i for i in central.list_backlog(status=None)][0]
    finally:
        central.close()
    assert row["promoted_wo_id"] is None
    assert row["status"] == "open"


# -- CLI ---------------------------------------------------------------------------


def test_cli_hide_unhide_and_list_filtering(started, capsys):
    wo = ops.create_work_order("proj_a", "shy task")

    cli.main(["wo", "hide", wo["id"], "--json"])
    assert _json.loads(capsys.readouterr().out)["hidden"] is True

    cli.main(["wo", "list", "--json"])
    assert _json.loads(capsys.readouterr().out) == []

    cli.main(["wo", "list", "--include-hidden", "--json"])
    assert [w["id"] for w in _json.loads(capsys.readouterr().out)] == [wo["id"]]

    cli.main(["wo", "unhide", wo["id"], "--json"])
    assert _json.loads(capsys.readouterr().out)["hidden"] is False
    cli.main(["wo", "list", "--json"])
    assert [w["id"] for w in _json.loads(capsys.readouterr().out)] == [wo["id"]]


def test_cli_delete_requires_confirmation(started, capsys):
    wo = ops.create_work_order("proj_a", "doomed task")

    with pytest.raises(SystemExit):
        cli.main(["wo", "delete", wo["id"], "--json"])
    ops.find_work_order(wo["id"])  # still there — refusal was real

    cli.main(["wo", "delete", wo["id"], "--yes", "--json"])
    out = _json.loads(capsys.readouterr().out)
    assert out["wo_id"] == wo["id"]
    with pytest.raises(ops.OpsError):
        ops.find_work_order(wo["id"])


def test_existing_databases_gain_the_hidden_column(project):
    """Production DBs predate `hidden`; opening one must ALTER it in, not blow up."""
    store = ProjectStore(project)
    wo = store.create_work_order("from before the feature")
    store.conn.execute("ALTER TABLE work_orders DROP COLUMN hidden")  # simulate an old DB
    store.close()

    store = ProjectStore(project)
    try:
        assert store.get_work_order(wo["id"])["hidden"] == 0
        assert [w["id"] for w in store.list_work_orders()] == [wo["id"]]
        store.set_hidden(wo["id"], True)
        assert store.list_work_orders() == []
    finally:
        store.close()
