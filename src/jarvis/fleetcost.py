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

import bisect
import json
import math
import re
import shlex
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

#: The windows a report can be asked for by NAME. §1 of
#: docs/superpowers/specs/2026-10-07-cost-window-selector.md.
WEEK = "week"                       #: the Claude usage week
SESSION = "5h"                      #: the 5-hour grid, anchored on the weekly reset
WINDOW_NAMES = (WEEK, SESSION)

#: Said in the payload rather than in a renderer, for `NOTES`' reason: nothing in the
#: codebase or in the usage data records Claude's real 5h session boundary, so a 5h
#: window here is a SLICE OF THE WEEK and not a claim about Anthropic's accounting (§2).
SESSION_ANCHOR_NOTE = (
    "a 5h window is a 5h slice of the usage week, anchored on the weekly reset: no "
    "table and no transcript records Claude's own session boundary, so stepping back is "
    "continuous in absolute time and an older window may not align with that week's "
    "own reset"
)


def usage_week(now: float, cfg: CostConfig, offset: int = 0) -> tuple[float, float]:
    """The Claude usage week containing `now`, as (since, until).

    VERIFIED: the week resets Monday 21:00 America/Los_Angeles, so Mon 2026-09-28 21:00
    PDT = 2026-09-29 04:00 UTC and the week of that date runs to 2026-10-06 04:00 UTC.
    Day, hour and zone all come from `CostConfig` and never from a literal here: a DST
    shift and an Anthropic policy change both move them.

    The arithmetic is done on the LOCAL wall clock and converted once, so a week that
    spans a DST change is still seven local days and still starts at the configured
    hour — absolute arithmetic on an aware datetime would slide the reset by an hour.

    `offset` steps back whole LOCAL weeks for the same reason, so a window spanning a
    DST change is 167 or 169 absolute hours and still starts at the configured hour
    (§4 of the window-selector spec).
    """
    zone = ZoneInfo(cfg.week_reset_zone)
    local = datetime.fromtimestamp(now, zone).replace(tzinfo=None)
    start = local.replace(hour=cfg.week_reset_hour, minute=0, second=0, microsecond=0)
    start -= timedelta(days=(start.weekday() - cfg.week_reset_weekday) % 7)
    if start > local:
        start -= timedelta(days=7)
    start += timedelta(days=7 * offset)
    return (start.replace(tzinfo=zone).timestamp(),
            (start + timedelta(days=7)).replace(tzinfo=zone).timestamp())


def session_window(now: float, cfg: CostConfig,
                   offset: int = 0) -> tuple[float, float]:
    """One 5h slice of the usage week — see `SESSION_ANCHOR_NOTE` for the anchor."""
    week_start, _ = usage_week(now, cfg)
    length = cfg.session_window_hours * 3600
    k = math.floor((now - week_start) / length)
    since = week_start + (k + offset) * length
    return (since, since + length)


def resolve_zone(tz: str | None, cfg: CostConfig) -> str:
    """The IANA zone to DISPLAY a window in: `tz` when given and valid, else the catalog.

    Its own function because the UI needs the zone BEFORE it can parse the custom form's
    naive datetimes, so it cannot wait for `resolve_window` to return (§11 of
    docs/superpowers/specs/2026-10-07-cost-window-selector.md). DISPLAY ONLY: the
    boundaries stay anchored in `cfg.week_reset_zone` whatever this returns.
    """
    from jarvis.ops import OpsError

    if not tz:
        return cfg.week_reset_zone
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        # A refusal with a sentence, never a silent fallback to the default (§11).
        raise OpsError(f"tz must be an IANA time zone name — {tz!r} is not a zone this "
                       f"report knows") from None
    return tz


