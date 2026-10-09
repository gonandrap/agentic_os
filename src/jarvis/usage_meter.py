"""The account's usage meter: one minute's reading, and the arithmetic over the series.

Spec docs/superpowers/specs/2026-10-08-usage-meter-samples-and-outside-spend.md §§1-5.
The only place in the OS that leaves the machine for a non-`gh`, non-Telegram reason. It
owns one table, one parser and the reconciliation arithmetic over its own time series:
`fleetcost` is about per-ORDER distribution and must not grow an account-level network
dependency, and `daemon.py` holds cadence, not parsing.

Adapters tier, below `ops`. `fleetcost` is imported INSIDE the functions that need it —
it imports this module's callers, so a module-level import is a cycle.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from . import claude_cli, db, nav_volume, paths, usage

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
BETA = "oauth-2025-04-20"            #: the `anthropic-beta` header the endpoint requires
FIRST_CLASS = ("five_hour", "seven_day")   #: the two windows with their own columns

#: Same shape as `notify.sink_telegram`, so the OS has one HTTP idiom (§1).
TIMEOUT = 10

#: The sampler's own period, a UNIT and not a setting: `daemon.USAGE_SAMPLE_EVERY_TICKS`
#: is one minute at the default poll interval, and coverage is measured against it.
SAMPLE_SECONDS = 60.0

#: Two missed minutes end a calibration span (§5). Not a threshold anyone tunes: it is
#: "the series stopped being continuous".
MAX_SPAN_GAP_SECONDS = 120.0

#: A pct fall this large with `resets_at` UNCHANGED is a reset the endpoint reported late
#: (§4 rule 2), not a measurement.
DROP_TOLERANCE = 1.0

NOTICE_STREAK_KEY = "usage_meter_notice_streak"


class MeterError(Exception):
    """Network, auth, or a payload that is not a meter. NEVER carries the body or the
    token — only the SHAPE of the failure (§1)."""

    def __init__(self, reason: str, *, http_status: int | None = None,
                 latency_ms: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status
        self.latency_ms = latency_ms


@dataclass
class Sample:
    ts: float
    ok: bool = True
    reason: str = ""
    http_status: int | None = None
    latency_ms: int | None = None
    five_hour_pct: float | None = None
    five_hour_resets_at: float | None = None
    seven_day_pct: float | None = None
    seven_day_resets_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def pct(self, key: str) -> float | None:
        return getattr(self, f"{key}_pct")

    def resets_at(self, key: str) -> float | None:
        return getattr(self, f"{key}_resets_at")


# -- 1. the sampler ---------------------------------------------------------------------


def token() -> str:
    """`claudeAiOauth.accessToken`, read FRESH — a cached token is a 401 waiting (§1)."""
    path = claude_cli.credentials_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise MeterError("no credentials file to read the usage endpoint with") from None
    except (OSError, ValueError):
        raise MeterError("the credentials file is unreadable") from None
    oauth = raw.get("claudeAiOauth") if isinstance(raw, dict) else None
    value = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(value, str) or not value:
        raise MeterError("credentials file has no claudeAiOauth.accessToken")
    return value


def _epoch(value: Any) -> float | None:
    """`resets_at` as epoch seconds. The payload sends an ISO string; a number passes."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _utilization(window: Any) -> float | None:
    if not isinstance(window, dict):
        return None
    value = window.get("utilization")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse(payload: dict, *, ts: float) -> Sample:
    """One reading, or `MeterError` naming the shape of the failure.

    A key going from null to an object — a new codename, Opus usage starting — is NORMAL
    and lands in `extra` (§2). `limits` is dropped: the same two numbers in a second
    shape, read by nothing.
    """
    if not isinstance(payload, dict):
        raise MeterError("the usage endpoint returned a body that is not a JSON object")
    sample = Sample(ts=ts, ok=True)
    for key in FIRST_CLASS:
        value = _utilization(payload.get(key))
        if value is None:
            raise MeterError(f"{key}.utilization is missing or not a number")
        setattr(sample, f"{key}_pct", value)
        setattr(sample, f"{key}_resets_at",
                _epoch(payload[key].get("resets_at")))
    for name, window in payload.items():
        if name in FIRST_CLASS or name == "limits":
            continue
        value = _utilization(window)
        if value is None:
            continue
        sample.extra[name] = {"utilization": value,
                              "resets_at": _epoch(window.get("resets_at"))}
    return sample


