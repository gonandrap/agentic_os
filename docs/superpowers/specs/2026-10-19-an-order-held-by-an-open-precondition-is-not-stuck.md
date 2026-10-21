# An order held by an open precondition is not stuck

Work order wo-afcea414, issue 1105. Neo question 1982 ruled the precedence (§3.3).

Fifth designed wait, same defect class and same two-part remedy as
`docs/superpowers/specs/2026-10-15-a-child-held-by-its-features-unratified-plan-is-not-stuck.md`
(exclude the live hold AND discount the held seconds). Sections are numbered so code
comments can cite them.

## 1. The problem

### 1.1 The precondition hold is not in the `excluded_cause` family

`ops.stuck_scan` (`src/jarvis/ops.py:9857`) builds `stuck.assess`'s `excluded_cause` from
five members and no more (`ops.py:9977-9985`):

```python
excluded_cause=(PAUSE_USAGE_LIMIT
                if window or any(h.open for h in usage)
                else VALIDATION_CI_CAUSE if ci_held
                else stuck.BASE_RED_CAUSE
                if base_red and wo["status"] == "waiting_pr_merge"
                else stuck.LIVE_DEPENDENCY if live_edges
                else stuck.PLAN_ASSUMPTION_CAUSE if plan_held else ""),
```

A SIXTH designed wait already ships and is in none of them: an open row in the
`preconditions` table (`src/jarvis/project_store.py:1019-1030`), written by
`jarvis wo create --blocked-by <ref>` / `ProjectStore.add_precondition`
(`project_store.py:2335`) and enforced inside the claim statement itself by
`_ClaimRule(code='precondition')` (`project_store.py:1925-1934`, SQL at `:1928`):
`NOT EXISTS (SELECT 1 FROM preconditions p WHERE p.wo_id = w.id AND p.status = 'open')`.
`ProjectStore.open_preconditions` (`project_store.py:2354`) is the readable predicate for
the same rule, already consumed by `invariants.status_label` (`invariants.py:2064`).

For a `pending` order `stuck_scan` reads only `pstore.unfinished_dependencies`
(`ops.py:9952`) and `pstore.plan_hold` (`ops.py:9933`). Neither sees a precondition row.
Consequence, mechanically:

1. `excluded_cause=''`. No exclusion.
2. No spans enter the single `holds.union_seconds` call (`ops.py:9948-9950`), so
   `discounted == 0` for the held time.
3. `pending` is not in `stuck.ACTIVITY_STATUSES` (`stuck.py:28`), so the order is judged
   on TIME IN STATUS against the 0.5h `pending` threshold — which it crosses roughly 30
   minutes after creation, while the hold is working exactly as designed.
4. The sweep buys one investigation per held order.

Evidence: wo-4b296093 was swept at 36m in `pending` with BOTH of its precondition rows
still `open`, and the sweep bought an investigation.

### 1.2 The blocker column contradicts the label on the same row

`stuck_scan`'s blocker chain (`ops.py:10000-10004`) falls through to
`"nothing the record can name"` for a precondition-held order, while
`invariants.status_label` on the SAME row (`ops.py:10005`) renders
`pending — held until {refs} clears (`jarvis wo unblock <id>`)` from
`invariants.py:2064-2070`. One row, two readers, contradicting each other — the identical
split that §1(2) of the 2026-10-15 plan-hold spec fixed for the plan hold.

### 1.3 Root cause

The ROOT CAUSE is that one of the OS's own withholding rules — the `preconditions` row,
the only one that is a property the order was CREATED with — was never taught to the
predicate that judges waiting. Not the threshold, not the investigation heuristics. The
symptom patch would be to stop the sweep investigating held orders; this fixes the cause.

## 2. The fix — part 1: a named cause in `stuck.py`

### 2.1 `stuck.PRECONDITION_CAUSE`

New module constant in `src/jarvis/stuck.py` beside `PLAN_ASSUMPTION_CAUSE`
(`stuck.py:51`), with its OWN entry in `stuck.EXCLUSION_REASON` (`stuck.py:56-76`).

The shipped `DEFAULT_EXCLUSION_REASON` (`stuck.py:80`) — "held by {cause}, which clears by
itself — nothing is judged while it does" — is FALSE for this hold.
`project_store.PRECONDITION_CLEARED_BY` (`project_store.py:1853`) is `("os", "user")`: the
row is cleared either by the OS when it resolves the ref, or by the user running
`jarvis wo unblock <id>` (`ProjectStore.clear_precondition`, `project_store.py:2361`,
asserts `by` is one of those two). So the sentence must NAME that move, the way
`PLAN_ASSUMPTION_CAUSE`'s entry names `jarvis wo review <planner-id>`.

### 2.2 It rides on `Verdict.excluded`, never on a `continue`