def resolve_window(*, window: str | None = None, offset: int = 0,
                   since: float | str | None = None,
                   until: float | str | None = None, tz: str | None = None,
                   cfg: CostConfig, now: float | None = None) -> dict[str, Any]:
    """THE window resolver — one answer for the CLI, `--json` and the page.

    Exactly one function interprets a window NAME (Neo q1420), and its eight keys are
    the contract every surface reads. Resolved ONCE per surface and passed to both
    payload builders: this is a function of `now`, so two calls a few milliseconds apart
    can land either side of a boundary and the page would show two windows again.

    A BAD PARAMETER IS A REFUSAL, never a silent fallback to the default week: a page
    that ignored `?window=5x` would report the week while the reader believes they
    picked 5h, which is the one failure mode a selector must not have (§3).
    """
    from jarvis.ops import OpsError

    zone = resolve_zone(tz, cfg)
    named = window is not None or offset
    custom = since is not None or until is not None
    if named and custom:
        raise OpsError("--since/--until and --window/--offset are two ways to name the "
                       "same thing — pass one or the other, not both")
    if window is not None and window not in WINDOW_NAMES:
        raise OpsError(f"window must be one of {', '.join(WINDOW_NAMES)} — {window!r} "
                       f"is not a window this report knows")
    if offset > 0:
        raise OpsError(f"offset must be 0 or negative — {offset} names a window that "
                       f"has not happened yet")
    if custom:
        start = _as_ts(since) if since is not None else 0.0
        end = _as_ts(until) if until is not None else _now(now)
        if start >= end:
            raise OpsError(f"since must be before until — {_stamp(start)} is at or "
                           f"after {_stamp(end)}, which is an empty window")
        return _window(start, end, zone, source="flags", window=None, offset=None)
    if window == SESSION:
        start, end = session_window(_now(now), cfg, offset)
        source = "session-window"
    else:
        start, end = usage_week(_now(now), cfg, offset)
        # The literal the existing template branch and CLI line already read, kept for
        # the default so neither changes meaning (§1).
        source = "usage-week" if not offset else "week-offset"
    return _window(start, end, zone, source=source, window=window or WEEK, offset=offset)


def window_of(since: float | str | None, until: float | str | None, cfg: CostConfig,
              *, now: float | None = None, tz: str | None = None) -> dict[str, Any]:
    """The window to report over: the flags if given, else the current usage week.

    A shim over `resolve_window`, kept because this signature is public: `report` calls
    it and tests use it. One implementation, no broken caller.
    """
    return resolve_window(
        since=since, until=until, tz=tz,
        window=None if (since is not None or until is not None) else WEEK,
        cfg=cfg, now=now)


def _window(since: float, until: float, zone: str, *, source: str,
            window: str | None, offset: int | None) -> dict[str, Any]:
    return {"since": since, "until": until, "label": _label(since, until),
            "local_label": _local_label(since, until, zone), "source": source,
            "window": window, "offset": offset, "zone": zone}


def _now(now: float | None) -> float:
    from jarvis import db

    return db.now() if now is None else now


def _as_ts(value: float | str) -> float:
    return parse_when(value) if isinstance(value, str) else float(value)


def _label(since: float, until: float) -> str:
    fmt = "%Y-%m-%d %H:%M"
    return (f"{datetime.fromtimestamp(since, timezone.utc).strftime(fmt)} to "
            f"{datetime.fromtimestamp(until, timezone.utc).strftime(fmt)} UTC")


