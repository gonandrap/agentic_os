"""`ops.evolution_report` — the one place section 9's numbers are computed.

Spec: docs/superpowers/specs/2026-09-27-self-evolution.md §9. The CLI and the dashboard
are later orders and BOTH render this dict verbatim, so every property worth pinning is
pinned here: a renderer that derived a number of its own is how the two surfaces start
disagreeing about what the registry says.

The three house rules these tests exist to enforce:

1. **Absent is never zero.** No armed rule is not a mechanical share of 0%, a detector
   with no fires has no hit rate, and fewer than `min_samples` is not a median. Each
   absence is `None` PLUS a sentence, and the sentence may not contain a digit that reads
   as a measurement.
2. **A failure never becomes a default answer.** A project store that will not open is
   UNREADABLE in the payload, never a zero and never a false, and it takes nothing else
   down with it.
3. **Counting is of the right thing.** The mechanical share counts ORDERS, not fires, and
   an order that both was mechanically resolved and was investigated is one order.
"""

from __future__ import annotations

import pytest

from jarvis import db, ops, rules
from jarvis.catalog import (
    DEFAULT_RULES_MIN_SAMPLES,
    Catalog,
    OsConfig,
    ProjectSpec,
    RulesConfig,
)
from jarvis.central_store import CentralStore

COND = {"all": [{"field": "status", "op": "eq", "value": "needs_review"},
                {"field": "seconds_in_status", "op": "gte", "value": 3600}]}

#: A Thursday, so the ISO-week Monday the bucketer resolves is unambiguous and a test
#: that walks back one week cannot land on the same bucket.
NOW = 1_760_000_000.0            # 2025-10-09T07:33:20Z
DAY = 86400.0
WEEK = 7 * DAY


@pytest.fixture()
def store(jarvis_home):
    """The registry, with the five builtin seed rows cleared.

    `tests/test_rules_cli.py` gives the reason: this file counts rows, and the seeds are
    proved where they are written. Counting them here too would make every assertion
    here move the day a sixth rule is seeded.
    """
    s = CentralStore()
    s.conn.execute("DELETE FROM remedy_rules")
    s.conn.execute("DELETE FROM detectors")
    s.conn.commit()
    yield s
    s.close()


@pytest.fixture()
def clock(monkeypatch):
    """A movable `db.now`, which is the ONLY sanctioned way to backdate a fire.

    `record_rule_fire` stamps `db.now()` itself and takes no `ts`. Writing a timestamp in
    with raw SQL behind the store's back would also skip the `hits`/`last_fired` bookkeeping
    the same call owns, so the fixture that tests a window has to move the clock.
    """
    holder = {"t": NOW}
    monkeypatch.setattr(db, "now", lambda: holder["t"])
    return holder


def _fire(store, clock, detector_id, *, order_id, outcome=rules.APPLIED,
          mode=rules.DRY_RUN, at=NOW, cleared_after=None, project="proj_a"):
    clock["t"] = at
    row = store.record_rule_fire(detector_id=detector_id, project=project,
                                 order_id=order_id, order_kind="work_order",
                                 fingerprint=f"fp-{order_id}", mode=mode,
                                 outcome=outcome)
    if cleared_after is not None:
        row = store.close_rule_fire(row["id"], now=float(row["ts"]) + cleared_after)
    clock["t"] = NOW
    return row


@pytest.fixture()
def detector(store, clock):
    """Registered ON the frozen clock, so the detector's own `ts` is inside the window
    every test reports over — a row stamped with the real wall clock would sit in the
    future of `NOW` and drop out of the timeline."""
    return store.add_detector("stale-panel-hold", COND, project="proj_a",
                              summary="a panel hold nobody clears", source="io",
                              io_id="io-1", fix_wo_id="wo-9")


