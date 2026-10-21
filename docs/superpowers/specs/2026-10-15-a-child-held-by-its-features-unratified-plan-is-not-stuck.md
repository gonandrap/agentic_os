# A child held by its feature's unratified plan is not stuck

Work order wo-f64f2ddd, issue 1077.

Direct sequel to
`docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md`:
same defect class, fourth designed wait, same two-part remedy (exclude the live hold AND
discount the held seconds).

## 1. The problem

`ops.stuck_scan` (`src/jarvis/ops.py:9665`) builds `stuck.assess`'s `excluded_cause` from
three members only (`ops.py:9742-9745`):

```python
excluded_cause=(PAUSE_USAGE_LIMIT
                if window or any(h.open for h in usage)
                else VALIDATION_CI_CAUSE if ci_held
                else stuck.LIVE_DEPENDENCY if live_edges else ""),
```

A FOURTH designed wait already ships and is not in that chain:
`project_store._ClaimRule(code='plan_assumption')` (`project_store.py:1863`, SQL at
`:1866`) withholds every `kind='worker'` child from `claim_next_pending` while its parent
feature's planner has `assumptions.status = 'pending'`. `ProjectStore.plan_hold`
(`project_store.py:2620`) is the readable predicate for the same rule, already consumed by
`invariants.status_label` (`invariants.py:1915`), `ops.waiting_on`, `Daemon.auto_merge`
and `ops.assumptions_with_rulings`.

Consequences, all three measurable on one row:

1. The child cannot be dispatched, so it sits in `pending`. `pending` is not in
   `stuck.ACTIVITY_STATUSES` (`stuck.py:28`), so it is judged on TIME IN STATUS and
   crosses the 30m `pending` threshold while behaving exactly as designed.
2. The blocker column renders `"nothing the record can name"` (`ops.py:9759`) while
   `row["status_label"]` on the SAME row (`ops.py:9760`) already names the plan hold and
   the review command. One row, two readers, contradicting each other.
3. The sweep buys one investigation PER HELD CHILD.

Measured: fo-2891ead5's plan holds 15 pending assumptions on planner wo-3511ecc4. All
seven children were swept; one of them, wo-a8509e79, produced investigation inv-1dd77cbb.

Root cause is the missing member of the `excluded_cause` family, not the investigation
heuristics and not the threshold.

## 2. The fix

### 2.1 `stuck.PLAN_ASSUMPTION_CAUSE`

New constant in `src/jarvis/stuck.py` beside `LIVE_DEPENDENCY` (`stuck.py:33`) and
`IDLE_MANAGER_CAUSE` (`stuck.py:41`), with its OWN sentence in `stuck.EXCLUSION_REASON`
(`stuck.py:49`).

The shipped `DEFAULT_EXCLUSION_REASON` (`stuck.py:63`) — "held by {cause}, which clears by
itself — nothing is judged while it does" — is FALSE for this hold: it clears only when a
person rules on the plan (`jarvis wo review <planner-id>`). `IDLE_MANAGER_CAUSE`'s entry
is the precedent for a cause whose wait is ended by somebody else, and the new sentence
must say who: the user, on the planner's assumptions.

It rides on `Verdict.excluded` inside the pure predicate (`stuck.assess`, `stuck.py:102`).
NEVER a `continue` in `stuck_scan` — kn-143a46df: a held order must remain a visible row
on `jarvis stuck` showing its arithmetic.

### 2.2 Where the cause is supplied

From `ops.stuck_scan` only, the one call site holding a `ProjectStore`. kn-4f88d1b7:
`assess` is pure over scalars and cannot discover a hold. The predicate is
`pstore.plan_hold(wo)` — already written, already the reader of the claim rule, pinned to
the claim SQL by `test_plan_hold_and_the_claim_query_agree`. Do not re-derive it.

### 2.3 The discount

An exclusion ALONE only moves the false positive to the first sweep after the user
reviews, because `pending` is judged on time in status (§1.1). kn-0a222b38 is the ruling
that an exclusion without a discount is an unfinished fix.

New PURE module-level function `project_store.plan_hold_spans(events, since, now)`, shaped
exactly like `project_store.validation_ci_hold_spans` (`project_store.py:252`): takes ROWS
and not a store, returns a sorted list of `(start, end)` intervals clipped to
`[since, now]`, never a scalar total.

It walks the PLANNER's timeline with a running pending count:

| event kind | effect on the count |
|---|---|
| `assumption` | `+1` (written at `project_store.py:4832`) |
| `autoreview_accepted` | `-1` (written at `ops.py:8605`) |
| `reviewed` | `-payload["count"]` (written at `ops.py:8743`) |

