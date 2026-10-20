"""A work order that stopped, and nothing coming for it.

Built from wo-a4bd6958, the planner of fo-6269be9a: its turn 4 ended cleanly at 12:03
saying "Waiting on exact counts before I open the PR", and nothing happened for 7h13m
until the user typed. Two separate failures produced that, and each has a test here.

* The order was NOT invisible — it settled to `needs_review` and carried the attention
  flag the whole time. It carried it for "assumptions pending review", a reason four
  hours older than the parking, which named none of what had just happened. That is
  `STALE_FINISH_BLOCKER`, and the rule it must obey is that it rides BESIDE the older
  reason and never over it.
* A work order with a settled turn behind it and no pass selecting it had no check at
  all. That is `PARKED_BLOCKER`.

WHICH ROW THAT IS, IS DERIVED (§5a,
docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md):
`parked_reason` asks `ops.scheduled_actor` instead of consulting `PARKABLE_STATUSES` and
`SPOKEN_FOR_WAITS`. So a `running` row with a done turn is NOT this shape — it is
`Daemon.settle_turns`' row, which moves it to `needs_review` where IDLE_NO_FINISH_BLOCKER
speaks (`daemon.settles_turn`, pinned by `tests/test_scheduled_actor.py::
test_a_done_turn_the_settler_does_move_still_names_it`). The fixture that used to say
`running` encoded a daemon that was NOT RUNNING, and no row predicate can see that — the
only evidence would be elapsed time, which is the second opinion this feature deletes.
The fixture below is a row nobody selects instead.

The noise rule is the other half of the suite and most of its assertions: an order
waiting on a dependency, a gate, Neo, a queued message, a scheduled retry or a turn in
flight is waiting CORRECTLY and must stay silent.
"""

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

#: Comfortably past the shipped 60-minute default, in seconds.
LONG_ENOUGH = 90 * 60


def _parked(store: ProjectStore, title: str = "the parked one") -> dict:
    """A row NO PASS SELECTS: the turn is reaped and done, the order sits in
    `waiting_pr_merge`, and no `pr_url` was ever recorded.

    So `Daemon.poll_pull_requests` — the only candidate for that status — has nothing to
    ask GitHub about, nothing else sweeps it, and `ops.scheduled_actor` names nobody.
    That is what "parked" means now, and it is one of the two shapes §5d was written for.

    The SESSION ID is part of the shape rather than decoration: without one
    `worker_session.delivery_hold` holds any queued message, so the message test below
    would pin the wrong verdict (the note `tests/test_scheduled_actor.py:534` makes).
    """
    wo = store.create_work_order(title)
    store.update_work_order(wo["id"], session_id="sess-1")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "done", result="waiting on exact counts")
    store.set_status(wo["id"], "waiting_pr_merge")
    return store.get_work_order(wo["id"])


def _waiting_on_a_delegate(store: ProjectStore) -> dict:
    """A worker parked in `waiting_input`, where the gate and question passes DO select.

    The noise rule's two delegate cases need this status rather than the one above:
    `daemon.reviews_gate` and `daemon.answers_question` are registered for
    `waiting_input` only, and they are the actors that make those waits correct.
    """
    wo = store.create_work_order("asked the delegate something")
    store.update_work_order(wo["id"], session_id="sess-1")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "done", result="waiting on the gate")
    store.set_status(wo["id"], "waiting_input")
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


def test_a_settled_turn_on_a_row_no_pass_selects_is_parked_once_the_threshold_passes(
        project):
    """GitHub issue 100's half of this suite, on the row the derivation can see: the turn
    is done, nobody is coming, and once the threshold passes the user is told so."""
    store = ProjectStore(project)
    wo = _parked(store)

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
    """The next turn is already written down; the daemon delivers it.

    THE STATUS AND THE SESSION ID ARE BOTH THE PREMISE, not decoration.
    `Daemon.deliver_messages` is registered for the statuses it sweeps, and
    `worker_session.delivery_hold` holds a message on an order with no session (nothing
    carries one into a dispatch prompt) — so without either, delivery is NOT the actor and
    the row is parked with a message behind it, which `true_blockers` reports as
    MESSAGE_STUCK_BLOCKER. The same note `tests/test_scheduled_actor.py:534` makes.
    """
    store = ProjectStore(project)
    wo = _waiting_on_a_delegate(store)
    store.queue_message(wo["id"], "carry on")
    wo = store.get_work_order(wo["id"])

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


