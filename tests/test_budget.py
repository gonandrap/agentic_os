"""A dollar ceiling per order, enforced on every `claude` call.

Four things are pinned here, and the first is the one the whole feature rests on.

**The flag is per INVOCATION.** `claude --max-budget-usd` caps one process and forgets
what the session spent before it (measured 2026-09-18; the probe is written up at the top
of src/jarvis/budget.py). So the value Jarvis passes has to be `budget - spent so far`,
recomputed on every single turn — a value resolved once at dispatch would let a ten-turn
work order spend ten budgets. `test_each_turn_is_capped_at_what_is_left` is that claim.

**The budget governs the WHOLE bill**, worker turns plus what Jarvis spent on the order
(Neo, the panel). Ruled by the user through Neo on question 404: the number they typed has
to be the number `jarvis cost` shows them.

**A feature's budget is a family budget, allocated by RESERVE-ON-DISPATCH.** Two children
dispatched in parallel must not be handed the same remainder. The invariant is that the
allocator never PROMISES more than the feature's unreserved remainder, and it is asserted
directly rather than through a proxy. The stronger-sounding
`spent + everything live children may still spend <= the budget` is deliberately not
claimed: the flag overshoots, so a child can put the family over before any allocation
happens. See `test_a_re_cut_never_promises_money_the_family_does_not_have`.

**Default off.** An order with no budget behaves exactly as it did before this shipped —
no flag on the argv, and the new status unreachable. That is the definition of done's
second half, and it is checked on the same paths as the first.
"""

from __future__ import annotations

import json
import time

import pytest

from jarvis import budget, ops
from jarvis.catalog import CatalogError, parse_catalog
from jarvis.claude_cli import turn_args
from jarvis.daemon import Daemon
from jarvis.project_store import (
    FO_OPEN_STATUSES,
    FO_STATUSES,
    NOT_RETRIED,
    OPEN_STATUSES,
    RETRY_SWEEP_STATUSES,
    TERMINAL_STATUSES,
    WO_STATUSES,
    ProjectStore,
)
from jarvis.testing import FIXTURE_DESIGN_DOC, fixture_spec_section


ASK = ("Add a CSV exporter to the reporting module, with a command that calls it and "
       "tests over both the happy path and an empty result set.")


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


def tick_until_parked(daemon, store, wo_id: str, timeout: float = 15.0) -> str:
    """Tick until the order lands in `budget_exhausted`, and say where it actually got to.

    A worker turn is a DETACHED process, so `wait_calls` only proves the turn was
    launched — the settler cannot park the order until that process has written its result
    and been reaped. A fixed two ticks is therefore a race, and the way it loses is
    peculiarly nasty: the top-up under test then runs against an order that is not parked,
    `resumed` is False for a reason that has nothing to do with the code under test, and it
    only happens on a loaded machine.
    """
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        daemon.tick()
        status = store.get_work_order(wo_id)["status"]
        if status == budget.EXHAUSTED:
            return status
        time.sleep(0.05)
    return status


def tick_until_reserved(daemon, store, fo_id: str, timeout: float = 15.0) -> dict:
    """Tick until the feature's first child has been dispatched and handed its slice.

    The other half of `tick_until_parked`'s race, and it loses the same way on a loaded
    machine: a fixed count of ticks has either not dispatched the child yet — no slice,
    so `budget.ceiling` is None and nothing can be exhausted — or has already reaped its
    worker and settled the order, and `settle_work_orders` never looks at a settled one
    again. Either way the billing below lands on an order the budget cannot park, which
    is what CI saw twice on a 3.11 runner that took 34 minutes.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        daemon.tick()
        cut = [c for c in store.feature_children(fo_id) if c["budget_reserved_usd"]]
        if cut:
            return cut[0]
        time.sleep(0.05)
    raise AssertionError(f"no child of {fo_id} was handed a slice within {timeout}s")


def budget_flag(call: dict) -> str | None:
    """`--max-budget-usd`'s value in one recorded invocation, or None if absent."""
    argv = call["argv"]
    return argv[argv.index("--max-budget-usd") + 1] if "--max-budget-usd" in argv else None


def worker_calls(fake_claude) -> list[dict]:
    """Every recorded WORKER TURN, oldest first — never a Neo or panel call."""
    return [c for c in fake_claude.calls
            if "-p" in c["argv"] and ("--session-id" in c["argv"]
                                      or "--resume" in c["argv"])]


def bill_the_turn(store: ProjectStore, wo_id: str, usd: float) -> None:
    """Charge a settled turn `usd`, the way a reaped turn's envelope would.

    Writes the column `budget.spent` sums rather than going through a fake turn, so a
    test about the ARITHMETIC of the ceiling is not also a test of the transport.
    """
    turn = store.create_turn(wo_id, kind="message", prompt="work")
    store.finish_turn(turn["id"], "done", result="done", cost_usd=usd,
                      usage_json=json.dumps({"total_cost_usd": usd}))


# -- the flag, and what the CLI does with it ------------------------------------------


def test_the_flag_is_absent_when_no_budget_is_set():
    """DEFAULT OFF, at the argv. An OS that starts refusing to work because of a number
    nobody set would be worse than the problem, so an unbudgeted order's command line is
    byte-for-byte what it was before this shipped."""
    assert "--max-budget-usd" not in turn_args("go", "sid", resume=False)


def test_the_flag_carries_the_remaining_budget():
    args = turn_args("go", "sid", resume=False, max_budget_usd=4.25)
    assert args[args.index("--max-budget-usd") + 1] == "4.250000"


