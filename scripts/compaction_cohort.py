#!/usr/bin/env python3
"""What would compacting at every expired boundary have cost, and what would it save?

The companion to `scripts/cache_ttl_cohort.py`. That one prices the OTHER remedy for
the same waste — buying the one-hour cache write — and answers "not yet". This one
prices compaction, boundary by boundary, over the same cohort window and the same
transcript population, and it is the measurement that set
`catalog.DEFAULT_COMPACT_MIN_CONTEXT`.

    uv run python scripts/compaction_cohort.py --days 30
    uv run python scripts/compaction_cohort.py --days 30 --floor 60000

THE MODEL, and every constant in it is measured rather than chosen. A compaction is a
plain-input call (the CLI does not cache-write it — probe below), so at an EXPIRED
boundary it replaces a 1.25x re-write with a 1.0x re-send and leaves a ~15k summary in
place of the whole history:

    before = 1.25*W_b + sum_j (0.1*read_j + 1.25*write_j + 1.0*input_j)
    after  = 1.0*C + out_rate*S_out                        <- the compaction call itself
           + 1.25*S_write + 0.1*(S_ctx - S_write)          <- the real turn's first call
           + sum_j (0.1*max(0, read_j - (C - S_ctx)) + 1.25*write_j + 1.0*input_j)

W_b and every read_j/write_j/input_j are the fleet's own numbers, read through
`usage.session_calls`. S_out, S_ctx and S_write are `COMPACT_SUMMARY_OUTPUT`,
`COMPACT_CONTEXT` and `COMPACT_FIRST_WRITE`, measured live — see the docstrings there.
`j` ranges over the calls after the boundary up to the NEXT boundary, i.e. the rest of
that turn; the saving compaction goes on producing in every LATER turn is deliberately
left out, so the figure is a floor.

Read-only. Costs nothing and calls no model.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jarvis import usage  # noqa: E402
from jarvis.catalog import (  # noqa: E402
    COMPACT_CONTEXT, COMPACT_FIRST_WRITE, COMPACT_SUMMARY_OUTPUT,
    DEFAULT_COMPACT_MIN_CONTEXT,
)


def _day(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def _rates(model: str) -> tuple[float, float]:
    """(input $/Mtok, output multiple of input) for the model that made a call."""
    price = usage.price_for(model)
    return price[0], price[1] / price[0]


def boundaries(calls: list, compactions: list[float], floor: int) -> list[dict]:
    """Every TTL-EXPIRED boundary in one session, with the rest of its turn attached.

    The classification is `usage._usage_of`'s, restated over the same calls because
    that one totals what this one has to keep apart: a boundary is a call that read
    LESS than the call before it, and it is an expiry when the gap reached the write
    TTL and nothing but the static head survived.

    A boundary the session ALREADY compacted across is skipped: there is nothing left
    to compact there, and counting it would credit this change with a saving the fleet
    has already taken (`usage.Usage.rewrite_compact_write`).
    """
    out: list[dict] = []
    for i, call in enumerate(calls):
        if i == 0:
            continue
        prev = calls[i - 1]
        if call.cache_read >= prev.cache_read:
            continue
        gap = call.ts - prev.ts if call.ts and prev.ts else None
        if gap is None or gap < usage.WRITE_TTL_SECONDS or call.cache_read > floor:
            continue  # the prefix moved, or the entry was still alive: not ours
        if any(prev.ts < c <= call.ts for c in compactions):
            continue  # already compacted across
        rest = []
        for later in calls[i + 1:]:
            if later.cache_read < (rest[-1] if rest else call).cache_read:
                break  # the next boundary: a new turn starts here
            rest.append(later)
        out.append({"call": call, "rest": rest,
                    "context": call.input + call.cache_write + call.cache_read})
    return out


def price(boundary: dict) -> tuple[float, float]:
    """(before, after) for one boundary, in dollars at list prices."""
    call, rest = boundary["call"], boundary["rest"]
    context = boundary["context"]
    rate, out_rate = _rates(call.model)
    per = rate / 1_000_000

    tail_before = sum(usage.CACHE_READ_RATE * c.cache_read
                      + usage.CACHE_WRITE_RATE * c.cache_write
                      + c.input for c in rest)
    shrink = max(0, context - COMPACT_CONTEXT)
    tail_after = sum(usage.CACHE_READ_RATE * max(0, c.cache_read - shrink)
                     + usage.CACHE_WRITE_RATE * c.cache_write
                     + c.input for c in rest)

    before = (usage.CACHE_WRITE_RATE * call.cache_write + call.input
              + usage.CACHE_READ_RATE * call.cache_read + tail_before)
    after = (context + out_rate * COMPACT_SUMMARY_OUTPUT
             + usage.CACHE_WRITE_RATE * COMPACT_FIRST_WRITE
             + usage.CACHE_READ_RATE * (COMPACT_CONTEXT - COMPACT_FIRST_WRITE)
             + tail_after)
    return before * per, after * per


def _classify(calls: list, compactions: list[float], floor: int) -> usage.Usage:
    """`usage._usage_of`'s boundary accounting over calls already in memory.

    Only the three fields the prefix guard needs. Re-stated rather than re-read for the
    reason above; `tests/test_compaction.py` pins it against the real one.
    """
    total = usage.Usage()
    previous = None
    for call in calls:
        total.cache_write += call.cache_write
        if previous is not None and call.cache_read < previous.cache_read:
            expired = (call.ts - previous.ts >= usage.WRITE_TTL_SECONDS
                       and call.cache_read <= floor)
            if any(previous.ts < c <= call.ts for c in compactions):
                total.rewrite_compact_write += call.cache_write
            elif expired:
                total.rewrite_ttl_write += call.cache_write
            else:
                total.rewrite_prefix_write += call.cache_write
        previous = call
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--since", type=_day)
    ap.add_argument("--until", type=_day)
    ap.add_argument("--floor", type=int, default=DEFAULT_COMPACT_MIN_CONTEXT,
                    help="only compact boundaries at or above this context size")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc).timestamp()
    since = args.since if args.since is not None else now - args.days * 86400
    until = args.until if args.until is not None else now

    from jarvis.ops import resolve_catalog
    cold_floor = resolve_catalog().os.cold_prefix_floor

    rows: list[dict] = []
    sessions = 0
    # The aggregate the prefix guard is read off, accumulated from the SAME walk. Every
    # transcript is opened once: at ~0.07s each, a second pass over a month of them is
    # five minutes of re-reading for numbers already in hand (kn-4b2ef07f (3)).
    fleet = usage.Usage()
    for session_id, paths in usage.index_sessions().items():
        started, _ = usage._time_span(sorted(paths)[0])
        if not started or not (since <= started < until):
            continue
        sessions += 1
        calls, compactions = [], []
        for path in sorted(paths):
            calls.extend(usage.calls_of(path))
            compactions.extend(usage.compaction_stamps(path))
        calls.sort(key=lambda c: c.ts)
        fleet = fleet + _classify(calls, sorted(compactions), cold_floor)
        for b in boundaries(calls, sorted(compactions), cold_floor):
            before, after = price(b)
            rows.append({"context": b["context"], "calls": len(b["rest"]),
                         "before": before, "after": after,
                         "write": b["call"].cache_write})

    span = f"{datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d} to " \
           f"{datetime.fromtimestamp(until, timezone.utc):%Y-%m-%d}"
    print(f"cohort {span} — {sessions} sessions, "
          f"{len(rows)} TTL-expired boundaries")
    if not rows:
        return 0

    fired = [r for r in rows if r["context"] >= args.floor]
    skipped = [r for r in rows if r["context"] < args.floor]
    saving = sum(r["before"] - r["after"] for r in fired)
    print(f"  ttl-expiry write volume  {sum(r['write'] for r in rows):>14,} tokens")
    print(f"  what those boundaries cost ${sum(r['before'] for r in rows):>12,.2f}")
    print(f"\n  floor {args.floor:,} tokens of context")
    print(f"    fires on              {len(fired):>6} boundaries "
          f"({sum(r['write'] for r in fired):,} tokens)")
    print(f"      before              ${sum(r['before'] for r in fired):>12,.2f}")
    print(f"      after (net of the compactions) "
          f"${sum(r['after'] for r in fired):>10,.2f}")
    print(f"      saving              ${saving:>12,.2f}")
    print(f"    skips                 {len(skipped):>6} boundaries below the floor; "
          f"compacting them would have "
          f"{'saved' if sum(r['before'] - r['after'] for r in skipped) > 0 else 'COST'} "
          f"${abs(sum(r['before'] - r['after'] for r in skipped)):,.2f}")

    print("\n  by context size — the break-even the floor is set from")
    edges = [0, 25_000, 50_000, 60_000, 75_000, 90_000, 100_000, 125_000, 150_000,
             200_000, 300_000, 10**9]
    for lo, hi in zip(edges, edges[1:]):
        band = [r for r in rows if lo <= r["context"] < hi]
        if not band:
            continue
        delta = [r["before"] - r["after"] for r in band]
        print(f"    {lo:>7,}-{hi if hi < 10**9 else 0:<7,} "
              f"n={len(band):>4}  median ${statistics.median(delta):>7.3f}  "
              f"total ${sum(delta):>9.2f}  "
              f"positive {sum(1 for d in delta if d > 0) / len(band):>5.0%}  "
              f"worst ${min(delta):>7.3f}  "
              f"median calls after {statistics.median(r['calls'] for r in band):>4.0f}")

    # THE OTHER GUARD, and the one a saving can quietly break. Both cache-health
    # post-conditions are RATIOS over every written token, so removing the TTL writes
    # raises every other share without anything getting worse. What the prefix check
    # would read after this ships is arithmetic, not a guess, so it is printed here
    # beside the saving that causes it (`invariants.check_prefix_stable`).
    removed = sum(r["write"] for r in fired)
    added = COMPACT_FIRST_WRITE * len(fired)
    after_writes = fleet.cache_write - removed + added
    print(f"\n  cache writes over the cohort {fleet.cache_write:>14,}")
    print(f"    prefix-invalidation share  {fleet.rewrite_prefix_write / fleet.cache_write:>13.1%}"
          f"   <- what check_prefix_stable reads today")
    print(f"    after this change          "
          f"{fleet.rewrite_prefix_write / after_writes:>13.1%}"
          f"   over {after_writes:,} written tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
