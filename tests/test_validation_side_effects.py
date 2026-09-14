"""The round machine's empty guards, end to end. Issue #200, spec 2026-09-12 §5.

These drive the real `Daemon._validate_work_order` through a real `ops.finish`, because
the defect was never in one function: the packet said "no files" honestly and the guard
read that as "nothing delivered". Only the whole path shows it.
"""

from __future__ import annotations

from jarvis import ops
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish, passed, rejected)

PR = "https://github.com/x/y/pull/30"


def _round(fleet, wo_id):
    store = fleet.store()
    try:
        return dict(store.validation_rounds(wo_id=wo_id)[-1])
    finally:
        store.close()


# ------------------------------------------------------- the guard, before and after


def test_a_submission_with_nothing_at_all_is_still_escalated_unjudged(fleet):
    """The negative control, and the half of the guard that must NOT move: a reviewer
    handed nothing will rubber-stamp it, and that single silent pass would make the
    whole panel theatre."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch()  # no change to the worktree at all
    finish(fleet, wo["id"])
    fleet.drain()

    assert seen.calls == [], "a reviewer was handed an empty submission"
    rnd = _round(fleet, wo["id"])
    assert rnd["outcome"] == "escalated"
    assert "nothing to review" in rnd["reason"]


def test_a_knowledge_only_submission_reaches_the_panel(fleet):
    """Issue #200's live case, in one test: wo-28405ea1 retracted a fleet-wide
    instruction, and the packet the panel never saw said it had changed nothing."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch()
    old = ops.learn_add("always call /snap/bin/gh", wo_id="wo-somebody-else")
    ops.learn_retract(old["id"], "it breaks gate-grant matching", wo_id=wo["id"])
    ops.learn_add("call plain `gh`", wo_id=wo["id"])

    finish(fleet, wo["id"])
    fleet.drain()

    assert len(seen.calls) == 1, "the panel was not asked to judge the retraction"
    packet = seen.calls[0]["packet"]
    assert packet.files == ()
    assert {e["kind"] for e in packet.side_effects} == {"knowledge_retracted",
                                                        "knowledge_added"}
    assert _round(fleet, wo["id"])["outcome"] == "passed"


def test_the_retracted_text_reaches_the_seats(fleet):
    """A summary line is not enough to judge a retraction on — the question is whether
    the text that was retired deserved to be."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch()
    old = ops.learn_add("the text under judgement", wo_id="wo-author")
    ops.learn_retract(old["id"], "superseded", wo_id=wo["id"])
    finish(fleet, wo["id"])
    fleet.drain()

    from jarvis import validation
    prompt = validation.build_packet_prompt(seen.calls[0]["packet"])
    assert "the text under judgement" in prompt
    assert "NO DIFF CAN SHOW" in prompt


# --------------------------------------------------------------- the repeat guard

def test_two_rounds_retracting_different_entries_are_both_judged(fleet):
    """The guard one step past the one issue #200 named: without `side_effects_sha` in
    the fingerprint, round 2 escalates as "identical to round 1" and the false
    escalation simply moves one guard along (spec §5)."""
    seen = Validator(rejected("try again"), passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch()
    one = ops.learn_add("first", wo_id="wo-author")
    ops.learn_retract(one["id"], "superseded", wo_id=wo["id"])
    finish(fleet, wo["id"])
    fleet.drain()
    assert _round(fleet, wo["id"])["outcome"] == "rejected"
    fleet.tick()  # deliver the feedback, so the work order is running again

    two = ops.learn_add("second", wo_id="wo-author")
    ops.learn_retract(two["id"], "also superseded", wo_id=wo["id"])
    finish(fleet, wo["id"])
    fleet.drain()

    assert len(seen.calls) == 2, "round 2 was escalated as a repeat"
    assert _round(fleet, wo["id"])["outcome"] == "passed"


def test_resubmitting_the_identical_side_effects_is_still_caught_as_a_repeat(fleet):
    """The other half. Widening the hash must not switch the repeat guard off."""
    seen = Validator(rejected("try again"), passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch()
    row = ops.learn_add("the only thing this did", wo_id="wo-author")
    ops.learn_retract(row["id"], "superseded", wo_id=wo["id"])
    finish(fleet, wo["id"])
    fleet.drain()
    fleet.tick()  # deliver the feedback, so the work order is running again

    finish(fleet, wo["id"])  # nothing new at all
    fleet.drain()

    assert len(seen.calls) == 1, "an identical resubmission was judged again"
    assert "identical to round 1" in _round(fleet, wo["id"])["reason"]
