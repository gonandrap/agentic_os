"""The steward: what the OS may spend on itself (docs/superpowers/specs/2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself.md)"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from . import db

if TYPE_CHECKING:  # pragma: no cover
    from .catalog import Catalog, EvolutionConfig
    from .central_store import CentralStore
    from .project_store import ProjectStore

log = logging.getLogger(__name__)

# §3.0 — one scale, least to most severe
SEVERITIES = ("cosmetic", "minor", "medium", "major", "critical", "blocker")
LOW = ("cosmetic", "minor")
URGENT = ("critical", "blocker")
#: `jarvis bug report --priority` on the §3.0 scale
BUG_PRIORITY_SEVERITY = {"low": "minor", "medium": "medium", "high": "major",
                         "critical": "critical", "blocker": "blocker"}

#: Where OS-originated work comes from (§3.4)
ORIGIN_KINDS = ("bug_agent", "investigation", "neo_rider", "red_main", "release",
                "remedy", "panel")

OUTCOMES = ("dispatch", "queue", "drop", "ask")

#: The work-order metadata key every steward-dispatched order carries: its provenance
STAMP_KEY = "steward"

WINDOW_SECONDS = 7 * 86400
PRECISION_WINDOW_SECONDS = 28 * 86400
#: A detector is judged on at least this many dispatched orders before it can be throttled
PRECISION_MIN_SAMPLES = 5

#: Events on a work order that mean the USER engaged with it (§3.2 decision 2)
USER_TOUCH_EVENTS = ("reviewed", "retry_requested", "budget_set", "budget_resumed")


def severity_rank(level: str) -> int:
    """Position on the §3.0 scale; an unknown word ranks as `medium`, neither ignored nor urgent"""
    level = (level or "").strip().lower()
    return SEVERITIES.index(level) if level in SEVERITIES else SEVERITIES.index("medium")


def normalise(level: str) -> str:
    """A rating on the §3.0 scale"""
    level = (level or "").strip().lower()
    level = BUG_PRIORITY_SEVERITY.get(level, level)
    return level if level in SEVERITIES else "medium"


def apply_evidence(level: str, evidence: Sequence[str]) -> str:
    """§3.0's evidence rule: `major` or above without evidence is lowered one level (#1122)"""
    level = normalise(level)
    if severity_rank(level) >= severity_rank("major") and not any(
            str(e).strip() for e in evidence):
        return SEVERITIES[severity_rank(level) - 1]
    return level


@dataclass
class Candidate:
    """One piece of work the OS proposes for itself"""

    origin_kind: str
    severity: str
    project: str
    subject: str
    evidence: tuple[str, ...] = ()
    #: The order whose run caused this one — what `generation` is counted from
    parent_wo_id: str = ""
    #: Distinct orders referencing the subject (`jarvis issues`' count), when it has one
    refs: int = 0

    def __post_init__(self) -> None:
        assert self.origin_kind in ORIGIN_KINDS, self.origin_kind


@dataclass
class Ledger:
    """Rolling 7-day spend, list-price USD (§3.2 decision 3)"""

    evolution_usd: float = 0.0
    critical_usd: float = 0.0
    fleet_usd: float = 0.0


@dataclass
class Decision:
    outcome: str
    rule: str
    reason: str
    severity: str
    claimed_severity: str
    generation: int
    mode: str
    #: Whether the caller may dispatch NOW
    allowed: bool = False
    budget_usd: float | None = None
    decision_id: int = 0
    stamp: dict[str, Any] = field(default_factory=dict)

    @property
    def would_dispatch(self) -> bool:
        return self.outcome == "dispatch"


def stamp_of(wo: Mapping[str, Any] | None) -> dict[str, Any]:
    """An order's steward provenance, or {} for one the steward never decided"""
    if not wo:
        return {}
    meta = db.from_json(wo.get("metadata"), {}) or {}
    stamp = meta.get(STAMP_KEY) if isinstance(meta, dict) else None
    return dict(stamp) if isinstance(stamp, dict) else {}


def lineage(store: ProjectStore | None, parent_wo_id: str) -> tuple[int, str]:
    """(generation the candidate WOULD have, origin kind of its parent or '')"""
    if store is None or not parent_wo_id:
        return 1, ""
    try:
        parent = store.get_work_order(parent_wo_id)
    except KeyError:
        return 1, ""
    stamp = stamp_of(parent)
    return int(stamp.get("generation", 0)) + 1, str(stamp.get("origin_kind") or "")


