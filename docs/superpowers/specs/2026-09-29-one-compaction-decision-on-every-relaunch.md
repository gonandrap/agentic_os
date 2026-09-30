# One compaction decision on every relaunch path

**Work order:** wo-725af9df · **Issue:** #866 · **Measured:** 2026-09-29 · **Status:** spec

Predecessors: `docs/superpowers/specs/2026-09-18-compact-past-the-ttl.md` (the decision and
the floor), issue #843 (`resume_compaction_due`, jarvis-0.10.31, commit 0154d7d), issue
#856 (`measured_context`, commit 36cf40e).

## 1. The problem

### 1.1 The issue's headline is stale — the defect is smaller and sharper

#866 reports 61 ttl-expiry re-writes against 17 compactions over the last 25 production
work orders and asks for a cost model to widen the trigger. Re-measured against
`wo_turns` in `~/workspace/agentic_os/.jarvis/jarvis.db`, over 11 days: **140** TTL-expired
turn boundaries carrying over 100,000 tokens of measured context, **71** of which
compacted. Restricted to boundaries AFTER `resume_compaction_due` shipped (2026-09-28):
**9 of 11**.

So #843 plus #856 already closed the bulk, and the ratio in the issue is dominated by
boundaries that predate the fix. What is left is not a coverage percentage — it is a
short list of relaunch paths that never ask the question at all. Two are observed, one
is reachable and not yet observed.

### 1.2 Survivor 1 — the budget-raise resume

`ops._resume_after_budget` (src/jarvis/ops.py:12779; the issue and the brief call it
`resume_after_budget_raise` — that name does not exist in the tree) builds a synthetic
`TurnPause(reason=PAUSE_BUDGET)` and calls `worker_session.retry` at
src/jarvis/ops.py:12814. It never asks `compaction_due` or `resume_compaction_due`.

Observed: wo-35fc3de7, seq 8 → 9. 115,829 tokens of measured context, 8-minute gap (past
`usage.WRITE_TTL_SECONDS` = 300), turn 8 ended in `budget_exhausted`, turn 9 relaunched
cold and re-wrote the whole conversation at the cache-WRITE rate.

The path is structurally invisible to the daemon: `turn_pause` returns None for a budget
stop by design (src/jarvis/ops.py:12804-12807 states it), so `Daemon.retry_paused_turns`
— the only caller that does ask — can never see this order.

### 1.3 Survivor 2 — message delivery over a NON-RESUMABLE pause

`worker_session.compaction_due` refuses every boundary whose last turn is `failed` with a
`turn_pause`, blanket (src/jarvis/worker_session.py:487-488). Its docstring gives two
reasons, and both are about the RETRY path:

* `_nudge` tells the worker "the conversation above is intact", which a compaction makes
  false;
* `turn_pause` is re-derived from the LATEST turn, so a compact turn behind a paused one
  erases the pause and the relaunch never comes.

`delivery_hold` already holds delivery for every RESUMABLE pause
(src/jarvis/worker_session.py:307-309). The only pause that therefore reaches
`Daemon._compacted_first` is one that will never be retried: no nudge is coming, and
erasing a pause nobody is waiting on costs nothing. The exclusion is over-broad for its
only remaining caller, and `tests/test_compaction.py:343` currently asserts the
over-broad behaviour as intended.

Observed: wo-cc7b356f, seq 11 → 12. 109,967 tokens, 328-minute gap, turn 11 failed with
"the turn's process ended without writing a result", turn 12 an ordinary message
delivery. Cold, over the floor, refused solely by the pause test.

### 1.4 Survivor 3 — dispatch-resume (reachable, no instance yet)

`worker_session.start` launches with `resume=_conversation_started(...)`
(src/jarvis/worker_session.py:347-352) precisely because a work order can reach dispatch
a second time with its session and worktree intact: `ops.defer_red_release`
(src/jarvis/ops.py:4232) re-parks a running release order to `pending`, and
`Daemon.dispatch_pending` → `dispatch.dispatch_work_order` → `worker_session.start`
(src/jarvis/dispatch.py:1310) then relaunches an existing conversation. `RED_HOLD_SECONDS`
guarantees the gap is past the TTL. No compaction decision anywhere on that path.

### 1.5 Root cause

