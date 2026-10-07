# A settled feature's live children must have a manager, or a hold

Work order wo-a04f9d70. GitHub issue 881 and its 2026-10-07 follow-up comment. Design
decided by Neo question 1393 — the three parts below are not re-opened here.

## The problem

Three defects on one incident, in order of causation.

### 1. A delivered child is settled as a failure

fo-ac00376e went `failed` because its child wo-604b5b99's turn died in a stampede
cleanup. The child **had delivered**: `pr_url` pointing at PR #839, open, and three
assumptions on record.

`src/jarvis/daemon.py:4956-4994`, `Daemon.settle_work_order`, the
`turn["state"] == "failed"` branch. Once the pause is exhausted it writes, with no test
of what the order produced:

```python
if wo["status"] != "failed":
    store.set_status(wo["id"], "failed")
    store.flag_attention(wo["id"], "worker turn failed — review and retry")
```

Settlement is derived from the TURN's fate, never from the ORDER's delivery. That is the
root cause of the whole incident, and it is one branch wide: every other arm of that
method reads `fresh["result_summary"]` and `pr_url` first (`daemon.py:5013` onwards).

`failed` is then load-bearing twice over: `invariants.dead_feature_children`
(`src/jarvis/invariants.py:537`) counts the child as dead, and `Daemon.settle_features`
(`daemon.py:1362-1371`) fails the feature on it.

### 2. Failing the feature killed its manager, and the siblings kept running headless

`settle_features` calls `_close_feature_manager` (`daemon.py:1603`, called at
`daemon.py:1370`), which completes manager wo-b57da97e. Siblings wo-3db50904 and
wo-0cb6dc6b stayed live with no addressee for the `manager` role.

wo-3db50904's `deferral_request`, envelope 155, went `undeliverable`
(`bus._unfilled`, `src/jarvis/bus.py:428-441`). INV-ENVELOPE-LOST
(`invariants.check_no_lost_feedback`, `invariants.py:2948`) is report-only by
construction — "Reported, never repaired. There is nothing to derive here" — so the
lost message was described and nothing was done about it. The live children themselves
raised nothing: `true_blockers` (`invariants.py:856`) has no line about a parent
feature, so `invariants.check_blocked_work_is_surfaced` had nothing to flag.

### 3. A reopened feature has no manager, and the handoff is silent about it

The feature was later reopened to `executing`; the manager stayed `completed`.

- `ops.resume_feature_order` (`src/jarvis/ops.py:9724-9784`) supersedes dead children,
  sets `executing`, clears the flag, files `--fix`. It never looks at the manager.
- INV-FEATURE-FALSE-FAILURE's repair (`invariants.check_feature_failures_are_real`,
  `invariants.py:3070-3111`) does `set_feature_status(fo_id, "executing")` plus
  `clear_feature_attention`. Same omission.
- `Daemon._manager_handoff` (`daemon.py:1544-1546`) then returns in silence:

```python
manager = store.manager_work_order(fo_id)
if not manager or manager["status"] != "idle":
    return
```

So with a judged round and every live child `completed`, neither the nudge
(`bus.ChildrenLanded`) nor FEATURE_MANAGER_STALLED/FEATURE_MANAGER_SILENT
(`daemon.py:318-323`) happens. The feature sat in `executing` for 5.4 days with no
signal of any kind.

Second root cause, one level up: manager liveness is WRITTEN ONCE at a transition
(`_close_feature_manager`) and never derived, so every path that reverses that
transition has to remember to undo it, and none of the three does.

### 4. The lost-envelope sentence leads with the wrong order

Today, `invariants.py:2990-2992`:

> envelope 155 (deferral_request to role manager) about wo-3db50904 reached nobody: …

The user read wo-3db50904 as the completed order. The settled fact — "its manager
wo-b57da97e is completed" — is what makes the sentence make sense, and it arrives last
or not at all.

## The fix

Four changes. (a) stops the misclassification, (b) makes the stranded-child case say so
without reopening anything, (c) makes manager liveness derived at one site, (d) fixes the
sentence.

### (a) A turn that dies after delivery settles to `needs_review`, not `failed`

**Where.** `Daemon.settle_work_order`, `daemon.py:4973`, inside the existing
`if wo["status"] != "failed":` block of the `turn["state"] == "failed"` branch — after
the auth park and the `pause and not pause.exhausted` return, so retries are untouched.

**Predicate.** New module-level helper in `invariants.py`, beside
`dead_feature_children` (it is the function whose input this changes):

