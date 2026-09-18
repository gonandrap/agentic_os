"""A validation round refused by the Claude usage window WAITS for it. GitHub issue #235.

The live failure is wo-752eced8 round 3: five seats refused free and instantly, the round
closed "nobody could be reached to review this submission", and the work order sat in
`needs_review` for eleven hours after the window had reopened. Workers already survive
that same window (`worker_session.turn_pause`); the panel did not, and the difference was
one read.

These drive the REAL `Daemon._validate_work_order` and the real `validation.decide` — the
defect spanned four modules (the transport's error classification, the seat, the panel's
verdict and the round machine) and a test of any one of them alone would have passed
throughout.
"""

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

#: The refusal exactly as Claude Code renders it — assembled from its own label table,
#: never written (kn-f6f418a9). The words are scenery; `reset_at` is the whole signal.
REFUSAL = "You've hit your session limit · resets 11:50pm (America/Los_Angeles)"


def refused(in_seconds: float) -> UsageLimitError:
    return UsageLimitError(UsageLimit(message=REFUSAL,
                                      reset_at=time.time() + in_seconds))


def _rounds(store, wo_id):
    return [(r["round"], r["outcome"]) for r in store.validation_rounds(wo_id=wo_id)]


# -- 1. the transport: the refusal must survive being turned into an exception ---------


def test_the_refusal_is_read_out_of_the_result_field_and_not_truncated_away():
    """Issue #235 defect 1, and the reason the cause was unknowable for four days.

    The envelope's preamble is longer than the 500-char cap the old code applied to raw
    stdout, so a message read from the far end of it is the regression: every seat on
    2026-09-13 stored `…"speed":"standard"},"` and the `result` field behind it was lost.
    """
    envelope = ('{"type":"result","subtype":"success","is_error":true,'
                + '"padding":"' + "x" * 900 + '",'
                + '"result":' + f'"{REFUSAL}"' + '}')
    error = claude_cli._cli_failure(["-p", "judge"], 1, envelope, "")

    assert isinstance(error, UsageLimitError)
    assert str(error) == REFUSAL
    assert error.limit.reset_at is not None


def test_an_ordinary_failure_is_still_an_ordinary_error():
    """The classification must not swallow the failures the budget is FOR. A spend cap
    is the case that matters most: it renders no reset, so it must never be held."""
    plain = claude_cli._cli_failure(["--version"], 1, "", "connection reset")
    assert type(plain) is claude_cli.ClaudeCliError
    assert "connection reset" in str(plain)

    cap = claude_cli._cli_failure(
        ["-p", "x"], 1,
        '{"result":"You\'ve hit your monthly limit · run /usage-credits to raise it"}', "")
    assert type(cap) is claude_cli.ClaudeCliError, "a spend cap must not be waited out"


# -- 2. the seat and the panel --------------------------------------------------------


def test_a_refused_seat_abstains_but_carries_the_window(monkeypatch, tmp_path):
    """Carried rather than raised: one refused seat must not take down a panel the other
    four answered — that call belongs to `validation.decide`."""
    monkeypatch.setattr(claude_cli, "run_headless_result",
                        lambda *a, **k: (_ for _ in ()).throw(refused(3600)))
    op = seats._run_seat("tester", "p", "s", "sonnet", 10, tmp_path)

    assert (op.status, op.replied) == ("abstained", False)
    assert op.refused is not None and op.refused.reset_at is not None


def test_a_panel_nobody_could_reach_still_escalates_when_the_window_is_not_why():
    """The branch this fix narrows, not one it removes. A panel that is genuinely down
    is still a human's problem, and silence is still not a pass."""
    down = [seats.Opinion(seat=s, raw="boom", status="abstained", replied=False)
            for s in ("tester", "security")]
    assert all(op.refused is None for op in down)


# -- 3. the round machine: held, not spent --------------------------------------------


def test_a_round_met_by_an_open_window_is_held_and_costs_nothing(fleet):
    """The defect, stated as a test: `VALIDATION_OUTAGE_LIMIT` is 3 and the retries land
    on CONSECUTIVE TICKS, so the whole budget used to be spent in about fifteen seconds
    against an outage the inbox measured at 11.2 hours.

    Five drains is well past the old budget. The validator being called ONCE is the
    assertion that matters — every further tick used to re-enter the same closed window.
    """
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
    """THE DEFINITION OF DONE, with no human touch anywhere in it: the next tick after
    the stated moment judges the same round, and the submitter spends round ONE."""
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
    """The two failures share the `failed` outcome and must not share the budget. Three
    holds then three genuine outages: if a hold counted, the third outage would arrive
    with the budget already gone and this would escalate a round early."""
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


