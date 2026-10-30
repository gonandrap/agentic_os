"""A work order that stopped, and nothing coming for it (docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md)"""

from __future__ import annotations

import json

import pytest

from jarvis import db, invariants, ops
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

pytestmark = pytest.mark.usefixtures("neo_projects")

#: Comfortably past the shipped 60-minute default, in seconds
LONG_ENOUGH = 90 * 60


def _parked(store: ProjectStore, title: str = "the parked one") -> dict:
    """A row NO PASS SELECTS: the turn is reaped and done"""
    wo = store.create_work_order(title)
    store.update_work_order(wo["id"], session_id="sess-1")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "done", result="waiting on exact counts")
    store.set_status(wo["id"], "waiting_pr_merge")
    return store.get_work_order(wo["id"])


def _waiting_on_a_delegate(store: ProjectStore) -> dict:
    """A worker parked in `waiting_input`, where the gate and question passes DO select"""
    wo = store.create_work_order("asked the delegate something")
    store.update_work_order(wo["id"], session_id="sess-1")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "done", result="waiting on the gate")
    store.set_status(wo["id"], "waiting_input")
    return store.get_work_order(wo["id"])


def _wo_a4bd6958(store: ProjectStore) -> dict:
    """wo-a4bd6958 itself: finished on an early turn, sent back to work, stopped again"""
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


def _later(store: ProjectStore, wo_id: str) -> float:
    """Long enough after this work order's last turn ended for the threshold to bite"""
    return float(store.latest_turn(wo_id)["ended_at"]) + LONG_ENOUGH


# -- the two shapes it must catch


def test_a_settled_turn_on_a_row_no_pass_selects_is_parked_once_the_threshold_passes(
        project):
    """GitHub issue 100's half of this suite"""
    store = ProjectStore(project)
    wo = _parked(store)

    assert parked_reason(store, wo, now=float(wo["updated_at"])) is None
    assert parked_reason(store, wo, now=_later(store, wo["id"])) == PARKED_BLOCKER
    assert true_blockers(store, wo, now=_later(store, wo["id"])) == [PARKED_BLOCKER]


def test_a_stale_finish_rides_beside_the_reason_it_was_masked_by(project):
    """The 7h13m itself"""
    store = ProjectStore(project)
    wo = _wo_a4bd6958(store)

    blockers = true_blockers(store, wo, now=_later(store, wo["id"]))

    assert blockers == ["3 assumptions pending your review", STALE_FINISH_BLOCKER]
    assert wo["attention_reason"] == "assumptions pending review"


def test_a_needs_review_order_whose_finish_is_current_says_nothing_extra(project):
    """The noise rule at its sharpest: every review the user has not got to yet has a settled turn behind..."""
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


# -- the noise rule


def test_a_dependency_blocked_order_is_not_parked(project):
    store = ProjectStore(project)
    first = store.create_work_order("the one it waits for")
    wo = store.create_work_order("the waiter", depends_on=[first["id"]])

    assert store.get_work_order(wo["id"])["status"] == "pending"
    assert parked_reason(store, store.get_work_order(wo["id"]),
                         now=db.now() + LONG_ENOUGH) is None


def test_a_gate_blocked_order_is_not_parked(project):
    store = ProjectStore(project)
    wo = _waiting_on_a_delegate(store)
    store.add_approval(wo["id"], "pr_merge", "gh pr merge 42")

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None


def test_an_order_with_a_pending_neo_question_is_not_parked(project):
    store = ProjectStore(project)
    wo = _waiting_on_a_delegate(store)
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
    """The next turn is already written down; the daemon delivers it"""
    store = ProjectStore(project)
    wo = _waiting_on_a_delegate(store)
    store.queue_message(wo["id"], "carry on")
    wo = store.get_work_order(wo["id"])

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None


def test_a_turn_waiting_on_its_scheduled_retry_is_not_parked(project):
    """`retry_paused_turns` relaunches this one unaided, and `ops.waiting_on` had to learn to say so"""
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