def decide(c: Candidate, cfg: EvolutionConfig, ledger: Ledger, *, generation: int,
           parent_kind: str = "", throttled: bool = False) -> tuple[str, str, str, str]:
    """(outcome, rule, reason, severity) — §3.2 decision 1, first matching rule wins"""
    severity = apply_evidence(c.severity, c.evidence)
    room = cfg.weekly_usd - ledger.evolution_usd
    if severity in LOW:
        return "drop", "low-severity", f"`{severity}` is never worth an order", severity
    if c.origin_kind == "release":
        # A7: releases ship what already landed; counted on the ledger, not gated by it
        return "dispatch", "release", "a landed fix ships", severity
    if generation > cfg.max_depth:
        return ("queue", "max-depth",
                f"generation {generation} exceeds max_depth {cfg.max_depth}", severity)
    if parent_kind and parent_kind == c.origin_kind:
        return ("queue", "self-reference",
                f"its subject is work the `{c.origin_kind}` detector itself created",
                severity)
    if severity in URGENT:
        spent = ledger.critical_usd
        if spent >= cfg.critical_weekly_usd or room <= 0:
            return ("ask", "critical-ceiling",
                    f"`{severity}`, but the critical ceiling is spent "
                    f"(${spent:.2f} of ${cfg.critical_weekly_usd:.2f}, "
                    f"${max(room, 0):.2f} left this week)", severity)
        return "dispatch", "urgent", f"`{severity}` with evidence: act now", severity
    if throttled:
        return ("queue", "low-precision",
                f"the user picks up too little of what `{c.origin_kind}` files", severity)
    if ledger.fleet_usd > cfg.yield_above_usd:
        return ("queue", "yield",
                f"fleet spend ${ledger.fleet_usd:.2f} this week is above "
                f"${cfg.yield_above_usd:.2f}: feature work comes first", severity)
    if room <= 0:
        return ("queue", "ledger",
                f"the evolution ledger is spent (${ledger.evolution_usd:.2f} of "
                f"${cfg.weekly_usd:.2f})", severity)
    threshold = cfg.major_min_refs if severity == "major" else cfg.medium_min_refs
    if c.refs < threshold:
        return ("queue", "recurrence",
                f"`{severity}` referenced by {c.refs} order(s); {threshold} needed",
                severity)
    return ("dispatch", severity, f"`{severity}`, recurring, and the ledger has room",
            severity)


# ---------------------------------------------------------------- the ledger


def _project_stores(catalog: Catalog) -> Iterable[tuple[str, Path]]:
    for spec in getattr(catalog, "projects", []) or []:
        path = Path(spec.path)
        if (path / ".jarvis" / "jarvis.db").exists():
            yield spec.name, path


def stamped_orders(store: ProjectStore, since: float) -> list[dict[str, Any]]:
    """Every order the steward dispatched that was created since `since`"""
    rows = store.conn.execute(
        "SELECT * FROM work_orders WHERE created_at>=? AND metadata LIKE ?",
        (since, f'%"{STAMP_KEY}"%')).fetchall()
    return [dict(r) for r in rows if stamp_of(dict(r))]


def ledger(catalog: Catalog, central: CentralStore | None = None, *,
           now: float | None = None) -> Ledger:
    """The rolling-7-day ledger, fleet-wide"""
    from . import budget
    from .project_store import OPEN_STATUSES, ProjectStore

    since = (now if now is not None else db.now()) - WINDOW_SECONDS
    out = Ledger()
    for _name, path in _project_stores(catalog):
        store = ProjectStore(path)
        try:
            row = store.conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM wo_turns WHERE started_at>=?",
                (since,)).fetchone()
            out.fleet_usd += float(row["c"] or 0.0)
            for wo in stamped_orders(store, since):
                cost = budget.spent(store, central, wo["id"]).total_usd
                reserved = 0.0
                if wo.get("status") in OPEN_STATUSES and wo.get("budget_usd"):
                    reserved = max(0.0, float(wo["budget_usd"]) - cost)
                out.evolution_usd += cost + reserved
                if stamp_of(wo).get("severity") in URGENT:
                    out.critical_usd += cost + reserved
        finally:
            store.close()
    if central is not None:
        out.fleet_usd += central.call_cost_since(since)
    return out


# ---------------------------------------------------------------- precision


