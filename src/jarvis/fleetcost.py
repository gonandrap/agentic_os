"""What a TYPICAL order costs — the distribution `jarvis cost` could never report.

`ops.cost_report` is a LISTING: one row per order, dearest first, and a listing has no
place to put a population statistic or a time window. So this adds a section rather than
re-shaping rows, and it lives in its own module for one hard reason: every database here
is opened `mode=ro` through `compaction_payoff.connect_ro`, and `ops` constructs
`ProjectStore`, whose `__init__` runs `_migrate()` (project_store.py:118). A report must
not migrate the thing it is measuring.

Deterministic and read-only: no model call, no write, no `ProjectStore`.

Spec §2-§5 of docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md.

TWO RULES THAT OVERRIDE THE SPEC AS WRITTEN, decided after it was reviewed:

  * MONEY OVER A MIXED POPULATION IS NEVER BLENDED (kn-e6bb1166 ruling 2).
    `wo_turns.cost_source` splits the turns in two: the `envelope` turns (the `claude`
    CLI's own `total_cost_usd`) are the headline figure, and the `transcript` turns — a
    list-price FLOOR for a turn whose envelope never arrived, issue #471 — are a SECOND
    figure with its own `n` and its own label. One average over both would be an average
    of two currencies.
  * `usage.BOUNDARY_UNDECIDED` is REPORTED, as its own count. All four causes
    `usage.classify_boundaries` can return appear, so the boundary lines reconcile
    against the transcript instead of quietly losing the ones no floor could decide.

And three the spec states that are easy to lose in a refactor:

  * IDLE IS EXCLUDED BY CONSTRUCTION. A turn's wall clock is its own
    `ended_at - started_at`, so time between turns — waiting for a Neo answer, parked on
    a usage limit, waiting for the user — is outside every interval. Nothing subtracts
    idle because there is none to subtract.
  * A RUNNING TURN (`ended_at IS NULL`) IS EXCLUDED and counted in `excluded.running`,
    never counted as zero duration.
  * `--since`/`--until` FILTER TURNS, not order creation. A long-running order is
    TRUNCATED to its in-window turns, counted once, and marked.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from jarvis import compaction_payoff, paths, usage
from jarvis.catalog import CostConfig
from jarvis.compaction_payoff import connect_ro, parse_when, quantile
from jarvis.project_store import (
    COMPACT_TURN,
    COST_FROM_ENVELOPE,
    COST_FROM_TRANSCRIPT,
    TURN_KINDS,
)

#: Payload version. Bumped only when a key is REMOVED or re-meant; additive keys do not.
PAYLOAD_VERSION = 1

#: The kinds of `wo_turns` row that are a worker TURN. `compact` is a turn the OS paid
#: for and the worker never saw, so it is counted on its own line and never here.
WORKER_TURN_KINDS = tuple(k for k in TURN_KINDS if k != COMPACT_TURN)

#: Provenance labels — what KIND of number a metric is, in the payload and on screen.
WO_TURNS, ENVELOPE, TRANSCRIPT, MIXED, RECORD = (
    "wo_turns", "envelope", "transcript", "mixed", "record")

#: Units, so a renderer can format a figure without knowing which metric it is.
USD, TOKENS, SECONDS, COUNT, SHARE = "usd", "tokens", "seconds", "count", "share"

#: The four ways a value can be left OUT of a metric. Every metric carries all four, so
#: a reader never has to wonder whether a missing key means zero or means unmeasured.
EXCLUDED_KEYS = ("running", "unrecorded", "no_transcript", "os_unattributed_to_turn")

#: Said in the payload rather than in a renderer, for `ops.COST_FLOOR_NOTE`'s reason: a
#: caveat that appears in one surface and not another is one the reader learns to ignore.
NOTES = (
    "idle is excluded by construction: a turn's wall clock is its own ended_at - "
    "started_at, so waiting between turns is outside every interval",
    "p90 is nearest-rank, so p90 and max are both values some order actually had and "
    "each can name its work order",
    "money from the claude CLI's envelope and money derived from a transcript are two "
    "currencies and are never averaged together: the transcript figures are a floor and "
    "are reported as their own metric with their own n (kn-e6bb1166)",
    "a turn still running is excluded and counted, never counted as zero",
    "boundary counts are per SESSION, not per window: a cache boundary is a property of "
    "the conversation and the transcript carries no window",
)


# ---------------------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------------------

def usage_week(now: float, cfg: CostConfig) -> tuple[float, float]:
    """The Claude usage week containing `now`, as (since, until).

    VERIFIED: the week resets Monday 21:00 America/Los_Angeles, so Mon 2026-09-28 21:00
    PDT = 2026-09-29 04:00 UTC and the week of that date runs to 2026-10-06 04:00 UTC.
    Day, hour and zone all come from `CostConfig` and never from a literal here: a DST
    shift and an Anthropic policy change both move them.

    The arithmetic is done on the LOCAL wall clock and converted once, so a week that
    spans a DST change is still seven local days and still starts at the configured
    hour — absolute arithmetic on an aware datetime would slide the reset by an hour.
    """
    zone = ZoneInfo(cfg.week_reset_zone)
    local = datetime.fromtimestamp(now, zone).replace(tzinfo=None)
    start = local.replace(hour=cfg.week_reset_hour, minute=0, second=0, microsecond=0)
    start -= timedelta(days=(start.weekday() - cfg.week_reset_weekday) % 7)
    if start > local:
        start -= timedelta(days=7)
    return (start.replace(tzinfo=zone).timestamp(),
            (start + timedelta(days=7)).replace(tzinfo=zone).timestamp())


def window_of(since: float | str | None, until: float | str | None, cfg: CostConfig,
              *, now: float | None = None) -> dict[str, Any]:
    """The window to report over: the flags if given, else the current usage week."""
    if since is not None or until is not None:
        start = _as_ts(since) if since is not None else 0.0
        end = _as_ts(until) if until is not None else _now(now)
        source = "flags"
    else:
        start, end = usage_week(_now(now), cfg)
        source = "usage-week"
    return {"since": start, "until": end, "label": _label(start, end), "source": source}


def _now(now: float | None) -> float:
    from jarvis import db

    return db.now() if now is None else now


def _as_ts(value: float | str) -> float:
    return parse_when(value) if isinstance(value, str) else float(value)


def _label(since: float, until: float) -> str:
    fmt = "%Y-%m-%d %H:%M"
    return (f"{datetime.fromtimestamp(since, timezone.utc).strftime(fmt)} to "
            f"{datetime.fromtimestamp(until, timezone.utc).strftime(fmt)} UTC")


# ---------------------------------------------------------------------------------------
# Rows and populations
# ---------------------------------------------------------------------------------------

@dataclass
class TurnRow:
    """One `wo_turns` row inside the window, joined to its work order."""

    wo_id: str
    project: str
    title: str
    status: str
    session_id: str
    kind: str
    seq: int
    started_at: float
    ended_at: float | None
    cost_usd: float | None
    cost_source: str | None
    usage: dict[str, Any] | None = None

    @property
    def seconds(self) -> float | None:
        return None if self.ended_at is None else self.ended_at - self.started_at

    @property
    def source(self) -> str | None:
        """Which reading wrote `cost_usd`, or None for a turn with no cost on record.

        NULL alongside a cost is a row written before the column existed and reads as
        `envelope` — the only source there was (project_store.py:1002-1008).
        """
        if self.cost_usd is None:
            return None
        return self.cost_source or COST_FROM_ENVELOPE


@dataclass
class OrderStats:
    """One order's in-window population: its turns, its compactions, its OS spend."""

    wo_id: str
    project: str
    title: str
    status: str
    session_id: str
    turns: list[TurnRow] = field(default_factory=list)
    compactions: list[TurnRow] = field(default_factory=list)
    truncated: bool = False
    os_cost_usd: float = 0.0
    validation_rounds: int = 0
    neo_questions: int = 0

    @property
    def live(self) -> bool:
        return self.status not in compaction_payoff.TERMINAL_STATUSES

    def cost(self, source: str) -> float:
        return sum(t.cost_usd or 0.0 for t in self.turns if t.source == source)


