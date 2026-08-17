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
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

from . import db, worker_session
from .project_store import (
    ACTIVE_STATUSES,
    DEPENDENCY_DEAD_STATUSES,
    UNGOVERNED_ORIGINS,
)

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
    if wo["status"] in ACTIVE_STATUSES:
        note = rate_limit_note(store, wo)
        return f"{wo['status']} — {note}" if note else wo["status"]
    if wo["status"] != "pending":
        return wo["status"]
    blockers = store.unfinished_dependencies(wo["id"])
    if blockers:
        return f"pending — blocked by {', '.join(dep['id'] for dep in blockers)}"
    # Ranked below the dependency label deliberately: a work order waiting on a sibling's
    # merge is not going to start when a slot frees, so naming the slot would be the less
    # true of the two answers. Neither raises attention — a slot always frees, so this is
    # the system working, not a decision anyone owes (the same rule that keeps a merely
    # unfinished dependency silent).
    cap = _slot_cap(store, wo)
    if cap:
        parent, limit, active = cap
        return (f"pending — waiting for a slot in {parent} "
                f"({active}/{limit} children running)")
    return "pending"


def rate_limit_note(store: ProjectStore, wo: dict[str, Any]) -> str:
    """Why this work order is not moving and when it will move again — or "" normally.

    `running` alone promises "a worker is working on this", which is a lie for a
    conversation Claude Code refused because the usage window is spent. It keeps its
    status and its slot on purpose — a refused turn changed nothing, and
    `Daemon.retry_rate_limited` relaunches it — but the user looking at a dashboard at
    midnight is owed the reason nothing is happening, and the time it will happen again.

    Every surface renders this one string (the CLI through `status_label`, the dashboard
    through `ops.os_status`) so they cannot disagree about the answer. Local wall-clock,
    not the timezone the CLI quoted: the reader is at this machine, and a time they have
    to convert is a time they will misread.
    """
    if wo["status"] not in ACTIVE_STATUSES:
        return ""
    pause = worker_session.rate_limit_pause(store, wo["id"])
    if pause is None or pause.exhausted:
        return ""
    when = time.strftime("%H:%M", time.localtime(pause.retry_at))
    return f"Claude usage limit reached, retrying by itself at {when}"


def _slot_cap(store: ProjectStore, wo: dict[str, Any]) -> tuple[str, int, int] | None:
    """`(feature id, max_parallel, active children)` when this work order's feature order
    is already running as many children as it allows, else None.

    A parentless work order — nearly all of them — is answered from the row it was handed
    and costs no query, the same shape `ops.blocked_by` uses for `depends_on`.
    """
    parent = wo.get("parent_id")
    if not parent or wo.get("kind") != "worker":
        return None
    try:
        fo = store.get_feature_order(parent)
    except KeyError:
        return None
    limit = fo.get("max_parallel")
    if not limit:
        return None
    active = store.count_active_children(parent)
    return (parent, int(limit), active) if active >= limit else None


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