def _stamp(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _local_label(since: float, until: float, zone: str) -> str:
    """The same span in the DISPLAY zone — by default the clock the reset is specified in.

    BOTH abbreviations when the ends differ: a single `%Z` across a DST transition is a
    label that is wrong at one end (§4). Takes the zone NAME rather than the config
    because the reader can pick a zone to read in (§11).
    """
    tzinfo = ZoneInfo(zone)
    start = datetime.fromtimestamp(since, tzinfo)
    end = datetime.fromtimestamp(until, tzinfo)
    fmt = "%Y-%m-%d %H:%M"
    if start.tzname() != end.tzname():
        return (f"{start.strftime(fmt)} {start.tzname()} to "
                f"{end.strftime(fmt)} {end.tzname()}")
    return f"{start.strftime(fmt)} to {end.strftime(fmt)} {end.tzname()}"


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
# What a TOOL cost — §10
#
# The fleet's largest avoidable token line is tool output, and until this section no
# surface in the OS could see it: the ledger above is keyed by API CALL, and a tool
# result is not a call — it is a payload that rides along inside every SUBSEQUENT call's
# prefix. That quantity is `carried cost`, and it is why counting result bytes
# understates the problem: a 20k-token `sed -n` dump read 30 more times costs 600k
# token-reads, not 20k.
#
# The WALK is `usage.tool_results` (a leaf: transcripts and prices, no store, no
# catalog). Everything here is the aggregation: the command-shape classifier, the
# carried-cost arithmetic and the fleet denominators.
#
# Spec §10 of docs/superpowers/specs/2026-10-06-fleet-cost-per-tool.md.
# ---------------------------------------------------------------------------------------

#: The Bash command SHAPES, in the order `classify_command` tests them. The axis is what
#: PRODUCED the bytes, never what clipped them, so every producer is tested before
#: `stream_head_tail` — which is the residual "output already clipped" bucket and the
#: ~167 tok/call population the motivating measurement found was already fine.
SHAPE_PYTEST = "pytest"
SHAPE_JARVIS = "jarvis"
SHAPE_GIT_READ = "git_read"
SHAPE_SED_RANGE = "sed_range"
SHAPE_CAT_FILE = "cat_file"
SHAPE_SEARCH = "search"
SHAPE_STREAM = "stream_head_tail"
SHAPE_OTHER = "other"
SHAPES = (SHAPE_PYTEST, SHAPE_JARVIS, SHAPE_GIT_READ, SHAPE_SED_RANGE, SHAPE_CAT_FILE,
          SHAPE_SEARCH, SHAPE_STREAM, SHAPE_OTHER)

#: Shell operators that CUT a command into segments, as standalone tokens.
_OPERATORS = frozenset({"|", "||", "&&", ";", "|&", "&"})

#: Stripped off the front of a segment before its head word is read. `uv` and `poetry`
#: only with a following `run`: `uv sync` is not a wrapper around anything.
_WRAPPERS = frozenset({"sudo", "time", "env", "nohup", "xargs"})
_RUNNERS = frozenset({"uv", "poetry"})

#: `python -m pytest`, where the head word is the interpreter and `pytest` is a token.
_INTERPRETERS = frozenset({"python", "python3", "uv", "poetry"})

_GIT_READ_VERBS = frozenset({"diff", "show", "log", "blame"})
_CAT_HEADS = frozenset({"cat", "bat"})
_SEARCH_HEADS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "find"})
_CLIP_HEADS = frozenset({"head", "tail"})

#: A `sed -n` ADDRESS: `50p`, `1,50p`, `1,$p`. Quotes are optional because the
#: whitespace fallback (an unbalanced quote) does not strip them.
_SED_ADDRESS = re.compile(r"""^['"]?\d+(,(\d+|\$))?p?['"]?$""")


