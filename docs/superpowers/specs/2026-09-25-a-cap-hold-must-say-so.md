# A cap hold must say so

GitHub issue #752. Work order wo-df267a02.

Two independent defects, both of which make the OS's record contradict what the OS is
actually doing to a work order. Neo has ruled on the design of the first two parts
(question 677 Option A; question 679 Option A) and those rulings are not reopened here.

## The problem

### 1. The retry sweep holds an order and writes nothing

`Daemon.retry_paused_turns` (src/jarvis/daemon.py:1350) has two cap skips and neither
leaves a trace:

* `daemon.py:1397-1398` — `if state is not None and state.blocked(): return`. The fleet
  cap (`os.defaults.max_in_flight`) or an account outage.
* `daemon.py:1409-1410` — `if takes_a_slot and budget <= 0: continue`. The project cap
  (`project.max_concurrent`), via `project_store.resume_spends_slot`.

Measured on wo-83e4183c: turn 13 was refused on the Claude usage limit (reset 15:50). At
15:50:02 the sweep resumed three other orders, which filled `max_in_flight=3`; the loop
then took the `return` at 1397 and wrote nothing at all on wo-83e4183c. Consequences,
both of them from the same silence:

* `invariants.pause_note` (src/jarvis/invariants.py:1297) kept rendering
  `needs_review — Claude usage limit reached, retrying by itself at 15:50` — a promise
  about a moment that had already passed. It is the one string every surface prints:
  `status_label` (CLI), `ops.os_status`'s `open_work_orders[].pause` (dashboard),
  `ui/app.py:859` and `:979`.
* `invariants.check_paused_turns_resume` — INV-PAUSE-OVERDUE, invariants.py:2234 — fired
  15 minutes later (`PAUSE_OVERDUE_GRACE = 15 * 60`, invariants.py:1260) with
  `… still not relaunched 15m later — the OS is not healing this by itself`. False: the
  OS was healing it, in the order the fleet cap dictated.

The invariant's own docstring states the property it is watching — "a paused turn whose
wait is over must actually be relaunched" — and its predicate is `resumable and
now - retry_at > PAUSE_OVERDUE_GRACE`. It has no way to tell "the sweep is broken" from
"the sweep looked at this order and deliberately deferred it", because the deferral is
not written down anywhere. The root cause is exactly that: **a hold that produces no
record**. It is not a wording bug in either surface.

Contrast `dispatch_pending` (daemon.py:877), where the same silence is correct: a held
back `pending` order is still `pending`, and `status_label`'s `_slot_cap` and
`fleet.blocked()` branches (invariants.py:1240-1251) derive the hold from live state
every time it is asked for. A paused resume has no such derivable state — the sweep's
decision depends on `budget`/`in_flight` at the instant it ran, which nothing persists.

### 2. A verdict for an earlier round moves an order whose worker is mid-turn

Same work order. A validation round that opened before turn 13 came back `passed` at
11:19:50, while turn 13 was still running. `Daemon._validate_work_order`'s passed branch
(daemon.py:1869-1877) closes the round, records `validation_passed`, then calls
`ops.land_when_cleared(store, wo)` (src/jarvis/ops.py:3026), which — with an assumption
pending — sets `needs_review` and flags attention. A planner halfway through a revision
therefore read as awaiting the user.

The guard that already exists one screen above, `store.round_machine_owns(...)` at
daemon.py:1836, is the same shape of check for the opposite direction: it protects the
ROUND from the work order settling underneath it. Nothing protects the WORK ORDER from
the round landing underneath a live turn. `wo` in that branch is the row read at
daemon.py:1717, before the seats ran — minutes stale by construction.

`_reject`, `_escalate` and the final-round rejection are covered in §3.3.

## The fix

Four parts. Parts 1-2 close defect 1; part 3 closes defect 2; part 4 writes down an
ordering the code already has.

### 1. The sweep records why it returned

New event kind `retry_held` on the work order, written by `retry_paused_turns` at each
of its two cap skips.

**Payload** (one shape for both skips, so a reader has one branch):

```
{"cause": "fleet_cap" | "project_cap" | "fleet_outage",
 "seq": <pause.turn["seq"]>,        # the turn whose relaunch is being held
 "reason": <pause.reason>,          # PAUSE_USAGE_LIMIT / PAUSE_TRANSIENT / PAUSE_AUTH
 "retry_at": <pause.retry_at>,      # the moment it came due
 "in_flight": N, "cap": M,          # fleet_cap and fleet_outage only
 "active": N, "max_concurrent": M,  # project_cap only
 "reopens_at": <float>}             # fleet_outage only