A span opens when the count goes 0 to positive and closes when it returns to 0 (or at
`now`, if still positive). Those two settlement events are EXHAUSTIVE: both callers of
`ProjectStore.review_assumption` write one of them — `ops.py:8600` (Neo's auto-review)
pairs with `autoreview_accepted`, `ops.py:8733` (the user's route) pairs with `reviewed`.

`ProjectStore.events_of_kind` (`project_store.py:3519`) takes ONE kind, so `stuck_scan`
reads the three kinds off the planner and passes the concatenation sorted by `ts`; the
pure function reads `e["kind"]` and `db.from_json(e["payload"], {})`.

The spans go into the SAME `holds.union_seconds` call as the usage and CI spans
(`ops.py:9717`) — a UNION and never a sum, for the reason already in that comment: a usage
window can shut while a plan hold stands.

Neo ruled this derivation (question 1803) over adding an `assumptions.decided_ts` column:
the timeline is what the OS already writes, and a NULL pre-migration column would
reintroduce exactly this stale-exclusion defect on every existing row.

### 2.4 Precedence and gating

- Both the exclusion and the discount are gated on `wo["status"] == "pending"` ONLY. The
  plan `_ClaimRule` only ever withholds a DISPATCH; a child already `running`,
  `validating` or `needs_review` is not held by it and must still clear its threshold.
  Same shape as the `validating` gate at `ops.py:9709` and the `pending` gate on
  `live_edges` at `ops.py:9720`.
- `PLAN_ASSUMPTION_CAUSE` is appended LAST in the `excluded_cause` chain:
  usage > CI > live dependency > plan hold. The shipped order is untouched. A fleet pause
  or a shut usage window is a fact about the whole fleet and outranks a fact about one
  feature's plan (`assess`'s own precedence paragraph, `stuck.py:140`).

### 2.5 Scope

The plan hold ONLY (kn-6e836793, the user's standing ruling that each new exclusion is
named on the record rather than the family widened). NOT `pending` generally. The 30m
`pending` threshold is correct and stays. `excluded_cause` has never been tunable — no new
setting, no config key.

Also out of scope and tracked, not forgotten: `ops.waiting_on` returns the bare string
`'plan_assumptions'` and the same token is a member of `ops.USER_IS_NEEDED_WAITS` and
`ops.FIX_USERS_MOVE`. `stuck.PLAN_ASSUMPTION_CAUSE` is a FOURTH reading of the same plan
hold. Collapsing the three onto one constant touches `resume-auto`'s diagnosis vocabulary
and is its own unit: https://github.com/gonandrap/agentic_os/issues/1099. A
`# Collapse onto one constant` comment at the `waiting_on` return points there too.

Root-cause note: this fixes the cause, not a symptom. The symptom patch would be to stop
the sweep from investigating held children; the cause is that one of the OS's own
withholding rules was never taught to the predicate that judges waiting.

## 3. Test obligations

The PAIR a reviewer must see, both on one held child:

1. **Excluded while held.** A `pending` child whose parent feature's planner has a pending
   assumption, 2h in status: `verdict.stuck is False`,
   `verdict.excluded == stuck.PLAN_ASSUMPTION_CAUSE`, and `verdict.reason` names the user's
   review rather than "clears by itself".
2. **Discounted after the review.** Same child, assumptions settled 1 minute ago, still
   `pending` (the next dispatch tick has not run): the FIRST post-review sweep does NOT
   fire. `active_seconds` is the ~1 minute since settlement, not the 2h in status.

Plus:

3. `plan_hold_spans` is pure and row-shaped: no store argument; spans clipped to
   `[since, now]`; `reviewed` with `count=N` closes a span opened by N `assumption`
   events; a still-open hold ends at `now`.
4. Union, not sum: a usage hold overlapping a plan hold discounts the union's seconds.
5. Gating and precedence: a `running` child of the same held feature is judged normally; a
   held child during a fleet usage window reports `PAUSE_USAGE_LIMIT`, not the plan cause.
6. The row stays visible: `stuck_report` lists the held child with `stuck=False` and a
   `blocker`/`status_label` pair that agree.

## 4. Rejected

- **A `continue` in `stuck_scan`.** Cheapest edit, and it deletes the row. kn-143a46df:
  a held order must be a visible row on `jarvis stuck` with its arithmetic shown, which is
  how the user sees the hold is real. A skip also hides it from `stuck_report`'s
  `stuck_only=False` callers.
- **Widening to `pending` generally, or a `pending` threshold knob.** `pending` with NO
  hold is the single most important thing the sweep catches — an order nobody will ever
  dispatch. A knob makes the false positive the user's problem and leaves the OS unable to
  name its own wait. The 30m threshold is right; the missing fact is the hold.
- **An `assumptions.decided_ts` column.** Neo q1803. Pre-migration rows are NULL, so every
  existing held child would discount either nothing or everything — the stale-exclusion
  defect of §1, reintroduced by the fix. The timeline already records both settlement
  paths exhaustively (§2.3).
- **Synthesising the hold inside `holds._episodes`.** kn-0a222b38 already rejected this
  for the CI hold. `holds` episodes are read by `jarvis inspect`, `check_burning_turns`,
  the alarms and the feature rollups; a synthetic episode there changes every one of those
  numbers to fix one sweep. The discount belongs in the sweep's own arithmetic, which is
  where `validation_ci_hold_spans` put it.

## 5. Precedents cited

- `docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md` —
  exclusion + discount, and the "Reconciliation with the standing ruling" section this one
  follows for kn-6e836793.
- `docs/superpowers/specs/2026-09-24-a-planner-assumption-holds-its-feature.md` — the hold
  itself (§2.1, §2.2).
- `docs/superpowers/specs/2026-10-09-a-designed-wait-is-not-a-stall.md` §2.2 —
  `LIVE_DEPENDENCY`, the `pending`-gated precedent.
- `docs/superpowers/specs/2026-10-12-a-managers-by-design-idle-is-not-a-standstill.md` §4 —
  per-cause `EXCLUSION_REASON` sentences.
- `docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets-investigated.md` — the
  sweep.
- Knowledge: kn-143a46df, kn-4f88d1b7, kn-0a222b38, kn-6e836793. Neo question 1803.