def touched(store: ProjectStore, wo_id: str) -> bool:
    """Did the user engage with this order at all — review it, message it, retry it?"""
    marks = ",".join("?" * len(USER_TOUCH_EVENTS))
    if store.conn.execute(
            f"SELECT 1 FROM wo_events WHERE wo_id=? AND kind IN ({marks}) LIMIT 1",
            (wo_id, *USER_TOUCH_EVENTS)).fetchone():
        return True
    return bool(store.conn.execute(
        "SELECT 1 FROM wo_messages WHERE wo_id=? AND direction='user_to_agent' "
        "AND (authored_by<>'' OR source='ui') LIMIT 1", (wo_id,)).fetchone())


def precision(catalog: Catalog, *, now: float | None = None) -> dict[str, tuple[int, int]]:
    """{origin_kind: (touched, dispatched)} over the last 28 days of stamped orders"""
    from .project_store import ProjectStore

    since = (now if now is not None else db.now()) - PRECISION_WINDOW_SECONDS
    out: dict[str, tuple[int, int]] = {}
    for _name, path in _project_stores(catalog):
        store = ProjectStore(path)
        try:
            for wo in stamped_orders(store, since):
                kind = str(stamp_of(wo).get("origin_kind") or "")
                hit, total = out.get(kind, (0, 0))
                out[kind] = (hit + int(touched(store, wo["id"])), total + 1)
        finally:
            store.close()
    return out


def throttled(cfg: EvolutionConfig, counts: Mapping[str, tuple[int, int]]) -> set[str]:
    """Detectors below `min_precision` on enough samples to say so (§3.2 decision 2)"""
    return {kind for kind, (hit, total) in counts.items()
            if total >= PRECISION_MIN_SAMPLES and hit / total < cfg.min_precision}


# ---------------------------------------------------------------- the gate


def gate(c: Candidate, *, catalog: Catalog, store: ProjectStore | None = None,
         central: CentralStore | None = None, self_dispatch: bool | None = None,
         now: float | None = None) -> Decision:
    """Decide, record, and say whether the caller may dispatch now"""
    from .central_store import CentralStore

    cfg = catalog.os.evolution
    mode = cfg.mode
    if self_dispatch is None:
        self_dispatch = bool(catalog.os.autonomy.self_dispatch)
    own_central = central is None
    central = central or CentralStore()
    try:
        generation, parent_kind = lineage(store, c.parent_wo_id)
        try:
            book = ledger(catalog, central, now=now)
            slow = throttled(cfg, precision(catalog, now=now))
        except Exception:  # noqa: BLE001 — see the docstring
            log.exception("steward: ledger unreadable; falling back to self_dispatch")
            book, slow = Ledger(), set()
        outcome, rule, reason, severity = decide(
            c, cfg, book, generation=generation, parent_kind=parent_kind,
            throttled=c.origin_kind in slow)
        claimed = normalise(c.severity)
        if mode == "enforce":
            allowed = self_dispatch and outcome == "dispatch"
        else:
            allowed = self_dispatch
        budget_usd = None
        if outcome == "dispatch" and mode == "enforce" and c.origin_kind != "release":
            budget_usd = round(max(0.0, min(cfg.order_budget_usd,
                                            cfg.weekly_usd - book.evolution_usd)), 2)
        decision = Decision(outcome=outcome, rule=rule, reason=reason, severity=severity,
                            claimed_severity=claimed, generation=generation, mode=mode,
                            allowed=allowed, budget_usd=budget_usd)
        decision.stamp = {"origin_kind": c.origin_kind, "generation": generation,
                          "severity": severity, "mode": mode}
        repeat = _repeat_of(central, c, outcome, now)
        if repeat:
            # A site the daemon re-asks every tick (a red `main`
            decision.decision_id = repeat
            decision.stamp["decision_id"] = repeat
            return decision
        decision.decision_id = central.record_steward_decision({
            "ts": now, "project": c.project, "origin_kind": c.origin_kind,
            "subject": c.subject[:500], "severity": severity, "claimed_severity": claimed,
            "outcome": outcome, "rule": rule, "reason": reason, "mode": mode,
            "applied": int(allowed and outcome == "dispatch"), "generation": generation,
            "parent_wo_id": c.parent_wo_id, "budget_usd": budget_usd})
        decision.stamp["decision_id"] = decision.decision_id
        if outcome == "ask" and mode == "enforce" and self_dispatch:
            central.add_inbox(c.project, f"steward: {c.subject[:120]}",
                              f"{reason}. Decide it yourself, or raise "
                              f"os.evolution.critical_weekly_usd.", level="critical")
        log.info("steward[%s] %s %s -> %s (%s): %s", mode, c.origin_kind, c.subject[:80],
                 outcome, rule, reason)
        return decision
    finally:
        if own_central:
            central.close()


#: How long an identical decision is not recorded again
REPEAT_SECONDS = 3600


