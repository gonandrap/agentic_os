"""A pull request whose checks have FINISHED RED is sent back before the panel sits (#1005)"""

from __future__ import annotations

import json

from jarvis import evidence as evidence_mod
from jarvis import ops
from jarvis.invariants import VALIDATION_STUCK_BLOCKER
from jarvis.project_store import VALIDATION_CI_CAUSE, ProjectStore
from jarvis.timeline import _describe
from tests.test_validation_ci_hold import FILES, GREEN, PENDING, RED, _packet
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish, passed, rejected)

#: One check red and one still running: the pull request has NOT finished reporting
MIXED = [{"name": "unit (3.11)", "status": "COMPLETED", "conclusion": "FAILURE"},
         {"name": "evals", "status": "IN_PROGRESS", "conclusion": ""}]


def _round(fleet, wo_id, index=-1):
    store = fleet.store()
    try:
        return dict(store.validation_rounds(wo_id=wo_id)[index])
    finally:
        store.close()


def _sent(store, wo_id) -> list[str]:
    return [m["content"] for m in store.list_messages(wo_id)
            if m["direction"] == "user_to_agent"]


def _red_return(fleet, fake_gh, checks=RED, **kw):
    """One red submission, returned"""
    wo = fleet.dispatch(**kw)
    fleet.change(wo["id"], "x = 1")
    pr = finish(fleet, wo["id"])["pr_url"]
    fake_gh.set_pr_artifact(pr, checks=checks, files=FILES, diff="+x = 1")
    fleet.drain()
    fleet.tick()  # the router delivers the envelope; the order goes back to running
    return wo, pr


# -- 1. the predicate


def test_a_finished_red_check_is_what_the_os_returns_for():
    assert evidence_mod.ci_failing(_packet(RED)) == ("unit (3.11)",)


def test_green_and_pending_and_the_two_non_answers_are_not_red():
    """`()` is "nothing to send back for", and it covers the two shapes that are not answers at all"""
    assert evidence_mod.ci_failing(_packet(GREEN)) == ()
    assert evidence_mod.ci_failing(_packet(PENDING)) == ()
    assert evidence_mod.ci_failing(_packet([])) == ()
    assert evidence_mod.ci_failing(_packet(None, source="worktree")) == ()


def test_the_red_predicate_is_githubs_and_is_not_re_derived():
    """`github.RED_CONCLUSIONS` stays the one definition of red in the OS (issue #224)"""
    from jarvis import github

    for conclusion in github.RED_CONCLUSIONS:
        assert evidence_mod.ci_failing(_packet(
            [{"name": "c", "status": "COMPLETED", "conclusion": conclusion}])) == ("c",)
    assert evidence_mod.ci_failing(_packet([{"status": "COMPLETED"}])) == ()


# -- 2. the return


def test_a_completed_red_pull_request_seats_nobody_and_spends_no_counted_round(
        fleet, fake_gh):
    """THE CLAIM OF THIS FILE"""
    never = Validator(passed())
    fleet.daemon.validator = never
    wo, _ = _red_return(fleet, fake_gh)

    assert never.calls == [], "a seat was asked to judge a pull request CI called red"
    store = fleet.store()
    try:
        rounds = store.validation_rounds(wo_id=wo["id"])
        assert [(r["round"], r["outcome"]) for r in rounds] == [(1, "rejected")]
        assert bool(rounds[0]["uncounted"]) is True
        assert rounds[0]["uncounted_cause"] == ops.RED_CI_CAUSE
        assert rounds[0]["hold_cause"] is None, "nothing is holding this round"
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 0
        assert store.validation_opinions(rounds[0]["id"]) == []
        assert store.get_work_order(wo["id"])["status"] == "running", (
            "the work order did not go back to its worker")
        assert store.get_work_order(wo["id"])["attention_reason"] in ("", None)
    finally:
        store.close()