def fetch(*, now: float | None = None, opener: Callable[..., Any] | None = None) -> Sample:
    """One reading of the endpoint. Raises `MeterError`; `sample_once` records the gap."""
    ts = db.now() if now is None else now
    bearer = token()
    req = urllib.request.Request(
        ENDPOINT, headers={"Authorization": f"Bearer {bearer}", "anthropic-beta": BETA})
    call = opener or urllib.request.urlopen
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        with call(req, timeout=TIMEOUT) as resp:
            status = getattr(resp, "status", None)
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise MeterError(f"http {e.code} from the usage endpoint",
                         http_status=int(e.code), latency_ms=elapsed()) from None
    except Exception as e:  # noqa: BLE001 — a transport failure is a gap row, not a crash
        raise MeterError(f"the usage endpoint did not answer ({type(e).__name__})",
                         latency_ms=elapsed()) from None
    latency = elapsed()
    if status is not None and int(status) != 200:
        raise MeterError(f"http {int(status)} from the usage endpoint",
                         http_status=int(status), latency_ms=latency)
    try:
        payload = json.loads(body)
    except ValueError:
        raise MeterError("the usage endpoint returned a body that is not JSON",
                         http_status=status, latency_ms=latency) from None
    try:
        sample = parse(payload, ts=ts)
    except MeterError as e:
        e.http_status = status if e.http_status is None else e.http_status
        e.latency_ms = latency
        raise
    sample.http_status = None if status is None else int(status)
    sample.latency_ms = latency
    return sample


def record(central: Any, sample: Sample) -> None:
    central.conn.execute(
        """INSERT INTO usage_samples (ts, ok, reason, http_status, latency_ms,
                                      five_hour_pct, five_hour_resets_at,
                                      seven_day_pct, seven_day_resets_at, extra_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sample.ts, 1 if sample.ok else 0, sample.reason, sample.http_status,
         sample.latency_ms, sample.five_hour_pct, sample.five_hour_resets_at,
         sample.seven_day_pct, sample.seven_day_resets_at,
         json.dumps(sample.extra, sort_keys=True)))


def sample_once(central: Any, *, opener: Callable[..., Any] | None = None,
                now: float | None = None) -> Sample:
    """Read, and record either the reading or ONE gap row saying why there is none."""
    ts = db.now() if now is None else now
    try:
        sample = fetch(now=ts, opener=opener)
    except MeterError as e:
        sample = Sample(ts=ts, ok=False, reason=e.reason, http_status=e.http_status,
                        latency_ms=e.latency_ms)
    record(central, sample)
    central.conn.commit()
    return sample


# -- reading the series back ------------------------------------------------------------


def _row_sample(row: Any) -> Sample:
    try:
        extra = json.loads(row["extra_json"] or "{}")
    except ValueError:
        extra = {}
    return Sample(
        ts=float(row["ts"]), ok=bool(row["ok"]), reason=row["reason"] or "",
        http_status=row["http_status"], latency_ms=row["latency_ms"],
        five_hour_pct=row["five_hour_pct"], five_hour_resets_at=row["five_hour_resets_at"],
        seven_day_pct=row["seven_day_pct"], seven_day_resets_at=row["seven_day_resets_at"],
        extra=extra if isinstance(extra, dict) else {})


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    """`mode=ro`: a read must never create or write the OS's own database (§8)."""
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def samples_between(home: Path, *, since: float, until: float) -> list[Sample]:
    conn = _connect_ro(Path(home) / "os.db")
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM usage_samples WHERE ts >= ? AND ts < ? ORDER BY ts",
            (since, until)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_row_sample(r) for r in rows]


@dataclass
class Streak:
    """The trailing run of gap rows, and the newest reading before it (§8)."""

    gap_rows: int = 0
    first_ts: float | None = None
    reason: str = ""
    last_ok_ts: float | None = None


def gap_streak(central: Any) -> Streak:
    try:
        rows = central.conn.execute(
            "SELECT ts, ok, reason FROM usage_samples ORDER BY ts DESC").fetchall()
    except sqlite3.Error:
        return Streak()
    found = Streak()
    for row in rows:
        if row["ok"]:
            found.last_ok_ts = float(row["ts"])
            break
        found.gap_rows += 1
        found.first_ts = float(row["ts"])
        found.reason = found.reason or (row["reason"] or "")
    return found


# -- 4. the segment sum -----------------------------------------------------------------


@dataclass
class Segment:
    start_ts: float
    end_ts: float
    start_pct: float
    last_pct: float
    delta: float
    cause: str = ""          #: "" first, "reset" on `resets_at`, "drop" on a late reset