```python
def has_delivered(store: ProjectStore, wo: dict[str, Any]) -> bool:
```

True when `wo.get("pr_url")` is set, or `store.all_assumptions(wo["id"])` is non-empty.
Not `result_summary`: a summary can be written by a worker that then kept working, while
a pull request and an assumption are both artefacts the order cannot take back. In
`invariants.py` rather than `ops.py` because the blocker below reads the same notion and
the two must not drift; the daemon already holds the module as `invariants_mod`.

**Write.** When `has_delivered` is true:

- `store.set_status(wo["id"], "needs_review")`;
- `store.add_event(wo["id"], TURN_DIED_AFTER_DELIVERY_EVENT, {...})` carrying
  `pause.attempts`/`pause.reason`/`pause.message` when there was a pause and
  `turn.get("error")`, so the record never reads as a clean delivery. New constant
  `TURN_DIED_AFTER_DELIVERY_EVENT = "turn_died_after_delivery"` in `invariants.py` next
  to the blocker, because the writer is the daemon and the reader is the derivation;
- `store.flag_attention(wo["id"], invariants_mod.true_blockers(store, fresh_row)[0])`,
  not a literal. The existing `turn_retries_exhausted` event and the
  `add_notification` call stay on both paths — the retries WERE spent, and the user
  still wants to know the session died.

The `failed` path is unchanged for an order that delivered nothing.

**The attention reason, worked out.** Two cases:

1. Assumptions pending — wo-604b5b99's case. `true_blockers` already derives
   `"3 assumptions pending your review"` (`invariants.py:884`), and the `auto_review_at`
   suppression at `invariants.py:872-879` does not apply, because
   `neo_reviews_later` is False in `needs_review` and `_os_is_confirming` is false for an
   assumption no confirmation pass ever ran on.
2. `pr_url` recorded and nothing pending — **nothing correct is derivable today.** The
   `governed and status == "needs_review"` ladder (`invariants.py:1049-1078`) falls
   through `pr_state == "CLOSED"` (not closed), `validation_escalated` (no round),
   `work_unlanded_open` (the work IS landed, on a branch with an open PR) and lands on
   `IDLE_NO_FINISH_BLOCKER` — "the worker stopped mid-task without `jarvis wo finish`",
   which is false twice: it finished, and the turn did not stop, it died.

   So add a line. New module-level constant in `invariants.py`, immediately after
   `IDLE_NO_FINISH_BLOCKER` (`invariants.py:419`), under that constant's documented
   obligation — the daemon raises it and this module re-derives it, or
   INV-ATTENTION-REASON relabels it on the next tick:

   ```python
   TURN_DIED_AFTER_DELIVERY_BLOCKER = (
       "its worker's session died after the work was delivered — the delivery is on "
       "record; read it, then `jarvis wo review` or `jarvis wo done`")
   ```

   No elapsed time in it (PARKED_BLOCKER's rule: `ack_attention` stores the string
   verbatim).

   **Ranked by splitting case 4 of the ladder, nothing else moved.** The final
   `elif not pending:` arm becomes: this blocker when
   `store.events_of_kind(wo["id"], TURN_DIED_AFTER_DELIVERY_EVENT)` is non-empty, else
   `IDLE_NO_FINISH_BLOCKER`. That is the only arm whose sentence is wrong for this state,
   and splitting it cannot reorder the three above it — the proof the ladder's comments
   carry stays valid. The read is already behind `status == "needs_review"` and `not
   pending`, so no other work order pays for the query.

**Why `needs_review` and not a retry.** The order has delivered; there is nothing to
re-run. `needs_review` is the status that means "a person decides what happens to
delivered work", `dead_feature_children` never sees it, and the feature stays
`executing` — defect 2 and 3 stop being reachable from this cause at all.

### (b) A live child under a settled feature carries a derived hold — never a reopen

Neo's ruling, verbatim: "HOLD at the fork. No reopen, not even guarded: derived,
self-clearing state is the house style, and a reopen that races settle_features buys
nothing a hold plus a revived manager doesn't."

A reopen at the fork would flap. `Daemon.settle_features` re-fails the feature on the
next tick off the same dead child, and `dead_feature_children` is deliberately ONE
function shared by the settler and INV-FEATURE-FALSE-FAILURE precisely so the two cannot
disagree (`invariants.py:539-543`). Putting a third writer on the other side of that
predicate is the infinite loop its docstring names.

