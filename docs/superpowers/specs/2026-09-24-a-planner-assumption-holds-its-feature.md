# A planner assumption holds its feature

Issue #726. Work order wo-2e530b76.

## 1. The problem

A feature's planner (`WO_KINDS` `planner`, `feature_orders.plan_wo_id`) records
assumptions. Neo escalates some, so they sit `status='pending'` in `assumptions` with the
planner as `wo_id`, and the planner sits `needs_review — assumptions pending review`.

**Nothing reads them.** Two sites decide whether a feature's work proceeds and neither
looks past the row in hand:

* `ProjectStore.claim_next_pending` (`src/jarvis/project_store.py:1964`) filters a
  candidate on three subqueries — dependency edges, the feature's `max_parallel`,
  `retry_after`. None mentions `assumptions`. `Daemon.dispatch_pending`
  (`src/jarvis/daemon.py:862`) adds only the two concurrency caps.
* `Daemon.auto_merge` reads `store.pending_assumptions(wo_id)` at
  `src/jarvis/daemon.py:4535` — **the child's own** assumptions — and hands the bool to
  `automerge.decide` condition 3 (`src/jarvis/automerge.py:217`). A child owing nothing
  itself merges while the plan it implements is unratified.

Observed in production on one feature: 7 children dispatched, 3 merged, one of them
shipping exactly the schema a pending planner assumption asked the user to sign off; the
planner still read `needs_review — assumptions pending review` 13 hours later. The user's
decision had been pre-empted by the OS and the record did not say so.

Root cause, stated plainly: **the assumption is attached to a work order, and the unit it
actually governs is the feature.** Everything that reads assumptions is keyed by `wo_id`,
so the plan's undecided questions are invisible to its children. This spec does not move
assumptions off `wo_id` — that would rewrite four surfaces and the auto-review path. It
adds one derivation that closes the gap at the two sites that act.

## 2. The fix

While a feature's planner has ANY pending assumption: **no child of that feature is
dispatched, and no child's pull request auto-merges.** Children already running are
untouched — nothing is cancelled, interrupted or messaged. Both holds are DERIVED, write
nothing, and raise no attention: waiting is not an attention item, the same rule that
keeps a dependency edge and a `max_parallel` slot silent.

**The whole plan waits, not the dependent part of it.** Per-assumption dependence is not
derivable from the record: an assumption is free prose on the planner's timeline, children
name `spec_section`s and not assumptions, and nothing links the two. Guessing the blast
radius by text match would be the OS ruling on scope for the user — the exact failure this
fixes. So the unit of the hold is the unit of the plan.

### 2.1 One derivation: `ProjectStore.plan_hold`

```python
def plan_hold(self, wo: dict) -> dict | None:
    """{"fo_id", "planner_id", "n"} when this child's plan is unratified, else None."""
```

Shape copied from `invariants._slot_cap` (`src/jarvis/invariants.py:1204`): short-circuit
on `wo["parent_id"]` and `wo["kind"] != "worker"` from the row already in hand, so a
parentless order — nearly all of them — costs no query. One join otherwise:
`feature_orders.plan_wo_id` → `assumptions` where `status='pending'`.

Readers: `invariants.status_label`, `ops.waiting_on`, `Daemon.auto_merge`,
`ops.assumptions_with_rulings`. The claim query below cannot use it (it must stay inside
the atomic `UPDATE`), and that duplication is pinned by a test rather than argued away.

### 2.2 The dispatch hold goes in `claim_next_pending`, not `dispatch_pending`

A fourth `NOT EXISTS` beside the `max_parallel` one:

```sql
AND NOT EXISTS (
    SELECT 1 FROM feature_orders f
    JOIN assumptions a ON a.wo_id = f.plan_wo_id
    WHERE f.id = w.parent_id AND w.kind = 'worker'
      AND a.status = 'pending'
)
```

Why there:

1. **It is the row SELECTION filter.** A held child is passed over inside the same
   statement that picks the next row, so a younger unblocked order — another feature's
   child, another project's, a parentless one — is claimed on that same call. This is the
   non-starvation property `claim_next_pending`'s docstring already claims for dependency
   edges, inherited for free.
