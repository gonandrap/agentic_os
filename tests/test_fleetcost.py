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
from zoneinfo import ZoneInfo

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


# -- 1b. the window SELECTOR -----------------------------------------------------------
#
# §1-§4 of docs/superpowers/specs/2026-10-07-cost-window-selector.md. One resolver, read
# by the CLI, `--json` and the page, so a bad parameter is a refusal on all three.

WEEK_SECONDS = 7 * 86400


def resolved(**kwargs):
    kwargs.setdefault("cfg", catalog.CostConfig())
    kwargs.setdefault("now", T0)
    return fleetcost.resolve_window(**kwargs)


def test_week_offsets_step_back_whole_local_weeks():
    windows = {n: resolved(window="week", offset=n) for n in (0, -1, -4)}

    assert (windows[0]["since"], windows[0]["until"]) == (SINCE, UNTIL)
    assert windows[0]["source"] == "usage-week"
    for n in (-1, -4):
        w = windows[n]
        assert w["source"] == "week-offset"
        assert w["window"] == "week" and w["offset"] == n
        # Seven LOCAL days, and the configured hour at each end.
        local = datetime.fromtimestamp(w["since"], ZoneInfo("America/Los_Angeles"))
        assert (local.hour, local.weekday()) == (21, 0)
        assert w["until"] - w["since"] == WEEK_SECONDS
    # The windows abut exactly: no gap and no overlap at a boundary.
    assert windows[-1]["until"] == windows[0]["since"]
    assert windows[-4]["since"] == SINCE - 4 * WEEK_SECONDS


def test_week_across_dst_in_both_directions():
    """A week spanning a transition is seven LOCAL days: 169 or 167 absolute hours."""
    autumn = resolved(window="week", now=stamp("2026-10-28T12:00:00+00:00"))
    spring = resolved(window="week", now=stamp("2027-03-10T12:00:00+00:00"))

    assert autumn["until"] - autumn["since"] == 169 * 3600
    assert spring["until"] - spring["since"] == 167 * 3600
    for w in (autumn, spring):
        local = datetime.fromtimestamp(w["since"], ZoneInfo("America/Los_Angeles"))
        assert (local.hour, local.weekday()) == (21, 0)
        # Both ends printed: one abbreviation across a transition is wrong at one end.
        assert "PDT" in w["local_label"] and "PST" in w["local_label"]
    assert autumn["local_label"] == "2026-10-26 21:00 PDT to 2026-11-02 21:00 PST"
    # The OFFSET path crosses it too, not just the week containing `now`.
    back = resolved(window="week", offset=-1, now=stamp("2026-11-04T12:00:00+00:00"))
    assert back["until"] - back["since"] == 169 * 3600
    assert back["local_label"] == autumn["local_label"]


def test_week_boundary_second_either_side():
    one_before = resolved(window="week", now=SINCE - 1)
    assert (one_before["since"], one_before["until"]) == (SINCE - WEEK_SECONDS, SINCE)
    earlier = resolved(window="week", offset=-1, now=SINCE - 1)
    assert earlier["until"] == SINCE - WEEK_SECONDS
    assert resolved(window="week", now=SINCE)["since"] == SINCE


def test_session_window_grid():
    cfg = catalog.CostConfig()
    now = SINCE + 12 * 3600
    current = resolved(window="5h", now=now, cfg=cfg)

    assert (current["since"], current["until"]) == (SINCE + 10 * 3600,
                                                    SINCE + 15 * 3600)
    assert current["source"] == "session-window"
    assert current["window"] == "5h" and current["offset"] == 0
    assert resolved(window="5h", offset=-1, now=now, cfg=cfg)["since"] == \
        SINCE + 5 * 3600
    assert resolved(window="5h", offset=-3, now=now, cfg=cfg)["since"] == \
        current["since"] - 15 * 3600
    # A non-default length moves every edge, and a fractional one is legal.
    hourly = resolved(window="5h", now=now,
                      cfg=catalog.CostConfig(session_window_hours=1.0))
    assert (hourly["since"], hourly["until"]) == (now, now + 3600)
    half = resolved(window="5h", now=now,
                    cfg=catalog.CostConfig(session_window_hours=2.5))
    assert (half["since"], half["until"]) == (SINCE + 10 * 3600, SINCE + 12.5 * 3600)


