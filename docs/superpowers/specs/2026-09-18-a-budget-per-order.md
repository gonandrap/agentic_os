# A token budget per order, enforced on every Claude call

**Status:** implemented (wo-61173704)
**Code:** `src/jarvis/budget.py`, and the six places that call it.

## Why

Four work orders — wo-43c4c665, wo-ec886454, wo-61e842d2, wo-b2f88616 — consumed 82.1M
tokens between them, about $101 at list prices, and exhausted the account's window.
Nothing stopped them and nothing could have: there was no per-order ceiling anywhere in
this OS. The accounting was verified correct against the raw transcripts, so this was
never a reporting problem. It was a missing control.

## 1. What `claude --max-budget-usd` actually does

None of this is in `claude --help`, which says only "Maximum dollar amount to spend on API
calls (only works with --print)". Every statement below was measured against the real API
on 2026-09-18, and the whole design rests on them.

**The cap is PER INVOCATION, not per session.** A session that had spent $0.0327 on its
first turn was resumed under `--max-budget-usd 0.01` and ran to completion, spending
another $0.0072. Prior spend on the same session id does not count, and `total_cost_usd`
on the resumed envelope reports that invocation's spend alone.

*Consequence:* the running total is Jarvis's to keep. Each turn is handed
`budget − spent so far`, recomputed in `worker_session.briefing_for` on **every** turn. A
value resolved once at dispatch would let a ten-turn work order spend ten budgets.

**It is checked BETWEEN API calls, so it overshoots.** A run capped at $0.001 spent
$0.0471 — 47× — because the first call was already past the line when the check ran.

*Consequence:* the flag is a stop signal, not a hard limit. The bound is "one API call of
overshoot", and a turn's first call against a 400k context is not cheap. Nothing in the OS
presents the remaining budget as a guarantee, and the escalation quotes what was **spent**,
not the cap.

**On exhaustion the process exits 1 and still writes a usable result JSON:**

```json
{"type": "result", "subtype": "error_max_budget_usd", "is_error": true,
 "terminal_reason": "budget_exhausted", "total_cost_usd": 0.0471047,
 "errors": ["Reached maximum budget ($0.001)"], "modelUsage": {...}}
```

What it does **not** carry is the `result` field. So `claude_cli.read_turn_result` falls
back to `errors`, or the record would read "turn reported is_error" about a turn whose own
envelope said why. `total_cost_usd` and `modelUsage` are accurate for the invocation, so
the last turn is billed like any other.

**Partial work is preserved and the session is resumable.** The transcript is written in
full, tool calls already made have taken effect, and resuming the exhausted session under a
larger cap continued the same conversation with its context intact. That is what makes
top-up-and-resume buildable rather than merely designable, and it is why the new state is
open rather than terminal.

**Subagents count against the same cap** — the envelope's `subagent_stats.refused.budget`
records the ones the CLI declined to spawn.

## 2. Which dollars the ceiling governs

**The order's whole bill, as `jarvis cost <id>` reports it**: the worker's own turns plus
what Jarvis spent on that order — Neo answers, panel seats, supervisor reviews, digests.
Not the worker session alone. Ruled by the user through Neo on question 404: the number
they typed has to be the number they can check, and the two readings differ by ~13% on the
four orders above.

The consequence is accepted rather than worked around: the worker's per-turn ceiling
shrinks as the panel spends, and **a panel round can carry an order past its cap with no
worker turn running at all**. So `budget.exhaustion` answers from the accounting
(`wo_turns.cost_usd` + `agent_calls.cost_usd`, two indexed sums) and treats the turn's exit
code as corroboration only. A check keyed on `terminal_reason` would miss every
Jarvis-side overrun and go on dispatching an order that had already spent its budget.

## 3. A feature order's budget is a family budget

It bounds the whole rollup `jarvis cost <fo-id>` adds up — planner, manager, every child.
Allocation is **reserve-on-dispatch** (option A, ruled on question 404):

- a child claims a slice of the feature's *unreserved* remainder at the moment it is
  dispatched, split equally across the children not yet settled and not yet holding one;
- that slice becomes the child's own ceiling (`work_orders.budget_reserved_usd`);
- release needs no write: a settled child drops out of `held`, and its real spend is
  already inside the pool's total.

