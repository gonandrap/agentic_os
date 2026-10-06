"""`jarvis cost --fleet` — the distribution, on synthetic state only.

Spec §7 of docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md. Everything
here is built by the `fleet_fixture` helper in `jarvis/testing.py`: no real transcript,
no real agent, and chosen timestamps, because every question this report answers is
about WHEN a turn ran and what population it belongs to.

Two rules are asserted harder than the arithmetic, because getting them wrong produces a
number that reads as true: a turn still running is EXCLUDED and never a zero, and money
from two different sources (`cost_source`) is reported as two figures and never blended
(kn-e6bb1166).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jarvis import catalog, fleetcost, ops


def stamp(text: str) -> float:
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


#: The acceptance week: Mon 2026-09-28 21:00 PDT -> the following Monday.
SINCE = stamp("2026-09-29T04:00:00+00:00")
UNTIL = stamp("2026-10-06T04:00:00+00:00")
#: Somewhere comfortably inside it.
T0 = stamp("2026-09-30T12:00:00+00:00")


def envelope(*, input: int = 0, cache_read: int = 0, cache_write: int = 0,
             output: int = 0) -> dict:
    return {"usage_v": 3, "input": input, "cache_read": cache_read,
            "cache_write": cache_write, "output": output, "cache_5m": cache_write,
            "cache_1h": 0}


# -- 1. the default window -------------------------------------------------------------


def test_usage_week_boundaries():
    """The Claude usage week, from the catalog's reset and never a literal."""
    cfg = catalog.CostConfig()

    assert fleetcost.usage_week(T0, cfg) == (SINCE, UNTIL)
    # The reset instant itself opens the new week.
    assert fleetcost.usage_week(SINCE, cfg) == (SINCE, UNTIL)
    # One second before it is still the PREVIOUS week.
    before = fleetcost.usage_week(SINCE - 1, cfg)
    assert before == (stamp("2026-09-22T04:00:00+00:00"), SINCE)
    # ...and the last instant of the week is the week.
    assert fleetcost.usage_week(UNTIL - 1, cfg) == (SINCE, UNTIL)

    # A non-default hour moves it: Monday 00:00 PDT is 07:00 UTC.
    midnight = fleetcost.usage_week(T0, catalog.CostConfig(week_reset_hour=0))
    assert midnight[0] == stamp("2026-09-28T07:00:00+00:00")
    # Zero is a legal hour and must not be read as "unset".
    assert midnight[1] == stamp("2026-10-05T07:00:00+00:00")


def test_window_of_prefers_the_flags():
    cfg = catalog.CostConfig()
    flagged = fleetcost.window_of(SINCE, UNTIL, cfg, now=T0)
    assert (flagged["since"], flagged["until"]) == (SINCE, UNTIL)
    assert flagged["source"] == "flags"
    assert fleetcost.window_of(None, None, cfg, now=T0)["source"] == "usage-week"


# -- 2. the one place a population statistic is computed -------------------------------


def test_metric_nearest_rank_p90():
    """p90 is nearest-rank, so it is always a value some order actually had."""
    def p90(n: int) -> float:
        values = [(float(i), f"wo-{i}") for i in range(1, n + 1)]
        return fleetcost.metric(values).p90

    assert p90(1) == 1.0
    assert p90(2) == 2.0
    assert p90(9) == 9.0
    assert p90(10) == 9.0
    assert p90(11) == 10.0

    m = fleetcost.metric([(1.0, "wo-a"), (7.0, "wo-b"), (2.0, "wo-c")])
    assert m.n == 3 and m.avg == pytest.approx(10 / 3)
    assert m.max == {"value": 7.0, "wo_id": "wo-b"}
    assert m.p90 in (1.0, 2.0, 7.0), "p90 is one of the inputs"

    empty = fleetcost.metric([])
    assert empty.n == 0
    assert empty.avg is None and empty.p90 is None and empty.max is None


# -- 3. turns, compactions, windows ----------------------------------------------------