def turn_rows(db_path: Path, since: float, until: float, *,
              project: str = "") -> list[TurnRow]:
    """Every turn of every order whose `started_at` is in the window. Read-only.

    EVERY kind, including `compact`: the caller splits them, because the two
    denominators (per order, per worker turn) must never be mixed up by a filter that
    happened earlier.
    """
    conn = connect_ro(db_path)
    try:
        return [_row(r, project) for r in conn.execute(
            """SELECT t.wo_id, t.seq, t.kind, t.started_at, t.ended_at, t.cost_usd,
                      t.cost_source, t.usage_json, w.title, w.status, w.session_id
               FROM wo_turns t JOIN work_orders w ON w.id = t.wo_id
               WHERE t.started_at >= ? AND t.started_at < ?
               ORDER BY t.wo_id, t.seq""", (since, until))]
    finally:
        conn.close()


def _row(r: sqlite3.Row, project: str) -> TurnRow:
    return TurnRow(
        wo_id=r["wo_id"], project=project, title=r["title"] or "",
        status=r["status"] or "", session_id=r["session_id"] or "",
        kind=r["kind"], seq=int(r["seq"]), started_at=float(r["started_at"]),
        ended_at=None if r["ended_at"] is None else float(r["ended_at"]),
        cost_usd=None if r["cost_usd"] is None else float(r["cost_usd"]),
        cost_source=r["cost_source"],
        usage=_json(r["usage_json"]))