def test_session_window_steps_across_a_week_start():
    """Continuous in ABSOLUTE time: the step stays the length, across the reset (§2)."""
    now = SINCE + 2 * 3600
    steps = [resolved(window="5h", offset=-n, now=now) for n in range(4)]

    for earlier, later in zip(steps[1:], steps):
        assert later["since"] - earlier["since"] == 5 * 3600
        assert earlier["until"] == later["since"]
    # It crosses the reset rather than re-anchoring on the previous week's own grid,
    # which is the accepted non-alignment: 168 is not a multiple of 5.
    assert steps[1]["since"] < SINCE < steps[0]["until"]


def test_custom_range():
    for until in ("2026-10-06", "2026-10-06T04:00:00+00:00", "2026-10-06T04:00"):
        window = resolved(since="2026-09-29T04:00", until=until)
        assert window["since"] == SINCE
        assert window["source"] == "flags"
        assert window["window"] is None and window["offset"] is None
    # Naive input is UTC, the way `--since` already reads it.
    assert resolved(since="2026-09-29T04:00:00")["since"] == SINCE


def test_refusals():
    with pytest.raises(ops.OpsError, match=r"window must be one of week, 5h — 'month' "
                                           r"is not a window this report knows"):
        resolved(window="month")
    with pytest.raises(ops.OpsError,
                       match="offset must be 0 or negative — 1 names a window that has "
                             "not happened yet"):
        resolved(window="week", offset=1)
    for since, until in ((UNTIL, SINCE), (SINCE, SINCE)):
        with pytest.raises(ops.OpsError, match="since must be before until"):
            resolved(since=since, until=until)
    with pytest.raises(ops.OpsError, match="which is an empty window"):
        resolved(since=SINCE, until=SINCE)
    with pytest.raises(
            ops.OpsError,
            match=r"--since/--until and --window/--offset are two ways to name the same "
                  r"thing — pass one or the other, not both"):
        resolved(window="week", since=SINCE)
    # The same refusal covers an offset beside a custom range.
    with pytest.raises(ops.OpsError, match="pass one or the other, not both"):
        resolved(offset=-1, until=UNTIL)


def test_window_of_still_resolves_the_week():
    cfg = catalog.CostConfig()
    week = fleetcost.window_of(None, None, cfg, now=T0)

    assert (week["since"], week["until"]) == (SINCE, UNTIL)
    assert week["source"] == "usage-week"
    assert week["window"] == "week" and week["offset"] == 0
    flagged = fleetcost.window_of(SINCE, UNTIL, cfg, now=T0)
    assert flagged["source"] == "flags" and flagged["window"] is None


# -- 1c. the DISPLAY zone --------------------------------------------------------------
#
# §11 of docs/superpowers/specs/2026-10-07-cost-window-selector.md. Display only: the
# boundaries stay anchored in `cfg.week_reset_zone`, so picking a zone must move NO
# number.


def test_the_default_zone_comes_from_the_catalog():
    cfg = catalog.CostConfig()

    assert fleetcost.resolve_zone(None, cfg) == cfg.week_reset_zone
    # Empty is ABSENT, not a bad zone: a blank form field is not a refusal.
    assert fleetcost.resolve_zone("", cfg) == cfg.week_reset_zone
    assert fleetcost.resolve_zone("Europe/Berlin", cfg) == "Europe/Berlin"
    # And it is read from the catalog, never a module constant.
    moved = catalog.CostConfig(week_reset_zone="Asia/Tokyo")
    assert fleetcost.resolve_zone(None, moved) == "Asia/Tokyo"

    week = resolved(window="week")
    assert week["zone"] == catalog.CostConfig().week_reset_zone
    assert week["local_label"] == "2026-09-28 21:00 to 2026-10-05 21:00 PDT"


def test_a_picked_zone_moves_the_label_and_no_number():
    """The load-bearing test: the zone is DISPLAY ONLY (Neo q1460)."""
    default = resolved(window="week")
    berlin = resolved(window="week", tz="Europe/Berlin")

    assert berlin["zone"] == "Europe/Berlin"
    assert berlin["local_label"] != default["local_label"]
    assert berlin["local_label"] == "2026-09-29 06:00 to 2026-10-06 06:00 CEST"
    # Byte-identical boundaries: the week stays anchored in `week_reset_zone`.
    assert berlin["since"] == default["since"]
    assert berlin["until"] == default["until"]
    assert berlin["label"] == default["label"]