def check_no_orphan_gate_requests(store: ProjectStore) -> Iterator[Violation]:
    """INV-GATE-ORPHAN — a gate request must not outlive the work order that filed it.

    A gate is a control on something about to happen. Once its work order is completed,
    cancelled or failed there is no worker left to run the command, so an approval could
    permit nothing and a denial could stop nothing — and yet the request keeps sitting in
    `jarvis gate list --pending`, and if Neo escalated it, in the user's attention list
    and inbox, asking them to authorise an action that has either already happened or
    never will.

    That is the state wo-52a6164d ended in: `completed`, the release shipped, and a
    pending gate still demanding permission to ship it. Read from the outside, the gate
    looks like it failed to hold — which is worse than a missing control, because it
    teaches the operator that the ones still standing mean nothing either.

    Repairable: close the request as superseded. It is not a verdict and authorises
    nothing (see `ProjectStore.supersede_approval`); the command string stays blocked.
    """
    for wo in store.list_work_orders(statuses=TERMINAL_STATUSES, include_hidden=True):
        for approval in store.pending_approvals(wo["id"]):
            store.supersede_approval(approval["id"], (
                f"work order is {wo['status']} — no worker is left to run this command, "
                f"so there is nothing to authorise or refuse"
            ))
            yield Violation(
                invariant="INV-GATE-ORPHAN",
                wo_id=wo["id"],
                detail=f"{wo['status']} work order still had gate request "
                       f"{approval['id']} ({approval['kind']}) pending"
                       + (" and escalated to the user" if approval["escalated"] else ""),
                repaired=True,
                repair=f"request {approval['id']} closed as superseded",
                context={"approval_id": approval["id"], "kind": approval["kind"],
                         "escalated": bool(approval["escalated"])},
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


# -- OS-level checks ($JARVIS_HOME state, not any project's database) -----------------

#: How long a pending-release marker may sit in flight before it is a fault. The whole
#: staged flow is minutes end to end (worker settles → restart → boot verification), so
#: an hour means a hand-off was dropped: the daemon never ran, the restart never landed,
#: or the boot check never fired.
RELEASE_MARKER_STALE_AFTER = 3600.0


def check_gate_canaries() -> Iterator[Violation]:
    """INV-GATE-CANARY — every command that must gate still gates.

    The gate recognisers are no longer constants; they are rows the OS writes itself when
    a reviewer dismisses a false positive (see gate_rules.py). That is the point — it is
    how the classifier stops re-asking a question already answered — and it is also the
    one change that could quietly disarm a gate. A learned exemption is canary-tested
    before it is admitted, but "tested once, at birth" is not the property worth having:
    rules accumulate, a later one can interact with an earlier one, and a user may retract
    a recogniser without seeing what it was holding up.

    So the check is re-derived from the LIVE table, and it asks the only question that
    matters about a self-modifying classifier: does a real `shipit`, a real `gh pr merge`,
    a real `systemctl restart` still get stopped? Failure here means something can now
    ship unreviewed, which is the failure this whole subsystem exists to prevent, and it
    is invisible from every other surface — nothing looks wrong when a gate simply never
    fires.

    WHERE IT RUNS, precisely, because the answer is narrower than "continuously" and the
    difference matters to anyone relying on it. This is an `OS_INVARIANTS` member, and
    `check_os()` is called by `jarvis doctor` — NOT by the daemon's reconcile tick, which
    runs the per-project `INVARIANTS` only. So this is a periodic audit the user (or a
    cron) triggers, not a live guard.

    That is sufficient because it is the third line of defence, not the first two. A rule
    is canary-tested before it is ever admitted (`gate_rules.propose_exemption`), and
    `ops.retract_gate_rule` re-runs the whole set the moment a recogniser is retracted and
    hands the failures straight back to whoever retracted it. What is left for this check
    is slow drift — rules accumulating until two of them interact — which is exactly the
    kind of fault an audit catches and a per-tick check would only find sooner.

    Not repairable automatically: which rule to retract is a judgement about what the user
    meant, and the wrong choice re-breaks the thing the exemption was fixing.
    """
    from .central_store import CentralStore
    from .gate_rules import RuleSet

    central = CentralStore()
    try:
        failures = RuleSet.load(central).check_canaries()
    finally:
        central.close()
    for f in failures:
        yield Violation(
            invariant="INV-GATE-CANARY",
            detail=(
                f"`{f['command'].splitlines()[0]}` must always trip the "
                f"`{f['kind']}` gate, and no longer does: {f['why']}. Inspect the rule "
                f"base with `jarvis gate rules` and retract what cleared it with "
                f"`jarvis gate rule-retract <id> --reason \"...\"`."
            ),
            context=f,
        )


def check_release_marker(now: float | None = None) -> list[Violation]:
    """INV-RELEASE-MARKER-STALE — a staged release must not sit unapplied for an hour.

    The marker (`$JARVIS_HOME/run/pending_release.json`) is a hand-off between the
    deploy script's `--stage` mode and the daemon (src/jarvis/release.py): `staged`
    waits for the shipping worker to settle, `restarting` waits for the new daemon to
    verify on boot. Both are meant to clear within minutes, so either state an hour on
    is a release the fleet believes is in flight and nothing is advancing.

    `failed_verification` is deliberately NOT flagged: that state already raised
    attention on the work order and an inbox warning when it was written, and it stays
    on disk until a human resolves it — reporting it hourly would say the same thing
    twice. An unreadable marker IS flagged, whatever it says: no state can clear it.

    OS-level, not per-project (it reads $JARVIS_HOME, not a project store), so it is
    not in `INVARIANTS`; `ops.run_doctor` calls it directly.

    Not repairable: which half of the hand-off died is not derivable from the file.
    """
    from . import release

    marker = release.read_marker()
    if marker is None:
        if release.marker_path().exists():
            return [Violation(
                invariant="INV-RELEASE-MARKER-STALE",
                detail=f"{release.marker_path()} exists but is not readable JSON — "
                       f"no release state can act on it",
            )]
        return []
    state = marker.get("state")
    if state not in ("staged", "restarting"):
        return []
    now = now if now is not None else time.time()
    reference = float(marker.get("restart_at") or marker.get("staged_at") or 0)
    age = now - reference
    if age <= RELEASE_MARKER_STALE_AFTER:
        return []
    return [Violation(
        invariant="INV-RELEASE-MARKER-STALE",
        wo_id=marker.get("wo_id"),
        detail=(f"pending release {marker.get('tag')} has been {state!r} for "
                f"{int(age // 60)} minutes — the daemon should have applied and "
                f"verified it within minutes (marker: {release.marker_path()})"),
        context={"state": state, "tag": marker.get("tag"), "age_seconds": int(age)},
    )]


# -- OS-level invariants --------------------------------------------------------------
#
# The project checks above are predicates over one project's store. These are predicates
# over the OS itself, so they take no store and run once per `jarvis doctor`, not once
# per project. They are never repairable: nothing here has a resolution derivable from
# state — a crashed dashboard needs a person.
#
# `check_release_marker` above is the same class of check but predates this registry and
# is still called directly by `ops.run_doctor`; it takes a `now` override the registry
# has no way to pass.


def check_ui_healthy() -> Iterator[Violation]:
    """INV-UI-HEALTHY — the OS's own dashboard must not be throwing 500s.

    The dashboard used to fail in a blind spot: uvicorn printed the traceback to the
    systemd journal and nothing else recorded it, so an HTTP 500 on the work-order page
    was invisible to `jarvis status`, to `jarvis doctor`, to the inbox and to every
    agent that can read `$JARVIS_HOME` but not `journalctl`. A user who was not already
    tailing a log had no way to learn the web UI was broken. `uilog` now keeps the
    errors next to the databases; this is the check that reads them back.

    Not repairable: reported only. The window is `uilog.ERROR_WINDOW_SECONDS`, so this
    clears itself once the dashboard has been quiet for a day.
    """
    from . import uilog

    recent, total = uilog.recent_errors()
    if not total:
        return
    hours = int(uilog.ERROR_WINDOW_SECONDS / 3600)
    yield Violation(
        invariant="INV-UI-HEALTHY",
        detail=f"the dashboard raised {total} unhandled error"
               f"{'s' if total != 1 else ''} in the last {hours}h "
               f"(latest: {recent[0].summary}) — see {uilog.ui_log_path()}",
        context={"count": total, "log": str(uilog.ui_log_path()),
                 "recent": [e.as_dict() for e in recent]},
    )


OS_INVARIANTS: tuple[Callable[[], Iterator[Violation]], ...] = (
    check_ui_healthy,
    check_gate_canaries,
)


def check_os() -> list[Violation]:
    """Run the OS-level invariants. Read-only by construction."""
    found: list[Violation] = []
    for check in OS_INVARIANTS:
        try:
            found.extend(check())
        except Exception as e:  # noqa: BLE001 — one broken check must not hide the rest
            found.append(Violation(
                invariant=getattr(check, "__name__", "unknown"),
                detail=f"invariant raised {e!r}",
            ))
    return found


INVARIANTS: tuple[Callable[[ProjectStore], Iterator[Violation]], ...] = (
    check_assumptions_persisted,   # rows first: the others read pending_assumptions
    check_no_orphan_gate_requests,  # ...and gates before the flag checks: an orphan
                                    # request is a blocker they would otherwise believe
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
                "set_status", "update_work_order", "supersede_approval")

    def __init__(self, store: ProjectStore):
        self._store = store

    def __getattr__(self, name: str) -> Any:
        if name in self._BLOCKED:
            return lambda *a, **k: None
        return getattr(self._store, name)
