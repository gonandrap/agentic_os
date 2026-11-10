"""The round machine's empty guards, end to end (#200)"""

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
    """The negative control, and the half of the guard that must NOT move: a reviewer handed nothing will..."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch()  # no change to the worktree at all
    finish(fleet, wo["id"])
    fleet.drain()

    assert seen.calls == [], "a reviewer was handed an empty submission"
    rnd = _round(fleet, wo["id"])
    assert rnd["outcome"] == "escalated"
    assert "nothing to review" in rnd["reason"]


def test_a_no_change_submission_waiting_on_the_user_names_the_command(fleet):
    """#1352: wo-b677d9e4 escalated as 'nothing to review' with its `jarvis config set` buried"""
    from jarvis import invariants

    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch()
    command = "jarvis config set shared_schedule rules.enabled true"
    ops.finish(wo["id"], "only the user may set it", pr_url=PR,
               evidence="read both catalogs", needs_user=command)
    fleet.drain()

    assert seen.calls == []
    rnd = _round(fleet, wo["id"])
    assert rnd["outcome"] == "escalated"
    assert command in rnd["reason"]
    store = fleet.store()
    try:
        row = store.get_work_order(wo["id"])
        assert command in row["attention_reason"]
        assert row["attention_reason"] == invariants.true_blockers(store, row)[0]
        assert ops.escalation_cause(store, wo_id=wo["id"], round_id=rnd["id"]) == \
            ops.ESCALATION_USER_ACTION
    finally:
        store.close()


FILES = [{"path": "app.py", "additions": 1, "deletions": 0}]
CI_RUNNING = [{"name": "unit (3.11)", "status": "IN_PROGRESS", "conclusion": ""}]
CI_GREEN = [{"name": "unit (3.11)", "status": "COMPLETED", "conclusion": "SUCCESS"}]


def test_a_draft_waiting_on_the_user_parks_before_ci_and_the_panel(fleet, fake_gh):
    """#1684: wo-d614af29's draft PR sat in `validating` with its `--needs-user` dropped"""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    fake_gh.set_pr_artifact(PR, checks=CI_RUNNING, files=FILES, diff="+x = 1",
                            draft=True)
    command = "jarvis wo send wo-x 'commented'"
    result = ops.finish(wo["id"], "probe half done", pr_url=PR,
                        evidence="ran the probe", needs_user=command)
    assert command in result["needs_user"] and "draft" in result["needs_user"]
    fleet.drain()

    assert seen.calls == []
    rnd = _round(fleet, wo["id"])
    assert rnd["outcome"] == "escalated"
    assert command in rnd["reason"]
    store = fleet.store()
    try:
        row = store.get_work_order(wo["id"])
        assert row["status"] == "needs_review" and command in row["attention_reason"]
        assert ops.user_action_path(store, row) == \
            f"`{command}`: parked for you before review"
    finally:
        store.close()


def test_a_ready_pull_request_waiting_on_the_user_is_judged_and_says_so(fleet, fake_gh):
    """#1506's post-merge park stays; the finish names the command and the path (#1684)"""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    fake_gh.set_pr_artifact(PR, checks=CI_GREEN, files=FILES, diff="+x = 1")
    command = "jarvis config set proj_a rules.enabled true"
    result = ops.finish(wo["id"], "flag it on after merge", pr_url=PR,
                        evidence="ran pytest", needs_user=command)
    assert "after it merges" in result["needs_user"] and command in result["needs_user"]
    fleet.drain()

    assert len(seen.calls) == 1
    assert _round(fleet, wo["id"])["outcome"] == "passed"


def test_a_knowledge_only_submission_reaches_the_panel(fleet):
    """Issue #200's live case, in one test: wo-28405ea1 retracted a fleet-wide instruction"""
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
    """A summary line is not enough to judge a retraction on"""
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
    """The guard one step past the one issue #200 named: without `side_effects_sha` in the fingerprint"""
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
    """The other half"""
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