def test_a_bad_zone_is_a_refusal_never_a_fallback():
    for bad in ("Mars/Olympus", "not a zone", "../etc/passwd"):
        with pytest.raises(ops.OpsError) as caught:
            resolved(window="week", tz=bad)
        assert str(caught.value) == (
            f"tz must be an IANA time zone name — {bad!r} is not a zone this report "
            f"knows")
        with pytest.raises(ops.OpsError):
            fleetcost.resolve_zone(bad, catalog.CostConfig())


def test_a_dst_change_in_a_picked_zone_prints_both_abbreviations():
    """Berlin's autumn transition is 2026-10-25, inside this usage week."""
    week = resolved(window="week", tz="Europe/Berlin",
                    now=stamp("2026-10-21T12:00:00+00:00"))

    assert "CEST" in week["local_label"] and "CET" in week["local_label"]
    assert week["local_label"] == "2026-10-20 06:00 CEST to 2026-10-27 05:00 CET"


def test_the_zone_rides_on_every_source():
    windows = (resolved(window="week", tz="Asia/Tokyo"),
               resolved(window="5h", offset=-2, tz="Asia/Tokyo"),
               resolved(since=SINCE, until=UNTIL, tz="Asia/Tokyo"),
               fleetcost.window_of(None, None, catalog.CostConfig(), now=T0,
                                   tz="Asia/Tokyo"))

    assert {w["source"] for w in windows} == {"usage-week", "session-window", "flags"}
    for w in windows:
        assert w["zone"] == "Asia/Tokyo"
        assert "JST" in w["local_label"]


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
    # §10.6: ADDITIVE under the existing key, and `version` stays 1 — the subtree
    # carries its own.
    "tools",
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
    assert set(fleet["window"]) == {"since", "until", "label", "local_label", "source",
                                    "window", "offset", "zone"}
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


def test_the_report_takes_a_named_window_or_a_resolved_one(fleet_fixture):
    """`resolved` wins and is refused beside any raw parameter: no "which one was it" (§6)."""
    wo = fleet_fixture.order(session_id="sess-a")
    fleet_fixture.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)

    week = ops.fleet_cost(window="week", offset=-1, now=T0)["fleet"]
    assert week["window"]["source"] == "week-offset"
    assert week["orders"]["n"] == 0, "last week, and the turn ran this week"

    picked = ops.cost_window(window="5h", now=T0)
    session = ops.fleet_cost(resolved=picked)["fleet"]
    assert session["window"] == picked
    # The anchor travels in the payload, not in one renderer (§2).
    assert fleetcost.SESSION_ANCHOR_NOTE in session["notes"]
    assert fleetcost.SESSION_ANCHOR_NOTE not in week["notes"]
    with pytest.raises(ops.OpsError, match="pass one or the other, not both"):
        ops.fleet_cost(resolved=picked, window="week")


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


def test_the_cli_takes_a_named_window_and_an_offset(fleet_fixture, capsys):
    """§6: `--fleet` is the windowed surface on the CLI; the bare listing is not."""
    from jarvis import cli

    wo = fleet_fixture.order(session_id="sess-a")
    fleet_fixture.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.25)

    assert cli.main(["cost", "--fleet", "--json", "--window", "5h",
                     "--offset", "-2"]) == 0
    window = json.loads(capsys.readouterr().out)["fleet"]["window"]

    assert window["window"] == "5h" and window["offset"] == -2
    assert window["source"] == "session-window"
    # The local clock the reset is specified in, beside the UTC label (§4).
    assert window["local_label"] and "UTC" in window["label"]
    assert cli.main(["cost", "--fleet", "--window", "week", "--offset", "-1"]) == 0
    assert "usage week" not in capsys.readouterr().out


def test_the_cli_refuses_a_window_beside_a_custom_range(fleet_fixture, capsys):
    from jarvis import cli

    assert cli.main(["cost", "--fleet", "--window", "week",
                     "--since", "2026-09-29T04:00:00+00:00"]) == 1

    assert ("--since/--until and --window/--offset are two ways to name the same thing "
            "— pass one or the other, not both") in capsys.readouterr().err


def test_the_cli_prints_the_local_window_beside_the_utc_one(fleet_fixture, capsys):
    from jarvis import cli

    wo = fleet_fixture.order(session_id="sess-a")
    fleet_fixture.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.25)

    assert cli.main(["cost", "--fleet", "--since", "2026-09-29T04:00:00+00:00",
                     "--until", "2026-10-06T04:00:00+00:00"]) == 0

    out = capsys.readouterr().out
    assert "2026-09-29 04:00 to 2026-10-06 04:00 UTC" in out
    assert "2026-09-28 21:00 to 2026-10-05 21:00 PDT" in out