def _catalog(tmp_path, *, os_min=DEFAULT_RULES_MIN_SAMPLES, project_min=None):
    project = ProjectSpec(name="proj_a", path=tmp_path / "proj_a")
    if project_min is not None:
        project.rules = RulesConfig(min_samples=project_min)
    return Catalog(os=OsConfig(rules=RulesConfig(min_samples=os_min)),
                   projects=[project])


# -- the mechanical share ---------------------------------------------------------------


def test_the_mechanical_share_counts_orders_and_never_fires(store, detector, clock):
    """One rule firing twice on one order and once on another is TWO orders.

    Counting fires would report the share of a fleet that resolved itself as higher than
    1.0 the moment any condition stood twice on the same order.
    """
    _fire(store, clock, detector["id"], order_id="wo-1", cleared_after=60)
    _fire(store, clock, detector["id"], order_id="wo-1", cleared_after=60)
    _fire(store, clock, detector["id"], order_id="wo-2", cleared_after=60)

    share = ops.evolution_report()["mechanical_share"]

    assert share["numerator"] == 2
    assert share["denominator"] == 2
    assert share["value"] == pytest.approx(1.0)


def test_an_order_both_resolved_and_investigated_is_in_the_denominator_once(
        store, detector, clock, tmp_path):
    from jarvis.project_store import ProjectStore

    path = tmp_path / "proj_a"
    path.mkdir()
    store.upsert_project("proj_a", str(path))
    _fire(store, clock, detector["id"], order_id="wo-1", cleared_after=60)
    _fire(store, clock, detector["id"], order_id="wo-2", cleared_after=60)
    project_store = ProjectStore(path)
    try:
        project_store.create_feature_order("why is wo-1 stuck",
                                           metadata={ops.SUBJECT_KEY: "wo-1"},
                                           kind="investigation")
        project_store.create_feature_order("why is wo-3 stuck",
                                           metadata={ops.SUBJECT_KEY: "wo-3"},
                                           kind="investigation")
    finally:
        project_store.close()

    share = ops.evolution_report()["mechanical_share"]

    assert share["numerator"] == 2
    assert share["denominator"] == 3        # wo-1, wo-2, wo-3 — wo-1 ONCE
    assert share["unreadable_projects"] == []


def test_an_empty_registry_has_no_share_and_the_note_carries_no_zero(store):
    share = ops.evolution_report()["mechanical_share"]

    assert share["value"] is None
    assert share["denominator"] == 0
    assert "no rule has been armed yet" in share["note"]
    # The whole point of the sentence: a fabricated zero reads as a measurement of the
    # fleet, when the truth is that nothing has been given the chance to resolve anything.
    assert "0%" not in share["note"] and "0.0" not in share["note"]


def test_a_project_store_that_will_not_open_is_unreadable_not_a_zero(
        store, detector, clock, tmp_path, monkeypatch):
    """House rule 2: a failure never becomes a default answer.

    The project is NAMED in the payload and every other number survives — a store that
    could not be read contributes no investigations and no claim that there were none.
    """
    path = tmp_path / "proj_a"
    path.mkdir()
    store.upsert_project("proj_a", str(path))
    _fire(store, clock, detector["id"], order_id="wo-1", cleared_after=60)

    def refuse(*a, **kw):
        raise RuntimeError("the project database could not be opened")

    monkeypatch.setattr(ops, "ProjectStore", refuse)

    report = ops.evolution_report()

    assert report["mechanical_share"]["unreadable_projects"] == ["proj_a"]
    assert report["mechanical_share"]["numerator"] == 1
    assert report["mechanical_share"]["denominator"] == 1
    assert report["per_rule"][0]["id"] == detector["id"]


# -- the week bucketer ------------------------------------------------------------------


def test_two_fires_in_one_week_are_one_bucket_of_two(store, detector, clock):
    _fire(store, clock, detector["id"], order_id="wo-1", at=NOW - DAY)
    _fire(store, clock, detector["id"], order_id="wo-2", at=NOW - 2 * DAY)

    by_class = ops.evolution_report()["by_gap_class"]

    assert len(by_class) == 1
    entry = by_class[0]
    assert entry["gap_class"] == "stale-panel-hold"
    assert entry["total"] == 2
    assert len(entry["weeks"]) == 1
    assert entry["weeks"][0]["count"] == 2


