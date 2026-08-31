#!/usr/bin/env python3
"""Is it time to buy the one-hour prompt cache yet?

`jarvis cost` prints this split for one order or for the fleet's whole history. This
prints it for a COHORT WINDOW, which is the form the decision actually needs: the answer
moves as the OS changes, and a figure averaged over all history hides the trend that
will eventually reverse it. Measured 2026-08-30, as a share of ALL cache writes: 0.3%
was TTL expiry before the `includeGitInstructions` fix, 15.4% after it, and 20.4% over
the trailing week — against a 39.5% break-even. See
`docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md`.

Classification is `usage.read_session`'s own, not a second implementation, so this
script and `jarvis cost` cannot drift apart — including the `os.cold_prefix_floor`
threshold, which both resolve from the catalog.

    uv run python scripts/cache_ttl_cohort.py --days 30
    uv run python scripts/cache_ttl_cohort.py --since 2026-08-15 --until 2026-08-23
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jarvis import usage  # noqa: E402

#: A 1h write costs 2.0x base input where a 5m write costs 1.25x, and a read is 0.1x
#: under either. So switching pays 0.75x more on EVERY written token and buys back 1.15x
#: on only those re-written because a 5m entry expired:
#:     1h wins iff  W_ttl / W_total > 0.75 / 1.90
TRIGGER = (usage.CACHE_WRITE_1H_RATE - usage.CACHE_WRITE_RATE) / (
    (usage.CACHE_WRITE_1H_RATE - usage.CACHE_WRITE_RATE)
    + (usage.CACHE_WRITE_RATE - usage.CACHE_READ_RATE))


def _day(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30,
                    help="cohort window ending now (default 30)")
    ap.add_argument("--since", type=_day, help="YYYY-MM-DD, overrides --days")
    ap.add_argument("--until", type=_day, help="YYYY-MM-DD")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc).timestamp()
    since = args.since if args.since is not None else now - args.days * 86400
    until = args.until if args.until is not None else now

    # Resolved once, and loudly: with no catalog registered this raises rather than
    # classifying every boundary against a threshold nobody configured.
    from jarvis.ops import resolve_catalog
    floor = resolve_catalog().os.cold_prefix_floor

    total = usage.Usage()
    sessions = 0
    for session_id, paths in usage.index_sessions().items():
        # A session belongs to the cohort it STARTED in: its turns are one conversation
        # and splitting them across windows would attribute a boundary to a window whose
        # configuration did not produce it.
        started, _ = usage._time_span(sorted(paths)[0])
        if not started or not (since <= started < until):
            continue
        sessions += 1
        total = total + usage.read_session(session_id, floor).total

    span = f"{datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d} to " \
           f"{datetime.fromtimestamp(until, timezone.utc):%Y-%m-%d}"
    print(f"cohort {span} — {sessions} sessions")
    if not sessions:
        return 0

    share = total.rewrite_ttl_share
    print(f"  cache write        {total.cache_write:>15,}")
    print(f"  re-write tax       {total.rewrite_excess:>15,}  "
          f"across {total.resume_boundaries} boundaries")
    if share is None:
        print("  no boundary was classified — nothing to decide on")
        return 0
    prefix = total.resume_boundaries - total.boundaries_ttl
    print(f"    prefix moved     {total.rewrite_excess - total.rewrite_ttl_excess:>15,}  "
          f"{prefix} boundaries — no TTL helps this")
    print(f"    TTL expired      {total.rewrite_ttl_excess:>15,}  "
          f"{total.boundaries_ttl} boundaries — a longer TTL buys this back")

    # THE TRIGGER'S DENOMINATOR IS EVERY WRITTEN TOKEN, NOT THE TAX. Switching pays the
    # 1h premium on the whole cache-write line, so the share that decides it is
    # W_ttl/cache_write. The share OF THE TAX is a much larger number about a much
    # smaller base, and reading one against the other says "nearly worth switching" when
    # the truth is "off by a factor of two" — which is how this script earned its place.
    ttl_write = total.rewrite_ttl_write
    decisive = ttl_write / total.cache_write if total.cache_write else 0.0
    saving = (usage.CACHE_WRITE_RATE - usage.CACHE_READ_RATE) * ttl_write
    penalty = (usage.CACHE_WRITE_1H_RATE - usage.CACHE_WRITE_RATE) * (
        total.cache_write - ttl_write)
    print(f"\n  TTL share of all writes {decisive:>10.1%}   <- the trigger, "
          f"which fires above {TRIGGER:.1%}")
    print(f"  TTL share of the tax    {share:>10.1%}       (context only — NOT the "
          f"trigger; different denominator)")
    verdict = "SWITCH to the 1-hour write" if saving > penalty else "KEEP the 5-minute write"
    print(f"  {verdict} — 1h would buy back {saving:,.0f} and cost "
          f"{penalty:,.0f} base-input-token equivalents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