def test_an_idle_manager_is_never_parked(project):
    """A project manager is idle BY DESIGN between its feature's messages (#264)"""
    store = ProjectStore(project)
    wo = store.create_work_order(
        "manage the feature", kind="manager",
        parent_id=store.create_feature_order("widen the supervisor", "…")["id"])
    store.set_status(wo["id"], "idle")
    turn = store.create_turn(wo["id"], "dispatch", "manage it")
    store.finish_turn(turn["id"], "done", result="nothing to do")

    assert parked_reason(store, store.get_work_order(wo["id"]),
                         now=_later(store, wo["id"])) is None


def test_a_manager_stranded_in_waiting_input_IS_parked(project):
    """The other half, and the reason the kind carve-out had to go"""
    store = ProjectStore(project)
    wo = store.create_work_order("manage the feature", kind="manager")
    store.set_status(wo["id"], "waiting_input")
    turn = store.create_turn(wo["id"], "dispatch", "manage it")
    store.finish_turn(turn["id"], "done", result="nothing to do")

    assert parked_reason(store, store.get_work_order(wo["id"]),
                         now=_later(store, wo["id"])) is not None


def test_an_injected_session_is_never_parked(project):
    """It was never briefed on `jarvis wo finish`, so its silence proves nothing"""
    store = ProjectStore(project)
    wo = store.create_work_order("the user's own session", origin="injected")
    store.set_status(wo["id"], "running")
    turn = store.create_turn(wo["id"], "dispatch", "whatever they typed")
    store.finish_turn(turn["id"], "done", result="…")

    assert parked_reason(store, store.get_work_order(wo["id"]),
                         now=_later(store, wo["id"])) is None


# -- the reconcile tick, and the surface


def test_the_reconciler_flags_a_parked_running_order(project, clock):
    """INV-ATTENTION-MISSING must select `running` for this: a status it skips has its blocker derived..."""
    store = ProjectStore(project)
    clock.ticking()                          # the rows must still be strictly ordered
    with clock.moved(-LONG_ENOUGH):
        wo = _parked(store)

    violations = check_project(store)

    assert wo["needs_attention"] == 0
    flagged = store.get_work_order(wo["id"])
    assert flagged["needs_attention"] == 1
    assert flagged["attention_reason"] == PARKED_BLOCKER
    assert [v.invariant for v in violations] == ["INV-ATTENTION-MISSING"]


def test_a_read_only_doctor_run_reports_the_parking_without_raising_it(
        project, clock):
    """`jarvis doctor` runs every invariant through `invariants._ReadOnly`"""
    store = ProjectStore(project)
    clock.ticking()
    with clock.moved(-LONG_ENOUGH):
        wo = _parked(store)

    violations = check_project(store, repair=False)

    assert [v.invariant for v in violations] == ["INV-ATTENTION-MISSING"]
    assert store.get_work_order(wo["id"])["needs_attention"] == 0


def test_jarvis_status_names_the_parked_order_and_what_it_is_parked_on(
        project, catalog_file, clock):
    store = ProjectStore(project)
    clock.ticking()
    with clock.moved(-LONG_ENOUGH):
        parked = _parked(store)
        masked = _wo_a4bd6958(store)
    check_project(store)
    store.close()
    _register(catalog_file)

    items = {a["wo_id"]: a for a in ops.os_status()["attention"]}

    assert items[parked["id"]]["reason"] == PARKED_BLOCKER
    assert "parked" not in items[parked["id"]]  # already the whole line
    assert items[masked["id"]]["reason"] == "assumptions pending review"
    assert items[masked["id"]]["parked"] == STALE_FINISH_BLOCKER


def test_a_parked_child_is_named_on_its_feature_orders_rolled_up_line(
        project, catalog_file, clock):
    """A feature's children get ONE rolled-up line instead of a line each, and wo-a4bd6958 was a child"""
    store = ProjectStore(project)
    fo = store.create_feature_order("widen the supervisor", "…")
    clock.ticking()
    with clock.moved(-LONG_ENOUGH):
        child = _wo_a4bd6958(store)
        store.update_work_order(child["id"], parent_id=fo["id"])
    check_project(store)
    store.close()
    _register(catalog_file)

    line = [a for a in ops.os_status()["attention"] if a.get("fo_id") == fo["id"]][0]

    assert child["id"] in line["reason"]
    assert "assumptions pending review" in line["reason"]
    assert STALE_FINISH_BLOCKER in line["reason"]