Every one of the three is the same defect, and it is not "a path was missed": **the
compaction decision is opt-in.** A relaunch path is correct by remembering to call
`compaction_due` before `_launch`, and nothing detects a path that does not. kn-6352bc0f
("no future path forgets") is asserted by review, not by the code or the suite. Two paths
already forgot; `_resume_after_budget` forgot within a release of #843 shipping.

What is NOT the root cause, despite being what the issue asks for: the floor. See §5.

## 2. Secondary defect found while reading

`compact_then_resume` (src/jarvis/worker_session.py:559-564) queues the relaunch message
and THEN calls `compact`. When `compact` raises `budget.BudgetExhausted`, the queued
message stays queued. `Daemon.retry_paused_turns` escalates and parks the order; when the
user tops it up, `ops._resume_after_budget` calls `retry` — and the still-queued relaunch
also goes out later. Two relaunches of one lost turn. Unobserved, latent since #843, and
the fix below closes it (§3.3, §3.5).

## 3. The fix

One entry point that decides AND acts, called by every relaunch path, plus a test that
discovers the paths itself.

### 3.1 The entry point

New in `src/jarvis/worker_session.py`, next to `compact`:

```python
def compact_before_relaunch(store, project, wo, min_context, *,
                            pause: TurnPause | None = None,
                            queue: str | None = None,
                            now: float | None = None) -> dict[str, Any] | None:
```

Returns the compaction turn row when it launched one — and then the caller must NOT
launch its own turn on this tick — or None, and the caller proceeds exactly as it does
today. `pause` and `queue` are mutually exclusive (assert it).

Body: `due = compaction_due(...)`; None → return None; otherwise
`compact_then_resume(store, project, wo, pause, due)` when `pause` is given,
`_compact_after_queueing(...)` when `queue` is given, `compact(store, project, wo, due)`
when neither.

**It raises, it does not swallow.** The three callers have three different and correct
policies — `_compacted_first` swallows everything so a saving never costs a delivery,
`retry_paused_turns` escalates `BudgetExhausted` and logs `ClaudeCliError`,
`_resume_after_budget` reports rather than raises. A shared swallow would silently delete
`retry_paused_turns`' budget escalation.

**Decision and action in ONE function, on purpose.** A caller that can call the predicate
and ignore the answer is the defect of §1.5 with a shorter name. It also gives the
discovery test something checkable: "this relaunch path reaches the shared decision"
becomes "this function calls `compact_before_relaunch`".

### 3.2 The two shapes collapse into one predicate

`compaction_due` gains `pause: TurnPause | None = None`; `resume_compaction_due` is
DELETED and becomes its pause branch. Justification: after §3.4 narrows the pause
exclusion the two bodies differ in two lines only — which turn supplies `ended_at`
(`store.latest_turn` vs `pause.turn`), and the scope of the `COMPACT_TURN` guard. They
already share the TTL test, the floor test, `measured_context` and the `min_context is
None` off switch. Two exported predicates is what let a caller pick neither.

The two ACTIONS stay two (`compact`, `compact_then_resume`) plus the new
`_compact_after_queueing`: they genuinely differ in whether a relaunch is queued first
and which event is written, and no caller chooses between them any more — the entry point
does.

Branch structure of the merged `compaction_due`:

* shared: `min_context is None` → None; TTL; floor via `measured_context`; the boundary
  turn must not be a `COMPACT_TURN`.
* `pause is None`: today's body — no turn on record, turn in flight, last turn is a
  compaction — plus the narrowed pause test of §3.4.
* `pause is not None`: today's `resume_compaction_due` body, anchored on
  `pause.turn["ended_at"] or pause.turn["started_at"]`, guarding
  `pause.turn["kind"] != COMPACT_TURN`. The stored pause is NOT re-read: the caller owns
  it, and `ops._resume_after_budget`'s is synthetic and is not on record at all.

### 3.3 Withdraw a queued relaunch when the compaction cannot launch

