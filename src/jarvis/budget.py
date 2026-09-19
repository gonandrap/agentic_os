"""A dollar ceiling on one order, enforced by the CLI on every call it makes.

Design: docs/superpowers/specs/2026-09-18-a-budget-per-order.md.

WHAT `claude --max-budget-usd` ACTUALLY DOES. None of this is documented by the CLI, so
it was measured on 2026-09-18 against the real API and every number below is from those
runs. The whole design rests on it:

* **The cap is PER INVOCATION, not per session.** A session that had already spent
  $0.0327 across its first turn resumed under `--max-budget-usd 0.01` and ran to
  completion, spending another $0.0072. Prior spend on the same session id does not
  count against the flag, and `total_cost_usd` on the resumed envelope reports that
  invocation's spend alone. THIS IS THE WHOLE REASON THIS MODULE EXISTS: the running
  total is Jarvis's to keep, and each turn is handed `budget - spent so far`.
* **It is checked BETWEEN API calls, so it overshoots.** A run capped at $0.001 spent
  $0.0471 — 47x — because the first call was already past the line when the check ran.
  The flag is a stop signal, not a hard limit; the bound is "one API call of overshoot",
  and a turn's first call against a 400k context is not cheap. `Ceiling.remaining` is
  therefore never presented as a guarantee, and `Exhaustion.spent_usd` reports what was
  actually spent rather than the cap.
* **On exhaustion the process exits 1 AND STILL WRITES A USABLE RESULT JSON.** It
  carries `type: "result"`, `is_error: true`, `subtype: "error_max_budget_usd"`,
  `terminal_reason: "budget_exhausted"` and `errors: ["Reached maximum budget ($X)"]`.
  What it does NOT carry is the `result` field, so `claude_cli.read_turn_result` sees an
  errored turn with no message — which is why `read_turn_result` falls back to the
  envelope's `errors` list, so the record says "Reached maximum budget ($5.00)" rather
  than "turn reported is_error".
  `total_cost_usd` and `modelUsage` are accurate for the invocation, so the accounting
  survives the cut and the last turn is billed like any other.
* **Partial work is preserved and the session is resumable.** The transcript is written
  in full, tool calls already made have already taken effect, and resuming the exhausted
  session under a larger cap continues the same conversation with its context intact.
  That is what makes `ops.set_work_order_budget`'s top-up-and-resume possible rather
  than merely designable.
* Subagents count against the same cap: the envelope's `subagent_stats.refused.budget`
  records the ones the CLI declined to spawn for it.

WHICH DOLLARS THE CEILING GOVERNS: the order's WHOLE BILL, exactly as `jarvis cost <id>`
reports it — the worker's own turns PLUS what Jarvis spent on that order (Neo answers,
panel seats, supervisor reviews, digests). Not the worker session alone. The user's
number has to be the number they can check, and on the four orders that motivated this
the two readings differ by ~13%. Ruled by the user through Neo on question 404.

The consequence is accepted rather than worked around: the worker's per-turn ceiling
shrinks as the panel spends, and a panel round can carry an order past its cap with no
worker turn running at all. So `exhaustion` answers from the ACCOUNTING, and the turn's
own exit code is only corroboration — see its docstring.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .claude_cli import USAGE_SCHEMA_VERSION
from .project_store import (
    FO_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    ProjectStore,
)


if TYPE_CHECKING:  # pragma: no cover
    from .catalog import ProjectSpec
    from .central_store import CentralStore


#: The work-order status an order lands in when its budget is gone. A status of its own
#: rather than a reuse, because `failed` says the work went wrong and `needs_review` says
#: a judgement is wanted, and neither is true of an order that was doing fine and ran out
#: of money. Declared in `project_store.WO_STATUSES`; named here because every reader in
#: this module wants it and none of them should re-type the string.
EXHAUSTED = "budget_exhausted"

#: ...and the feature-order one. Same word, different tuple (`FO_STATUSES`): a feature
#: order runs no session of its own, so it shares the vocabulary and not the lifecycle.
FO_EXHAUSTED = "budget_exhausted"

#: Reading the CLI's own refusal off a result envelope is `claude_cli`'s job, not this
#: module's — see `claude_cli.stopped_for_budget` and the constants beside it.


@dataclass(frozen=True)
class Spend:
    """What one order has cost so far, split into the two halves `jarvis cost` shows.

    Both come from SQL sums over numbers the CLI itself reported — `wo_turns.cost_usd`
    is `total_cost_usd` off each turn's result envelope, `agent_calls.cost_usd` is the
    same field off each OS call. Deliberately NOT `bill.for_work_order`, which walks
    Claude Code's whole transcript tree: this is asked on every dispatch and on every
    reconcile tick of every order, and it has to be two indexed queries.

    The two disagree with `jarvis cost` in one direction only, and it is the safe one:
    a turn still running has not written its envelope yet, so `worker_usd` lags by at
    most the turn in flight. A ceiling computed from a lagging total is too generous by
    one turn, never too mean — and the next turn sees the real number.
    """

    worker_usd: float = 0.0
    jarvis_usd: float = 0.0

    @property
    def total_usd(self) -> float:
        return self.worker_usd + self.jarvis_usd

    def __add__(self, other: Spend) -> Spend:
        return Spend(self.worker_usd + other.worker_usd,
                     self.jarvis_usd + other.jarvis_usd)


def spent(store: ProjectStore, central: CentralStore | None, wo_id: str) -> Spend:
    """One work order's whole bill to date. Two queries, no transcript walk."""
    row = _worker_row(store, wo_id)
    if row["stale"]:
        # A turn counted under an older reading of the result envelope is re-derived
        # before it is summed, ONCE, and written back (`ops._turn_usage`) — the same
        # lazy repair `jarvis cost` runs, done here because this is the number that
        # STOPS work: version-2 rows hold the resumed session's running total, so an
        # order would exhaust a budget it had spent a third of. Costs one JSON read per
        # stale turn and nothing at all afterwards.
        from . import ops

        ops._turn_rows(store, wo_id)
        row = _worker_row(store, wo_id)
    worker = float(row["c"] or 0.0)
    jarvis = 0.0
    if central is not None:
        jarvis = central.wo_call_cost(wo_id)
    return Spend(worker_usd=worker, jarvis_usd=jarvis)


