"""A work order that stopped, and nothing coming for it.

Built from wo-a4bd6958, the planner of fo-6269be9a: its turn 4 ended cleanly at 12:03
saying "Waiting on exact counts before I open the PR", and nothing happened for 7h13m
until the user typed. Two separate failures produced that, and each has a test here.

* The order was NOT invisible — it settled to `needs_review` and carried the attention
  flag the whole time. It carried it for "assumptions pending review", a reason four
  hours older than the parking, which named none of what had just happened. That is
  `STALE_FINISH_BLOCKER`, and the rule it must obey is that it rides BESIDE the older
  reason and never over it.
* A work order the OS still calls `running` with a settled turn behind it had no check
  at all: `running` was not in `BLOCKED_STATUSES`, so `true_blockers` never looked. That
  is `PARKED_BLOCKER`.

The noise rule is the other half of the suite and most of its assertions: an order
waiting on a dependency, a gate, Neo, a queued message, a scheduled retry or a turn in
flight is waiting CORRECTLY and must stay silent.
"""

from __future__ import annotations

import json

import pytest

from jarvis import db, ops
from jarvis.central_store import CentralStore
from jarvis.invariants import (
    PARKED_BLOCKER,
    STALE_FINISH_BLOCKER,
    check_project,
    parked_reason,
    true_blockers,
)
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

#: Comfortably past the shipped 60-minute default, in seconds.
LONG_ENOUGH = 90 * 60


def _parked_running(store: ProjectStore, title: str = "the parked one") -> dict:
    """The shape `Daemon.settle_work_order` leaves behind when it never gets there: the
    turn is reaped and done, the work order still says `running`, nothing is queued."""
    wo = store.create_work_order(title)
    store.set_status(wo["id"], "running")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "done", result="waiting on exact counts")
    return store.get_work_order(wo["id"])


def _wo_a4bd6958(store: ProjectStore) -> dict:
    """wo-a4bd6958 itself: finished on an early turn, sent back to work, stopped again.

    The `finished` event is what `settle_work_order` can never re-reach — it reads
    `result_summary`, a column no later turn clears — and the assumptions are the older
    flag that ended up speaking for the whole 7h13m.
    """
    wo = store.create_work_order("Plan: widen the supervisor")
    early = store.create_turn(wo["id"], "dispatch", "plan it")
    store.finish_turn(early["id"], "done", result="submitted the plan")
    store.update_work_order(wo["id"], result_summary="planned it into six work orders")
    store.add_event(wo["id"], "finished", {"summary": "planned it"})
    for i in range(3):
        store.add_assumption(wo["id"], f"assumption {i}")
    later = store.create_turn(wo["id"], "message", "go and open the PR")
    store.finish_turn(later["id"], "done", result="waiting on exact counts before the PR")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], "assumptions pending review")
    return store.get_work_order(wo["id"])


def _rewind(monkeypatch, seconds: float):
    """Build the fixture `seconds` in the past, with the clock still MOVING.

    Freezing `db.now` is not good enough here: the whole of `STALE_FINISH_BLOCKER` is
    that one recorded moment is earlier than another, and a constant clock makes every
    row in the fixture simultaneous. Returns the callable that puts the real one back.
    """
    was = db.now
    base = was() - seconds
    ticks = iter(range(100_000))
    monkeypatch.setattr(db, "now", lambda: base + next(ticks))
    return lambda: monkeypatch.setattr(db, "now", was)


def _later(store: ProjectStore, wo_id: str) -> float:
    """Long enough after this work order's last turn ended for the threshold to bite."""
    return float(store.latest_turn(wo_id)["ended_at"]) + LONG_ENOUGH


# -- the two shapes it must catch ------------------------------------------------------


def test_a_settled_turn_on_a_running_order_is_parked_once_the_threshold_passes(project):
    store = ProjectStore(project)
    wo = _parked_running(store)

    assert parked_reason(store, wo, now=float(wo["updated_at"])) is None
    assert parked_reason(store, wo, now=_later(store, wo["id"])) == PARKED_BLOCKER
    assert true_blockers(store, wo, now=_later(store, wo["id"])) == [PARKED_BLOCKER]


