#!/usr/bin/env python3
"""Did each worker-conversation compaction the OS paid for actually pay off?

The retrospective companion to `scripts/compaction_cohort.py`. That one PREDICTS what
compacting at every TTL-expired boundary would save; this one MEASURES the compactions
the daemon really ran (`worker_session.compact_before_relaunch`), one by one, against
what the same work order went on to do afterwards.

    JARVIS_HOME=/home/gonzalo/workspace/production/state \\
        uv run python scripts/compaction_payoff.py --since 2026-09-28 [--json]

THE ARITHMETIC LIVES IN `jarvis.compaction_payoff` and is re-exported below, not copied:
this script is its renderer. Two copies of `assess` would fork silently, so the module's
objects are imported by identity and `tests/test_compaction_payoff.py` asserts it. See
that module's docstring for the sources, the method and every approximation, and §1 of
docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md for why the move happened.

Deterministic and read-only: every SQLite database is opened `mode=ro`, transcripts are
only read, no model is called.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jarvis import paths  # noqa: E402
from jarvis.compaction_payoff import (  # noqa: E402,F401
    MATCH_SLACK_SECONDS,
    MODEL_CONTEXT_WINDOW,
    RATE_INPUT,
    RATE_READ,
    RATE_WRITE,
    ROW_KEYS,
    TERMINAL_STATUSES,
    Case,
    analyse,
    assess,
    connect_ro,
    gather,
    parse_when,
    prefix_rate,
    quantile,
    summarise,
)

#: The pre-move private names, kept so a caller of either spelling gets the SAME object.
_parse_when = parse_when
_quantile = quantile


# ---------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------

def _fmt_k(n: int | None) -> str:
    return "-" if n is None else f"{n / 1000:,.0f}k"


def render_text(rows: Sequence[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        f"{'when (UTC)':<20} {'wo':<12} {'before':>7} {'after':>6} {'removed':>7} "
        f"{'calls':>5} {'wr':>3} {'sub':>4} {'turns':>5} {'cost':>7} {'savings':>8} "
        f"{'net':>8} {'b/e':>4}  flags",
    ]
    for r in rows:
        flags = []
        if r["open"]:
            flags.append("OPEN")
        elif r["paid_off"]:
            flags.append("paid")
        else:
            flags.append("LOSS")
        if r["capped"]:
            flags.append("capped")
        if not r["boundary_found"]:
            flags.append("no-boundary")
        if not r["ok"]:
            flags.append("failed")
        lines.append(
            f"{r['when'][:16].replace('T', ' '):<20} {r['wo_id']:<12} "
            f"{_fmt_k(r['context_before']):>7} {_fmt_k(r['context_after']):>6} "
            f"{_fmt_k(r['removed']):>7} {r['calls_after']:>5} "
            f"{r['write_calls_after']:>3} {r['subagent_calls_after']:>4} "
            f"{r['turns_after']:>5} ${r['cost_usd']:>6.2f} ${r['savings_usd']:>7.2f} "
            f"{'+' if r['net_usd'] >= 0 else '-'}${abs(r['net_usd']):>6.2f} "
            f"{r['break_even_turn'] if r['break_even_turn'] is not None else '-':>4}  "
            f"{' '.join(flags)}")
    s = summary

    def opt(v: Any, fmt: str = "{}") -> str:
        return "-" if v is None else fmt.format(v)

    lines += [
        "",
        f"compactions        {s['count']}  ({s['closed']} closed, {s['open']} open, "
        f"{s['capped']} capped, {s['failed']} failed, "
        f"{s['unmatched_boundaries']} without a transcript boundary)",
        f"total cost         ${s['total_cost_usd']:,.2f}",
        f"est. savings       ${s['total_savings_usd']:,.2f}",
        f"net                ${s['net_usd']:,.2f}",
        f"closed only        cost ${s['closed_cost_usd']:,.2f}  savings "
        f"${s['closed_savings_usd']:,.2f}  net ${s['closed_net_usd']:,.2f}",
        f"paid off           {s['paid_off']}/{s['closed']} closed "
        f"({opt(s['pct_paid_off'], '{}%')})",
        f"turns after        median {opt(s['turns_after_median'])}  "
        f"p90 {opt(s['turns_after_p90'])}",
        f"break-even         median turn {opt(s['break_even_turn_median'])} "
        f"(paid-off rows); fleet: {opt(s['break_even_turns_fleet'])} turns at "
        f"{opt(s['savings_per_turn_usd'], '${}')} saved per turn",
        "",
        "savings = the R removed tokens re-priced on every later main-chain call at the "
        "rate it paid for its prefix (write 1.25x / read 0.1x / input 1x); R is a floor "
        "(see --help). b/e = post-compaction turn by which savings covered the cost.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=parse_when,
                    help="YYYY-MM-DD or ISO datetime (naive = UTC), inclusive")
    ap.add_argument("--until", type=parse_when, help="same, exclusive")
    ap.add_argument("--project")
    ap.add_argument("--wo")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    home = paths.jarvis_home()
    if not (home / "os.db").exists():
        print(f"no os.db under {home} (set JARVIS_HOME)", file=sys.stderr)
        return 2
    rows = analyse(gather(home, since=args.since, until=args.until,
                          project=args.project, wo=args.wo))
    summary = summarise(rows)
    if args.json:
        print(json.dumps({
            "version": 1,
            "jarvis_home": str(home),
            "since": args.since, "until": args.until,
            "project": args.project, "wo": args.wo,
            "summary": summary,
            "compactions": [{k: r[k] for k in ROW_KEYS} for r in rows],
        }, indent=2))
    else:
        print(render_text(rows, summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