def test_the_cli_takes_a_display_zone(fleet_fixture, capsys):
    """§11: `--tz` so the CLI label and the page label agree, and nothing else moves."""
    from jarvis import cli

    args = ["cost", "--fleet", "--json", "--since", "2026-09-29T04:00:00+00:00",
            "--until", "2026-10-06T04:00:00+00:00"]
    assert cli.main(args) == 0
    plain = json.loads(capsys.readouterr().out)["fleet"]["window"]
    assert cli.main(args + ["--tz", "Europe/Berlin"]) == 0
    berlin = json.loads(capsys.readouterr().out)["fleet"]["window"]

    assert plain["zone"] == "America/Los_Angeles"
    assert berlin["zone"] == "Europe/Berlin"
    assert berlin["local_label"] == "2026-09-29 06:00 to 2026-10-06 06:00 CEST"
    assert (berlin["since"], berlin["until"]) == (plain["since"], plain["until"])
    assert berlin["label"] == plain["label"]
    # A bad zone is the same refusal here as on the page, with a non-zero exit.
    assert cli.main(args + ["--tz", "Mars/Olympus"]) == 1
    assert "is not a zone this report knows" in capsys.readouterr().err


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


# -- 9. §10.5: the Bash command-shape classifier ---------------------------------------
#
# A TABLE, because the rules are ordered and first-match-wins: what breaks is never one
# rule, it is two rules in the wrong order. The tie-break axis is WHAT PRODUCED THE
# BYTES, not what clipped them, so every clipped variant below still names its producer.

CLASSIFIER_CASES = [
    # one per shape the §10.5 table names, in rule order
    ("uv run pytest -q tests/test_fleetcost.py", "pytest"),
    ("python -m pytest -x", "pytest"),
    ("jarvis wo show wo-1", "jarvis"),
    ("git diff --stat", "git_read"),
    ("sed -n '1,50p' src/jarvis/usage.py", "sed_range"),
    ("cat src/jarvis/usage.py", "cat_file"),
    ("grep -rn needle src/", "search"),
    ("rg --json needle", "search"),
    ("head -50 src/jarvis/usage.py", "stream_head_tail"),
    ("ls -la", "other"),
    # the six tie-breaks of §10.5, verbatim
    ("sed -n '1,50p' f | head -20", "sed_range"),
    ("cat f | head", "cat_file"),
    ("git log | head -5", "git_read"),
    ("jarvis cost --json | head -40", "jarvis"),
    ("uv run pytest -q 2>&1 | tail -40", "pytest"),
    ("./build.sh | head", "stream_head_tail"),
    # §10.5's "other by construction" list
    ("git status", "other"),
    ("git commit -m wip", "other"),
    ("sed -i s/a/b/ f", "other"),
    ("sed -n p f", "other"),
    ("sudo systemctl restart jarvisd", "other"),
    # wrappers and assignments are stripped before the head word is read
    ("time cat f", "cat_file"),
    ("JARVIS_HOME=/tmp/h jarvis status", "jarvis"),
]


@pytest.mark.parametrize("command,shape", CLASSIFIER_CASES)
def test_classify_command_table(command, shape):
    assert fleetcost.classify_command(command) == shape


def test_classify_command_adversarial_strings():
    """Two strings that break a naive classifier, and neither may raise.

    A quoted ARGUMENT that reads like a `sed -n` range is not a file dump — posix
    tokenizing is what keeps it out — and an UNBALANCED quote must fall back to
    whitespace splitting rather than let `shlex`'s `ValueError` out into a report.
    """
    assert fleetcost.classify_command('git commit -m "use sed -n 1,5p"') == "other"
    assert fleetcost.classify_command("cat 'f.py | head") == "cat_file"
    # Total: every string has a shape, and the degenerate ones are not a crash.
    for odd in ("", "   ", "|", "&& ||", "sed", 'echo "unclosed', "\x01\x02"):
        assert fleetcost.classify_command(odd) in fleetcost.SHAPES


# -- 10. §10.4/§10.6: what a tool cost, and what it CARRIED ----------------------------
#
# The quantity under test is not the result's size: it is the size times the number of
# later API calls it rode along in. A 20k-token dump read 30 more times is a 600k-token
# charge, which is why `carried_usd` is what the table sorts on.

