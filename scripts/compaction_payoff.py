#!/usr/bin/env python3
"""Did each worker-conversation compaction the OS paid for actually pay off?

The retrospective companion to `scripts/compaction_cohort.py`. That one PREDICTS what
compacting at every TTL-expired boundary would save; this one MEASURES the compactions
the daemon really ran (`worker_session.compact_before_relaunch`), one by one, against
what the same work order went on to do afterwards.

    JARVIS_HOME=/home/gonzalo/workspace/production/state \\
        uv run python scripts/compaction_payoff.py --since 2026-09-28 [--json]

Deterministic and read-only: every SQLite database is opened `mode=ro`, transcripts are
only read, no model is called.

SOURCES
  * The compactions and what each COST: central `os.db`, `agent_calls` rows with
    `kind='compaction'` (`worker_session._record_compaction`). `cost_usd` is the CLI's
    own figure. `--since/--until` filter on that row's `ts`.
  * Where the compaction happened in the conversation: the `compact_boundary` system row
    Claude Code writes into the session transcript (`usage.compactions_in`), matched to
    the `agent_calls` row through the compact turn in the project store (`wo_turns`,
    `label = "turn <seq>"`), falling back to the nearest boundary before the row's ts.
  * Every API call the worker made: the transcript, via `usage.calls_of` (one `Call`
    per deduped assistant message). Subagent transcripts (`<session>/subagents/*.jsonl`)
    are counted separately and never priced into the counterfactual.
  * Turn boundaries and whether the order is still live: the project store
    (`wo_turns`, `work_orders.status`).
  * Prices: `usage.price_for`, `usage.write_rate`, `usage.CACHE_READ_RATE` — the model
    each call ACTUALLY used, at Anthropic list prices.

METHOD, for one compaction C on work order W at time t
  N   context just before C = the last main-chain call before t: its prompt
      (input + cache_write + cache_read) plus its output, which the next call re-sends.
  S   context just after C  = the FIRST main-chain call after t: its whole prompt.
  R   tokens removed = max(0, N - S).
      APPROXIMATION: S includes the relaunch prompt P that went out after C, which the
      counterfactual would ALSO have carried, so the true removal is N + P - S. P is not
      separable from the transcript's token counts, so R is a FLOOR (P is ~1-5k against
      an R of ~100k). The CLI's own `preTokens/postTokens` are reported beside it but not
      used: `postTokens` counts the summary only, not the ~40k static head (system prompt,
      tools, CLAUDE.md) that survives every compaction, so pre - post overstates R.
  Without C, every later main-chain call until the next compaction (any trigger) or the
  end of the session would have carried R more prompt tokens. Each is priced at the rate
  THAT call actually paid for its prefix:
      * the first call after C, and any later call whose cache read went BACKWARDS (a
        ttl-expiry / prefix-miss re-write): R x the call's cache-write rate (1.25x at the
        5-minute TTL, more for a 1-hour write — `usage.write_rate`). For the first call
        this is "the relaunch would have re-written N instead of S".
      * a call that read its prefix from cache: R x 0.1.
      * a call with no cache read and no cache write: R x 1.0.
  If the session's next compaction is in range, it would have had R more tokens of plain
  input to summarise: + R x 1.0.
  savings = sum of the above;  net = savings - C.cost_usd;  paid_off = net > 0.
  CAP: when a later call's counterfactual context (its own prompt + R) exceeds the
  project's autocompact window (`worker.autocompact_window`, default
  `catalog.DEFAULT_AUTOCOMPACT_WINDOW`), Claude Code would have auto-compacted there
  anyway. Savings stop accruing at that call and the row is flagged `capped`; the auto-
  compaction's own cost in the counterfactual is NOT credited, so a capped row is a floor.
  OPEN: W is still live (not completed/cancelled/failed) and no later compaction has
  closed C's segment — more savings may still come. Listed, but excluded from the
  paid-off percentage and the break-even figures.
  CHAINS: two compactions with no API call between them (the second summarising a
  summary) are scored as a chain. The first gets no savings (its segment holds no call)
  and is noted `back-to-back`; the second measures N before the chain and is noted
  `chained`, so the chain's combined net is right and neither row invents a saving.
  break_even_turn: the first post-C worker turn (1-based) by whose end cumulative
  savings >= cost, or null. Turns come from `wo_turns` (compact turns excluded); with no
  project store, a turn is approximated as a run of calls between cache re-writes.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jarvis import paths, usage  # noqa: E402
from jarvis.catalog import DEFAULT_AUTOCOMPACT_WINDOW  # noqa: E402
from jarvis.usage import Call  # noqa: E402

#: Work-order statuses after which no further turn can run in the session.
TERMINAL_STATUSES = frozenset({"completed", "cancelled", "failed"})
#: When the window is unbounded (`autocompact_window: null`), the model's own window.
MODEL_CONTEXT_WINDOW = 1_000_000
#: How far after a compaction's recorded ts its transcript boundary may sit, in the
#: fallback match (no project store): the row is written when the turn is reaped.
MATCH_SLACK_SECONDS = 30 * 60

RATE_WRITE, RATE_READ, RATE_INPUT = "write", "read", "input"

#: Stable JSON keys of one analysed row, in output order.
ROW_KEYS = (
    "wo_id", "project", "session_id", "label", "ts", "when", "model", "ok",
    "cost_usd", "boundary_found", "cli_pre_tokens", "cli_post_tokens",
    "context_before", "context_after", "removed", "window",
    "calls_after", "write_calls_after", "subagent_calls_after", "turns_after",
    "savings_usd", "savings_write_usd", "savings_read_usd", "savings_input_usd",
    "savings_next_compaction_usd", "net_usd", "paid_off", "break_even_turn",
    "capped", "open", "segment_end", "notes",
)


# ---------------------------------------------------------------------------------------
# Pure core: in-memory inputs -> rows -> summary. No I/O below until `gather`.
# ---------------------------------------------------------------------------------------

@dataclass
class Case:
    """Everything one compaction's payoff is computed from, already loaded."""

    wo_id: str
    cost_usd: float
    ts: float                       # agent_calls.ts (when the OS recorded the cost)
    project: str = ""
    session_id: str = ""
    label: str = ""
    model: str = ""
    ok: bool = True
    boundary: dict[str, Any] | None = None   # usage.compactions_in entry for C
    later_boundaries: Sequence[float] = ()   # EVERY compact_boundary ts in the session
    calls: Sequence[Call] = ()               # main-chain calls of the session, by ts
    subagent_stamps: Sequence[float] = ()    # ts of every subagent API call
    turns: Sequence[dict[str, Any]] | None = None  # wo_turns rows (seq, kind, started_at)
    window: int = DEFAULT_AUTOCOMPACT_WINDOW
    live: bool = False
    notes: list[str] = field(default_factory=list)