def classify_command(command: str) -> str:
    """One Bash command's SHAPE: what produced its bytes. Pure, total, deterministic.

    Never shells out and never touches the filesystem — no `os.path.exists`, no glob
    expansion — because the path may be in a worktree that is gone (the reason
    `usage.index_sessions` exists) and a classifier whose answer depends on the disk is
    not reproducible. Never raises: an unbalanced quote falls back to whitespace
    splitting, and anything unrecognised is `other`, the total last rule.

    §10.5's ordered rule list, first match wins.
    """
    if not isinstance(command, str):
        return SHAPE_OTHER
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        # An unbalanced quote. The shape is still worth having, so the cheaper
        # tokenizing is used rather than the call being dropped.
        tokens = command.split()
    segments = [s for s in _segments(tokens) if s]
    if not segments:
        return SHAPE_OTHER
    heads = [_head_word(s) for s in segments]

    # 1-2: scanned over EVERY segment, so neither is ever hidden behind a pipe. A
    # `jarvis` call is a thing the user asks about by name, and a test run is the one
    # population already known to be cheap per call.
    for segment, head in zip(segments, heads):
        if head == SHAPE_PYTEST or (head in _INTERPRETERS and SHAPE_PYTEST in segment):
            return SHAPE_PYTEST
    if SHAPE_JARVIS in heads:
        return SHAPE_JARVIS

    first, head = segments[0], heads[0]
    rest = first[first.index(head) + 1:] if head in first else []
    if head == "git" and rest and rest[0] in _GIT_READ_VERBS:
        return SHAPE_GIT_READ
    if head == "sed" and "-n" in first and any(_SED_ADDRESS.match(t) for t in rest):
        return SHAPE_SED_RANGE
    if head in _CAT_HEADS:
        return SHAPE_CAT_FILE
    if head in _SEARCH_HEADS:
        return SHAPE_SEARCH
    # 7: the LAST segment, which covers both `cmd | head` and a bare `head -50 file`.
    if heads[-1] in _CLIP_HEADS:
        return SHAPE_STREAM
    return SHAPE_OTHER


def _segments(tokens: Sequence[str]) -> list[list[str]]:
    """Cut a token list on the shell operators found as standalone tokens."""
    out: list[list[str]] = [[]]
    for token in tokens:
        if token in _OPERATORS:
            out.append([])
        else:
            out[-1].append(token)
    return out


#: Said in the payload rather than in a renderer, the same rule `NOTES` follows. The six
#: KNOWN INACCURACIES of §10.4 plus the one thing a reader would otherwise add up wrong.
TOOL_NOTES = (
    "a tool result's size is DERIVED and never reported by the API: context-delta is "
    "exact to the token for a single result between two clean calls, a parallel batch "
    "is a char-proportional split of an exact total, and chars is an estimate whose "
    "divisor is cost.chars_per_token — the counts are in token_basis",
    "a call's output count is a GENERATION count, not an input count: the assistant "
    "message is re-sent as input on the next call and the two tokenizations need not "
    "agree, so any difference lands on the tool result under context-delta",
    "carried cost assumes the result survives in the prefix until the next compaction, "
    "so it is an OVER-estimate wherever context-window truncation ended the ride "
    "earlier — compact_boundary is the only replacement event a transcript records",
    "the pricing of a result's share of a call is exact and its size is not: one call "
    "pays one rate for its whole prefix, so result_tokens x rate x per_token blends "
    "nothing",
    "a result that was never carried still cost its own round trip: the last tool call "
    "of a session has carried_calls 0 and carried_usd 0.0, and its result_tokens are "
    "still reported",
    "the chars estimator's divisor (cost.chars_per_token) is UNCALIBRATED against the "
    "fleet: the default is 4.0, it is overridable per project, and the token_basis "
    "counts say how many calls depend on it",
    "the carried dollars are a SHARE of money already counted in cost_per_order_usd, "
    "not an addition to it: the two are different decompositions of overlapping tokens "
    "and summing them would double-count",
)


def _head_word(segment: Sequence[str]) -> str:
    """A segment's first token, with `VAR=value` assignments and wrappers stripped."""
    i = 0
    while i < len(segment):
        token = segment[i]
        if token in _WRAPPERS or ("=" in token and not token.startswith("-")
                                  and token.split("=", 1)[0].isidentifier()):
            i += 1
            continue
        if token in _RUNNERS and i + 1 < len(segment) and segment[i + 1] == "run":
            i += 2
            continue
        return token
    return ""


#: The one tool whose NAME says nothing about what it read. Every other tool is keyed by
#: `tool_use.name` verbatim — no normalization, no prefix stripping, because two
#: spellings of one tool is how a reader loses a row. The render elides for display only.
BASH_TOOL = "Bash"


