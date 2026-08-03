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

from . import db
from .project_store import DEPENDENCY_DEAD_STATUSES, UNGOVERNED_ORIGINS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .project_store import ProjectStore

# Statuses where the user is the one holding the work up. A `running` worker is not
# blocked on anything the user can see. `pending` used to be excluded on the same
# reasoning — it hasn't started, so nobody is waiting on a decision — and dependency
# edges broke that: a pending work order whose dependency was cancelled will never
# start, and only the user can choose between cancelling it and cutting the edge.
# This tuple must cover every status `true_blockers` can return a blocker for, or the
# blocker is derived correctly and then never surfaced.
BLOCKED_STATUSES = ("waiting_input", "needs_review", "failed", "pending")
# Statuses where nothing can possibly be pending: the work order is over.
TERMINAL_STATUSES = ("completed", "cancelled")

#: What a work order says when the pull request it was parked behind was closed without
#: being merged. Lives here rather than at the call site because `true_blockers` is the
#: only source of attention reasons — INV-ATTENTION-REASON rewrites any flag it cannot
#: derive, so a reason raised by `Daemon.poll_pull_requests` and not repeated here would
#: silently become "finished without a completion signal" on the next reconcile tick.
PR_CLOSED_BLOCKER = "pull request closed without merging — the work was not accepted"


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


#: What a FEATURE order says when one of its children ended badly. Two reasons rather
#: than one because they ask the user for different things: a failed child is a problem
#: to diagnose, a cancelled one is a decision already taken whose consequences for the
#: rest of the feature have not been. Both are formatted with the child's id.
#:
#: Unlike the two constants around them these are NOT re-derived by `true_blockers` —
#: that function answers "what does this WORK ORDER need from me", and a feature order is
#: not a work order, so INV-ATTENTION-REASON never sees these and cannot relabel them.
#: They live here anyway so that every reason the OS puts in front of the user is written
#: in one file. If a feature-order invariant is ever added, it inherits the obligation:
#: whatever derives the flag has to be able to produce these strings.
FEATURE_CHILD_FAILED = "{id} failed — this feature cannot finish without it"
FEATURE_CHILD_CANCELLED = ("{id} was cancelled — this feature will not deliver what the "
                           "plan promised")

#: What a work order says when something it depends on can never complete. A dependency
#: that is `cancelled` or `failed` will not come back, so the dependent would sit in
#: `pending` for ever with nothing on its face to say why — the stranded row the
#: feature-order design warns about. Same rule as PR_CLOSED_BLOCKER above: an attention
#: reason must be re-derivable by `true_blockers` or the next tick relabels it.
DEAD_DEPENDENCY_BLOCKER = "blocked by a dependency that can never complete"


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
    # A privileged action Neo declined to decide on. Nobody else can open that gate, and
    # the work order cannot proceed past it.
    for approval in store.escalated_approvals(wo["id"]):
        blockers.append(
            f"gate approval needed: {approval['kind']} (request {approval['id']})"
        )
    # Two of the blockers below only exist because Jarvis dispatched the worker and
    # briefed it on the contract (`jarvis wo finish`, worktree, OPERATION.md). A session
    # the user started and injected got none of that — no JARVIS_WO_ID, no briefing — so
    # it cannot signal completion and its session ending is not a failure. Holding it to
    # the contract anyway made every such session a guaranteed attention item.
    # See Daemon.retire_ungoverned.
    governed = wo.get("origin") not in UNGOVERNED_ORIGINS
    if governed and wo["status"] == "failed":
        blockers.append("worker failed — review and retry")
    # Blocked on a prompt is real whoever started the session: nobody else can unstick it.
    # A worker waiting on a gate that is still with Neo is the exception: routing those
    # away from the user is the entire point of having a delegate, so it stays silent
    # until Neo either decides or escalates (handled above).
    if wo["status"] == "waiting_input" and not _waiting_on_neo_gate(store, wo):
        blockers.append("worker is waiting on your input")
    # A dependency that will never complete. Ordinary blocking is silent on purpose —
    # `pending — blocked by …` is a listing label, not a decision anyone owes — but a
    # dependency that is cancelled or failed cannot clear itself, so the work order will
    # wait for ever and only the user can choose between cancelling it and cutting the
    # edge (`jarvis wo unblock`). That is the difference between waiting and stranded.
    if wo["status"] == "pending" and dead_dependencies(store, wo):
        blockers.append(DEAD_DEPENDENCY_BLOCKER)
    if governed and wo["status"] == "needs_review" and not pending:
        # Two very different ways to arrive at `needs_review` without an assumption to
        # decide, and they ask the user for opposite things. A closed pull request means
        # the work WAS delivered and then refused, so there is nothing to review in the
        # session — the question is what to do about the refusal.
        if wo.get("pr_state") == "CLOSED":
            blockers.append(PR_CLOSED_BLOCKER)
        else:
            blockers.append("finished without a completion signal — review the session")
    # What the user has already looked at and dismissed stops being a blocker — but only
    # exactly that. Anything new still gets through (a pending assumption never can be
    # acknowledged away; `jarvis wo ack` refuses).
    acked = db.from_json(wo.get("acknowledged_blockers"), []) or []
    return [b for b in blockers if b not in acked]