def test_a_week_with_no_data_is_absent_from_the_series_not_a_zero_point(
        store, detector, clock):
    """House rule 1 on the series: a week nothing happened in is ABSENT.

    A point with `count: 0` would draw a line through weeks nobody measured, which reads
    as "the share was zero then" rather than "there is nothing to say about then".
    """
    _fire(store, clock, detector["id"], order_id="wo-1", at=NOW - DAY,
          cleared_after=60)
    _fire(store, clock, detector["id"], order_id="wo-2", at=NOW - 2 * WEEK,
          cleared_after=60)

    series = ops.evolution_report()["mechanical_share"]["series"]

    assert len(series) == 2
    assert [p["numerator"] for p in series] == [1, 1]
    weeks = [p["week"] for p in series]
    assert weeks == sorted(weeks) and len(set(weeks)) == 2


# -- per rule ---------------------------------------------------------------------------


def test_a_detector_that_never_fired_has_no_hit_rate(store, detector):
    entry = ops.evolution_report()["per_rule"][0]

    assert entry["id"] == detector["id"]
    assert entry["hit_rate"] is None
    assert "not a hit rate of zero" in entry["hit_rate_note"]
    assert entry["median_cleared_seconds"] is None
    assert entry["median_cleared_seconds_note"]


def test_a_median_under_min_samples_is_not_a_median(store, detector, clock,
                                                    tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "resolve_catalog", lambda *a, **k: _catalog(tmp_path))
    _fire(store, clock, detector["id"], order_id="wo-1", cleared_after=30)
    _fire(store, clock, detector["id"], order_id="wo-2", cleared_after=90)

    entry = ops.evolution_report()["per_rule"][0]

    assert entry["median_cleared_seconds"] is None
    assert "fewer than the 3" in entry["median_cleared_seconds_note"]


# -- stuck resolution -------------------------------------------------------------------


def test_stuck_resolution_under_min_samples_reports_insufficient_history(
        store, detector, clock, tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "resolve_catalog", lambda *a, **k: _catalog(tmp_path))
    for n, order in enumerate(("wo-1", "wo-2", "wo-3")):
        _fire(store, clock, detector["id"], order_id=order, mode=rules.DRY_RUN,
              cleared_after=60 + n)
    _fire(store, clock, detector["id"], order_id="wo-4", mode=rules.ARMED,
          cleared_after=10)

    entry = ops.evolution_report()["stuck_resolution"][0]

    assert entry["gap_class"] == "stale-panel-hold"
    assert entry["dry_run_median"] is None
    assert entry["armed_median"] is None
    assert entry["delta"] is None
    assert "3 dry-run and 1 armed samples" in entry["note"]
    assert "not enough history to compare yet" in entry["note"]


def test_stuck_resolution_compares_once_both_sides_have_enough(
        store, detector, clock, tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "resolve_catalog",
                        lambda *a, **k: _catalog(tmp_path, os_min=2))
    for order, secs in (("wo-1", 100), ("wo-2", 300)):
        _fire(store, clock, detector["id"], order_id=order, mode=rules.DRY_RUN,
              cleared_after=secs)
    for order, secs in (("wo-3", 10), ("wo-4", 30)):
        _fire(store, clock, detector["id"], order_id=order, mode=rules.ARMED,
              cleared_after=secs)

    entry = ops.evolution_report()["stuck_resolution"][0]

    assert entry["dry_run_median"] == pytest.approx(200.0)
    assert entry["armed_median"] == pytest.approx(20.0)
    assert entry["delta"] == pytest.approx(-180.0)
    assert entry["note"] is None


# -- recurrences ------------------------------------------------------------------------