def prefix_rate(call: Call, previous: Call | None, first: bool) -> tuple[str, float]:
    """(kind, multiple of the input price) this call paid for the prefix it carried."""
    if call.cache_read == 0 and call.cache_write == 0:
        return RATE_INPUT, 1.0
    if first or call.cache_read == 0 or (
            previous is not None and call.cache_read < previous.cache_read):
        return RATE_WRITE, usage.write_rate(call.cache_write, call.cache_1h, call.cache_5m)
    return RATE_READ, usage.CACHE_READ_RATE


def _per_token(model: str) -> float:
    return usage.price_for(model)[0] / 1_000_000


def _turn_index(turns: Sequence[dict[str, Any]], ts: float) -> int | None:
    """0-based index of the turn (in `turns`, sorted by start) a call at `ts` ran in."""
    found = None
    for i, turn in enumerate(turns):
        if turn["started_at"] <= ts:
            found = i
        else:
            break
    return found


def assess(case: Case) -> dict[str, Any]:
    """One compaction's payoff. Pure: reads only `case`."""
    notes = list(case.notes)
    t = case.boundary["ts"] if case.boundary else case.ts
    later = sorted(b for b in case.later_boundaries if b > t)
    end = later[0] if later else math.inf
    calls = sorted(case.calls, key=lambda c: c.ts)
    before = [c for c in calls if c.ts < t]
    after = [c for c in calls if t < c.ts < end]

    n = (before[-1].context + before[-1].output) if before else 0
    s = after[0].context if after else 0
    removed = max(0, n - s) if (before and after and case.ok) else 0
    earlier = [b for b in case.later_boundaries if b < t]
    if earlier and before and before[-1].ts < earlier[-1]:
        notes.append("chained: no call since the previous compaction, so N is measured "
                     "before that one and this row carries the chain's removal")
    if case.ok and not after and math.isfinite(end):
        notes.append("back-to-back: another compaction followed before any call")
    if not case.ok:
        notes.append("compaction failed: nothing was removed")
    elif not before:
        notes.append("no main-chain call before the compaction")
    elif after and n <= s:
        notes.append("context after >= before: nothing measurable removed")

    savings = {RATE_WRITE: 0.0, RATE_READ: 0.0, RATE_INPUT: 0.0}
    write_calls = 0
    capped = False
    per_call: list[tuple[float, float]] = []   # (ts, extra $) for the break-even walk
    previous = before[-1] if before else None
    for i, call in enumerate(after):
        kind, mult = prefix_rate(call, previous, first=(i == 0))
        previous = call
        if kind == RATE_WRITE:
            write_calls += 1
        if capped or not removed:
            continue
        if call.context + removed > case.window:
            capped = True
            notes.append(f"counterfactual context {call.context + removed:,} > window "
                         f"{case.window:,}: Claude Code would have auto-compacted")
            continue
        extra = removed * mult * _per_token(call.model)
        savings[kind] += extra
        per_call.append((call.ts, extra))

    next_extra = 0.0
    if removed and not capped and math.isfinite(end):
        model = after[-1].model if after else case.model
        next_extra = removed * 1.0 * _per_token(model)
        per_call.append((end, next_extra))

    # Turns after C, and the turn by which C had paid for itself.
    if case.turns is not None:
        turns = sorted((tr for tr in case.turns
                        if tr.get("kind") != "compact" and t <= tr["started_at"] < end),
                       key=lambda tr: tr["started_at"])
        turn_starts = [{"started_at": tr["started_at"]} for tr in turns]
    else:
        # Approximation: a turn starts at the first call and at every cache re-write.
        turn_starts, prev = [], None
        for i, call in enumerate(after):
            if prefix_rate(call, prev, first=(i == 0))[0] == RATE_WRITE:
                turn_starts.append({"started_at": call.ts})
            prev = call
    turns_after = len(turn_starts)

    total = sum(savings.values()) + next_extra
    net = total - case.cost_usd
    is_open = case.live and not math.isfinite(end)
    break_even = None
    if case.cost_usd <= 0:
        break_even = 0
    else:
        by_turn: dict[int, float] = {}
        for ts, extra in per_call:
            idx = _turn_index(turn_starts, ts)
            by_turn[idx if idx is not None else 0] = (
                by_turn.get(idx if idx is not None else 0, 0.0) + extra)
        running = 0.0
        for idx in sorted(by_turn):
            running += by_turn[idx]
            if running >= case.cost_usd:
                break_even = idx + 1
                break
    if is_open:
        notes.append("open: work order still live and no later compaction yet")

    return {
        "wo_id": case.wo_id, "project": case.project, "session_id": case.session_id,
        "label": case.label, "ts": case.ts,
        "when": datetime.fromtimestamp(case.ts, timezone.utc).isoformat(timespec="seconds"),
        "model": case.model, "ok": case.ok,
        "cost_usd": round(case.cost_usd, 6),
        "boundary_found": case.boundary is not None,
        "cli_pre_tokens": (case.boundary or {}).get("pre"),
        "cli_post_tokens": (case.boundary or {}).get("post"),
        "context_before": n, "context_after": s, "removed": removed,
        "window": case.window,
        "calls_after": len(after), "write_calls_after": write_calls,
        "subagent_calls_after": sum(1 for x in case.subagent_stamps if t < x < end),
        "turns_after": turns_after,
        "savings_usd": round(total, 6),
        "savings_write_usd": round(savings[RATE_WRITE], 6),
        "savings_read_usd": round(savings[RATE_READ], 6),
        "savings_input_usd": round(savings[RATE_INPUT], 6),
        "savings_next_compaction_usd": round(next_extra, 6),
        "net_usd": round(net, 6),
        "paid_off": None if is_open else net > 0,
        "break_even_turn": break_even,
        "capped": capped, "open": is_open,
        "segment_end": end if math.isfinite(end) else None,
        "notes": notes,
    }


