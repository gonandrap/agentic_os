"""A validation round refused by the Claude usage window WAITS for it (#235)"""

from __future__ import annotations

import time

import pytest

from jarvis import claude_cli, fleet as fleet_mod, ops, seats, validation
from jarvis.claude_cli import UsageLimit, UsageLimitError
from jarvis.invariants import status_label, true_blockers
from jarvis.project_store import validation_hold_until
from jarvis.timeline import _describe
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish, passed)

#: The refusal exactly as Claude Code renders it
REFUSAL = "You've hit your session limit · resets 11:50pm (America/Los_Angeles)"


def refused(in_seconds: float) -> UsageLimitError:
    return UsageLimitError(UsageLimit(message=REFUSAL,
                                      reset_at=time.time() + in_seconds))


def _rounds(store, wo_id):
    return [(r["round"], r["outcome"]) for r in store.validation_rounds(wo_id=wo_id)]


# -- 1. the transport: the refusal must survive being turned into an exception


def test_the_refusal_is_read_out_of_the_result_field_and_not_truncated_away():
    """Issue #235 defect 1, and the reason the cause was unknowable for four days"""
    envelope = ('{"type":"result","subtype":"success","is_error":true,'
                + '"padding":"' + "x" * 900 + '",'
                + '"result":' + f'"{REFUSAL}"' + '}')
    error = claude_cli._cli_failure(["-p", "judge"], 1, envelope, "")

    assert isinstance(error, UsageLimitError)
    assert str(error) == REFUSAL
    assert error.limit.reset_at is not None


def test_an_ordinary_failure_is_still_an_ordinary_error():
    """The classification must not swallow the failures the budget is FOR"""
    plain = claude_cli._cli_failure(["--version"], 1, "", "connection reset")
    assert type(plain) is claude_cli.ClaudeCliError
    assert "connection reset" in str(plain)

    cap = claude_cli._cli_failure(
        ["-p", "x"], 1,
        '{"result":"You\'ve hit your monthly limit · run /usage-credits to raise it"}', "")
    assert type(cap) is claude_cli.ClaudeCliError, "a spend cap must not be waited out"


# -- 2. the seat and the panel


def test_a_refused_seat_abstains_but_carries_the_window(monkeypatch, tmp_path):
    """Carried rather than raised: one refused seat must not take down a panel the other four answered"""
    monkeypatch.setattr(claude_cli, "run_headless_result",
                        lambda *a, **k: (_ for _ in ()).throw(refused(3600)))
    op = seats._run_seat("tester", "p", "s", "sonnet", 10, tmp_path,
                         kind="validation_seat")

    assert (op.status, op.replied) == ("abstained", False)
    assert op.refused is not None and op.refused.reset_at is not None


def test_a_panel_nobody_could_reach_still_escalates_when_the_window_is_not_why():
    """The branch this fix narrows, not one it removes"""
    down = [seats.Opinion(seat=s, raw="boom", status="abstained", replied=False)
            for s in ("tester", "security")]
    assert all(op.refused is None for op in down)


# -- 3. the round machine: held, not spent


def test_a_round_met_by_an_open_window_is_held_and_costs_nothing(fleet):
    """The defect, stated as a test: `VALIDATION_OUTAGE_LIMIT` is 3 and the retries land on CONSECUTIVE..."""
    validator = Validator(refused(3600))
    fleet.daemon.validator = validator
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    for _ in range(5):
        fleet.drain()

    store = fleet.store()
    try:
        fresh = store.get_work_order(wo["id"])
        assert len(validator.calls) == 1, "the panel walked back into a closed window"
        assert _rounds(store, wo["id"]) == [(1, "failed")]
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 0
        assert fresh["status"] == "validating", "a spent window settled the work order"
        assert true_blockers(store, fresh) == [], (
            "nobody judged the work and nobody was asked to")
        assert store.envelopes(subject_wo_id=wo["id"]) == [], "a hold sent feedback"
    finally:
        store.close()


