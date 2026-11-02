"""The steward: what the OS may spend on itself (docs/superpowers/specs/2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself.md)"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

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

#: Where OS-originated work comes from (§3.4); `io_finding` is a finding Neo, not the user, accepted
ORIGIN_KINDS = ("bug_agent", "investigation", "neo_rider", "red_main", "release",
                "remedy", "panel", "morpheus", "worker_io", "io_finding")

OUTCOMES = ("dispatch", "queue", "drop", "ask")

#: The work-order metadata key every steward-dispatched order carries: its provenance
STAMP_KEY = "steward"

WINDOW_SECONDS = 7 * 86400
PRECISION_WINDOW_SECONDS = 28 * 86400
#: A detector is judged on at least this many dispatched orders before it can be throttled
PRECISION_MIN_SAMPLES = 5
#: Measured floor for a one-pass order: less room queues, never dispatches scraps (#1305)
MIN_ORDER_USD = 5.0

#: Events on a work order that mean the USER engaged with it (§3.2 decision 2)
USER_TOUCH_EVENTS = ("reviewed", "retry_requested", "budget_set", "budget_resumed",
                     # `jarvis io review`, on the analyst: only the user reviews findings
                     "findings_reviewed")


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
    #: The file the candidate's defect is in — what it is deduped by (`dedupe_key`)
    file: str = ""
    #: What the caller needs to dispatch it LATER, should it queue (`drain`, #1181)
    payload: dict[str, Any] = field(default_factory=dict)

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
    #: The decision this one restates, when the candidate is a re-wording of it
    duplicate_of: int = 0
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
        if spent >= cfg.critical_weekly_usd or room < MIN_ORDER_USD:
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
    if room < MIN_ORDER_USD:
        return ("queue", "ledger",
                f"the evolution ledger is spent (${ledger.evolution_usd:.2f} of "
                f"${cfg.weekly_usd:.2f}; an order needs ${MIN_ORDER_USD:.2f})", severity)
    threshold = cfg.min_refs_by_kind.get(
        c.origin_kind, cfg.major_min_refs if severity == "major" else cfg.medium_min_refs)
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


def stamped_features(store: ProjectStore, since: float) -> list[dict[str, Any]]:
    """Every feature order (a worker's io) the steward dispatched since `since` (#59)"""
    rows = store.conn.execute(
        "SELECT * FROM feature_orders WHERE created_at>=? AND metadata LIKE ?",
        (since, f'%"{STAMP_KEY}"%')).fetchall()
    return [dict(r) for r in rows if stamp_of(dict(r))]


def _features_spend(store: ProjectStore, central: CentralStore | None, since: float,
                    counted: set[str]) -> Iterable[tuple[dict[str, Any], float]]:
    """(feature order, spend + what its budget still reserves) — orders in `counted` once only"""
    from . import budget

    for fo in stamped_features(store, since):
        cost = sum(budget.spent(store, central, child["id"]).total_usd
                   for child in budget.family(store, fo) if child["id"] not in counted)
        reserved = 0.0
        if not budget.is_settled_feature(fo) and fo.get("budget_usd"):
            reserved = max(0.0, float(fo["budget_usd"]) - cost)
        yield fo, cost + reserved


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
            orders = stamped_orders(store, since)
            for fo, usd in _features_spend(store, central, since,
                                           {wo["id"] for wo in orders}):
                out.evolution_usd += usd
                if stamp_of(fo).get("severity") in URGENT:
                    out.critical_usd += usd
            for wo in orders:
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
    """{origin_kind: (touched, dispatched)} over the last 28 days of stamped orders and ios"""
    from . import budget
    from .project_store import ProjectStore

    since = (now if now is not None else db.now()) - PRECISION_WINDOW_SECONDS
    out: dict[str, tuple[int, int]] = {}
    for _name, path in _project_stores(catalog):
        store = ProjectStore(path)
        try:
            units = [(wo, touched(store, wo["id"])) for wo in stamped_orders(store, since)
                     if not wo.get("parent_id") or not _stamped_parent(store, wo)]
            # An io is touched when the user engaged with any order in its family (#59)
            units += [(fo, any(touched(store, child["id"])
                               for child in budget.family(store, fo)))
                      for fo in stamped_features(store, since)]
            for unit, hit in units:
                kind = str(stamp_of(unit).get("origin_kind") or "")
                hits, total = out.get(kind, (0, 0))
                out[kind] = (hits + int(hit), total + 1)
        finally:
            store.close()
    return out


def _stamped_parent(store: ProjectStore, wo: Mapping[str, Any]) -> bool:
    """Is this order part of a stamped io's family, so the io is the one sample?"""
    try:
        return bool(stamp_of(store.get_feature_order(str(wo["parent_id"]))))
    except KeyError:
        return False


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
        key = dedupe_key(c)
        prior = _duplicate_of(central, key, c)
        duplicate = int(prior["id"]) if prior else 0
        if prior:
            # One defect worded by several seats, or re-raised in a later round
            severer = severity_rank(severity) > severity_rank(str(prior["severity"]))
            dispatched = bool(prior["wo_id"]) or bool(prior["applied"])
            held = _candidate_of(prior)
            if severer and not dispatched:
                # One decision per defect, re-decided at the max severity (Q2133)
                reason = f"{reason}; escalated from `{prior['severity']}` (#{duplicate})"
                decision.reason = reason
                decision.decision_id = duplicate
                # The severer wording is the one acted on; the earlier one is kept (#57)
                worded = _also_worded(held, str(prior["subject"] or ""), c.subject)
                central.update_steward_decision(duplicate, {
                    "severity": severity, "claimed_severity": claimed, "outcome": outcome,
                    "rule": rule, "reason": reason, "budget_usd": budget_usd,
                    "applied": int(allowed and outcome == "dispatch"),
                    "subject": c.subject[:500],
                    "candidate_json": _candidate_json(
                        c, also_worded=worded, raised=held.get("raised_pending") or []),
                    **_queue_fields(c, outcome, allowed, now)})
                decision.stamp["decision_id"] = duplicate
                log.info("steward[%s] escalated decision #%s to %s -> %s (%s)",
                         mode, duplicate, severity, outcome, rule)
                return decision
            outcome, rule = "drop", "duplicate"
            reason = f"the same defect as steward decision #{duplicate}"
            # Every seat's wording travels with the one decision, whatever it became (#1181)
            held["also_worded"] = _also_worded(held, c.subject, str(prior["subject"] or ""))
            if severer:
                # The order fixing it is already running: no second order, but say it got worse
                rule = "duplicate-of-dispatched"
                reason = (f"steward decision #{duplicate} already dispatched this defect; "
                          f"raised there from `{prior['severity']}` to `{severity}`")
                raised = _raise(severity, c, duplicate)
                if not _worsened(store, str(prior["wo_id"] or ""), raised):
                    # Applied but not linked yet: `linked` delivers it (#54)
                    held.setdefault("raised_pending", []).append(raised)
                central.update_steward_decision(duplicate, {
                    "severity": severity, "candidate_json": json.dumps(held, default=str)})
            else:
                central.update_steward_decision(
                    duplicate, {"candidate_json": json.dumps(held, default=str)})
            decision.outcome, decision.rule, decision.reason = outcome, rule, reason
            decision.duplicate_of = duplicate
            if mode == "enforce":
                allowed, budget_usd = False, None
                decision.allowed, decision.budget_usd = allowed, budget_usd
        repeat = 0 if duplicate else _repeat_of(central, c, outcome, now)
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
            "parent_wo_id": c.parent_wo_id, "budget_usd": budget_usd,
            "dedupe_key": key, "duplicate_of": duplicate,
            "candidate_json": _candidate_json(c),
            **({} if duplicate else _queue_fields(c, outcome, allowed, now))})
        decision.stamp["decision_id"] = decision.decision_id
        if outcome == "ask" and mode == "enforce" and self_dispatch:
            central.add_inbox(c.project, f"steward: {c.subject[:120]}",
                              ask_body(c, reason, decision.decision_id, catalog),
                              level="critical", wo_id=c.parent_wo_id or None)
        log.info("steward[%s] %s %s -> %s (%s): %s", mode, c.origin_kind, c.subject[:80],
                 outcome, rule, reason)
        return decision
    finally:
        if own_central:
            central.close()