def report(**kwargs):
    return ops.fleet_cost(since=SINCE, until=UNTIL, **kwargs)["fleet"]


def test_compact_turns_are_not_turns(fleet_fixture):
    wo = fleet_fixture.order(session_id="sess-a")
    for i, kind in enumerate(["dispatch", "message", "message"]):
        fleet_fixture.turn(wo, kind=kind, started_at=T0 + i * 100,
                           ended_at=T0 + i * 100 + 60, cost_usd=1.0)
    for i in range(2):
        fleet_fixture.turn(wo, kind="compact", started_at=T0 + 500 + i,
                           ended_at=T0 + 510 + i, cost_usd=0.2)

    fleet = report()

    assert fleet["metrics"]["turns_per_order"]["n"] == 1
    assert fleet["metrics"]["turns_per_order"]["avg"] == 3.0
    assert fleet["metrics"]["compactions_per_order"]["avg"] == 2.0
    # The compact turn's dollars are not a worker turn's dollars either.
    assert fleet["metrics"]["cost_per_turn_usd"]["n"] == 3


def test_window_truncates_an_order(fleet_fixture):
    wo = fleet_fixture.order(session_id="sess-a")
    fleet_fixture.turn(wo, started_at=SINCE - 3600, ended_at=SINCE - 3500, cost_usd=9.0)
    for i in range(3):
        fleet_fixture.turn(wo, kind="message", started_at=T0 + i * 100,
                           ended_at=T0 + i * 100 + 50, cost_usd=1.0)
    fleet_fixture.turn(wo, kind="message", started_at=UNTIL + 10, ended_at=UNTIL + 60,
                       cost_usd=9.0)
    outside = fleet_fixture.order(session_id="sess-b")
    fleet_fixture.turn(outside, started_at=SINCE - 7200, ended_at=SINCE - 7100,
                       cost_usd=5.0)

    fleet = report()

    assert fleet["orders"]["n"] == 1, "counted once, not once per window"
    assert fleet["orders"]["truncated"] == 1
    assert fleet["orders"]["excluded_no_turns"] == 1
    assert fleet["metrics"]["turns_per_order"]["avg"] == 3.0
    assert fleet["metrics"]["cost_per_order_usd"]["avg"] == 3.0


def test_running_turn_excluded_not_zero(fleet_fixture):
    wo = fleet_fixture.order(status="running", session_id="sess-a")
    for i in range(2):
        fleet_fixture.turn(wo, started_at=T0 + i * 1000, ended_at=T0 + i * 1000 + 100,
                           cost_usd=1.0)
    fleet_fixture.turn(wo, kind="message", started_at=T0 + 5000, ended_at=None,
                       state="running", cost_usd=None)

    seconds = report()["metrics"]["seconds_per_turn"]

    assert seconds["n"] == 2
    assert seconds["avg"] == 100.0, "the average did not move toward zero"
    assert seconds["excluded"]["running"] == 1
    assert report()["orders"]["live"] == 1


# -- 4. two currencies, never blended --------------------------------------------------


def test_mixed_cost_basis_labelled(fleet_fixture):
    """`envelope` is the headline; the transcript FLOOR is its own figure with its own n."""
    wo = fleet_fixture.order(session_id="sess-a")
    fleet_fixture.turn(wo, started_at=T0, ended_at=T0 + 60, cost_usd=1.0,
                       cost_source="envelope")
    fleet_fixture.turn(wo, kind="message", started_at=T0 + 100, ended_at=T0 + 160,
                       cost_usd=3.0, cost_source="envelope")
    fleet_fixture.turn(wo, kind="message", started_at=T0 + 200, ended_at=T0 + 260,
                       cost_usd=0.5, cost_source="transcript")
    fleet_fixture.turn(wo, kind="message", started_at=T0 + 300, ended_at=T0 + 360,
                       cost_usd=None)

    metrics = report()["metrics"]
    headline = metrics["cost_per_turn_usd"]
    floor = metrics["cost_per_turn_usd_transcript_floor"]

    assert headline["provenance"] == "envelope"
    assert headline["n"] == 2 and headline["avg"] == 2.0
    assert headline["cost_basis"] == {"envelope": 2, "transcript": 1, "unrecorded": 1}
    assert headline["excluded"]["unrecorded"] == 1
    assert floor["provenance"] == "transcript"
    assert floor["n"] == 1 and floor["avg"] == 0.5
    # The one thing that must never happen: a single blended average.
    assert headline["avg"] != pytest.approx((1.0 + 3.0 + 0.5) / 3)