@dataclass
class Coverage:
    samples: int = 0
    gap_rows: int = 0
    expected: int = 0
    share: float = 0.0
    longest_gap_seconds: float = 0.0
    head_uncovered_seconds: float = 0.0


@dataclass
class SegmentSum:
    segments: list[Segment] = field(default_factory=list)
    delta_points: float = 0.0
    reset_count: int = 0
    anomalies: int = 0
    coverage: Coverage = field(default_factory=Coverage)


def _boundary(a: Sample, b: Sample, key: str) -> str:
    """"", "reset" or "drop" between two consecutive readings (§4 rule 2)."""
    before, after = a.resets_at(key), b.resets_at(key)
    if before is not None and after is not None and before != after:
        return "reset"
    pct_a, pct_b = a.pct(key), b.pct(key)
    if pct_a is not None and pct_b is not None and pct_b < pct_a - DROP_TOLERANCE:
        return "drop"
    return ""


def segments(samples: Sequence[Sample], *, since: float, until: float, key: str,
             nearest_seconds: float | None = None) -> SegmentSum:
    """Per-segment delta, because utilisation drops to ~0 at `resets_at` (§4).

    `samples` may extend either side of the span: the sample at or before `since` is what
    makes the head covered. Gap rows are skipped here and counted in `coverage` — a
    failed read is an unknown, never a value.
    """
    if nearest_seconds is None:
        from .catalog import DEFAULT_COST_METER_NEAREST_SECONDS

        nearest_seconds = DEFAULT_COST_METER_NEAREST_SECONDS
    ordered = sorted(samples, key=lambda s: s.ts)
    inside = [s for s in ordered
              if s.ok and since <= s.ts < until and s.pct(key) is not None]
    gaps = [s for s in ordered if not s.ok and since <= s.ts < until]
    expected = max(1, int(round((until - since) / SAMPLE_SECONDS)))
    coverage = Coverage(samples=len(inside), gap_rows=len(gaps), expected=expected,
                        share=min(1.0, len(inside) / expected))
    if not inside:
        return SegmentSum(coverage=coverage)
    coverage.longest_gap_seconds = max(
        (b.ts - a.ts for a, b in zip(inside, inside[1:])), default=0.0)

    prior = [s for s in ordered
             if s.ok and s.ts < since and s.pct(key) is not None]
    head = prior[-1] if prior and since - prior[-1].ts <= nearest_seconds else None
    if head is None:
        coverage.head_uncovered_seconds = inside[0].ts - since

    found = SegmentSum(coverage=coverage)
    start_pct = (head or inside[0]).pct(key)
    start_ts = (head or inside[0]).ts
    cause = ""
    for index in range(1, len(inside) + 1):
        edge = ("" if index == len(inside)
                else _boundary(inside[index - 1], inside[index], key))
        if index < len(inside) and not edge:
            continue
        last = inside[index - 1]
        last_pct = float(last.pct(key) or 0.0)
        delta = last_pct - float(start_pct or 0.0)
        if delta < 0.0:
            found.anomalies += 1
        found.segments.append(Segment(start_ts=start_ts, end_ts=last.ts,
                                      start_pct=float(start_pct or 0.0),
                                      last_pct=last_pct, delta=max(0.0, delta),
                                      cause=cause))
        if edge == "drop":
            found.anomalies += 1
        if index < len(inside):
            # Every later segment starts at 0.0 BY DEFINITION: whatever was spent between
            # the reset instant and the first post-reset sample is inside this span (§4.4).
            start_pct, start_ts, cause = 0.0, inside[index].ts, edge
    found.delta_points = sum(s.delta for s in found.segments)
    found.reset_count = len(found.segments) - 1
    return found


# -- 5. dollars per point ---------------------------------------------------------------


def band(*, total_usd: float, total_points: float, spans: int) -> tuple[float, float]:
    """The quantisation band: the meter reports whole percent, so ±1 point PER SPAN (§5)."""
    low = total_usd / (total_points + spans) if total_points + spans > 0 else 0.0
    high = total_usd / max(total_points - spans, 0.5)
    return (low, high)


