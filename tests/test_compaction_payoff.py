"""The payoff arithmetic of `scripts/compaction_payoff.py`, on synthetic inputs.

No real state: every case is built in memory, priced on Opus ($5/MTok input), so each
expected figure below is hand arithmetic over the rates in `usage`.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from jarvis import usage
from jarvis.usage import Call

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "compaction_payoff.py"
_spec = importlib.util.spec_from_file_location("compaction_payoff", SCRIPT)
assert _spec is not None and _spec.loader is not None
cp =importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cp  # dataclasses resolve annotations through it
_spec.loader.exec_module(cp)

OPUS = "claude-opus-5"
PER = 5.0 / 1_000_000   # $ per input token on Opus
T = 1_000_000.0         # the compaction instant


def call(ts, *, read=0, write=0, inp=2, out=100, model=OPUS):
    return Call(ts=ts, model=model, input=inp, cache_write=write, cache_read=read,
                output=out)


def case(calls, **kw):
    kw.setdefault("cost_usd", 1.0)
    kw.setdefault("ts", T + 60)
    kw.setdefault("boundary", {"ts": T, "pre": 150_000, "post": 8_000,
                               "trigger": "manual"})
    kw.setdefault("later_boundaries", [T])
    kw.setdefault("window", 400_000)
    return cp.Case(wo_id="wo-x", calls=calls, **kw)


def before():
    # Last call before C: 149,898 prompt + 100 output + 2 input = N of 150,000.
    return call(T - 600, read=140_000, write=9_898)


def test_removed_is_before_minus_first_call_after():
    first = call(T + 120, read=13_000, write=36_998)          # S = 50,000
    row = cp.assess(case([before(), first]))
    assert row["context_before"] == 150_000
    assert row["context_after"] == 50_000
    assert row["removed"] == 100_000


def test_first_relaunch_is_priced_as_a_rewrite_of_the_removed_tokens():
    first = call(T + 120, read=13_000, write=36_998)
    row = cp.assess(case([before(), first], cost_usd=0.5))
    expected = 100_000 * usage.CACHE_WRITE_RATE * PER       # $0.625
    assert row["savings_write_usd"] == pytest.approx(expected)
    assert row["savings_usd"] == pytest.approx(expected)
    assert row["net_usd"] == pytest.approx(expected - 0.5)
    assert row["paid_off"] is True
    assert row["break_even_turn"] == 1


def test_cache_reads_afterwards_save_a_tenth_each():
    calls = [before(), call(T + 120, read=13_000, write=36_998)]
    calls += [call(T + 130 + i, read=50_000 + i * 1000, write=1000) for i in range(10)]
    row = cp.assess(case(calls, cost_usd=10.0))
    assert row["calls_after"] == 11
    assert row["write_calls_after"] == 1
    assert row["savings_read_usd"] == pytest.approx(10 * 100_000 * 0.1 * PER)
    assert row["savings_usd"] == pytest.approx(100_000 * PER * (1.25 + 10 * 0.1))
    assert row["paid_off"] is False
    assert row["break_even_turn"] is None


def test_a_later_ttl_expiry_is_a_second_rewrite():
    calls = [before(),
             call(T + 120, read=13_000, write=36_998),
             call(T + 130, read=50_000, write=500),
             call(T + 2_000, read=13_000, write=38_000)]       # read went backwards
    row = cp.assess(case(calls))
    assert row["write_calls_after"] == 2
    assert row["savings_write_usd"] == pytest.approx(2 * 100_000 * 1.25 * PER)
    assert row["savings_read_usd"] == pytest.approx(100_000 * 0.1 * PER)


def test_uncached_call_pays_full_input_rate():
    calls = [before(), call(T + 120, read=13_000, write=36_998),
             call(T + 130, read=0, write=0, inp=51_000)]
    row = cp.assess(case(calls))
    assert row["savings_input_usd"] == pytest.approx(100_000 * PER)


def test_one_hour_write_is_priced_at_its_own_rate():
    first = Call(ts=T + 120, model=OPUS, input=2, cache_write=36_998, cache_read=13_000,
                 output=10, cache_1h=36_998, cache_5m=0)
    row = cp.assess(case([before(), first]))
    assert row["savings_write_usd"] == pytest.approx(
        100_000 * usage.CACHE_WRITE_1H_RATE * PER)


def test_model_price_comes_from_the_call():
    first = call(T + 120, read=13_000, write=36_998, model="claude-sonnet-4-5")
    row = cp.assess(case([before(), first]))
    assert row["savings_usd"] == pytest.approx(100_000 * 1.25 * 3.0 / 1_000_000)


def test_segment_stops_at_the_next_compaction_and_credits_its_smaller_input():
    nxt = T + 5_000
    calls = [before(), call(T + 120, read=13_000, write=36_998),
             call(T + 130, read=50_000, write=500),
             call(nxt + 120, read=13_000, write=36_998)]        # after the next one
    row = cp.assess(case(calls, later_boundaries=[T, nxt]))
    assert row["calls_after"] == 2
    assert row["savings_next_compaction_usd"] == pytest.approx(100_000 * PER)
    assert row["segment_end"] == nxt


def test_subagent_calls_are_counted_not_priced():
    calls = [before(), call(T + 120, read=13_000, write=36_998)]
    row = cp.assess(case(calls, subagent_stamps=[T - 5, T + 200, T + 300]))
    assert row["subagent_calls_after"] == 2
    assert row["savings_usd"] == pytest.approx(100_000 * 1.25 * PER)


def test_capped_when_the_counterfactual_overflows_the_window():
    calls = [before(), call(T + 120, read=13_000, write=36_998),
             call(T + 130, read=50_000, write=500),
             call(T + 140, read=250_000, write=500)]            # 250.5k + 100k > 300k
    row = cp.assess(case(calls, window=300_000))
    assert row["capped"] is True
    # The first two calls count; the overflowing one and anything after do not.
    assert row["savings_usd"] == pytest.approx(100_000 * PER * (1.25 + 0.1))


def test_open_when_live_and_unclosed_and_excluded_from_the_rate():
    calls = [before(), call(T + 120, read=13_000, write=36_998)]
    live = cp.assess(case(calls, live=True, cost_usd=5.0))
    done = cp.assess(case(calls, cost_usd=0.1))
    assert live["open"] is True and live["paid_off"] is None
    summary = cp.summarise([live, done])
    assert summary["count"] == 2 and summary["closed"] == 1 and summary["open"] == 1
    assert summary["pct_paid_off"] == 100.0
    assert summary["total_cost_usd"] == pytest.approx(5.1)


def test_no_calls_after_a_finished_order_is_a_pure_loss():
    row = cp.assess(case([before()], cost_usd=0.8))
    assert row["savings_usd"] == 0
    assert row["net_usd"] == pytest.approx(-0.8)
    assert row["paid_off"] is False and row["open"] is False


def test_failed_compaction_saves_nothing():
    calls = [before(), call(T + 120, read=140_000, write=10_000)]
    row = cp.assess(case(calls, ok=False, boundary=None, later_boundaries=[]))
    assert row["removed"] == 0 and row["savings_usd"] == 0
    assert any("failed" in n for n in row["notes"])


def test_back_to_back_compactions_score_as_a_chain():
    c1, c2 = T, T + 3_000
    calls = [before(), call(c2 + 120, read=13_000, write=36_998)]
    first = cp.assess(case(calls, later_boundaries=[c1, c2]))
    second = cp.assess(case(calls, boundary={"ts": c2, "pre": 3_000, "post": 9_000,
                                             "trigger": "manual"},
                            later_boundaries=[c1, c2]))
    assert first["savings_usd"] == 0
    assert any("back-to-back" in n for n in first["notes"])
    assert second["removed"] == 100_000
    assert any("chained" in n for n in second["notes"])


def test_break_even_counts_turns():
    turns = [{"seq": 2, "kind": "compact", "started_at": T - 100},
             {"seq": 3, "kind": "message", "started_at": T + 100},
             {"seq": 4, "kind": "message", "started_at": T + 1_000}]
    calls = [before(), call(T + 120, read=13_000, write=36_998),
             call(T + 1_100, read=50_000, write=500)]
    # Turn 3 saves $0.625, turn 4 $0.05: a $0.65 compaction breaks even in turn 2.
    row = cp.assess(case(calls, turns=turns, cost_usd=0.65))
    assert row["turns_after"] == 2
    assert row["break_even_turn"] == 2


def test_summary_quantiles_and_fleet_break_even():
    rows = []
    for n_turns, cost, saving in ((1, 1.0, 3.0), (2, 1.0, 0.5), (5, 1.0, 2.0)):
        rows.append({"open": False, "ok": True, "capped": False, "boundary_found": True,
                     "cost_usd": cost, "savings_usd": saving, "turns_after": n_turns,
                     "paid_off": saving > cost, "break_even_turn": 1 if saving > cost
                     else None})
    s = cp.summarise(rows)
    assert s["paid_off"] == 2 and s["pct_paid_off"] == pytest.approx(66.7)
    assert s["net_usd"] == pytest.approx(2.5)
    assert s["turns_after_median"] == 2 and s["turns_after_p90"] == 5
    assert s["savings_per_turn_usd"] == pytest.approx(5.5 / 8, abs=1e-4)
    assert s["break_even_turns_fleet"] == pytest.approx(1.0 / (5.5 / 8), abs=0.01)


def test_rows_carry_every_stable_key():
    row = cp.assess(case([before(), call(T + 120, read=13_000, write=36_998)]))
    assert set(cp.ROW_KEYS) <= set(row)
    json.dumps(row)


def test_the_script_re_exports_the_package_objects_themselves():
    """The pure core lives in `jarvis.compaction_payoff`; the script re-exports it.

    Identity, not equality: two copies of `assess` would fork the arithmetic silently.
    Spec §1 of docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md.
    """
    import jarvis.compaction_payoff as core

    assert cp.assess is core.assess
    assert cp.Case is core.Case
    assert cp.analyse is core.analyse
    assert cp.summarise is core.summarise
    assert cp.gather is core.gather
    assert cp.connect_ro is core.connect_ro
    assert cp.prefix_rate is core.prefix_rate
    assert cp.ROW_KEYS is core.ROW_KEYS
    assert cp.quantile is core.quantile
    assert cp._parse_when is core.parse_when


def test_gather_opens_databases_read_only(tmp_path):
    db = tmp_path / "os.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    ro = cp.connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (1)")