def test_an_all_envelope_population_is_not_labelled_mixed(fleet_fixture):
    wo = fleet_fixture.order(session_id="sess-a")
    fleet_fixture.turn(wo, started_at=T0, ended_at=T0 + 60, cost_usd=2.0)

    metrics = report()["metrics"]

    assert metrics["cost_per_turn_usd"]["cost_basis"]["transcript"] == 0
    assert metrics["cost_per_turn_usd_transcript_floor"]["n"] == 0
    assert metrics["cost_per_turn_usd_transcript_floor"]["avg"] is None


# -- 5. boundaries: four causes, four counts -------------------------------------------


def test_ttl_and_prefix_reported_separately(fleet_fixture):
    """All FOUR causes of `usage.classify_boundaries`, each on its own line."""
    f = fleet_fixture
    rows = [
        f.call_row(at=T0, write=50_000, out=100),
        f.call_row(at=T0 + 10, read=50_000, out=100),
        f.call_row(at=T0 + 620, read=100, write=40_000, out=100),   # ttl: gap + cold
        f.call_row(at=T0 + 630, read=40_000, out=100),
        f.call_row(at=T0 + 640, read=100, write=40_000, out=100),   # prefix: no gap
        f.compact_row(at=T0 + 645),
        f.call_row(at=T0 + 650, read=50, write=5_000, out=100),     # compacted
    ]
    f.transcript("sess-a", rows)

    counts = fleetcost.boundary_counts("sess-a", 5_000)

    assert counts == {"ttl": 1, "prefix": 1, "compacted": 1, "undecided": 0}
    # No floor configured: the TTL split is left OPEN rather than guessed, and the
    # undecided ones are reported as their own count instead of being dropped.
    assert fleetcost.boundary_counts("sess-a", None) == {
        "ttl": 0, "prefix": 0, "compacted": 1, "undecided": 2}


def test_the_four_boundary_causes_reach_the_payload(fleet_fixture):
    f = fleet_fixture
    wo = f.order(session_id="sess-a")
    f.turn(wo, started_at=T0, ended_at=T0 + 700, cost_usd=1.0)
    f.transcript("sess-a", [
        f.call_row(at=T0, write=50_000, out=100),
        f.call_row(at=T0 + 10, read=50_000, out=100),
        f.call_row(at=T0 + 620, read=100, write=40_000, out=100),
        f.call_row(at=T0 + 630, read=40_000, out=100),
        f.call_row(at=T0 + 640, read=100, write=40_000, out=100),
    ])

    metrics = report()["metrics"]

    assert metrics["ttl_expiry_per_order"]["avg"] == 1.0
    assert metrics["prefix_miss_per_order"]["avg"] == 1.0
    assert metrics["undecided_boundaries_per_order"]["avg"] == 0.0
    assert metrics["compacted_boundaries_per_order"]["avg"] == 0.0
    assert metrics["ttl_expiry_per_order"]["provenance"] == "transcript"


def test_subagent_share_comes_off_the_transcript(fleet_fixture):
    f = fleet_fixture
    wo = f.order(session_id="sess-a")
    f.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)
    f.transcript("sess-a", [f.call_row(at=T0, write=100_000, out=1_000)],
                 subagents=[[f.call_row(at=T0 + 10, mid="s1", write=100_000,
                                        out=1_000)]])

    share = fleetcost.subagent_share("sess-a", 5_000)

    assert share == pytest.approx(0.5, abs=0.01), "half the session was a subagent"
    assert report()["metrics"]["subagent_share"]["avg"] == pytest.approx(share)