#: One input token at list price for the fixture's model, and the two rates a carried
#: result can be billed at. Read off `usage` rather than written down, for the same
#: reason §10.4 reuses `prefix_rate`: a second copy of a price is a second answer.
OPUS = 5.0 / 1e6
READ_RATE = 0.10
WRITE_RATE = 1.25


def order_stats(session_id: str, wo_id: str = "wo-1") -> fleetcost.OrderStats:
    return fleetcost.OrderStats(wo_id=wo_id, project="proj_a", title="t",
                                status="completed", session_id=session_id)


def tool_costs(sessions, *, floor=5_000, **cfg_keys):
    cfg = catalog.CostConfig(**cfg_keys)
    return fleetcost.tool_costs([order_stats(s, f"wo-{i}")
                                 for i, s in enumerate(sessions)],
                                cfg=cfg, floor=floor)


def test_carried_cost_stops_at_the_next_compaction(fleet_fixture):
    """A compaction REPLACES the conversation, so the result stops being carried."""
    f = fleet_fixture
    f.transcript("sess-a", [
        f.call_row(at=T0, write=1_000, out=50, tools=[("t1", "Read", {})]),
        f.result_row(("t1", "x" * 40), at=T0 + 1),
        f.call_row(at=T0 + 2, read=1_200, mid="m2"),
        f.call_row(at=T0 + 3, read=1_250, mid="m3"),
        f.compact_row(at=T0 + 4),
        f.call_row(at=T0 + 5, read=100, write=500, mid="m4"),
        f.call_row(at=T0 + 6, read=600, mid="m5"),
    ])

    tools = tool_costs(["sess-a"])
    read = tools["by_tool"]["Read"]

    assert read["result_tokens"] == 150, "1200 - 1000 - 50, token-exact"
    assert read["token_basis"] == {"context_delta": 1, "chars": 0}
    assert read["carried_calls"] == 2, "the two calls before the compaction"
    assert read["carried_tokens"] == 300
    assert tools["totals"]["carried_calls"] == 2


def test_carried_cost_splits_read_ttl_and_prefix(fleet_fixture):
    """TTL expiry and a prefix miss cost the same and have different fixes (§10.4)."""
    f = fleet_fixture
    dump = "sed -n '1,50p' src/jarvis/usage.py"
    f.transcript("sess-a", [
        f.call_row(at=T0, write=50_000, out=100,
                   tools=[("t1", "Bash", {"command": dump})]),
        f.result_row(("t1", "x" * 40), at=T0 + 1),
        f.call_row(at=T0 + 10, read=50_000, input=200, out=100, mid="m2"),   # read
        f.call_row(at=T0 + 620, read=100, write=40_000, out=100, mid="m3"),  # ttl
        f.call_row(at=T0 + 630, read=40_000, out=100, mid="m4"),             # read
        f.call_row(at=T0 + 640, read=100, write=40_000, out=100, mid="m5"),  # prefix
    ])

    shape = tool_costs(["sess-a"])["by_tool"]["Bash"]["shapes"]["sed_range"]

    assert shape["result_tokens"] == 100, "50200 - 50000 - 100"
    assert shape["carried_calls"] == 4
    assert shape["carried_usd"] == pytest.approx(
        100 * OPUS * (2 * READ_RATE + 2 * WRITE_RATE))
    assert shape["carried_read_usd"] == pytest.approx(100 * OPUS * 2 * READ_RATE)
    assert shape["carried_rewrite_ttl_usd"] == pytest.approx(100 * OPUS * WRITE_RATE)
    assert shape["carried_rewrite_prefix_usd"] == pytest.approx(100 * OPUS * WRITE_RATE)
    assert shape["carried_read_usd"] + shape["carried_rewrite_ttl_usd"] + shape[
        "carried_rewrite_prefix_usd"] == pytest.approx(shape["carried_usd"])

    # No floor configured: the TTL split is left OPEN. The field is None — never 0.00,
    # which a renderer would print as "no TTL expiry" (usage.py's standing rule).
    open_split = tool_costs(["sess-a"], floor=None)["by_tool"]["Bash"]
    assert open_split["carried_rewrite_ttl_usd"] is None
    assert open_split["carried_rewrite_prefix_usd"] == pytest.approx(
        100 * OPUS * 2 * WRITE_RATE)
    assert open_split["carried_usd"] == pytest.approx(shape["carried_usd"])


