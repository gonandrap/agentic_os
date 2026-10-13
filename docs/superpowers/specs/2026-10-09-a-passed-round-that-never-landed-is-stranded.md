# A passed round that never landed is stranded

io-df4fa1e3, kn-a00e8895, kn-a8e2cc94. Neo question 1530 settled the design. Spec for a
second arm on `invariants.check_validation_progresses` (src/jarvis/invariants.py:3183).

## The problem

A work order can PASS review and never land. It stays `validating` for ever, and nothing
in the OS selects that shape.

The deferral. `Daemon._validate_work_order` lands a passed round only while no worker
turn is in flight (src/jarvis/daemon.py:2713-2719):

```python
if worker_session.busy(store, wo_id) is None:
    status = ops.land_when_cleared(store, wo)
else:
    status = "deferred — a worker turn is in flight"
    store.add_event(wo_id, VALIDATION_LANDING_DEFERRED,
                    {"round": n, "round_id": round_id, "outcome": "passed"})
```

The deferral is correct — `wo` is minutes stale and landing would write a status under a
worker that is typing — and the landing is delegated to `Daemon.settle_work_order`.

The delegate is unreachable. `settle_work_order` returns early for exactly this status
(daemon.py:5466-5472, "THE ROUND MACHINE OWNS THIS WORK ORDER"), and the
deferred-landing branch that would take it sits ~140 lines BELOW that return
(daemon.py:5609-5613). A landing is only ever deferred while the order IS `validating`,
so the branch is dead in every case it exists for.

Measured 2026-10-08: wo-9f00e3b5 stranded 7.5d, wo-12b4e416 8d, wo-d2d777dc 2d. Latest
round `passed`, no round open, never reached `waiting_pr_merge` — so the PR merge poll,
the conflict auto-repair and the auto-merge gate never ran on delivered, passed work.

Nothing selects it. Four places could have and none does:

* `ProjectStore.work_orders_awaiting_validation` (src/jarvis/project_store.py:5035) keys
  off a RUNNABLE outcome — `("pending", "failed")`, project_store.py:118. `passed` is
  deliberately absent.
* the `validating`-with-no-round warning (daemon.py:2415-2419) needs the round ABSENT;
  here it is present and closed.
* `invariants.check_validation_progresses` selects `WHERE r.outcome='pending'`
  (invariants.py:3220-3223).
* `invariants.true_blockers` (invariants.py:974-1245) has no `validating` branch at all —
  `validating` is deliberately silent, so no attention flag is raised either.

Root cause: the landing of a passed round is held in ONE control-flow path and nowhere in
the STATE. A sibling work order hoists the settle branch above the early return, which
repairs the reachable case. This spec is the other half io-df4fa1e3 demands: the landing
must be recoverable FROM THE STATE, so the class also survives a daemon that dies between
the deferral and the settle, and does not depend on one branch staying reachable.

## The fix

A SECOND ARM on `invariants.check_validation_progresses`, under the same invariant id
`INV-VALIDATION-STRANDED`. Same defect — a unit under review nothing will ever move
again — in its other state.

### 1. Predicate

All three conjuncts, work orders only:

1. `work_orders.status == 'validating'`,
2. `store.latest_validation_round(wo_id=...)["outcome"] == "passed"`,
3. `worker_session.busy(store, wo_id) is None`.

No staleness threshold of its own.

Why conjunct 3: landing under a live turn is exactly what the deferral protects against
(daemon.py:2706-2712, spec
docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md §3.1). An order whose worker
is typing is not stranded, it is waiting correctly.

Why no threshold: once no turn is in flight there is nothing left that could move it, so
the state is already a defect rather than a late round. The `pending` arm needs a
threshold because a round at 1.2x its budget is still being judged; a `passed` round is
finished being judged.

REJECTED: derive it from the `VALIDATION_LANDING_DEFERRED` / `VALIDATION_LANDED` event
pair via `daemon._landing_deferred` (daemon.py:550-565). That pair misses the case the
state-derived predicate exists for — a daemon that died between
`close_validation_round(round_id, 'passed')` (daemon.py:2701) and writing the deferral
event. The event pair is the settler's bookkeeping, not evidence about the world.

Test 5 ("an order already carrying `validation_landed` yields nothing") needs a WRITTEN
GUARD, not construction. `land_when_cleared` returns `validating` for an open round, so an
order can carry a `validation_landed` event for the latest round and still be `validating`
— and a pure state predicate would select it and land it twice. Fourth conjunct: skip the
order when the latest `validation_landed` event's payload `round_id` equals
`latest["id"]`.

