"""A validation round WAITS for GitHub to finish the checks before it judges anything.

The user's ruling, 2026-09-18 (kn-356c724b): a worker runs the TARGETED tests and CI runs
the suite. The full suite takes ~21 minutes, a worker turn is one conversation with a
five-minute prompt cache, and one blocking call that long re-sends the whole conversation
at the cache-WRITE rate on the next call — wo-16a488ee paid that seven times, ~1.5M
tokens, and its round-1 rejection was still about CI contradicting the local green it had
spent 75 minutes producing.

Somebody still has to wait for the real suite. THE OS IS THE CHEAP PLACE TO WAIT: nothing
is billed while a round is held and no seat is reached, whereas the same wait inside a
worker turn is paid for twice.

The mechanism is `_validation_held`'s, reused rather than copied — the round is closed
`failed` (RUNNABLE, uncounted, so no round number is spent) with `reopens_at` on the
event, which `validation_hold_until` reads back. These tests drive the REAL
`Daemon._validate_work_order`, because "the validator was not called" is the whole claim
and a unit test of the predicate alone would pass with the branch deleted.
"""

from __future__ import annotations

import json
import time

from jarvis import evidence as evidence_mod
from jarvis.daemon import CI_HOLD_DEADLINE_SECONDS
from jarvis.project_store import (VALIDATION_CI_CAUSE, VALIDATION_HELD_CAUSE,
                                  ProjectStore, validation_hold_until)
from jarvis.timeline import _describe
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish, passed)

PENDING = [{"name": "unit (3.11)", "status": "IN_PROGRESS", "conclusion": ""},
           {"name": "evals", "status": "QUEUED", "conclusion": ""}]
GREEN = [{"name": "unit (3.11)", "status": "COMPLETED", "conclusion": "SUCCESS"},
         {"name": "evals", "status": "COMPLETED", "conclusion": "SUCCESS"}]
RED = [{"name": "unit (3.11)", "status": "COMPLETED", "conclusion": "FAILURE"}]

#: A real changed file on the artifact. Not scenery: `nothing_to_judge` escalates a
#: packet with no files before the validator is ever reached, so an artifact registered
#: without one would make every "the panel judged it" assertion below pass or fail for a
#: reason that has nothing to do with CI.
FILES = [{"path": "app.py", "additions": 1, "deletions": 0}]


def _packet(checks, source="pull_request"):
    """The one field `ci_pending` reads, in the shape the collector builds."""
    return evidence_mod.EvidencePacket(
        unit="work_order", subject_id="wo-1", title="t", description="", summary="",
        declared="", pr_url="https://github.com/x/y/pull/1", base="a", head="b",
        stat="", files=(), diff="", diff_truncated=False, dropped_files=(),
        diff_sha="", source=source,
        pr={"checks": checks, "head_sha": "abc"} if source == "pull_request" else None)


# -- 1. the predicate -----------------------------------------------------------------


def test_an_unfinished_check_is_what_the_os_waits_for():
    assert evidence_mod.ci_pending(_packet(PENDING)) == ("unit (3.11)", "evals")


def test_a_finished_check_is_never_waited_for_whatever_it_concluded():
    """GREEN and RED are both ANSWERS. Waiting on a red check would park the work order
    until the deadline lapsed instead of letting a seat say the code is broken — the
    distinction `failing_checks` draws, read from `status` rather than `conclusion`."""
    assert evidence_mod.ci_pending(_packet(GREEN)) == ()
    assert evidence_mod.ci_pending(_packet(RED)) == ()


def test_nothing_to_wait_for_is_not_a_wait():
    """Both shapes that can never become a verdict. A repository that runs no checks,
    and a packet with no pull request behind it: a round that waited on either would
    wait until the deadline every single time."""
    assert evidence_mod.ci_pending(_packet([])) == ()
    assert evidence_mod.ci_pending(_packet(None, source="worktree")) == ()


# -- 2. the hold ----------------------------------------------------------------------


def test_the_round_is_held_while_ci_is_still_running_and_no_seat_is_reached(
        fleet, fake_gh):
    """THE CLAIM OF THIS FILE. The panel must not judge a submission against an empty
    check list: `validator-seats/tester.md` judges the declared evidence against the
    check runs, so judging now would make every submission's testing claim unverifiable
    at exactly the moment the worker stopped proving it locally."""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    never = Validator(passed())
    fleet.daemon.validator = never
    pr = finish(fleet, wo["id"])["pr_url"]
    fake_gh.set_pr_artifact(pr, checks=PENDING, files=FILES, diff="+x = 1")

    fleet.drain()

    assert never.calls == [], "a seat was asked to judge a pull request CI had not run"
    store = ProjectStore(fleet.project)
    try:
        rounds = store.validation_rounds(wo_id=wo["id"])
        assert [(r["round"], r["outcome"]) for r in rounds] == [(1, "failed")]
        assert "waiting for GitHub" in rounds[0]["reason"]
        assert "unit (3.11)" in rounds[0]["reason"]
        held = validation_hold_until(
            store.events_of_kind(wo["id"], "validation_failed"), 1)
        assert held > time.time(), "nothing would stop the next tick re-asking at once"
    finally:
        store.close()