def test_main_and_subagent_are_a_partition(fleet_fixture):
    """And a subagent's result rides on the SUBAGENT's calls, never the lead's."""
    f = fleet_fixture
    f.transcript("sess-a", [
        f.call_row(at=T0, write=1_000, out=50, tools=[("t1", "Read", {})]),
        f.result_row(("t1", "x" * 40), at=T0 + 1),
        f.call_row(at=T0 + 2, read=1_200, mid="m2"),
        f.call_row(at=T0 + 20, read=1_300, mid="m3"),
    ], subagents=[[
        f.call_row(at=T0 + 10, write=500, out=10, mid="s1",
                   tools=[("u1", "Grep", {})]),
        f.result_row(("u1", "y" * 8), at=T0 + 11),
        f.call_row(at=T0 + 12, read=600, mid="s2"),
    ]])

    tools = tool_costs(["sess-a"])
    totals, by_caller = tools["totals"], tools["totals"]["by_caller"]

    assert totals["calls"] == 2 and totals["result_tokens"] == 240
    assert by_caller["main"]["calls"] + by_caller["subagent"]["calls"] == totals["calls"]
    assert (by_caller["main"]["result_tokens"]
            + by_caller["subagent"]["result_tokens"]) == totals["result_tokens"]
    assert by_caller["subagent"]["result_tokens"] == 90, "600 - 500 - 10"
    # m3 at T0+20 is a LEAD call after the subagent's last one: not in its denominator.
    assert tools["by_tool"]["Grep"]["carried_calls"] == 1
    assert tools["by_tool"]["Read"]["carried_calls"] == 2
    assert "by_caller" not in by_caller["main"], "two levels deep, never recursive"


def test_error_results_are_counted_and_not_averaged_away(fleet_fixture):
    """A refused call returns a two-line refusal: counted, never averaged in silently."""
    f = fleet_fixture
    f.transcript("sess-a", [
        f.call_row(at=T0, write=1_000, out=50, tools=[
            ("t1", "Bash", {"command": "cat big.py"}),
            ("t2", "Bash", {"command": "cat other.py"}),
            ("t3", "Bash", {"command": "cat third.py"})]),
        f.result_row(("t1", "Error: refused"), at=T0 + 1),
        f.result_row(("t2", "boom"), at=T0 + 2, is_error=True),
        f.result_row(("t3", "x" * 40), at=T0 + 3),
        f.call_row(at=T0 + 4, read=1_200, mid="m2"),
    ])

    bash = tool_costs(["sess-a"])["by_tool"]["Bash"]

    assert bash["calls"] == 3 and bash["errors"] == 2
    assert bash["result_tokens_max"] > bash["result_tokens_avg"]
    assert bash["shapes"]["cat_file"]["calls"] == 3


def test_an_unmatched_call_is_excluded_not_zero_filled(fleet_fixture):
    """The turn was killed between the `tool_use` and its result."""
    f = fleet_fixture
    f.transcript("sess-a", [
        f.call_row(at=T0, write=1_000, out=50, tools=[("t1", "Read", {})]),
        f.result_row(("t1", "x" * 40), at=T0 + 1),
        f.call_row(at=T0 + 2, read=1_200, mid="m2", tools=[("t2", "Read", {})]),
    ])

    tools = tool_costs(["sess-a"])

    assert tools["excluded"]["unmatched_calls"] == 1
    assert tools["by_tool"]["Read"]["calls"] == 1, "the killed call is not a call"
    assert tools["by_tool"]["Read"]["result_tokens_avg"] == 150.0


def test_shapes_partition_bash_calls_exactly_once(fleet_fixture):
    f = fleet_fixture
    commands = ["sed -n '1,50p' f", "cat f | head", "git log | head -5",
                "uv run pytest -q", "jarvis status", "grep -rn x .", "./b.sh | head",
                "ls", "echo hi"]
    f.transcript("sess-a", [
        f.call_row(at=T0, write=1_000, out=50,
                   tools=[(f"t{i}", "Bash", {"command": c})
                          for i, c in enumerate(commands)]),
        *[f.result_row((f"t{i}", "x" * 40), at=T0 + 1 + i)
          for i in range(len(commands))],
        f.call_row(at=T0 + 50, read=1_200, mid="m2"),
    ])

    bash = tool_costs(["sess-a"])["by_tool"]["Bash"]

    assert bash["calls"] == len(commands)
    assert sum(s["calls"] for s in bash["shapes"].values()) == bash["calls"]
    assert sum(s["result_tokens"] for s in bash["shapes"].values()) == bash[
        "result_tokens"]
    assert set(bash["shapes"]) <= set(fleetcost.SHAPES)
    assert bash["shapes"]["other"]["calls"] == 2, "ls and echo"