def test_the_envelope_names_the_checks_and_points_at_the_logs(fleet, fake_gh):
    """A REFERENCE, NEVER A PAYLOAD"""
    fleet.daemon.validator = Validator(passed())
    wo, pr = _red_return(fleet, fake_gh)

    store = fleet.store()
    try:
        envelopes = store.envelopes(subject_wo_id=wo["id"])
        assert len(envelopes) == 1
        assert envelopes[0]["from_role"] == "reviewer"
        assert envelopes[0]["to_role"] == "implementor"

        texts = _sent(store, wo["id"])
        assert len(texts) == 1
        text = texts[0]
        assert "unit (3.11)" in text
        assert f"gh pr checks {pr}" in text
        assert "no round" in text.lower() and "round 1 of 3" not in text
        # The two legitimate remedies — issue 1025's gap 2 (spec §3.4, §6.2)
        assert "merge" in text.lower()
        # No log text: the fake CI reported none, and nothing may invent any
        assert "Traceback" not in text and "assert" not in text
        for forbidden in ("panel", "seat", "validator"):
            assert forbidden not in text.lower(), f"the worker was told about {forbidden}"

        reason = store.validation_rounds(wo_id=wo["id"])[0]["reason"]
        assert "unit (3.11)" in reason
        assert "gh pr checks" not in reason, "the instructions are the template's"
    finally:
        store.close()


def test_the_record_says_which_checks_it_returned_for(fleet, fake_gh):
    """GitHub's check list has moved on by the time anyone reads the record"""
    fleet.daemon.validator = Validator(passed())
    wo, _ = _red_return(fleet, fake_gh)

    store = fleet.store()
    try:
        [event] = store.events_of_kind(wo["id"], "validation_rejected")
        payload = json.loads(event["payload"])
        assert payload["uncounted"] is True
        assert payload["uncounted_cause"] == ops.RED_CI_CAUSE
        assert payload["round"] == 1
        assert payload["failing"] == ["unit (3.11)"]

        title, _ = _describe("validation_rejected", payload)
        assert "no round spent" in title
    finally:
        store.close()


# -- 3. the two worlds that are unchanged


def test_a_green_pull_request_still_reaches_the_seats(fleet, fake_gh):
    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    panel = Validator(passed())
    fleet.daemon.validator = panel
    pr = finish(fleet, wo["id"])["pr_url"]
    fake_gh.set_pr_artifact(pr, checks=GREEN, files=FILES, diff="+x = 1")

    fleet.drain()

    assert [c["round"] for c in panel.calls] == [1]
    assert _round(fleet, wo["id"])["outcome"] == "passed"


def test_one_red_check_beside_one_still_running_holds_rather_than_returns(
        fleet, fake_gh):
    """A pull request that has not finished reporting is a WAIT"""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    panel = Validator(passed())
    fleet.daemon.validator = panel
    pr = finish(fleet, wo["id"])["pr_url"]
    fake_gh.set_pr_artifact(pr, checks=MIXED, files=FILES, diff="+x = 1")

    fleet.drain()

    assert panel.calls == []
    row = _round(fleet, wo["id"])
    assert (row["outcome"], row["hold_cause"]) == ("failed", VALIDATION_CI_CAUSE)
    assert row["uncounted_cause"] in ("", None), "a hold is not a red-CI return"
    store = fleet.store()
    try:
        assert store.envelopes(subject_wo_id=wo["id"]) == []
    finally:
        store.close()


def test_a_lapsed_hold_on_a_partly_red_pull_request_is_judged_not_returned(
        fleet, fake_gh, monkeypatch):
    """After `CI_HOLD_DEADLINE_SECONDS` the fail-SAFE direction is the seats"""
    monkeypatch.setattr("jarvis.daemon.CI_HOLD_DEADLINE_SECONDS", -1.0)
    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    panel = Validator(passed())
    fleet.daemon.validator = panel
    pr = finish(fleet, wo["id"])["pr_url"]
    fake_gh.set_pr_artifact(pr, checks=MIXED, files=FILES, diff="+x = 1")

    fleet.drain()

    assert len(panel.calls) == 1
    assert _round(fleet, wo["id"])["uncounted_cause"] in ("", None)