**Where.** `invariants.true_blockers`. New constants at module level, with the other
blockers:

```python
#: What a live child says when the feature it belongs to has settled under it.
SETTLED_FEATURE_BLOCKER = ("its feature order {fo_id} is {status} — nothing will "
                           "coordinate this work order until the feature is live "
                           "again; {remedy}")
SETTLED_FEATURE_REMEDY = {
    "failed": "`jarvis fo resume {fo_id}`",
    "cancelled": "`jarvis wo cancel {wo_id}` if this work order is not wanted either",
    "completed": "`jarvis wo done {wo_id}`, or `jarvis wo cancel {wo_id}`",
}
```

One template, a remedy per settled status, because the way out genuinely differs: `fo
resume` refuses anything but `failed` (`ops.py:9759-9763`), so naming it under a
cancelled feature would send the user at a command that errors.

**Derivation, following the neighbouring conventions exactly.** Gated on status so no
other work order pays for the query — the same shape as the `release_red_park_open` and
PR_REPAIR arms:

```python
if wo["status"] in OPEN_STATUSES and wo.get("parent_id"):
    parent = store.get_feature_order(wo["parent_id"])   # KeyError -> treated as gone
    if parent["status"] in FO_TERMINAL_STATUSES:
        blockers.append(...)
```

`FO_TERMINAL_STATUSES` is `("completed", "failed", "cancelled")`
(`project_store.py:453`). Placed after the `pending`/DEAD_DEPENDENCY arm and before the
`needs_review` ladder: it is a fact about the work order's context, not about its own
delivery, and it must not displace an assumption the user owes — the assumptions line is
appended above it anyway. No elapsed time in the string; `kind='manager'` orders are
skipped (a manager has `parent_id` set to its feature and `_close_feature_manager`
settling it is correct, not a hold — gate on `wo.get("kind") != "manager"`).

**It reaches the user with no new writer.** `check_blocked_work_is_surfaced`
(INV-ATTENTION-MISSING, `invariants.py:2296`) runs over `BLOCKED_STATUSES`, which covers
every open status except `validating`, and raises `blockers[0]`. So the hold appears
within one reconcile tick and — this is the point — **clears itself** the moment the
feature is reopened, because it is derived and INV-ATTENTION-PHANTOM/INV-ATTENTION-REASON
re-derive against the same function. No tick writes a flag the next tick cannot
re-derive (kn-089de524). A child in `validating` carries no hold, deliberately: the round
machine owns it and nobody is waiting on the manager.

**INV-ENVELOPE-LOST stays report-only, and that is the decision.** The option was a
`repaired=True` repair raising a flag. Rejected: the hold in `true_blockers` IS the
remedy, so there is nothing left for the invariant to write, and anything it wrote would
be a flag it cannot re-derive next tick — the exact failure mode the check's own
"nothing to derive here" paragraph is guarding. `bus._unfilled` already flags the FEATURE
(`bus.py:436-440`) when it is still open. What changes in this check is its sentence
only, in (d), including naming the hold the child now carries.

### (c) Manager liveness becomes derived, at one site

**The helper.**

```python
# src/jarvis/ops.py
def revive_feature_manager(store: ProjectStore, fo_id: str, why: str) -> dict | None:
```

Given a feature order that is `executing` and a manager work order that is not in
`OPEN_STATUSES`, it:

1. `store.set_status(manager["id"], "idle")`;
2. `store.clear_attention(manager["id"])`;
3. `store.add_event(manager["id"], "manager_revived", {"feature_order": fo_id,
   "was": manager["status"], "why": why})` — the exact mirror of
   `_close_feature_manager`'s `feature_settled` event, so the manager's timeline reads as
   a pair;
4. returns the manager row, or `None` when there is no manager row at all, or when the
   manager is already open, or when the feature is not `executing` — the callers below
   all need to tell those apart.

**`idle`, not `waiting_input`.** That is the manager's designed steady state since issue
#264 (`project_store.py:22-25`; `_close_feature_manager`'s docstring describes the
`waiting_input` version as the old shape), it is what `_manager_handoff` tests for
(`daemon.py:1545`), and `invariants.true_blockers` deliberately derives nothing from
`idle` except MESSAGE_STUCK_BLOCKER (`invariants.py:86-92`) — so a revived manager asks
the user for nothing.