def test_a_tiny_remainder_never_reaches_argv_in_scientific_notation():
    """`str(1e-05)` is "1e-05", which the CLI's parser does not accept — and a remainder
    that small is exactly what the last turn of a spent budget is handed."""
    args = turn_args("go", "sid", resume=False, max_budget_usd=0.00001)
    assert args[args.index("--max-budget-usd") + 1] == "0.000010"


def test_the_flag_is_not_in_the_shared_briefing():
    """`--max-budget-usd` only works with `--print`. `_briefing_args` is shared with
    `spawn_background`, which has no `-p`, so a flag placed there would be accepted and
    silently ignored — capping nothing while reading as capped."""
    from jarvis.claude_cli import _briefing_args

    assert "--max-budget-usd" not in _briefing_args(model="sonnet", effort="high")


def test_the_cli_refusal_is_read_off_the_structured_fields():
    """Never off the prose: this repo's own workers write about budget exhaustion, and a
    text match would let a worker quoting the error park its own work order."""
    from jarvis.claude_cli import TurnResult, stopped_for_budget

    assert stopped_for_budget(TurnResult(ok=False, terminal_reason="budget_exhausted"))
    assert stopped_for_budget(TurnResult(ok=False, subtype="error_max_budget_usd"))
    assert not stopped_for_budget(TurnResult(ok=False, terminal_reason="api_error"))
    assert not stopped_for_budget(
        TurnResult(ok=False, result="Reached maximum budget ($5.00)"))


def test_a_budget_stop_reports_what_the_envelope_said(tmp_path):
    """The real CLI writes NO `result` field when it stops for the budget, so without the
    `errors` fallback the record would read "turn reported is_error" about a turn whose
    own envelope said why."""
    from jarvis.claude_cli import read_turn_result

    out = tmp_path / "t.json"
    out.write_text(json.dumps({
        "type": "result", "subtype": "error_max_budget_usd", "is_error": True,
        "terminal_reason": "budget_exhausted", "total_cost_usd": 0.047,
        "errors": ["Reached maximum budget ($0.001)"],
    }))
    result = read_turn_result(out)
    assert result is not None
    assert "Reached maximum budget" in result.error
    # And the spend is still recorded: the turn paid for everything up to the refusal.
    assert result.cost_usd == 0.047


# -- the arithmetic -------------------------------------------------------------------


def test_the_ceiling_counts_both_halves_of_the_bill(started, store):
    """The user's $20 is the $20 `jarvis cost` shows: the worker's turns AND what Jarvis
    spent on the order. Neo, question 404."""
    from jarvis.central_store import CentralStore

    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=10.0)
    bill_the_turn(store, wo["id"], 3.0)
    central = CentralStore()
    try:
        central.add_agent_call("neo", label="question", model="sonnet",
                                  project="proj_a", wo_id=wo["id"], ok=True,
                                  usage={"total_cost_usd": 1.0})
        spend = budget.spent(store, central, wo["id"])
        cap = budget.ceiling(store, central, store.get_work_order(wo["id"]))
    finally:
        central.close()
    assert spend.worker_usd == 3.0
    assert spend.jarvis_usd == 1.0
    assert cap is not None
    assert cap.remaining_usd == 6.0


def test_a_ceiling_re_reads_turns_counted_as_the_whole_session(started, store,
                                                               tmp_path):
    """A live order's remaining budget cannot be computed from the old reading.

    `wo_turns.cost_usd` held each turn's `total_cost_usd`, and from CLI 2.1.277 that is
    the resumed session's running bill — so a three-turn order looked to have spent
    $2.49 + $16.90 + $27.10 when it had spent $27.10, and stopped at a third of the
    ceiling its user set (issue #470). The rows are re-derived from the result JSONs
    before they are summed, which is the only way an order already stopped on a phantom
    overrun starts running again.
    """
    from tests.test_turn_usage import running, spend, turn_file

    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=10.0)
    turns = [spend(0, 2_000, 1_000, 100), spend(0, 500, 4_000, 60)]
    for i, (own, cum, cost) in enumerate(zip(turns, running(turns), [2.0, 6.5]),
                                         start=1):
        out = turn_file(tmp_path, i, own=own, cumulative=cum, cost=cost)
        turn = store.create_turn(wo["id"], kind="message", prompt="work")
        store.conn.execute("UPDATE wo_turns SET outfile=? WHERE id=?",
                           (str(out), turn["id"]))
        # Recorded the way the release before this one did: the session's running bill.
        store.finish_turn(turn["id"], "done", result="done", cost_usd=cost,
                          usage_json=json.dumps({"usage_v": 2, "total_cost_usd": cost}))

    assert budget.spent(store, None, wo["id"]).worker_usd == pytest.approx(6.5)
    cap = budget.ceiling(store, None, store.get_work_order(wo["id"]))
    assert cap is not None and cap.remaining_usd == pytest.approx(3.5)


def test_a_cap_is_not_tripped_by_the_session_running_total(started, store, tmp_path):
    """Recorded live, turn by turn, the way a running order records them.

    wo-966987af's real dollars under the $50 standing ceiling jarvis_os now carries.
    Summing `total_cost_usd` as the envelope reports it gives $2.28 + $25.60 + $28.88 +
    $29.87 + $32.02 = $118.65 and stops the order at turn 4, having really spent
    $29.87 of its $50. The conversation cost $32.02 and must run to the end.
    """
    from jarvis import worker_session
    from tests.test_turn_usage import running, spend, turn_file

    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=50.0)
    turns = [spend(0, 0, own, 0)
             for own in (2_170_000, 39_120_000, 4_120_000, 790_000, 2_960_000)]
    costs = [2.28, 25.60, 28.88, 29.87, 32.02]
    for i, (own, cum, cost) in enumerate(zip(turns, running(turns), costs), start=1):
        turn = store.create_turn(wo["id"], kind="dispatch", prompt="go")
        out = turn_file(tmp_path, i, own=own, cumulative=cum, cost=cost)
        store.conn.execute("UPDATE wo_turns SET outfile=?, started_at=? WHERE id=?",
                           (str(out), time.time() - 60, turn["id"]))
        worker_session.poll(store)
        cap = budget.ceiling(store, None, store.get_work_order(wo["id"]))
        assert cap is not None and cap.remaining_usd > 0, f"cut short at turn {i}"

    assert budget.spent(store, None, wo["id"]).worker_usd == pytest.approx(32.02)
    cap = budget.ceiling(store, None, store.get_work_order(wo["id"]))
    assert cap is not None and cap.remaining_usd == pytest.approx(17.98)