def test_with_the_key_at_zero_a_red_head_reaches_the_seats_exactly_as_before(
        fleet, fake_gh):
    """`red_ci_returns: 0` is today's behaviour"""
    fleet.reconfigure(red_ci_returns=0)
    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    panel = Validator(rejected())
    fleet.daemon.validator = panel
    pr = finish(fleet, wo["id"])["pr_url"]
    fake_gh.set_pr_artifact(pr, checks=RED, files=FILES, diff="+x = 1")

    fleet.drain()

    assert len(panel.calls) == 1
    row = _round(fleet, wo["id"])
    assert (row["outcome"], bool(row["uncounted"])) == ("rejected", False)


# -- 4. the cap


def test_past_the_cap_the_seats_judge_the_red_head_and_it_escalates_as_before(
        fleet, fake_gh):
    """No new terminal state: the branch's condition goes false and the round continues as an ordinary..."""
    fleet.reconfigure(red_ci_returns=1, max_rounds=1)
    panel = Validator(rejected())
    fleet.daemon.validator = panel
    wo, pr = _red_return(fleet, fake_gh)
    assert panel.calls == []

    fleet.change(wo["id"], "x = 2")  # new work, still red
    finish(fleet, wo["id"], summary="tried again")
    fake_gh.set_pr_artifact(pr, checks=RED, files=FILES, diff="+x = 2")
    fleet.drain()

    assert len(panel.calls) == 1, "past the cap the red head was never judged"
    store = fleet.store()
    try:
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
        assert store.validation_rounds(wo_id=wo["id"])[-1]["outcome"] == "escalated"
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "needs_review"
        assert fresh["attention_reason"] == VALIDATION_STUCK_BLOCKER
    finally:
        store.close()


# -- 5. the guards


def test_an_unchanged_resubmission_after_a_return_is_exempt_until_the_cap(
        fleet, fake_gh):
    """THE EXEMPTION THIS FEATURE CANNOT SHIP WITHOUT (spec §5.1). wo-72c3d20b's branch was correct and... (#1023)"""
    fleet.reconfigure(red_ci_returns=2, max_rounds=3)
    panel = Validator(passed())
    fleet.daemon.validator = panel
    wo, pr = _red_return(fleet, fake_gh)

    # Unchanged resubmission, CI still red: returned again, nobody seated
    finish(fleet, wo["id"], summary="waited for the sibling")
    fleet.drain()
    fleet.tick()
    assert panel.calls == []
    rows = fleet.store()
    try:
        assert [r["uncounted_cause"] for r in rows.validation_rounds(wo_id=wo["id"])] \
            == [ops.RED_CI_CAUSE, ops.RED_CI_CAUSE]
        assert rows.counted_validation_rounds(wo_id=wo["id"]) == 0
    finally:
        rows.close()

    # Past the cap the repeat guard is live again on the same unchanged fingerprint
    finish(fleet, wo["id"], summary="and again")
    fleet.drain()

    assert panel.calls == [], "identical work reached a seat"
    store = fleet.store()
    try:
        last = store.validation_rounds(wo_id=wo["id"])[-1]
        assert last["outcome"] == "escalated"
        assert "identical to round 2" in last["reason"]
    finally:
        store.close()


def test_the_same_branch_once_ci_goes_green_is_judged_rather_than_refused(
        fleet, fake_gh):
    """The other half of the exemption"""
    panel = Validator(passed())
    fleet.daemon.validator = panel
    wo, pr = _red_return(fleet, fake_gh)

    fake_gh.set_pr_artifact(pr, checks=GREEN, files=FILES, diff="+x = 1")
    finish(fleet, wo["id"], summary="the sibling merged")
    fleet.drain()

    assert [c["round"] for c in panel.calls] == [2]
    assert _round(fleet, wo["id"])["outcome"] == "passed"