def _worker_row(store: ProjectStore, wo_id: str) -> Any:
    """The worker half of one order's spend, and how many of its turns were counted
    under a superseded reading of the result envelope — in one query, because the
    common case is zero and must stay a single indexed scan."""
    return store.conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c, "
        "       COALESCE(SUM(CASE WHEN COALESCE("
        "           json_extract(usage_json, '$.usage_v'), 1) < ? "
        "           AND outfile IS NOT NULL AND outfile <> '' "
        "           AND state IN ('done', 'failed') THEN 1 ELSE 0 END), 0) AS stale "
        "FROM wo_turns WHERE wo_id=?",
        (USAGE_SCHEMA_VERSION, wo_id),
    ).fetchone()


def feature_spent(store: ProjectStore, central: CentralStore | None,
                  fo: dict[str, Any]) -> Spend:
    """A feature order's ROLLUP: its planner, its manager and every child.

    The same set `bill.for_feature_order` rolls up, so the two answer the same question
    about the same dollars — which is the point of a family budget the user checks with
    `jarvis cost <fo-id>`.
    """
    total = Spend()
    for child in _family(store, fo):
        total = total + spent(store, central, child["id"])
    return total


def _family(store: ProjectStore, fo: dict[str, Any]) -> list[dict[str, Any]]:
    """Every work order whose spend the feature is answerable for."""
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


# -- the family budget ----------------------------------------------------------------
#
# RESERVE ON DISPATCH (option A, ruled by the user through Neo on question 404). A child
# claims a slice of its feature's unreserved remainder at the moment it is dispatched,
# that slice becomes the child's own ceiling, and the unspent part of it returns to the
# pool when the child settles. The alternative — re-reading the feature's remainder on
# every turn — is always TRUE but hands two children dispatched in parallel the same
# dollars, and the family then overshoots by up to (N-1) turns.
#
# THE INVARIANT, which `Pool` exists to keep and `test_budget.py` pins:
#
#     the allocator never PROMISES more than `unreserved`, which is floored at zero
#
# It holds because a reservation is only ever cut from `unreserved`, which already has
# every outstanding reservation subtracted from it. Release needs no write at all: a
# settled child drops out of `held`, and its real spend is already inside `total`.
#
# NOT the stronger-sounding `spent + everything live children may still spend <= budget`,
# which cannot hold and whose failure has nothing to do with allocation: the flag is
# checked between API calls and overshoots (see the probe at the top of this file), so a
# child that ran 51c past its slice has already put the family 51c over before anything
# here is asked. State the property the allocator owns, not the one it would be nice to
# have — a comment claiming the stronger one sends the next reader hunting a bug in the
# arithmetic that is really in the CLI.