**Placement, and why there.** `ops.py`, not `daemon.py` and not `invariants.py`. `ops`
is the write-API layer and already owns `resume_feature_order`; `daemon` imports `ops`
inside the methods that need it (`daemon.py:1538`, `daemon.py:5000`), and `invariants`
already reaches into `ops` with a function-local import for exactly this reason
(`from .ops import auto_review_at`, `invariants.py:880`). Putting it in `daemon` would
make `ops` import the daemon; putting it in `invariants` would put a status write in the
module whose contract is derivation.

**Three callers.**

1. `ops.resume_feature_order`, immediately after `set_feature_status(fo_id,
   "executing")` / `clear_feature_attention` (`ops.py:9767-9768`) and before the `--fix`
   child is filed, so a crash between them leaves a live manager rather than a child with
   no addressee.
2. INV-FEATURE-FALSE-FAILURE's repair, `invariants.check_feature_failures_are_real`,
   after its own `set_feature_status`/`clear_feature_attention` pair
   (`invariants.py:3099-3100`). The `repair=` string gains "manager revived" when the
   helper returned a row, so `jarvis doctor` says what it did.
3. `Daemon._manager_handoff`, replacing the silent return at `daemon.py:1545-1546`:

   ```python
   manager = store.manager_work_order(fo_id)
   if manager is None:
       # flag the feature naming the missing manager; return
   if manager["status"] not in ("idle",) and ops.revive_feature_manager(...) is None:
       return   # genuinely busy: running, or mid-turn — not this branch's business
   ```

   Precisely: a manager that is `running`, or holds a queued message, or has a `queued`
   manager envelope, still returns early exactly as today — it has not finished reacting.
   A manager that is SETTLED is revived and the branch proceeds to its existing nudge or
   flag. A feature with no manager row at all (features created before the manager
   existed; `ProjectStore.create_feature_order`, `project_store.py:3119-3172`, is the
   only creator) gets `store.flag_feature_attention(fo_id, ...)` naming the missing
   manager, under a new constant beside FEATURE_MANAGER_STALLED/SILENT
   (`daemon.py:318-323`) — `FEATURE_MANAGER_MISSING`, keyed into the same
   `FEATURE_HANDOFF_EVENT` dedupe (`action: "flagged"`) so it is raised once and never
   re-raised over an unchanged situation. **Never a silent return.**

**INV-MANAGER-SLOTS cannot break.** `check_manager_slots` (`invariants.py:3564`) compares
`store.count_active()` against the count of `SLOT_STATUSES` rows with
`kind != 'manager'`. `SLOT_STATUSES` is `("dispatching", "running")`
(`project_store.py:366`); `idle` is in neither side, and `count_active` excludes managers
regardless. Reviving a manager to `idle` moves both sides by zero.

**Bus side effect, intended.** `bus.resolve`'s `manager` role resolves to the
`kind='manager'` order under the subject's feature and a settled one fills no role
(`bus.py:237-243`). `idle` is in `OPEN_STATUSES`, so a revived manager is an addressee
again and the next round's feedback has somewhere to land.

### (d) The envelope-lost sentence leads with what is settled

`invariants.check_no_lost_feedback`, the `detail=` at `invariants.py:2990-2992`. New
shape, settled order first:

> `the manager work order wo-b57da97e is completed under feature fo-ac00376e, so
> envelope 155 (deferral_request to role manager) about wo-3db50904 reached nobody:
> <note>. wo-3db50904 is held until the feature is live again.`

Derived, not stored: the check already has the envelope and reads the subject's status;
the settled addressee comes from `store.manager_work_order(_feature_of(...))` for a
`to_role == "manager"` envelope. For every other role the sentence is unchanged — only
the manager case had a second order in it to confuse. `context` gains `manager_wo_id`
and `manager_status`.

**The other two sites need no change, and that is checked, not assumed.**
`bus._unfilled`'s `note` (`bus.py:433-434`) already opens "the manager work order
{id} is {status}, so nothing can act on this", and the feature attention reason at
`bus.py:439-440` already opens "its manager work order is {status}". Both lead with the
settled fact. The second is additionally stored verbatim and compared by
`ProjectStore.ack_attention`, so rewording it would invalidate every existing ack of it
for no gain.

## Rejected alternatives

1. **Reopen the feature at the fork when a child is still live.** Neo question 1393
   rejected it: `settle_features` re-fails it on the next tick off the same dead child,
   via the predicate the settler and the invariant deliberately share. The user watches
   it flap.