def test_an_idle_manager_is_never_parked(project):
    """A project manager is idle BY DESIGN between its feature's messages — flagging one
    would put a permanent second line on every feature order in the fleet.

    The SILENCE IS NOW BOUGHT BY THE VERDICT (§5a): `ops.manager_awaiting_feature` says
    `Daemon.deliver_envelopes` still routes to this row, which is a POSITIVE actor rather
    than an absence — so the LIVE FEATURE is the premise, where the status used to carry
    it by being absent from PARKABLE_STATUSES (issue #264). `idle` is only ever written to
    a feature's manager, so a row without one was never the shape this test is about.
    """
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
    """The other half, and the reason the kind carve-out had to go. A manager reaches
    `waiting_input` only by ASKING for something; one sitting there with a finished turn
    and nothing out has had its wait end without anything restarting it, and no reconciler
    will notice. The carve-out silenced exactly that."""
    store = ProjectStore(project)
    wo = store.create_work_order("manage the feature", kind="manager")
    store.set_status(wo["id"], "waiting_input")
    turn = store.create_turn(wo["id"], "dispatch", "manage it")
    store.finish_turn(turn["id"], "done", result="nothing to do")

    assert parked_reason(store, store.get_work_order(wo["id"]),
                         now=_later(store, wo["id"])) is not None


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
    """INV-ATTENTION-MISSING must select `running` for this: a status it skips has its
    blocker derived correctly and then never surfaced."""
    store = ProjectStore(project)
    restore = _rewind(monkeypatch, LONG_ENOUGH)
    wo = _parked(store)
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
    wo = _parked(store)
    restore()

    violations = check_project(store, repair=False)

    assert [v.invariant for v in violations] == ["INV-ATTENTION-MISSING"]
    assert store.get_work_order(wo["id"])["needs_attention"] == 0


def test_jarvis_status_names_the_parked_order_and_what_it_is_parked_on(
        project, catalog_file, monkeypatch):
    store = ProjectStore(project)
    restore = _rewind(monkeypatch, LONG_ENOUGH)
    parked = _parked(store)
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
    ordinary = _parked(store, title="nothing to add")
    restore()
    check_project(store)  # the reconciler is what puts the flag on `ordinary`
    store.close()
    ops.start_os(str(catalog_file), foreground=True)
    client = TestClient(create_app(), follow_redirects=False)

    def second_blocker(text: str) -> str:
        # The added paragraph exactly. Matched as markup rather than by counting the
        # bare sentence, which also appears in the `attention` timeline event below it.
        return f'<p class="st wrap tone-warn"><span class="i">◭</span>{text}</p>'

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
    # ...and what is left is the line the THRESHOLD does not govern: the alarms switch
    # turns off "it has been quiet for an hour", never "no pass selects this row" (§5b —
    # the hole is a fact about the row, with no clock in it).
    assert true_blockers(store, wo, now=_later(store, wo["id"])) == [
        invariants.NO_ACTOR_BLOCKER.format(status="waiting_pr_merge")]


def test_the_parked_reason_does_not_move_with_the_clock(project):
    """It is compared against `attention_reason` and stored by `ack_attention`, so a
    reason carrying an elapsed time would be rewritten every tick and never stick."""
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
    """The supervisor's gated remedy is the OS's one path into a worker's inbox
    (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §5). This check
    only raises a flag — wo-a4bd6958's own case is the argument: the suite run it was
    waiting on died with its process, so "continue" would have resumed nothing.
    """
    store = ProjectStore(project)
    wo = _parked(store)
    check_project(store)

    assert store.queued_messages(wo["id"]) == []
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


# -- 5a: the verdict is the predicate --------------------------------------------------
# docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md §5a.