TOOLS_KEYS = {"version", "totals", "by_tool", "excluded", "notes", "row_limit"}
TOOL_COST_KEYS = {
    "calls", "errors", "result_tokens", "result_tokens_avg", "result_tokens_p90",
    "result_tokens_max", "carried_calls", "carried_tokens", "carried_usd",
    "carried_read_usd", "carried_rewrite_ttl_usd", "carried_rewrite_prefix_usd",
    "token_basis", "share_of_result_tokens", "share_of_carried_usd", "by_caller",
}


def test_tools_payload_keys_stable(fleet_fixture):
    """§7.9's guard, one level down: a renderer must not be able to rename a field."""
    f = fleet_fixture
    wo = f.order(session_id="sess-a")
    f.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)
    f.transcript("sess-a", [
        f.call_row(at=T0, write=1_000, out=50,
                   tools=[("t1", "Bash", {"command": "cat f"})]),
        f.result_row(("t1", "x" * 40), at=T0 + 1),
        f.call_row(at=T0 + 2, read=1_200, mid="m2"),
    ])

    fleet = report()
    tools = fleet["tools"]

    assert "tools" in set(fleet) == FLEET_KEYS, "additive under the existing key"
    assert fleet["version"] == 1, "§10.6: the parent version is NOT bumped"
    assert tools["version"] == 1
    assert set(tools) == TOOLS_KEYS
    assert set(tools["excluded"]) == {"unmatched_calls", "no_transcript",
                                      "orders_capped", "sessions_walked"}
    assert set(tools["totals"]) == TOOL_COST_KEYS
    bash = tools["by_tool"]["Bash"]
    assert set(bash) == TOOL_COST_KEYS | {"shapes"}, "shapes are Bash's alone"
    # A shape keeps `by_caller` — the table has a main/sub column on every row — and
    # the caller level carries neither, so the structure cannot recurse.
    assert set(bash["shapes"]["cat_file"]) == TOOL_COST_KEYS
    assert set(bash["by_caller"]["main"]) == TOOL_COST_KEYS - {"by_caller"}
    assert "shapes" not in tools["by_tool"]["Bash"]["by_caller"]["main"]
    assert bash["share_of_result_tokens"] == 1.0
    assert len(tools["notes"]) >= 6, "the known inaccuracies ride in the payload"


def test_tools_respects_max_orders(fleet_fixture):
    """The cap is `cost.max_orders`, applied before anything reads a transcript."""
    f = fleet_fixture
    f.set_cost(max_orders=1)
    for i in range(3):
        wo = f.order(session_id=f"sess-{i}")
        f.turn(wo, started_at=T0 + i * 10, ended_at=T0 + i * 10 + 5, cost_usd=1.0)
        f.transcript(f"sess-{i}", [
            f.call_row(at=T0, write=1_000, out=50, tools=[("t1", "Read", {})]),
            f.result_row(("t1", "x" * 40), at=T0 + 1),
            f.call_row(at=T0 + 2, read=1_200, mid="m2"),
        ])

    tools = report()["tools"]

    assert tools["excluded"]["orders_capped"] == 2
    assert tools["excluded"]["sessions_walked"] == 1
    assert tools["totals"]["calls"] == 1


def test_an_order_with_no_transcript_is_counted_not_dropped(fleet_fixture):
    f = fleet_fixture
    wo = f.order(session_id="sess-gone")
    f.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)

    tools = report()["tools"]

    assert tools["excluded"]["no_transcript"] == 1
    assert tools["excluded"]["sessions_walked"] == 0
    assert tools["totals"]["calls"] == 0 and tools["by_tool"] == {}


def test_tools_read_only(fleet_fixture, monkeypatch):
    """Not one byte written to a project DB or to a transcript the report measures."""
    f = fleet_fixture
    wo = f.order(session_id="sess-a")
    f.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)
    f.transcript("sess-a", [
        f.call_row(at=T0, write=1_000, out=50, tools=[("t1", "Read", {})]),
        f.result_row(("t1", "x" * 40), at=T0 + 1),
        f.call_row(at=T0 + 2, read=1_200, mid="m2"),
    ])
    transcript = f.transcript_root / "-proj" / "sess-a.jsonl"

    from jarvis import project_store

    def refuse(*_a, **_k):
        raise AssertionError("fleetcost constructed a ProjectStore")

    monkeypatch.setattr(project_store.ProjectStore, "__init__", refuse)
    before = (f.db.stat().st_mtime_ns, transcript.stat().st_mtime_ns)

    tools = report()["tools"]

    assert tools["totals"]["result_tokens"] == 150
    assert (f.db.stat().st_mtime_ns, transcript.stat().st_mtime_ns) == before