def analyse(cases: Iterable[Case]) -> list[dict[str, Any]]:
    """Every case assessed, oldest first."""
    return sorted((assess(c) for c in cases), key=lambda r: r["ts"])


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    # Nearest-rank: deterministic, and an actual observed value.
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def summarise(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Fleet totals over analysed rows. Open rows count in the money, not in the rates."""
    closed = [r for r in rows if not r["open"]]
    paid = [r for r in closed if r["paid_off"]]
    cost = sum(r["cost_usd"] for r in rows)
    savings = sum(r["savings_usd"] for r in rows)
    closed_cost = sum(r["cost_usd"] for r in closed)
    closed_savings = sum(r["savings_usd"] for r in closed)
    closed_turns = sum(r["turns_after"] for r in closed)
    turns = [r["turns_after"] for r in closed]
    break_evens = [r["break_even_turn"] for r in paid if r["break_even_turn"] is not None]
    per_turn = closed_savings / closed_turns if closed_turns else None
    return {
        "count": len(rows),
        "closed": len(closed),
        "open": len(rows) - len(closed),
        "capped": sum(1 for r in rows if r["capped"]),
        "failed": sum(1 for r in rows if not r["ok"]),
        "unmatched_boundaries": sum(1 for r in rows if not r["boundary_found"]),
        "total_cost_usd": round(cost, 4),
        "total_savings_usd": round(savings, 4),
        "net_usd": round(savings - cost, 4),
        "closed_cost_usd": round(closed_cost, 4),
        "closed_savings_usd": round(closed_savings, 4),
        "closed_net_usd": round(closed_savings - closed_cost, 4),
        "paid_off": len(paid),
        "pct_paid_off": round(100 * len(paid) / len(closed), 1) if closed else None,
        "turns_after_median": statistics.median(turns) if turns else None,
        "turns_after_p90": _quantile(turns, 0.9),
        "break_even_turn_median": statistics.median(break_evens) if break_evens else None,
        "savings_per_turn_usd": round(per_turn, 4) if per_turn is not None else None,
        # The fleet-level break-even: how many post-compaction turns, at the average
        # saving per turn, it takes to recover the average compaction's cost.
        "break_even_turns_fleet": (round((closed_cost / len(closed)) / per_turn, 2)
                                   if closed and per_turn else None),
    }


# ---------------------------------------------------------------------------------------
# I/O: read-only loading from $JARVIS_HOME, the project stores and the transcripts.
# ---------------------------------------------------------------------------------------

def connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_when(text: str) -> float:
    """A date or ISO datetime; naive values are UTC."""
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _project_info(central: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    info: dict[str, dict[str, Any]] = {}
    for row in central.execute("SELECT name, path, catalog_json FROM projects"):
        window: int | None = DEFAULT_AUTOCOMPACT_WINDOW
        try:
            worker = (json.loads(row["catalog_json"] or "{}") or {}).get("worker") or {}
            if "autocompact_window" in worker:
                window = worker["autocompact_window"]
        except (ValueError, AttributeError):
            pass
        info[row["name"]] = {"path": Path(row["path"]),
                             "window": window if window else MODEL_CONTEXT_WINDOW}
    return info


def _project_store(path: Path, cache: dict[Path, sqlite3.Connection | None]
                   ) -> sqlite3.Connection | None:
    if path not in cache:
        db = paths.project_db_path(path)
        cache[path] = connect_ro(db) if db.exists() else None
    return cache[path]


def _subagent_stamps(paths_: Sequence[Path]) -> list[float]:
    stamps: list[float] = []
    for path in paths_:
        subdir = path.with_suffix("") / "subagents"
        if subdir.is_dir():
            for sub in sorted(subdir.glob("*.jsonl")):
                stamps.extend(c.ts for c in usage.calls_of(sub))
    return sorted(stamps)


def gather(home: Path, *, since: float | None = None, until: float | None = None,
           project: str | None = None, wo: str | None = None,
           index: dict[str, list[Path]] | None = None) -> list[Case]:
    """Load one Case per recorded compaction. Read-only throughout."""
    central = connect_ro(home / "os.db")
    try:
        projects = _project_info(central)
        sql = ("SELECT ts, project, wo_id, label, model, ok, cost_usd, session_id "
               "FROM agent_calls WHERE kind = 'compaction'")
        args: list[Any] = []
        for clause, value in (("ts >= ?", since), ("ts < ?", until),
                              ("project = ?", project), ("wo_id = ?", wo)):
            if value is not None:
                sql += f" AND {clause}"
                args.append(value)
        records = [dict(r) for r in central.execute(sql + " ORDER BY ts", args)]
    finally:
        central.close()

    index = usage.index_sessions() if index is None else index
    stores: dict[Path, sqlite3.Connection | None] = {}
    sessions: dict[str, tuple[list[Call], list[dict[str, Any]], list[float]]] = {}
    cases: list[Case] = []
    try:
        for rec in records:
            notes: list[str] = []
            proj = projects.get(rec["project"]) or {}
            store = _project_store(proj["path"], stores) if proj else None
            turns = status = None
            session_id = rec["session_id"] or ""
            if store is not None:
                turns = [dict(r) for r in store.execute(
                    "SELECT seq, kind, state, started_at, ended_at FROM wo_turns "
                    "WHERE wo_id = ? ORDER BY seq", (rec["wo_id"],))]
                row = store.execute("SELECT status, session_id FROM work_orders "
                                    "WHERE id = ?", (rec["wo_id"],)).fetchone()
                if row is not None:
                    status = row["status"]
                    session_id = session_id or (row["session_id"] or "")
            else:
                notes.append("no project store: turns approximated, liveness unknown")

            if session_id not in sessions:
                files = sorted(index.get(session_id) or [])
                calls: list[Call] = []
                bounds: list[dict[str, Any]] = []
                for path in files:
                    calls.extend(usage.calls_of(path))
                    bounds.extend(usage.compactions_in(path))
                calls.sort(key=lambda c: c.ts)
                bounds.sort(key=lambda b: b["ts"])
                sessions[session_id] = (calls, bounds, _subagent_stamps(files))
                if not files:
                    notes.append("no transcript found for the session")
            calls, bounds, sub_stamps = sessions[session_id]

            boundary = _match_boundary(rec, turns, bounds)
            if boundary is None and bounds is not None and rec["ok"]:
                notes.append("no compact_boundary matched in the transcript")
            cases.append(Case(
                wo_id=rec["wo_id"], cost_usd=float(rec["cost_usd"] or 0.0),
                ts=float(rec["ts"]), project=rec["project"], session_id=session_id,
                label=rec["label"] or "", model=rec["model"] or "", ok=bool(rec["ok"]),
                boundary=boundary, later_boundaries=[b["ts"] for b in bounds],
                calls=calls, subagent_stamps=sub_stamps, turns=turns,
                window=int(proj.get("window") or DEFAULT_AUTOCOMPACT_WINDOW),
                live=(status is not None and status not in TERMINAL_STATUSES),
                notes=notes))
    finally:
        for conn in stores.values():
            if conn is not None:
                conn.close()
    return cases


def _match_boundary(rec: dict[str, Any], turns: list[dict[str, Any]] | None,
                    bounds: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """The transcript boundary this `agent_calls` row paid for."""
    if not rec["ok"] or not bounds:
        return None
    label = rec["label"] or ""
    if turns and label.startswith("turn "):
        try:
            seq = int(label.split()[1])
        except (IndexError, ValueError):
            seq = None
        turn = next((tr for tr in turns if tr["seq"] == seq), None)
        if turn is not None:
            hi = (turn["ended_at"] or rec["ts"]) + 60
            inside = [b for b in bounds if turn["started_at"] - 1 <= b["ts"] <= hi]
            if inside:
                return inside[-1]
    near = [b for b in bounds
            if rec["ts"] - MATCH_SLACK_SECONDS <= b["ts"] <= rec["ts"] + 60
            and b["trigger"] == "manual"]
    return near[-1] if near else None


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
    ap.add_argument("--since", type=_parse_when,
                    help="YYYY-MM-DD or ISO datetime (naive = UTC), inclusive")
    ap.add_argument("--until", type=_parse_when, help="same, exclusive")
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