def _runs(rows: Sequence[Sample], key: str) -> list[list[Sample]]:
    """Maximal runs of consecutive readings with no gap row, no >120s hole, no reset."""
    runs: list[list[Sample]] = []
    current: list[Sample] = []

    def close() -> None:
        if len(current) > 1:
            runs.append(list(current))

    for row in rows:
        if not row.ok or row.pct(key) is None:
            close()
            current = []
            continue
        if current:
            previous = current[-1]
            if row.ts - previous.ts > MAX_SPAN_GAP_SECONDS or _boundary(previous, row, key):
                close()
                current = []
        current.append(row)
    close()
    return runs


def _visible_usd(*, since: float, until: float, home: Path) -> float:
    """Every dollar the OS can account for over the span: Jarvis's own (§7) plus §6's
    outside sessions.

    `fleetcost` imported here and not at module level: it reaches this module's callers,
    so a module-level import is a cycle.
    """
    from . import fleetcost

    total = 0.0
    for name, path in fleetcost.registered_paths(home).items():
        for row in fleetcost.turn_rows(paths.project_db_path(path), since, until,
                                       project=name):
            total += row.cost_usd or 0.0
    by_kind, _ = fleetcost.os_by_kind(since, until)
    total += sum(float(k["cost_usd"] or 0.0) for k in by_kind)
    total += sum(s.usd for s in outside_sessions(since=since, until=until, home=home))
    return total


def dollars_per_point(*, now: float, cfg: Any, home: Path,
                      visible: Callable[..., float] | None = None) -> dict[str, Any]:
    """What one 5h point costs, with its uncertainty stated as a band (§5).

    The MEDIAN of the per-span ratios, never the mean: one span containing an unseen
    phone session is an outlier that must not move the estimator.
    """
    since = now - cfg.meter_calibration_days * 86_400
    rows = samples_between(Path(home), since=since, until=now)
    spend = visible or (lambda *, since, until: _visible_usd(since=since, until=until,
                                                             home=Path(home)))
    ratios: list[float] = []
    total_usd = total_points = 0.0
    for run in _runs(rows, "five_hour"):
        points = float(run[-1].pct("five_hour") or 0.0) - float(run[0].pct("five_hour") or 0.0)
        if run[-1].ts - run[0].ts < cfg.meter_calibration_min_minutes * 60:
            continue
        if points < cfg.meter_calibration_min_points:
            continue
        usd = spend(since=run[0].ts, until=run[-1].ts)
        ratios.append(usd / points)
        total_usd += usd
        total_points += points
    if not ratios:
        seed = float(cfg.meter_dollars_per_point)
        return {"value": seed, "low": seed, "high": seed, "source": "seed",
                "basis_spans": 0, "basis_points": 0.0, "basis_usd": 0.0}
    low, high = band(total_usd=total_usd, total_points=total_points, spans=len(ratios))
    return {"value": statistics.median(ratios), "low": low, "high": high,
            "source": "measured" if len(ratios) >= 3 else "thin",
            "basis_spans": len(ratios), "basis_points": total_points,
            "basis_usd": total_usd}


# -- 6. outside-Jarvis spend, and the ownership predicate -------------------------------


@dataclass
class OutsideSession:
    """One session on this machine that no Jarvis record names (§6).

    Its subagents are folded IN rather than listed apart: a subagent is owned iff its
    lead is, so it has no row of its own and must not be counted under one too.
    """

    session_id: str
    project_dir: str                  #: the raw slugified cwd the transcript lives under
    project: str | None               #: the registered project, or None — outside them all
    title: str = ""
    models: list[str] = field(default_factory=list)
    calls: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    usd: float = 0.0
    first_ts: float = 0.0
    last_ts: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "project_dir": self.project_dir,
                "project": self.project, "title": self.title, "models": self.models,
                "calls": self.calls, "tokens": dict(self.tokens),
                "usd": round(self.usd, 2), "first_ts": self.first_ts,
                "last_ts": self.last_ts}


def owned_session_ids(home: Path) -> set[str]:
    """Every session id Jarvis minted — the ownership predicate, in full (§6).

    EXACTLY THREE RECORDS: a work order's live `session_id`, its `prior_sessions` (a
    pre-headless order forked a new id per turn), and `agent_calls.session_id` (Neo
    answers, panel seats, digests). `wo_turns` has no session column — the work-order row
    is the single source. The project SLUG is explicitly not part of this: a session
    created in a registered checkout that Jarvis never minted is OUTSIDE, which is the
    whole case being measured.
    """
    from . import fleetcost

    owned: set[str] = set()
    for path in fleetcost.registered_paths(Path(home)).values():
        conn = _connect_ro(paths.project_db_path(path))
        if conn is None:
            continue
        try:
            found = conn.execute(
                "SELECT session_id, prior_sessions FROM work_orders").fetchall()
        except sqlite3.Error:
            found = []
        finally:
            conn.close()
        for row in found:
            if row["session_id"]:
                owned.add(str(row["session_id"]))
            try:
                prior = json.loads(row["prior_sessions"] or "[]")
            except ValueError:
                prior = []
            if isinstance(prior, list):
                owned.update(str(s) for s in prior if s)
    conn = _connect_ro(Path(home) / "os.db")
    if conn is not None:
        try:
            owned.update(str(r["session_id"]) for r in conn.execute(
                "SELECT DISTINCT session_id FROM agent_calls WHERE session_id != ''"))
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return owned