def test_no_budget_means_no_ceiling(started, store):
    wo = ops.create_work_order("proj_a", "uncapped", description="do it")
    assert store.get_work_order(wo["id"])["budget_usd"] is None
    assert budget.ceiling(store, None, store.get_work_order(wo["id"])) is None


def test_the_tighter_of_the_two_caps_wins(started, store):
    """An order can carry its own budget AND a slice its feature reserved for it. They
    are not added — neither claim entitles the order to the other's dollars."""
    wo = ops.create_work_order("proj_a", "child", description="do it", budget_usd=10.0)
    store.update_work_order(wo["id"], budget_reserved_usd=4.0)
    cap = budget.ceiling(store, None, store.get_work_order(wo["id"]))
    assert cap is not None
    assert (cap.cap_usd, cap.source) == (4.0, "feature")

    store.update_work_order(wo["id"], budget_reserved_usd=40.0)
    cap = budget.ceiling(store, None, store.get_work_order(wo["id"]))
    assert cap is not None
    assert (cap.cap_usd, cap.source) == (10.0, "work_order")


def test_exhaustion_is_answered_from_the_accounting_not_the_exit_code(started, store):
    """The budget governs the whole bill, so a run of Neo answers can carry an order past
    its cap with no worker turn having failed — or even run. A check that keyed on
    `terminal_reason` would miss every one of those."""
    from jarvis.central_store import CentralStore

    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=1.0)
    central = CentralStore()
    try:
        central.add_agent_call("panel", label="tester", model="sonnet",
                                  project="proj_a", wo_id=wo["id"], ok=True,
                                  usage={"total_cost_usd": 1.5})
        out = budget.exhaustion(store, central, store.get_work_order(wo["id"]))
    finally:
        central.close()
    assert out is not None
    assert store.list_turns(wo["id"]) == []  # no turn ever ran, let alone failed
    assert "$1.50" in out.reason and "$1.00" in out.reason


# -- end to end: a capped order stops, and says what it spent -------------------------


def test_a_budgeted_order_stops_at_its_budget_and_says_what_it_spent(
        started, store, fake_claude, monkeypatch):
    """THE DEFINITION OF DONE, first half."""
    monkeypatch.setenv("FAKE_CLAUDE_TURN", "budget")
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=0.50)
    started.tick()
    fake_claude.wait_calls(lambda c: budget_flag(c) is not None)
    assert tick_until_parked(started, store, wo["id"]) == "budget_exhausted"

    row = store.get_work_order(wo["id"])
    assert row["needs_attention"]
    # What it spent, not what the cap was: the CLI overshoots by the call that crossed
    # the line, and a reason quoting the cap would understate the bill.
    assert "$0.55" in row["attention_reason"]
    assert "$0.50" in row["attention_reason"]
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert "turn_budget_exhausted" in kinds
    assert "budget_exhausted" in kinds


def test_an_unbudgeted_order_behaves_exactly_as_before(started, store, fake_claude):
    """THE DEFINITION OF DONE, second half — checked on the same path as the first."""
    wo = ops.create_work_order("proj_a", "uncapped", description="do it")
    started.tick()
    fake_claude.wait_calls(lambda c: "--session-id" in c["argv"])
    started.tick()

    assert budget_flag(worker_calls(fake_claude)[0]) is None
    assert store.get_work_order(wo["id"])["status"] != "budget_exhausted"


def test_each_turn_is_capped_at_what_is_left(started, store, fake_claude):
    """THE CLAIM THE WHOLE DESIGN RESTS ON. `--max-budget-usd` is per INVOCATION — a
    resumed session's earlier spend does not count against it — so a value resolved once
    at dispatch would let a ten-turn work order spend ten budgets. Every turn must be
    handed a SMALLER number than the last."""
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=5.0)
    started.tick()
    fake_claude.wait_calls(lambda c: budget_flag(c) is not None)
    started.tick()

    ops.send_message(wo["id"], "keep going")
    started.tick()
    fake_claude.wait_calls(lambda c: "--resume" in c["argv"])
    started.tick()

    caps = [float(f) for f in (budget_flag(c) for c in worker_calls(fake_claude))
            if f is not None]
    assert len(caps) >= 2, caps
    assert caps[0] == 5.0            # nothing spent yet
    assert caps[1] < caps[0]         # ...and the first turn's $0.01 came off the second
    assert caps[1] == pytest.approx(5.0 - 0.01)


def test_a_spent_order_is_never_relaunched_by_the_retry_sweep():
    """Relaunching would spend money nobody authorised, and the sweep cannot ask.

    Asserted against the partition rather than the sweep's body: the two tuples cover
    `WO_STATUSES` between them, so this is the decision, not a symptom of it.
    """
    assert "budget_exhausted" in NOT_RETRIED
    assert "budget_exhausted" not in RETRY_SWEEP_STATUSES