In `compact_then_resume` and `_compact_after_queueing`, wrap the `compact` call: on any
exception, `store.mark_message(msg_id, "failed")` and re-raise. Queue-first stays
(erasing the pause before the relaunch is queued is the stall #843 exists to prevent);
this makes the failure path leave nothing behind. Closes §2.

### 3.4 Narrow `compaction_due`'s pause exclusion to a RESUMABLE pause

```python
if turn["state"] == "failed":
    p = turn_pause(store, wo["id"])
    if p is not None and p.resumable:
        return None
```

Rewrite the docstring bullet to state the new, true reason:

> * The last turn is paused AND RESUMABLE. That pause is `Daemon.retry_paused_turns`'
>   business and `compaction_due(..., pause=…)` is how it asks — compacting here would
>   make `_nudge`'s "the conversation above is intact" false, and a compact turn behind a
>   paused one ERASES the pause (`turn_pause` re-derives it from the LATEST turn), so the
>   relaunch it was waiting for would never come. A NON-resumable pause has neither
>   problem: no relaunch is coming, so no nudge will lie and there is no pause anyone is
>   waiting on. `delivery_hold` holds the delivery for a resumable pause — but the
>   decision must not depend on the caller having checked.

Keep the position of the test last: it is the only exclusion that costs a second query.

### 3.5 Wire `ops._resume_after_budget`

At src/jarvis/ops.py:12811-12814, between `set_status(..., "running")` and the `retry`
call:

1. **Guard against a relaunch already queued.** If any of
   `store.queued_messages(wo["id"])` has `source == worker_session.RESUME_SOURCE`, do not
   retry. Set `running`, clear attention, return
   `(True, "its queued relaunch goes out on the next tick")`. Without this the sequence
   "compaction launched, then the delivery of the queued relaunch exhausts the budget
   again, then the user tops up again" produces two relaunches of one turn.
2. **Ask the shared decision**, passing the synthetic pause:

```python
turn_row = worker_session.compact_before_relaunch(
    store, spec, wo, catalog_os.compact_min_context, pause=pause)
fresh = turn_row or worker_session.retry(store, spec, wo, pause)
```

inside the existing `try`, so both existing handlers keep their behaviour: on
`BudgetExhausted` it escalates and returns `(False, "still has no headroom — it stopped
again immediately")`; on anything else it returns `(False, "budget raised, but the
relaunch failed (…)")` and parks `pending`. Best-effort is preserved — nothing new
raises out of this function.

The relaunch prompt is not lost when the compaction wins: `compact_then_resume` queued it
as a `RESUME_SOURCE` message before the compaction started, `delivery_hold` holds it
while the compaction turn is busy, and `Daemon.deliver_messages` sends it on the tick
after it settles. The `budget_resumed` event then records the COMPACTION's seq, so add
`"compacted": True` to its payload — otherwise the timeline reads as if the worker
resumed directly.

`compact_min_context` comes from the catalog this function already resolves
(`resolve_catalog()` at src/jarvis/ops.py:12798 — take `.os.compact_min_context` off the
same object rather than re-resolving).

**A compaction spends budget too, and this order just left `budget_exhausted`.**
Three cases, all decided:

* The compaction itself is refused by `budget.exhaustion` inside `_launch`: it raises
  before `create_turn`, so no turn row exists, §3.3 withdraws the queued relaunch, and
  the existing handler escalates. Identical to today's outcome for a refused `retry`.
* The compaction launches and the new ceiling is spent by the compaction turn: the
  relaunch message stays queued, `Daemon._deliver` catches `BudgetExhausted` and
  escalates, the order parks in `budget_exhausted` with the relaunch still queued, and
  the next top-up takes branch 1 above rather than retrying.
* The compaction launches and there is headroom: ordinary delivery on the next tick.

The compaction is worth spending the raise on because the boundary was going to be paid
either way — a 115,829-token order such as wo-35fc3de7 pays the 1.25x re-write out of the
same raised budget if it does not compact.

### 3.6 Wire the dispatch-resume path

The call goes in `dispatch.dispatch_work_order`, immediately before
`worker_session.start` (src/jarvis/dispatch.py:1310) and AFTER `budget.reserve` and the
`resolved` write, so a compacted re-dispatch leaves the same resolved row as a launched
one:

```python
compacted = worker_session.compact_before_relaunch(
    store, project, wo, cfg.compact_min_context, queue=prompt)
```

No extra "is this a re-dispatch" guard: `compaction_due` returns None when there is no
turn on record, so an ordinary first dispatch is byte-identical to today.

When it returns a turn, the prompt that was going to be launched **is the queued
message** — `_compact_after_queueing` writes it with `source=RESUME_SOURCE`, and delivery
sends it into the summarised conversation on the tick after the compaction settles. On
that branch `dispatch_work_order`:

* skips the seq-1 context ledger — there is no dispatch turn, and `_launch` records the
  compaction turn's own row;
* calls `store.clear_dispatch_attempts` and `store.set_status(wo["id"], "running")`. The
  claimed-but-no-turn state `settle_work_order` fails on is not reachable: the compaction
  IS a live turn.
* writes `dispatch_deferred_for_compaction` (`{"turn": <compaction seq>, "msg_id": …,
  **resolved}`) instead of `dispatched`, because no worker prompt has been sent yet. The
  audit trail for a compacted re-dispatch is therefore
  `dispatch_deferred_for_compaction` → `compacted` → `message_delivered`, and
  `timeline._describe` needs one line for the new event kind.
* calls `central.touch_project(project.name)` and returns
  `store.get_work_order(wo["id"])`, as today.

### 3.7 The discovery test

`tests/test_relaunch_paths.py`. No fleet fixture, no DB — it reads source.

1. Parse `src/jarvis/worker_session.py`, `src/jarvis/daemon.py`, `src/jarvis/ops.py` and
   `src/jarvis/dispatch.py` with `ast`. **dispatch.py is in the list although the brief
   named three files**: src/jarvis/dispatch.py:1310 is the only call site of
   `worker_session.start` in the tree, so omitting it would exempt the §1.4 path from the
   very test written to catch it.
2. Collect every `ast.Call` whose func is an `Attribute` with `attr` in
   `{"start", "send", "retry", "_launch", "compact", "compact_then_resume"}` on a `Name`
   of `worker_session`, plus bare `Name` calls to the same inside `worker_session.py`
   itself.
3. For each, resolve the enclosing `FunctionDef`/`AsyncFunctionDef` to a qualified name
   (`module.Class.func` or `module.func`) using a parent map built in one walk.
4. A call site PASSES when either:
   * its enclosing function's body contains a call whose `attr`/`id` is
     `compact_before_relaunch`; or
   * one lexical hop: the enclosing function calls a same-module function or method
     (`self.x(...)` or `x(...)`) whose own body contains `compact_before_relaunch` — this
     is what covers `Daemon._deliver` → `Daemon._compacted_first`; or
   * its qualified name is a key of `EXEMPT`.
5. `EXEMPT: dict[str, str]` is declared at the top of the test module, qualified name →
   one-line reason. Its contents after this change are only `worker_session`'s own
   internals — `worker_session.start`, `.send`, `.retry`, `.compact` and
   `._compact_after_queueing` calling `_launch`, reason "the transport itself; the
   decision is above it". A new legitimate caller registers by adding a key with a
   reason, which puts the justification in the diff a reviewer reads.
6. On failure, assert with the full list of unregistered sites as `file:line
   qualified_name calls worker_session.X`, plus the two ways to fix it. **No `skip`, no
   `xfail`, and no silent pass when the parse finds nothing** — a zero-site result is
   itself a failure (`assert sites`), which guards against a module-alias rename making
   the test vacuous.

Neo's condition is met by 2 + 4 + 6: the test enumerates from the AST rather than from a
hand-written list of paths, and an unregistered caller fails loudly.

**Rejected for this test:** monkeypatching `_launch` at import time to assert a decision
was taken. It only covers paths a test happens to exercise, which is exactly the coverage
that missed §1.2 and §1.4.

## 4. Rejected alternatives

1. **Patch the three paths, no shared entry point.** Cheapest diff, and it is what #843
   did for one path. Loses to §1.5: `ops._resume_after_budget` was written after #843
   shipped and still skipped the decision. Four paths patched independently is four
   places for the fifth to be forgotten.
2. **Put the decision inside `_launch`** — "the one point every turn passes through"
   (src/jarvis/worker_session.py:622), which is where the budget check correctly lives.
   Loses for three reasons. A compaction is itself a `_launch`, so the decision would
   re-enter the function it lives in. `_launch`'s contract is "returns the turn row"; it
   has no way to tell a caller "your turn did not happen, do not mark the messages
   delivered". And the post-launch bookkeeping differs per path (`mark_message` +
   `message_delivered`; `turn_resumed` + status; `dispatched` + the context ledger), and
   only the caller can skip its own.