def _fresh(path: Path, since: float) -> bool:
    """The mtime pre-filter: a transcript not written since `since` cannot hold a call
    inside the span, and the tree is 2.7 GB / ~11,900 lead files (§6.1)."""
    try:
        return path.stat().st_mtime >= since
    except OSError:
        return False


def _attribute(dir_name: str, slugs: dict[str, str]) -> str | None:
    """Which registered project a transcript directory belongs to, or None.

    `in_slug_scope` with the OTHER projects' slugs as `exclude`, never a bare
    `startswith`: `slug_of` is not injective, and a prefix test folded a sibling
    project's sessions into its neighbour's report (§6.5, PR 927 review).
    """
    matches = [name for name, slug in slugs.items()
               if nav_volume.in_slug_scope(
                   dir_name, slug,
                   [other for key, other in slugs.items() if key != name])]
    return max(matches, key=lambda name: len(slugs[name])) if matches else None


def _row_of(session_id: str, dir_name: str, project: str | None, calls: list[usage.Call],
            files: Sequence[Path]) -> OutsideSession:
    total = usage.Usage()
    for call in calls:
        total += usage.priced(call.model, input=call.input,
                              cache_write=call.cache_write, cache_read=call.cache_read,
                              output=call.output, cache_1h=call.cache_1h,
                              cache_5m=call.cache_5m)
    title = ""
    for path in files:
        title = usage.first_prompt(path)
        if title:
            break
    return OutsideSession(
        session_id=session_id, project_dir=dir_name, project=project, title=title,
        models=sorted({c.model for c in calls if c.model}), calls=len(calls),
        tokens={"input": total.input, "cache_write": total.cache_write,
                "cache_read": total.cache_read, "output": total.output,
                "cache_1h": total.cache_1h, "cache_5m": total.cache_5m,
                "total": total.total_tokens},
        usd=total.list_cost_usd, first_ts=calls[0].ts, last_ts=calls[-1].ts)


def outside_sessions(*, since: float, until: float, home: Path,
                     index: dict[str, list[Path]] | None = None) -> list[OutsideSession]:
    """Sessions on this machine Jarvis did not mint, priced over one span (§6).

    Read-only and network-free: transcripts on disk plus three `mode=ro` queries.
    Dedupe of a rewritten assistant message is NOT re-done here —
    `usage._assistant_messages` keys on `message.id` and takes the MAX of each field,
    and a genuine retry is a new id that must be counted (§6, dedupe).
    """
    root = usage.transcript_root()
    if not root.is_dir():
        return []
    from . import fleetcost

    owned = owned_session_ids(Path(home))
    slugs = {name: nav_volume.slug_of(path)
             for name, path in fleetcost.registered_paths(Path(home)).items()}
    if index is None:
        index = usage.index_sessions(root)
    found: list[OutsideSession] = []
    seen: set[str] = set()
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        for path in sorted(project_dir.glob("*.jsonl")):
            session_id = path.stem
            if session_id in owned or session_id in seen:
                continue
            # The lead's every segment, which `session_calls` merges, and then each
            # subagent file — owned iff its lead is, so a PATH test and never a slug one.
            calls = list(usage.session_calls(session_id, index=index)) \
                if _fresh(path, since) else []
            for sub in nav_volume._subagents_of(path):
                if _fresh(sub, since):
                    calls.extend(usage.calls_of(sub))
            # By CALL timestamp, never by file: a session straddling the span
            # contributes only the calls inside it (§6.3).
            inside = sorted((c for c in calls if since <= c.ts < until),
                            key=lambda c: c.ts)
            if not inside:
                continue
            seen.add(session_id)
            found.append(_row_of(session_id, project_dir.name,
                                 _attribute(project_dir.name, slugs), inside,
                                 index.get(session_id) or [path]))
    found.sort(key=lambda s: s.usd, reverse=True)
    return found