def test_the_new_state_is_open_and_not_terminal():
    """Terminal would make it a DEPENDENCY_DEAD_STATUS: every dependent stranded and the
    parent feature failed, over a number the user can change in one command. It is also
    what makes resuming possible at all."""
    assert "budget_exhausted" in WO_STATUSES
    assert "budget_exhausted" in OPEN_STATUSES
    assert "budget_exhausted" not in TERMINAL_STATUSES
    assert "budget_exhausted" in FO_STATUSES
    assert "budget_exhausted" in FO_OPEN_STATUSES


def test_the_reason_survives_a_reconcile_tick(started, store, fake_claude, monkeypatch):
    """`invariants.true_blockers` re-derives every attention reason on every tick, so a
    line only `budget.escalate` knew how to write would be overwritten by a generic one.
    Ticking twice more is the test."""
    monkeypatch.setenv("FAKE_CLAUDE_TURN", "budget")
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=0.50)
    started.tick()
    fake_claude.wait_calls(lambda c: budget_flag(c) is not None)
    assert tick_until_parked(started, store, wo["id"]) == "budget_exhausted"
    first = store.get_work_order(wo["id"])["attention_reason"]

    started.tick()
    started.tick()
    assert store.get_work_order(wo["id"])["attention_reason"] == first
    assert store.get_work_order(wo["id"])["needs_attention"]


def test_a_queued_message_waits_rather_than_launching_an_unfunded_turn(
        started, store, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_TURN", "budget")
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=0.50)
    started.tick()
    fake_claude.wait_calls(lambda c: budget_flag(c) is not None)
    assert tick_until_parked(started, store, wo["id"]) == "budget_exhausted"

    before = len(worker_calls(fake_claude))
    ops.send_message(wo["id"], "carry on")
    started.tick()
    assert len(worker_calls(fake_claude)) == before
    assert [m["status"] for m in store.list_messages(wo["id"])] == ["queued"]


# -- raising the budget and carrying on ------------------------------------------------


def test_raising_the_budget_resumes_the_same_session(
        started, store, fake_claude, monkeypatch):
    """Partial work is preserved and the transcript is resumable (measured), so a top-up
    continues the conversation rather than restarting the work."""
    monkeypatch.setenv("FAKE_CLAUDE_TURN", "budget")
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=0.50)
    started.tick()
    fake_claude.wait_calls(lambda c: budget_flag(c) is not None)
    assert tick_until_parked(started, store, wo["id"]) == "budget_exhausted"
    session = store.get_work_order(wo["id"])["session_id"]

    monkeypatch.delenv("FAKE_CLAUDE_TURN")
    out = ops.set_work_order_budget(wo["id"], 20.0)
    assert out["resumed"], out
    assert store.get_work_order(wo["id"])["status"] != "budget_exhausted"

    # THE SAME SESSION ID, which is the whole claim: the stopped turn left a complete
    # transcript, so the top-up continues that conversation instead of opening a new one
    # and throwing away everything the worker had already done.
    #
    # Asserted on the ID rather than on `--resume` because `worker_session.retry`
    # re-decides that flag from the filesystem — a session whose transcript was never
    # written cannot be resumed and has to be re-opened with `--session-id`, which is the
    # branch the fake takes. Either flag preserves the id; only the id is the property.
    #
    # Read off the list `wait_calls` RETURNS rather than off `calls[-1]`: the relaunch is
    # detached, `wait_calls` gives up quietly on timeout, and indexing the log afterwards
    # would then assert against the turn BEFORE the one under test — a failure that only
    # appears when the machine is loaded, which is exactly when nobody can reproduce it.
    # The relaunch's cap is the REMAINDER, 20 less the 0.55 already spent — not the 20 the
    # user typed. Matching on "20.000000" would never fire, and the old spelling only
    # looked green because `wait_calls` times out quietly and the assertion below then ran
    # against whatever call happened to be last.
    sent = fake_claude.wait_calls(
        lambda c: budget_flag(c) is not None and 0 < float(budget_flag(c) or 0) < 20)
    assert sent, "the top-up never launched a turn"
    argv = sent[-1]["argv"]
    flag = "--resume" if "--resume" in argv else "--session-id"
    assert argv[argv.index(flag) + 1] == session


def test_raising_it_by_too_little_does_not_launch_a_doomed_turn(
        started, store, fake_claude, monkeypatch):
    """Setting $0.52 on an order that has already spent $0.55 must leave it where it is
    — the CLI would stop the turn on its first call and charge for the privilege."""
    monkeypatch.setenv("FAKE_CLAUDE_TURN", "budget")
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=0.50)
    started.tick()
    fake_claude.wait_calls(lambda c: budget_flag(c) is not None)
    assert tick_until_parked(started, store, wo["id"]) == "budget_exhausted"

    before = len(worker_calls(fake_claude))
    out = ops.set_work_order_budget(wo["id"], 0.52)
    assert not out["resumed"]
    assert "still over its ceiling" in out["note"]
    assert store.get_work_order(wo["id"])["status"] == "budget_exhausted"
    assert len(worker_calls(fake_claude)) == before


def test_clearing_the_budget_resumes_it_too(started, store, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_TURN", "budget")
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=0.50)
    started.tick()
    fake_claude.wait_calls(lambda c: budget_flag(c) is not None)
    assert tick_until_parked(started, store, wo["id"]) == "budget_exhausted"

    monkeypatch.delenv("FAKE_CLAUDE_TURN")
    session = store.get_work_order(wo["id"])["session_id"]
    out = ops.set_work_order_budget(wo["id"], None)
    assert out["resumed"]
    assert store.get_work_order(wo["id"])["budget_usd"] is None
    # No ceiling means no flag, on the relaunch as much as on a first dispatch. Matched on
    # the ABSENCE of the flag for this session, which only the relaunch can satisfy — the
    # first dispatch carried one — and read off what `wait_calls` returns, so a loaded
    # machine fails here rather than silently asserting against the earlier turn.
    sent = fake_claude.wait_calls(
        lambda c: session in c["argv"] and budget_flag(c) is None)
    assert sent, "the clear never launched an uncapped turn"