def dead_dependencies(store: ProjectStore, wo: dict[str, Any]) -> list[dict[str, Any]]:
    """Dependencies of this work order that can never be satisfied.

    Cancelled, failed, or deleted out from under it. Anything else is merely not done
    yet, which is the normal condition of a dependency and asks nothing of anyone.
    """
    return [dep for dep in store.unfinished_dependencies(wo["id"])
            if dep["status"] in DEPENDENCY_DEAD_STATUSES or dep["status"] == "missing"]


def status_label(store: ProjectStore, wo: dict[str, Any]) -> str:
    """How this work order's status should read to a human.

    `pending` alone promises "will start as soon as a slot frees", which is a lie for a
    row that is waiting on another work order — possibly for days. Every surface that
    prints a status renders it through here, so the CLI listing, `jarvis status` and the
    dashboard cannot drift apart on the answer. (They have before: a dashboard listing
    and its own header disagreed about what needed the user because each derived it
    separately — see the FEATURED_STATUSES fix in PR 65.)
    """
    if wo["status"] != "pending":
        return wo["status"]
    blockers = store.unfinished_dependencies(wo["id"])
    if not blockers:
        return "pending"
    return f"pending — blocked by {', '.join(dep['id'] for dep in blockers)}"


def _mentions_assumptions(reason: str | None) -> bool:
    return "assumption" in (reason or "").lower()