# -- 7. reconciliation, and the residual ------------------------------------------------

#: BOTH HALVES, EVERY TIME AND IN THIS ORDER (§7): a reader told only the first will
#: never suspect the second. Said once here and rendered, never re-worded.
RESIDUAL_LABEL = ("either usage this machine cannot see (claude.ai in a browser, the "
                  "phone app, another machine, a cloud session) or an accounting error "
                  "in Jarvis")

#: A span the meter has no reading for. Nothing is derived from it — no dollar figure, no
#: residual, no percentages (§9).
NO_SAMPLES = "no usage-meter samples cover this span"

#: The payload's own version key, the §10.6 rule the rest of the cost payload follows.
PAYLOAD_VERSION = 1

#: At most this many points reach a browser: a 7-day span is ~10,000 rows (§9).
TIMELINE_POINTS = 300


def _home_path(home: Path | str | None) -> Path:
    return Path(home) if home is not None else paths.jarvis_home()


def latest_sample(*, now: float, within: float,
                  home: Path | str | None = None) -> Sample | None:
    """The newest `ok` reading within `within` seconds of `now`, or None.

    What `fleetcost.resolve_window` anchors the 5h window on (§10). A reading older than
    that is not a reading of THIS window, and anchoring on it would move every boundary
    to a stale one.
    """
    rows = samples_between(_home_path(home), since=now - within, until=now + within)
    fresh = [s for s in rows if s.ok and s.five_hour_resets_at is not None]
    return fresh[-1] if fresh else None


def jarvis_spend(*, since: float, until: float, home: Path | str | None = None,
                 project: str | None = None) -> tuple[float, float]:
    """`(workers_usd, jarvis_calls_usd)` over the span, from records already kept (§7).

    The workers' half is summed as `fleetcost.report` already sums it. The OS's own half
    is `os_by_kind`, which ALREADY CONTAINS the unattributed line — that line is a subset
    of the by-kind rows, so adding it again would count Neo's between-turn answers twice.
    """
    from . import fleetcost

    root = _home_path(home)
    workers = 0.0
    for name, path in fleetcost.registered_paths(root).items():
        if project and name != project:
            continue
        db_path = paths.project_db_path(path)
        # A registered project that has never been written to has no database, which is
        # NOT a failure to read one: it has spent nothing.
        if not db_path.exists():
            continue
        for row in fleetcost.turn_rows(db_path, since, until, project=name):
            workers += row.cost_usd or 0.0
    by_kind, _ = fleetcost.os_by_kind(since, until, project)
    calls = sum(float(k["cost_usd"] or 0.0) for k in by_kind)
    return (workers, calls)