3. **Drop the pause exclusion entirely** instead of narrowing it to resumable. Loses: it
   re-opens the permanent stall — a compact turn behind a RESUMABLE pause erases it, the
   retry sweep never sees it again, and the order waits for ever.
4. **Lower the floor / build the cost model #866 asks for.** See §5: the model exists and
   already set the floor at the point where lowering it loses money.
5. **Sweep for cold, large conversations and compact them proactively**, outside any
   relaunch path. Loses: a compaction is only ever cheaper at a boundary that is ABOUT to
   be paid (`compact-past-the-ttl.md` §2.3). A sweep pays 1.0x for a re-write that may
   never happen.

## 5. The 100,000-token floor does NOT move

#866 asks for a cost model to decide when compaction pays. It already exists:
`scripts/compaction_cohort.py`'s module docstring is the model, and
`catalog.DEFAULT_COMPACT_MIN_CONTEXT` (src/jarvis/catalog.py:336-353) is its output, with
the 30-day bands to 2026-09-19 in the comment.

Re-run over this window — `scripts/compaction_cohort.py --days 11`:

| context band | net-positive | median |
|---|---|---|
| 50,000–60,000 | 17% | -$0.121 |
| 60,000–75,000 | 42% | -$0.043 |
| 75,000–90,000 | 42% | -$0.045 |
| 90,000–100,000 | 50% | -$0.001 |
| 100,000–125,000 | 78% | +$0.251 |

