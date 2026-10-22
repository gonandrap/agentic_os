"""A validation round WAITS for GitHub to finish the checks before it judges anything"""

from __future__ import annotations

import json

from jarvis import db
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

#: A real changed file on the artifact
FILES = [{"path": "app.py", "additions": 1, "deletions": 0}]


def _packet(checks, source="pull_request"):
    """The one field `ci_pending` reads, in the shape the collector builds"""
    return evidence_mod.EvidencePacket(
        unit="work_order", subject_id="wo-1", title="t", description="", summary="",
        declared="", pr_url="https://github.com/x/y/pull/1", base="a", head="b",
        stat="", files=(), diff="", diff_truncated=False, dropped_files=(),
        diff_sha="", source=source,
        pr={"checks": checks, "head_sha": "abc"} if source == "pull_request" else None)


# -- 1. the predicate


def test_an_unfinished_check_is_what_the_os_waits_for():
    assert evidence_mod.ci_pending(_packet(PENDING)) == ("unit (3.11)", "evals")


def test_a_finished_check_is_never_waited_for_whatever_it_concluded():
    """GREEN and RED are both ANSWERS"""
    assert evidence_mod.ci_pending(_packet(GREEN)) == ()
    assert evidence_mod.ci_pending(_packet(RED)) == ()


def test_nothing_to_wait_for_is_not_a_wait():
    """Both shapes that can never become a verdict"""
    assert evidence_mod.ci_pending(_packet([])) == ()
    assert evidence_mod.ci_pending(_packet(None, source="worktree")) == ()


# -- 2. the hold


def test_the_round_is_held_while_ci_is_still_running_and_no_seat_is_reached(
        fleet, fake_gh):
    """THE CLAIM OF THIS FILE"""
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
        assert held > db.now(), "nothing would stop the next tick re-asking at once"
    finally:
        store.close()


def test_waiting_for_ci_spends_no_round_number(fleet, fake_gh):
    """`failed` is RUNNABLE and `counted_validation_rounds` ignores it"""
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
    """The other half, and the one that makes the wait a wait rather than a stall: the SAME round is..."""
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
    """The ceiling, and it fails SAFE rather than open: the round is judged on what GitHub actually..."""
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


def test_a_lapsed_ci_hold_is_judged_and_its_held_seconds_still_discounted(
        fleet, fake_gh, monkeypatch):
    """The §1(2) half of the root cause: EXCLUSION ALONE IS NOT THE FIX"""
    from jarvis import db, ops
    from jarvis.catalog import FleetHealthConfig
    from tests.test_health_sweep import park_order

    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    fleet.daemon.validator = Validator(passed())
    pr = finish(fleet, wo["id"])["pr_url"]
    fake_gh.set_pr_artifact(pr, checks=PENDING, files=FILES, diff="+x = 1")
    fleet.drain()

    store = ProjectStore(fleet.project)
    try:
        assert store.get_work_order(wo["id"])["status"] == "validating"
        now = db.now()
        # Twenty minutes in `validating`
        at = park_order(store, wo["id"], "validating", hours=20 / 60.0)
        for row in store.events_of_kind(wo["id"], "validation_failed"):
            payload = db.from_json(row["payload"], {})
            if payload.get("cause") != VALIDATION_CI_CAUSE:
                continue
            payload["reopens_at"] = now - 60
            store.conn.execute("UPDATE wo_events SET ts=?, payload=? WHERE id=?",
                               (at, json.dumps(payload), row["id"]))
        store.conn.commit()

        judged = ops.stuck_scan(store, "proj_a", FleetHealthConfig(), now,
                                stuck_only=False)[1]
        row = [r for r in judged if r["wo"]["id"] == wo["id"]][0]

        assert row["verdict"].excluded == "", "the lapsed hold still excluded the order"
        assert row["discounted"] >= 18 * 60, row["discounted"]
        assert row["verdict"].stuck is False, row["verdict"].reason
    finally:
        store.close()


# -- 3. what a person reads


def test_the_two_holds_are_one_mechanism_and_two_sentences():
    """Same behaviour, different facts"""
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


# -- 4. the three renderers


class _Pr:
    """The narrowest pull request `automerge.decide` reads before it holds"""
    head_oid = "abc123abc123"
    state = "OPEN"
    mergeable_now = True
    checks_green = True


class _Cfg:
    enabled = True
    auto_merge = True


def test_the_hold_is_on_the_ROUND_and_not_only_on_the_event(fleet, fake_gh):
    """GitHub issue #581"""
    from jarvis import automerge, ops
    from jarvis.project_store import validation_standing

    wo = fleet.dispatch()
    fleet.change(wo["id"], "x = 1")
    fleet.daemon.validator = Validator(passed())
    pr = finish(fleet, wo["id"])["pr_url"]
    fake_gh.set_pr_artifact(pr, checks=PENDING, files=FILES, diff="+x = 1")

    fleet.drain()

    store = ProjectStore(fleet.project)
    try:
        row = store.validation_rounds(wo_id=wo["id"])[0]
        assert row["outcome"] == "failed", "the mechanism layer must not change"
        assert row["hold_cause"] == VALIDATION_CI_CAUSE

        # 1. the CLI — `jarvis wo show`, `fo show`, `validation show`
        line = ops.round_line(ops.validation_rounds(store, wo_id=wo["id"])[0])
        assert "waiting for CI" in line and "· failed ·" not in line

        # 2. the auto-merge hold sentence, which is the one the user quoted
        held = automerge.decide(dict(row),
                                {**store.get_work_order(wo["id"]),
                                 "status": "waiting_pr_merge"},
                                _Pr(), _Cfg(), validated_head="",
                                pending_assumptions=False)
        assert held.armed is False
        assert held.code == automerge.HELD_NOT_PASSED
        assert held.reason == "round 1 is waiting for CI"
    finally:
        store.close()

    # 3. the dashboard badge, rendered in tests/test_ui.py
    assert validation_standing(row) == ("waiting for CI", "active", "◑")


def test_a_round_closed_for_any_other_reason_clears_the_hold(fleet):
    """Written on EVERY close, not only the holding ones"""
    from jarvis.project_store import validation_standing

    store = ProjectStore(fleet.project)
    try:
        wo = store.create_work_order("t")
        rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="f")
        store.close_validation_round(rnd["id"], "failed", "waiting",
                                     hold_cause=VALIDATION_CI_CAUSE)
        assert store.get_validation_round(rnd["id"])["hold_cause"] == VALIDATION_CI_CAUSE

        store.close_validation_round(rnd["id"], "failed", "the validator was unreachable")

        after = store.get_validation_round(rnd["id"])
        assert after["hold_cause"] is None
        assert validation_standing(after) == ("failed", "bad", "✗")
    finally:
        store.close()


def test_a_row_written_before_the_column_existed_reads_as_it_always_did(fleet):
    """kn-c712a5d6: a new column is untested until a test reads a row that predates it"""
    from jarvis import ops

    store = ProjectStore(fleet.project)
    try:
        wo = store.create_work_order("t")
        rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="f")
        store.close_validation_round(rnd["id"], "failed", "no validator is configured")
        store.conn.execute("UPDATE validation_rounds SET hold_cause=NULL WHERE id=?",
                           (rnd["id"],))
        store.conn.commit()
    finally:
        store.close()

    store = ProjectStore(fleet.project)  # re-opened, so `_migrate` has run over the row
    try:
        assert store.validation_rounds(wo_id=wo["id"])[0]["hold_cause"] is None
        assert "· failed ·" in ops.round_line(
            ops.validation_rounds(store, wo_id=wo["id"])[0])
    finally:
        store.close()