def _json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def window_facts(db_path: Path, since: float, until: float) -> dict[str, Any]:
    """What the window itself did to this project: how many orders, which were cut.

    Separate from `turn_rows` because it asks about rows the window EXCLUDED, and a
    loader that returned those would invite them into a metric.
    """
    conn = connect_ro(db_path)
    try:
        outside = {r["wo_id"] for r in conn.execute(
            "SELECT DISTINCT wo_id FROM wo_turns WHERE started_at < ? OR started_at >= ?",
            (since, until))}
        orders = int(conn.execute("SELECT COUNT(*) AS n FROM work_orders")
                     .fetchone()["n"])
        rounds: dict[str, int] = {r["wo_id"]: int(r["n"]) for r in conn.execute(
            """SELECT wo_id, COUNT(*) AS n FROM validation_rounds
               WHERE wo_id IS NOT NULL AND ts >= ? AND ts < ? GROUP BY wo_id""",
            (since, until))}
        return {"outside": outside, "orders": orders, "rounds": rounds}
    finally:
        conn.close()


def per_order(rows: Iterable[TurnRow]) -> dict[str, OrderStats]:
    """Group in-window turns by order. The PER-ORDER denominator."""
    orders: dict[str, OrderStats] = {}
    for row in rows:
        stats = orders.get(row.wo_id)
        if stats is None:
            stats = orders[row.wo_id] = OrderStats(
                wo_id=row.wo_id, project=row.project, title=row.title,
                status=row.status, session_id=row.session_id)
        if row.kind == COMPACT_TURN:
            stats.compactions.append(row)
        elif row.kind in WORKER_TURN_KINDS:
            stats.turns.append(row)
    return orders


def per_turn(rows: Iterable[TurnRow]) -> list[TurnRow]:
    """The worker turns, compactions dropped. The PER-TURN denominator."""
    return [r for r in rows if r.kind in WORKER_TURN_KINDS]


# ---------------------------------------------------------------------------------------
# The one place n / avg / p90 / max are computed
# ---------------------------------------------------------------------------------------