```

`seq` is on it because the hold is about ONE pause: a later turn makes the event
meaningless, and a reader that could not tell would suppress an invariant about a
different failure (the rule `ops.resumed_from`, ops.py:4739, already applies to
`turn_resumed`).

**The outage is not relabelled a cap.** `fleet.Fleet.blocked()` (src/jarvis/fleet.py:76)
returns two different sentences: the outage one when `shut()`, the in-flight one
otherwise. When the skip at daemon.py:1397 is taken with `state.shut()` true, the cause
recorded is `fleet_outage` and NOT `fleet_cap` — the words the user sees for that case
are `invariants.fleet_hold_note`'s (invariants.py:1122), which already owns them since
issue #714, and a second source for the same sentence is the drift this spec is paying
for elsewhere. The event is still written, because the INVARIANT suppression in §2b is
needed for an outage hold exactly as it is for a cap hold: the sweep returns, the pause
goes overdue, and INV-PAUSE-OVERDUE says the OS is not healing something the OS is
correctly waiting on. What §2a renders for that cause is stated there.

**Dedupe.** Two constants, both in invariants.py beside `PAUSE_OVERDUE_GRACE` (:1260) and
imported by the daemon, so the inequality between them is visible in one place:

```python
RETRY_HELD_RESTATE = 5 * 60        # write at most one `retry_held` per order per 5 min
RETRY_HELD_FRESH_FOR = 2 * RETRY_HELD_RESTATE   # 10 min — a reader's "this hold is live"
```

The sweep writes a `retry_held` only when the newest one for this `(wo_id, seq)` is older
than `RETRY_HELD_RESTATE`. A 5-hour usage-limit hold therefore costs ~60 events instead
of ~1,800 at the `RETRY_EVERY_TICKS = 2` (~10s) sweep cadence (daemon.py:152).

Why 5 minutes and not 15: `RETRY_HELD_FRESH_FOR` must be strictly less than
`PAUSE_OVERDUE_GRACE`, and the restate interval must be strictly less than the freshness
bound, or the two readers in §2 would flap. With restate 5 / fresh 10 / grace 15, an
ongoing hold always has an event newer than 10 minutes (restated every 5, with a full 5
minutes of slack for a tick that slipped), and a hold that ENDED goes stale inside 10
minutes — so the invariant comes back within 10 minutes of the last hold, never later.
Setting restate at the grace itself would mean a live hold could be 15 minutes without a
restate, exactly when the invariant is deciding whether to fire.

**How the pass stays cheap while naming every order it held.** The `return` at
daemon.py:1398 becomes a per-iteration hold string:

```python
for wo in store.list_work_orders(statuses=RETRY_SWEEP_STATUSES):
    held = state.blocked() if state is not None else ""
    ...                       # origin filter, turn_pause, resumable/due filter unchanged
    if held:                  # after the filter, before `resume_spends_slot`
        self._record_retry_held(store, wo, pause, cause=..., figures=...)
        continue
    takes_a_slot = resume_spends_slot(wo)
    if takes_a_slot and budget <= 0:
        self._record_retry_held(store, wo, pause, cause="project_cap", ...)
        continue