@dataclass(frozen=True)
class Pool:
    """A feature order's budget, decomposed the way the user has to see it to act.

    `unreserved` is the one Neo made a condition of option A: when a child exhausts its
    slice, the escalation states this number, so the user tops the child up rather than
    guessing why a funded feature stalled.
    """

    budget_usd: float
    spent_usd: float
    #: The unspent part of every live child's reservation — money already promised.
    held_usd: float
    #: Children not yet settled and not yet holding a reservation.
    unclaimed: int

    @property
    def pool_usd(self) -> float:
        """What is left of the money, before anyone's claim on it."""
        return max(0.0, self.budget_usd - self.spent_usd)

    @property
    def unreserved_usd(self) -> float:
        """...and what is left after the claims. What a new child may be cut from."""
        return max(0.0, self.pool_usd - self.held_usd)

    def slice_for_one_more(self) -> float:
        """An equal share of the unreserved remainder, this claimant included.

        Equal rather than proportional to anything: nothing at dispatch time knows what
        a child will need, so the only defensible split is the one that makes no claim
        to. It self-corrects — a child that settles cheaply releases the rest of its
        slice, and the next child's share is cut from a larger pool.
        """
        return self.unreserved_usd / max(1, self.unclaimed)


def pool(store: ProjectStore, central: CentralStore | None, fo: dict[str, Any],
         claimant: str | None = None) -> Pool | None:
    """A feature order's budget as it stands right now, or None if it has no budget.

    `claimant` is the child about to reserve, counted among `unclaimed` so it gets a
    share — and excluded from `held` so a re-dispatch re-cuts its slice rather than
    reserving a second one on top of the first.
    """
    budget = fo.get("budget_usd")
    if not budget:
        return None
    total = Spend()
    held = 0.0
    unclaimed = 0
    for child in _family(store, fo):
        child_spend = spent(store, central, child["id"])
        total = total + child_spend
        if child["id"] == claimant:
            unclaimed += 1
            continue
        if child["status"] in TERMINAL_STATUSES:
            # Settled: its reservation is released. Its real spend stays in `total`.
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
    """Cut this child's slice out of its feature's budget, at dispatch or afterwards.

    Returns the new reservation, or None when there is nothing to cut from — no parent,
    or a parent with no budget, in which case the child is bounded by its own budget
    alone. Idempotent by way of `pool(claimant=…)`: reserving twice re-cuts one share
    rather than stacking two.

    RE-CUTTING IS THE WAY BACK for a child that stopped on its slice. A reservation is
    the child's ceiling, and `ceiling` takes the tighter of it and the child's own
    budget, so without a re-cut a slice-exhausted child could never be raised out of
    `budget_exhausted` by any number the user typed on it — the stale slice would go on
    winning the `min`. `ops.set_work_order_budget` calls this before it decides, so
    topping up the child spends the family's CURRENT unreserved remainder on it.

    The reservation written is `already spent + the new share`, not the share alone,
    because it is a LIFETIME cap and `ceiling` measures the child's whole spend against
    it. Cutting the share alone would price the child's past twice — once inside
    `pool.spent_usd` and once inside its own ceiling — and leave the family holding back
    money it had already accounted for. At dispatch the child has spent nothing and the
    two are the same number.
    """
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


# -- the ceiling one turn is launched under -------------------------------------------


@dataclass(frozen=True)
class Ceiling:
    """The dollar cap for this work order's NEXT turn, and where it came from.

    `remaining_usd` is what goes to `claude --max-budget-usd`. It is never a guarantee —
    see the overshoot note at the top of this file — and `source` is what the escalation
    quotes so a user whose work order stopped can tell "I set $5 on this order" from
    "its feature lent it $0.40".
    """

    #: The tightest cap that applies, in dollars.
    cap_usd: float
    #: What has already gone against that cap.
    spent_usd: float
    #: Which of the caps was tightest: 'work_order' or 'feature'.
    source: str

    @property
    def remaining_usd(self) -> float:
        return self.cap_usd - self.spent_usd

    @property
    def exhausted(self) -> bool:
        """Nothing left to spend.

        `<= 0`, with no floor under it. A minimum viable turn budget was considered and
        rejected: the first API call of a turn against a large context costs real money
        (measured at ~$0.03-0.05), so a few cents of remainder does buy a wasted call —
        but any floor makes the effective cap `budget - floor`, which is not the number
        the user typed, and being able to check the number is the whole point.
        """
        return self.remaining_usd <= 0