@dataclass
class _Chain:
    """One conversation's calls, with the running price of carrying ONE token in them.

    A result's carried cost is its size times what every LATER call in the SAME CHAIN
    paid to carry a token — so the per-token price is accumulated once per chain and
    read off by timestamp, rather than re-walked per result. The main chain is
    `session_calls` (every segment of the session: a result keeps riding across a
    segment boundary because the conversation did); a subagent file is its own chain,
    and a lead call is never in a subagent's denominator nor the reverse.
    """

    stamps: list[float] = field(default_factory=list)
    #: Cumulative (all, read, ttl-rewrite, prefix-rewrite) dollars per carried token.
    #: `len(stamps) + 1` entries, so a half-open range of calls is one subtraction.
    cumulative: list[tuple[float, float, float, float]] = field(
        default_factory=lambda: [(0.0, 0.0, 0.0, 0.0)])
    compactions: list[float] = field(default_factory=list)
    #: Exclusive right edge of the report's window. Calls at or after it are not in the
    #: window, so a result never rides on them. `_chain` always passes the real value;
    #: `inf` is the identity for this bound (2026-10-08-cost-tool-section-window-clip §3).
    until: float = math.inf

    def carried(self, ts: float) -> tuple[int, tuple[float, float, float, float]]:
        """(later calls, dollars per carried token) for a result that landed at `ts`.

        The ride ENDS at whichever comes first: the first compaction after `ts` — a
        compaction replaces the conversation, so the result stops being carried there —
        or the window's `until`, since a call outside the window is not in the population
        the report is about (2026-10-08-cost-tool-section-window-clip §2).
        """
        start = bisect.bisect_right(self.stamps, ts)
        stop = next((c for c in self.compactions if c > ts), None)
        end = (len(self.stamps) if stop is None
               else bisect.bisect_left(self.stamps, stop))
        # Half-open: a call exactly at `until` is out.
        end = min(end, bisect.bisect_left(self.stamps, self.until))
        if end <= start:
            return (0, (0.0, 0.0, 0.0, 0.0))
        before, after = self.cumulative[start], self.cumulative[end]
        return (end - start,
                (after[0] - before[0], after[1] - before[1],
                 after[2] - before[2], after[3] - before[3]))


def _chain(calls: Sequence[usage.Call], compactions: Sequence[float],
           floor: int | None, *, until: float) -> _Chain:
    """Price carrying one token in every call of a chain, split by what it was billed as.

    `compaction_payoff.prefix_rate` is REUSED, not reimplemented: it returns the multiple
    of base input price one call paid *for the prefix it carried*, which is precisely the
    rate a result inside that prefix was billed at. The CAUSE of a write comes from
    `usage.classify_boundaries`, joined to the call by `Boundary.ts` — TTL expiry and a
    prefix miss cost the same and have completely different fixes.

    The chain is BUILT over every call in the file, in or out of the window: `prefix_rate`
    and `classify_boundaries` read each call against its PREDECESSOR, so dropping
    out-of-window calls here would mis-price the first in-window call and lose the
    left-edge boundary. `until` clips at READ time, inside `_Chain.carried`.
    """
    ordered = sorted(calls, key=lambda c: c.ts)
    stamps = sorted(compactions)
    causes = {b.ts: b.cause for b in usage.classify_boundaries(
        ordered, compactions=stamps, cold_prefix_floor=floor)}
    chain = _Chain(compactions=stamps, until=until)
    previous: usage.Call | None = None
    for i, call in enumerate(ordered):
        kind, rate = compaction_payoff.prefix_rate(call, previous, i == 0)
        unit = compaction_payoff.per_token(call.model) * rate
        read = unit if kind == compaction_payoff.RATE_READ else 0.0
        # Everything that is not a cache READ was re-written (or paid full input price,
        # which is the same thing for a reader asking what the re-write tax cost). With
        # no floor the boundary is UNDECIDED and lands in the prefix bucket, never TTL.
        ttl = unit if (not read and causes.get(call.ts) == usage.BOUNDARY_TTL) else 0.0
        running = chain.cumulative[-1]
        chain.cumulative.append((running[0] + unit, running[1] + read,
                                 running[2] + ttl,
                                 running[3] + unit - read - ttl))
        chain.stamps.append(call.ts)
        previous = call
    return chain