def test_the_dashboard_shows_the_blocker_the_flag_could_not_carry(
        project, catalog_file, jarvis_home, fake_claude, clock):
    """`attention_reason` is one column fed from `true_blockers[0]`"""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    store = ProjectStore(project)
    clock.ticking()
    with clock.moved(-LONG_ENOUGH):
        masked = _wo_a4bd6958(store)
        ordinary = _parked(store, title="nothing to add")
    check_project(store)  # the reconciler is what puts the flag on `ordinary`
    store.close()
    ops.start_os(str(catalog_file), foreground=True)
    client = TestClient(create_app(), follow_redirects=False)

    def second_blocker(text: str) -> str:
        # The added paragraph exactly
        return f'<p class="st wrap tone-warn"><span class="i">◭</span>{text}</p>'

    page = client.get(f"/wo/proj_a/{masked['id']}").text

    assert "assumptions pending review" in page
    assert second_blocker(STALE_FINISH_BLOCKER) in page
    # ...and nothing added to an order whose one reason already says it
    other = client.get(f"/wo/proj_a/{ordinary['id']}").text
    assert second_blocker(PARKED_BLOCKER) not in other
    assert PARKED_BLOCKER in other


# -- the threshold is a setting


def _register(catalog_file) -> None:
    """What `jarvis start` does to the central store"""
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
    wo = _parked(store)
    _set_inspect(catalog_file, os_block={"alarm_parked_minutes": minutes})

    got = parked_reason(store, wo, now=_later(store, wo["id"]))

    assert (got == PARKED_BLOCKER) is parked


def test_a_project_overrides_the_fleet_threshold_and_inherits_the_rest(
        project, catalog_file):
    store = ProjectStore(project)
    wo = _parked(store)
    _set_inspect(catalog_file, os_block={"alarm_parked_minutes": 10},
                 project_block={"alarm_parked_minutes": 600})

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None
    assert ops.inspect_config_at(project).alarm_turn_minutes == 60


def test_disabling_the_inspect_alarms_disables_the_parked_check(project, catalog_file):
    store = ProjectStore(project)
    wo = _parked(store)
    _set_inspect(catalog_file, os_block={"enabled": False})

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None
    # ...and what is left is the line the THRESHOLD does not govern: the alarms switch turns off "it has...
    assert true_blockers(store, wo, now=_later(store, wo["id"])) == [
        invariants.NO_ACTOR_BLOCKER.format(status="waiting_pr_merge")]


def test_the_parked_reason_does_not_move_with_the_clock(project):
    """It is compared against `attention_reason` and stored by `ack_attention`"""
    store = ProjectStore(project)
    wo = _parked(store)
    ended = float(store.latest_turn(wo["id"])["ended_at"])

    assert (parked_reason(store, wo, now=ended + LONG_ENOUGH)
            == parked_reason(store, wo, now=ended + 40 * LONG_ENOUGH))


def test_acknowledging_a_parked_order_puts_the_flag_down_for_good(project):
    store = ProjectStore(project)
    wo = _parked(store)
    now = _later(store, wo["id"])
    store.ack_attention(wo["id"], true_blockers(store, wo, now=now))

    assert true_blockers(store, store.get_work_order(wo["id"]), now=now) == []


def test_nothing_here_nudges_a_worker(project):
    """The supervisor's gated remedy is the OS's one path into a worker's inbox... (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §5)"""
    store = ProjectStore(project)
    wo = _parked(store)
    check_project(store)

    assert store.queued_messages(wo["id"]) == []
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


# -- 5a: the verdict is the predicate


def test_a_settled_work_order_is_never_asked_who_owns_it(project):
    """The gate is `OPEN_STATUSES`, not `PARKABLE_STATUSES`"""
    store = ProjectStore(project)
    wo = _parked(store)
    store.set_status(wo["id"], "completed")

    assert parked_reason(store, store.get_work_order(wo["id"]),
                         now=_later(store, wo["id"])) is None