def ask_body(c: Candidate, reason: str, decision_id: int, catalog: Catalog) -> str:
    """An `ask`'s inbox body: the one command that acts on it, and where it is listed"""
    from .notify import ui_base_url

    item = str(c.payload.get("morpheus_item") or "")
    act = act_on(c.origin_kind, item, c.parent_wo_id, c.project)
    if c.origin_kind == "morpheus" and item:
        act += f" {ui_base_url(catalog)}/morpheus#{item}"
    # An `ask` is never re-offered (`_queue_fields`), so the cap is no answer for THIS one
    return (f"{reason}. Act: {act}. Raising os.evolution.critical_weekly_usd lets later "
            f"ones through; it does not re-run this one.\n"
            f"{ui_base_url(catalog)}/steward#decision-{decision_id}")


def act_on(origin_kind: str, morpheus_item: str, parent_wo_id: str, project: str) -> str:
    """The one command that acts on held self-work, by where it came from"""
    if origin_kind == "morpheus" and morpheus_item:
        return (f"`jarvis morpheus confirm {morpheus_item} --fix \"<what to fix>\"` files the "
                f"fix; `jarvis morpheus waive|requeue {morpheus_item}` if it is not broken.")
    if origin_kind == "morpheus":
        return "`jarvis morpheus status` names the item; then `jarvis morpheus confirm <mv-id>`"
    if parent_wo_id:
        return f"`jarvis investigate {parent_wo_id} --why \"<what you saw>\"`"
    return f"`jarvis wo create {project} \"<title>\"` to file it by hand"


