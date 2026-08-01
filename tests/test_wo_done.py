"""Marking a work order done — the user closing it themselves.

The dashboard used to offer only Cancel, Hide and Delete, none of which says "the work
is finished": cancel records that it should not have happened, hide only stops showing
it, delete destroys the record. `ops.mark_done` is the missing verb, and it borrows two
behaviours on purpose — it stops the worker like `cancel`, and it refuses over pending
assumptions like `ack_attention`.
"""

from __future__ import annotations

import json as _json

import pytest

from jarvis import claude_cli, cli, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def test_mark_done_completes_the_work_order(started, project):
    wo = ops.create_work_order("proj_a", "already handled by hand")

    out = ops.mark_done(wo["id"])

    assert out["status"] == "completed"
    assert out["was"] == "pending"
    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["status"] == "completed"
    assert "marked_done" in [e["kind"] for e in store.list_events(wo["id"])]


def test_mark_done_kills_a_running_worker_turn(started, fake_claude, project):
    """The same hazard `cancel` guards: nobody reads the output, the process runs on."""
    fake_claude.hold_turns()
    wo = ops.create_work_order("proj_a", "worker still going")
    started.tick()
    store = ProjectStore(project)
    turn = store.latest_turn(wo["id"])
    assert claude_cli.process_alive(turn["pid"])

    out = ops.mark_done(wo["id"])

    assert out["session_stopped"] is True
    assert store.latest_turn(wo["id"])["state"] == "failed"
    assert not claude_cli.process_alive(turn["pid"])


def test_mark_done_clears_the_attention_flag(started, project):
    wo = ops.create_work_order("proj_a", "flagged")
    store = ProjectStore(project)
    store.flag_attention(wo["id"], "worker asked something")

    ops.mark_done(wo["id"])

    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"] == 0
    assert not fresh["attention_reason"]


def test_mark_done_refuses_while_assumptions_are_pending(started, project):
    """Closing over a pending decision would accept it on the user's behalf."""
    wo = ops.create_work_order("proj_a", "has an open question")
    ops.assume(wo["id"], "went with the sqlite backend")

    with pytest.raises(ops.OpsError) as e:
        ops.mark_done(wo["id"])

    assert "jarvis wo review" in str(e.value)
    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["status"] != "completed"
    assert store.pending_assumptions(wo["id"])  # untouched, not silently accepted


def test_mark_done_leaves_the_workers_own_summary_alone(started, project):
    wo = ops.create_work_order("proj_a", "worker reported first")
    ops.finish(wo["id"], "shipped the parser")

    ops.mark_done(wo["id"])

    assert ProjectStore(project).get_work_order(wo["id"])["result_summary"] == \
        "shipped the parser"


def test_mark_done_closes_the_backlog_item_it_came_from(started, project):
    central = CentralStore()
    item = central.add_backlog("proj_a", "tidy the parser")
    result = ops.promote_backlog(item["id"], force=True)

    ops.mark_done(result["wo_id"])

    assert [i for i in central.list_backlog(status=None)
            if i["id"] == item["id"]][0]["status"] == "done"


def test_cli_wo_done(started, project, capsys):
    wo = ops.create_work_order("proj_a", "close me from the terminal")

    assert cli.main(["wo", "done", wo["id"], "--json"]) == 0

    out = _json.loads(capsys.readouterr().out)
    assert out["status"] == "completed"
    assert ProjectStore(project).get_work_order(wo["id"])["status"] == "completed"