### 2. Repair

`ops.land_when_cleared(store, fresh_wo)` — the settler's own act (daemon.py:5611), never a
second copy of the join. Re-read the row first: `land_when_cleared` must see a `pr_url`
the last turn wrote.

Then record the daemon's `VALIDATION_LANDED` event with the round id, so the hoisted
settler cannot land the same round twice — `daemon._landing_deferred` reads that pair:

```python
store.add_event(wo_id, VALIDATION_LANDED, {"round_id": int(latest["id"])})
```

The event-kind constants live in daemon.py (daemon.py:426-427) and daemon imports
invariants at module level, so invariants.py must reach them by a LAZY import INSIDE the
function — the shape `check_envelopes_move` already uses for its bus-side import
(invariants.py:2631).

### 3. `repair=False` must write nothing

Skip the `land_when_cleared` call ENTIRELY when `getattr(store, "readonly", False)`, as
`check_envelopes_move` does (invariants.py:2637-2643), and still yield the `Violation`
with `repaired=True` and a `repair=` describing what WOULD be applied — the `_ReadOnly`
convention, invariants.py:5031-5032.

The proxy is not enough on its own. Every PROJECT-store write `land_when_cleared` makes is
already blocked by `_ReadOnly._BLOCKED` (`set_status`, `clear_attention`,
`flag_attention`, `add_event` — invariants.py:5035-5046), but `ops.land_finished`
(src/jarvis/ops.py:4840-4885) reaches `CentralStore.mark_backlog` at ops.py:4880-4885 for
a no-PR order with a `backlog_id`, and a proxy over the project store cannot intercept a
write to the CENTRAL store. Same reason `check_envelopes_move` skips rather than relies on
the proxy.

### 4. Hard constraints

* MUST NOT widen the existing `pending` arm, and MUST NOT move its
  `2 * os.validation.timeout` threshold (invariants.py:3213-3216). The two arms are
  separate predicates over separate outcomes.
* WORK ORDERS only. The feature-order half of the existing invariant selects
  `feature_orders.status='validating'` (invariants.py:3225-3226) and nothing sets a
  feature there yet — there is no deferral to recover.
* One `Violation` per order per tick, `invariant="INV-VALIDATION-STRANDED"`, `wo_id` set.
  `context` carries the round id, the round number and `unit="work order"`, matching the
  `pending` arm's shape so every consumer of that invariant keeps working.

### 5. Tests

`tests/test_invariants.py`, next to the existing INV-VALIDATION-STRANDED block
(tests/test_invariants.py:620-679). The helper `_stranded(store, age=…, outcome=…)`
(test_invariants.py:627-648) already builds a CLOSED round, so `outcome="passed"` needs no
new fixture; `_stranded_violations(store, repair=…)` and `_round` are reused.

1. `validating` + latest round `passed` + idle worker repairs to `waiting_pr_merge` in ONE
   `check_project(repair=True)`, and reports ONCE — a second call yields nothing.
2. the same order under `check_project(repair=False)` is REPORTED and NOT written: status
   is still `validating` afterwards.
3. a PENDING round is untouched by the new arm — the existing threshold pairing still
   holds (a round one timeout old stays `pending`, is not reported).
4. an order whose worker turn IS in flight (`worker_session.busy` not None) yields
   nothing. The deferral is still correct there.
5. an order already carrying `VALIDATION_LANDED` for that round yields nothing.

Run `uv run pytest tests/test_invariants.py tests/test_validation_loop.py -q` while
working.

## What is NOT changed

* `Daemon._validate_work_order`'s deferral (daemon.py:2713-2719) — correct as written.
* `Daemon.settle_work_order`'s early return (daemon.py:5466-5472) and the hoist of its
  deferred-landing branch — the sibling work order's scope.
* `ops.land_when_cleared`, `ops.land_finished`, `daemon._landing_deferred`,
  `ProjectStore.work_orders_awaiting_validation`, `RUNNABLE_VALIDATION_OUTCOMES`.
* `invariants.true_blockers` — gains no `validating` branch. `validating` stays silent;
  the repair lands the order, which is what raises the flag the order actually needs.
* The invariant REGISTRY. `check_validation_progresses` is already in `INVARIANTS`; this
  adds an arm, not a checker.
* No migration. The three measured orders are repaired by the first reconcile tick after
  the release, from their own state.
