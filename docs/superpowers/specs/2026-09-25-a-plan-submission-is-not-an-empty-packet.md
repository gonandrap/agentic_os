# A plan submission is not an empty packet

GitHub issue #758. Work order wo-8471afff. Neo question 697 (option A).

Companion to `docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md`, which
built the collector registry and the void outcome. This adds the third collector and
nothing else.

## The problem

A planner work order can never settle. It lands `needs_review` with
`VALIDATION_STUCK_BLOCKER` and stays there for ever.

### 1. The chain, with the code

1. `ops.submit_plan` is the planner's terminal action. Its last
   act calls `finish(fo["plan_wo_id"], ...)` — `jarvis fo plan`
   IS the planner's `jarvis wo finish`, which is why the planner briefing forbids the
   latter.
2. A plan changes no files in the planner's worktree. The design doc it wrote is
   snapshotted INTO the stored plan (`plan["design_doc_content"]`, in
   `ops.submit_plan`) and the children are created later by a different call, so
   the evidence packet carries no `files`, no `pr_url`, and — before this change — no side
   effects.
3. `evidence.nothing_to_judge` (`src/jarvis/evidence.py`) hits row 2 of its table
   (no files, no side effects) and returns `"escalate"`.
4. `Daemon._validate_work_order` (`src/jarvis/daemon.py`) escalates with, verbatim:
   `this submission changes no files and records no other durable effect, so there is
   nothing to review. Nobody has judged the work.`
5. `_escalate` delegates to `ops.escalate_validation_round`, which closes the round
   `escalated` and flags `VALIDATION_STUCK_BLOCKER`. `invariants.validation_escalated`
   keys on `outcome == "escalated"`, so `true_blockers` re-derives that flag on EVERY
   reconcile tick — `jarvis wo ack` cannot put it down.

### 2. Why nothing repairs it afterwards

`ops.review_plan(accept=True)` (`src/jarvis/ops.py`) creates the children, records
the feature's `base_sha`, moves the feature to `executing`, installs the feature agent,
and adds a `plan_reviewed` event to the planner (`src/jarvis/ops.py`). It never
touches the planner's status. No other machine re-derives it either: a `needs_review` work
order is settled as far as the reconciler is concerned.

With no pending assumptions the work-order page hides the Accept-all form, so the only
exit the user has is Mark done — closing by hand a work order that did exactly what it was
told to do.

### 3. Evidence

fo-ff8570fa, planner wo-83e4183c, jarvis-0.10.23. Planner `needs_review`, attention
`VALIDATION_STUCK_BLOCKER`, zero pending assumptions, the feature already `executing` off
the same plan.

### 4. Root cause, named

The registry `ops.SIDE_EFFECT_COLLECTORS` (`src/jarvis/ops.py`) has no collector for
a plan, so a plan submission is indistinguishable from a work order that delivered
nothing. Everything in §1 follows correctly from that one gap. This is the root cause and
it is what §5 fixes; the escalation and the stuck status are symptoms and are not touched.

## The fix

One registry entry. Not a widening of `daemon.py`'s guard, and not a status write in
`review_plan`.

### 5. The collector

Add to `ops.SIDE_EFFECT_COLLECTORS` (`src/jarvis/ops.py`):

```python
SideEffectCollector("plan", _plan_effects, attested=True),
```

`_plan_effects(store, wo_id)` collects the plan THIS work order submitted, as one durable
non-file effect:

- `kind`: `"plan_submitted"` — the same name `submit_plan` already writes on the timeline
  (`src/jarvis/ops.py`).
- `id`: the feature order id.
- `summary`: which feature order, and how many children the plan decomposes it into.
- `detail`: the design doc path, the child count, and the Neo plan question id
  (`plan_question_id`) — the review the plan is already queued for. What no diff can show:
  the plan lives in the feature order's `plan` column and dispatch materialises each
  child's brief from it.
- `verified`: per §6.

It returns `[]` when no feature order points at this work order, and raises nothing. That
is `side_effects_of`'s contract (`src/jarvis/ops.py`): a raising collector leaves the
round unjudged and retried for ever, and a swallowed exception could drop a judgeable
effect from a packet that then reads as all-attested and voids. `attested` is not set on
the record — `side_effects_of` computes it as the registry's AND of the collector's opt-in
and this effect's own `verified`, and overwrites anything a collector wrote.

### 6. `verified` is STRICT, and why the pointer is proof

`verified` is true only when a stored plan exists on a feature order whose `plan_wo_id`
equals THIS work order's id. Neo's condition, question 697. Explicitly NOT "this project
has a feature order in `plan_review`": that would attest any work order in a project where
some plan happened to be pending.

Why that pointer is proof where `_release_effects`' marker is only a claim
(`src/jarvis/ops.py`, whose docstring makes the distinction):

- `ops.submit_plan` is the ONLY writer of `plan` and of `plan_question_id`
  (`src/jarvis/ops.py`), and `plan_wo_id` is set when the planner is created — never
  by a worker.
- It writes them only after `plans.parse_plan` (`src/jarvis/ops.py`) and
  `plans.spec_problems` (`src/jarvis/ops.py`) have both passed, and after the named
  `design_doc` was found on disk.
- So the row is the OS's own validation record of a plan it accepted. A worker cannot
  author it from its worktree, which is exactly what a release marker under
  `$JARVIS_HOME/run/` or a timeline event CAN be.

No cross-check against external state is needed or added, because the artifact is not a
claim in the first place.

### 7. What changes downstream — nothing but the table row