def next_step(row: Mapping[str, Any], now: float | None = None) -> dict[str, Any]:
    """What a steward decision asks of the user: `yours` and one line saying what to do, if anything"""
    item = str((_candidate_of(row).get("payload") or {}).get("morpheus_item") or "")
    act = act_on(row.get("origin_kind") or "", item, row.get("parent_wo_id") or "",
                 row.get("project") or "")
    outcome, state = row.get("outcome"), row.get("queue_state") or ""
    if row.get("duplicate_of"):
        return {"yours": False, "text": f"Nothing to do: the same defect as decision "
                f"#{row['duplicate_of']}, which carries it."}
    if row.get("wo_id"):
        return {"yours": False, "text": f"Nothing to do: order {row['wo_id']} carries it."}
    if outcome == "ask" and not row.get("applied"):
        if (now or db.now()) - float(row.get("ts") or 0) < WINDOW_SECONDS:
            return {"yours": True, "text": (
                f"Needs you: the OS held this {row.get('severity')} fix because its weekly "
                f"critical ceiling is spent, and it never retries it. Act: {act} Or leave it "
                "if it no longer matters. Raising os.evolution.critical_weekly_usd lets later "
                "ones through; it does not re-run this one.")}
        return {"yours": False, "text": f"No longer waiting on you (asked over "
                f"{WINDOW_SECONDS // 86400} days ago). Only if it still matters: {act}"}
    if state == "waiting":
        return {"yours": False, "text": (
            f"Nothing to do: the daemon re-offers it every ~{DRAIN_EVERY_MINUTES} min once "
            f"the ledger has room, and drops it after {QUEUE_TTL_SECONDS // 86400} days. "
            f"Want it now: {act}")}
    if state == "reoffered":
        return {"yours": False, "text": "Nothing to do: re-offered from the queue."}
    if state == "expired":
        return {"yours": False, "text": "Nothing to do: expired from the queue; never re-offered."}
    if outcome == "drop":
        return {"yours": False, "text": "Nothing to do: recorded for counting only."}
    if outcome == "queue":
        return {"yours": False, "text": "Nothing to do: held, not dispatched; its source "
                "asks again by itself, or it stays on the tracker for `jarvis issues start`."}
    if row.get("mode") == "shadow":
        return {"yours": False, "text": "Nothing to do: shadow record of what the steward "
                "would have done."}
    return {"yours": False, "text": "Nothing to do: dispatched."}


#: How long an identical decision is not recorded again
REPEAT_SECONDS = 3600


def dedupe_key(c: Candidate) -> str:
    """One FILE's bucket: origin unit + file, '' when no code is named (spec §3.2, Q2109)"""
    path = c.file.strip().lower()
    if not path or not c.parent_wo_id:
        return ""
    return f"{c.project}|{c.origin_kind}|{c.parent_wo_id}|{path}"