@dataclass
class ToolCost:
    """What one tool (or one Bash shape, or one caller of either) cost in a window.

    ONE shape at every level of `fleet.tools`, and `by_caller` is a PARTITION of the
    level above it rather than an addend (kn-7a2180ba): `main.calls + subagent.calls ==
    calls`, and the same for every additive field.
    """

    calls: int = 0
    errors: int = 0
    result_tokens: int = 0
    carried_calls: int = 0
    carried_tokens: int = 0
    carried_usd: float = 0.0
    carried_read_usd: float = 0.0
    carried_rewrite_ttl_usd: float = 0.0
    carried_rewrite_prefix_usd: float = 0.0
    basis_context_delta: int = 0
    basis_chars: int = 0
    #: Every result's size, for the p90 and the max. Nearest-rank, so both are figures
    #: some call actually came back with.
    sizes: list[int] = field(default_factory=list)
    by_caller: dict[str, ToolCost] = field(default_factory=dict)
    shapes: dict[str, ToolCost] = field(default_factory=dict)

    def add(self, result: usage.ToolResult, later: int,
            money: tuple[float, float, float, float]) -> None:
        self.calls += 1
        self.errors += 1 if result.is_error else 0
        self.result_tokens += result.tokens
        self.sizes.append(result.tokens)
        self.carried_calls += later
        self.carried_tokens += result.tokens * later
        self.carried_usd += result.tokens * money[0]
        self.carried_read_usd += result.tokens * money[1]
        self.carried_rewrite_ttl_usd += result.tokens * money[2]
        self.carried_rewrite_prefix_usd += result.tokens * money[3]
        if result.token_basis == usage.BASIS_CONTEXT_DELTA:
            self.basis_context_delta += 1
        else:
            self.basis_chars += 1

    def as_dict(self, *, percentile: float, totals: ToolCost, ttl_known: bool,
                callers: bool = True, shapes: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "calls": self.calls,
            "errors": self.errors,
            "result_tokens": self.result_tokens,
            "result_tokens_avg": round(self.result_tokens / self.calls, 6
                                       ) if self.calls else 0.0,
            "result_tokens_p90": int(quantile(
                [float(s) for s in self.sizes], percentile) or 0),
            "result_tokens_max": max(self.sizes, default=0),
            "carried_calls": self.carried_calls,
            "carried_tokens": self.carried_tokens,
            "carried_usd": round(self.carried_usd, 6),
            "carried_read_usd": round(self.carried_read_usd, 6),
            # Never 0.00 when no floor could decide: a renderer would print that as "no
            # TTL expiry", which is a claim the configuration did not support.
            "carried_rewrite_ttl_usd": (round(self.carried_rewrite_ttl_usd, 6)
                                        if ttl_known else None),
            "carried_rewrite_prefix_usd": round(self.carried_rewrite_prefix_usd, 6),
            "token_basis": {"context_delta": self.basis_context_delta,
                            "chars": self.basis_chars},
            "share_of_result_tokens": _share(self.result_tokens, totals.result_tokens),
            "share_of_carried_usd": _share(self.carried_usd, totals.carried_usd),
        }
        if callers:
            out["by_caller"] = {
                name: self.by_caller.get(name, ToolCost()).as_dict(
                    percentile=percentile, totals=totals, ttl_known=ttl_known,
                    callers=False, shapes=False)
                for name in (usage.CALLER_MAIN, usage.CALLER_SUBAGENT)}
        if shapes and self.shapes:
            out["shapes"] = {
                name: cost.as_dict(percentile=percentile, totals=totals,
                                   ttl_known=ttl_known, shapes=False)
                for name, cost in sorted(self.shapes.items())}
        return out