With the collector in place a planner's packet has no files, one side effect, that effect
attested, and no PR. `evidence.nothing_to_judge` falls through to row 4 and returns
`"void"`. Then, unchanged code:

1. `Daemon._validate_work_order` (`src/jarvis/daemon.py`) calls `_void`.
2. `_void` (`src/jarvis/daemon.py`) closes the round `void`, adds `validation_void`,
   and calls `ops.land_when_cleared(store, wo, panel_cleared=True)`.
3. `void` is in none of the OPEN/RUNNABLE/COUNTED outcome sets and
   `invariants.validation_escalated` keys on `escalated`, so no flag comes back on the
   next tick.
4. `ops.land_when_cleared` (`src/jarvis/ops.py`) settles the planner the way
   `submit_plan`'s docstring already says it intends: `completed` when nothing else holds
   it; `needs_review` when the planner left pending assumptions.

A planner with pending assumptions still lands `needs_review`, and that is CORRECT — the
user genuinely owes those decisions. It is not this defect and is not in scope.

### 8. A planner with unlanded code is still judged

`nothing_to_judge` checks `packet.files` first and `pr_url`/`pr_error` before the attested
test. A planner that also committed code has non-empty `files`; one that opened a pull
request has a `pr_url` (or a `pr_error`, which counts the same). Either way it returns
`""` and the panel judges it as before. This change can never void a planner whose code is
unmerged, and never parks an unread pull request on the merge queue.

### 9. The rejection path keeps working

`ops.review_plan(accept=False)` reaches a planner that is now `completed` rather than
`needs_review`. That is already permitted: `ops.send_message` (`src/jarvis/ops.py`)
accepts a `completed`/`failed`/`cancelled` work order and notes `the session will be
revived`. The feedback is queued as a message and the planner revises from its existing
session, exactly as `submit_plan`'s docstring promises. Neo required a test, §11.2.

## 10. Rejected alternatives

1. **Widen `daemon.py`'s empty-packet guard for planner work orders.** The guard has been
   widened twice already for the same lesson (issue #200, wo-ec96a1e9), which is why the
   registry exists; and `nothing_to_judge` has two callers (work-order loop
   `daemon.py`, feature loop `daemon.py`), so a guard-side special case is a
   second copy of the rule waiting to drift.
2. **Have `review_plan` set the planner's status.** Repairs the symptom at the far end:
   the planner would sit flagged with `VALIDATION_STUCK_BLOCKER` for however long the
   review takes, an escalated round would stay on its record claiming nobody judged the
   work, and `true_blockers` re-derives that flag from the round — so `review_plan` would
   have to rewrite a validation round it has no business touching.
3. **Have `submit_plan` skip validation for planners.** Puts a "this unit is exempt" path
   in the submitter, which is precisely the door void exists to keep shut: a submitter must
   not be able to reach an unjudged settle by declaring itself special.
4. **`attested=False` on the plan collector.** Makes the packet judgeable rather than
   void, so a panel is called to review a plan Neo is ALREADY reviewing
   (`plan_question_id`) — two reviewers, one artifact, and a second chance to disagree.
5. **Store the plan's design doc as `packet.files`.** Fabricates a diff. The doc is in the
   planner's worktree on a branch nobody merges, and `files` is where a reviewer looks for
   what shipped.

## 11. Tests

House conventions: the `fleet` fixture and `Validator`, `finish`, `passed` from
`tests/test_validation_loop.py`; `a_plan`/`child` and the `planning` fixture shape from
`tests/test_feature_orders.py`; the decision-table style of
`tests/test_validation_void.py`. Drive the real `Daemon._validate_work_order`, never
`nothing_to_judge` alone — the defect was never in one function.

New file `tests/test_plan_side_effect.py`.

1. **A submitted plan voids its round and settles its planner.** Create a feature order,
   tick to dispatch the planner, `ops.submit_plan(fo_id, a_plan(child("schema")))`, drive
   the validation tick. Assert: the latest round's `outcome == "void"`; the planner is NOT
   `needs_review`; `invariants.true_blockers` returns no `VALIDATION_STUCK_BLOCKER` for it.
   Then `ops.review_plan(fo_id, accept=True)` and assert the planner is `completed` and
   absent from the needs-review list.
2. **Rejection still reaches a settled planner.** From the state test 1 left — planner
   settled off a voided round — call `ops.review_plan(fo_id, accept=False,
   feedback="split the schema child")`. Assert a message is queued on the planner work
   order, the result carries `delivered`, and it carries NO `delivery_error`.
3. **`verified` is strict, both directions.** (a) A work order no feature order points at
   collects `ops.side_effects_of(store, wo_id) == []`, and a round on it still escalates —
   the row-2 guard is intact for a work order that really delivered nothing. (b) A feature
   order whose `plan_wo_id` is a DIFFERENT work order does not attest this one:
   `side_effects_of` for the unrelated work order is `[]`, and its round escalates.
4. **A planner that also wrote code is judged.** A planner whose packet has files or a
   `pr_url` reaches the panel: assert the validator was called and the round did not void.

## 12. Out of scope

- The planner landing `needs_review` when it left pending assumptions (§7). Correct
  behaviour.
- The work-order page hiding the Accept-all form when there are no pending assumptions
  (§2). It is why the stuck state had no exit, but it is a separate UI question and this
  fix removes the state that made it visible.
- Feature-order rounds (`daemon.py`). They share `nothing_to_judge` and inherit the
  new row for free; nothing here is specific to them.
- Backfilling planners already stuck in production, fo-ff8570fa included. `jarvis
  validation force` cannot open a round on a `needs_review` planner with no pull request;
  those exit by `jarvis wo done`.