@dataclass
class Metric:
    """A population statistic and everything needed to read it honestly."""

    n: int = 0
    avg: float | None = None
    p90: float | None = None
    max: dict[str, Any] | None = None
    unit: str = COUNT
    provenance: str = WO_TURNS
    cost_basis: dict[str, int] | None = None
    excluded: dict[str, int] = field(default_factory=lambda: dict.fromkeys(
        EXCLUDED_KEYS, 0))

    def as_dict(self) -> dict[str, Any]:
        return {"n": self.n, "avg": self.avg, "p90": self.p90, "max": self.max,
                "unit": self.unit, "provenance": self.provenance,
                "cost_basis": self.cost_basis, "excluded": dict(self.excluded)}


def metric(values: Sequence[tuple[float, str]], *, percentile: float = 0.9,
           **detail: Any) -> Metric:
    """n / avg / p90 / max over (value, wo_id) pairs. Empty is `n: 0`, never zeros.

    p90 is NEAREST-RANK via `compaction_payoff.quantile` — one implementation of the
    percentile in the OS, and one that always returns an observed value, so `max` and
    `p90` can both name the order holding them.
    """
    m = Metric(**detail)
    if not values:
        return m
    numbers = [float(v) for v, _ in values]
    top = max(values, key=lambda pair: pair[0])
    m.n = len(numbers)
    m.avg = round(sum(numbers) / len(numbers), 6)
    m.p90 = round(quantile(numbers, percentile) or 0.0, 6)
    m.max = {"value": round(float(top[0]), 6), "wo_id": top[1]}
    return m


# ---------------------------------------------------------------------------------------
# What only a transcript can say
# ---------------------------------------------------------------------------------------

def boundary_counts(session_id: str, floor: int | None,
                    index: dict[str, list[Path]] | None = None) -> dict[str, int]:
    """The session's cold boundaries, split by CAUSE — all four of them.

    `floor` is `os.cold_prefix_floor` and has no default anywhere (see `usage`'s note on
    `TTL_BREAK_EVEN`): passing None is legal and means the TTL/prefix split is left OPEN,
    which is what `undecided` counts. Dropping those would make the line unreconcilable
    against the transcript, which is the whole point of reporting it.
    """
    index = usage.index_sessions() if index is None else index
    files = sorted(index.get(session_id) or [])
    calls, compactions = [], []
    for path in files:
        calls.extend(usage.calls_of(path))
        compactions.extend(usage.compaction_stamps(path))
    calls.sort(key=lambda c: c.ts)
    found = usage.classify_boundaries(calls, compactions=sorted(compactions),
                                      cold_prefix_floor=floor)
    counts = {"ttl": 0, "prefix": 0, "compacted": 0, "undecided": 0}
    for boundary in found:
        counts[{usage.BOUNDARY_TTL: "ttl", usage.BOUNDARY_PREFIX: "prefix",
                usage.BOUNDARY_COMPACTED: "compacted",
                usage.BOUNDARY_UNDECIDED: "undecided"}[boundary.cause]] += 1
    return counts


def subagent_share(session_id: str, floor: int, *,
                   index: dict[str, list[Path]] | None = None) -> float | None:
    """What share of the session's own bill its Task-tool subagents were.

    None, never 0.0, when there is no transcript left or nothing was spent: the question
    is unanswerable there, and answering it with a zero is a claim.
    """
    session = usage.read_session(session_id, floor, index=index)
    if not session.found:
        return None
    total = session.total.list_cost_usd
    if total <= 0:
        return None
    return round(session.subagents.list_cost_usd / total, 6)