# -- 6. the OS's own spend -------------------------------------------------------------


def test_os_by_kind_includes_zero_cost_kinds(fleet_fixture):
    f = fleet_fixture
    wo = f.order(session_id="sess-a")
    f.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)
    f.os_call("neo_answer", ts=T0 + 10, wo_id=wo, cost_usd=2.0)
    f.os_call("observe_turn", ts=T0 + 20, wo_id=wo, cost_usd=0.0)
    f.os_call("digest", ts=T0 + 30, wo_id="", cost_usd=0.25)
    f.os_call("neo_answer", ts=SINCE - 100, wo_id=wo, cost_usd=99.0)

    fleet = report()
    by_kind = {row["kind"]: row for row in fleet["os_cost_by_kind"]}

    assert by_kind["observe_turn"]["cost_usd"] == 0.0, "a $0 kind still reconciles"
    assert by_kind["neo_answer"]["cost_usd"] == 2.0, "out-of-window call excluded"
    assert fleet["os_unattributed"] == {"calls": 1, "cost_usd": 0.25}
    # The unattributed call lands on no order; the attributed one does.
    assert fleet["metrics"]["cost_per_order_usd"]["avg"] == 3.0


def test_os_spend_between_turns_is_reported_not_dropped(fleet_fixture):
    f = fleet_fixture
    wo = f.order(session_id="sess-a")
    f.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)
    f.os_call("neo_answer", ts=T0 + 50, wo_id=wo, cost_usd=0.5)     # inside the turn
    f.os_call("neo_answer", ts=T0 + 500, wo_id=wo, cost_usd=0.75)   # between turns

    metrics = report()["metrics"]

    assert metrics["cost_per_turn_usd"]["avg"] == 1.5
    assert metrics["cost_per_turn_usd"]["excluded"]["os_unattributed_to_turn"] == 1
    assert metrics["cost_per_order_usd"]["avg"] == 2.25, "the order carries both"


def test_records_are_counted_not_priced(fleet_fixture):
    f = fleet_fixture
    wo = f.order(session_id="sess-a")
    f.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)
    f.validation_round(wo, ts=T0 + 50, round=1)
    f.validation_round(wo, ts=T0 + 60, round=2)
    f.neo_question(wo, ts=T0 + 70)

    metrics = report()["metrics"]

    assert metrics["validation_rounds_per_order"]["avg"] == 2.0
    assert metrics["validation_rounds_per_order"]["provenance"] == "record"
    assert metrics["validation_rounds_per_order"]["cost_basis"] is None
    assert metrics["neo_questions_per_order"]["avg"] == 1.0


# -- 7. the contract and the read-only guarantee ---------------------------------------


FLEET_KEYS = {
    "version", "window", "scope", "orders", "metrics", "os_cost_by_kind",
    "os_unattributed", "compaction_payoff", "floor", "floor_reason", "notes",
}
METRIC_KEYS = {
    "turns_per_order", "cost_per_turn_usd", "cost_per_turn_usd_transcript_floor",
    "tokens_per_turn.input", "tokens_per_turn.cache_read", "tokens_per_turn.cache_write",
    "tokens_per_turn.output", "compactions_per_order", "ttl_expiry_per_order",
    "prefix_miss_per_order", "compacted_boundaries_per_order",
    "undecided_boundaries_per_order", "seconds_per_turn", "cost_per_order_usd",
    "cost_per_order_usd_transcript_floor", "subagent_share", "rewrite_tax_share",
    "validation_rounds_per_order", "neo_questions_per_order",
}