2. **`dispatch_pending` filters too late.** Its `while` loop calls `claim_next_pending`,
   which has already written `dispatching`. Skipping there means either releasing the
   claim back to `pending` (a write per tick per held child) or leaving the row
   `dispatching` with no turn — the state `settle_work_order` fails as "worker turn never
   started". And a held row is re-claimed on the loop's next iteration, spending the whole
   `max_concurrent` ceiling inside one tick: the defect `retry_after` exists to prevent.
3. **Both existing caps already live here**, so the three answers cannot drift apart.

Nothing is written when it fires — the order stays `pending`, exactly as a
dependency-blocked one does.

`w.kind = 'worker'` keeps the PLANNER dispatchable. Same exemption as `max_parallel`, and
stronger here: the planner is created and dispatched before the plan exists, so holding it
on its own assumptions would deadlock the feature permanently.

### 2.3 How it reads

**`invariants.status_label`, `pending` branch** — the single funnel every surface renders
through (`jarvis wo list`, `wo show`, `jarvis status`, dashboard). Reuse the dependency
wording path:

```
pending — blocked by fo-1a2b3c4d's plan (2 assumptions await your review — `jarvis wo review wo-9f8e7d6c`)
```

Ranked **above** the dependency label and the slot-cap label, inverting the existing
order. Justification, in that function's own terms: a dependency and a slot both clear
themselves, and this one clears only when a person acts. Of the three true sentences it is
the one naming a move.

`invariants.true_blockers` gets **no** branch, which is what keeps the hold flagless.
`pending` is already in `BLOCKED_STATUSES` (`invariants.py:75`) and
`check_blocked_work_is_surfaced` skips any row for which `true_blockers` is empty, so
INV-ATTENTION-MISSING stays quiet without an exception.

**`ops.waiting_on`** — a new branch immediately above the `status == "pending"` branch,
which today answers "not dispatched yet — no worker exists to nudge": true and useless.

```python
{"what": "plan_assumptions", "stalled": False,
 "detail": "its feature's plan is waiting on you — 2 assumption(s) on wo-9f8e7d6c; "
           "`jarvis wo review wo-9f8e7d6c`"}
```

`stalled` is False: a nudge cannot move it, the user's ruling can. The same branch must
also precede the `waiting_pr_merge`/`needs_review` catch-all, so a child parked behind the
merge hold does not report "nothing is running to nudge" either.

### 2.4 The auto-merge hold gets its OWN code

`automerge.HELD_PLAN_ASSUMPTIONS = "plan_assumptions"`, checked directly after
`HELD_ASSUMPTIONS` (the order's own decision is the more specific fact about the artifact
in hand). **Not folded into the existing `pending_assumptions` flag**, for two reasons and
the first is mechanical:

* `Daemon._note_automerge_held` dedupes on `(head_sha, code, reason)`
  (`daemon.py:5063`), and the module's own rule is ONE CODE PER CONDITION — kn-0aba30f0,
  issue #263, where four conditions sharing `pr_not_ready` deduped the second hold away as
  a repeat of the first and sent the user to look at CI for a merge conflict.
* The user reading the hold must be able to tell the two apart, because the command
  differs: `jarvis wo review <this order>` against `jarvis wo review <the planner>`.

Signature: `plan_assumptions: str = ""` — the planner's work-order id, empty meaning no
hold — rather than a second bool, so the reason can name it and `decide` stays pure:

> "the plan this work order implements is still waiting on you — assumptions on
> wo-9f8e7d6c, and the machine does not merge over a decision a person owes"

**ONE READ, used everywhere it is needed.** `Daemon.auto_merge` reads
`store.plan_hold(wo)` once, beside the existing `round_row` read at `daemon.py:4534`, and
passes the value to both `automerge.decide` and `automerge.only_the_head_moved` — the same
discipline, and for the same reason: a second read can disagree with the first by a
microsecond and produce a hold that was never true, deduped for ever.