Every band below the floor has a NEGATIVE median. The floor sits where the majority
flips, and it flips between 90,000–100,000 and 100,000–125,000 in this window exactly as
it did in the 30-day one. Lowering it loses money on an unattended decision taken on
every work order. No change to `DEFAULT_COMPACT_MIN_CONTEXT`, `COMPACT_MIN_CONTEXT_MIN`
or `os.compact_min_context`.

## 6. Tests

New, in `tests/test_compaction.py` beside the existing exclusion tests (`fleet`,
`settle_turns`, `_age_last_turn`, `_big_context`, `_deliver`, `_turns` are all there
already), one per relaunch path, each on a cold conversation over the floor, each
asserting a `COMPACT_TURN` was launched and that what was going to be launched survives:

1. dispatch-resume — an order with turns on record re-parked to `pending`
   (`ops.defer_red_release` shape), dispatched again: compaction first, the dispatch
   prompt queued as `RESUME_SOURCE`, delivered on the next tick.
2. message delivery with NO pause —
   `test_a_cold_boundary_with_a_large_conversation_compacts_before_the_prompt` covers it;
   it must pass unchanged.
3. message delivery over a NON-RESUMABLE pause — REPLACES
   `test_a_pause_nothing_will_relaunch_reaches_the_decision_and_is_refused`
   (tests/test_compaction.py:343). Its premise section (`delivery_hold` returns None,
   past the TTL, over the floor) is reusable verbatim; its assertions invert — a
   compaction IS launched, and the message goes out after it.
4. paused-turn retry — `test_a_cold_relaunch_compacts_first_and_the_relaunch_survives`,
   unchanged behaviour through the new entry point.
5. budget-raise resume — raise the budget on an order in `budget_exhausted` with a cold
   115k conversation: a compaction turn, a queued `RESUME_SOURCE` relaunch, and
   `budget_resumed` carrying `"compacted": True`.

Negative cases that must keep passing, each named so a regression says which rule broke:

* a RESUMABLE pause is still refused by `compaction_due(..., pause=None)` —
  `test_a_message_queued_behind_a_paused_turn_never_compacts`
  (tests/test_compaction.py:310), unchanged;
* a warm boundary is refused (`test_a_boundary_inside_the_ttl_does_not_compact`);
* an under-floor conversation is refused
  (`test_a_small_conversation_past_the_ttl_does_not_compact`);
* a compaction is never launched behind another
  (`test_a_compaction_is_never_followed_by_another`) — assert it on the two new paths
  too, dispatch-resume and budget-raise;
* an opening dispatch, no turn on record, never compacts
  (`test_an_opening_turn_is_never_compacted_before`);
* `min_context is None` switches everything off
  (`test_the_off_switch_stops_it_and_nothing_else`), including the three new call sites;
* a compaction that cannot be launched never costs the delivery
  (`test_a_compaction_that_cannot_be_launched_never_costs_the_message`), plus the new
  §3.3 case: the queued relaunch is withdrawn, not left behind.

Plus `tests/test_relaunch_paths.py` per §3.7, and a `_nudge_after_compaction` case for
`PAUSE_BUDGET`: it currently falls through to "the work order was paused"
(src/jarvis/worker_session.py:1347-1349) while `_nudge` has an honest budget branch
(src/jarvis/worker_session.py:1317). Add the matching branch — the budget-raise path is
now a caller — and assert it does not tell the worker the transport failed.