The invariant, asserted directly in `tests/test_budget.py`:

> already spent + everything live children may still spend ≤ the budget

It holds because a reservation is only ever cut from `unreserved`, which already has every
outstanding reservation subtracted from it. **Two children dispatched in parallel are
therefore never handed the same remainder** — which the alternative (re-read the feature's
remainder on every turn) cannot say: it is always *true*, and still lets N live children
each spend the full remainder once, overshooting by up to (N−1) turns.

Neo attached one condition: when a child exhausts its slice, the escalation states the
feature's unreserved remainder, so the user can top up rather than guess why a funded
feature stalled. `Exhaustion.reason` carries it.

## 4. The new state

`budget_exhausted`, in both `WO_STATUSES` and `FO_STATUSES`. A status of its own rather
than a reuse: `failed` says the work went wrong and `needs_review` says a judgement is
wanted, and neither is true of an order that was doing fine and ran out of money.

**Open, not terminal.** Terminal would make it a `DEPENDENCY_DEAD_STATUS` — every
dependent stranded, the parent feature failed — over a number the user can change in one
command. It is also what leaves resuming possible.

- It is in `NOT_RETRIED`, not `RETRY_SWEEP_STATUSES`: relaunching would spend money nobody
  authorised, and the sweep cannot ask.
- `invariants.true_blockers` **re-derives** its reason from live accounting. Attention is
  rebuilt from state on every reconcile tick, so a line only `budget.escalate` knew how to
  write would be overwritten by a generic one on the next tick.
- `worker_session.delivery_hold` holds queued messages rather than failing them: nothing is
  wrong with the message, and raising the budget is exactly what sends it.

**Raising the budget resumes it**, in the same session (`ops.set_work_order_budget`). The
relaunch goes through `worker_session.retry`'s rules — a nudge when the conversation
already reached the model, the original prompt when it did not — so the worker is never
told to redo what it has already done. Raising it by *less* than the overshoot leaves the
order where it is and says so, rather than launching a turn the CLI would stop on its first
call.

## 5. Surfaces

| Where | What |
|---|---|
| `jarvis wo create --budget` / `jarvis fo create --budget` | set at creation |
| `jarvis wo budget <id> [USD] [--clear]` | show, set, raise, clear — and resume |
| `jarvis fo budget <id> [USD] [--clear]` | the same, for the family |
| `jarvis wo show` / `fo show` | a `budget` block, always present |
| dashboard | the ceiling, the spend, and the control, on both order pages |
| catalog | `worker.budget_usd`, `worker.feature_budget_usd`, and the `os.defaults` twins |

Two catalog settings rather than one scaled from the other: a feature's children are not
known when the default is written, so a per-work-order number says nothing useful about a
family. The defaults are resolved at **creation** and stamped onto the row — unlike
`autocompact_window`, which is re-read every turn. A budget is a contract about one order:
`jarvis wo show` has to be able to state it, and lowering a fleet default must not strand
work already authorised at the old number. Both reach the config version ledger.

## 6. Default off

No budget means no ceiling — no flag on the argv, and the new status unreachable. An OS
that started refusing to work because of a number nobody set would be worse than the
problem it solves. `DEFAULT_BUDGET_USD` is `None`, and
`test_an_unbudgeted_order_behaves_exactly_as_before` checks it on the same path the capped
case runs down.

## 7. Decisions taken here, and what would reverse them

**No minimum viable turn budget.** `Ceiling.exhausted` is `remaining <= 0`, with no floor.
A floor was considered: the first API call of a turn against a large context costs ~$0.03–
0.05, so a few cents of remainder does buy one wasted call. It was rejected because any
floor makes the effective cap `budget − floor`, which is not the number the user typed, and
being able to check the number is the whole point. Reverse this if wasted final turns turn
out to cost more than the confusion a floor creates.

**No second enforcement in Python.** The work order was explicit: do not build a watcher
that kills the process, which would race the call it is trying to bound. The CLI's own flag
is the enforcement point; everything in `budget.py` computes the number handed to it and
decides what the order does afterwards.

**Topping up a feature does not re-fund its children.** They stay in their own
`budget_exhausted` until each is topped up. The family has money again, but which child
gets it is the user's call — silently re-funding every one would spend the new budget on
whatever happened to be running rather than on what the user meant to rescue.