`only_the_head_moved` is in practice unreachable under a plan hold (the new check returns
before the sha checks, so `HELD_SHA_MOVED` cannot be the code), but it takes
`pending_assumptions` today and takes this beside it — belt to that braces, and the OS must
not open a fresh validation round on behalf of a plan the user has not ratified.

### 2.5 Self-healing: no new invariant, and the tick is enough

**Nothing needs to poke anything when the last assumption is settled.**

* Dispatch: `Daemon.dispatch_pending` runs on **every** tick (~5s; only
  `track_injected_sessions` and `check_invariants` are on `RECONCILE_EVERY_TICKS = 6`).
  The held child is claimed on the next tick after the ruling.
* Merge: `Daemon.poll_pull_requests` runs on `PR_POLL_EVERY_TICKS = 24`, ~2min
  (`daemon.py:97`) — the latency the user already accepts between their own hand-merge and
  the work order closing.

So `ops.review_work_order` gains no call. Adding one would be a second place deciding what
clearing means, and it could not be the only one: `autoreview`'s delivery pass and the
dashboard settle assumptions too, so a poke would have to be added at three write sites
and would still be the slower of two paths to the same state.

**No new invariant, and the reason is structural:** this change writes no state. A held
child is `pending` with nothing recorded; a held merge writes only an `automerge_held`
event, which is a sentence and not a latch, re-decided from scratch on the next poll.
There is nothing that can go stale, therefore nothing to re-derive and nothing to repair.
(The rejected alternative in §4.1 — a status or a column — is exactly what would have
needed `INV-PLAN-HOLD-LIFTS` in `src/jarvis/invariants.py`, and that obligation is the
argument against it.) The guarantee is proved instead by an end-to-end test that rules the
last assumption and asserts the child dispatches and merges with no further command
(§5).

### 2.6 The note: children already merged

For the features this has already happened to, **the assumption stays `pending`.** No
auto-moot, no status change, nothing decided: the OS must never rule on the user's
question for them. What it adds is the fact the user needs in order to rule — that a
rejection now means filing a follow-up fix rather than unwinding.

Derived, no new column: a pending assumption on a planner, plus at least one child of that
feature carrying a `pr_merged` event.

* Derived from the **timeline**, never from `work_orders.pr_state` — kn-dbc4971d, that
  column is stale by construction and has one permitted reader. `pr_merged` is written by
  `ops.complete_merged` (`ops.py:4019`), which is the single close-out for both a
  hand-merge and an auto-merge, so one read covers both routes.
