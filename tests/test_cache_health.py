"""The fleet's cache configuration as a `jarvis doctor` post-condition (docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md)"""

from __future__ import annotations

import itertools
import json

import pytest

from jarvis import bill, db, invariants, ops, usage
from jarvis.catalog import CatalogError, load_catalog, parse_catalog
from jarvis.project_store import ProjectStore

DAY = 86_400

#: Enough orders and boundaries to clear both volume floors with room to spare
ORDERS = 25
BOUNDARIES_EACH = 4

_SEQ = itertools.count()


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return catalog_file


@pytest.fixture()
def store(started):
    s = ProjectStore(ops.registered_project_paths()["proj_a"])
    try:
        yield s
    finally:
        s.close()


def _seal(store, *, cache_write, ttl_write, prefix_write, boundaries=BOUNDARIES_EACH,
          ttl_boundaries=2, compact_write=0, compact_boundaries=0, age_days=1.0,
          version=5):
    """One settled order with a frozen bill, as `bill._worker_extras` writes it"""
    wo = store.create_work_order(f"order {next(_SEQ)}", status="completed")
    share = ttl_write / (ttl_write + prefix_write) if ttl_write + prefix_write else None
    tax = (ttl_write + prefix_write) * 3 // 2
    rewrite = {
        "tokens": tax,
        "list_usd": 1.0,
        "boundaries": boundaries,
        "ttl_share": share,
        "ttl_tokens": round(tax * share) if share is not None else 0,
        "ttl_boundaries": ttl_boundaries,
    }
    if version >= 3:
        rewrite |= {"ttl_write": ttl_write, "prefix_write": prefix_write,
                    "cache_write": cache_write}
    if version >= 4:
        rewrite |= {"compact_write": compact_write,
                    "compact_boundaries": compact_boundaries}
    if version >= 5:
        # Whole rollup on the main side, subagent side a structural zero: these cohorts model single-session...
        rewrite |= {"by_side": {
            "main": {"tokens": tax, "boundaries": boundaries,
                     "cache_write": cache_write},
            "subagent": {"tokens": 0, "boundaries": 0, "cache_write": 0,
                         "count": 0, "structural_zero": True},
        }}
    payload = {"payload_v": version, "total": {"cost": {"list_usd": 10.0}},
               "rewrite": rewrite}
    store.seal_bill(wo["id"], json.dumps(payload), at=db.now() - age_days * DAY)
    return wo["id"]


def _cohort(store, *, cache_write, ttl_write, prefix_write, orders=ORDERS, **kw):
    """`orders` identical sealed bills, so the fleet's summed ratios are each order's"""
    for _ in range(orders):
        _seal(store, cache_write=cache_write, ttl_write=ttl_write,
              prefix_write=prefix_write, **kw)


def _one(found, invariant):
    matching = [v for v in found if v.invariant == invariant]
    assert len(matching) <= 1, f"{invariant} raised {len(matching)} times"
    return matching[0] if matching else None


# -- 1. the break-even, and the single place it is spelled


def test_the_break_even_is_derived_from_the_three_rates():
    """39.5% is arithmetic, not a preference: the 1h premium over the 5m write"""
    assert usage.TTL_BREAK_EVEN == pytest.approx(0.75 / 1.90)