# -- the family budget -----------------------------------------------------------------


def a_feature(daemon, store, *keys: str, budget_usd: float | None = None,
              max_parallel: int | None = None) -> dict:
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK,
                                  budget_usd=budget_usd, max_parallel=max_parallel)
    daemon.tick()
    ops.submit_plan(fo["id"], {
        "summary": "an exporter FORCE_APPROVE",
        "design_doc": FIXTURE_DESIGN_DOC,
        "children": [{
            "key": k, "title": f"Build {k}",
            "description": (f"Build the {k} part of the exporter: add the module, wire "
                            f"it into the command that calls it, and cover both paths "
                            f"with tests. FORCE_APPROVE"),
            "needs": [], "spec_section": fixture_spec_section(k),
        } for k in keys],
    })
    daemon._neo_drain()
    return store.get_feature_order(fo["id"])


def test_two_parallel_children_are_never_handed_the_same_remainder(
        started, store, fake_claude):
    """THE HAZARD THE WORK ORDER CALLED OUT, and the reason allocation is
    reserve-on-dispatch rather than re-read-per-turn. Re-reading is always TRUE and still
    lets N live children each spend the whole remainder once."""
    fo = a_feature(started, store, "reader", "writer", budget_usd=12.0)
    for _ in range(4):
        started.tick()

    children = store.feature_children(fo["id"])
    reserved = [c["budget_reserved_usd"] for c in children
                if c["budget_reserved_usd"] is not None]
    assert len(reserved) == 2, [dict(c) for c in children]
    assert sum(reserved) <= 12.0
    # Not the same dollars: neither slice is the whole remainder.
    assert all(r < 12.0 for r in reserved)


def test_the_family_never_promises_more_than_it_has(started, store, fake_claude):
    """THE INVARIANT, asserted directly rather than through a proxy:

        already spent + everything live children may still spend <= the budget
    """
    from jarvis.central_store import CentralStore

    fo = a_feature(started, store, "reader", "writer", "docs", budget_usd=12.0)
    for _ in range(5):
        started.tick()

    central = CentralStore()
    try:
        pool = budget.pool(store, central, store.get_feature_order(fo["id"]))
        assert pool is not None
        committed = pool.spent_usd + pool.held_usd
        assert committed <= pool.budget_usd + 1e-9, (pool.spent_usd, pool.held_usd)
        assert pool.unreserved_usd == pytest.approx(pool.budget_usd - committed)
    finally:
        central.close()


def test_a_settled_child_releases_the_rest_of_its_slice(started, store):
    """Release needs no write: a settled child drops out of `held`, and its real spend is
    already inside the pool's total. This is what makes an equal split self-correcting —
    a cheap child leaves more for the next one."""
    from jarvis.central_store import CentralStore

    fo = a_feature(started, store, "reader", "writer", budget_usd=12.0)
    for _ in range(4):
        started.tick()
    children = store.feature_children(fo["id"])

    central = CentralStore()
    try:
        before = budget.pool(store, central, store.get_feature_order(fo["id"]))
        bill_the_turn(store, children[0]["id"], 1.0)
        store.set_status(children[0]["id"], "completed")
        after = budget.pool(store, central, store.get_feature_order(fo["id"]))
    finally:
        central.close()
    assert before is not None and after is not None
    assert after.held_usd < before.held_usd
    assert after.unreserved_usd > before.unreserved_usd


def test_a_child_that_runs_out_is_told_what_its_feature_still_has(started, store):
    """NEO'S CONDITION ON RESERVE-ON-DISPATCH (question 404): a child can exhaust its
    slice while the feature is still funded, and without this the user cannot tell a
    stalled feature from one they only need to top up."""
    from jarvis.central_store import CentralStore

    fo = a_feature(started, store, "reader", "writer", budget_usd=12.0)
    for _ in range(4):
        started.tick()
    child = store.feature_children(fo["id"])[0]
    bill_the_turn(store, child["id"], (child["budget_reserved_usd"] or 0) + 1)

    central = CentralStore()
    try:
        out = budget.exhaustion(store, central, store.get_work_order(child["id"]))
    finally:
        central.close()
    assert out is not None
    assert out.pool is not None
    assert "unreserved" in out.reason
    assert "its feature's slice" in out.reason


def test_a_feature_stops_when_the_whole_family_has_spent_its_budget(started, store):
    fo = a_feature(started, store, "reader", "writer", budget_usd=12.0)
    for _ in range(4):
        started.tick()
    for child in store.feature_children(fo["id"]):
        bill_the_turn(store, child["id"], 9.0)
    started.tick()

    fresh = store.get_feature_order(fo["id"])
    assert fresh["status"] == "budget_exhausted"
    assert fresh["needs_attention"]
    assert "$12.00" in fresh["attention_reason"]


def test_a_feature_with_no_budget_reserves_nothing(started, store):
    """DEFAULT OFF one level up: a feature without a budget lends its children nothing
    and they run uncapped, exactly as every feature did before this shipped."""
    fo = a_feature(started, store, "reader", "writer")
    for _ in range(4):
        started.tick()
    assert all(c["budget_reserved_usd"] is None
               for c in store.feature_children(fo["id"]))
    assert budget.pool(store, None, store.get_feature_order(fo["id"])) is None


