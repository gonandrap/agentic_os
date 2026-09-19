"""Money the budget accounting cannot see.

`budget.spent` is `SUM(wo_turns.cost_usd)`, and that column is written exactly once — by
`ProjectStore.finish_turn`, at reap. Nothing writes it while a turn runs, and nothing
repairs it afterwards. Three consequences, none of them asserted in `test_budget.py`,
because every spend in that file is written by its `bill_the_turn` helper — which opens a
turn and settles it in the same breath. No test in the suite has ever asked the accounting
a question while a turn row was `running`, or after one was reaped without an envelope.

Observed on wo-2d259874 (production, jarvis-0.10.10): the work-order page read
"~$3.25 · 3.6M tokens" and, on the line directly below it, "Budget: $0.00 of $30.00 ·
$30.00 left". Both numbers were current. They disagree because they have different
sources — the first is `bill.for_work_order`, which walks the live transcript and sees a
turn in flight; the second is `budget.spent`, which cannot.
"""

from __future__ import annotations

import json

import pytest

from jarvis import budget, ops
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    from jarvis.catalog import load_catalog

    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def test_a_turn_in_flight_is_invisible_to_the_budget(started, store):
    """A turn that has been burning money for an hour is billed $0.00 until it is reaped.

    `Spend`'s docstring calls this lag safe — "too generous by one turn, never too mean".
    A turn is unbounded, so "one turn" is not a bound, and the number the user is shown
    while it runs is not the number they typed minus what they have spent.
    """
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=30.0)
    store.create_turn(wo["id"], kind="dispatch", prompt="work")  # running, never reaped

    cap = budget.ceiling(store, None, store.get_work_order(wo["id"]))
    assert cap is not None
    assert store.list_turns(wo["id"])[0]["state"] == "running"
    assert cap.spent_usd > 0.0, (
        "an order whose only turn is in flight reports its whole budget as unspent")


def test_a_turn_reaped_without_an_envelope_is_billed_nothing_for_ever(started, store):
    """The permanent half. `worker_session._reap`'s no-result path calls
    `finish_turn(..., "failed")` with no `cost_usd`, so a turn killed by a reboot, an OOM
    or a daemon restart has its whole spend written off — and `finish_turn` SETs the
    column, so NULL is recorded rather than left. `jarvis cost` still finds the money in
    the transcript; the budget never does.
    """
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=5.0)
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="work")
    store.finish_turn(turn["id"], "failed", error="no result in the outfile")

    assert store.get_turn(turn["id"])["cost_usd"] is None
    assert budget.spent(store, None, wo["id"]).total_usd > 0, (
        "a turn that ran long enough to be killed spent money; the budget records none")


def test_the_late_usage_repair_does_not_repair_the_cost(started, store):
    """...and nothing puts it back. `ops.turn_usage` re-reads a settled turn's outfile to
    backfill `usage_json`, but `set_turn_usage` writes that column alone. A row can
    therefore say it cost $4.19 and be summed as $0.00 by the one query the enforcement
    runs on every dispatch.
    """
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=5.0)
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="work")
    store.finish_turn(turn["id"], "failed", error="no result in the outfile")
    store.set_turn_usage(turn["id"], json.dumps({"total_cost_usd": 4.19}))

    assert json.loads(store.get_turn(turn["id"])["usage_json"])["total_cost_usd"] == 4.19
    assert budget.spent(store, None, wo["id"]).total_usd == pytest.approx(4.19), (
        "the row knows what the turn cost; `budget.spent` reads the other column")