def test_the_script_and_the_invariant_share_one_definition():
    """The cohort script is what the violation tells the reader to run"""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "scripts/cache_ttl_cohort.py"
    spec = importlib.util.spec_from_file_location("cache_ttl_cohort", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.TRIGGER is usage.TTL_BREAK_EVEN


# -- 2


def test_a_cohort_over_the_break_even_on_the_TAX_alone_stays_quiet(store):
    """THE REGRESSION THIS FILE EXISTS FOR. 300k of 500k classified writes is 60% of the tax"""
    _cohort(store, cache_write=1_000_000, ttl_write=300_000, prefix_write=200_000)
    writes = bill.cache_writes_since(store, db.now() - 30 * DAY)
    assert writes.ttl_share_of_tax == pytest.approx(0.60)
    assert writes.ttl_share_of_writes == pytest.approx(0.30)
    assert writes.ttl_share_of_tax > usage.TTL_BREAK_EVEN > writes.ttl_share_of_writes

    assert _one(invariants.check_os(), "INV-CACHE-TTL-TRIGGER") is None


def test_a_cohort_over_the_break_even_on_ALL_WRITES_is_reported(store):
    _cohort(store, cache_write=1_000_000, ttl_write=450_000, prefix_write=100_000)
    found = _one(invariants.check_os(), "INV-CACHE-TTL-TRIGGER")
    assert found is not None
    assert found.context["ttl_share_of_writes"] == pytest.approx(0.45)
    assert "45.0%" in found.detail and "39.5%" in found.detail


def test_the_violation_prints_both_ratios_and_says_which_one_decides(store):
    """A reader who has met the share-of-the-tax figure elsewhere must not be able to meet this one alone..."""
    _cohort(store, cache_write=1_000_000, ttl_write=450_000, prefix_write=100_000)
    found = _one(invariants.check_os(), "INV-CACHE-TTL-TRIGGER")
    assert found.context["ttl_share_of_tax"] == pytest.approx(0.45 / 0.55)
    assert "81.8%" in found.detail                     # the share of the tax, labelled…
    assert "NOT for comparison" in found.detail        # …and disarmed
    assert found.detail.count(bill.COHORT_COMMAND) == 1  # named once, where it is explained


# -- 3. the quiet day


def test_too_few_orders_reports_nothing_however_wild_the_ratio(store):
    """A fleet that settled three work orders has a ratio"""
    _cohort(store, cache_write=1_000_000, ttl_write=900_000, prefix_write=90_000,
            orders=3)
    writes = bill.cache_writes_since(store, db.now() - 30 * DAY)
    assert writes.ttl_share_of_writes == pytest.approx(0.90)   # far over the break-even
    assert writes.prefix_share_of_writes == pytest.approx(0.09)

    found = invariants.check_os()
    assert _one(found, "INV-CACHE-TTL-TRIGGER") is None
    assert _one(found, "INV-PREFIX-DRIFT") is None


def test_too_few_boundaries_reports_nothing_even_with_orders_enough(store):
    """The other floor, and it is not redundant: 30 orders that crossed one boundary each is a..."""
    _cohort(store, cache_write=1_000_000, ttl_write=900_000, prefix_write=90_000,
            orders=30, boundaries=1, ttl_boundaries=1)
    writes = bill.cache_writes_since(store, db.now() - 30 * DAY)
    assert writes.orders == 30            # clears the order floor
    assert writes.boundaries == 30        # ...and fails the boundary floor

    assert _one(invariants.check_os(), "INV-CACHE-TTL-TRIGGER") is None


def test_the_floors_are_a_silence_and_not_a_hedged_finding(store):
    """Reporting a thin cohort with a caveat still puts a number in front of someone who will act on it"""
    _cohort(store, cache_write=1_000_000, ttl_write=900_000, prefix_write=900_000,
            orders=2)
    assert [v for v in invariants.check_os()
            if v.invariant in ("INV-CACHE-TTL-TRIGGER", "INV-PREFIX-DRIFT")] == []


# -- 4. prefix stability


def test_prefix_drift_is_reported_over_the_threshold(store):
    _cohort(store, cache_write=1_000_000, ttl_write=100_000, prefix_write=500_000)
    found = _one(invariants.check_os(), "INV-PREFIX-DRIFT")
    assert found is not None
    assert found.context["prefix_share_of_writes"] == pytest.approx(0.50)
    assert "50.0%" in found.detail


def test_exactly_at_each_threshold_the_two_checks_differ_on_purpose(store):
    """AND THE ASYMMETRY IS THE POINT, not an off-by-one"""
    _cohort(store, cache_write=1_000_000, ttl_write=int(1_000_000 * usage.TTL_BREAK_EVEN),
            prefix_write=450_000)
    writes = bill.cache_writes_since(store, db.now() - 30 * DAY)
    assert writes.ttl_share_of_writes == pytest.approx(usage.TTL_BREAK_EVEN, abs=1e-6)
    assert writes.prefix_share_of_writes == pytest.approx(0.45)

    found = invariants.check_os()
    assert _one(found, "INV-CACHE-TTL-TRIGGER") is None     # neutral, so silent
    assert _one(found, "INV-PREFIX-DRIFT") is not None      # at the ceiling is over it


def test_a_normal_fleet_is_quiet(store):
    """35% of writes is what this fleet measured on 2026-09-16 with the `includeGitInstructions` fix..."""
    _cohort(store, cache_write=1_000_000, ttl_write=350_000, prefix_write=350_000)
    assert _one(invariants.check_os(), "INV-PREFIX-DRIFT") is None


def test_prefix_drift_does_not_read_the_share_of_the_tax(store):
    """THE SAME TRAP FROM THE OTHER SIDE, and the reason the ratio is over all writes"""
    _cohort(store, cache_write=1_000_000, ttl_write=10_000, prefix_write=90_000)
    writes = bill.cache_writes_since(store, db.now() - 30 * DAY)
    assert 1 - writes.ttl_share_of_tax == pytest.approx(0.90)
    assert writes.prefix_share_of_writes == pytest.approx(0.09)

    assert _one(invariants.check_os(), "INV-PREFIX-DRIFT") is None


def test_rising_ttl_expiry_cannot_make_the_prefix_look_better(store):
    """The property that chose this denominator"""
    _cohort(store, cache_write=1_000_000, ttl_write=100_000, prefix_write=500_000)
    before = bill.cache_writes_since(store, db.now() - 30 * DAY)
    _cohort(store, cache_write=1_000_000, ttl_write=300_000, prefix_write=500_000)
    after = bill.cache_writes_since(store, db.now() - 30 * DAY)

    assert (1 - after.ttl_share_of_tax) < (1 - before.ttl_share_of_tax)
    assert after.prefix_share_of_writes == before.prefix_share_of_writes


# -- 5. what the checks can and cannot see


def test_a_bill_sealed_before_the_split_is_counted_not_zeroed(store):
    """An unmeasured order is not evidence of a healthy one"""
    _cohort(store, cache_write=1_000_000, ttl_write=450_000, prefix_write=100_000)
    for _ in range(7):
        _seal(store, cache_write=9_000_000, ttl_write=0, prefix_write=0, version=2)

    writes = bill.cache_writes_since(store, db.now() - 30 * DAY)
    assert writes.orders == ORDERS
    assert writes.unmeasured_orders == 7
    assert writes.ttl_share_of_writes == pytest.approx(0.45)  # undiluted by the seven

    found = _one(invariants.check_os(), "INV-CACHE-TTL-TRIGGER")
    assert "7 more" in found.detail


def test_orders_outside_the_window_are_not_measured(store):
    """A cohort window and never all history: the split between the two causes is drifting"""
    _cohort(store, cache_write=1_000_000, ttl_write=450_000, prefix_write=100_000,
            age_days=90.0)
    assert bill.cache_writes_since(store, db.now() - 30 * DAY).orders == 0
    assert _one(invariants.check_os(), "INV-CACHE-TTL-TRIGGER") is None


def test_the_violation_names_the_population_it_measured(store):
    """The figure is over Jarvis's own dispatched workers"""
    _cohort(store, cache_write=1_000_000, ttl_write=450_000, prefix_write=100_000)
    found = _one(invariants.check_os(), "INV-CACHE-TTL-TRIGGER")
    assert "dispatched workers" in found.detail
    assert f"{ORDERS} settled work orders" in found.detail


# -- 6. the wiring


def test_both_checks_are_os_level_and_reachable_from_doctor(started):
    """OS-level and not per-project: each decides something there is exactly one of"""
    names = [f.__name__ for f in invariants.OS_INVARIANTS]
    assert "check_cache_ttl_trigger" in names
    assert "check_prefix_stable" in names
    assert invariants.check_cache_ttl_trigger not in invariants.INVARIANTS
    assert invariants.check_prefix_stable not in invariants.INVARIANTS

    report = ops.run_doctor(catalog_path=str(started))
    assert report["include_os"] is True      # …and `jarvis doctor` is what runs them
    assert isinstance(report["os"], list)


def test_the_seal_carries_the_raw_split(store):
    """`ttl_share` cannot recover these: it is their ratio"""
    session = usage.SessionUsage(session_id="s", main=usage.Usage(
        cache_write=1_000, rewrite_ttl_write=400, rewrite_prefix_write=100))
    extras = bill._worker_extras(session)
    assert extras["rewrite"]["ttl_write"] == 400
    assert extras["rewrite"]["prefix_write"] == 100
    assert extras["rewrite"]["cache_write"] == 1_000
    assert bill.PAYLOAD_VERSION >= 3


def test_the_fixture_is_shaped_like_a_real_bill(store):
    """THE SEAM BETWEEN THE TWO SIDES"""
    session = usage.SessionUsage(session_id="s", main=usage.Usage(
        cache_write=1_000, rewrite_ttl_write=400, rewrite_prefix_write=100))
    real = bill._worker_extras(session)["rewrite"]

    wo_id = _seal(store, cache_write=1_000, ttl_write=400, prefix_write=100)
    sealed = json.loads(store.get_work_order(wo_id)["bill_json"])["rewrite"]
    assert set(sealed) == set(real)
    # The by-side block is nested, so a key renamed one level down would pass the check above while...
    assert set(sealed["by_side"]) == set(real["by_side"])
    for side in real["by_side"]:
        assert set(sealed["by_side"][side]) == set(real["by_side"][side])


def test_prefix_stability_is_written_down_as_the_authoritative_measurement():
    """The standing consequence, stated where the computation is rather than in the work orders that defer..."""
    doc = invariants.check_prefix_stable.__doc__
    assert "AUTHORITATIVE" in doc
    assert "hashing the rendered system prompt" in doc     # the session-start hook
    assert "recorded baseline" in doc                      # a snapshot-style proxy
    assert "which region of the rendered prompt" in doc     # the structural proxy
    assert "evals/test_prefix_drift.py" in doc              # named, so it resolves
    assert "the API ITSELF reported" in doc                # why this one wins


# -- 7. configuration


def test_the_thresholds_are_fleet_settings(catalog_file):
    cfg = load_catalog(catalog_file).os
    assert cfg.cache_health_window_days == 30
    assert cfg.cache_health_min_orders == 20
    assert cfg.cache_health_min_boundaries == 50
    assert cfg.cache_health_prefix_share == pytest.approx(0.45)


@pytest.mark.parametrize("key, value", [
    ("cache_health_window_days", 0),
    ("cache_health_min_orders", 0),
    ("cache_health_min_boundaries", 0),
    ("cache_health_prefix_share", 45),      # a percentage, not a share
    ("cache_health_prefix_share", 0.0),
    ("cache_health_prefix_share", "0.45"),
])
def test_a_bad_threshold_is_refused_at_boot(key, value):
    """Rejected where it is written, not where it is read: a bad value would otherwise surface as a..."""
    with pytest.raises(CatalogError, match=key):
        parse_catalog({"os": {key: value}, "projects": []})


def test_jarvis_config_set_can_move_a_threshold(started, monkeypatch):
    """A setting nobody can set is a constant with a longer name"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    out = ops.set_config("os.cache_health_prefix_share", 0.6,
                         catalog_path=str(started), reason="test")
    assert out["apply"] == "hot"
    assert out["safety"] is False
    assert load_catalog(started).os.cache_health_prefix_share == pytest.approx(0.6)

    with pytest.raises(Exception, match="cache_health_prefix_share"):
        ops.set_config("os.cache_health_prefix_share", 60,
                       catalog_path=str(started), reason="test")


def test_the_window_is_honoured(store, catalog_file):
    """Setting it narrower must move what the checks can see, or it is not a setting"""
    data = json.loads(catalog_file.read_text())
    data["os"]["cache_health_window_days"] = 2
    catalog_file.write_text(json.dumps(data))
    ops.start_os(str(catalog_file), foreground=True)

    _cohort(store, cache_write=1_000_000, ttl_write=450_000, prefix_write=100_000,
            age_days=5.0)
    assert _one(invariants.check_os(), "INV-CACHE-TTL-TRIGGER") is None