def _share(part: float, whole: float) -> float:
    return round(part / whole, 6) if whole else 0.0


def tool_costs(orders: Iterable[OrderStats], *, cfg: CostConfig, floor: int | None,
               since: float, until: float,
               index: dict[str, list[Path]] | None = None,
               orders_capped: int = 0) -> dict[str, Any]:
    """`fleet.tools`: what every tool cost in the window, and what it CARRIED.

    The window is `[since, until)` and it bounds BOTH ends of the question: a tool result
    counts only if its own `ts` falls inside it, and the carried ride is clipped at
    `until` as well as at the next compaction — so every figure here describes the same
    population as the rest of the page (2026-10-08-cost-tool-section-window-clip.md).
    Both are REQUIRED: an optional window defaulting to the whole chain is the silent
    default that produced issue #990.

    Bounded by `cost.max_orders` one level up — the orders `fleetcost` already selected
    for the window are the sessions walked, and `excluded` publishes what the cap did.
    No cache in this stage: `--fleet` is an on-demand report, not a 15s pulse (§10.9).

    Read-only and deterministic: two passes per transcript, no store, no model call.
    """
    index = usage.index_sessions() if index is None else index
    totals = ToolCost()
    by_tool: dict[str, ToolCost] = {}
    unmatched = no_transcript = walked = outside_window = 0
    for order in orders:
        files = sorted(index.get(order.session_id) or []) if order.session_id else []
        if not files:
            # Counted, never dropped: a share needs a denominator a reader can check.
            no_transcript += 1
            continue
        main = _chain(usage.session_calls(order.session_id, index=index),
                      [c for path in files for c in usage.compaction_stamps(path)],
                      floor, until=until)
        for path in files:
            seen, late = _walk_tools(path, main, cfg, totals, by_tool,
                                     since=since, until=until)
            unmatched += seen
            outside_window += late
            walked += 1
            sub_dir = path.with_suffix("") / "subagents"
            if not sub_dir.is_dir():
                continue
            for sub in sorted(sub_dir.glob("*.jsonl")):
                # A subagent transcript is where the big dumps live: clipping only the
                # main chain would leave most of the over-count in place (§5).
                seen, late = _walk_tools(
                    sub, _chain(usage.calls_of(sub), usage.compaction_stamps(sub),
                                floor, until=until),
                    cfg, totals, by_tool, since=since, until=until)
                unmatched += seen
                outside_window += late
                walked += 1
    ttl_known = floor is not None
    detail = {"percentile": cfg.percentile, "totals": totals, "ttl_known": ttl_known}
    return {
        "version": PAYLOAD_VERSION,
        "totals": totals.as_dict(**detail),
        "by_tool": {name: cost.as_dict(**detail)
                    for name, cost in sorted(by_tool.items())},
        "excluded": {"unmatched_calls": unmatched, "no_transcript": no_transcript,
                     "orders_capped": orders_capped, "sessions_walked": walked,
                     "outside_window": outside_window},
        "row_limit": cfg.tool_rows,
        "notes": list(TOOL_NOTES),
    }


