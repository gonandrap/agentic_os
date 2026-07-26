"""Post-conditions — the OS checking that its own state still means what it says.

Every action the OS takes has an intended effect. An event log does not prove the effect
survived: `attention {"reason": "assumptions pending review"}` records that we set a
reason, not that the reason is still true ninety seconds later when an unrelated hook
overwrites it. That exact sequence shipped two work orders to the dashboard labelled
"waiting for your input" when what they actually needed was an assumption review — every
component behaved correctly and the resulting state was a lie.

So invariants here are **steady-state predicates**, not write-time assertions. They are
re-evaluated on every reconcile tick against the database as it currently is, which is
the only way to catch state that was correct when written and went wrong afterwards.

Three rules for this module:

1. **No LLM, ever.** These are cheap SQL-level predicates. Determinism is the point:
   the checker must be more trustworthy than the thing it checks.
2. **Repair only what is unambiguous.** A violation with exactly one correct resolution
   derivable from state (a stale reason, a phantom flag) is repaired automatically and
   the repair is recorded. Anything else is reported and left alone.
3. **Report once, not every tick.** Callers dedupe on `Violation.key`; a standing
   violation must not spam the timeline or the inbox.

Adding an invariant: write a `_check_*` generator yielding `Violation`s and register it
in `INVARIANTS`. Give it a stable id — ids appear in work order timelines and in
`jarvis doctor` output, so renaming one rewrites history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .project_store import ProjectStore

# Statuses where the user is the one holding the work up. A `running` worker is not
# blocked on anything the user can see, and a `pending` one hasn't started.
BLOCKED_STATUSES = ("waiting_input", "needs_review", "failed")
# Statuses where nothing can possibly be pending: the work order is over.
TERMINAL_STATUSES = ("completed", "cancelled")


@dataclass
class Violation:
    """One invariant found false, with whatever the checker did about it."""

    invariant: str
    detail: str
    wo_id: str | None = None
    repaired: bool = False
    repair: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str | None]:
        """Identity for dedupe: the same invariant failing on the same work order is
        one standing problem, however many ticks it survives."""
        return (self.invariant, self.wo_id)

    def __str__(self) -> str:
        where = f"{self.wo_id}: " if self.wo_id else ""
        fixed = f" [repaired: {self.repair}]" if self.repaired else ""
        return f"{self.invariant} — {where}{self.detail}{fixed}"


# -- the derivation everything else is checked against ------------------------------


def true_blockers(store: ProjectStore, wo: dict[str, Any]) -> list[str]:
    """The reasons this work order genuinely needs the user, derived from state alone.

    Ordered most-actionable first, so `[0]` is the canonical attention reason. This is
    the single source of truth for "what does this work order want from me" — the UI
    banner, the attention list and the invariants below all have to agree with it, and
    any of them disagreeing is a bug in that surface, not here.
    """
    blockers: list[str] = []
    pending = store.pending_assumptions(wo["id"])
    if pending:
        n = len(pending)
        blockers.append(f"{n} assumption{'s' if n != 1 else ''} pending your review")
    if wo["status"] == "failed":
        blockers.append("worker failed — review and retry")
    if wo["status"] == "waiting_input":
        blockers.append("worker is waiting on your input")
    if wo["status"] == "needs_review" and not pending:
        blockers.append("finished without a completion signal — review the session")
    return blockers


def _mentions_assumptions(reason: str | None) -> bool:
    return "assumption" in (reason or "").lower()


# -- invariants ---------------------------------------------------------------------


def check_attention_reason_is_true(store: ProjectStore) -> Iterator[Violation]:
    """INV-ATTENTION-REASON — a flagged work order's reason must name the real blocker.

    The observed failure: a work order settles into `needs_review` with
    "assumptions pending review", then Claude Code's routine idle Notification fires a
    minute later and the hook stamps "Claude is waiting for your input" over it. The
    user is sent looking for a question that does not exist while the actual action —
    approve or reject the assumptions — sits unlabelled below it.

    Repairable: the correct reason is derivable from state.
    """
    for wo in store.list_work_orders(include_hidden=True):
        if not wo["needs_attention"]:
            continue
        blockers = true_blockers(store, wo)
        if not blockers:
            continue
        reason = wo.get("attention_reason") or ""
        # Only assumptions are enforced strictly. For other blockers a hook-supplied
        # reason ("needs permission to run X") is more specific than anything we can
        # derive, and clobbering it would repeat the very bug this invariant exists for.
        if not _mentions_assumptions(blockers[0]):
            continue
        if _mentions_assumptions(reason):
            continue
        store.flag_attention(wo["id"], blockers[0])
        yield Violation(
            invariant="INV-ATTENTION-REASON",
            wo_id=wo["id"],
            detail=f"attention reason {reason!r} does not name the real blocker",
            repaired=True,
            repair=f"reason set to {blockers[0]!r}",
            context={"was": reason, "now": blockers[0]},
        )


def check_no_phantom_attention(store: ProjectStore) -> Iterator[Violation]:
    """INV-ATTENTION-PHANTOM — a work order with nothing pending must not ask for you.

    Covers the "I acked it and it is still in my face" case: once a work order is
    completed or cancelled there is nothing the user can act on, so a lingering flag is
    pure noise on the dashboard and in the attention list.

    Repairable: clear the flag.
    """
    for wo in store.list_work_orders(statuses=TERMINAL_STATUSES, include_hidden=True):
        if not wo["needs_attention"]:
            continue
        store.clear_attention(wo["id"])
        yield Violation(
            invariant="INV-ATTENTION-PHANTOM",
            wo_id=wo["id"],
            detail=f"{wo['status']} work order still flagged "
                   f"({wo.get('attention_reason') or 'no reason'})",
            repaired=True,
            repair="attention cleared",
        )


def check_blocked_work_is_surfaced(store: ProjectStore) -> Iterator[Violation]:
    """INV-ATTENTION-MISSING — work that needs the user must actually say so.

    The mirror of the two above, and the more dangerous direction: a work order stuck
    with pending assumptions but no attention flag never appears in `jarvis status`, the
    attention list or the dashboard strip. It is invisibly stalled, which is how a fleet
    quietly stops moving.

    Repairable: raise the flag with the derived reason.
    """
    for wo in store.list_work_orders(statuses=BLOCKED_STATUSES, include_hidden=True):
        if wo["needs_attention"]:
            continue
        blockers = true_blockers(store, wo)
        if not blockers:
            continue
        store.flag_attention(wo["id"], blockers[0])
        yield Violation(
            invariant="INV-ATTENTION-MISSING",
            wo_id=wo["id"],
            detail=f"{wo['status']} work order needs the user ({blockers[0]}) "
                   f"but was not flagged",
            repaired=True,
            repair=f"flagged: {blockers[0]!r}",
        )


def check_assumptions_persisted(store: ProjectStore) -> Iterator[Violation]:
    """INV-ASSUMPTION-PERSISTED — every recorded assumption must exist as a row.

    The timeline saying "Assumption recorded" is a claim about a write. This checks the
    write: an `assumption` event whose content has no matching row is an assumption the
    worker believes it filed, the user will never be asked about, and no other surface
    can show — the review queue reads rows, not events.

    Repairable: the event payload carries the full content, so the row is reconstructed
    from it. Matching is by exact content, so a re-run never duplicates.
    """
    for wo in store.list_work_orders(include_hidden=True):
        events = [e for e in store.list_events(wo["id"], limit=1000)
                  if e.get("kind") == "assumption"]
        if not events:
            continue
        rows = store.all_assumptions(wo["id"])
        stored = {r["content"] for r in rows}
        for e in events:
            payload = e.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            content = (payload or {}).get("content")
            if not content or content in stored:
                continue
            store.add_assumption(wo["id"], content)
            stored.add(content)
            yield Violation(
                invariant="INV-ASSUMPTION-PERSISTED",
                wo_id=wo["id"],
                detail="assumption recorded in the timeline had no row in the "
                       "review queue",
                repaired=True,
                repair="row reconstructed from the event payload",
                context={"content": content[:200]},
            )


def check_attention_has_reason(store: ProjectStore) -> Iterator[Violation]:
    """INV-ATTENTION-BLANK — a flagged work order must say what it wants.

    "Needs you" with an empty reason is indistinguishable from noise, and it is the
    fastest way to teach an operator to ignore the attention strip.

    Repairable when a blocker is derivable; reported otherwise.
    """
    for wo in store.list_work_orders(include_hidden=True):
        if not wo["needs_attention"] or (wo.get("attention_reason") or "").strip():
            continue
        blockers = true_blockers(store, wo)
        if blockers:
            store.flag_attention(wo["id"], blockers[0])
            yield Violation(
                invariant="INV-ATTENTION-BLANK", wo_id=wo["id"],
                detail="flagged with no reason",
                repaired=True, repair=f"reason set to {blockers[0]!r}",
            )
        else:
            yield Violation(
                invariant="INV-ATTENTION-BLANK", wo_id=wo["id"],
                detail="flagged with no reason and no derivable blocker",
            )


INVARIANTS: tuple[Callable[[ProjectStore], Iterator[Violation]], ...] = (
    check_assumptions_persisted,   # rows first: the others read pending_assumptions
    check_attention_reason_is_true,
    check_no_phantom_attention,
    check_blocked_work_is_surfaced,
    check_attention_has_reason,
)


def check_project(store: ProjectStore, repair: bool = True) -> list[Violation]:
    """Run every invariant over one project. Returns the violations found.

    With `repair=False` the checks run against a read-only view of the store, so
    reporting never mutates state — that is what `jarvis doctor` uses before the user
    has decided whether to let it touch anything.
    """
    target = store if repair else _ReadOnly(store)
    found: list[Violation] = []
    for check in INVARIANTS:
        try:
            found.extend(check(target))  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001 — one broken check must not hide the rest
            found.append(Violation(
                invariant=getattr(check, "__name__", "unknown"),
                detail=f"invariant raised {e!r}",
            ))
    return found


class _ReadOnly:
    """Store proxy that swallows repairs, so `check_project(repair=False)` is a pure read.

    Only the handful of mutators the invariants above call are intercepted; everything
    else passes straight through. A `Violation` produced against this proxy still claims
    `repaired=True` — it describes the repair that *would* be applied — so callers that
    ask for no repair must present it as proposed, not done (`jarvis doctor` does).
    """

    _BLOCKED = ("flag_attention", "clear_attention", "add_assumption", "add_event",
                "set_status", "update_work_order")

    def __init__(self, store: ProjectStore):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        if name in self._BLOCKED:
            return lambda *a, **k: None
        return getattr(self._store, name)