* Computed in `ops.assumptions_with_rulings` (`ops.py:2367`) — already THE one derivation
  that decorates assumption rows for both surfaces, precisely so `jarvis wo show` and the
  work-order page cannot disagree (issue #712). It adds a key per row, e.g.
  `overtaken: {"merged": 3, "of": 7}`, only when the row is `pending` and the work order
  is a planner; every other call is unchanged and costs nothing.
* Rendered by a new pure `ops.overtaken_line(a)`, appended to the `parts` list in
  `ops.assumption_line`'s pending branch beside `assumption_ruling_line`,
  `provisional_line` and `objection_line`; and exported to the template the way
  `assumption_ruling_line` already is (`ui/app.py:690`, `ui/templates/work_order.html:246`).

Wording: `3 of 7 children have already merged — rejecting this now means a follow-up fix,
not an unwind`.

## 3. What must not change

1. A planner with **zero** pending assumptions holds nothing — the subqueries are
   vacuously true and `plan_hold` returns None on the first branch.
2. A work order with no `parent_id` holds nothing, and pays no query for the answer.
3. An adhoc/injected order (`UNGOVERNED_ORIGINS`) is untouched: it has no parent.
4. The planner itself is never held by its own assumptions (`kind='worker'` in the
   clause).
5. Running children keep running. The hold is in the claim and in the merge decision, and
   in nothing else — there is no cancel path, no message, no status write.
6. Cancelled and failed features: **no feature-status term is added.** A cancelled
   feature's children are already `cancelled` and so are not `pending` — nothing changes
   for them. A failed feature's remaining `pending` children are held by the same universal
   rule if and only if the planner owes a decision, which is the correct answer rather than
   an exception; a status term would be a second rule to keep in sync with
   `Daemon.settle_features`.

## 4. Rejected

**4.1 A status, a column, or an attention flag.** `pending` plus a derived label is the
shape `max_parallel` and dependency edges already use, and `project_store.py:281-285`
states the rule: `waiting_pr_merge` earned a status because nothing derived it; this does
not. A written hold needs an invariant to un-write it, and a flag raised at the write site
re-raises every tick and overwrites the user's `jarvis wo ack` (kn-089de524).

**4.2 Hold only the children that depend on the assumption.** Not derivable — §2.

**4.3 Mark an overtaken assumption moot automatically.** It is the user's question; the OS
answering it is the bug, one level up.

**4.4 Cancel or message the running children.** Work in flight is not wrong yet, and
interrupting it destroys context the user may well ratify a minute later. The hold is on
starting and on landing, which are the two irreversible edges.

**4.5 Fold the merge hold into `pending_assumptions`.** One line smaller, and it breaks
the dedupe key and the user's ability to tell two decisions apart — §2.4.

**4.6 Poke the children from `ops.review_work_order`.** §2.5.

## 5. Tests

`tests/test_feature_orders.py`:

1. `test_a_planner_assumption_holds_its_features_children` — one pending assumption on the
   planner; `claim_next_pending` does not return the child.
2. `test_a_held_child_does_not_starve_the_queue` — the held child is the OLDEST row; a
   younger parentless order and another feature's child are both claimed. The property
   §2.2 rests on.
3. `test_the_planner_itself_is_never_held_by_its_own_assumptions`.
4. `test_a_running_child_is_untouched_by_a_new_plan_hold` — status, session and message
   queue all unchanged.
5. `test_plan_hold_and_the_claim_query_agree` — for a matrix of rows, `plan_hold(wo) is
   None` iff `claim_next_pending` returns it. The guard on §2.1's one duplication.
6. NEGATIVE `test_a_planner_with_no_pending_assumptions_holds_nothing` — dispatch proceeds.
7. NEGATIVE `test_a_work_order_with_no_feature_holds_nothing`.
8. `test_ruling_the_last_assumption_dispatches_on_the_next_tick` — `jarvis wo review` and
   then one tick, no other command.
9. `test_a_held_child_raises_no_attention` — `true_blockers` empty,
   `check_blocked_work_is_surfaced` yields nothing.
10. `test_the_hold_reads_as_blocked_by_the_plan` — `status_label` names the feature, the
    count and `jarvis wo review <planner>`.

`tests/test_automerge.py`:

11. `test_a_plan_assumption_holds_the_merge` — code is `HELD_PLAN_ASSUMPTIONS`.
12. `test_the_plan_hold_is_not_the_orders_own_assumption_hold` — distinct codes AND
    distinct reasons naming different work orders, so `_note_automerge_held` records both.
13. `test_the_plan_hold_is_read_once_per_poll` — counted call, the `round_row` discipline.
14. NEGATIVE `test_a_child_whose_plan_is_clear_still_arms`.
15. `test_a_ruled_plan_merges_on_the_next_poll` — no user command after `wo review`.

`tests/test_resume_auto_diagnosis.py`:

16. `test_resume_auto_names_the_plan_hold` — `what == "plan_assumptions"`, detail names the
    planner; and the parked-child case does not say "nothing is running to nudge".

`tests/test_feature_orders.py` (the note):

17. `test_an_overtaken_assumption_says_children_already_merged` — counts, and the row's
    `status` is still `pending` afterwards.
18. NEGATIVE `test_no_overtaken_note_when_no_child_has_merged`, and no note on a
    non-planner work order.

## 6. Out of scope

* Moving assumptions from `wo_id` to a unit that can be a feature. §1 names it as the root
  cause; this spec closes the two acting sites instead, and the cost of that choice is that
  any THIRD site later added which acts on a feature's behalf must remember `plan_hold`.
* The feature order's own surfaces (`jarvis fo show`, `/fo/…`) gain no hold line here. The
  children's lines already render through `status_label` and say it.
* Unwinding an already-merged child after a rejection. That is a follow-up work order the
  user files, which is what §2.6's note exists to tell them.