def test_a_stale_finish_rides_beside_the_reason_it_was_masked_by(project):
    """The 7h13m itself. The assumptions line stays first and stays the flag; the
    parking is appended, so the user reads both instead of one replacing the other."""
    store = ProjectStore(project)
    wo = _wo_a4bd6958(store)

    blockers = true_blockers(store, wo, now=_later(store, wo["id"]))

    assert blockers == ["3 assumptions pending your review", STALE_FINISH_BLOCKER]
    assert wo["attention_reason"] == "assumptions pending review"


def test_a_needs_review_order_whose_finish_is_current_says_nothing_extra(project):
    """The noise rule at its sharpest: every review the user has not got to yet has a
    settled turn behind it, and none of them is news."""
    store = ProjectStore(project)
    wo = store.create_work_order("ordinary review")
    turn = store.create_turn(wo["id"], "dispatch", "do it")
    store.add_assumption(wo["id"], "one call I made")
    store.add_event(wo["id"], "finished", {"summary": "done"})
    store.finish_turn(turn["id"], "done", result="done")
    store.update_work_order(wo["id"], result_summary="done")
    store.set_status(wo["id"], "needs_review")
    wo = store.get_work_order(wo["id"])

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None
    assert true_blockers(store, wo, now=_later(store, wo["id"])) == [
        "1 assumption pending your review"]


# -- the noise rule --------------------------------------------------------------------


def test_a_dependency_blocked_order_is_not_parked(project):
    store = ProjectStore(project)
    first = store.create_work_order("the one it waits for")
    wo = store.create_work_order("the waiter", depends_on=[first["id"]])

    assert store.get_work_order(wo["id"])["status"] == "pending"
    assert parked_reason(store, store.get_work_order(wo["id"]),
                         now=db.now() + LONG_ENOUGH) is None


def test_a_gate_blocked_order_is_not_parked(project):
    store = ProjectStore(project)
    wo = _parked_running(store)
    store.add_approval(wo["id"], "pr_merge", "gh pr merge 42")

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None


def test_an_order_with_a_pending_neo_question_is_not_parked(project):
    store = ProjectStore(project)
    wo = _parked_running(store)
    neo = NeoStore()
    try:
        neo.ask("proj_a", wo["id"], "which way round?")
    finally:
        neo.close()

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None


def test_an_order_with_a_turn_in_flight_is_not_parked(project):
    store = ProjectStore(project)
    wo = store.create_work_order("still working")
    store.set_status(wo["id"], "running")
    store.create_turn(wo["id"], "dispatch", "do the thing")

    assert parked_reason(store, store.get_work_order(wo["id"]),
                         now=db.now() + LONG_ENOUGH) is None


def test_a_queued_message_is_not_parked(project):
    """The next turn is already written down; the daemon delivers it."""
    store = ProjectStore(project)
    wo = _parked_running(store)
    store.queue_message(wo["id"], "carry on")

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None


def test_a_turn_waiting_on_its_scheduled_retry_is_not_parked(project):
    """`retry_paused_turns` relaunches this one unaided, and `ops.waiting_on` had to
    learn to say so — its fall-through called a booked retry a permission prompt."""
    store = ProjectStore(project)
    wo = store.create_work_order("hit the usage limit")
    store.set_status(wo["id"], "running")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "failed",
                      error="Claude usage limit reached|1788400000",
                      terminal_reason="usage_limit")
    wo = store.get_work_order(wo["id"])

    assert ops.waiting_on(store, wo)["what"] == "retry_pending"
    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None


def test_a_manager_is_never_parked(project):
    """A project manager is idle BY DESIGN between its feature's messages — flagging one
    would put a permanent second line on every feature order in the fleet."""
    store = ProjectStore(project)
    wo = store.create_work_order("manage the feature", kind="manager")
    store.set_status(wo["id"], "waiting_input")
    turn = store.create_turn(wo["id"], "dispatch", "manage it")
    store.finish_turn(turn["id"], "done", result="nothing to do")

    assert parked_reason(store, store.get_work_order(wo["id"]),
                         now=_later(store, wo["id"])) is None