def test_a_submission_after_a_return_is_a_counted_round_and_not_promoted(
        fleet, fake_gh):
    """NO REBIND PROMOTION (spec §5.2)"""
    panel = Validator(rejected())
    fleet.daemon.validator = panel
    wo, pr = _red_return(fleet, fake_gh)

    fake_gh.set_pr_artifact(pr, checks=GREEN, files=FILES, diff="+x = 1")
    fleet.change(wo["id"], "x = 2")
    finish(fleet, wo["id"], summary="fixed the build")
    fleet.drain()

    row = _round(fleet, wo["id"])
    assert (row["round"], bool(row["uncounted"])) == (2, False)
    assert row["uncounted_cause"] in ("", None)
    store = fleet.store()
    try:
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
    finally:
        store.close()


def test_a_return_can_be_the_last_judged_round_and_bounces_nobody(fleet, fake_gh):
    """NO BOUNCE (spec §5.3)"""
    panel = Validator(rejected())
    fleet.daemon.validator = panel
    wo, pr = _red_return(fleet, fake_gh)

    store = fleet.store()
    try:
        judged = store.last_judged_round(wo_id=wo["id"])
        assert judged["uncounted_cause"] == ops.RED_CI_CAUSE
        assert store.validation_opinions(int(judged["id"])) == []
    finally:
        store.close()

    fake_gh.set_pr_artifact(pr, checks=GREEN, files=FILES, diff="+x = 1")
    fleet.change(wo["id"], "x = 2")
    finish(fleet, wo["id"], summary="fixed the build")
    fleet.drain()

    assert len(panel.calls) == 1, "the submission was bounced instead of judged"
    store = fleet.store()
    try:
        assert store.events_of_kind(wo["id"], "validation_bounced") == []
        assert ops.consecutive_bounces(store, wo["id"], 1) == 0
    finally:
        store.close()


def test_the_seats_see_the_return_as_history_and_can_tell_what_it_was(fleet, fake_gh):
    """`prior_round_history` filters on `COUNTED_VALIDATION_OUTCOMES`"""
    panel = Validator(passed())
    fleet.daemon.validator = panel
    wo, pr = _red_return(fleet, fake_gh)

    fake_gh.set_pr_artifact(pr, checks=GREEN, files=FILES, diff="+x = 1")
    fleet.change(wo["id"], "x = 2")
    finish(fleet, wo["id"], summary="fixed the build")
    fleet.drain()

    [history] = panel.calls[0]["packet"].history
    assert history["round"] == 1
    assert list(history["blockers"]) == []
    assert "unit (3.11)" in history["reason"]
    assert "without review" in history["reason"]


def test_a_red_feature_packet_cannot_reach_the_branch_at_all(fleet):
    """Work-order rounds only (spec §6.1)"""
    packet = _packet(RED, source="feature")
    assert evidence_mod.ci_failing(packet) == ()


def test_red_ci_is_an_uncounted_cause_and_never_a_holding_one(fleet):
    """Because this SETTLES rather than holds, no hold accounting changes"""
    from jarvis.project_store import VALIDATION_HOLDING_CAUSES

    assert ops.RED_CI_CAUSE == "red_ci"
    assert ops.RED_CI_CAUSE not in VALIDATION_HOLDING_CAUSES

    store = ProjectStore(fleet.project)
    try:
        wo = store.create_work_order("t")
        rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="f")
        assert bool(store.get_validation_round(rnd["id"])["uncounted"]) is False
        store.mark_validation_round_uncounted(rnd["id"], ops.RED_CI_CAUSE)
        row = store.get_validation_round(rnd["id"])
        assert bool(row["uncounted"]) is True
        assert row["uncounted_cause"] == ops.RED_CI_CAUSE
        assert row["hold_cause"] is None
    finally:
        store.close()
