"""List, meter and plot read one per-call ledger (io-82bcb830, finding two-ledgers-plot-vs-list)"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jarvis import ops, usage_meter
from jarvis.project_store import ProjectStore

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


def test_an_order_is_listed_iff_it_spent_inside_the_window_and_only_for_that_part(fleet_fixture):
    """User ruling kn-58e36cf1, whichever part of the order the window catches"""
    f = fleet_fixture
    picked = window(T0, T0 + HOUR)

    def session(title: str, *costs: tuple[float, float], turn: tuple[float, float]) -> None:
        wo = f.order(title, session_id=title)
        f.turn(wo, started_at=turn[0], ended_at=turn[1], cost_usd=99.0)
        f.transcript(title, [f.call_row(at=at, out=f.output_for(usd), mid=f"m-{title}-{at}")
                             for at, usd in costs])

    # Its beginning, its end, its middle, all of it
    session("beginning", (T0 + HOUR - 9, 1.0), (T0 + HOUR + 9, 8.0), turn=(T0 + HOUR - 9, T0 + 2 * HOUR))
    session("ending", (T0 - 9, 8.0), (T0 + 9, 2.0), turn=(T0 - 9, T0 + 9))
    session("middle", (T0 - 9, 8.0), (T0 + 9, 3.0), (T0 + HOUR + 9, 8.0),
            turn=(T0 - 9, T0 + HOUR + 9))
    session("whole", (T0 + 9, 4.0), (T0 + 19, 1.0), turn=(T0 + 9, T0 + 19))
    # A call past its turn's recorded end: no turn says so, the transcript does
    session("past its turn", (T0 + 9, 5.0), turn=(T0 - 3 * HOUR, T0 - 2 * HOUR))
    # A turn across the window that spent only outside it
    session("spent elsewhere", (T0 - 9, 8.0), turn=(T0 - 9, T0 + HOUR + 9))

    report = ops.cost_report(window=picked)

    assert {u["title"]: round(u["total_cost_usd"], 2) for u in report["units"]} == {
        "beginning": 1.0, "ending": 2.0, "middle": 3.0, "whole": 5.0, "past its turn": 5.0}
    assert meter_total(f, picked) == pytest.approx(16.0, abs=0.01)


def test_a_hidden_order_stays_on_the_list_the_meter_and_the_plot(fleet_fixture):
    f = fleet_fixture
    fleet(f)
    hidden = f.spend("hidden", 7.0, at=T0 + 700, session_id="hidden")
    os_hidden = f.order("hidden os call")
    f.os_call("neo_answer", ts=T0 + 710, wo_id=os_hidden, cost_usd=0.5,
              output=f.output_for(0.5))
    store = ProjectStore(f.project_path)
    try:
        store.set_hidden(hidden)
        store.set_hidden(os_hidden)
    finally:
        store.close()
    picked = window(T0, T0 + HOUR)

    listed = {u["id"]: u["total_cost_usd"] for u in ops.cost_report(window=picked)["units"]}
    assert listed[hidden] == pytest.approx(7.0, abs=0.01)
    assert listed[os_hidden] == pytest.approx(0.5, abs=0.01)

    # 12 from the scene, 7.5 hidden
    assert report_total(picked) == pytest.approx(19.5, abs=0.01)
    assert plot_total(f, picked)[0] == pytest.approx(19.5, abs=0.01)
    assert meter_total(f, picked) == pytest.approx(19.5, abs=0.01)


def test_a_pruned_transcript_still_shows_as_unmeasured(fleet_fixture):
    wo = fleet_fixture.order("pruned", session_id="gone")
    fleet_fixture.turn(wo, started_at=T0 + 9, ended_at=T0 + 19, cost_usd=1.0)

    report = ops.cost_report(window=window(T0, T0 + HOUR))

    assert [u["title"] for u in report["units"]] == ["pruned"]
    assert report["unmeasured"] == 1


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
