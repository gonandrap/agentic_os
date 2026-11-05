"""Cost, search, links and inspect for monitoring orders (docs/superpowers/specs/2026-11-01-monitoring-orders.md §7)"""

from __future__ import annotations

import pytest

from jarvis import agent_usage, bill, fleetcost, morpheus, notify, ops, search, usage
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


def test_an_arm_session_bills_its_messages_once_at_the_served_model(central, tmp_path,
                                                                    monkeypatch):
    """Issue #1462 pin, through the real recording path: run_session, claude_cli parsing, bill"""
    import json

    from jarvis import claude_cli
    from jarvis.catalog import Catalog, OsConfig

    cfg = MorpheusConfig(model="claude-sonnet-4-5")
    served = "claude-opus-5"
    # Three API messages in one session; the CLI's modelUsage is their sum
    messages = [{"input_tokens": 3, "cache_creation_input_tokens": 1_000,
                 "cache_read_input_tokens": 0, "output_tokens": 40},
                {"input_tokens": 2, "cache_creation_input_tokens": 200,
                 "cache_read_input_tokens": 1_000, "output_tokens": 60},
                {"input_tokens": 1, "cache_creation_input_tokens": 50,
                 "cache_read_input_tokens": 1_200, "output_tokens": 80}]
    total = {k: sum(m[k] for m in messages) for k in messages[0]}
    envelope = {
        "result": json.dumps({"script": "exit 0\n", "notes": "ok"}),
        "session_id": "sess-arm-1", "num_turns": len(messages), "total_cost_usd": 0.42,
        "usage": {**total, "iterations": messages},
        "modelUsage": {served: {
            "inputTokens": total["input_tokens"], "outputTokens": total["output_tokens"],
            "cacheReadInputTokens": total["cache_read_input_tokens"],
            "cacheCreationInputTokens": total["cache_creation_input_tokens"],
            "costUSD": 0.42, "contextWindow": 200_000}},
    }
    seen = []
    monkeypatch.setattr(claude_cli, "_run",
                        lambda args, **kw: seen.append(args) or json.dumps(envelope))
    mo = _mo(central)
    reply = morpheus.run_session(central, Catalog(os=OsConfig(morpheus=cfg), projects=[]),
                                 mo, purpose="arm", cwd=tmp_path)
    assert reply == {"script": "exit 0\n", "notes": "ok", "trigger": ""}
    assert seen[0][seen[0].index("--model") + 1] == cfg.model  # asked for cfg.model...

    got = bill.for_monitoring_order(mo["id"])["total"]
    # One row per session: `claude -p` JSON carries no per-message ids, so the session is the unit
    assert got["calls"] == 1
    # The sum of the messages, once: #1462 billed a multiple of it
    assert {k: got["tokens"][k] for k in ("input", "cache_write", "cache_read", "output")} == {
        "input": total["input_tokens"], "cache_write": total["cache_creation_input_tokens"],
        "cache_read": total["cache_read_input_tokens"], "output": total["output_tokens"]}
    assert got["cost"]["exact_usd"] == pytest.approx(0.42)
    # ...priced at the model that ran, not cfg.model; vacuous if the two cost the same
    assert usage.price_for(served) != usage.price_for(cfg.model)
    assert got["models"] == [served]
    listed = usage.class_costs(served, input=total["input_tokens"],
                               cache_write=total["cache_creation_input_tokens"],
                               cache_read=total["cache_read_input_tokens"],
                               output=total["output_tokens"])
    assert got["cost"]["list_usd"] == pytest.approx(sum(listed.values()))


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


def test_release_counts_an_order_older_than_the_newest_call_limit_rows(central, monkeypatch):
    """The vacuous filter read the newest CALL_LIMIT rows and filtered in Python"""
    monkeypatch.setattr(bill, "CALL_LIMIT", 3)
    old = _mo(central, "old", "jarvis-1.0.0")
    for i in range(bill.CALL_LIMIT + 1):
        _mo(central, f"newer {i}", "jarvis-2.0.0")
    # Same created_ts in a fast test: make the order of rows explicit
    central.conn.execute("UPDATE monitoring_orders SET created_ts = created_ts - 100 WHERE id=?",
                         (old["id"],))
    central.conn.commit()
    assert old["id"] not in {r["id"] for r in central.morpheus_items(limit=bill.CALL_LIMIT)}
    assert [o["id"] for o in bill.for_release("jarvis-1.0.0")["orders"]] == [old["id"]]


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