```

Three properties this shape buys, each of which a hoisted pre-loop check would lose:

1. `state.blocked()` is re-read per iteration, because `state.launched()` mutates
   `in_flight` inside this very loop — the pass can fill the cap itself (`Fleet` docstring,
   fleet.py:56). A hoisted read would hold back nothing on the tick that filled it.
2. The recording site is BELOW the `pause is None or not resumable or not due` filter at
   daemon.py:1406, so only a paused, due, resumable order is ever recorded. That is the
   only order worth recording — nothing else was going to be relaunched on this pass, so
   nothing else is being held.
3. The cost of naming them is the cost the loop already pays. Diagnosing the pause is one
   indexed `latest_turn` read plus a parse (`worker_session.turn_pause`), and the
   unblocked pass makes exactly that call for every order in `RETRY_SWEEP_STATUSES` on
   every sweep. The held pass now makes the same calls and no others: no subprocess, no
   `worker_session.retry`, and at most one small INSERT per order per 5 minutes. What is
   given up is the early `return`'s saving on the REMAINING orders of a blocked project,
   which is bounded by the size of `RETRY_SWEEP_STATUSES` for one project — a single-digit
   number of SQLite reads per 10 seconds on a fleet that is by definition doing nothing
   else.

**Store primitive.** `ProjectStore.last_event_of_kind(wo_id, kind) -> dict | None`,
beside `events_of_kind` (project_store.py:2716), `SELECT * FROM wo_events WHERE wo_id=?
AND kind=? ORDER BY ts DESC LIMIT 1`. `events_of_kind` is oldest-first and uncapped and
`list_events` (project_store.py:3286) takes the oldest `limit` rows; both are the wrong
read for "the newest one", and a hold that restates is the first caller that wants it.
Served by `idx_events_wo` (project_store.py:1000).

**Timeline.** `retry_held` joins `timeline.DEBUG_KINDS` (timeline.py:32). Precedent is
`health_reviewed` at timeline.py:45-51: the sweep LOOKING is not the same event as the
sweep finding something, and up to twelve rows an hour of "still queued" would bury the
work in `jarvis wo show`. The user-facing half of this hold is §2a's one sentence, not a
row per restate; the events are there for `--debug` and for the invariant.

**Not in scope:** `Daemon.deliver_messages` (daemon.py:1472) holds a queued message under
the same project cap and writes only a `log.debug` (daemon.py:1534-1536). Same shape of
silence, different surface — `invariants.stuck_message` already renders a reason there —
so it is left alone rather than widened into this change.

### 2. The two surfaces read the hold back

New reader, in invariants.py beside `pause_note`, used by both surfaces so they cannot
drift:

```python
def retry_hold(store, wo, pause, now=None) -> dict | None:
    """The `retry_held` payload explaining why this due pause has not relaunched, if it
    is still live: same turn `seq`, written inside RETRY_HELD_FRESH_FOR."""
```

#### 2a. `pause_note`, not `status_label`

The change lands in `pause_note` (invariants.py:1297) and nowhere else. `status_label`
reaches it from TWO branches — the `ACTIVE_STATUSES` one at invariants.py:1171 and the
settled-status one at invariants.py:1194 added by issue #259 — and `ops.os_status`
(ops.py:601) and both dashboard sites (ui/app.py:859, :979) call it directly. One edit in
`pause_note` reaches all five; an edit in `status_label` would reach two of them and would
have to be made twice.

Position inside `pause_note`: after the `pause is None or pause.exhausted` guard, ABOVE
the `PAUSE_AUTH` branch and above both clock branches. It outranks all three because it
is a fact about the same pause that is strictly more current: those three say what the
pause is waiting for, this says that the wait is over and something else is now in the
way. Ranking it below the auth branch in particular would misreport the case that
motivates the ordering — a user who has just signed in, whose pause is now due, and whose
relaunch is queued behind the cap, would still read "resuming once you sign in again".

Wording, from the event payload rather than a live `Fleet` (`pause_note` is handed a
store and a row, the same reason `status_label` gives at invariants.py:1152 for not
reading the catalog):

| cause | note |
|---|---|
| `fleet_cap` | `waiting for a free in-flight slot ({in_flight} of {cap} worker turns already in flight)` |
| `project_cap` | `waiting for a free slot in this project ({active} of {max_concurrent} running)` |
| `fleet_outage` | `""` when `wo["status"]` is in `FLEET_HELD_STATUSES` (invariants.py:1119) — `fleet_hold_note` owns those words; otherwise `the Claude usage window is spent, reopening at {clock(reopens_at)}` |

The `fleet_cap` parenthesis is `Fleet.blocked()`'s own clause (fleet.py:86) verbatim, so
the header of `jarvis status` and a work order's line say the same thing in the same
words. The `fleet_outage` split exists because `fleet_hold_note` is gated on
`FLEET_HELD_STATUSES` and a `needs_review`/`waiting_pr_merge` order held by the outage
would otherwise lose the sentence it prints today — a regression, not a fix. Widening
`fleet_hold_note` to those statuses is issue #714's business and is NOT done here.

Neither sentence raises attention. Same rule as the dependency and slot labels at
invariants.py:1240-1251: a slot frees by itself, so nothing is owed by anyone.

#### 2b. INV-PAUSE-OVERDUE stops firing on an explained hold — and comes back

In `check_paused_turns_resume` (invariants.py:2234), between the `resumable` check
(:2288) and the `overdue` computation (:2291):

```python
if retry_hold(store, wo, pause) is not None:
    continue    # the sweep looked at this and deferred it; nothing is broken
