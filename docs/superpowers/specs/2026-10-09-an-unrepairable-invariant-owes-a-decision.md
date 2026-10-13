# An unrepairable invariant owes a decision

Work order wo-60a00de6. Root cause established by improvement order io-df4fa1e3; knowledge
entries kn-4b49d106 (severity inflation is not a visibility mechanism) and kn-06a0fb9e.

## The problem

An invariant the OS **cannot repair** has exactly one surface today: an inbox
notification. `Daemon.check_invariants` (src/jarvis/daemon.py:5169-5177) ends with

```python
if not v.repaired:
    store.add_notification(..., level=v.level, wo_id=v.wo_id, source="invariants")
```

and never calls `flag_attention`. So "the OS fixed it" and "a person must decide something"
collapse into the same channel.

Evidence:

- **wo-1abd3886 sat `completed` with PR #925 open and MERGEABLE for 6.5 days**, invisible on
  `jarvis status`. INV-WORK-LANDED was firing the whole time. Its notification was one
  `warning` among 230 unacked inbox items.
- INV-WORK-LANDED is **correctly** non-repairable: merge / re-open / drop is the user's
  decision, stated in `check_work_lands`' own docstring (src/jarvis/invariants.py:3699-3701).
  Nothing is wrong with the checker. The disposition set is what is missing.
- Second half of the cause — a `completed` order is **settled**, so even a flag raised there
  could not survive:
  - `invariants.true_blockers` (src/jarvis/invariants.py:974) returns nothing for
    `TERMINAL_STATUSES = ("completed", "cancelled")`: every branch is gated on
    `OPEN_STATUSES`, `PR_REPAIR_STATUSES`, `needs_review`, `waiting_input`, `pending`,
    `failed`, `budget_exhausted`.
  - `check_no_phantom_attention` (INV-ATTENTION-PHANTOM, src/jarvis/invariants.py:2397)
    calls `store.clear_attention(wo["id"])` on **any** flagged terminal order,
    unconditionally. Next tick wipes the flag and spends the acks with it.
  - `check_blocked_work_is_surfaced` (INV-ATTENTION-MISSING) walks `BLOCKED_STATUSES`
    (src/jarvis/invariants.py:76), which has no `completed`. Nothing re-raises it either.

Root cause, named: **`Violation` has two dispositions where the domain has three.**
`repaired=True` means the OS acted; the default means notify-only. There is no way to say
"cannot repair, and a DECISION is owed". Patching INV-WORK-LANDED alone — raising its
`level` to `critical` so `ops.os_status`' seam picks it up — is the symptom fix, and
kn-4b49d106 is explicitly about why: severity inflation to buy visibility is what
`jarvis bug report --expedite` exists to avoid.

## The fix

A third disposition, persisted on the row the critical-violation seam already uses, routed
to the user through `true_blockers` like every other thing a work order owes.

### 1. `invariants.Violation.owed: str = ""`

New field beside `repaired` / `repair` / `level` (src/jarvis/invariants.py:543-568).
Non-empty means: not repairable, and a decision is owed by the user. Its **value is the
blocker sentence**, authored by the checker, naming the decision and the command that takes
it.

**Constraint on every present and future owed checker: the string must be free of elapsed
time and of anything else that changes tick to tick.** `ProjectStore.ack_attention`
(project_store.py:2782) stores the derived blockers **verbatim** in
`acknowledged_blockers`, and `true_blockers` filters by exact string equality against
`acknowledged(wo)` (invariants.py:1248). A reason carrying "6.2h" or a changing count can
never be acked down — it would come back as a new string on the next tick, for ever.

Why `owed: str` and not `owed: bool` reusing `detail`: that constraint has to be expressible
per checker. `detail` is free to carry minutes and does — INV-OS-HEALTH-SWEEP-DARK and
INV-STUCK-SWEEP-DARK both put elapsed time in it. Overloading `detail` would either break
those or make the time-free rule unstatable.

### 2. Persisted on the existing `violation_reports` row — no second path

`violation_reports` already carries `level` and `detail` precisely so `ops.os_status` can
build an attention item from state without re-asking the checker (Neo 1084; the
`ADDED_COLUMNS` note at project_store.py:1610-1617).

- Add `"owed": "TEXT NOT NULL DEFAULT ''"` to that **same** `ADDED_COLUMNS["violation_reports"]`
  entry.