def test_an_injected_session_is_never_parked(project):
    """It was never briefed on `jarvis wo finish`, so its silence proves nothing —
    the same excuse `true_blockers` already makes for it."""
    store = ProjectStore(project)
    wo = store.create_work_order("the user's own session", origin="injected")
    store.set_status(wo["id"], "running")
    turn = store.create_turn(wo["id"], "dispatch", "whatever they typed")
    store.finish_turn(turn["id"], "done", result="…")

    assert parked_reason(store, store.get_work_order(wo["id"]),
                         now=_later(store, wo["id"])) is None


# -- the reconcile tick, and the surface --------------------------------------------


def test_the_reconciler_flags_a_parked_running_order(project, monkeypatch):
    """`running` joined BLOCKED_STATUSES for this alone: without it the blocker is
    derived correctly and then never surfaced."""
    store = ProjectStore(project)
    restore = _rewind(monkeypatch, LONG_ENOUGH)
    wo = _parked_running(store)
    restore()

    violations = check_project(store)

    assert wo["needs_attention"] == 0
    flagged = store.get_work_order(wo["id"])
    assert flagged["needs_attention"] == 1
    assert flagged["attention_reason"] == PARKED_BLOCKER
    assert [v.invariant for v in violations] == ["INV-ATTENTION-MISSING"]


def test_a_read_only_doctor_run_reports_the_parking_without_raising_it(
        project, monkeypatch):
    """`jarvis doctor` runs every invariant through `invariants._ReadOnly`, which
    swallows `flag_attention` — and `parked_reason` reaches `store.project_path` for its
    threshold through that same proxy, which is a passthrough of a non-callable."""
    store = ProjectStore(project)
    restore = _rewind(monkeypatch, LONG_ENOUGH)
    wo = _parked_running(store)
    restore()

    violations = check_project(store, repair=False)

    assert [v.invariant for v in violations] == ["INV-ATTENTION-MISSING"]
    assert store.get_work_order(wo["id"])["needs_attention"] == 0


def test_jarvis_status_names_the_parked_order_and_what_it_is_parked_on(
        project, catalog_file, monkeypatch):
    store = ProjectStore(project)
    restore = _rewind(monkeypatch, LONG_ENOUGH)
    parked = _parked_running(store)
    masked = _wo_a4bd6958(store)
    restore()
    check_project(store)
    store.close()
    _register(catalog_file)

    items = {a["wo_id"]: a for a in ops.os_status()["attention"]}

    assert items[parked["id"]]["reason"] == PARKED_BLOCKER
    assert "parked" not in items[parked["id"]]  # already the whole line
    assert items[masked["id"]]["reason"] == "assumptions pending review"
    assert items[masked["id"]]["parked"] == STALE_FINISH_BLOCKER


def test_a_parked_child_is_named_on_its_feature_orders_rolled_up_line(
        project, catalog_file, monkeypatch):
    """A feature's children get ONE rolled-up line instead of a line each, and
    wo-a4bd6958 was a child — so the rollup is the only place `jarvis status` can say it.
    """
    store = ProjectStore(project)
    fo = store.create_feature_order("widen the supervisor", "…")
    restore = _rewind(monkeypatch, LONG_ENOUGH)
    child = _wo_a4bd6958(store)
    store.update_work_order(child["id"], parent_id=fo["id"])
    restore()
    check_project(store)
    store.close()
    _register(catalog_file)

    line = [a for a in ops.os_status()["attention"] if a.get("fo_id") == fo["id"]][0]

    assert child["id"] in line["reason"]
    assert "assumptions pending review" in line["reason"]
    assert STALE_FINISH_BLOCKER in line["reason"]