def rewrite_share(order: OrderStats) -> float | None:
    """The re-write tax's share of one order's recorded spend, from the envelopes.

    Every turn after the first re-sends the whole conversation at the cache-WRITE rate,
    so the numerator is the cache-write tokens of the order's in-window turns after the
    first, priced at the rate the envelope's own 1h/5m split says they paid
    (`usage.priced`). The denominator is the order's recorded `envelope` spend. None when
    either side is unknown — no envelope, or nothing recorded to be a share OF.
    """
    turns = sorted(order.turns, key=lambda t: t.seq)[1:]
    spend = order.cost(COST_FROM_ENVELOPE)
    if not turns or spend <= 0:
        return None
    tax = 0.0
    seen = False
    for turn in turns:
        if not turn.usage:
            continue
        seen = True
        write = int(turn.usage.get("cache_write") or 0)
        model = _model_of(turn.usage)
        tax += usage.priced(model, cache_write=write,
                            cache_1h=int(turn.usage.get("cache_1h") or 0),
                            cache_5m=int(turn.usage.get("cache_5m") or 0)).list_cost_usd
    return round(tax / spend, 6) if seen else None


def _model_of(envelope: dict[str, Any]) -> str:
    """The turn's dominant model, or "" so `usage.price_for` uses its own default."""
    by_model = envelope.get("by_model") or []
    if isinstance(by_model, list) and by_model and isinstance(by_model[0], dict):
        return str(by_model[0].get("model") or "")
    return ""


# ---------------------------------------------------------------------------------------
# The OS's own spend
# ---------------------------------------------------------------------------------------

