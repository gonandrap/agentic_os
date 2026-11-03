"""Cost, search, links and inspect for monitoring orders (docs/superpowers/specs/2026-11-01-monitoring-orders.md §7)"""

from __future__ import annotations

import pytest

from jarvis import agent_usage, bill, fleetcost, morpheus, notify, ops, search
from jarvis.catalog import MorpheusConfig
from jarvis.central_store import CentralStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)


@pytest.fixture()
def central(started):
    c = CentralStore()
    yield c
    c.close()


def _mo(central, title="watch the login spinner", tag=""):
    if not tag:
        return morpheus.create(central, project="proj_a", title=title, prompt="spin stops")
    # A release's orders are its PRs
    n = len(central.morpheus_items()) + 1
    return morpheus.enqueue(
        central, MorpheusConfig(),
        morpheus.PullRequestBody(number=n, url=f"https://github.com/o/r/pull/{n}",
                                 title=title, body=f"## How to validate in production\n\n{title}\n"),
        project="proj_a", repo="o/r", release_tag=tag)


def _call(central, mo_id, cost, label="arm"):
    agent_usage.record(agent_usage.MORPHEUS, wo_id=mo_id, project="proj_a", label=label,
                       model="claude-sonnet-4-5", store=central,
                       usage={"input": 10, "output": 5, "total_cost_usd": cost})


def test_cost_of_an_order_is_the_sum_of_its_calls(central):
    mo = _mo(central)
    _call(central, mo["id"], 0.25, "arm")
    _call(central, mo["id"], 0.5, "judge")
    # Another order's call must not leak in: the vacuous bill reads every MORPHEUS row
    _call(central, _mo(central, "other")["id"], 9.0)
    got = ops.bill(mo["id"])
    assert got["kind"] == "monitoring_order"
    assert got["total"]["calls"] == 2
    assert got["total"]["cost"]["exact_usd"] == pytest.approx(0.75)
    assert got["checks"]["balanced"], got["checks"]
    assert bill.build(mo["id"])["total"] == got["total"]


def test_an_unknown_mo_id_is_an_error_not_an_empty_bill(central):
    with pytest.raises(ops.OpsError, match="no monitoring order"):
        ops.bill("mo-00000000")


def test_release_sums_only_its_own_orders(central):
    a, b = _mo(central, "a", "jarvis-1.2.0"), _mo(central, "b", "jarvis-1.2.0")
    c = _mo(central, "c", "jarvis-1.1.0")
    _call(central, a["id"], 0.25)
    _call(central, b["id"], 0.5)
    _call(central, c["id"], 4.0)
    got = bill.for_release("jarvis-1.2.0")
    assert got["count"] == 2
    assert {o["id"] for o in got["orders"]} == {a["id"], b["id"]}
    assert got["total"]["cost"]["exact_usd"] == pytest.approx(0.75)
    assert got["checks"]["balanced"], got["checks"]
    with pytest.raises(ops.OpsError):
        bill.for_release("jarvis-9.9.9")


def test_cost_cli_prints_an_mo_and_a_release(central, capsys):
    from jarvis.cli import main

    mo = _mo(central, tag="jarvis-1.2.0")
    _call(central, mo["id"], 0.25)
    assert main(["cost", mo["id"]]) == 0
    assert mo["id"] in capsys.readouterr().out
    assert main(["cost", "--release", "jarvis-1.2.0"]) == 0
    assert "1 monitoring order for jarvis-1.2.0" in capsys.readouterr().out


def test_fleet_counts_an_mo_call_once_under_morpheus(central):
    mo = _mo(central)
    _call(central, mo["id"], 0.25)
    kinds, unattributed = fleetcost.os_by_kind(0, 1e12)
    lines = [k for k in kinds if k["kind"] == agent_usage.MORPHEUS]
    assert len(lines) == 1 and lines[0]["calls"] == 1
    assert unattributed["calls"] == 0


def test_search_finds_an_mo_by_title(central):
    mo = _mo(central, "watch the flux capacitor")
    hits = search.search("flux capacitor", kinds=("monitoring",))
    assert [(h["kind"], h["id"]) for h in hits] == [("monitoring", mo["id"])]
    assert hits[0]["url"] == f"/mo/{mo['id']}"
    assert search.search("flux capacitor", kinds=("backlog",)) == []


def test_check_wo_link_resolves_mo_and_mv_ids(central):
    catalog = ops.resolve_catalog()
    base = notify.ui_base_url(catalog)
    mo = _mo(central)
    assert notify.check_wo_link(central, catalog, "proj_a", mo["id"]) == (
        f"{base}/mo/{mo['id']}", "")
    # A pre-feature row keeps its mv- id; nothing mints one any more
    central.conn.execute("UPDATE monitoring_orders SET id='mv-1234abcd' WHERE id=?",
                         (_mo(central, "legacy")["id"],))
    assert notify.check_wo_link(central, catalog, "proj_a", "mv-1234abcd") == (
        f"{base}/mo/mv-1234abcd", "")
    url, problem = notify.check_wo_link(central, catalog, "proj_a", "mo-00000000")
    assert url is None and "does not exist" in problem


def test_inspect_without_a_transcript_points_at_cost(central, capsys):
    from jarvis.cli import main

    mo = _mo(central)
    res = ops.inspect_report(mo["id"])
    assert res["units"] == []
    assert res["note"] == f"no transcript; see jarvis cost {mo['id']}"
    assert main(["inspect", mo["id"]]) == 0
    assert f"no transcript; see jarvis cost {mo['id']}" in capsys.readouterr().out
