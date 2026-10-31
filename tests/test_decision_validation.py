"""Decision orders: a researcher's report is judged by a narrow panel (docs/superpowers/specs/2026-10-31-decision-orders.md §4)"""

from __future__ import annotations

import subprocess

from jarvis import ops, validation
from jarvis.catalog import DEFAULT_VALIDATION_ROSTER
from jarvis.project_store import WO_KIND_CONTRACTS
from tests.test_decision_researcher import REPORT
from tests.test_plan_side_effect import _round, submitted
from tests.test_validation_loop import _settle
from tests.test_validation_loop import (  # noqa: F401
    Validator, finish, fleet, passed)


def researching(fleet) -> dict:
    """A decision order whose researcher has been opened, dispatched and had its turn"""
    do = ops.create_decision_order("proj_a", "which cache", "compare the two caches")
    fleet.tick()
    store = fleet.store()
    try:
        row = store.get_feature_order(do["id"])
        assert row["plan_wo_id"], "the daemon opened no researcher"
        assert _settle(store, row["plan_wo_id"])
        return row
    finally:
        store.close()


def test_a_researchers_report_is_judged_by_deliverable_and_chair_not_voided(
        fleet, monkeypatch):
    seen: list = []

    def decide(store, round_row, packet, cfg):
        seen.append((cfg.roster, packet))
        return passed()

    monkeypatch.setattr(validation, "decide", decide)
    fleet.daemon.validator = None  # the real seam, so the kind's roster is applied
    do = researching(fleet)

    out = ops.submit_decision_report(do["id"], REPORT)
    assert "warning" not in out, out
    fleet.drain()

    rnd = _round(fleet, do["plan_wo_id"])
    assert rnd["outcome"] not in ("void", "escalated"), rnd["reason"]
    assert len(seen) == 1, "no seat was asked about the report"
    roster, packet = seen[0]
    assert roster == ("deliverable", "chair")
    effect = next(e for e in packet.side_effects if e["kind"] == "decision_report")
    assert effect["attested"] is False
    assert "compare the two caches" in effect["detail"], "the ask is beside the report"
    assert "B is 30% cheaper at our volume" in effect["detail"]
    assert not [e for e in packet.side_effects if e["kind"] == "plan_submitted"]


def test_a_regular_work_order_keeps_the_projects_roster(fleet, monkeypatch):
    seen: list = []
    monkeypatch.setattr(validation, "decide",
                        lambda s, r, p, cfg: seen.append(cfg.roster) or passed())
    fleet.daemon.validator = None
    wo = fleet.dispatch("a change")
    fleet.change(wo["id"], "print('x')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    assert seen == [DEFAULT_VALIDATION_ROSTER]
    assert WO_KIND_CONTRACTS["worker"].roster is None


def test_a_feature_planners_plan_is_still_one_attested_effect(fleet):
    fleet.daemon.validator = Validator(passed())
    fo = submitted(fleet)

    store = fleet.store()
    try:
        effects = ops.side_effects_of(store, fo["plan_wo_id"])
    finally:
        store.close()
    assert [(e["kind"], e["attested"]) for e in effects] == [("plan_submitted", True)]
    assert _round(fleet, fo["plan_wo_id"])["outcome"] == "void"


def test_a_dirty_tracked_file_still_refuses_the_researchers_finish(fleet):
    fleet.daemon.validator = Validator(passed())
    do = researching(fleet)
    tree = fleet.worktree(do["plan_wo_id"])
    tracked = subprocess.run(["git", "ls-files"], cwd=tree, capture_output=True,
                             text=True, check=True).stdout.split()[0]
    (tree / tracked).write_text("edited by a researcher\n")

    out = ops.submit_decision_report(do["id"], REPORT)

    assert "warning" in out, out
    store = fleet.store()
    try:
        assert store.get_work_order(do["plan_wo_id"])["status"] != "validating"
    finally:
        store.close()