def os_by_kind(since: float, until: float,
               project: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Windowed `agent_calls`, by kind and dearest first, plus the unattributed line.

    Through `CentralStore.agent_call_totals` (which gained `since=`/`until=` for this)
    rather than a second query of my own: that method is where the OS's own spend is
    summed, and two implementations of "what did Jarvis spend" would drift.

    `observe_*` kinds appear with their real $0.00. A by-kind table that dropped its
    zero-cost kinds could not be checked against `SELECT kind, count(*) FROM
    agent_calls`, and reconciling is the point of the line.
    """
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        groups = central.agent_call_totals(project, since=since, until=until)
    finally:
        central.close()
    by_kind: dict[str, dict[str, Any]] = {}
    unattributed = {"calls": 0, "cost_usd": 0.0}
    for row in groups:
        kind = by_kind.setdefault(row["kind"], {"kind": row["kind"], "calls": 0,
                                                "cost_usd": 0.0})
        kind["calls"] += int(row["calls"] or 0)
        kind["cost_usd"] += float(row["cost_usd"] or 0.0)
        if not (row["wo_id"] or ""):
            unattributed["calls"] += int(row["calls"] or 0)
            unattributed["cost_usd"] += float(row["cost_usd"] or 0.0)
    for kind in by_kind.values():
        kind["cost_usd"] = round(kind["cost_usd"], 6)
    unattributed["cost_usd"] = round(unattributed["cost_usd"], 6)
    return (sorted(by_kind.values(), key=lambda k: -k["cost_usd"]), unattributed)


def _os_calls(home: Path, since: float, until: float,
              project: str | None) -> list[dict[str, Any]]:
    """Every attributable OS call in the window, with its `ts`. Read-only.

    `agent_call_totals` is grouped and carries no timestamp, and attribution to a TURN is
    by timestamp interval — `agent_calls` has no turn id (spec §9: the root cause is that
    the OS does not stamp its own calls with the turn they were made during). So this
    reads the rows themselves, and the payload discloses what could not be attributed
    rather than hiding it.
    """
    conn = connect_ro(home / "os.db")
    try:
        sql = ("SELECT wo_id, ts, cost_usd FROM agent_calls "
               "WHERE ts >= ? AND ts < ? AND wo_id IS NOT NULL AND wo_id != ''")
        args: list[Any] = [since, until]
        if project:
            sql += " AND project = ?"
            args.append(project)
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _attribute(calls: Sequence[dict[str, Any]], orders: dict[str, OrderStats]
               ) -> tuple[dict[tuple[str, int], float], int]:
    """OS spend per (wo_id, turn seq), and how many rows no turn contained.

    A row belongs to the turn whose `[started_at, ended_at)` holds its `ts`. A row
    between turns, or on an order with no in-window turn, is reported on the ORDER and
    counted as unattributed-to-a-turn — hence "attributed to the turn where possible".
    """
    by_turn: dict[tuple[str, int], float] = {}
    unattributed = 0
    for call in calls:
        order = orders.get(call["wo_id"] or "")
        cost = float(call["cost_usd"] or 0.0)
        if order is not None:
            order.os_cost_usd += cost
        turn = None if order is None else next(
            (t for t in order.turns
             if t.started_at <= float(call["ts"]) < (t.ended_at or math.inf)), None)
        if turn is None:
            unattributed += 1
            continue
        key = (turn.wo_id, turn.seq)
        by_turn[key] = by_turn.get(key, 0.0) + cost
    return by_turn, unattributed


def _neo_questions(since: float, until: float, project: str | None) -> dict[str, int]:
    """Neo questions per work order, from `neo.db`, read-only."""
    path = paths.neo_db_path()
    if not path.exists():
        return {}
    conn = connect_ro(path)
    try:
        sql = ("SELECT wo_id, COUNT(*) AS n FROM questions "
               "WHERE ts >= ? AND ts < ? AND wo_id != ''")
        args: list[Any] = [since, until]
        if project:
            sql += " AND project = ?"
            args.append(project)
        return {r["wo_id"]: int(r["n"])
                for r in conn.execute(sql + " GROUP BY wo_id", args)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------------------

def registered_paths(home: Path) -> dict[str, Path]:
    """The active projects, read out of `os.db` WITHOUT constructing a store."""
    conn = connect_ro(home / "os.db")
    try:
        return {r["name"]: Path(r["path"]) for r in conn.execute(
            "SELECT name, path FROM projects WHERE status = 'active'")}
    finally:
        conn.close()


def cost_config(project: str | None = None) -> CostConfig:
    """`cost.*` for the scope being reported, fleet-wide or per project.

    Field-level inheritance is already done by `catalog._parse_cost`, so this consults
    exactly one object and never two (the kn-6ca2bcd9 rule).
    """
    from jarvis.catalog import CatalogError
    from jarvis.ops import resolve_catalog

    resolved = resolve_catalog()
    if not project:
        return resolved.os.cost
    try:
        return resolved.project(project).cost
    except CatalogError:
        return resolved.os.cost


def report(*, project: str | None = None, since: float | str | None = None,
           until: float | str | None = None, now: float | None = None,
           home: Path | None = None) -> dict[str, Any]:
    """The whole `fleet` payload. One computation for the CLI, `--json` and the page."""
    from jarvis.bill import _cold_prefix_floor
    from jarvis.ops import COST_FLOOR_NOTE

    cfg = cost_config(project)
    window = window_of(since, until, cfg, now=now)
    start, end = window["since"], window["until"]
    home = paths.jarvis_home() if home is None else home
    floor = _cold_prefix_floor()
    index = usage.index_sessions()

    rows: list[TurnRow] = []
    outside: set[str] = set()
    total_orders = 0
    rounds: dict[str, int] = {}
    known = registered_paths(home)
    if project and project not in known:
        # Refused rather than reported as an empty week: a typo in a project name must
        # not come back as "nothing ran", which reads as a fact about the fleet.
        from jarvis.ops import OpsError

        raise OpsError(f"project {project!r} not registered (known: {sorted(known)})")
    for name, path in sorted(known.items()):
        if project and name != project:
            continue
        db_path = paths.project_db_path(path)
        if not db_path.exists():
            continue
        rows.extend(turn_rows(db_path, start, end, project=name))
        facts = window_facts(db_path, start, end)
        outside |= facts["outside"]
        total_orders += facts["orders"]
        rounds.update(facts["rounds"])

    orders = per_order(rows)
    # The cap is on orders WALKED, so it is applied before anything reads a transcript,
    # and it keeps the most recent: an old order's distribution is the one a reader is
    # least likely to be asking about.
    if len(orders) > cfg.max_orders:
        keep = sorted(orders.values(),
                      key=lambda o: max(t.started_at for t in
                                        (o.turns + o.compactions)), reverse=True
                      )[:cfg.max_orders]
        orders = {o.wo_id: o for o in keep}
    questions = _neo_questions(start, end, project)
    for wo_id, order in orders.items():
        order.truncated = wo_id in outside
        order.validation_rounds = rounds.get(wo_id, 0)
        order.neo_questions = questions.get(wo_id, 0)
    os_turn_cost, os_unattributed_to_turn = _attribute(
        _os_calls(home, start, end, project), orders)
    kinds, unattributed = os_by_kind(start, end, project)

    return {"fleet": {
        "version": PAYLOAD_VERSION,
        "window": window,
        "scope": project or "fleet",
        "orders": {
            "n": len(orders),
            "live": sum(1 for o in orders.values() if o.live),
            "truncated": sum(1 for o in orders.values() if o.truncated),
            "excluded_no_turns": max(0, total_orders - len(orders)),
        },
        "metrics": _metrics(orders, os_turn_cost, os_unattributed_to_turn,
                            cfg=cfg, floor=floor, index=index),
        "os_cost_by_kind": kinds,
        "os_unattributed": unattributed,
        "compaction_payoff": compaction_payoff.summarise(compaction_payoff.analyse(
            compaction_payoff.gather(home, since=start, until=end, project=project,
                                     index=index))),
        "floor": True,
        "floor_reason": COST_FLOOR_NOTE,
        "notes": list(NOTES),
    }}


def _metrics(orders: dict[str, OrderStats], os_turn_cost: dict[tuple[str, int], float],
             os_unattributed_to_turn: int, *, cfg: CostConfig, floor: int,
             index: dict[str, list[Path]]) -> dict[str, Any]:
    """Every metric, each over the population it is honestly about."""
    q = cfg.percentile
    turns = [t for o in orders.values() for t in o.turns]
    basis = {
        "envelope": sum(1 for t in turns if t.source == COST_FROM_ENVELOPE),
        "transcript": sum(1 for t in turns if t.source == COST_FROM_TRANSCRIPT),
        "unrecorded": sum(1 for t in turns if t.source is None),
    }
    running = sum(1 for t in turns if t.ended_at is None)
    no_usage = sum(1 for t in turns if not t.usage)

    def turn_money(source: str) -> list[tuple[float, str]]:
        return [(float(t.cost_usd or 0.0) + os_turn_cost.get((t.wo_id, t.seq), 0.0),
                 t.wo_id) for t in turns if t.source == source]

    def order_money(source: str) -> list[tuple[float, str]]:
        """Per order: its turns of this source, plus ALL the OS spend it caused.

        The OS half rides with the envelope figure because that is where a reader looks
        for "what did this order cost"; an order with no envelope turn still reports it,
        which is why the population is every order and not only the ones with a cost.
        """
        if source == COST_FROM_ENVELOPE:
            return [(round(o.cost(source) + o.os_cost_usd, 6), o.wo_id)
                    for o in orders.values()]
        return [(round(o.cost(source), 6), o.wo_id) for o in orders.values()
                if any(t.source == source for t in o.turns)]

    def tokens(cls: str) -> list[tuple[float, str]]:
        return [(float((t.usage or {}).get(cls) or 0), t.wo_id)
                for t in turns if t.usage]

    boundaries = {o.wo_id: boundary_counts(o.session_id, floor, index)
                  for o in orders.values() if o.session_id}
    shares = {o.wo_id: subagent_share(o.session_id, floor, index=index)
              for o in orders.values() if o.session_id}
    no_transcript = len(orders) - sum(1 for v in shares.values() if v is not None)

    def cause(name: str) -> list[tuple[float, str]]:
        return [(float(counts[name]), wo_id) for wo_id, counts in boundaries.items()]

    money_excluded = dict.fromkeys(EXCLUDED_KEYS, 0) | {
        "running": running, "unrecorded": basis["unrecorded"],
        "os_unattributed_to_turn": os_unattributed_to_turn}
    out = {
        "turns_per_order": metric(
            [(float(len(o.turns)), o.wo_id) for o in orders.values()],
            percentile=q, unit=COUNT, provenance=WO_TURNS),
        "cost_per_turn_usd": metric(
            turn_money(COST_FROM_ENVELOPE), percentile=q, unit=USD,
            provenance=ENVELOPE, cost_basis=dict(basis), excluded=money_excluded),
        "cost_per_turn_usd_transcript_floor": metric(
            turn_money(COST_FROM_TRANSCRIPT), percentile=q, unit=USD,
            provenance=TRANSCRIPT, cost_basis=dict(basis),
            excluded=dict(money_excluded)),
        "compactions_per_order": metric(
            [(float(len(o.compactions)), o.wo_id) for o in orders.values()],
            percentile=q, unit=COUNT, provenance=WO_TURNS),
        "seconds_per_turn": metric(
            [(t.seconds or 0.0, t.wo_id) for t in turns if t.seconds is not None],
            percentile=q, unit=SECONDS, provenance=WO_TURNS,
            excluded=dict.fromkeys(EXCLUDED_KEYS, 0) | {"running": running}),
        "cost_per_order_usd": metric(
            order_money(COST_FROM_ENVELOPE), percentile=q, unit=USD,
            provenance=ENVELOPE, cost_basis=dict(basis),
            excluded=dict(money_excluded)),
        "cost_per_order_usd_transcript_floor": metric(
            order_money(COST_FROM_TRANSCRIPT), percentile=q, unit=USD,
            provenance=TRANSCRIPT, cost_basis=dict(basis),
            excluded=dict(money_excluded)),
        "subagent_share": metric(
            [(v, wo_id) for wo_id, v in shares.items() if v is not None],
            percentile=q, unit=SHARE, provenance=TRANSCRIPT,
            excluded=dict.fromkeys(EXCLUDED_KEYS, 0) | {
                "no_transcript": no_transcript}),
        "rewrite_tax_share": metric(
            [(v, o.wo_id) for o in orders.values()
             if (v := rewrite_share(o)) is not None],
            percentile=q, unit=SHARE, provenance=ENVELOPE,
            excluded=dict.fromkeys(EXCLUDED_KEYS, 0) | {"unrecorded": no_usage}),
        "validation_rounds_per_order": metric(
            [(float(o.validation_rounds), o.wo_id) for o in orders.values()],
            percentile=q, unit=COUNT, provenance=RECORD),
        "neo_questions_per_order": metric(
            [(float(o.neo_questions), o.wo_id) for o in orders.values()],
            percentile=q, unit=COUNT, provenance=RECORD),
    }
    for cls in usage.TOKEN_CLASSES:
        out[f"tokens_per_turn.{cls}"] = metric(
            tokens(cls), percentile=q, unit=TOKENS, provenance=ENVELOPE,
            excluded=dict.fromkeys(EXCLUDED_KEYS, 0) | {"unrecorded": no_usage})
    # All FOUR causes `usage.classify_boundaries` can return, each its own count: TTL
    # expiry and a prefix miss cost the same and have completely different fixes, and
    # `undecided` is the honest answer where no floor could decide — never a silent drop.
    for name, key in (("ttl", "ttl_expiry_per_order"),
                      ("prefix", "prefix_miss_per_order"),
                      ("compacted", "compacted_boundaries_per_order"),
                      ("undecided", "undecided_boundaries_per_order")):
        out[key] = metric(cause(name), percentile=q, unit=COUNT, provenance=TRANSCRIPT,
                          excluded=dict.fromkeys(EXCLUDED_KEYS, 0) | {
                              "no_transcript": len(orders) - len(boundaries)})
    return {name: m.as_dict() for name, m in out.items()}