def test_payload_keys_stable(fleet_fixture):
    wo = fleet_fixture.order(session_id="sess-a")
    fleet_fixture.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0,
                       usage=envelope(input=10, cache_read=20, cache_write=30,
                                      output=40))

    fleet = report()

    assert set(fleet) == FLEET_KEYS
    assert set(fleet["metrics"]) == METRIC_KEYS
    assert set(fleet["window"]) == {"since", "until", "label", "source"}
    assert set(fleet["orders"]) == {"n", "live", "truncated", "excluded_no_turns"}
    one = fleet["metrics"]["cost_per_turn_usd"]
    assert set(one) == {"n", "avg", "p90", "max", "unit", "provenance", "cost_basis",
                        "excluded"}
    assert set(fleet["metrics"]["turns_per_order"]["excluded"]) == {
        "running", "unrecorded", "no_transcript", "os_unattributed_to_turn"}
    assert fleet["version"] == 1 and fleet["floor"] is True
    assert fleet["floor_reason"] == ops.COST_FLOOR_NOTE
    assert fleet["metrics"]["tokens_per_turn.cache_write"]["avg"] == 30.0
    assert fleet["metrics"]["tokens_per_turn.cache_write"]["unit"] == "tokens"
    # The payoff block is the existing arithmetic, verbatim.
    assert "pct_paid_off" in fleet["compaction_payoff"]


def test_an_unknown_project_is_refused_not_reported_as_empty(fleet_fixture):
    """"Nothing ran" is a claim about the fleet; a typo must not make it."""
    with pytest.raises(ops.OpsError, match="not registered"):
        report(project="proj_typo")


def test_read_only(fleet_fixture, monkeypatch):
    """No `ProjectStore`, so no `_migrate`, so not one byte written to a project DB."""
    wo = fleet_fixture.order(session_id="sess-a")
    fleet_fixture.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)

    from jarvis import project_store

    def refuse(*_a, **_k):
        raise AssertionError("fleetcost constructed a ProjectStore")

    monkeypatch.setattr(project_store.ProjectStore, "__init__", refuse)
    before = fleet_fixture.db.stat().st_mtime_ns

    fleet = report()

    assert fleet["orders"]["n"] == 1
    assert fleet_fixture.db.stat().st_mtime_ns == before


# -- 8. the surfaces -------------------------------------------------------------------


def test_the_cli_renders_the_fleet_section(fleet_fixture, capsys):
    from jarvis import cli

    wo = fleet_fixture.order(session_id="sess-a")
    fleet_fixture.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.25)
    fleet_fixture.os_call("compaction", ts=T0 + 10, wo_id=wo, cost_usd=0.5)

    assert cli.main(["cost", "--fleet", "--since", "2026-09-29T04:00:00+00:00",
                     "--until", "2026-10-06T04:00:00+00:00"]) == 0
    out = capsys.readouterr().out
    assert "cost_per_turn_usd" in out
    assert "compaction" in out


def test_the_cli_fleet_json_is_the_payload(fleet_fixture, capsys):
    from jarvis import cli

    wo = fleet_fixture.order(session_id="sess-a")
    fleet_fixture.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.25)

    assert cli.main(["cost", "--fleet", "--json",
                     "--since", "2026-09-29T04:00:00+00:00",
                     "--until", "2026-10-06T04:00:00+00:00"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert set(payload["fleet"]) == FLEET_KEYS


def test_the_default_render_names_subproc_and_shows_subagents(fleet_fixture, capsys):
    """The `sub` column measured subprocesses; subagent spend was never printed at all."""
    from jarvis import cli

    f = fleet_fixture
    wo = f.order(session_id="sess-a")
    f.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)
    f.transcript("sess-a", [f.call_row(at=T0, write=100_000, out=1_000)],
                 subagents=[[f.call_row(at=T0 + 10, mid="s1", write=100_000,
                                        out=1_000)]])

    assert cli.main(["cost"]) == 0
    out = capsys.readouterr().out
    header = next(line for line in out.splitlines() if "work order" in line)

    assert "subproc" in header and "subagent" in header
    assert " sub " not in header, "the ambiguous name is gone"