def _repeat_of(central: CentralStore, c: Candidate, outcome: str,
               now: float | None) -> int:
    """The id of the same decision about the same subject in the last hour, or 0"""
    since = (now if now is not None else db.now()) - REPEAT_SECONDS
    row = central.conn.execute(
        "SELECT id FROM steward_decisions WHERE ts>=? AND project=? AND origin_kind=? "
        "AND subject=? AND outcome=? AND wo_id='' ORDER BY id DESC LIMIT 1",
        (since, c.project, c.origin_kind, c.subject[:500], outcome)).fetchone()
    return int(row["id"]) if row else 0


def stamped(metadata: Mapping[str, Any] | None, decision: Decision) -> dict[str, Any]:
    """`metadata` with the decision's provenance on it — written AT CREATION (`ops.create_work_order`)"""
    return {**(metadata or {}), STAMP_KEY: decision.stamp}


def linked(decision: Decision, wo_id: str, central: CentralStore | None = None) -> None:
    """Record which order a decision led to"""
    if not decision.decision_id or not wo_id:
        return
    from .central_store import CentralStore

    own = central is None
    central = central or CentralStore()
    try:
        central.link_steward_decision(decision.decision_id, wo_id)
    except Exception:  # noqa: BLE001
        log.exception("steward: could not link decision %s to %s",
                      decision.decision_id, wo_id)
    finally:
        if own:
            central.close()


# ---------------------------------------------------------------- the weekly digest


def digest(catalog: Catalog, central: CentralStore, *, now: float | None = None
           ) -> dict[str, Any]:
    """§3.2 decision 2's one artefact: spend by detector, what was held back, precision"""
    now = now if now is not None else db.now()
    cfg = catalog.os.evolution
    book = ledger(catalog, central, now=now)
    rows = central.steward_decisions(now - WINDOW_SECONDS, limit=5000)
    by_kind: dict[str, dict[str, int]] = {}
    for row in rows:
        counts = by_kind.setdefault(row["origin_kind"], {o: 0 for o in OUTCOMES})
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    counts = precision(catalog, now=now)
    return {"mode": cfg.mode, "ledger": book, "weekly_usd": cfg.weekly_usd,
            "critical_weekly_usd": cfg.critical_weekly_usd,
            "yield_above_usd": cfg.yield_above_usd, "by_kind": by_kind,
            "held": [r for r in rows if r["outcome"] in ("queue", "ask")][:10],
            "precision": counts, "throttled": sorted(throttled(cfg, counts))}


def render_digest(d: Mapping[str, Any]) -> str:
    book: Ledger = d["ledger"]
    lines = [f"steward ({d['mode']}): evolution ${book.evolution_usd:.2f} of "
             f"${d['weekly_usd']:.2f} this week, critical ${book.critical_usd:.2f} of "
             f"${d['critical_weekly_usd']:.2f}; fleet ${book.fleet_usd:.2f} "
             f"(new self-work yields above ${d['yield_above_usd']:.2f})"]
    for kind, counts in sorted(d["by_kind"].items()):
        lines.append(f"  {kind:<14} " + "  ".join(f"{o} {counts.get(o, 0)}"
                                                    for o in OUTCOMES))
    if d["precision"]:
        lines.append("precision (user touched / dispatched, 28 days):")
        for kind, (hit, total) in sorted(d["precision"].items()):
            mark = "  THROTTLED" if kind in d["throttled"] else ""
            lines.append(f"  {kind:<14} {hit}/{total}{mark}")
    if d["held"]:
        lines.append("held back:")
        lines += [f"  [{r['outcome']}] {r['origin_kind']} {r['severity']}: "
                  f"{r['subject'][:70]} — {r['rule']}" for r in d["held"]]
    return "\n".join(lines)


#: os_state key holding when the weekly digest last went to the inbox
DIGEST_STATE_KEY = "steward_digest_at"


def digest_due(central: CentralStore, *, now: float | None = None) -> bool:
    now = now if now is not None else db.now()
    return now - float(central.get_state(DIGEST_STATE_KEY) or 0) >= WINDOW_SECONDS


def send_digest(catalog: Catalog, central: CentralStore, *, now: float | None = None
                ) -> int:
    """One inbox item a week; 0 when the steward decided nothing that week"""
    now = now if now is not None else db.now()
    d = digest(catalog, central, now=now)
    central.set_state(DIGEST_STATE_KEY, str(now))
    if not d["by_kind"]:
        return 0
    return central.add_inbox("", "steward: the week in self-evolution",
                             render_digest(d), level="info")