def test_it_never_asks_ops_waiting_on(project, monkeypatch):
    """§5a: cutting the `parked_reason` to `waiting_on` edge is what lets section 6 rebase `waiting_on` on..."""
    store = ProjectStore(project)
    wo = _parked(store)
    monkeypatch.setattr(ops, "waiting_on",
                        lambda *a, **k: pytest.fail("parked_reason asked waiting_on"))

    parked_reason(store, wo, now=_later(store, wo["id"]))


def test_the_caller_can_hand_over_a_verdict_it_already_derived(project):
    """The sentinel is NOT None, and that distinction is the argument's whole point: a caller that derived..."""
    store = ProjectStore(project)
    wo = _parked(store)
    now = _later(store, wo["id"])
    somebody = ops.Actor(pass_name="Daemon.settle_turns", why="…",
                         predicate="daemon.settles_turn")

    assert parked_reason(store, wo, now=now, actor=somebody) is None
    assert parked_reason(store, wo, now=now, actor=None) == PARKED_BLOCKER
    assert parked_reason(store, wo, now=now,
                         actor=invariants.ACTOR_NOT_DERIVED) == \
        parked_reason(store, wo, now=now)


def test_resume_auto_still_nudges_a_parked_worker(project, catalog_file):
    """The other half of the GitHub issue 100 pin: the flag is one surface"""
    store = ProjectStore(project)
    wo = store.create_work_order("stopped mid-task")
    store.set_status(wo["id"], "running")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "done", result="waiting on exact counts")
    wo = store.get_work_order(wo["id"])
    store.close()
    _register(catalog_file)

    answer = ops.resume_in_auto(wo["id"])

    assert answer["nudged"] is True


# -- 2b: a head the OS has already judged is not an undelivered one

#: The commit the passing round judged. wo-3312682f's shape: the head never moved
JUDGED = "1f2e3d4c5b6a7980112233445566778899aabbcc"


def _judged_then_replied(store: ProjectStore, clock, *, head: str,
                         judged: str) -> dict:
    """wo-3312682f: delivered, judged green, then a reply turn the OS itself opened"""
    clock.ticking()
    with clock.moved(-4 * LONG_ENOUGH):
        wo = store.create_work_order("delivered behind a pull request")
        first = store.create_turn(wo["id"], "dispatch", "do it")
        store.finish_turn(first["id"], "done", result="opened the PR")
        store.add_event(wo["id"], "finished", {"summary": "opened the PR"})
        round_ = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
        store.set_validation_head(round_["id"], judged)
        store.close_validation_round(round_["id"], "passed", "green")
        msg = store.queue_message(wo["id"], "the gate request was dismissed", source="gate",
                                  status="delivered")
        reply = store.create_turn(wo["id"], "message", "it was dismissed", msg_id=msg)
        store.finish_turn(reply["id"], "done", result="noted, nothing to do")
        store.set_status(wo["id"], "needs_review", pr_url="https://example/pull/1",
                         pr_head_oid=head, pr_head_seen_at=db.now())
    return store.get_work_order(wo["id"])


def test_a_reply_turn_on_an_already_judged_head_is_not_a_stale_finish(project, clock):
    """Spec §2b"""
    store = ProjectStore(project)
    wo = _judged_then_replied(store, clock, head=JUDGED, judged=JUDGED)

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None


def test_a_round_that_recorded_no_commit_is_no_excuse(project, clock):
    """Spec §2b: an EMPTY head is never read as equal"""
    store = ProjectStore(project)
    wo = _judged_then_replied(store, clock, head="", judged="")

    assert parked_reason(store, wo, now=_later(store, wo["id"])) == STALE_FINISH_BLOCKER


def test_a_head_that_moved_past_the_judged_commit_still_reports_the_stale_finish(
        project, clock):
    """The worker pushed after the round passed: that IS something undeclared"""
    store = ProjectStore(project)
    wo = _judged_then_replied(store, clock, head="0" * 40, judged=JUDGED)

    assert parked_reason(store, wo, now=_later(store, wo["id"])) == STALE_FINISH_BLOCKER


def test_the_merged_head_excuses_it_too(project, clock):
    """The other member of the judged set: the newest `pr_merged` event's `head_oid`"""
    store = ProjectStore(project)
    wo = _judged_then_replied(store, clock, head=JUDGED, judged="")
    store.add_event(wo["id"], "pr_merged", {"head_oid": JUDGED})

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None