The cause is passed as `excluded_cause` by `ops.stuck_scan` — the one call site holding a
`ProjectStore`; `stuck.assess` (`stuck.py:119`) is pure over scalars and cannot discover a
hold — whenever `pstore.open_preconditions(wo['id'])` is non-empty, and it must surface as
`Verdict.excluded` out of that pure predicate.

NEVER a `continue` at the call site. That is a ruling, not taste: kn-143a46df and
kn-e91ff3a5 record that a call-site filter has no unit test and produces no `Verdict` row,
which is how this whole defect class arrives. A held order stays a visible row on
`jarvis stuck` with its arithmetic shown.

## 3. The fix — part 2: the discount

### 3.1 Why it is not optional

kn-0a222b38 and kn-fe8f7d5b: for any status judged on TIME IN STATUS, an exclusion with no
discount only MOVES the false positive later. The instant the hold clears,
`seconds_in_status` is already past the threshold, and the next sweep buys the
investigation the exclusion existed to prevent. So the held seconds are discounted too.

The discount is derived from the precondition rows' own `created_at` / `cleared_at`
columns — the table has both (`project_store.py:1025-1026`) — which is why an order whose
hold has SINCE CLEARED is not retroactively judged for the time it was held.

### 3.2 `project_store.precondition_hold_spans(rows, since, now)`

New PURE module-level function in `src/jarvis/project_store.py`, shaped exactly like
`plan_hold_spans` (`project_store.py:294`):

