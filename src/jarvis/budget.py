"""A dollar ceiling on one order, enforced by the CLI on every call it makes (docs/superpowers/specs/2026-09-18-a-budget-per-order.md)"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import verdicts
from .claude_cli import USAGE_SCHEMA_VERSION
from .project_store import (
    COMPACT_TURN,
    FO_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    ProjectStore,
)


if TYPE_CHECKING:  # pragma: no cover
    from .catalog import ProjectSpec
    from .central_store import CentralStore


#: The work-order status an order lands in when its budget is gone
EXHAUSTED = "budget_exhausted"

#: ...and the feature-order one
FO_EXHAUSTED = "budget_exhausted"

#: Reading the CLI's own refusal off a result envelope is `claude_cli`'s job, not this module's


@dataclass(frozen=True)
class Spend:
    """What one order has cost so far, split into the two halves `jarvis cost` shows (#471)"""

    worker_usd: float = 0.0
    jarvis_usd: float = 0.0

    @property
    def total_usd(self) -> float:
        return self.worker_usd + self.jarvis_usd

    def __add__(self, other: Spend) -> Spend:
        return Spend(self.worker_usd + other.worker_usd,
                     self.jarvis_usd + other.jarvis_usd)


def spent(store: ProjectStore, central: CentralStore | None, wo_id: str) -> Spend:
    """One work order's whole bill to date"""
    row = _worker_row(store, wo_id)
    if row["stale"]:
        # A turn counted under an older reading of the result envelope is re-derived before it is summed
        from . import ops

        ops._turn_rows(store, wo_id)
        row = _worker_row(store, wo_id)
    worker = float(row["c"] or 0.0)
    jarvis = 0.0
    if central is not None:
        jarvis = central.wo_call_cost(wo_id)
    return Spend(worker_usd=worker, jarvis_usd=jarvis)


def _worker_row(store: ProjectStore, wo_id: str) -> Any:
    """The worker half of one order's spend"""
    # The compaction is charged on the Jarvis half (docs/superpowers/specs/2026-10-08-count-a-compaction-once.md §2)
    return store.conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind <> ? THEN cost_usd ELSE 0 END), 0) AS c, "
        "       COALESCE(SUM(CASE WHEN COALESCE("
        "           json_extract(usage_json, '$.usage_v'), 1) < ? "
        "           AND outfile IS NOT NULL AND outfile <> '' "
        "           AND state IN ('done', 'failed') THEN 1 ELSE 0 END), 0) AS stale "
        "FROM wo_turns WHERE wo_id=?",
        (COMPACT_TURN, USAGE_SCHEMA_VERSION, wo_id),
    ).fetchone()


def in_flight(store: ProjectStore, wo_id: str) -> float:
    """What the turn running RIGHT NOW has spent so far — an estimate, for display only"""
    from . import usage

    turn = store.latest_turn(wo_id)
    if turn is None or turn["state"] != "running":
        return 0.0
    try:
        wo = store.get_work_order(wo_id)
    except KeyError:
        return 0.0
    session_id = wo.get("session_id")
    if not session_id:
        return 0.0
    try:
        return usage.cost_between(session_id, turn["started_at"], time.time())
    except OSError:
        return 0.0


def feature_in_flight(store: ProjectStore, fo: dict[str, Any]) -> float:
    """The same for a feature order's whole family — its planner and every child"""
    return sum(in_flight(store, child["id"]) for child in family(store, fo))


def feature_spent(store: ProjectStore, central: CentralStore | None,
                  fo: dict[str, Any]) -> Spend:
    """A feature order's ROLLUP: its planner, its manager and every child"""
    total = Spend()
    for child in family(store, fo):
        total = total + spent(store, central, child["id"])
    return total


def family(store: ProjectStore, fo: dict[str, Any]) -> list[dict[str, Any]]:
    """Every work order whose spend the feature is answerable for"""
    orders: list[dict[str, Any]] = []
    planner_id = fo.get("plan_wo_id")
    if planner_id:
        try:
            orders.append(store.get_work_order(planner_id))
        except KeyError:
            pass
    seen = {o["id"] for o in orders}
    orders.extend(c for c in store.feature_children(fo["id"]) if c["id"] not in seen)
    return orders


# -- the family budget


@dataclass(frozen=True)
class Pool:
    """A feature order's budget, decomposed the way the user has to see it to act"""

    budget_usd: float
    spent_usd: float
    #: The unspent part of every live child's reservation — money already promised
    held_usd: float
    #: Children not yet settled and not yet holding a reservation
    unclaimed: int

    @property
    def pool_usd(self) -> float:
        """What is left of the money, before anyone's claim on it"""
        return max(0.0, self.budget_usd - self.spent_usd)

    @property
    def unreserved_usd(self) -> float:
        """...and what is left after the claims"""
        return max(0.0, self.pool_usd - self.held_usd)

    def slice_for_one_more(self) -> float:
        """An equal share of the unreserved remainder, this claimant included"""
        return self.unreserved_usd / max(1, self.unclaimed)