#: Words that say nothing about WHICH defect a wording is about
_STOPWORDS = frozenset("""
    the and for when that this with but not yet its are was were from into than then once
    only one has have had been out off all any can does did never still also which while
    what who why how per via every each same now
""".split())
_WORD_RE = re.compile(r"[a-z0-9]+")
#: Shared words over the SHORTER wording's words at which two wordings are one defect (#55)
SAME_DEFECT_OVERLAP = 0.4


def _defect_words(text: str, ignore: frozenset[str] = frozenset()) -> set[str]:
    words = set()
    for w in _WORD_RE.findall((text or "").lower()):
        if len(w) < 3 or w in _STOPWORDS or w in ignore:
            continue
        words.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return words


def same_defect(a: str, b: str, *, file: str = "") -> bool:
    """Do two wordings in one file's bucket name the same defect? (#55, spec §3.2)"""
    # The file's own name is in most wordings of every defect in it, so it tells none apart
    ignore = frozenset(_WORD_RE.findall(file.lower()))
    left, right = _defect_words(a, ignore), _defect_words(b, ignore)
    if not left or not right:
        return True  # a wording with nothing to tell it apart cannot be told apart
    return len(left & right) / min(len(left), len(right)) >= SAME_DEFECT_OVERLAP


def _candidate_of(row: Mapping[str, Any]) -> dict[str, Any]:
    held = db.from_json(row.get("candidate_json"), {}) or {}
    return dict(held) if isinstance(held, dict) else {}


def _candidate_json(c: Candidate, *, also_worded: Sequence[str] = (),
                    raised: Sequence[Mapping[str, Any]] = ()) -> str:
    """The whole candidate, as `steward_decisions.candidate_json` keeps it"""
    return json.dumps({**asdict(c), "also_worded": list(also_worded),
                       "raised_pending": list(raised)}, default=str)


def _also_worded(held: Mapping[str, Any], *wordings: str) -> list[str]:
    """`held`'s other wordings plus all of `wordings` but the last, the one acted on"""
    *extra, acting = wordings
    out: list[str] = []
    for w in [*(held.get("also_worded") or ()), *extra]:
        w = str(w)[:500]
        if w.strip() and w != acting[:500] and w not in out:
            out.append(w)
    return out


def _duplicate_of(central: CentralStore, key: str, c: Candidate) -> dict[str, Any] | None:
    """The decision in `key`'s bucket this candidate restates, or None (spec §3.2, #55)"""
    if not key:
        return None
    best, score = None, 0
    for row in central.steward_dedupe_bucket(key):
        wordings = [str(row.get("subject") or ""),
                    *(_candidate_of(row).get("also_worded") or ())]
        # Newest first, so a tie goes to the most recent decision about the defect
        hits = sum(same_defect(c.subject, str(w), file=c.file) for w in wordings)
        if hits > score:
            best, score = row, hits
    return best


def _raise(severity: str, c: Candidate, decision_id: int) -> dict[str, Any]:
    return {"severity": severity, "subject": c.subject[:200],
            "origin_kind": c.origin_kind, "decision_id": decision_id}


def _event_target(store: ProjectStore, order_id: str) -> str:
    """The work order whose timeline carries an event about `order_id`, or '' for none yet"""
    try:
        store.get_work_order(order_id)
        return order_id
    except KeyError:
        pass
    try:
        fo = store.get_feature_order(order_id)
    except KeyError:
        return ""
    # A feature order's events live on its planner's (an io's analyst's) or manager's timeline
    if fo.get("plan_wo_id"):
        return str(fo["plan_wo_id"])
    manager = store.manager_work_order(order_id)
    return str(manager["id"]) if manager else ""


def _worsened(store: ProjectStore | None, wo_id: str, raised: Mapping[str, Any]) -> bool:
    """Tell the order already fixing this defect that a seat now rates it worse (Q2133)"""
    if store is None or not wo_id:
        return False
    try:
        target = _event_target(store, wo_id)
        if not target:
            return False
        store.add_event(target, "steward_severity_raised", dict(raised))
        return True
    except Exception:  # noqa: BLE001 — the decision is recorded either way
        log.exception("steward: could not record the raised severity on %s", wo_id)
        return False