# -- 11. §10.7: the render --------------------------------------------------------------


def test_the_cli_renders_the_tool_table(fleet_fixture, capsys):
    from jarvis import cli

    f = fleet_fixture
    wo = f.order(session_id="sess-a")
    f.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)
    f.transcript("sess-a", [
        f.call_row(at=T0, write=50_000, out=100, tools=[
            ("t1", "Bash", {"command": "sed -n '1,900p' f"}),
            ("t2", "Read", {})]),
        f.result_row(("t1", "x" * 4_000), ("t2", "y" * 40), at=T0 + 1),
        f.call_row(at=T0 + 10, read=50_000, input=20_000, out=100, mid="m2"),
    ])

    assert cli.main(["cost", "--fleet", "--since", "2026-09-29T04:00:00+00:00",
                     "--until", "2026-10-06T04:00:00+00:00"]) == 0
    out = capsys.readouterr().out

    assert "tool cost" in out
    rows = [line for line in out.splitlines() if line.startswith(("Bash", "Read"))]
    assert rows[0].startswith("Bash · sed_range"), "carried $ descending"
    assert "main/sub" in out
    # Bash is one row PER SHAPE and never also a total line: two rows that sum to a
    # third invite the reader to add the wrong pair (§10.7).
    assert not any(line.strip() == "Bash" for line in out.splitlines())


def test_the_render_truncates_at_cost_tool_rows(fleet_fixture, capsys):
    """At most `cost.tool_rows` rows, and a long tool name is elided for DISPLAY only:
    the `--json` payload always carries it in full."""
    from jarvis import cli

    f = fleet_fixture
    f.set_cost(tool_rows=2)
    wo = f.order(session_id="sess-a")
    f.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)
    f.transcript("sess-a", [
        f.call_row(at=T0, write=50_000, out=100, tools=[
            ("t1", "Bash", {"command": "sed -n '1,900p' f"}),
            ("t2", "Read", {}),
            ("t3", "mcp__plugin_serena_serena__find_referencing_symbols", {})]),
        f.result_row(("t1", "x" * 400), ("t2", "y" * 40), ("t3", "z" * 4_000),
                     at=T0 + 1),
        f.call_row(at=T0 + 10, read=50_000, input=20_000, out=100, mid="m2"),
    ])

    assert cli.main(["cost", "--fleet", "--since", "2026-09-29T04:00:00+00:00",
                     "--until", "2026-10-06T04:00:00+00:00"]) == 0
    out = capsys.readouterr().out

    assert "(1 more tool," in out, "nothing is silently dropped"
    assert "mcp__plugin_s…" in out, "the long name is middle-elided"
    assert "find_referencing_symbols" not in out
    long_name = "mcp__plugin_serena_serena__find_referencing_symbols"
    assert long_name in report()["tools"]["by_tool"], "--json carries it in full"


def test_the_dashboard_renders_the_same_payload(fleet_fixture):
    """No second computation: the partial reads `fleet.tools` and nothing else."""
    import jinja2

    from jarvis.ui.app import TEMPLATES

    f = fleet_fixture
    wo = f.order(session_id="sess-a")
    f.turn(wo, started_at=T0, ended_at=T0 + 100, cost_usd=1.0)
    f.transcript("sess-a", [
        f.call_row(at=T0, write=50_000, out=100,
                   tools=[("t1", "Bash", {"command": "sed -n '1,900p' f"})]),
        f.result_row(("t1", "x" * 4_000), at=T0 + 1),
        f.call_row(at=T0 + 10, read=50_000, input=20_000, out=100, mid="m2"),
    ])

    fleet = ops.fleet_cost(since=SINCE, until=UNTIL)["fleet"]
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)))
    html = env.get_template("_fleet_distribution.html").render(
        fleet=fleet, fmt_tok=lambda n: str(n), tool_table=fleetcost.tool_table)

    assert "What the tools cost" in html
    assert "Bash · sed_range" in html