def ceiling(store: ProjectStore, central: CentralStore | None,
            wo: dict[str, Any], spend: Spend | None = None) -> Ceiling | None:
    """What bounds this work order, or None when nothing does.

    NONE IS THE DEFAULT AND THE DEFAULT IS NO CEILING. An OS that starts refusing to
    work because of a number nobody set would be worse than the problem it solves, so an
    order with no budget and no budgeted parent behaves exactly as it did before this
    shipped: no flag on the argv, no new status reachable, nothing to escalate.

    Two caps can apply at once — the order's own and the slice its feature reserved for
    it — and the tightest wins. They are not added: the reservation is the family's
    statement about this child, the budget is the user's, and neither entitles the order
    to the other's dollars.
    """
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


# -- running out ----------------------------------------------------------------------


@dataclass(frozen=True)
class Exhaustion:
    """An order that ran out of money, and everything the user needs to answer it."""

    ceiling: Ceiling
    #: The feature's remaining unreserved budget, when this order is a child of a
    #: budgeted feature. Neo's condition on reserve-on-dispatch: without it a user whose
    #: child stopped cannot tell a stalled feature from a funded one.
    pool: Pool | None = None
    #: What the worker was in the middle of when it stopped — its last turn's prompt,
    #: squeezed to one line. Empty when the order never ran a turn.
    doing: str = ""

    @property
    def reason(self) -> str:
        """The attention line, re-derivable by `invariants.true_blockers`.

        Everything the user needs to decide is in it, because the attention list is the
        only place many of these are ever read: what the cap was, what was actually
        spent (which EXCEEDS the cap — see the overshoot note), and, for a child, what
        its feature still has to top it up with.
        """
        c = self.ceiling
        where = "its feature's slice" if c.source == "feature" else "its budget"
        line = (f"budget spent — ${c.spent_usd:.2f} of {where} of ${c.cap_usd:.2f}"
                f"; raise it with `jarvis wo budget <id> <amount>` or close it")
        if self.pool is not None:
            line += (f" (its feature has ${self.pool.unreserved_usd:.2f} unreserved "
                     f"of ${self.pool.budget_usd:.2f})")
        return line


def exhaustion(store: ProjectStore, central: CentralStore | None,
               wo: dict[str, Any]) -> Exhaustion | None:
    """Has this work order run out of money? Answered from the ACCOUNTING.

    Not from the turn's exit code, and that is deliberate rather than defensive. The
    budget governs the order's whole bill, so a panel round or a run of Neo answers can
    carry an order past its cap with no worker turn having failed — or even run — at
    all. A check that keyed on `terminal_reason` would miss every one of those, and the
    order would go on being dispatched against a budget it had already spent.

    The turn's own signal is not ignored, it is simply redundant here: a turn the CLI
    stopped for the budget has by definition spent up to the cap it was given, so the
    sum below is already at or past it. `worker_session._reap` records the signal on the
    timeline because it is evidence about THAT TURN, and this decides the order.
    """
    c = ceiling(store, central, wo)
    if c is None or not c.exhausted:
        return None
    parent_pool = None
    parent_id = wo.get("parent_id")
    if parent_id:
        try:
            parent_pool = pool(store, central, store.get_feature_order(parent_id))
        except KeyError:
            parent_pool = None
    turn = store.latest_turn(wo["id"])
    doing = " ".join((turn or {}).get("prompt", "").split())[:200]
    return Exhaustion(ceiling=c, pool=parent_pool, doing=doing)


def feature_exhaustion(store: ProjectStore, central: CentralStore | None,
                       fo: dict[str, Any]) -> Pool | None:
    """The feature's own pool when the family has spent it all, else None.

    Distinct from a child exhausting its slice: this is the whole family out of money,
    and no top-up to one child can restart it.
    """
    p = pool(store, central, fo)
    if p is None or p.pool_usd > 0:
        return None
    return p


class BudgetExhausted(RuntimeError):
    """Raised instead of launching a turn there is no money for.

    The last line of defence rather than the first. `Daemon.settle_work_order` moves a
    spent order to `budget_exhausted` on the tick after it runs out, and `NOT_RETRIED`
    and `delivery_hold` keep the other two launch paths off it — but all three of those
    are decisions taken BEFORE the launch, and between any of them and the spawn a panel
    round can land and take the order past its cap. So the transport checks too, at the
    one point every turn passes through.
    """

    def __init__(self, exhausted: Exhaustion):
        super().__init__(exhausted.reason)
        self.exhausted = exhausted