def timeline(samples: Sequence[Sample], *, points: int = TIMELINE_POINTS
             ) -> list[dict[str, Any]]:
    """The series, decimated to at most `points` buckets — the mean of each bucket, and
    `ok=False` if ANY sample in it was a gap (§9)."""
    rows = sorted(samples, key=lambda s: s.ts)
    if not rows:
        return []
    size = max(1, -(-len(rows) // points))
    found: list[dict[str, Any]] = []
    for start in range(0, len(rows), size):
        bucket = rows[start:start + size]
        ok = [s for s in bucket if s.ok]

        def mean(key: str, of=ok) -> float | None:
            values = [s.pct(key) for s in of if s.pct(key) is not None]
            return sum(values) / len(values) if values else None

        found.append({"ts": sum(s.ts for s in bucket) / len(bucket),
                      "five_hour_pct": mean("five_hour"),
                      "seven_day_pct": mean("seven_day"),
                      "ok": len(ok) == len(bucket)})
    return found


def sample_rows(*, since: float, until: float,
                home: Path | str | None = None) -> list[dict[str, Any]]:
    """The raw series for one span, as rows — what `ops.meter_samples` hands the page."""
    return [{"ts": s.ts, "five_hour_pct": s.five_hour_pct,
             "seven_day_pct": s.seven_day_pct, "ok": s.ok}
            for s in samples_between(_home_path(home), since=since, until=until)]


def _points(value: float) -> str:
    return f"{round(value, 1):g}"


def _money(value: float) -> str:
    """A negative figure keeps its sign OUTSIDE the currency, so `-$0.80` reads as a
    deficit rather than as `$-0.80` (§7: the residual is never clamped)."""
    return f"-${abs(value):.2f}" if value < 0 else f"${value:.2f}"


def _share(value: float | None) -> str:
    return f"{round(100 * (value or 0.0)):.0f}%"


def _named(sessions: Sequence[OutsideSession]) -> str:
    top = sessions[0]
    where = top.project or top.project_dir
    title = f' — "{top.title}"' if top.title else ""
    return f"session {top.session_id[:8]}, {where}{title}"


def sentence(*, five_hour: dict[str, Any], spend: dict[str, Any],
             sessions: Sequence[OutsideSession], dollars: dict[str, Any],
             coverage: dict[str, Any], covered: bool) -> str:
    """THE one sentence, built ONCE and rendered verbatim by both surfaces (§9).

    A payload key and not a renderer, for `fleetcost.NOTES`' reason: two renderers
    producing two wordings of the same arithmetic is a difference the user has to
    explain to themselves.
    """
    if not covered:
        return NO_SAMPLES
    implied = float(spend["implied_usd"] or 0.0)
    rough = dollars["source"] in ("thin", "seed")
    figure = f"roughly ${implied:.1f}" if rough else f"~${implied:.1f}"
    said = (f"the 5h meter rose {_points(five_hour['delta_points'])} points ({figure}); "
            f"Jarvis spent {_money(spend['jarvis_usd'])} "
            f"({_share(spend['jarvis_share'])})")
    outside = f"other sessions on this machine {_money(spend['outside_usd'])} " \
              f"({_share(spend['outside_share'])}"
    outside += f": {_named(sessions)})" if sessions else ")"
    said += f", {outside}, unexplained {_money(spend['residual_usd'])} " \
            f"({_share(spend['residual_share'])}) — {RESIDUAL_LABEL}"
    if spend.get("scope_project"):
        # The scoped figure, named and never subtracted from the meter (§7).
        said += (f"; {spend['scope_project']}'s own share of that is "
                 f"{_money(spend['scope_jarvis_usd'])}")
    if dollars["source"] == "seed":
        said += (f"; from the shipped {dollars['value']:g}/point estimate, not measured "
                 f"here")
    elif dollars["source"] == "thin":
        spans = dollars["basis_spans"]
        said += f"; from {spans} measured span{'s' if spans != 1 else ''}"
    if coverage["share"] < 0.9:
        said += (f"; the meter covers only {round(100 * coverage['share'])}% of this "
                 f"span, so the rise is a lower bound")
    if five_hour["reset_count"]:
        times = five_hour["reset_count"]
        said += (f"; the 5h window reset {'once' if times == 1 else f'{times} times'} "
                 f"inside this span, so the rise is summed per segment")
    return said


def _window_dict(sum_: SegmentSum) -> dict[str, Any]:
    segments_ = sum_.segments
    return {
        "start_pct": segments_[0].start_pct if segments_ else None,
        "end_pct": segments_[-1].last_pct if segments_ else None,
        "delta_points": round(sum_.delta_points, 2),
        "reset_count": sum_.reset_count,
        "anomalies": sum_.anomalies,
        "segments": [{"since": s.start_ts, "until": s.end_ts,
                      "start_pct": s.start_pct, "end_pct": s.last_pct,
                      "delta": round(s.delta, 2), "cause": s.cause}
                     for s in segments_],
    }


def _coverage_dict(cov: Coverage, rows: Sequence[Sample]) -> dict[str, Any]:
    ok = [s for s in rows if s.ok]
    return {"samples": cov.samples, "expected": cov.expected,
            "share": round(cov.share, 4), "gap_rows": cov.gap_rows,
            "longest_gap_seconds": cov.longest_gap_seconds,
            "head_uncovered_seconds": cov.head_uncovered_seconds,
            "first_ts": ok[0].ts if ok else None,
            "last_ts": ok[-1].ts if ok else None}


def reconciliation(*, resolved: dict[str, Any], project: str | None = None,
                   home: Path | str | None = None, now: float | None = None,
                   cfg: Any = None) -> dict[str, Any]:
    """The `meter` subtree: what the meter says, what the records say, and the gap (§9).

    One additive subtree under one key, so a consumer that does not know about the meter
    reads the rest of the cost payload unchanged. Read-only throughout: this is reached
    from `jarvis cost` and from `/cost`, and a read must never write (§8).
    """
    from . import fleetcost

    root = _home_path(home)
    cfg = cfg if cfg is not None else fleetcost.cost_config(project)
    since, until = float(resolved["since"]), float(resolved["until"])
    now = db.now() if now is None else now
    # The head of the span is covered by the sample BEFORE it, so the read reaches back
    # `meter_nearest_seconds` (§4) — `segments` decides what counts.
    rows = samples_between(root, since=since - cfg.meter_nearest_seconds, until=until)
    in_span = [s for s in rows if since <= s.ts < until]
    five = segments(rows, since=since, until=until, key="five_hour",
                    nearest_seconds=cfg.meter_nearest_seconds)
    seven = segments(rows, since=since, until=until, key="seven_day",
                     nearest_seconds=cfg.meter_nearest_seconds)
    dollars = dollars_per_point(now=now, cfg=cfg, home=root)
    covered = five.coverage.samples > 0

    sessions = outside_sessions(since=since, until=until, home=root)
    outside_usd = sum(s.usd for s in sessions)
    # The residual arithmetic is ALWAYS fleet-wide, whatever the query's scope (§7).
    workers_usd, calls_usd = jarvis_spend(since=since, until=until, home=root,
                                          project=None)
    jarvis_usd = workers_usd + calls_usd
    scope_workers, scope_calls = (
        jarvis_spend(since=since, until=until, home=root, project=project)
        if project else (None, None))
    implied = five.delta_points * float(dollars["value"]) if covered else None
    residual = (implied - jarvis_usd - outside_usd) if covered else None

    def share(value: float | None) -> float | None:
        if implied is None or implied <= 0 or value is None:
            return None
        return round(value / implied, 4)

    spend = {"implied_usd": round(implied, 2) if covered else None,
             "workers_usd": round(workers_usd, 2),
             "jarvis_calls_usd": round(calls_usd, 2),
             "jarvis_usd": round(jarvis_usd, 2),
             "outside_usd": round(outside_usd, 2),
             # NEVER CLAMPED: a negative residual is evidence about the estimator, and
             # clamping it to zero would hide the drift (§7).
             "residual_usd": round(residual, 2) if covered else None,
             "residual_share": share(residual), "outside_share": share(outside_usd),
             "jarvis_share": share(jarvis_usd),
             # Reported BESIDE the meter and never subtracted from it (§7).
             "scope_project": project,
             "scope_workers_usd": None if project is None else round(scope_workers, 2),
             "scope_jarvis_calls_usd": (None if project is None
                                        else round(scope_calls, 2)),
             "scope_jarvis_usd": (None if project is None
                                  else round(scope_workers + scope_calls, 2))}
    coverage = _coverage_dict(five.coverage, in_span)
    five_hour = _window_dict(five)
    return {"meter": {
        "version": PAYLOAD_VERSION,
        "window": {"since": since, "until": until,
                   "source": resolved.get("source", "")},
        "five_hour": five_hour,
        "seven_day": _window_dict(seven),
        "coverage": coverage,
        "dollars_per_point": dollars,
        "spend": spend,
        # The cap is on ROWS SHOWN; the remainder stays in `total_usd` and is disclosed
        # as `n` (§9).
        "outside": {"total_usd": round(outside_usd, 2), "n": len(sessions),
                    "sessions": [s.as_dict()
                                 for s in sessions[:cfg.meter_outside_rows]]},
        "timeline": timeline(in_span),
        "sentence": sentence(five_hour=five_hour, spend=spend, sessions=sessions,
                             dollars=dollars, coverage=coverage, covered=covered),
        "alerts": alerts(spend=spend, dollars=dollars, cfg=cfg),
    }}


def alerts(*, spend: dict[str, Any], dollars: dict[str, Any],
           cfg: Any) -> dict[str, Any]:
    """Which of the two conditions §8 alarms on is true of this span.

    Computed HERE, beside the arithmetic, and read by the daemon rather than re-derived
    there: one place decides, so the payload and the alarm can never disagree.
    """
    implied = spend["implied_usd"]
    big = implied is not None and implied >= cfg.meter_alert_min_usd
    # An alarm computed from a shipped constant is an alarm about the constant (§8).
    measured = dollars["source"] != "seed"
    outside = spend["outside_share"]
    residual = spend["residual_share"]
    return {
        "outside": bool(big and measured and outside is not None
                        and outside >= cfg.meter_outside_alert_share),
        "residual": bool(big and measured and residual is not None
                         and abs(residual) >= cfg.meter_residual_alert_share),
        "thresholds": {"outside": cfg.meter_outside_alert_share,
                       "residual": cfg.meter_residual_alert_share},
        "min_usd": cfg.meter_alert_min_usd,
    }