def _queue_fields(c: Candidate, outcome: str, allowed: bool,
                  now: float | None) -> dict[str, Any]:
    """A held `queue` the OS can dispatch later waits for `drain`; anything else does not"""
    waits = (outcome == "queue" and not allowed and c.origin_kind in DRAINABLE
             and bool(c.payload))
    return {"queue_state": "waiting" if waits else "", "queue_note": "",
            "queue_ts": (now if now is not None else db.now()) if waits else None}


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


def linked(decision: Decision, wo_id: str, central: CentralStore | None = None, *,
           store: ProjectStore | None = None) -> None:
    """Record which order a decision led to, and hand it any raise that waited for it (#54)"""
    if not decision.decision_id or not wo_id:
        return
    from .central_store import CentralStore

    own = central is None
    central = central or CentralStore()
    try:
        central.link_steward_decision(decision.decision_id, wo_id)
        _deliver_raised(central, decision.decision_id, wo_id, store)
    except Exception:  # noqa: BLE001
        log.exception("steward: could not link decision %s to %s",
                      decision.decision_id, wo_id)
    finally:
        if own:
            central.close()


def _deliver_raised(central: CentralStore, decision_id: int, wo_id: str,
                    store: ProjectStore | None) -> None:
    """The severer wordings that landed between the decision and its order's creation"""
    row = central.steward_decision(decision_id)
    held = _candidate_of(row or {})
    pending = held.get("raised_pending") or []
    if not row or not pending:
        return
    own = store is None
    store = store or _store_for(central, str(row["project"]))
    try:
        # One that still has nowhere to go (an io with no analyst yet) stays on the row
        held["raised_pending"] = [r for r in pending if not _worsened(store, wo_id, r)]
    finally:
        if own and store is not None:
            store.close()
    central.update_steward_decision(decision_id,
                                    {"candidate_json": json.dumps(held, default=str)})


def _store_for(central: CentralStore, project: str) -> ProjectStore | None:
    """The registered project's store, or None when it is gone"""
    from .project_store import ProjectStore

    spec = central.get_project(project) if project else None
    if not spec or not (Path(spec["path"]) / ".jarvis" / "jarvis.db").exists():
        return None
    return ProjectStore(Path(spec["path"]))


# ---------------------------------------------------------------- the queue (#1181)

#: Origin kinds whose `queue` the OS dispatches later on its own; the rest re-ask by themselves
DRAINABLE = ("panel", "worker_io", "remedy", "neo_rider", "io_finding", "bug_agent")
#: Origin kinds whose candidate is about the PARENT unit's code: moot once that is cancelled
PARENT_BOUND = ("panel",)
#: A queued decision older than this is not re-offered: the code it named has moved on
QUEUE_TTL_SECONDS = 14 * 86400
#: How often the daemon runs `drain` — `daemon.STEWARD_DRAIN_EVERY_TICKS` at the default poll
DRAIN_EVERY_MINUTES = 10
#: At most this many queued decisions dispatched per pass, the ledger re-read after each
DRAIN_PER_PASS = 3
#: A panel follow-up files an issue, not an order, whatever `self_dispatch` says (ops)
SELF_DISPATCH_EXEMPT = ("panel",)

#: `(decision row, candidate, decision) -> the order it created, or ''`; raising keeps it waiting
Dispatcher = Callable[[Mapping[str, Any], Candidate, Decision], str]


def candidate_wordings(row: Mapping[str, Any]) -> list[str]:
    """The other wordings folded onto a decision (spec §3.2: one patch covers them all)"""
    return [str(w) for w in _candidate_of(row).get("also_worded") or () if str(w).strip()]


def candidate_from(row: Mapping[str, Any]) -> Candidate:
    """The candidate a decision row kept, with every folded wording counted as a sighting"""
    held = _candidate_of(row)
    fields = {k: held[k] for k in ("origin_kind", "severity", "project", "subject",
                                   "parent_wo_id", "refs", "file", "payload") if k in held}
    c = Candidate(**{"origin_kind": row["origin_kind"], "severity": row["severity"],
                     "project": row["project"], "subject": row["subject"], **fields,
                     "evidence": tuple(held.get("evidence") or ())})
    # Each seat that worded the defect independently is one more sighting of it
    c.refs = max(int(c.refs or 0), 1 + len(held.get("also_worded") or ()))
    return c