def pool(store: ProjectStore, central: CentralStore | None, fo: dict[str, Any],
         claimant: str | None = None) -> Pool | None:
    """A feature order's budget as it stands right now, or None if it has no budget"""
    budget = fo.get("budget_usd")
    if not budget:
        return None
    total = Spend()
    held = 0.0
    unclaimed = 0
    for child in family(store, fo):
        child_spend = spent(store, central, child["id"])
        total = total + child_spend
        if child["id"] == claimant:
            unclaimed += 1
            continue
        if child["status"] in TERMINAL_STATUSES:
            # Settled: its reservation is released
            continue
        reserved = child.get("budget_reserved_usd")
        if reserved:
            held += max(0.0, float(reserved) - child_spend.total_usd)
        else:
            unclaimed += 1
    return Pool(budget_usd=float(budget), spent_usd=total.total_usd,
                held_usd=held, unclaimed=unclaimed)


def reserve(store: ProjectStore, central: CentralStore | None,
            wo: dict[str, Any]) -> float | None:
    """Cut this child's slice out of its feature's budget, at dispatch or afterwards"""
    parent_id = wo.get("parent_id")
    if not parent_id:
        return None
    try:
        fo = store.get_feature_order(parent_id)
    except KeyError:
        return None
    p = pool(store, central, fo, claimant=wo["id"])
    if p is None:
        return None
    already = spent(store, central, wo["id"]).total_usd
    share = p.slice_for_one_more()
    store.update_work_order(wo["id"], budget_reserved_usd=already + share)
    store.add_event(wo["id"], "budget_reserved", {
        "fo_id": parent_id, "reserved_usd": round(already + share, 4),
        "share_usd": round(share, 4), "spent_usd": round(already, 4),
        "feature_budget_usd": p.budget_usd, "feature_spent_usd": round(p.spent_usd, 4),
        "unreserved_usd": round(p.unreserved_usd, 4),
    })
    return already + share


# -- the ceiling one turn is launched under


@dataclass(frozen=True)
class Ceiling:
    """The dollar cap for this work order's NEXT turn, and where it came from"""

    #: The tightest cap that applies, in dollars
    cap_usd: float
    #: What has already gone against that cap
    spent_usd: float
    #: Which of the caps was tightest: 'work_order' or 'feature'
    source: str

    @property
    def remaining_usd(self) -> float:
        return self.cap_usd - self.spent_usd

    @property
    def exhausted(self) -> bool:
        """Nothing left to spend"""
        return self.remaining_usd <= 0


def ceiling(store: ProjectStore, central: CentralStore | None,
            wo: dict[str, Any], spend: Spend | None = None) -> Ceiling | None:
    """What bounds this work order, or None when nothing does"""
    spend = spent(store, central, wo["id"]) if spend is None else spend
    caps: list[tuple[float, str]] = []
    own = wo.get("budget_usd")
    if own:
        caps.append((float(own), "work_order"))
    reserved = wo.get("budget_reserved_usd")
    if reserved:
        caps.append((float(reserved), "feature"))
    if not caps:
        return None
    cap, source = min(caps, key=lambda c: c[0])
    return Ceiling(cap_usd=cap, spent_usd=spend.total_usd, source=source)


# -- running out


@dataclass(frozen=True)
class Exhaustion:
    """An order that ran out of money, and everything the user needs to answer it"""

    ceiling: Ceiling
    #: The feature's remaining unreserved budget, when this order is a child of a budgeted feature
    pool: Pool | None = None
    #: What the worker was in the middle of when it stopped — its last turn's prompt, squeezed to one line
    doing: str = ""
    #: What this child's family is CALLED — "feature", "investigation", "improvement order"
    family: str = "feature"

    @property
    def reason(self) -> str:
        """The attention line, re-derivable by `invariants.true_blockers`"""
        c = self.ceiling
        where = f"its {self.family}'s slice" if c.source == "feature" else "its budget"
        line = (f"budget spent — ${c.spent_usd:.2f} of {where} of ${c.cap_usd:.2f}"
                f"; raise it with `jarvis wo budget <id> <amount>` or close it")
        if self.pool is not None:
            line += (f" (its {self.family} has ${self.pool.unreserved_usd:.2f} unreserved "
                     f"of ${self.pool.budget_usd:.2f})")
        return line


def exhaustion(store: ProjectStore, central: CentralStore | None,
               wo: dict[str, Any]) -> Exhaustion | None:
    """Has this work order run out of money?"""
    c = ceiling(store, central, wo)
    if c is None or not c.exhausted:
        return None
    parent_pool = None
    family = "feature"
    parent_id = wo.get("parent_id")
    if parent_id:
        try:
            parent = store.get_feature_order(parent_id)
        except KeyError:
            parent = None
        if parent is not None:
            parent_pool = pool(store, central, parent)
            # The ONE place that knows the parent's kind
            from .ops import family_prose
            family = family_prose(parent.get("kind"))[0]
    turn = store.latest_turn(wo["id"])
    doing = " ".join((turn or {}).get("prompt", "").split())[:200]
    return Exhaustion(ceiling=c, pool=parent_pool, doing=doing, family=family)


