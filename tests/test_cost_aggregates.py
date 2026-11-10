"""ops.cost_aggregates (docs/superpowers/specs/2026-11-09-cost-overview-first.md §1.4, §2)"""
from __future__ import annotations

import json

import pytest

from jarvis import cli, db, ops


def _unit(uid, project, *, found=True, list_cost=0.0, os_cost=0.0, subproc=0.0,
          ttl=0, ttl_tokens=0, **extra):
    return {"id": uid, "project": project, "found": found, "kind": "worker",
            "list_cost_usd": list_cost, "os_cost_usd": os_cost,
            "subproc_cost_usd": subproc, "rewrite_cost_usd": 0.0, "rewrite_excess": 0,
            "resume_boundaries": 0, "subagent_cost_usd": 0.0, "output": 0,
            "billed_input": 0, "boundaries_ttl": ttl, "rewrite_ttl_excess": ttl_tokens,
            **extra}


def test_slices_reconcile_to_the_headline():
    # Half-cent parts so every rounding step bites
    units = [_unit("wo-a1", "alpha", list_cost=1.005, os_cost=0.115, subproc=0.335),
             _unit("wo-a2", "alpha", list_cost=2.125, os_cost=0.005),
             _unit("fo-b1", "beta", list_cost=0.555, os_cost=0.445, subproc=0.015),
             _unit("io-c1", "gamma", found=False, os_cost=0.725)]
    unattributed = {"os_cost_usd": 0.12345}
    agg = ops.cost_aggregates(units, unattributed)

    assert agg["fleet_cost_usd"] == ops._rollup(units, unattributed)["totals"]["total_cost_usd"]
    assert (sum(p["cost_usd"] for p in agg["projects"]) + agg["unattributed_cost_usd"]
            + agg["rounding_usd"]) == pytest.approx(agg["fleet_cost_usd"])
    assert abs(agg["rounding_usd"]) < 0.015 * (len(agg["projects"]) + 1)
    assert agg["unattributed_cost_usd"] == pytest.approx(0.12345)
    costs = [p["cost_usd"] for p in agg["projects"]]
    assert costs == sorted(costs, reverse=True), "dearest first"


def test_an_unfound_units_os_spend_lands_in_its_project_but_not_its_ttl():
    units = [_unit("wo-a1", "alpha", ttl=2, ttl_tokens=1000),
             _unit("wo-a2", "alpha", found=False, list_cost=9.0, os_cost=0.5,
                   ttl=7, ttl_tokens=99_999)]
    (alpha,) = ops.cost_aggregates(units)["projects"]

    assert alpha["cost_usd"] == 0.5
    assert alpha["ttl_misses"] == 2
    assert alpha["ttl_rewrite_tokens"] == 1000
    assert ops.cost_aggregates(units)["unattributed_cost_usd"] == 0.0


def test_order_kinds_come_from_the_id_prefix_and_children_are_never_counted():
    child = _unit("wo-kid", "alpha", list_cost=50.0)
    units = [_unit("wo-1", "alpha", list_cost=1.0),
             _unit("fo-2", "alpha", list_cost=2.0, children=[child], kind="planner"),
             _unit("io-3", "beta", list_cost=3.0),
             _unit("inv-4", "beta", list_cost=4.0)]
    agg = ops.cost_aggregates(units)

    assert agg["orders"] == 4
    assert agg["by_order_kind"] == {"fo": 1, "inv": 1, "io": 1, "wo": 1}
    beta, alpha = agg["projects"]
    assert alpha["by_order_kind"] == {"fo": {"orders": 1, "cost_usd": 2.0},
                                      "wo": {"orders": 1, "cost_usd": 1.0}}
    assert alpha["cost_usd"] == 3.0 and beta["cost_usd"] == 7.0


def test_jarvis_cost_json_carries_the_aggregates(fleet_fixture, capsys):
    wo = fleet_fixture.order("an order")
    fleet_fixture.turn(wo, started_at=db.now() - 60, ended_at=db.now(), cost_usd=1.0)

    assert cli.main(["cost", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)

    agg = out["aggregates"]
    assert agg["orders"] == len(out["units"])
    assert agg["fleet_cost_usd"] == out["totals"]["total_cost_usd"]