def _stale(central: CentralStore, row: Mapping[str, Any], store: ProjectStore | None,
           now: float) -> str:
    """Why a queued decision may no longer be re-offered, or ''"""
    if now - float(row["ts"]) > QUEUE_TTL_SECONDS:
        return f"waited more than {QUEUE_TTL_SECONDS // 86400} days for room"
    if store is None:
        return f"project {row['project']!r} is no longer registered"
    parent = str(row["parent_wo_id"] or "")
    if row["origin_kind"] in PARENT_BOUND and parent:
        try:
            unit = (store.get_work_order(parent) if parent.startswith("wo-")
                    else store.get_feature_order(parent))
        except KeyError:
            return f"{parent} no longer exists"
        if unit.get("status") == "cancelled":
            return f"{parent} was cancelled, so its code never landed"
    held, key = _candidate_of(row), str(row["dedupe_key"] or "")
    alarm_id = str((held.get("payload") or {}).get("alarm_id") or "")
    io_id = str((held.get("payload") or {}).get("io_id") or "")
    if row["origin_kind"] == "io_finding" and io_id:
        try:
            store.get_feature_order(io_id)
        except KeyError:
            return f"{io_id} no longer exists"
    if row["origin_kind"] == "remedy" and alarm_id:
        try:
            status = store.get_alarm(alarm_id)["status"]
        except KeyError:
            return f"alarm {alarm_id} no longer exists"
        if status != "proposed":
            return f"alarm {alarm_id} was already answered (`{status}`)"
    for other in central.steward_dedupe_bucket(key) if key else ():
        if (int(other["id"]) != int(row["id"]) and (other["wo_id"] or other["applied"])
                and same_defect(str(row["subject"]), str(other["subject"] or ""),
                                file=str(held.get("file") or ""))):
            return f"superseded by steward decision #{other['id']}, already dispatched"
    return ""


#: Why an enforced `queue` with nothing kept to dispatch it from is settled as expired
UNRECOVERABLE_NOTE = ("queued before the OS kept what it needs to re-offer it "
                      "(no candidate stored); cannot be re-offered")


def settle_unrecoverable(central: CentralStore, *, now: float | None = None) -> int:
    """Expire the enforced queues of drainable kinds that kept no payload: they can never move"""
    rows = central.conn.execute(
        "SELECT id, candidate_json FROM steward_decisions WHERE outcome='queue' "
        "AND queue_state='' AND mode='enforce' AND applied=0 AND duplicate_of=0 "
        f"AND wo_id='' AND origin_kind IN ({','.join('?' * len(DRAINABLE))})",
        DRAINABLE).fetchall()
    stuck = [int(r["id"]) for r in rows if not _candidate_of(dict(r)).get("payload")]
    for decision_id in stuck:
        central.update_steward_decision(decision_id, {
            "queue_state": "expired", "queue_note": UNRECOVERABLE_NOTE,
            "queue_ts": now if now is not None else db.now()})
    return len(stuck)


def drain(catalog: Catalog, central: CentralStore, *, now: float | None = None,
          dispatchers: Mapping[str, Dispatcher] | None = None,
          limit: int = DRAIN_PER_PASS) -> dict[str, int]:
    """Re-offer the queued decisions the ledger now has room for, severest then oldest first"""
    now = now if now is not None else db.now()
    cfg = catalog.os.evolution
    out = {"waiting": 0, "dispatched": 0, "expired": settle_unrecoverable(central, now=now),
           "failed": 0}
    rows = central.steward_queue()
    if not rows:
        return out
    if cfg.mode != "enforce":
        # Shadow dispatches by today's rules, which never held these back
        out["waiting"] = len(rows)
        return out
    self_dispatch = bool(catalog.os.autonomy.self_dispatch)
    if dispatchers is None:
        from . import ops

        dispatchers = ops.steward_dispatchers()
    book: Ledger | None = None
    slow: set[str] = set()
    for row in sorted(rows, key=lambda r: (-severity_rank(r["severity"]), r["ts"], r["id"])):
        store = _store_for(central, str(row["project"]))
        try:
            why = _stale(central, row, store, now)
            if why:
                central.update_steward_decision(int(row["id"]), {
                    "queue_state": "expired", "queue_note": why, "queue_ts": now})
                out["expired"] += 1
                continue
            dispatch = dispatchers.get(str(row["origin_kind"]))
            if not self_dispatch and row["origin_kind"] not in SELF_DISPATCH_EXEMPT:
                dispatch = None  # the master switch: the OS files nothing nobody typed
            if out["dispatched"] >= limit or dispatch is None:
                out["waiting"] += 1
                continue
            if book is None:
                try:
                    book = ledger(catalog, central, now=now)
                    slow = throttled(cfg, precision(catalog, now=now))
                except Exception:  # noqa: BLE001 — a failure is never a verdict
                    log.exception("steward: ledger unreadable; nothing re-offered")
                    out["waiting"] = (len(rows) - out["expired"] - out["dispatched"]
                                      - out["failed"])
                    return out
            landed = _reoffer(central, cfg, book, slow, row, store, dispatch, now)
            out[landed] += 1
            if landed == "dispatched":
                book = None  # it spent or reserved some of the week: read the ledger again
        finally:
            if store is not None:
                store.close()
    return out