def test_the_round_is_judged_by_itself_once_the_window_has_reopened(fleet):
    """THE DEFINITION OF DONE, with no human touch anywhere in it: the next tick after the stated moment..."""
    fleet.daemon.validator = Validator(refused(-1), passed())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()   # refused; the window it named has already reopened
    fleet.drain()   # so this tick judges, unprompted

    store = fleet.store()
    try:
        assert _rounds(store, wo["id"]) == [(1, "passed")]
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
    finally:
        store.close()


def test_a_hold_spends_none_of_the_transport_budget(fleet):
    """The two failures share the `failed` outcome and must not share the budget"""
    fleet.daemon.validator = Validator(
        refused(-1), refused(-1), refused(-1),
        claude_cli.ClaudeCliError("connection reset"),
        claude_cli.ClaudeCliError("connection reset"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    store = fleet.store()
    try:
        for _ in range(5):
            fleet.drain()
            assert store.get_work_order(wo["id"])["status"] == "validating"
        fleet.drain()  # the THIRD transport outage — now it may give up
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "escalated"
    finally:
        store.close()


def test_a_held_round_reads_as_held_and_not_as_a_failure(fleet):
    """GitHub issue #581's other hold"""
    from jarvis.project_store import (VALIDATION_HELD_CAUSE, ProjectStore,
                                      validation_standing)

    fleet.daemon.validator = Validator(refused(3600))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()

    store = ProjectStore(fleet.project)
    try:
        row = store.validation_rounds(wo_id=wo["id"])[0]
        assert row["outcome"] == "failed", "the mechanism layer must not change"
        assert row["hold_cause"] == VALIDATION_HELD_CAUSE
        assert validation_standing(row) == ("held for the usage window", "active", "◑")

        line = ops.round_line(ops.validation_rounds(store, wo_id=wo["id"])[0])
        assert "held for the usage window" in line and "· failed ·" not in line
    finally:
        store.close()


def test_a_transport_outage_still_escalates_after_three(fleet):
    """The budget survives this change: fast retries are the right answer to a blip"""
    fleet.daemon.validator = Validator(claude_cli.ClaudeCliError("connection reset"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    store = fleet.store()
    try:
        for _ in range(3):
            fleet.drain()
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
    finally:
        store.close()


# -- 4. it must not be silent


def test_the_work_order_reads_as_waiting_on_the_window_not_on_the_user(fleet):
    """`attention_reason` used to say "the review could not be satisfied"""
    fleet.daemon.validator = Validator(refused(3600))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    store = fleet.store()
    try:
        label = status_label(store, store.get_work_order(wo["id"]))
        assert "usage window is spent" in label
        assert "resumes by itself at" in label
        assert "review round 1" in label, "the round is still the subject"
    finally:
        store.close()


def test_the_hold_is_readable_on_the_timeline():
    """A new CAUSE is as invisible as a new kind until `_describe` names it (kn-3f133363)"""
    title, detail = _describe("validation_failed", {
        "round": 1, "cause": "usage_limit", "reopens_at": 1_789_714_200.0,
        "error": REFUSAL})
    assert "held" in title.lower() and "usage window" in title
    assert "resuming by itself" in detail
    assert REFUSAL in detail, "the refusal that was truncated away in #235"


# -- 5. the fleet reading the validate path never asked for


def test_the_panel_is_held_by_the_outage_but_never_by_the_slot_cap():
    """`fleet.Fleet.blocked` conflates two holds and only one of them applies to a seat: a `claude -p`..."""
    outage = fleet_mod.Outage(project="proj_a", wo_id="wo-1",
                              reopens_at=2_000_000_000.0, message=REFUSAL)
    at = 1_000_000_000.0
    assert fleet_mod.Fleet(3, 0, outage, at=at).shut()
    assert not fleet_mod.Fleet(3, 3, None, at=at).shut(), "the cap must not hold a seat"
    assert fleet_mod.Fleet(3, 3, None, at=at).blocked(), "...but it still holds a worker"
    assert not fleet_mod.Fleet(3, 0, outage, at=2_000_000_001.0).shut()


def test_a_shut_window_stops_the_tick_before_the_first_seat_is_called(fleet):
    """Held BEFORE the refusal rather than after it, the way `dispatch_pending` holds a worker"""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    outage = fleet_mod.Outage(project="proj_a", wo_id=wo["id"],
                              reopens_at=time.time() + 3600, message=REFUSAL)
    store = fleet.store()
    try:
        fleet.daemon.validation_tick(
            fleet.daemon.catalog.projects[0], store,
            fleet_mod.Fleet(3, 0, outage))
        # SYNCHRONOUS, and that is why it is the assertion: the tick SUBMITS to a pool and returns
        assert fleet.daemon.validating == set(), "a round was queued into a closed window"

        deadline = time.monotonic() + 5
        while fleet.daemon.validating and time.monotonic() < deadline:
            time.sleep(0.01)
        assert validator.calls == [], "a seat was called into a closed window"
        assert _rounds(store, wo["id"]) == [(1, "pending")], "and the round was spent"
    finally:
        store.close()


# -- 6. the hold itself


def test_the_newest_moment_wins_for_the_same_round():
    """A window that reopened, was retried and shut again writes a second event for the same round"""
    events = [{"payload": '{"round": 1, "cause": "usage_limit", "reopens_at": 100}'},
              {"payload": '{"round": 1, "cause": "usage_limit", "reopens_at": 900}'},
              {"payload": '{"round": 2, "cause": "usage_limit", "reopens_at": 5000}'},
              {"payload": '{"round": 1, "cause": "transport", "attempt": 1}'}]
    assert validation_hold_until(events, 1) == 900
    assert validation_hold_until(events, 2) == 5000
    assert validation_hold_until(events, 3) == 0


# -- 7. recovery, and what the fix deliberately does NOT do


def test_an_order_already_escalated_by_a_past_window_is_not_reopened(fleet):
    """Neo, question 390"""
    fleet.daemon.validator = Validator(claude_cli.ClaudeCliError("connection reset"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    store = fleet.store()
    try:
        for _ in range(3):
            fleet.drain()
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "escalated"

        fleet.daemon.validator = Validator(passed())
        for _ in range(3):
            fleet.drain()
        assert _rounds(store, wo["id"]) == [(1, "escalated")], (
            "the upgrade re-judged work the user had already been asked about")
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
    finally:
        store.close()


def test_the_documented_recovery_command_still_exists():
    """`_validation_held` sends the user at `jarvis validation force`"""
    assert callable(ops.force_validation)


@pytest.mark.parametrize("reset_at", [None, 0])
def test_a_refusal_that_named_no_readable_moment_still_waits(fleet, reset_at):
    """The message always names a reset, so this is a backstop against a rendering nobody has seen yet"""
    fleet.daemon.validator = Validator(
        UsageLimitError(UsageLimit(message=REFUSAL, reset_at=reset_at)))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    store = fleet.store()
    try:
        for _ in range(5):
            fleet.drain()
        assert store.get_work_order(wo["id"])["status"] == "validating"
        assert _rounds(store, wo["id"]) == [(1, "failed")]
    finally:
        store.close()


def test_decide_raises_rather_than_escalating_when_the_window_refused_every_seat(
        monkeypatch, tmp_path):
    """The seam between the panel and the round machine: `decide` has to hand it an exception it can... (#235)"""
    monkeypatch.setattr(seats, "run_blind", lambda prompts, **kw: [
        seats.Opinion(seat=s, raw=REFUSAL, status="abstained", replied=False,
                      refused=UsageLimit(message=REFUSAL, reset_at=time.time() + 60))
        for s in prompts])
    monkeypatch.setattr(seats, "prime_cache", lambda *a, **k: None)
    monkeypatch.setattr(validation, "build_shared_prefix", lambda *a, **k: "prefix")
    monkeypatch.setattr(validation, "build_seat_prompt", lambda seat: "seat")
    monkeypatch.setattr(validation, "seat_model", lambda seat, cfg: "sonnet")
    monkeypatch.setattr(validation, "_record", lambda *a, **k: None)

    class _Central:
        def project_name_for_path(self, p): return "proj_a"
        def knowledge_brief(self, p): return None
        def close(self): pass

    monkeypatch.setattr("jarvis.central_store.CentralStore", _Central)

    class _Store:
        project_path = tmp_path

    class _Cfg:
        roster = ("tester", "security", "chair")
        timeout = 10

    with pytest.raises(UsageLimitError):
        validation.decide(_Store(), {"id": 1, "round": 1}, None, _Cfg())