def test_topping_up_a_feature_puts_it_back_to_executing(started, store):
    fo = a_feature(started, store, "reader", budget_usd=12.0)
    for _ in range(4):
        started.tick()
    for child in store.feature_children(fo["id"]):
        bill_the_turn(store, child["id"], 20.0)
    started.tick()
    assert store.get_feature_order(fo["id"])["status"] == "budget_exhausted"

    ops.set_feature_budget(fo["id"], 100.0)
    started.tick()
    assert store.get_feature_order(fo["id"])["status"] == "executing"


# -- the surfaces ----------------------------------------------------------------------


def test_a_project_default_is_stamped_onto_new_orders(started, store, monkeypatch,
                                                      catalog_file):
    """Stamped at CREATION, unlike the autocompact window, which is re-read every turn.
    A budget is a contract about ONE order: `jarvis wo show` has to be able to state it,
    and lowering a fleet default must not strand work already authorised at the old
    number."""
    data = json.loads(catalog_file.read_text())
    data["projects"][0]["worker"] = {"budget_usd": 7.5, "feature_budget_usd": 60}
    catalog_file.write_text(json.dumps(data))

    wo = ops.create_work_order("proj_a", "capped", description="do it")
    assert store.get_work_order(wo["id"])["budget_usd"] == 7.5
    # ...and an explicit --budget still wins over the default.
    wo2 = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=1.0)
    assert store.get_work_order(wo2["id"])["budget_usd"] == 1.0


def test_a_bad_catalog_budget_fails_at_load_not_at_the_first_dispatch():
    for bad in (0, -1, "five"):
        with pytest.raises(CatalogError):
            parse_catalog({"os": {"defaults": {"budget_usd": bad}}, "projects": []})


def test_a_catalog_carrying_nan_is_refused_at_load():
    """`json.loads` accepts a BARE `NaN` literal, so a catalog can carry one, and this is
    the only thing between it and every new order in the project. It passes both earlier
    guards: `isinstance(nan, float)` is true, and `nan <= 0` is False like every other
    comparison against it."""
    loaded = json.loads('{"os": {"defaults": {"budget_usd": NaN}}, "projects": []}')
    assert loaded["os"]["defaults"]["budget_usd"] != loaded["os"]["defaults"]["budget_usd"]
    with pytest.raises(CatalogError):
        parse_catalog(loaded)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(CatalogError):
            parse_catalog({"os": {"defaults": {"feature_budget_usd": bad}},
                           "projects": []})


def test_the_catalog_budget_reaches_the_config_ledger():
    """A setting outside the version ledger is a setting that cannot be audited or rolled
    back with the rest of the configuration."""
    from jarvis import config_version

    resolved = config_version.resolve(parse_catalog(
        {"os": {"defaults": {"budget_usd": 5, "feature_budget_usd": 40}},
         "projects": []}))
    assert resolved["os.defaults.budget_usd"] == 5.0
    assert resolved["os.defaults.feature_budget_usd"] == 40.0


def test_an_amount_is_parsed_the_way_a_user_types_it():
    assert budget.parse_amount("5") == 5.0
    assert budget.parse_amount("$5.50") == 5.5
    assert budget.parse_amount(" 1,250 ") == 1250.0


def test_zero_is_refused_rather_than_read_as_no_ceiling():
    """`--budget 0` reads like "spend nothing"; silently inverting that into "spend
    anything" is the one misreading that costs money."""
    with pytest.raises(ValueError):
        budget.parse_amount("0")
    with pytest.raises(ValueError):
        budget.parse_amount("-3")


def test_a_budget_that_is_not_a_finite_number_is_refused():
    """`float()` accepts all of these, and none of them is a dollar amount. Reachable from
    `jarvis wo budget <id> nan` and from the dashboard's budget box, both of which parse
    here."""
    for bad in ("nan", "NaN", "inf", "-inf", "Infinity", "$nan"):
        with pytest.raises(ValueError):
            budget.parse_amount(bad)


def test_why_nan_is_the_one_that_matters_it_fails_open():
    """NOT a restatement of the guard — this is the behaviour the guard exists to prevent,
    asserted on the real `Ceiling` and the real argv formatter so that removing the guard
    fails HERE with the consequence spelled out rather than only at the parser.

    A `nan` cap reads as enabled on every surface and enforces nothing: `exhausted` is
    `nan - spent <= 0`, False for the life of the order, so the order never parks and
    `INV-BUDGET-OVERSPENT` never fires — while the turn goes out under a cap of zero,
    because `max(0.0, nan)` keeps its FIRST argument when the comparison is False.
    """
    nan = float("nan")
    cap = budget.Ceiling(cap_usd=nan, spent_usd=10_000.0, source="work_order")
    assert not cap.exhausted                     # ...after ten thousand dollars
    assert max(0.0, cap.remaining_usd) == 0.0    # ...and the turn is capped at nothing
    assert f"{max(0.0, cap.remaining_usd):.6f}" == "0.000000"


def test_the_budget_verb_reports_both_halves(started, store):
    from jarvis.central_store import CentralStore

    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=10.0)
    bill_the_turn(store, wo["id"], 2.0)
    central = CentralStore()
    try:
        central.add_agent_call("neo", label="question", model="sonnet",
                                  project="proj_a", wo_id=wo["id"], ok=True,
                                  usage={"total_cost_usd": 0.5})
    finally:
        central.close()

    out = ops.work_order_budget(wo["id"])
    assert out["worker_usd"] == 2.0
    assert out["jarvis_usd"] == 0.5
    assert out["remaining_usd"] == 7.5


def test_a_settled_order_refuses_a_budget(started, store):
    wo = ops.create_work_order("proj_a", "done", description="do it")
    store.set_status(wo["id"], "completed")
    with pytest.raises(ops.OpsError, match="nothing left to spend"):
        ops.set_work_order_budget(wo["id"], 5.0)


# -- the post-condition ----------------------------------------------------------------