def _reoffer(central: CentralStore, cfg: EvolutionConfig, book: Ledger, slow: set[str],
             row: Mapping[str, Any], store: ProjectStore | None, dispatch: Dispatcher,
             now: float) -> str:
    """One queued decision re-decided against today's ledger: the `drain` count it lands in"""
    c = candidate_from(row)
    generation, parent_kind = lineage(store, c.parent_wo_id)
    outcome, rule, reason, severity = decide(c, cfg, book, generation=generation,
                                             parent_kind=parent_kind,
                                             throttled=c.origin_kind in slow)
    if outcome != "dispatch":
        central.update_steward_decision(int(row["id"]), {
            "queue_note": f"re-offered, still `{outcome}` ({rule}): {reason}",
            "queue_ts": now})
        return "waiting"
    budget_usd = round(max(0.0, min(cfg.order_budget_usd,
                                    cfg.weekly_usd - book.evolution_usd)), 2)
    decision = Decision(
        outcome="dispatch", rule=rule, severity=severity, generation=generation,
        reason=f"{reason}; re-offered from the queue (was `{row['rule']}`)",
        claimed_severity=str(row["claimed_severity"] or severity), mode=cfg.mode,
        allowed=True, budget_usd=budget_usd, decision_id=int(row["id"]))
    decision.stamp = {"origin_kind": c.origin_kind, "generation": generation,
                      "severity": severity, "mode": cfg.mode,
                      "decision_id": decision.decision_id}
    try:
        made = dispatch(row, c, decision)
    except Exception as e:  # noqa: BLE001 — it stays queued and is offered again next pass
        log.exception("steward: re-offering decision #%s failed", row["id"])
        central.update_steward_decision(int(row["id"]), {
            "queue_note": f"re-offer failed: {e}"[:300], "queue_ts": now})
        return "failed"
    central.update_steward_decision(int(row["id"]), {
        "outcome": "dispatch", "rule": rule, "reason": decision.reason,
        "severity": severity, "applied": 1, "budget_usd": budget_usd,
        "queue_state": "reoffered", "queue_ts": now,
        "queue_note": f"re-offered: {made}" if made else "re-offered"})
    if made.startswith(("wo-", "io-", "fo-")):
        linked(decision, made, central, store=store)
    log.info("steward re-offered queued decision #%s -> %s (%s)", row["id"],
             made or "filed", rule)
    return "dispatched"


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
            "queue": central.steward_queue_counts(),
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
    if any((d.get("queue") or {}).values()):
        lines.append(queue_line(d["queue"]))
    if d["held"]:
        lines.append("held back:")
        lines += [f"  [{r['outcome']}] {r['origin_kind']} {r['severity']}: "
                  f"{r['subject'][:70]} — {r['rule']}" for r in d["held"]]
    return "\n".join(lines)


def queue_line(counts: Mapping[str, int]) -> str:
    """How much queued self-work is waiting, in one line (`digest`, `jarvis steward decisions`)"""
    return (f"queued: {counts.get('waiting', 0)} waiting for room (re-offered every "
            f"~{DRAIN_EVERY_MINUTES} min, severest then oldest, once the ledger and the "
            f"yield line allow; dropped after {QUEUE_TTL_SECONDS // 86400} days), "
            f"{counts.get('reoffered', 0)} re-offered, {counts.get('expired', 0)} expired")


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