- Takes ROWS, never a store (`validation_hold_until`'s reason).
- Returns SPANS and never a total. Only intervals can be unioned with the usage, CI and
  base-red spans by `holds.union_seconds`, and a usage window can shut while a
  precondition stands.
- For each row, the interval is `[created_at, cleared_at or now]`, clipped to
  `[since, now]`; empty results are dropped. Overlapping rows need no merging here —
  `holds.union_seconds` unions.

THE ROWS ARE NOT FILTERED BY `since` BEFORE THE WALK. A precondition is normally created
WITH the order, i.e. before the `pending` status span even opens; filtering first would
lose exactly the normal case. `plan_hold_spans` documents the same rule, and the clipping
is therefore applied to the resulting spans.

It needs ALL of the order's rows, not just the open ones, so a new `ProjectStore` method
is required beside `open_preconditions`: returning EVERY `preconditions` row for a `wo_id`,
oldest first (`ORDER BY created_at, id`, the same ordering). Suggested name
`preconditions_of`. `open_preconditions` could NOT be reused: its SQL is
`WHERE wo_id=? AND status='open'`, so a cleared row is invisible to it, and a cleared row
is precisely what carries the `cleared_at` the discount is computed from. Widening
`open_preconditions` instead is rejected in §6.

`stuck_scan` adds those spans to the SINGLE `holds.union_seconds` call at `ops.py:9948` —
a union and never a sum, for the reason already in that comment.

### 3.3 Precedence — Neo question 1982

`PRECONDITION_CAUSE` goes BELOW the fleet facts (usage window, `VALIDATION_CI_CAUSE`,
`BASE_RED_CAUSE`) and ABOVE `LIVE_DEPENDENCY` and `PLAN_ASSUMPTION_CAUSE`. The shipped
order among the other five is untouched. Resulting chain:

usage > CI > base red > **precondition** > live dependency > plan hold

The reason is agreement between two surfaces, not aesthetics: `invariants.status_label`
already ranks the precondition label FIRST of its four `pending` answers
(`invariants.py:2059-2070`, above the plan hold at `:2075`, the dependency list at `:2078`
and the slot cap at `:2086`). This ranking makes the cause the sweep reports match the
sentence `status_label` prints on the same row — ONE ranking in the codebase, not two.

## 4. The fix — part 3: the blocker column

One formatter, two readers — kn-432d0f19, the arrangement `plan_hold_note`
(`invariants.py:1944`) already is.

1. Extract the sentence `status_label` composes inline today (`invariants.py:2064-2070`:
   the `pending — held until {refs} clears (`jarvis wo unblock <id>`)` branch, INCLUDING
   its `"; "`-joined `note` suffix) into a new `invariants.precondition_note(rows)` beside
   `plan_hold_note`, returning `""` for no rows.
2. `status_label` calls it. Its rendered output must be byte-identical to today's.
3. `stuck_scan`'s blocker chain (`ops.py:10000-10004`) calls it AHEAD of
   `plan_hold_note`, so the blocker column's ordering matches §3.3.

FLAGLESS. `true_blockers` gets NO branch, so `INV-ATTENTION-MISSING`
(`check_blocked_work_is_surfaced`) stays quiet without an exception — kn-143a46df is
explicit that a non-empty `true_blockers` is a FLAG and not a label, and
`invariants.py:2061-2063` already records that the precondition label is flagless for the
same reason.

## 5. Scope

`pending` ONLY, and only with open precondition rows. kn-6e836793: each new exclusion is
named on the record rather than the family widened.

Both the exclusion and the discount reads are gated on `wo["status"] == "pending"`, for
two reasons:

- A precondition withholds a DISPATCH — the `_ClaimRule` fragment at
  `project_store.py:1925-1934` is evaluated by the claim statement that picks the next
  `pending` row. An order already `running`, `validating` or `needs_review` is not held by
  it and must still clear its threshold.
- Gating on `pending` is also what keeps every other order from paying for the two reads
  (`true_blockers`' rule, and the same shape as the `validating` gate at `ops.py:9909` and
  the `pending` gate on `live_edges` at `ops.py:9952`).

NO new `fleet_health.*` key is introduced. The discount is arithmetic over rows the OS
already writes; `excluded_cause` has never been tunable, and the 0.5h `pending` threshold
is correct and stays.

## 6. Rejected alternatives

- **A `continue` in `stuck_scan`.** Cheapest edit; deletes the row. kn-143a46df and
  kn-e91ff3a5: no unit test, no `Verdict` row, invisible to `stuck_report`'s
  `stuck_only=False` callers — which is how this defect class arrives in the first place.
- **Widening `blocked_by` / `true_blockers` to return preconditions.** `true_blockers` is
  the ATTENTION flag. A precondition label is deliberately flagless (§4,
  `invariants.py:2061`); widening it would raise `INV-ATTENTION-MISSING`-shaped attention
  on every `--blocked-by` order and force an exception to keep it quiet.
- **A new `decided_ts`-style column.** Rejected for `plan_hold_spans` by Neo q1803 and
  stronger here: `preconditions` ALREADY has `created_at` and `cleared_at`
  (`project_store.py:1025-1026`), both written on every path. A new nullable column would
  be NULL on every existing row and reintroduce §1.1's stale-exclusion defect inside the
  fix.
- **Widening `open_preconditions` to return cleared rows.** It is the predicate pinned to
  the claim rule and read by `status_label` for the LIVE hold; adding cleared rows to it
  would make both of those print and enforce holds that no longer stand.
- **Synthesising the hold inside `holds._episodes`.** Already rejected for the CI hold
  (kn-0a222b38). `holds` episodes feed `jarvis inspect`, `check_burning_turns`, the alarms
  and the feature rollups; a synthetic episode changes all of those to fix one sweep.

## 7. Test plan

1. **Pure `precondition_hold_spans` arithmetic** — no store argument: a single open row
   (span ends at `now`); a cleared row (span ends at `cleared_at`); several overlapping
   rows (all spans returned, clipped, sorted — the union is `holds.union_seconds`' job); a
   row created BEFORE `since` (clipped to start at `since`, NOT dropped); a row cleared
   before `since` (dropped, empty result).
2. **`stuck.assess`** returns `Verdict.excluded == stuck.PRECONDITION_CAUSE` with
   `stuck is False`, and `verdict.reason` names `jarvis wo unblock` rather than "clears by
   itself".
3. **Precedence (§3.3)** against the other causes: a usage window / CI hold / red base
   wins over a precondition; a precondition wins over `LIVE_DEPENDENCY` and over
   `PLAN_ASSUMPTION_CAUSE`.
4. **`stuck_scan` excludes** a `pending` order with an open precondition row, 2h in
   status: `stuck is False`, the cause named.
5. **`stuck_scan` does not judge the held time after the hold clears**: same order, row
   cleared 1 minute ago, still `pending` (the next dispatch tick has not run) — the FIRST
   post-clear sweep does not fire, and `active_seconds` is ~1 minute, not 2h.
6. **Gating**: a `running` order with an open precondition row is judged normally.
7. **The blocker column** names the hold — `stuck_report`'s `blocker` and `status_label`
   on the same row agree, neither saying "nothing the record can name".
8. **`status_label` output unchanged** after the formatter extraction (§4), including the
   `"; "`-joined notes suffix and the single/multiple-ref forms.

## 8. Precedents cited

- `docs/superpowers/specs/2026-10-15-a-child-held-by-its-features-unratified-plan-is-not-stuck.md`
  — the shape of this spec, §2.1's per-cause sentence, §2.3's spans, §1(2)'s blocker split.
- `docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md` — the
  `preconditions` table, its two clearing parties (§2.1) and the claim rule (§2.2).
- `docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md` —
  exclusion + discount, and the reconciliation with kn-6e836793.
- `docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets-investigated.md` — the
  sweep.
- Knowledge: kn-143a46df, kn-e91ff3a5, kn-0a222b38, kn-fe8f7d5b, kn-6e836793, kn-432d0f19,
  kn-4f88d1b7. Neo questions 1982 (precedence), 1906, 1803.