def test_the_os_reports_an_order_that_outran_its_ceiling_unparked(started, store):
    """INV-BUDGET-OVERSPENT. The settler parks a spent order every tick, so a violation
    here is not "nobody has got to it yet" — it is that something is keeping the order
    out of that branch, and until it clears, every turn the order starts is launched with
    no headroom."""
    from jarvis.invariants import check_budgets_are_enforced

    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=1.0)
    bill_the_turn(store, wo["id"], 4.0)
    store.set_status(wo["id"], "running")

    found = list(check_budgets_are_enforced(store))
    assert [v.invariant for v in found] == ["INV-BUDGET-OVERSPENT"]
    assert found[0].wo_id == wo["id"]

    # ...and it goes quiet once the order is where it belongs.
    store.set_status(wo["id"], "budget_exhausted")
    assert list(check_budgets_are_enforced(store)) == []


def test_the_post_condition_is_silent_on_an_unbudgeted_fleet(started, store):
    from jarvis.invariants import check_budgets_are_enforced

    wo = ops.create_work_order("proj_a", "uncapped", description="do it")
    bill_the_turn(store, wo["id"], 400.0)
    store.set_status(wo["id"], "running")
    assert list(check_budgets_are_enforced(store)) == []


def test_the_status_reads_with_its_figures(started, store):
    """`status_label` is the string every listing prints. "budget_exhausted" alone sends
    the reader to a second command for the one thing they need — how much more it takes."""
    from jarvis.invariants import status_label

    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=2.0)
    bill_the_turn(store, wo["id"], 2.5)
    store.set_status(wo["id"], "budget_exhausted")
    label = status_label(store, store.get_work_order(wo["id"]))
    assert "$2.50" in label and "$2.00" in label


def test_the_label_does_not_promise_a_retry_that_will_never_come(started, store):
    """A spent order can also hold a booked retry, and only one of the two happens:
    `NOT_RETRIED` means the sweep never relaunches this status."""
    from jarvis.invariants import status_label

    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=2.0)
    turn = store.create_turn(wo["id"], kind="message", prompt="work")
    store.finish_turn(turn["id"], "failed",
                      error="API Error: 500 Internal server error. This is a "
                            "server-side issue, usually temporary.",
                      cost_usd=2.5, terminal_reason="api_error", api_error_status=500,
                      usage_json=json.dumps({"total_cost_usd": 2.5,
                                             "duration_api_ms": 1000}))
    store.set_status(wo["id"], "budget_exhausted")
    label = status_label(store, store.get_work_order(wo["id"]))
    assert "budget spent" in label
    assert "retry" not in label


def test_a_resumed_worker_is_told_the_truth_about_why_it_stopped(started, store,
                                                                fake_claude, monkeypatch):
    """The nudge is what the WORKER reads, and the transport wording is false here: its
    turn was not lost to a 500, it cost money and the user chose to fund more. Telling it
    "this was the transport, not anything you or the work did" would be a false account
    of its own conversation."""
    from jarvis.worker_session import PAUSE_BUDGET, TurnPause, _nudge

    nudge = _nudge(TurnPause(reason=PAUSE_BUDGET, turn={}, retry_at=0.0, attempts=1,
                             message="its budget was raised"))
    assert "budget" in nudge
    assert "transport" not in nudge
    assert "API" not in nudge
    # ...and it still carries the one instruction the whole nudge exists for.
    assert "Do not start again" in nudge


def test_an_open_round_is_allowed_to_finish_over_budget(started, store, catalog_file):
    """Judging delivered work is how that work LANDS, so a round already open is not
    interrupted by the budget. Parking would not even stop the machine —
    `work_orders_awaiting_validation` is keyed off the round and bounded by
    `OPEN_STATUSES`, which `budget_exhausted` is in — it would only make the two fight,
    the round settling the status and the settler re-parking it on every tick."""
    from jarvis.catalog import load_catalog

    spec = load_catalog(catalog_file).projects[0]
    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=1.0)
    store.update_work_order(wo["id"], session_id="sess-x", result_summary="done",
                            pr_url="https://example.invalid/pr/1")
    bill_the_turn(store, wo["id"], 5.0)
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="fp-1")
    store.set_status(wo["id"], "validating")

    started.settle_work_order(spec, store, store.get_work_order(wo["id"]))
    assert store.get_work_order(wo["id"])["status"] == "validating"

    # ...and the moment the round settles, the budget takes over.
    store.close_validation_round(rnd["id"], "passed")
    store.set_status(wo["id"], "waiting_pr_merge")
    started.settle_work_order(spec, store, store.get_work_order(wo["id"]))
    assert store.get_work_order(wo["id"])["status"] == "budget_exhausted"


# -- review round 1: the two paths that were stated and not shown -----------------------


