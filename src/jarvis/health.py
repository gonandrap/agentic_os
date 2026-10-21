"""When the OS looks at an open instrument, and what it looks at (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .project_store import WORKER_CHILDREN

if TYPE_CHECKING:
    from .ops import Actor

SECONDS_PER_MINUTE = 60  # a unit, not a setting

#: The vocabulary `due` answers in, and `health_reviews.trigger` records (docs/superpowers/specs/2026-09-28-a-usage-limit-is-not-a-failed-sweep.md §3)
TRIGGERS = ("first-look", "changed", "stale", "re-assert", "account-window")

#: Named rather than spelled at each call site: the daemon routes on it and the supervisor records it
REASSERT = "re-assert"

#: THE BLOCKER ID FOR EACH TRANSPORT PAUSE CAUSE
_TRANSPORT_CAUSE_NAMES = {
    "usage-limit": "PAUSE_USAGE_LIMIT",
    "api-outage": "PAUSE_TRANSIENT",
    "expired-login": "PAUSE_AUTH",
}


def transport_blockers() -> dict[str, str]:
    """`_TRANSPORT_CAUSE_NAMES` with each name resolved to the `holds` pause cause"""
    from . import holds

    return {blocker_id: getattr(holds, name)
            for blocker_id, name in _TRANSPORT_CAUSE_NAMES.items()}


#: THE RE-DERIVABLE REASONS A UNIT IS STANDING STILL
BLOCKERS = ("user", "dependency", "pull-request", *_TRANSPORT_CAUSE_NAMES, "assumptions")

#: A FEATURE's answer, and it is not in `BLOCKERS` because it is not a reason of its own: it means...
CHILDREN = "children"

#: What separates the parts of a fingerprint
SEP = "|"


def observer_kinds() -> tuple[str, ...]:
    """The `wo_events` kinds the OS writes ABOUT a unit while watching it"""
    from .project_store import ALARM_EVENT_KINDS

    # `investigation_verdict` is written ONTO the subject by `ops.submit_verdict` (§7 of... (docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets-investigated.md)
    return (*ALARM_EVENT_KINDS, "attention", "investigation_verdict")


def fingerprint(pstore: Any, subject: dict[str, Any]) -> str:
    """A cheap, deterministic summary of everything about this unit that can move"""
    row = subject["row"]
    if subject["kind"] == "feature_order":
        # A feature has no session, so what moves is its children and its rounds
        children = pstore.feature_children(row["id"], kinds=WORKER_CHILDREN)
        latest = pstore.latest_validation_round(fo_id=row["id"])
        parts = [
            str(row.get("status") or ""),
            ",".join(str(c.get("status") or "") for c in children),
            str(latest["round"]) if latest else "-",
            str(len(pstore.superseded_children(row["id"]))),
        ]
    else:
        turn = pstore.latest_turn(row["id"])
        parts = [
            str(row.get("status") or ""),
            str(turn["seq"]) if turn else "-",
            str(turn["state"]) if turn else "-",
            str(pstore.count_events(row["id"], exclude=observer_kinds())),
            str(len(pstore.pending_assumptions(row["id"]))),
            str(len(pstore.queued_messages(row["id"]))),
        ]
    return SEP.join(parts)


def blocker(pstore: Any, subject: dict[str, Any], cfg: Any) -> str | None:
    """The id of the first re-derivable reason this unit is standing still, or None"""
    if subject["kind"] == "feature_order":
        return _children_blocker(pstore, subject["row"], cfg)
    return _work_order_blocker(pstore, subject["row"], cfg)


def actor(pstore: Any, subject: dict[str, Any]) -> Actor | None:
    """The daemon pass that will next touch this unit, or None (docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md)"""
    from . import db, ops
    from .project_store import OPEN_STATUSES

    if subject["kind"] == "feature_order":
        return None
    row = subject["row"]
    # ON `project_store.OPEN_STATUSES` AND NOT `ops.SCHEDULED_ACTORS`
    if row.get("status") not in OPEN_STATUSES:
        return None
    # ON `db.now()`, the OS's clock, for the reason `_work_order_blocker` gives below
    return ops.scheduled_actor(pstore, row, now=db.now())


def _work_order_blocker(pstore: Any, row: dict[str, Any], cfg: Any) -> str | None:
    """FIRST MATCH WINS, in `BLOCKERS`' order"""
    from . import db, holds, invariants, ops

    counts = tuple(getattr(cfg, "health_reassert_blockers", BLOCKERS) or ())
    causes = transport_blockers()
    for blocker_id in BLOCKERS:
        if blocker_id not in counts:
            continue
        # ON `db.now()`, THE OS'S CLOCK, which is what the sweep itself measures every window against...
        if blocker_id == "user" and invariants.true_blockers(pstore, row, now=db.now()):
            return blocker_id
        # NOT `invariants.dead_dependencies` (docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md)
        if blocker_id == "dependency" and (ops.blocked_by(pstore, row)
                                           or ops.open_preconditions(pstore, row)):
            return blocker_id
        # A PURE ROW READ, NO NETWORK: `github.pr_view` is the daemon's poll, never a sweep's
        if (blocker_id == "pull-request" and row.get("status") == "waiting_pr_merge"
                and row.get("pr_url")):
            return blocker_id
        # ONE CAUSE PER ID, never the whole of `holds.TRANSPORT`: a catalog narrowed to `usage-limit` has...
        if blocker_id in causes and any(
                h.open and h.cause == causes[blocker_id]
                for h in holds.held(pstore, row["id"], now=db.now())):
            return blocker_id
        # LAST, and reachable only where `user` declined it: `true_blockers` suppresses the assumptions line...
        if blocker_id == "assumptions" and pstore.pending_assumptions(row["id"]):
            return blocker_id
    return None


def _children_blocker(pstore: Any, row: dict[str, Any], cfg: Any) -> str | None:
    """A FEATURE's stillness is its children's, and nothing else explains it (Neo q1220)"""
    from .project_store import TERMINAL_STATUSES

    open_children = [c for c in pstore.feature_children(row["id"], kinds=WORKER_CHILDREN)
                     if c.get("status") not in TERMINAL_STATUSES]
    if not open_children or any(c.get("status") == "running" for c in open_children):
        return None
    if all(_work_order_blocker(pstore, c, cfg) for c in open_children):
        return CHILDREN
    return None


def due(review: dict[str, Any] | None, current: str, cfg: Any, now: float,
        created: float, last_attempt: float | None = None,
        blocker: str | None = None) -> str | None:
    """Which trigger says to look at this unit now, or None to leave it alone"""
    interval = cfg.health_min_interval_minutes * SECONDS_PER_MINUTE
    stale = cfg.health_stale_minutes * SECONDS_PER_MINUTE
    # THE FLOOR IS ON ATTEMPTS, NOT ON JUDGEMENTS (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)
    if last_attempt is not None and now - last_attempt < interval:
        return None
    if review is None:
        # A unit created moments ago has nothing to say yet
        return "first-look" if now - created >= interval else None
    since = now - float(review.get("ts") or 0.0)
    if str(review.get("fingerprint") or "") != current:
        return "changed" if since >= interval else None
    if since < stale:
        return None
    # THE FREE ROW, AND THE GATE IS NARROW ON PURPOSE
    if str(review.get("outcome")) == "findings" and blocker is not None:
        return REASSERT
    return "stale"