```

Freshness is `RETRY_HELD_FRESH_FOR` (10 min) on an event whose `seq` equals
`pause.turn["seq"]`. Both conditions are load-bearing and they fail in the two directions
that matter:

* **The hold ends and the relaunch works.** A new turn exists, `turn_pause` returns None,
  and the loop `continue`s before this line ever runs. Unchanged from today.
* **The hold ends and the relaunch still fails.** No sweep writes a further `retry_held`
  for that `seq` — the recording site is inside the cap skips and nothing else — so the
  newest one ages past 10 minutes and the violation comes back, with `overdue` already
  well past the 15-minute grace. Worst case the report is 10 minutes later than today.
  **This is the liveness property the invariant exists for and the fix does not blind
  it**: suppression requires a positive, recent, turn-matched statement from the pass
  that is supposed to be doing the work. Absence of evidence is still a violation.
* **The pass dies mid-hold** (exception, daemon restart, a clock skew like PR 129's):
  nothing is written, the last event ages out, the invariant fires. That is the failure
  mode the whole check was built for, and it is still covered.

INV-PAUSE-DRIFT is untouched: it watches `retry_at` moving, which a cap hold does not do.

### 3. A stale verdict must not move a live order

#### 3.1 Guard the landing, not the round

In `_validate_work_order`'s `outcome == "passed"` branch (daemon.py:1869-1877), the round
still closes `passed` with its real reason, the follow-ups are still filed and
`validation_passed` is still recorded. Only the call at daemon.py:1875 changes:

```python
if worker_session.busy(store, wo_id) is None:
    status = ops.land_when_cleared(store, wo)
else:
    store.add_event(wo_id, "validation_landing_deferred",
                    {"round": n, "round_id": round_id, "outcome": "passed"})
```

**The predicate is `worker_session.busy` (worker_session.py:235), not a `seq`/`ts`
comparison against `round_row["ts"]`.** Three reasons, in order of weight:

1. It is the question actually being asked. The harm is writing a status under a worker
   that is typing; `busy` is the OS's one definition of that, already enforced as an
   invariant of the transport ("one turn at a time, always") and already the predicate
   `deliver_messages` and `worker_session.delivery_hold` use before touching a
   conversation. A timestamp comparison approximates it and would be a second definition.
2. A turn that started after the round opened and has ALREADY ENDED needs no guard:
   `settle_work_order` re-derives that order's status from the finished turn on the very
   next tick, so deferring there would buy nothing and cost a tick. A `ts` comparison
   cannot tell those two apart without also reading the turn's state — at which point it
   is `busy` with extra steps.
3. One indexed read on `idx_turns_wo`, on a thread that has just spent minutes in the
   seats.

The race it does not close: `busy` can be false here and a turn launched a microsecond
later by the tick thread's `deliver_messages`. That window is one `set_status` wide and
self-correcting — `settle_work_order` re-derives the status from the turn the moment it
settles. Locking two threads together to close it costs more than the symptom.

#### 3.2 The deferred landing is picked up post-turn — explicitly

Verified against the code: `Daemon.settle_work_order` (daemon.py:3933) **never calls
`land_when_cleared`** and never consults the validation round on its post-turn path. What
it does today for a done turn (daemon.py:4057-4098):

* `result_summary` + pending assumptions -> `needs_review` + "assumptions pending review";
* `result_summary` + `pr_url` -> `back = pr_repair_origin or resumed_from or
  "waiting_pr_merge"` (daemon.py:4083-4085);
* `result_summary`, no `pr_url` -> `ops.land_finished` (ops.py:2859);
* no `result_summary` -> `needs_review` + `IDLE_NO_FINISH_BLOCKER`.

So a deferred landing usually *happens to* arrive at the same status — `result_summary`
is a column written by `ops.finish` and survives later turns — but it arrives by
re-deriving the join from the turn rather than from the round, and it skips two rules that
only `land_when_cleared` knows: `refusal_answered` (ops.py:3068) and the
`OPEN_VALIDATION_OUTCOMES` re-park. Two derivations of one join is precisely the drift
`land_when_cleared`'s docstring says it exists to prevent. Per Neo's ruling, the re-land
is therefore made explicit rather than left to coincidence.

Edit, in `settle_work_order` after the `queued_messages` guard and the `fresh =
store.get_work_order(...)` read at daemon.py:4057, before `if fresh.get("result_summary")`:

```python
if round_open == "passed" and _landing_deferred(store, wo["id"]):
    from . import ops as ops_mod
    ops_mod.land_when_cleared(store, fresh)
    store.add_event(wo["id"], "validation_landed", {"round_id": <deferred round_id>})
    return