def escalate(store: ProjectStore, wo: dict[str, Any], out: Exhaustion) -> bool:
    """Park a spent work order in `budget_exhausted` and put it in front of the user.

    Returns whether anything changed, so a reconcile tick that meets the same order
    again writes no second event and sends no second notification.

    The flag is set here AND re-derived by `invariants.true_blockers` from the status —
    which is the rule this codebase states for any new attention reason, because the
    reconciler rewrites `needs_attention` from state on every tick and a flag only this
    function set would be wiped on the next one.
    """
    if wo["status"] == EXHAUSTED:
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
        # What it was in the middle of, which is the half a bare dollar figure cannot
        # give: the user is deciding whether to fund another turn, and that decision is
        # about the work, not the number.
        body=(out.reason + (f"\n\nIt was working on: {out.doing}" if out.doing else "")),
        level="warning", wo_id=wo["id"], source="reconciler",
    )
    return True


def escalate_feature(store: ProjectStore, fo: dict[str, Any], p: Pool) -> bool:
    """The same, one level up: the whole family is out of money."""
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
    """The feature order's attention line when its family budget is gone."""
    return (f"budget spent — ${p.spent_usd:.2f} of ${p.budget_usd:.2f} across this "
            f"feature's planner and children; raise it with "
            f"`jarvis fo budget <id> <amount>` or cancel it")


def parse_amount(raw: str) -> float:
    """A budget as the user types it: `5`, `5.50`, `$5.50`. Raises ValueError.

    Zero is refused rather than treated as "no budget": `--budget 0` reads like an
    instruction to spend nothing, and silently turning it into "spend anything" is the
    one misreading that costs money. `--clear` is how a budget is removed.

    `nan` AND `inf` ARE REFUSED FIRST, and they are why `float()` alone is not enough:
    both are accepted by `float()` and both survive a `<= 0` test, `nan` because every
    comparison against it is False. A `nan` budget is the worst possible outcome for a
    spend control — it FAILS OPEN while reading as enabled. `Ceiling.exhausted` is
    `nan - spent <= 0`, False for the life of the order, so the cap is never reached and
    `INV-BUDGET-OVERSPENT` never fires; meanwhile `briefing_for`'s `max(0.0, nan)` is
    0.0 (`max` keeps its first argument when the comparison is False), so every turn goes
    out under `--max-budget-usd 0.000000`, a value the probes at the top of this file
    never measured. The dashboard would show the order as budgeted throughout. An
    infinite budget is the same lie told less subtly.
    """
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
    """One way of writing a budget, so every surface writes it the same way."""
    return "—" if not value else f"${value:,.2f}"


def status_note(store: ProjectStore, central: CentralStore | None,
                wo: dict[str, Any]) -> str:
    """`$1.20 of $5.00` for a listing, or "" when the order has no ceiling."""
    c = ceiling(store, central, wo)
    if c is None:
        return ""
    return f"{format_usd(c.spent_usd)} of {format_usd(c.cap_usd)}"


def default_for(project: ProjectSpec | None) -> float | None:
    """The catalog's standing budget for a new order in this project, if it sets one.

    Resolved at CREATION and stamped onto the row, unlike `autocompact_window`, which is
    re-read from the catalog on every turn. The opposite choice on purpose: a budget is a
    contract with the user about ONE order — `jarvis wo show` has to be able to say what
    the cap is, and lowering a fleet default must not silently strand work the user
    already authorised at the old number.
    """
    if project is None:
        return None
    return getattr(project.worker, "budget_usd", None)


def feature_default_for(project: ProjectSpec | None) -> float | None:
    """The catalog's standing budget for a new FEATURE order — the family's whole bill.

    A separate setting from `default_for`, and not a multiple of it: a feature's children
    are not known when the default is written, so a per-work-order number says nothing
    useful about the family. A project that sets only one of the two gets a ceiling on
    that kind of order alone, which is the honest reading of having set only one.
    """
    if project is None:
        return None
    return getattr(project.worker, "feature_budget_usd", None)


def live_children(store: ProjectStore, fo_id: str) -> list[dict[str, Any]]:
    """The children of a feature that are still able to spend. Used by the escalation."""
    return [c for c in store.feature_children(fo_id)
            if c["status"] not in TERMINAL_STATUSES and c["status"] != EXHAUSTED]


def is_settled_feature(fo: dict[str, Any]) -> bool:
    return fo["status"] in FO_TERMINAL_STATUSES
