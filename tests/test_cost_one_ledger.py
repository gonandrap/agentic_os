"""List, meter and plot read one per-call ledger (io-82bcb830, finding two-ledgers-plot-vs-list)"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jarvis import ops, usage_meter

HOUR = 3600.0
T0 = datetime(2026, 9, 30, 12, tzinfo=timezone.utc).timestamp()


def window(since: float, until: float) -> dict:
    return {"since": since, "until": until, "source": "custom", "label": "the span"}


def report_total(picked: dict) -> float:
    """The list, and the part of it declared beside the rows"""
    report = ops.cost_report(window=picked)
    assert sum(u["total_cost_usd"] for u in report["units"]) == pytest.approx(
        report["totals"]["total_cost_usd"], abs=0.01)
    return report["totals"]["total_cost_usd"] + report["os_unattributed"]["os_cost_usd"]


def plot_total(f, picked: dict) -> tuple[float, float]:
    events = usage_meter.spend_events(since=picked["since"], until=picked["until"], home=f.home)
    return (sum(usd for _, usd, own in events if own),
            sum(usd for _, usd, own in events if not own))


def meter_total(f, picked: dict) -> float:
    return ops.cost_meter(resolved=picked, home=f.home, now=picked["until"])["meter"]["spend"][
        "jarvis_usd"]


def fleet(f) -> None:
    """Every shape that made the ledgers part"""
    # A compacted order: the turn row and the agent_calls row both hold the compaction
    compacted = f.spend("compacted", 4.0, at=T0 + 60)
    f.turn(compacted, started_at=T0 + 120, ended_at=T0 + 180, kind="compact", cost_usd=2.5)
    f.os_call("compaction", ts=T0 + 150, wo_id=compacted, cost_usd=2.5,
              output=f.output_for(2.5))
    # One turn across the window's head, its calls either side of it
    straddler = f.order("straddler", session_id="straddling")
    f.turn(straddler, started_at=T0 - HOUR, ended_at=T0 + HOUR, cost_usd=9.0)
    f.transcript("straddling", [
        f.call_row(at=T0 - HOUR + 60, out=f.output_for(6.0), mid="m-before"),
        f.call_row(at=T0 + 30, out=f.output_for(1.0), mid="m-lead"),
    ], subagents=[[f.call_row(at=T0 + 40, out=f.output_for(0.5), mid="m-sub")]])
    # Still running: no recorded cost, no `ended_at`, and spending
    running = f.order("running", session_id="running", status="running")
    f.turn(running, started_at=T0 + 300, ended_at=None, state="running")
    f.transcript("running", [f.call_row(at=T0 + 330, out=f.output_for(2.0), mid="m-run")])
    # Only an OS call in the window, though its session spent there too
    os_only = f.order("os only", session_id="os-only")
    f.turn(os_only, started_at=T0 - 5 * HOUR, ended_at=T0 - 4 * HOUR)
    f.transcript("os-only", [f.call_row(at=T0 + 400, out=f.output_for(0.25), mid="m-os")])
    f.os_call("neo_answer", ts=T0 + 410, wo_id=os_only, cost_usd=0.75,
              output=f.output_for(0.75))
    # Declared beside the rows: nobody's call, and a call naming a deleted order
    f.os_call("digest", ts=T0 + 500, cost_usd=0.5, output=f.output_for(0.5))
    f.os_call("neo_answer", ts=T0 + 510, wo_id="wo-deleted", cost_usd=0.5,
              output=f.output_for(0.5))
    # An outside session: the plot's other half, on no list
    f.transcript("loose", [f.call_row(at=T0 + 600, out=f.output_for(3.0), mid="m-out")],
                 directory="-elsewhere")


def test_a_compact_turn_counts_once_and_every_surface_agrees(fleet_fixture):
    fleet(fleet_fixture)
    picked = window(T0, T0 + HOUR)

    list_usd = report_total(picked)
    jarvis, outside = plot_total(fleet_fixture, picked)

    # 4 + 2.5 compaction + 1.5 straddler + 2 running + 0.25 + 0.75 os-only + 1 declared
    assert list_usd == pytest.approx(12.0, abs=0.01)
    assert jarvis == pytest.approx(list_usd, abs=0.01)
    assert meter_total(fleet_fixture, picked) == pytest.approx(list_usd, abs=0.01)
    assert outside == pytest.approx(3.0, abs=0.01)


@pytest.mark.parametrize("since,until", [
    (T0 + 35, T0 + 335),            # through the straddler's lead, subagent and the running turn
    (T0 + 100, T0 + 200),           # only the compaction, nothing a turn row would place here
    (T0 + 505, T0 + 900),           # a deleted order's call and the outside session
    (T0 - 2 * HOUR, T0),            # before any call of the straddler's inside part
])
def test_any_dragged_range_agrees(fleet_fixture, since, until):
    fleet(fleet_fixture)
    picked = window(since, until)

    jarvis, _ = plot_total(fleet_fixture, picked)

    assert jarvis == pytest.approx(report_total(picked), abs=0.01)
    assert meter_total(fleet_fixture, picked) == pytest.approx(jarvis, abs=0.01)


def test_a_transcript_that_grows_is_read_again(fleet_fixture):
    fleet_fixture.spend("growing", 1.0, at=T0, session_id="growing")
    first = fleet_fixture.call_row(at=T0, out=fleet_fixture.output_for(1.0), mid="m-growing")
    picked = window(T0, T0 + HOUR)
    assert plot_total(fleet_fixture, picked)[0] == pytest.approx(1.0, abs=0.01)

    fleet_fixture.transcript("growing", [
        first, fleet_fixture.call_row(at=T0 + 9, out=fleet_fixture.output_for(2.0), mid="m-next")])

    assert plot_total(fleet_fixture, picked)[0] == pytest.approx(3.0, abs=0.01)


def test_a_window_lists_every_order_however_many(fleet_fixture):
    for i in range(3):
        fleet_fixture.spend(f"order {i}", 1.0, at=T0 + i)
    picked = window(T0, T0 + HOUR)

    report = ops.cost_report(limit=1, window=picked)

    assert len(report["units"]) == 3
    assert report["totals"]["total_cost_usd"] == pytest.approx(3.0, abs=0.01)


def test_a_project_scope_matches_the_scoped_list(fleet_fixture):
    fleet(fleet_fixture)
    picked = window(T0, T0 + HOUR)

    report = ops.cost_report(project="proj_a", window=picked)
    meter = ops.cost_meter(resolved=picked, project="proj_a", home=fleet_fixture.home,
                           now=picked["until"])["meter"]

    assert meter["spend"]["scope_jarvis_usd"] == pytest.approx(
        report["totals"]["total_cost_usd"] + report["os_unattributed"]["os_cost_usd"], abs=0.01)