2. **`jarvis wo retry` the delivered-then-killed child.** Nothing to retry — the work is
   delivered and the pull request is open. It would re-open a session to tell the user
   what the record already says, and it needs a person to type it.
3. **Widen `dead_feature_children` to exempt a delivered `failed` child.** Leaves the
   child's status a lie, where every other surface reads it: dependents treat `failed`
   as a dead dependency, `true_blockers` says "worker failed — review and retry". Fix
   the status, not the readers.
4. **Write the hold as a flag at the settle site** (`settle_features`,
   `_close_feature_manager`) instead of deriving it. Breaks kn-089de524: nothing
   re-derives it, so INV-ATTENTION-REASON relabels it on the next tick and the flag
   survives the feature being reopened.
5. **Keep manager liveness at each call site** — add the revive to
   `resume_feature_order` and to the invariant, and leave `_manager_handoff` alone. That
   IS today's bug: liveness written at transitions is what left three paths out of step.
   Two callers today become five next year.
6. **Have `_manager_handoff` create a replacement manager when the row is missing.** A
   manager carries the feature's whole conversation; a fresh one would be briefed on
   nothing and would answer a round of feedback blind. Flagging names a situation only
   the user can decide about.

## Tests

In `tests/`, beside `tests/test_feature_order_resume.py` and
`tests/test_validation_surfaces.py`.

1. **`tests/test_delivered_turn_death.py`** — a child with `pr_url` and three
   assumptions, a `failed` turn with an exhausted pause, one `Daemon.settle_work_order`
   pass: the child is `needs_review`, a `turn_died_after_delivery` event is on its
   timeline, the feature stays `executing` after `settle_features`, and
   `dead_feature_children` returns `[]`. Control: the same turn death on a child with no
   `pr_url` and no assumptions still lands `failed`. Reason cases: with assumptions
   pending the flag reads `"3 assumptions pending your review"`; with a `pr_url` and
   nothing pending it reads `TURN_DIED_AFTER_DELIVERY_BLOCKER` and survives a full
   invariant pass — INV-ATTENTION-REASON must not relabel it.
2. **`tests/test_settled_feature_hold.py`** — a live child under a `failed` feature:
   `true_blockers` leads with `SETTLED_FEATURE_BLOCKER` naming the feature and
   `jarvis fo resume`, and one reconcile tick raises the flag with that string
   (INV-ATTENTION-MISSING). After `ops.resume_feature_order`, the line is gone from
   `true_blockers` and the flag is down with no extra command. Controls: a child under an
   `executing` feature carries nothing; the manager order itself carries nothing; a
   `cancelled` feature names the `jarvis wo cancel` remedy, not `fo resume`.
3. **`tests/test_feature_order_resume.py`** (additions) — after `jarvis fo resume` on a
   feature whose manager was completed by `_close_feature_manager`, the manager is `idle`,
   unflagged, carries a `manager_revived` event, and `bus.post(..., to_role="manager")`
   for the latest round delivers instead of going `undeliverable`. The same four
   assertions after INV-FEATURE-FALSE-FAILURE's repair, driven through a doctor pass with
   no `jarvis fo resume` anywhere. And: `check_manager_slots` yields nothing across both.
4. **`tests/test_manager_order.py`** (additions) — `_manager_handoff` with an `executing`
   feature, a judged round, every live child `completed`, and a `completed` manager:
   never returns silently — the manager ends `idle` and either an envelope was posted or
   the feature is flagged. Second case: no manager row at all — the feature is flagged
   with `FEATURE_MANAGER_MISSING` naming it, once across two ticks. Control: a `running`
   manager is left alone and nothing is posted.
5. **`tests/test_validation_surfaces.py`** (additions) — for a lost `manager` envelope,
   the INV-ENVELOPE-LOST detail names the settled manager before the subject work order:
   the index of the manager id is lower than the index of the subject id. Control: a lost
   `implementor` envelope keeps the old sentence exactly.

## Not covered

- The stampede cleanup that killed the turn. The turn died legitimately as far as this
  spec is concerned; the defect is what the OS concluded from it.
- Any change to `Daemon.settle_features`, `dead_feature_children` or the feature state
  machine. The fork stays as it is; the inputs to it stop being wrong.
- Retroactive repair of features already settled this way. INV-FEATURE-FALSE-FAILURE
  plus (c) reopens them with a live manager once a child recovers; a feature whose child
  is still `failed` needs `jarvis fo resume`, which is the existing route and now revives
  the manager.
