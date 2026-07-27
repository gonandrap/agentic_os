# Post-conditions — the OS checking its own state

**Status:** implemented (v1: five invariants, daemon-enforced, `jarvis doctor`)
**Motivation:** two live work orders, six days apart, told the user they were blocked on
a question that did not exist.

## 1. Problem

The OS has plenty of machinery for *doing* things and none for *checking that the thing
it did still holds*. Every write is trusted the moment it returns.

The failure that motivated this, reconstructed from a real work order's event log:

```
23:07:55  assumption recorded (×2)   → attention = "assumptions pending review"   ✅
23:08:12  status → needs_review, finished with a summary                          ✅
23:08:43  worker's final reply captured                                           ✅
23:09:31  Claude Code's idle Notification fires
          hook stamps  attention = "Claude is waiting for your input"             ❌
```

Every component behaved correctly. The worker did its job; the assumptions were stored
and rendered in the UI with approve/reject controls; the status machine was right. The
Notification hook was doing its documented job — it just applied its status guard to the
*status* field and not to the *reason* field.

The result was a dashboard that said a finished work order was blocked on input, while
the actual pending action sat below the misleading banner. It happened to both work
orders the user ever created, and it is why they stopped using the OS.

**The general shape:** an action's post-condition was correct at write time and false
ninety seconds later, and nothing looked again. An append-only event log cannot catch
this — it faithfully records both the correct write and the clobber, and draws no
conclusion. *The audit trail proves what happened, not what is true.*

## 2. Design

### 2.1 Steady-state predicates, not assertions

The distinguishing choice. A write-time assertion would have **passed** on the sequence
above: at 23:08:12 the reason was correct. Invariants are therefore predicates over the
database *as it currently is*, re-evaluated on every reconcile tick — the only formulation
that catches state which was right when written and went wrong afterwards.

### 2.2 One derivation, many surfaces

`true_blockers(store, wo)` derives, from state alone, the ordered list of reasons a work
order needs the user. It is the single source of truth for "what does this want from me",
and every surface — the attention strip, `jarvis status`, the invariants — must agree with
it. A surface disagreeing is a bug in that surface.

This is what makes the checker cheap: it does not encode a second model of correctness,
it compares stored state against a derivation of the same state.

### 2.3 Repair only what is unambiguous

A violation with exactly one correct resolution derivable from state is repaired
automatically and the repair is recorded on the work order's timeline (`invariant`
event, rendered as "OS self-check repaired this"). Anything else is reported and left
alone — a wrong automatic repair is worse than a reported violation.

### 2.4 No LLM, ever

These are SQL-level predicates. The checker must be more trustworthy than the thing it
checks, which rules out anything nondeterministic. This is the "deterministic control
plane" rule applied to the control plane's own correctness.

### 2.5 Report once

Callers dedupe on `(invariant, wo_id)`. A standing violation is one problem, not one per
tick — otherwise the timeline and the inbox become the noise they exist to cut through.

## 3. The v1 invariant library

| id | predicate | repair |
| --- | --- | --- |
| `INV-ASSUMPTION-PERSISTED` | every `assumption` event has a matching row | rebuild the row from the event payload (content-matched, so never duplicates) |
| `INV-ATTENTION-REASON` | a flagged order's reason names the real blocker | rewrite to the derived reason |
| `INV-ATTENTION-PHANTOM` | a completed/cancelled order is not flagged | clear the flag |
| `INV-ATTENTION-MISSING` | an order that needs the user says so | raise the flag with the derived reason |
| `INV-ATTENTION-BLANK` | a flagged order has a non-empty reason | fill from the derivation, else report |

`INV-ATTENTION-REASON` enforces only the assumptions case. For other blockers a
hook-supplied reason ("needs permission to run npm test") is more specific than anything
derivable, and overwriting it would repeat the original bug in the other direction.

`INV-ATTENTION-MISSING` is the most valuable of the five and the least obvious: work that
needs the user but is not flagged appears on no surface at all. That is how a fleet stops
moving without anyone noticing.

## 4. Surfaces

- **Daemon** (`Daemon.check_invariants`): runs after reconcile on every reconcile tick,
  with repair enabled. Unrepairable violations raise a notification through the normal
  inbox path.
- **`jarvis doctor [project] [--repair] [--json]`**: read-only by default. Exit code 1
  when violations exist, so it works as a CI or cron check. `--catalog` makes it usable
  before the OS has ever been started.

## 5. Why this is prerequisite, not a feature

The obvious next step for the OS is autonomy — more delegation, learned policy,
self-improvement. All of it selects among options using signals the OS emits about
itself. A system that cannot detect that its own state has gone quietly wrong emits
signals that are quietly wrong, and everything built on top inherits the error.

Note in particular that no amount of friction-mining or self-improvement machinery finds
the motivating bug: the OS emitted no distress signal, because it believed it was fine.
The user's disengagement was the only signal, and it arrived as churn rather than as data.

Verification is therefore upstream of evolution, not part of it — proprioception before
a nervous system.

## 6. Extending it

Write a `_check_*` generator yielding `Violation`s in `src/jarvis/invariants.py` and
register it in `INVARIANTS`. Give it a stable id: ids appear in work order timelines and
in `jarvis doctor` output, so renaming one rewrites history.

Candidates deliberately left for later, each needing a decision first:

- **Evidence bundle** — a work order may not report `done` without a passing test/lint
  exit status. Needs work orders to carry machine-checkable acceptance criteria, stated
  at creation by the human.
- **Reply captured** — a settled work order whose worker turn produced no recorded final
  message (the supervisor's async result file lost the race). Today that is a
  `worker_reply_lost` event nobody reads.
- **Session liveness** — a `running` work order whose session no longer exists. The
  reconciler already handles this; making it an invariant would let the checker catch a
  reconciler regression.
- **Settings drift** — injected project settings still match the catalog.