def _waiting_on_neo_gate(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """True when this work order is parked on a privileged-action request that Neo has
    not answered yet. Not a user blocker: Neo is the one holding it."""
    return any(not a["escalated"] for a in store.pending_approvals(wo["id"]))


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


def check_adhoc_not_governed(store: ProjectStore) -> Iterator[Violation]:
    """INV-ADHOC-NOT-GOVERNED — an injected session must not be judged as a worker.

    A session the user started and handed over with `jarvis wo inject` is a *mirror*,
    not a dispatch: it never received the worker contract and has no way to call
    `jarvis wo finish`. Judging it against that contract parked it in `needs_review`
    ("worker idle without `jarvis wo finish`") the moment it ended a turn, and in
    `failed` ("worker session disappeared") the moment the user cleaned it up — one live
    fleet accumulated fifteen of these, one of which was the session the user was
    talking to. (Back then Jarvis adopted these sessions on its own; it no longer does,
    which is why `adhoc` rows still on disk get the same treatment as `injected` ones.)

    `true_blockers` stops *new* ones. This retires the records already on disk, so the
    fix reaches a running fleet on the next reconcile tick instead of waiting for the
    user to hand-clear a dashboard.

    Repairable: with no contract there is no verdict to make. The record, its timeline
    and whatever reply was captured all stay; only the demand for the user goes away.
    Anything the session genuinely left pending (an assumption it filed itself) is left
    exactly where it is.
    """
    for wo in store.list_work_orders(statuses=("failed", "needs_review"),
                                     include_hidden=True):
        if (wo["origin"] not in UNGOVERNED_ORIGINS
                or store.pending_assumptions(wo["id"])):
            continue
        store.set_status(wo["id"], "completed")
        store.clear_attention(wo["id"])
        yield Violation(
            invariant="INV-ADHOC-NOT-GOVERNED",
            wo_id=wo["id"],
            detail=f"{wo['origin']} session held to the worker contract "
                   f"({wo.get('attention_reason') or wo['status']})",
            repaired=True,
            repair="retired to completed; attention cleared",
            context={"was": wo["status"], "reason": wo.get("attention_reason")},
        )


def check_legacy_adhoc_retired(store: ProjectStore) -> Iterator[Violation]:
    """INV-ADHOC-LEGACY-RETIRED — a session Jarvis adopted on its own is let go.

    Jarvis used to adopt every Claude session running under a registered project path
    into an `origin="adhoc"` work order, and then track it. It no longer does: adoption
    is now the user's explicit act (`jarvis wo inject`), so nothing follows those rows
    any more.

    That is why leaving them alone is not the neutral option. A row parked in
    `waiting_input` is a genuine user blocker — "worker is waiting on your input" is
    true whoever started the session, so `true_blockers` does not suppress it — and with
    nothing left to refresh it, it would ask for the user forever, about a session that
    may have ended weeks ago. This closes them once, at the point of upgrade.

    Repairable: the record, its timeline, its captured replies and any assumptions all
    stay exactly as they are; only the demand for the user goes away, which is the same
    treatment `Daemon.retire_ungoverned` gives a session that ends normally. A row with
    assumptions still pending is left alone: the user owes it an answer, and that is a
    real blocker, not adoption noise.

    Idempotent: a retired row is `completed`, so a re-run finds nothing. Only `adhoc`
    rows are touched — `injected` ones were handed over deliberately and are still
    tracked.
    """
    # Adoption only ever produced these two statuses, and no path moves an adhoc row to
    # `pending`/`dispatching`. `failed`/`needs_review` are INV-ADHOC-NOT-GOVERNED's.
    for wo in store.list_work_orders(statuses=("running", "waiting_input"),
                                     include_hidden=True):
        if wo["origin"] != "adhoc" or store.pending_assumptions(wo["id"]):
            continue
        why = ("Jarvis no longer adopts sessions on its own (GitHub issue 47); "
               "this record was closed by the upgrade, not by anything the session did")
        store.set_status(wo["id"], "completed")
        store.clear_attention(wo["id"])
        store.add_event(wo["id"], "session_retired", {"why": why, "was": wo["status"]})
        yield Violation(
            invariant="INV-ADHOC-LEGACY-RETIRED",
            wo_id=wo["id"],
            detail=f"auto-adopted session still open ({wo['status']}) with nothing "
                   f"left to track it",
            repaired=True,
            repair="retired to completed; attention cleared",
            context={"was": wo["status"], "reason": wo.get("attention_reason")},
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


# -- configuration checks (catalog, not database state) ------------------------------


def check_gate_deny_conflict(spec: Any) -> Iterator[Violation]:
    """INV-GATE-DENY-CONFLICT — an enabled gate must not also be denied outright.

    A gate and a `deny` rule for the same command are not belt-and-braces, they are a
    contradiction that resolves against the gate. Claude Code evaluates deny rules
    regardless of what a PreToolUse hook returned, so the approved retry is blocked too
    — and every surface reports success along the way: the request is filed, Neo reviews
    it, Neo approves it, the timeline says `gate_opened`. Only the command silently never
    runs, which reads to the user as the worker being incompetent rather than the
    catalog being wrong.

    Not repairable: the fix is an edit to the user's catalog, which is theirs to make.
    Reported with the exact rule to delete.
    """
    from .gates import deny_conflicts

    if not spec.gates:
        return
    deny_rules = (
        (spec.settings_overrides.get("permissions") or {}).get("deny") or []
    )
    for gate_name, rule in deny_conflicts(spec.gates, deny_rules):
        yield Violation(
            invariant="INV-GATE-DENY-CONFLICT",
            detail=(
                f"project {spec.name!r} enables the `{gate_name}` gate but also denies "
                f"{rule!r} in settings_overrides. A deny rule beats a hook's allow, so "
                f"approval can never take effect: remove {rule!r} from the catalog and "
                f"let the gate mediate it."
            ),
            context={"project": spec.name, "gate": gate_name, "rule": rule},
        )


def check_catalog(catalog: Any, project: str | None = None) -> list[Violation]:
    """Config-level post-conditions over the catalog itself.

    Separate from the store checks below because the subject is different: these predict
    that the OS *cannot* do something it has been configured to do, which no amount of
    database state will reveal.
    """
    found: list[Violation] = []
    for spec in catalog.projects:
        if project and spec.name != project:
            continue
        found.extend(check_gate_deny_conflict(spec))
    return found


INVARIANTS: tuple[Callable[[ProjectStore], Iterator[Violation]], ...] = (
    check_assumptions_persisted,   # rows first: the others read pending_assumptions
    check_attention_reason_is_true,
    check_adhoc_not_governed,      # retire before the flag checks judge the leftovers
    check_legacy_adhoc_retired,    # ...and before them, let go of what nothing tracks
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
