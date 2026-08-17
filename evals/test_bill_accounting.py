"""Does the bill account for every token, measured against the raw records?

`tests/test_bill.py` checks that the bill is internally consistent — that its parts sum
to its whole. That is necessary and it is not the claim the page makes. A bill can be
perfectly self-consistent and still be missing a third of the spend, which is exactly
what the surface it replaced was doing: it reported the turns whose result JSON survived
and said nothing about the rest.

So the ground truth here is computed INDEPENDENTLY of `jarvis.bill`, out of the three
places spend is actually stored — `wo_turns.usage_json`, the `agent_calls` table, and
Claude Code's session transcript — and the bill is compared against it. An eval that
re-derived the total the same way the code does would only be able to prove the code
agrees with itself.

Every scenario runs the REAL dispatch path against the fake `claude` CLI, so what is
being measured includes the capture (`derive_turn_usage`), the persistence and the
reader, not just the arithmetic at the end.
"""

from __future__ import annotations

import json

import pytest

from jarvis import agent_usage, db, ops
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore

scenario = pytest.mark.scenario


# -- ground truth, from the stores rather than from the bill ---------------------------


def raw_spend(project_path, wo_id: str) -> dict[str, int]:
    """Every token recorded against one work order, summed straight out of storage.

    Deliberately naive and deliberately duplicated from nothing: two readings of the
    same rows that agree are evidence, and a helper shared with the implementation
    would be one reading twice.
    """
    totals = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    store = ProjectStore(project_path)
    try:
        for turn in store.list_turns(wo_id):
            envelope = db.from_json(turn.get("usage_json"), None)
            if not envelope:
                continue
            for cls in totals:
                totals[cls] += envelope.get(cls) or 0
    finally:
        store.close()
    central = CentralStore()
    try:
        for row in central.agent_calls(wo_id=wo_id, limit=10_000):
            for cls in totals:
                totals[cls] += row[cls] or 0
    finally:
        central.close()
    return totals


def bill_tokens(b: dict) -> dict[str, int]:
    return {cls: b["total"]["tokens"][cls]
            for cls in ("input", "cache_write", "cache_read", "output")}


def os_call(wo_id: str, kind: str, label: str = "", **over) -> None:
    agent_usage.record(kind, project="proj_a", wo_id=wo_id, label=label,
                       model="claude-fake-1", question_id=over.pop("question_id", None),
                       usage={"total_cost_usd": 0.02, "input": 7, "cache_write": 900,
                              "cache_read": 4_000, "output": 250, **over})


@pytest.fixture()
def fleet(jarvis_home, project, fake_claude, catalog_file):
    """The OS started over one project, with a daemon to drive dispatch by hand.

    The real path on purpose: this eval is about what the OS actually records, so a
    turn has to be dispatched, run by the fake CLI and reaped the way a live one is.
    """
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    return {"path": project, "daemon": Daemon(load_catalog(catalog_file))}


def dispatched(fleet, settle_turns, title: str = "an order", turns: int = 1):
    """A work order taken through the real dispatch path, with `turns` settled turns."""
    wo = ops.create_work_order("proj_a", title)
    store = ProjectStore(fleet["path"])
    try:
        fleet["daemon"].tick()
        assert settle_turns(store)
        for i in range(turns - 1):
            ops.send_message(wo["id"], f"more work {i}")
            fleet["daemon"].tick()
            assert settle_turns(store)
    finally:
        store.close()
    return wo


# -- the scenarios ---------------------------------------------------------------------


@scenario("bill accounting", "the bill totals every token in the stores")
def test_the_bill_equals_the_raw_records(fleet, settle_turns):
    """The headline claim: nothing recorded is left off the bill.

    All three classes of spend on one order, because the failure that motivated this is
    a whole class going missing rather than a rounding difference.
    """
    wo = dispatched(fleet, settle_turns, "an order that asked for help", turns=3)
    os_call(wo["id"], "neo_answer", "question", question_id=1)
    for seat in ("premise", "record", "blast", "taste", "chair"):
        os_call(wo["id"], "panel_seat", seat, question_id=1)
    os_call(wo["id"], agent_usage.WORKER_SUBPROCESS, "pytest")

    b = ops.bill(wo["id"])

    assert bill_tokens(b) == raw_spend(fleet["path"], wo["id"])
    assert b["checks"]["balanced"], b["checks"]["problems"]