def _recur(store, detector_id, verdict, order_id):
    return store.add_recurrence(gap_class="stale-panel-hold", detector_id=detector_id,
                                project="proj_a", order_id=order_id, verdict=verdict)


def test_verdict_counts_cover_all_four_and_unreadable_stays_out_of_the_comparison(
        store, detector, clock):
    """kn-289d4b89: `unreadable` and `not_armed` are not evidence about either half.

    An unreadable recurrence is the record that nothing could be judged, and a not-armed
    one is a statement about the switch above the rule. Folding either into
    missed-vs-remedy_failed would attribute to the condition or the remedy a failure
    neither of them had.
    """
    _recur(store, detector["id"], rules.MISSED, "wo-1")
    _recur(store, detector["id"], rules.REMEDY_FAILED, "wo-2")
    for order in ("wo-3", "wo-4", "wo-5"):
        _recur(store, detector["id"], rules.UNREADABLE_RECURRENCE, order)
    _recur(store, detector["id"], rules.NOT_ARMED, "wo-6")

    section = ops.evolution_report()["recurrences"]

    assert section["verdict_counts"] == {rules.MISSED: 1, rules.REMEDY_FAILED: 1,
                                         rules.UNREADABLE_RECURRENCE: 3,
                                         rules.NOT_ARMED: 1}
    assert section["weaker_half"]["weaker"] is None        # 1 against 1 is not a tie
    assert section["weaker_half"]["missed"] == 1
    assert section["weaker_half"]["remedy_failed"] == 1
    assert section["weaker_half"]["excluded"] == {rules.UNREADABLE_RECURRENCE: 3,
                                                  rules.NOT_ARMED: 1}
    assert "equally often" in section["weaker_half"]["note"]
    assert len(section["rows"]) == 6


def test_the_weaker_half_names_conditions_when_more_was_missed(store, detector):
    _recur(store, detector["id"], rules.MISSED, "wo-1")
    _recur(store, detector["id"], rules.MISSED, "wo-2")
    _recur(store, detector["id"], rules.REMEDY_FAILED, "wo-3")

    half = ops.evolution_report()["recurrences"]["weaker_half"]

    assert half["weaker"] == "conditions"
    assert "conditions" in half["note"]


def test_no_recurrence_at_all_is_its_own_sentence(store, detector):
    half = ops.evolution_report()["recurrences"]["weaker_half"]

    assert half["weaker"] is None
    assert "no recurrence" in half["note"]


# -- the timeline -----------------------------------------------------------------------


def test_the_timeline_folds_the_registry_and_omits_a_missing_link(store, detector,
                                                                  clock):
    store.retract_detector(detector["id"], "fixed in code")

    timeline = ops.evolution_report()["timeline"]
    kinds = [e["kind"] for e in timeline]

    assert "detector_added" in kinds
    assert "detector_retracted" in kinds
    assert "gap_class_first_seen" in kinds
    assert [e["ts"] for e in timeline] == sorted((e["ts"] for e in timeline),
                                                 reverse=True)
    added = next(e for e in timeline if e["kind"] == "detector_added")
    assert added["links"]["io"] == "io-1"
    # NO BROKEN URL: the detector carries no issue and no pull request, so neither key is
    # there at all rather than present and empty.
    assert "issue" not in added["links"] and "pr" not in added["links"]


# -- min_samples resolution -------------------------------------------------------------


def test_min_samples_takes_the_project_override_and_falls_back_to_the_fleet(
        store, tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "resolve_catalog",
                        lambda *a, **k: _catalog(tmp_path, os_min=4, project_min=7))

    assert ops.evolution_report("proj_a")["min_samples"] == 7
    assert ops.evolution_report()["min_samples"] == 4
    # A project the catalog does not name keeps the fleet answer rather than raising.
    assert ops.evolution_report("proj_b")["min_samples"] == 4


def test_min_samples_falls_back_to_the_shipped_default_with_no_catalog(store):
    assert ops.evolution_report()["min_samples"] == DEFAULT_RULES_MIN_SAMPLES