def test_the_dashboard_shows_the_blocker_the_flag_could_not_carry(
        project, catalog_file, jarvis_home, fake_claude, monkeypatch):
    """`attention_reason` is one column fed from `true_blockers[0]`, so a masked order
    reports only the decision. Listings link; the work order's own page tells."""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    store = ProjectStore(project)
    restore = _rewind(monkeypatch, LONG_ENOUGH)
    masked = _wo_a4bd6958(store)
    ordinary = _parked_running(store, title="nothing to add")
    restore()
    check_project(store)  # the reconciler is what puts the flag on `ordinary`
    store.close()
    ops.start_os(str(catalog_file), foreground=True)
    client = TestClient(create_app(), follow_redirects=False)

    def second_blocker(text: str) -> str:
        # The added paragraph exactly. Matched as markup rather than by counting the
        # bare sentence, which also appears in the `attention` timeline event below it.
        return f'<p class="st tone-warn"><span class="i">◭</span>{text}</p>'

    page = client.get(f"/wo/proj_a/{masked['id']}").text

    assert "assumptions pending review" in page
    assert second_blocker(STALE_FINISH_BLOCKER) in page
    # ...and nothing added to an order whose one reason already says it.
    other = client.get(f"/wo/proj_a/{ordinary['id']}").text
    assert second_blocker(PARKED_BLOCKER) not in other
    assert PARKED_BLOCKER in other


# -- the threshold is a setting ------------------------------------------------------


def _register(catalog_file) -> None:
    """What `jarvis start` does to the central store, minus starting anything: the
    catalog `inspect_config_at` resolves against, and the project row `os_status` walks.
    """
    data = json.loads(catalog_file.read_text())
    central = CentralStore()
    try:
        central.set_state("catalog_path", str(catalog_file))
        for spec in data["projects"]:
            central.upsert_project(spec["name"], spec["path"], spec.get("description", ""))
    finally:
        central.close()


def _set_inspect(catalog_file, *, os_block: dict | None = None,
                 project_block: dict | None = None) -> None:
    data = json.loads(catalog_file.read_text())
    if os_block is not None:
        data["os"]["inspect"] = os_block
    if project_block is not None:
        data["projects"][0]["inspect"] = project_block
    catalog_file.write_text(json.dumps(data))
    _register(catalog_file)


@pytest.mark.parametrize("minutes,parked", [(600, False), (10, True)])
def test_the_threshold_is_a_fleet_setting(project, catalog_file, minutes, parked):
    store = ProjectStore(project)
    wo = _parked_running(store)
    _set_inspect(catalog_file, os_block={"alarm_parked_minutes": minutes})

    got = parked_reason(store, wo, now=_later(store, wo["id"]))

    assert (got == PARKED_BLOCKER) is parked


def test_a_project_overrides_the_fleet_threshold_and_inherits_the_rest(
        project, catalog_file):
    store = ProjectStore(project)
    wo = _parked_running(store)
    _set_inspect(catalog_file, os_block={"alarm_parked_minutes": 10},
                 project_block={"alarm_parked_minutes": 600})

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None
    assert ops.inspect_config_at(project).alarm_turn_minutes == 60


def test_disabling_the_inspect_alarms_disables_the_parked_check(project, catalog_file):
    store = ProjectStore(project)
    wo = _parked_running(store)
    _set_inspect(catalog_file, os_block={"enabled": False})

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None
    assert true_blockers(store, wo, now=_later(store, wo["id"])) == []


def test_the_parked_reason_does_not_move_with_the_clock(project):
    """It is compared against `attention_reason` and stored by `ack_attention`, so a
    reason carrying an elapsed time would be rewritten every tick and never stick."""
    store = ProjectStore(project)
    wo = _parked_running(store)
    ended = float(store.latest_turn(wo["id"])["ended_at"])

    assert (parked_reason(store, wo, now=ended + LONG_ENOUGH)
            == parked_reason(store, wo, now=ended + 40 * LONG_ENOUGH))


def test_acknowledging_a_parked_order_puts_the_flag_down_for_good(project):
    store = ProjectStore(project)
    wo = _parked_running(store)
    now = _later(store, wo["id"])
    store.ack_attention(wo["id"], true_blockers(store, wo, now=now))

    assert true_blockers(store, store.get_work_order(wo["id"]), now=now) == []


def test_nothing_here_nudges_a_worker(project):
    """The supervisor's gated remedy is the OS's one path into a worker's inbox
    (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §5). This check
    only raises a flag — wo-a4bd6958's own case is the argument: the suite run it was
    waiting on died with its process, so "continue" would have resumed nothing.
    """
    store = ProjectStore(project)
    wo = _parked_running(store)
    check_project(store)

    assert store.queued_messages(wo["id"]) == []
    assert store.get_work_order(wo["id"])["status"] == "running"