@scenario("bill accounting", "a turn's tokens are the whole turn, not its tail")
def test_the_recorded_turn_is_the_model_usage_total(fleet, settle_turns):
    """The correction this feature turned on, end to end through the real capture.

    The fake CLI reports `usage` as the tail of the turn and `modelUsage` as the whole
    of it, in the proportion the real one does. A turn recorded from the wrong object
    understates by roughly two thirds — and the fake is built so that this test is the
    thing that notices.
    """
    wo = dispatched(fleet, settle_turns, "one turn")

    b = ops.bill(wo["id"])
    tokens = b["total"]["tokens"]

    assert (tokens["input"], tokens["cache_write"], tokens["cache_read"],
            tokens["output"]) == (9, 3_000, 6_000, 300)
    worker = next(line for line in b["actors"] if line["key"] == "worker")
    assert worker["usage_versions"] == [2]


@scenario("bill accounting", "every charge lands on exactly one turn")
def test_the_turn_view_accounts_for_everything(fleet, settle_turns):
    wo = dispatched(fleet, settle_turns, "three turns", turns=3)
    os_call(wo["id"], "neo_answer", "question", question_id=1)
    os_call(wo["id"], agent_usage.WORKER_SUBPROCESS, "pytest")

    b = ops.bill(wo["id"])

    per_turn = sum(line["tokens"]["total"] for line in b["turns"])
    assert per_turn == b["total"]["tokens"]["total"]
    assert sum(line["calls"] for line in b["turns"]) == b["total"]["calls"]


@scenario("bill accounting", "a feature order totals its children")
def test_a_feature_orders_bill_is_its_orders(fleet, settle_turns):
    store = ProjectStore(fleet["path"])
    try:
        fo = store.create_feature_order("a feature", "")
        children = [store.create_work_order(f"child {i}", "", parent_id=fo["id"])
                    for i in range(3)]
        store.conn.commit()
    finally:
        store.close()
    store = ProjectStore(fleet["path"])
    try:
        fleet["daemon"].tick()
        assert settle_turns(store)
    finally:
        store.close()
    os_call(children[0]["id"], "neo_answer", "question", question_id=1)

    b = ops.bill(fo["id"])

    assert b["checks"]["balanced"], b["checks"]["problems"]
    assert sum(o["total"]["tokens"]["total"] for o in b["orders"]) \
        == b["total"]["tokens"]["total"]
    # THE PLANNER COUNTS. The daemon plans a pending feature order itself, and that
    # session is usually the dearest thing an unfinished feature has spent — a bill
    # that listed only the children would understate exactly the feature someone is
    # asking about.
    store = ProjectStore(fleet["path"])
    try:
        planner = store.get_feature_order(fo["id"])["plan_wo_id"]
    finally:
        store.close()
    assert planner and planner in {o["id"] for o in b["orders"]}
    expected = {wo_id: raw_spend(fleet["path"], wo_id)["output"]
                for wo_id in [planner, *(c["id"] for c in children)]}
    assert {o["id"]: o["total"]["tokens"]["output"] for o in b["orders"]} == expected


@scenario("bill accounting", "spend survives a pruned transcript")
def test_an_order_with_no_transcript_still_bills_what_was_recorded(fleet,
                                                                  settle_turns):
    """Recorded turns and `agent_calls` are the OS's own records and outlive the
    transcript Claude Code prunes on its own schedule."""
    wo = dispatched(fleet, settle_turns, "pruned but not free", turns=2)
    os_call(wo["id"], "neo_answer", "question", question_id=1)

    b = ops.bill(wo["id"])

    assert not b["session_found"]          # no transcript in the isolated world
    assert bill_tokens(b) == raw_spend(fleet["path"], wo["id"])
    assert b["total"]["tokens"]["total"] > 0
    assert b["checks"]["balanced"], b["checks"]["problems"]


@scenario("bill accounting", "an order that spent nothing says so")
def test_an_undispatched_order_is_empty_not_wrong(fleet):
    """Zero is a real answer here, and it must still balance — an empty bill that
    reported a phantom line would be the same failure in the other direction."""
    wo = ops.create_work_order("proj_a", "never dispatched")

    b = ops.bill(wo["id"])

    assert b["total"]["tokens"]["total"] == 0
    assert b["actors"] == [] and b["turns"] == []
    assert b["checks"]["balanced"]


@scenario("bill accounting", "the CLI and the dashboard bill the same order alike")
def test_one_payload_behind_both_surfaces(fleet, settle_turns, capsys):
    """Two renderers, one payload. The moment they compute their own totals, a user
    reading the terminal and a user reading the page are told different things."""
    from jarvis import cli

    wo = dispatched(fleet, settle_turns, "rendered twice", turns=2)
    os_call(wo["id"], "neo_answer", "question", question_id=1)
    b = ops.bill(wo["id"])
    capsys.readouterr()

    assert cli.main(["cost", wo["id"], "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)

    assert printed["total"]["tokens"] == b["total"]["tokens"]
    assert printed["checks"]["balanced"]
