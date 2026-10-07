---
name: compaction-payoff
description: Use when asked whether Jarvis OS's worker-conversation compactions pay off, what compaction costs or saves, whether `os.compact_min_context` is tuned right, or when the `compaction` line of `jarvis cost` / `agent_calls` looks high. Runs the mechanical, read-only `scripts/compaction_payoff.py` against the OS state and reports cost vs. counterfactual savings. No model calls.
allowed-tools: Bash(uv run python scripts/compaction_payoff.py:*)
---

# Does compaction pay off?

Before relaunching a worker conversation whose prompt cache has expired, the daemon may
compact it (`worker_session.compaction_due`). The compaction is a model turn that re-reads
the whole conversation and writes a summary, so it costs money up front; it only pays if
later turns run on the smaller context. `scripts/compaction_payoff.py` measures that per
compaction, deterministically, from `agent_calls` (what each compaction cost) and the
worker transcripts (what every later API call actually paid).

**Do not re-derive this by hand or with an ad-hoc script.** Run the script, report its
numbers. The methodology (how tokens removed, rates and the counterfactual are computed,
every approximation) is the module docstring — `--help` prints it.

## Run it

```bash
# production state (the fleet the user cares about). Run from the repo checkout.
JARVIS_HOME=/home/gonzalo/workspace/production/state \
  uv run python scripts/compaction_payoff.py --since 2026-09-28 [--until D] [--project P] [--wo WO_ID] [--json]
```

- `--since/--until`: date or ISO datetime, naive = UTC, since inclusive / until exclusive.
  A Claude usage week runs Monday 21:00 America/Los_Angeles (= Tuesday 04:00 UTC).
- `--json`: stable keys (`version`, `summary`, `rows`) — use it when you need to compute
  anything further; never scrape the text table.
- Omit `JARVIS_HOME` only when you mean the dev instance.

## Report

Lead with the summary: count, total cost, estimated savings, net, % of CLOSED compactions
that paid off, median/p90 turns after a compaction, break-even turn. Then the few worst
and best rows (wo, cost, savings, net) and why the worst lost (almost always: the order
finished within ~1 turn of the compaction).

Read the flags before concluding anything:
- `open` — the order is still running; its savings can still grow. Excluded from the paid-off %.
- `capped` — without compaction Claude Code would have hit `worker.autocompact_window`
  and auto-compacted anyway; savings stop there, so the row is a floor.
- `back-to-back` / `chained` — a compaction followed by another with no API call between
  them (summarising a summary). That is an OS defect, not a tuning question: report it
  with `jarvis bug report` rather than adjusting thresholds.
- unmatched boundary / $0 cost — the CLI did nothing; moves no money.

Savings are an estimate (removed tokens are a floor; later turns are assumed unchanged).
Dollars are list prices, a unit for comparison — not a bill.