```

* **Free for every order that never deferred.** `round_open` is already read at
  daemon.py:3954 for the budget guard; keep the whole row (`latest_round =
  store.latest_validation_round(...) or {}`) instead of only its `outcome`, and the two
  `last_event_of_kind` reads happen only when the latest round is `passed`.
* `_landing_deferred` is "the newest `validation_landing_deferred` names a round_id that
  the newest `validation_landed` does not". Both events, no column: the rule at
  project_store.py:281-285 — `waiting_pr_merge` earned a status because nothing derived
  it; this is derived.
* **After the `queued_messages` guard**, because another turn is going out this tick and
  the landing must wait for that conversation too.
* **On `fresh`, not `wo`**, so the landing sees a `pr_url` the just-finished turn wrote.

#### 3.3 The rejected and escalated branches

* `outcome == "rejected"` with `n < max_rounds` (daemon.py:1878-1883, `Daemon._reject`,
  daemon.py:1978): **no defect, no guard.** It writes no status. Its feedback is a
  `bus.post` envelope, which becomes a queued message and is held behind the live turn by
  `worker_session.delivery_hold` in `deliver_messages` (daemon.py:1528-1529). The
  existing machinery already does the right thing.
* Final-round rejection and `escalated` (daemon.py:1884-1901) both route through
  `ops.escalate_validation_round` (ops.py:3131), which DOES `set_status(wo_id,
  "needs_review")` + `flag_attention(VALIDATION_STUCK_BLOCKER)` under a possibly-live
  turn. Same shape of defect — **and the same guard must NOT be applied.**

  Why not: `invariants.true_blockers` derives `VALIDATION_STUCK_BLOCKER` only for
  `wo["status"] == "needs_review"` (invariants.py:852-860, case 2). Deferring the status
  write would leave the flag underivable, and INV-ATTENTION-REASON would rewrite or drop
  it on the next reconcile tick — the OS would swallow a give-up. A give-up is also the
  one transition in this machine that notifies (`_escalate`'s docstring, daemon.py:2044),
  precisely because it must reach the user immediately.

  What is needed there instead is the opposite guarantee: the escalation must not be
  silently UNDONE by the NEXT turn. Not by the live one — `settle_turns` sweeps only
  `running`/`idle`/`waiting_input`/`dispatching` (daemon.py:4025-4026) and the escalation
  writes `needs_review` before that turn ends, so the turn that was live when the round
  gave up is out of the sweep and `settle_work_order` is never called on it at all. The
  exposure is the turn that STARTS after: a queued message un-parks the order to
  `running`, and when that turn ends the `pr_url` an earlier `finish` recorded is still on
  the row, so the `back` derivation — `pr_repair_origin` and `resumed_from` only
  (daemon.py:4083-4085) — parks the give-up in `waiting_pr_merge` and it leaves the user's
  list. Fix, one line in that same derivation, in the same shape as its two existing
  terms:

  ```python
  back = ("needs_review" if invariants_mod.validation_escalated(store, fresh)
          else ops_mod.pr_repair_origin(...) or ops_mod.resumed_from(...)
          or "waiting_pr_merge")
  ```

  `validation_escalated` is the predicate `true_blockers` already uses, so the status and
  the flag are derived from one fact.

### 4. Resumes take the free slot before new dispatches — stated

No behaviour change. `Daemon.tick` already calls `retry_paused_turns` at daemon.py:709
and `dispatch_pending` at daemon.py:739, so a resume wins the slot today; verified, not
assumed. `deliver_messages` (daemon.py:1503-1505) already states this rule for its own
pass and is the wording precedent.

**The rule:** when a slot frees, it goes to a paused turn being resumed before it goes to
a pending work order being dispatched.

**The reason:** a resumed conversation is cheaper than a fresh one and it is already
somebody's work in progress. A dispatch starts a new session that will re-send its whole
context at the cache-WRITE rate for every turn of its life; a resume continues one whose
prompt prefix is already paid for, and which is holding a user's half-finished task,
possibly a worktree, and possibly dependents. Starving it in favour of new work is the
one direction of unfairness that costs money AND time.

Written in two places:

* `Daemon.retry_paused_turns`'s docstring, in the paragraph that already explains the
  fleet cap (daemon.py:1382-1389) — it is that pass's own guarantee.
* A comment beside `if retry_paused:` at daemon.py:708, where the tick orders its passes,
  in the shape of the comments already there ("Before delivery, not after: …").

Pinned by a test (§5), because the ordering is invisible: reordering two lines in `tick`
would break it with no other symptom.

## Tests

`tests/`, targeted only — CI runs the suite. One per part; where a test cannot fail
before the change, that is said.

| # | file | test | fails before |
|---|---|---|---|
| 1 | `tests/test_fleet_cap.py` | `test_a_cap_held_resume_says_so_on_the_record` | yes |
| 1b | `tests/test_fleet_cap.py` | `test_an_outage_hold_is_recorded_and_does_not_stall_the_sweep` | yes |
| 1c | `tests/test_fleet_cap.py` | `test_a_project_cap_hold_names_the_slot_it_waits_for` | yes |
| 2a | `tests/test_fleet_cap.py` | `test_a_cap_held_order_does_not_promise_a_retry` | yes |
| 2b | `tests/test_parked_retry.py` | `test_the_overdue_invariant_is_quiet_under_a_cap_hold_and_returns_after` | yes |
| 3 | `tests/test_validation_loop.py` | `test_a_passed_round_does_not_land_an_order_whose_worker_is_typing` | yes |
| 3b | `tests/test_validation_loop.py` | `test_a_give_up_survives_the_turn_that_follows_it` | yes |
| 4 | `tests/test_fleet_cap.py` | `test_a_due_resume_takes_the_slot_before_a_pending_dispatch` | **no** — pin |

1. Build on `test_the_retry_pass_is_staggered_by_the_cap`
   (tests/test_fleet_cap.py:328), which already stages four siblings refused by one window
   under `max_in_flight=2`. After the capped tick, assert the two held orders each carry a
   `retry_held` event with `cause == "fleet_cap"`, `in_flight == 2`, `cap == 2` and the
   held pause's `seq`; then tick again inside `RETRY_HELD_RESTATE` and assert no second
   event. Today: zero events, so the first assertion fails.
1b. The OTHER cause behind the same skip at daemon.py:1397, and the one that dereferences
   `state.outage` inside the per-order loop — an exception there stalls every resume in
   the project, not only this order's. Two refusals in one project: one whose reset is in
   the future (it is what `fleet.read` builds the outage from, so `state.shut()` holds)
   and one that is due. Sweep the project; assert the due order carries one `retry_held`
   with `cause == "fleet_outage"` and `reopens_at == state.outage.reopens_at`, and the
   not-due one carries none — it was never going to relaunch. Then both halves of §2a's
   `fleet_outage` split: `pause_note` is `""` while the status is in
   `FLEET_HELD_STATUSES`, and is `the Claude usage window is spent, reopening at …` once
   it is `needs_review`. Today: zero events.
1c. The project cap, with the fleet deliberately wide open (`max_in_flight=50` and an
   unblocked `Fleet`) so nothing else can be the hold: `max_concurrent=1` filled by a
   `running` order, plus a due pause on a `needs_review` order, which
   `resume_spends_slot` says takes a slot. Assert one `retry_held` with
   `cause == "project_cap"`, `active == 1`, `max_concurrent == 1`, and `pause_note` ==
   `waiting for a free slot in this project (1 of 1 running)`. Today: zero events.
2a. Same fixture. `invariants.status_label(store, held_wo)` contains
   `waiting for a free in-flight slot` and does NOT contain `retrying by itself`. Today it
   is the second string.
2b. In `tests/test_parked_retry.py`, beside its existing INV-PAUSE-OVERDUE coverage
   (tests/test_parked_retry.py:267): stage a due pause, write a `retry_held` at
   `now`, advance the clock past `PAUSE_OVERDUE_GRACE`, assert `check_paused_turns_resume`
   yields nothing; then advance past `RETRY_HELD_FRESH_FOR` and assert the violation is
   back. Today the first half fails — the violation fires.
3. In `tests/test_validation_loop.py`: an order with an open round and a held turn
   (`fake_claude.hold_turns()`), an injected validator returning `passed`. Assert the
   round closed `passed`, the `validation_passed` event exists, the status is still
   `running`, and a `validation_landing_deferred` event was written; then settle the turn,
   run a tick, and assert it lands (`waiting_pr_merge` with a `pr_url`, `needs_review`
   with a pending assumption). Today the status is `needs_review` while the turn runs.
3b. The mirror of 3, for §3.3's `back` line. `max_rounds=1` and a rejecting validator, so
   the first round escalates, on an order that has already finished with a `pr_url`.
   Assert `needs_review` + `VALIDATION_STUCK_BLOCKER` at the give-up; then run the turn
   that starts AFTER it — a queued message un-parks the order, asserted `running` — hold
   it open, settle it and tick. Assert the status is still `needs_review` and the blocker
   still derives. Today the settler's `pr_url` branch parks it in `waiting_pr_merge` and
   the flag goes with the status, taking a refusal off the user's list with nobody having
   decided anything.
4. A pin, and it passes before the change: one free fleet slot, one due paused order and
   one `pending` order in the same project; one tick; assert the paused order has a new
   running turn and the pending one is still `pending`. It fails only if someone reorders
   `tick`, which is what it is for.

## Rejected alternatives

* **Render the cap hold live, from `Fleet`, instead of recording an event.** The obvious
  fix, and it is what `status_label`'s `pending` branch does (invariants.py:1246-1251). It
  cannot work here: the sweep's decision depends on `budget` and `in_flight` at the moment
  it ran, and by the time a surface asks, the fleet may be idle while the order is still
  parked — a surface reading live state would say "nothing is in your way" about an order
  that was, and INV-PAUSE-OVERDUE could not be suppressed at all, because it would have no
  way to know the sweep had ever seen the order. The event is not a cache of derivable
  state; it is the only record that a decision was taken.
* **Widen `PAUSE_OVERDUE_GRACE` so a cap hold never trips it.** Trades a false alarm for
  blindness: the grace is what bounds how long a genuinely broken sweep stays invisible,
  and a cap hold can last hours. The 12-hour silence in PR 129 is what that buys.
* **Suppress INV-PAUSE-OVERDUE whenever the fleet is capped right now.** Fleet-wide state
  at check time, not a statement about this order at hold time — it would suppress a
  genuinely stuck order that happens to share a busy tick, which is the invariant's exact
  subject matter.
* **Make `retry_paused_turns` exempt the order it is holding from the fleet cap.** Removes
  the symptom by removing the cap. The cap exists because four siblings resuming together
  re-wrote 1.37M tokens on 2026-09-02 (tests/test_fleet_cap.py:328-333).
* **Make the round machine wait for the live turn instead of deferring the landing.** A
  worker turn can run for hours; the validate thread is single and would be held by one
  order's conversation, stalling every other project's reviews. Recording the deferral
  costs one event and one predicate on a path that is already re-derived every tick.
* **Have `_validate_work_order` cancel or nudge the live turn.** The round's verdict is
  about a commit; the turn may be doing something newer. Killing a worker to make a status
  write safe is a larger blast radius than the status write.
* **A `landing_deferred` column on `work_orders` instead of two events.** Refused by
  project_store.py:281-285's rule: a state that can be derived gets no column. The pair of
  events also keeps the WHY on the record, which a boolean would not.