def tool_table(tools: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The tool table's rows, in §10.7's total order: carried $, tokens, then name.

    HERE rather than in either renderer, for the reason the partial already states: a
    figure — or an order — computed in a renderer is one the other renderer disagrees
    with. `jarvis cost --fleet` and the dashboard both call this.

    Bash is one row PER SHAPE and never also a Bash total line: the total is recoverable
    from the shapes, and two rows that sum to a third invite the reader to add the wrong
    pair. Sorting on carried cost rather than calls is the whole point of §10.4 —
    `pytest` at ~167 tok/call must not outrank a `sed -n` dump for being frequent. The
    order is TOTAL, so the table is reproducible run to run.
    """
    rows: list[tuple[str, dict[str, Any]]] = []
    for name, cost in (tools.get("by_tool") or {}).items():
        if cost.get("shapes"):
            rows.extend((f"{name} · {shape}", shape_cost)
                        for shape, shape_cost in cost["shapes"].items())
        else:
            rows.append((name, cost))
    rows.sort(key=lambda row: (-row[1]["carried_usd"], -row[1]["result_tokens"], row[0]))
    return rows


def _walk_tools(path: Path, chain: _Chain, cfg: CostConfig, totals: ToolCost,
                by_tool: dict[str, ToolCost], *, since: float,
                until: float) -> tuple[int, int]:
    """Fold one transcript's tool calls in. Returns (unmatched, outside the window).

    An UNMATCHED call — the turn was killed between the `tool_use` and its result —
    contributes no tokens and no carried cost and is never zero-filled: a call that
    produced nothing must not pull a tool's average down as though it had. That check
    runs FIRST: an unmatched call has no `ts` to place, and its own counter is already
    its disclosure (2026-10-08-cost-tool-section-window-clip.md §1).

    A matched result counts only if `result.ts` is in `[since, until)` — the stamp of when
    the result LANDED, since that is when it started riding in later prefixes. A result
    whose stamp could not be parsed (`0.0`) is outside every real window by the same
    arithmetic and needs no second rule.
    """
    unmatched = outside_window = 0
    for result in usage.tool_results(path, chars_per_token=cfg.chars_per_token):
        if not result.matched:
            unmatched += 1
            continue
        if not since <= result.ts < until:
            outside_window += 1
            continue
        later, money = chain.carried(result.ts)
        tool = by_tool.setdefault(result.name, ToolCost())
        buckets = [totals, tool]
        if result.name == BASH_TOOL:
            shape = classify_command(str(result.input.get("command") or ""))
            buckets.append(tool.shapes.setdefault(shape, ToolCost()))
        for bucket in buckets:
            bucket.add(result, later, money)
            bucket.by_caller.setdefault(result.caller, ToolCost()).add(
                result, later, money)
    return (unmatched, outside_window)


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
           until: float | str | None = None, window: str | None = None,
           offset: int = 0, tz: str | None = None,
           resolved: dict[str, Any] | None = None,
           now: float | None = None,
           home: Path | None = None) -> dict[str, Any]:
    """The whole `fleet` payload. One computation for the CLI, `--json` and the page.

    `resolved` is a window `resolve_window` already returned — the page resolves ONCE
    and hands the same dict to both payload builders (§6). It wins, and is refused
    beside any raw parameter so there is never a question of which one was used.
    """
    from jarvis.bill import _cold_prefix_floor
    from jarvis.ops import COST_FLOOR_NOTE, OpsError

    cfg = cost_config(project)
    if resolved is not None:
        if since is not None or until is not None or window is not None or offset:
            raise OpsError("a resolved window and --since/--until/--window/--offset are "
                           "two ways to name the same thing — pass one or the other, "
                           "not both")
    picked = resolved if resolved is not None else resolve_window(
        window=window, offset=offset, since=since, until=until, tz=tz, cfg=cfg, now=now)
    start, end = picked["since"], picked["until"]
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
    capped = max(0, len(orders) - cfg.max_orders)
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
        "window": picked,
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
        # ADDITIVE, and `version` above stays 1: §10.6's rule, the one `cost_report`
        # already applies. The subtree carries its own version so a later re-shaping of
        # it is detectable without bumping the parent.
        "tools": tool_costs(orders.values(), cfg=cfg, floor=floor,
                            since=start, until=end, index=index,
                            orders_capped=capped),
        "compaction_payoff": compaction_payoff.summarise(compaction_payoff.analyse(
            compaction_payoff.gather(home, since=start, until=end, project=project,
                                     index=index))),
        "floor": True,
        "floor_reason": COST_FLOOR_NOTE,
        # The 5h anchor is said only where it applies: a caveat about a grid nobody
        # asked for is one the reader learns to ignore (§2).
        "notes": list(NOTES) + ([SESSION_ANCHOR_NOTE]
                                if picked.get("window") == SESSION else []),
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