def test_a_settled_work_order_is_never_asked_who_owns_it(project):
    """The gate is `OPEN_STATUSES`, not `PARKABLE_STATUSES`, and it has to be: three
    callers outside `true_blockers` pass rows in ANY status, and `ops.SCHEDULED_ACTORS`
    is keyed by exactly the nine open ones — so a settled row would raise `KeyError`."""
    store = ProjectStore(project)
    wo = _parked(store)
    store.set_status(wo["id"], "completed")

    assert parked_reason(store, store.get_work_order(wo["id"]),
                         now=_later(store, wo["id"])) is None


def test_it_never_asks_ops_waiting_on(project, monkeypatch):
    """§5a: cutting the `parked_reason` to `waiting_on` edge is what lets section 6
    rebase `waiting_on` on the same verdict without a cycle. A call coming back here is
    that cycle, whatever answer it returns."""
    store = ProjectStore(project)
    wo = _parked(store)
    monkeypatch.setattr(ops, "waiting_on",
                        lambda *a, **k: pytest.fail("parked_reason asked waiting_on"))

    parked_reason(store, wo, now=_later(store, wo["id"]))


def test_the_caller_can_hand_over_a_verdict_it_already_derived(project):
    """The sentinel is NOT None, and that distinction is the argument's whole point: a
    caller that derived "nobody will act" passes None, and one that derived nothing at
    all leaves the default so this function derives it (Neo's ruling)."""
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
    """The other half of the GitHub issue 100 pin: the flag is one surface, and the
    command the flag sends the user to has to still do something when they get there.

    ITS OWN FIXTURE, and a `running` one: `ops.resume_in_auto` reads `ops.waiting_on`,
    which §5a did not touch and section 6 owns — so this half is pinned on the row the
    command is actually run against, not on the row the attention list now derives.
    """
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


# -- 2b: a head the OS has already judged is not an undelivered one ---------------------

#: The commit the passing round judged. wo-3312682f's shape: the head never moved.
JUDGED = "1f2e3d4c5b6a7980112233445566778899aabbcc"


def _judged_then_replied(store: ProjectStore, monkeypatch, *, head: str,
                         judged: str) -> dict:
    """wo-3312682f: delivered, judged green, then a reply turn the OS itself opened.

    The reply is a GATE DISMISSAL, which is not a PR repair — so `PR_REPAIR_SOURCES`
    cannot excuse it and only head equality can.
    """
    restore = _rewind(monkeypatch, 4 * LONG_ENOUGH)
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
    restore()
    return store.get_work_order(wo["id"])


def test_a_reply_turn_on_an_already_judged_head_is_not_a_stale_finish(project,
                                                                     monkeypatch):
    """Spec §2b. The head the panel passed IS the head GitHub reports, so nothing is
    undelivered — whatever opened the turn."""
    store = ProjectStore(project)
    wo = _judged_then_replied(store, monkeypatch, head=JUDGED, judged=JUDGED)

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None


def test_a_round_that_recorded_no_commit_is_no_excuse(project, monkeypatch):
    """Spec §2b: an EMPTY head is never read as equal. Pre-0.10.0 rounds recorded none,
    so the check degrades to exactly what it did before."""
    store = ProjectStore(project)
    wo = _judged_then_replied(store, monkeypatch, head="", judged="")

    assert parked_reason(store, wo, now=_later(store, wo["id"])) == STALE_FINISH_BLOCKER


def test_a_head_that_moved_past_the_judged_commit_still_reports_the_stale_finish(
        project, monkeypatch):
    """The worker pushed after the round passed: that IS something undeclared."""
    store = ProjectStore(project)
    wo = _judged_then_replied(store, monkeypatch, head="0" * 40, judged=JUDGED)

    assert parked_reason(store, wo, now=_later(store, wo["id"])) == STALE_FINISH_BLOCKER


def test_the_merged_head_excuses_it_too(project, monkeypatch):
    """The other member of the judged set: the newest `pr_merged` event's `head_oid`."""
    store = ProjectStore(project)
    wo = _judged_then_replied(store, monkeypatch, head=JUDGED, judged="")
    store.add_event(wo["id"], "pr_merged", {"head_oid": JUDGED})

    assert parked_reason(store, wo, now=_later(store, wo["id"])) is None