- `ProjectStore.open_violation_report` (project_store.py:4464) takes `owed: str = ""` and
  writes it on INSERT and refreshes it on UPDATE, beside `level` and `detail`.
- New reader `ProjectStore.owed_violations(wo_id: str) -> list[dict]`: standing rows for that
  work order with `owed != ''`. A `WHERE wo_id=? AND owed<>''` read, ordered by `first_seen`,
  shaped like `standing_violations`.

The row is the dedupe. It survives process restart, which is the whole reason the table
exists rather than a set on the daemon object.

### 3. Routed through `true_blockers`, never flagged with a reason of its own

New branch in `invariants.true_blockers`: append each `store.owed_violations(wo["id"])`
row's `owed` string.

- **Ungated by status.** That IS the settled-order path — gating it is the bug.
- **Ranked after the `budget_exhausted` / `failed` block and before the status-specific
  waits.** Nothing it can co-occur with today: every later branch is gated on an open or
  parked status, and INV-WORK-LANDED only fires on `completed`. The position is documentary,
  for the next owed checker.
- `acknowledged()` filtering at the tail of the function is what makes `jarvis wo ack` put it
  down **for good** — no new ack machinery. `ops.ack_refusal` refuses only over pending
  assumptions, so an owed blocker is ackable.

Consequence, stated because it is load-bearing: `check_attention_reason_is_true`
(INV-ATTENTION-REASON, invariants.py:2197) re-derives `true_blockers[0]`, so the flag
**cannot be relabelled** by a Claude Code hook stamping "Claude is waiting for your input"
over it. Deriving the reason is what buys that; flagging with a reason of the daemon's own
would not.

### 4. `Daemon.check_invariants` raises the flag and withholds the notification

In the loop at daemon.py:5157-5177, for a violation with non-empty `owed` **and** a `wo_id`:

1. Pass `owed=v.owed` to `open_violation_report` — so the row exists **before** anything
   derives from it.
2. Re-derive `invariants.true_blockers(store, wo)` and, when the order is not already
   flagged and the derivation is non-empty, `store.flag_attention(wo["id"], blockers[0])`.
3. Run this **BEFORE** the dedupe `continue` at daemon.py:5162.

Why before the `continue`, and why that is idempotent rather than noisy:

- An **acked** blocker leaves `true_blockers` (step 3's filter), so the derivation is empty
  and the flag stays down for good. Re-running every tick cannot re-raise it.
- A flag cleared by **anything else** while the violation still stands comes back — the
  self-heal kn-2efe73e4 demands. This is the case the `continue` would lose:
  `check_work_lands` is in `SLOW_INVARIANTS`, so without the pre-`continue` placement the
  flag would only ever be raised on the one sweep tick that first saw the violation, and
  INV-ATTENTION-MISSING cannot put it back (`BLOCKED_STATUSES` has no `completed`).
- The existing `flag_attention` guard is "not already flagged", so no duplicate `attention`
  timeline event per tick.

**An owed violation gets NO `add_notification`.** The attention item replaces the inbox line;
it does not join it. Two surfaces for one decision is the double-report that put wo-1abd3886
in a 230-item pile.

A violation with neither `repaired` nor `owed` is **unchanged**: notification only, exactly
as today.

### 5. The settled-order path: INV-ATTENTION-PHANTOM stops clearing

`check_no_phantom_attention` must skip a terminal order whose `true_blockers` is non-empty
(invariants.py:2407-2417), clearing only when the derivation is empty.

Narrow **by construction**: no other branch of `true_blockers` fires for `completed` or
`cancelled`, so this changes behaviour for owed violations ONLY. `failed` is not in
`TERMINAL_STATUSES`, so flagged failed workers are untouched. The docstring's promise —
"once a work order is completed or cancelled there is nothing the user can act on" — is now
false, and the docstring says what replaced it.

### 6. INV-WORK-LANDED becomes the first owed checker

The unmerged/refused `Violation` in `check_work_lands` (invariants.py:3729-3737) passes the
sentence it already builds as `owed` as well as `detail` — **built once**, assigned to both.
That sentence already satisfies §1: it names the decision ("Merge it" / "Re-open and merge
it") and the command that records the alternative (`jarvis wo finish <id> --summary "..."
--abandon "<why>"`), and carries no elapsed time.

Its **refusal to repair is UNCHANGED**. `owed` is a surface, not a repair: the OS still does
not decide what happens to an unmerged pull request.

`INV-LANDING-AUDIT-FRESH` (invariants.py:3741, no `wo_id`) is **not** owed — it is a
project-level fact about the audit, with no order to flag.

**No invariant's `level` is raised.** See kn-4b49d106.

### 7. `ops.os_status` does not double-report

The critical seam at ops.py:730 (`for report in store.standing_violations(level="critical")`)
skips rows with a non-empty `owed`. The work order's own attention flag already carries
them, and two lines for one decision is the double-report the attention strip exists to
prevent. No owed checker is `critical` today; the skip is what keeps that safe when one is.

### 8. `--abandon` silences it within a tick

`ops.finish`' abandon branch (ops.py:6483-6500) already records the decision and returns
early for a `TERMINAL_STATUSES` order. Add: `store.close_violation_report(invariants.INV_WORK_LANDED, wo_id)`.

- The blocker stops deriving on the next reconcile tick (`owed_violations` returns nothing),
  and INV-ATTENTION-PHANTOM then lowers the flag through its ordinary path — §5's guard no
  longer holds.
- Without it the user waits up to an hour for the next landing sweep's
  `close_violation_reports` to notice. Clearing one alert and watching it sit there is the
  wo-5eedc84d shape the surrounding comment is already about.
- `close_violation_report` (singular) is the right call: `close_violation_reports` deletes
  every row not in the iterable it is given and is sound only where every check ran
  (project_store.py:4518-4534).
- Introduce `invariants.INV_WORK_LANDED = "INV-WORK-LANDED"` as the shared constant and use
  it at both sites, rather than a second literal in `ops`.

**Residual, accepted:** a violation fixed by any other route (the PR is merged by hand, the
order is `wo done`) still clears on the sweep cadence — up to an hour. `check_invariants`'
own docstring (daemon.py:5134-5141) already accepts and explains that trade: closing a report
on a non-sweep tick would re-announce the whole batch hourly.

### Rejected alternatives

1. **Raise INV-WORK-LANDED's `level` to `critical`** so `ops.os_status`' existing seam lists
   it. Cheapest change, and wrong twice: it is severity inflation to buy visibility
   (kn-4b49d106), and it still produces a project-level line with
   `"decide": "jarvis doctor <project>"` rather than a flag on the work order the user has to
   act on. It also leaves every other non-repairable invariant in the inbox.
2. **Flag attention from `check_work_lands` itself**, like INV-ATTENTION-MISSING does.
   Breaks on INV-ATTENTION-PHANTOM (the flag is wiped next tick) and on INV-ATTENTION-REASON
   (the reason cannot be re-derived, so it is relabelled generic). Fixing both for one
   checker means fixing them the way §3 and §5 do anyway — plus a reason that lives at a call
   site instead of in `true_blockers`, which the `BUDGET_SPENT_BLOCKER` note at
   invariants.py:109-113 records the cost of.
3. **`owed: bool`, reusing `detail` as the sentence.** Rejected in §1: `detail` carries
   elapsed time in two live checkers, and the ack mechanism cannot tolerate that in a blocker.
4. **A separate `owed_decisions` table.** A second path to the attention list, out of step
   with the dedupe row the OS already keeps per violation. The whole point of §2 is that the
   seam exists.
5. **Un-settle the order** (move it back to `needs_review` so the existing machinery works).
   Falsifies the status: the work WAS completed. It would also re-open validation paths over
   a session that is gone — the wo-5eedc84d failure, inverted.

## Test obligations

What must be proved, not how.

1. A `completed` order with a fresh `landing_seen` showing an open unmerged PR is flagged
   **exactly once** — one `attention` event across repeated `check_invariants` ticks — and
   appears on `ops.os_status`' attention list with the derived blocker as its reason.
2. `jarvis wo ack <id>` puts it down, and a later tick (sweep and non-sweep) does **not**
   raise it again.
3. INV-ATTENTION-PHANTOM does **not** clear the flag while the violation stands; it DOES
   clear a terminal order flagged with no derivable blocker (the existing behaviour, still
   covered).
4. `jarvis wo finish <id> --summary "..." --abandon "<why>"` on that order closes the report,
   and the next reconcile tick leaves the order unflagged — without a landing sweep.
5. A plain unrepaired violation (no `owed`) still produces a notification and **NO** attention
   flag.
6. INV-ATTENTION-REASON leaves the derived owed reason alone: a flag carrying it is not
   reported as a violation and not rewritten.
7. An owed violation with no `wo_id` raises no flag (and, being project-level, keeps its
   notification).