def test_a_child_stopped_on_its_slice_is_resumable_by_topping_the_child_up(
        started, store, fake_claude):
    """THE WHOLE LOOP for a child that ran out on its FEATURE'S slice, not its own budget.

    `ceiling` takes the tighter of the child's own budget and its reservation, so without
    the re-cut in `ops.set_work_order_budget` no number the user typed on the child could
    ever free it: the stale slice would go on winning the `min` and the order would
    answer "still over its ceiling" for ever.

    TWO STEPS, and that is the design rather than a wrinkle. Equal splitting hands the
    children the whole pool between them, so a feature whose child overran has nothing
    spare to re-cut from until either a sibling settles cheaply or the user tops the
    FAMILY up. `jarvis fo budget` adds the money and says which children are stuck;
    `jarvis wo budget` spends it on the one the user chose. Re-funding every child from
    the feature would make that choice for them.
    """
    fo = a_feature(started, store, "reader", "writer", budget_usd=20.0)
    for _ in range(4):
        started.tick()
    child = store.feature_children(fo["id"])[0]
    slice_usd = child["budget_reserved_usd"]
    assert slice_usd, "the fixture never reserved, so the claim is vacuous"

    # Billed rather than acted out through the fake CLI: exhaustion is answered from the
    # ACCOUNTING, so the settler parks it from this alone, and a fake worker stopping on
    # its own cap would only also test the transport. `running` because that is where an
    # order that runs out actually is — the fixture's children have already delivered.
    store.set_status(child["id"], "running")
    bill_the_turn(store, child["id"], slice_usd + 1.0)
    started.tick()
    assert store.get_work_order(child["id"])["status"] == "budget_exhausted"

    # Step one: the family. This alone does NOT move the child — the recorded assumption
    # — and the result says which children are waiting on the user's choice.
    fed = ops.set_feature_budget(fo["id"], 200.0)
    assert child["id"] in fed["exhausted_children"]
    started.tick()
    assert store.get_work_order(child["id"])["status"] == "budget_exhausted"

    # Step two: the child. The re-cut finds the new money and the order goes back to work.
    session = store.get_work_order(child["id"])["session_id"]

    def a_capped_turn_for_this_child(call: dict) -> bool:
        return session in call["argv"] and budget_flag(call) is not None

    before = len([c for c in fake_claude.calls if a_capped_turn_for_this_child(c)])
    out = ops.set_work_order_budget(child["id"], 60.0)
    assert out["resumed"], out
    fresh = store.get_work_order(child["id"])
    assert fresh["status"] != "budget_exhausted"
    # Re-cut above what it has already spent, which is what makes it resumable at all.
    assert fresh["budget_reserved_usd"] > slice_usd + 1.0

    # ...and a turn really goes out, under the re-cut slice. Waited for rather than read
    # straight off: the relaunch is a detached process that records itself when it runs.
    sent = fake_claude.wait_calls(a_capped_turn_for_this_child, count=before + 1)
    assert len(sent) == before + 1
    assert float(budget_flag(sent[-1]) or 0) > slice_usd


def test_a_re_cut_never_promises_money_the_family_does_not_have(started, store):
    """The re-cut is bounded by `unreserved`, which is floored at zero, so it can only
    ever hand out money nobody has claimed — and hands out NOTHING when a sibling is
    holding the pool. It therefore never widens an overshoot it did not cause.

    Note what is NOT asserted: that `spent + held <= budget` still holds here. It cannot,
    and not because of the re-cut — `--max-budget-usd` is checked between API calls and
    overshoots, so a child that ran 51c past its slice has already put the family 51c
    over. The property the allocator owns is that it never PROMISES more, and that is the
    one measured.
    """
    from jarvis.central_store import CentralStore

    fo = a_feature(started, store, "reader", "writer", budget_usd=20.0)
    for _ in range(4):
        started.tick()
    child = store.feature_children(fo["id"])[0]
    bill_the_turn(store, child["id"], (child["budget_reserved_usd"] or 0) + 0.5)
    started.tick()

    central = CentralStore()
    try:
        f = store.get_feature_order(fo["id"])
        spare = budget.pool(store, central, f, claimant=child["id"])
        before = budget.pool(store, central, f)
        cut = budget.reserve(store, central, store.get_work_order(child["id"]))
        after = budget.pool(store, central, f)
        burnt = budget.spent(store, central, child["id"]).total_usd
    finally:
        central.close()
    assert spare is not None and before is not None and after is not None
    # A sibling holds the pool, so there was nothing spare and nothing was promised.
    assert spare.unreserved_usd == 0.0
    assert after.held_usd <= before.held_usd + spare.unreserved_usd + 1e-9
    # ...and the cut is still the child's whole spend, so the row stays readable as a
    # lifetime cap rather than becoming a number smaller than what it has already spent.
    assert cut == pytest.approx(burnt)


def test_topping_a_child_up_with_a_broke_feature_says_to_raise_the_feature(
        started, store):
    """"Top up the child" and "top up the feature" are different acts, and a user who
    cannot tell them apart tops up the wrong one. When the re-cut finds nothing to cut,
    the note names the feature and its unreserved remainder rather than repeating a
    ceiling the user just raised."""
    fo = a_feature(started, store, "reader", budget_usd=3.0)
    child = tick_until_reserved(started, store, fo["id"])
    bill_the_turn(store, child["id"], 9.0)          # past the slice AND past the family
    assert tick_until_parked(started, store, child["id"]) == "budget_exhausted"

    out = ops.set_work_order_budget(child["id"], 500.0)
    assert not out["resumed"]
    assert "its feature's slice" in out["note"]
    assert "jarvis fo budget" in out["note"]
    assert "unreserved" in out["note"]


def test_the_post_condition_exempts_the_window_the_settler_declines(started, store):
    """INV-BUDGET-OVERSPENT must not report the OS's own design as a defect. An order
    whose validation round is still runnable is deliberately left unparked so the panel
    can finish judging delivered work — flagging it would fire on EVERY tick of that
    window and teach the reader to ignore the line."""
    from jarvis.invariants import check_budgets_are_enforced

    wo = ops.create_work_order("proj_a", "capped", description="do it", budget_usd=1.0)
    bill_the_turn(store, wo["id"], 4.0)
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="fp-1")
    store.set_status(wo["id"], "validating")
    assert list(check_budgets_are_enforced(store)) == []

    # ...and the exemption is exactly as wide as the settler's, no wider: the moment the
    # round is no longer runnable, an unparked over-budget order is a violation again.
    store.close_validation_round(rnd["id"], "passed")
    store.set_status(wo["id"], "running")
    assert [v.invariant for v in check_budgets_are_enforced(store)] == [
        "INV-BUDGET-OVERSPENT"]