def feature_exhaustion(store: ProjectStore, central: CentralStore | None,
                       fo: dict[str, Any]) -> Pool | None:
    """The feature's own pool when the family has spent it all, else None"""
    p = pool(store, central, fo)
    if p is None or p.pool_usd > 0:
        return None
    return p


class BudgetExhausted(RuntimeError):
    """Raised instead of launching a turn there is no money for"""

    def __init__(self, exhausted: Exhaustion):
        super().__init__(exhausted.reason)
        self.exhausted = exhausted


def escalate(store: ProjectStore, wo: dict[str, Any], out: Exhaustion) -> bool:
    """Park a spent work order in `budget_exhausted` and put it in front of the user"""
    if wo["status"] == EXHAUSTED:
        return False
    if wo["status"] in TERMINAL_STATUSES:
        return False
    if verdicts.verdict_stored(store, wo):
        return False
    store.set_status(wo["id"], EXHAUSTED)
    store.flag_attention(wo["id"], out.reason)
    c = out.ceiling
    store.add_event(wo["id"], "budget_exhausted", {
        "cap_usd": round(c.cap_usd, 4), "spent_usd": round(c.spent_usd, 4),
        "source": c.source, "doing": out.doing,
        **({"feature_unreserved_usd": round(out.pool.unreserved_usd, 4),
            "feature_budget_usd": out.pool.budget_usd} if out.pool else {}),
    })
    store.add_notification(
        title=f"{wo['id']} has spent its budget",
        # What it was in the middle of, which is the half a bare dollar figure cannot give: the user is...
        body=(out.reason + (f"\n\nIt was working on: {out.doing}" if out.doing else "")),
        level="warning", wo_id=wo["id"], source="reconciler",
    )
    return True


def escalate_feature(store: ProjectStore, fo: dict[str, Any], p: Pool) -> bool:
    """The same, one level up: the whole family is out of money"""
    if fo["status"] == FO_EXHAUSTED:
        return False
    store.set_feature_status(fo["id"], FO_EXHAUSTED)
    store.flag_feature_attention(fo["id"], feature_reason(p))
    store.add_notification(
        title=f"{fo['id']} has spent its budget",
        body=feature_reason(p), level="warning", source="reconciler",
    )
    return True


def feature_reason(p: Pool) -> str:
    """The feature order's attention line when its family budget is gone"""
    return (f"budget spent — ${p.spent_usd:.2f} of ${p.budget_usd:.2f} across this "
            f"feature's planner and children; raise it with "
            f"`jarvis fo budget <id> <amount>` or cancel it")


def parse_amount(raw: str) -> float:
    """A budget as the user types it: `5`, `5.50`, `$5.50`"""
    text = raw.strip().lstrip("$").replace(",", "")
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"not a dollar amount: {raw!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"not a dollar amount: {raw!r}")
    if value <= 0:
        raise ValueError(
            f"a budget must be greater than zero (got {value}); use --clear to remove one")
    return value


def format_usd(value: float | None) -> str:
    """One way of writing a budget, so every surface writes it the same way"""
    return "—" if not value else f"${value:,.2f}"


def format_money(value: float) -> str:
    """A figure that is MONEY, where zero is a number and not an absence"""
    return f"${value:,.2f}"


def status_note(store: ProjectStore, central: CentralStore | None,
                wo: dict[str, Any]) -> str:
    """`$1.20 of $5.00` for a listing, or "" when the order has no ceiling"""
    c = ceiling(store, central, wo)
    if c is None:
        return ""
    return f"{format_money(c.spent_usd)} of {format_usd(c.cap_usd)}"


def default_for(project: ProjectSpec | None) -> float | None:
    """The catalog's standing budget for a new order in this project, if it sets one"""
    if project is None:
        return None
    return getattr(project.worker, "budget_usd", None)


def feature_default_for(project: ProjectSpec | None) -> float | None:
    """The catalog's standing budget for a new FEATURE order — the family's whole bill"""
    if project is None:
        return None
    return getattr(project.worker, "feature_budget_usd", None)


def investigation_default_for(project: ProjectSpec | None) -> float | None:
    """The standing budget for a new INVESTIGATION order — the order plus its one investigator (docs/superpowers/specs/2026-09-27-investigation-orders.md)"""
    from .catalog import DEFAULT_INVESTIGATION_BUDGET_USD

    if project is None:
        return DEFAULT_INVESTIGATION_BUDGET_USD
    explicit = getattr(project.worker, "investigation_budget_usd", None)
    if explicit is not None:
        return explicit
    return feature_default_for(project)


def live_children(store: ProjectStore, fo_id: str) -> list[dict[str, Any]]:
    """The children of a feature that are still able to spend"""
    return [c for c in store.feature_children(fo_id)
            if c["status"] not in TERMINAL_STATUSES and c["status"] != EXHAUSTED]


def is_settled_feature(fo: dict[str, Any]) -> bool:
    return fo["status"] in FO_TERMINAL_STATUSES