def test_a_transport_outage_still_escalates_after_three(fleet):
    """The budget survives this change: fast retries are the right answer to a blip."""
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


# -- 4. it must not be silent ---------------------------------------------------------


def test_the_work_order_reads_as_waiting_on_the_window_not_on_the_user(fleet):
    """`attention_reason` used to say "the review could not be satisfied — the work needs
    your judgement", which was false twice over. The status line has to name the window
    and the moment instead."""
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
    """A new CAUSE is as invisible as a new kind until `_describe` names it (kn-3f133363),
    and this one must not read as either of the two it sits beside."""
    title, detail = _describe("validation_failed", {
        "round": 1, "cause": "usage_limit", "reopens_at": 1_789_714_200.0,
        "error": REFUSAL})
    assert "held" in title.lower() and "usage window" in title
    assert "resuming by itself" in detail
    assert REFUSAL in detail, "the refusal that was truncated away in #235"


# -- 5. the fleet reading the validate path never asked for ---------------------------


def test_the_panel_is_held_by_the_outage_but_never_by_the_slot_cap():
    """`fleet.Fleet.blocked` conflates two holds and only one of them applies to a seat:
    a `claude -p` seat is not a worker turn and does not count against `max_in_flight`."""
    outage = fleet_mod.Outage(project="proj_a", wo_id="wo-1",
                              reopens_at=2_000_000_000.0, message=REFUSAL)
    at = 1_000_000_000.0
    assert fleet_mod.Fleet(3, 0, outage, at=at).shut()
    assert not fleet_mod.Fleet(3, 3, None, at=at).shut(), "the cap must not hold a seat"
    assert fleet_mod.Fleet(3, 3, None, at=at).blocked(), "...but it still holds a worker"
    assert not fleet_mod.Fleet(3, 0, outage, at=2_000_000_001.0).shut()


def test_a_shut_window_stops_the_tick_before_the_first_seat_is_called(fleet):
    """Held BEFORE the refusal rather than after it, the way `dispatch_pending` holds a
    worker — otherwise every project pays one refused round to learn what the tick's own
    fleet reading already knew."""
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
        # SYNCHRONOUS, and that is why it is the assertion: the tick SUBMITS to a pool
        # and returns, so `validator.calls` alone is empty either way for a moment and
        # would pass against a tick that had just queued the round.
        assert fleet.daemon.validating == set(), "a round was queued into a closed window"

        deadline = time.monotonic() + 5
        while fleet.daemon.validating and time.monotonic() < deadline:
            time.sleep(0.01)
        assert validator.calls == [], "a seat was called into a closed window"
        assert _rounds(store, wo["id"]) == [(1, "pending")], "and the round was spent"
    finally:
        store.close()


# -- 6. the hold itself ---------------------------------------------------------------


def test_the_newest_moment_wins_for_the_same_round():
    """A window that reopened, was retried and shut again writes a second event for the
    same round. Taking the earlier moment would walk straight back into the new window."""
    events = [{"payload": '{"round": 1, "cause": "usage_limit", "reopens_at": 100}'},
              {"payload": '{"round": 1, "cause": "usage_limit", "reopens_at": 900}'},
              {"payload": '{"round": 2, "cause": "usage_limit", "reopens_at": 5000}'},
              {"payload": '{"round": 1, "cause": "transport", "attempt": 1}'}]
    assert validation_hold_until(events, 1) == 900
    assert validation_hold_until(events, 2) == 5000
    assert validation_hold_until(events, 3) == 0


# -- 7. recovery, and what the fix deliberately does NOT do ---------------------------


def test_an_order_already_escalated_by_a_past_window_is_not_reopened(fleet):
    """Neo, question 390. The escalation reached the inbox and flagged attention, so the
    user has seen it; re-judging behind their back could land and auto-merge a pull
    request nobody asked for. The recovery is `jarvis validation force`, on demand."""
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
    """`_validation_held` sends the user at `jarvis validation force`, which is the only
    thing that reopens a stranded order. A rename would leave that instruction pointing
    at nothing, and nothing else in the suite pairs the two."""
    assert callable(ops.force_validation)


@pytest.mark.parametrize("reset_at", [None, 0])
def test_a_refusal_that_named_no_readable_moment_still_waits(fleet, reset_at):
    """The message always names a reset, so this is a backstop against a rendering nobody
    has seen yet — but it has to be a WAIT, not a fall-through to the fifteen-second
    budget the whole issue is about."""
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
    """The seam between the panel and the round machine: `decide` has to hand it an
    exception it can classify, not the `escalated` verdict that made #235 terminal."""
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