def test_waiting_for_ci_spends_no_round_number(fleet, fake_gh):
    """`failed` is RUNNABLE and `counted_validation_rounds` ignores it, so a submission
    held for twenty minutes still gets its three real rounds. A hold that burned a round
    would give a worker two attempts because GitHub was slow."""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    fleet.daemon.validator = Validator(passed())
    pr = finish(fleet, wo["id"])["pr_url"]
    fake_gh.set_pr_artifact(pr, checks=PENDING, files=FILES, diff="+x = 1")

    fleet.drain()

    store = ProjectStore(fleet.project)
    try:
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 0
    finally:
        store.close()


def test_the_hold_lifts_by_itself_once_the_checks_report(fleet, fake_gh, monkeypatch):
    """The other half, and the one that makes the wait a wait rather than a stall: the
    SAME round is judged on a later tick, with no worker turn, no user and no command.

    The recheck delay is shortened rather than the event rewritten. `validation_hold_until`
    takes the LATEST moment for a round, so a lapsed event written beside a live one
    changes nothing — the hold has to expire on its own terms or this test would assert
    over a path the daemon never takes.
    """
    monkeypatch.setattr("jarvis.daemon.CI_HOLD_RECHECK_SECONDS", -1.0)
    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    panel = Validator(passed())
    fleet.daemon.validator = panel
    pr = finish(fleet, wo["id"])["pr_url"]
    fake_gh.set_pr_artifact(pr, checks=PENDING, files=FILES, diff="+x = 1")
    fleet.drain()
    assert panel.calls == [], "the round was judged while CI was still running"

    fake_gh.set_pr_artifact(pr, checks=GREEN, files=FILES, diff="+x = 1")
    fleet.drain()

    assert len(panel.calls) == 1, "the round never went again after CI reported"
    assert panel.calls[0]["round"] == 1, "waiting for CI cost the submitter a round"


def test_ci_that_never_reports_is_judged_anyway_rather_than_parked_for_ever(
        fleet, fake_gh, monkeypatch):
    """The ceiling, and it fails SAFE rather than open: the round is judged on what
    GitHub actually reported, and the tester seat is already told to say "no checks
    reported" rather than read it as a pass. Waiting for ever is the failure mode
    `VALIDATION_OUTAGE_LIMIT` exists to prevent one authority along — a workflow that
    never finishes would park the work order in `validating` with nobody watching."""
    monkeypatch.setattr("jarvis.daemon.CI_HOLD_DEADLINE_SECONDS", -1.0)
    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    panel = Validator(passed())
    fleet.daemon.validator = panel
    pr = finish(fleet, wo["id"])["pr_url"]
    fake_gh.set_pr_artifact(pr, checks=PENDING, files=FILES, diff="+x = 1")

    fleet.drain()

    assert len(panel.calls) == 1, \
        "a workflow that never reports parked the round for ever"
    assert CI_HOLD_DEADLINE_SECONDS > 0, "the real ceiling must not be the test's"


# -- 3. what a person reads -----------------------------------------------------------


def test_the_two_holds_are_one_mechanism_and_two_sentences():
    """Same behaviour, different facts. A reader must not be told the account is out of
    budget when GitHub is merely still running, so the cause is its own — and
    `validation_hold_until` honours BOTH, which is what stops a new cause holding once
    and then spinning every tick for ever."""
    ci = {"payload": '{"round": 1, "cause": "ci_pending", "reopens_at": 50, '
                     '"pending": ["unit (3.11)"]}'}
    window = {"payload": '{"round": 1, "cause": "usage_limit", "reopens_at": 90}'}

    assert validation_hold_until([ci], 1) == 50
    assert validation_hold_until([window], 1) == 90
    assert validation_hold_until([ci, window], 1) == 90, "the later moment must win"

    title, detail = _describe("validation_failed", json.loads(ci["payload"]))
    assert title == "Validation waiting for CI"
    assert "unit (3.11)" in detail
    assert "usage" not in title.lower() and "limit" not in title.lower()
    assert VALIDATION_CI_CAUSE != VALIDATION_HELD_CAUSE
