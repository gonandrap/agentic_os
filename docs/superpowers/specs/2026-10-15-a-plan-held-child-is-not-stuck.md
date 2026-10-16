# A plan-held child is not stuck

GitHub issue 1078. Neo question 1804 decided the discount mechanism (B); this spec records
the reconciliation and the exact shape.

Precedent copied end to end:
`docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md` — the
exclude-and-discount pair, the pure span function, the union. Everything below is that spec
applied to a second designed wait.

## The problem

`ops.stuck_scan` (`src/jarvis/ops.py:9665`) computes `excluded_cause` for a `pending` row
from exactly one wait:

```python
edges = (pstore.unfinished_dependencies(wo["id"]) if wo["status"] == "pending" else [])
live_edges = bool(edges) and not any(invariants.dependency_is_dead(dep) for dep in edges)
```

A child of a feature order whose PLAN is unratified has `depends_on: []`, so `edges` is
empty, `live_edges` is False, and the row is judged on the plain `pending` clock —
`catalog.DEFAULT_FLEET_HEALTH_THRESHOLDS["pending"] = 30` minutes
(`src/jarvis/catalog.py:1074`). The hold itself is `ProjectStore.plan_hold`
(`src/jarvis/project_store.py:2621`) and the `plan_assumption` entry of `CLAIM_RULES`
(`src/jarvis/project_store.py:1863`), whose SQL passes the row over inside the atomic
claim: **the OS never attempts to dispatch it**, by design, until a person rules on the
planner's assumptions. The sweep cannot see either.

Observed, 2026-10-15: wo-89ec035a, child of fo-2891ead5, planner wo-3511ecc4,
`depends_on: []`, timeline containing only `created`, judged at 30m against the 0.5h
`pending` threshold and sent to an investigation. The issue body is the authoritative
statement of the defect; those are its rows.

The same row's `invariants.status_label` (`src/jarvis/invariants.py:1916`) already prints
the wait in words:

```
pending — blocked by fo-2891ead5's plan (N assumptions await your review — `jarvis wo review wo-3511ecc4`)
```

One surface knows and the other spends a session. It fires on every child of every feature
order whose plan is not ratified within 30 minutes of the children being created — i.e. on
any plan the user does not review the same half hour.

### Root cause, and it is two facts, not one

1. **No exclusion.** `plan_hold` is not in the sweep's exclusion set, though it is in the
   claim's refusal set and in three readers' labels.
2. **`pending` is not in `stuck.ACTIVITY_STATUSES`** (`src/jarvis/stuck.py:28`), so it is
   judged on **time in status**. An exclusion alone only postpones the defect: the moment
   the user ratifies the plan, `seconds_in_status` is already past 30 minutes, so the first
   sweep after the ruling buys exactly the investigation the exclusion existed to prevent —
   while the child is being claimed normally. The held seconds must be **discounted** as
   well as excluded. kn-0a222b38 is that pair rule, and it is why this spec has two halves.

### Reconciliation with the standing rulings

- **kn-6e836793** (2026-09-27): the only hold exclusion is an order paused on the Claude
  usage/session limit. Already extended once, on the record, by the 2026-10-12 CI spec's
  two tests. The plan hold passes the FIRST test — the OS is deliberately not acting, no
  claim, no launch, no attempt, no attention flag — and FAILS the second as that spec
  worded it: it clears when a **person** runs `jarvis wo review`. The extension is granted
  anyway, and on a different ground, which §6 of the fix states in full: the decision is
  owed on the PLANNER, which is flagged, and the child carries no decision of its own. The
  auth hold stayed judged because the order the sweep was judging *was* the one owing the
  user's action; here it is not. That distinction is the whole of the extension, and it is
  stated so the next hold cannot be assumed in.
- **kn-4fb740a9**: a designed wait the sweep investigated anyway is a GAP, and the stuck
  entity is the SWEEP, not the subject. This defect is that class exactly — the subject is
  waiting correctly.
- **kn-0a222b38**: exclusion plus discount wherever the status is judged on time in status;
  the discount is a UNION of hold intervals and never a sum.
- **kn-143a46df**, **kn-e91ff3a5**: the decision stays in the pure predicate, and a
  `continue` at the call site is refused — §2.
- **kn-fe8f7d5b** is cited by the work order's brief as the governing ruling for the
  constant's single home and named carriers (§1). This seat could not read the ledger
  (`jarvis learn show` needs a shell it does not have), so the implementer must read
  kn-fe8f7d5b before paraphrasing it anywhere in code comments.

## The fix

Exclude while the plan hold is live **and** discount the held seconds — the pair
`stuck_scan` already applies to `PAUSE_USAGE_LIMIT` and to `ci_pending` at that one call
site. No new `fleet_health` catalog key, no new status.

### 1. The cause constant: one home, no fourth spelling

The token `"plan_assumptions"` already exists twice, as the same string with two spellings
of its name:

- `automerge.HELD_PLAN_ASSUMPTIONS = "plan_assumptions"` (`src/jarvis/automerge.py:105`);
- a bare literal in `ops.waiting_on` (`src/jarvis/ops.py:1554`), also appearing in
  `ops.USER_IS_NEEDED_WAITS` (`src/jarvis/ops.py:1716`) and `ops.FIX_USERS_MOVE`
  (`src/jarvis/ops.py:2535`).

**A third definition is not minted.** The home is `stuck.py`, beside its three siblings:

```python
#: §1 of docs/superpowers/specs/2026-10-15-a-plan-held-child-is-not-stuck.md.
PLAN_ASSUMPTIONS_CAUSE = "plan_assumptions"
```

and `automerge.HELD_PLAN_ASSUMPTIONS` becomes an **alias** of it — exactly the move
`IDLE_MANAGER_CAUSE` already made, where `stuck.py` holds the one definition and
`ops.IDLE_MANAGER_CAUSE` re-exports it (`src/jarvis/stuck.py:35-41`). Direction of
dependency: `automerge` imports `stuck`, never the reverse; `stuck.py` imports only
`health.SEP` today and must keep importing nothing else, because its whole claim is that it
opens nothing.

The two `ops` frozensets and `ops.waiting_on`'s literal are **left as literals** in this
work order. Replacing them is a mechanical sweep with its own blast radius across
`resume-auto`'s diagnosis vocabulary (`tests/test_resume_auto_diagnosis.py:371` asserts the
string), and a spec that changes the exclusion and that vocabulary in one commit cannot be
reverted in halves. §10 records it as out of scope and names it, and issue #1099 tracks the
collapse.

**Value, not a new one.** The cause string a surface prints is already
`"plan_assumptions"` on the merge hold and in `waiting_on`, so a reader who learns it once
reads it everywhere.

### 2. The exclusion, inside the pure predicate

`stuck.EXCLUSION_REASON` gains its own one-sentence entry, because
`DEFAULT_EXCLUSION_REASON` ("held by {cause}, which clears by itself") is **false** here —
this hold does not clear by itself:

```python
EXCLUSION_REASON = {
    ...,
    PLAN_ASSUMPTIONS_CAUSE: ("its feature's plan is still waiting on a ruling, so the "
                             "claim passes this child over by design — the decision is "
                             "owed on the planner, where it is flagged"),
}
```

`stuck.assess`'s docstring (`src/jarvis/stuck.py:111-143`) MUST be amended: it enumerates
the `excluded_cause` family and calls them "all five", and it states that every member
resumes by itself. Both sentences become false. Record the extension and cite kn-6e836793
and this spec, as the CI extension did.

At the call site, `ops.stuck_scan`, beside the existing `edges` read:

```python
# A CHILD WHOSE PLAN IS UNRATIFIED IS NOT STUCK (§2 of this spec). `pending` and
# `kind == 'worker'` ONLY, mirroring the claim rule's own scope.
plan_held = bool(wo["status"] == "pending"
                 and str(wo.get("kind") or "worker") == "worker"
                 and pstore.plan_hold(wo))
```

reaching `assess` as data, after the dependency cause:

```python
excluded_cause=(PAUSE_USAGE_LIMIT if window or any(h.open for h in usage)
                else VALIDATION_CI_CAUSE if ci_held
                else stuck.LIVE_DEPENDENCY if live_edges
                else stuck.PLAN_ASSUMPTIONS_CAUSE if plan_held else "")
```

**`plan_hold` and not a re-derived query.** It is the rule's own reader, already used by
`invariants.status_label`, `ops.waiting_on`, `Daemon.auto_merge` and
`ops.assumptions_with_rulings`, and it costs no query at all on a parentless row — it
returns on `wo["parent_id"]` before touching SQL, which is nearly every work order in the
fleet. The `status`/`kind` gate in the caller is therefore not a cost optimisation; it is
the scope statement, and it is written out rather than left to `plan_hold`'s internals so
the gate is visible at the site that spends the money.

**Scoped to `status == 'pending'`, deliberately.** `plan_hold` answers True for a child in
any status — `Daemon.auto_merge` reads it at `waiting_pr_merge`. A `waiting_pr_merge` or
`needs_review` child owes something itself; excluding there would hide a wait the sweep
exists to surface. Same refusal the CI spec made for the `validating`/`needs_review`
overlap.

**Scoped to `kind == 'worker'`, and the PLANNER stays judged.** `plan_hold` already exits
on `kind != 'worker'` for the reason it states: the planner is dispatched *before* the plan
exists, so holding it on its own assumptions would deadlock the feature permanently. The
same asymmetry is what keeps the sweep honest here — a planner wedged in `pending` for half
an hour is a real standstill and must still be investigated. Writing the `kind` test at the
call site, where `CLAIM_RULES`' SQL writes `w.kind = 'worker'`, makes the two scopes read
the same.

**No `continue` in `stuck_scan`** (kn-143a46df, kn-e91ff3a5). A `continue` is
indistinguishable from an order comfortably inside its threshold, the row vanishes from
`jarvis stuck` and `/stuck`, and half the spend decision moves to a site with no unit test
and no `Verdict`. An excluded `Verdict` is a **visible row** naming the cause, with both
numbers populated; `stuck_only=True` drops it before `true_blockers` and before the
candidate list, as it already does for every other exclusion.

Precedence: usage, then CI, then a live dependency, then this. Unchanged rule — nothing in
the OS outranks the account (`holds._RANK`), and a dependency edge is a fact about this
order while the plan hold is a fact about its feature.

### 3. The discount needs one column: `assumptions.decided_at`

The discount measures "how long was this child held", which is "how long has an assumption
on its planner been pending". The record cannot answer it today: `assumptions` stores `ts`
(raised) and `status`, and `review_assumption` (`src/jarvis/project_store.py:4868`) writes
the verdict and its attribution but **no timestamp**. So a ratified plan leaves nothing
saying *when* it was ratified, and the span can only be bounded for assumptions still
pending.

Mechanism (B), confirmed by Neo 1804. `ADDED_COLUMNS['assumptions']`
(`src/jarvis/project_store.py:1649`) gains:

```python
# WHEN IT WAS SETTLED, which `status` plus `decided_by` cannot answer: the fleet-health
# sweep discounts the seconds a child spent held behind its feature's plan
# (docs/superpowers/specs/2026-10-15-a-plan-held-child-is-not-stuck.md §4), and that is
# an interval, so it needs the end of one. NULL on every row settled before this
# existed, and `plan_hold_spans` yields NO span for those rather than guessing — an
# UNDER-discount, never one in the direction that buys an investigation.
"decided_at": "REAL",
```

`ADDED_COLUMNS` is the ONLY place it is declared — the `CREATE TABLE` is not edited; that
is the surrounding convention and it is what makes a live database get the column.

Written in **one** site, `ProjectStore.review_assumption`, which stamps `db.now()` into the
same UPDATE that writes `status`. One site is both routes: `ops.review_work_order` (the
user's `jarvis wo review`) and the auto-review route both settle through this method, so
neither can be forgotten and the two cannot disagree. `record_provisional` and the
confirmation verbs do **not** write it — they settle nothing (`status` stays `pending`), and
a timestamp there would end a span the OS is still inside.

### 4. The span function: pure, rows-not-store, beside its precedent

In `project_store.py` beside `validation_ci_hold_spans`
(`src/jarvis/project_store.py:253`), the same signature shape:

```python
def plan_hold_spans(assumptions: Iterable[Any], since: float,
                    now: float) -> list[tuple[float, float]]
```

`assumptions` is the PLANNER's assumption rows — `pstore.all_assumptions(planner_id)`, the
carrier `pending_assumptions(wo_id)` already filters. Rows and not a store, for
`validation_ci_hold_spans`' stated reason: one home, named carriers.

Per row, the interval it held the feature's children:

- `start = max(a["ts"], since)`;
- `end = a["decided_at"]` when it is not NULL;
- `end = now` when `status == 'pending'` — the hold is still standing;
- **no span at all** when `status != 'pending'` and `decided_at` IS NULL.

clipped to `[since, now]`, empty spans dropped, returned sorted oldest first.

That third-and-fourth split is a correction to the brief's shorthand
`[a.ts, a.decided_at or now]`: `or now` is correct for a PENDING row and wrong for a
settled pre-column one, where it would run the span to `now` and OVER-discount a hold that
ended days ago. Skipping the row is what makes the degradation conservative, which is the
property Neo 1804 required — the error is always an under-discount, never a number in the
direction that buys another investigation. Those rows age out: every plan ratified after
this ships has a `decided_at`.

**ALL the planner's assumptions, no round-style filter.** Clipping to `since` — the child's
current status span — makes one unnecessary and still correct.

**Spans and not a total**, for §5.

### 5. No double counting

`ops.stuck_scan` already unions the usage intervals with the CI spans. The plan spans join
that same call:

```python
discounted = holds.union_seconds(
    [(max(h.started, since), min(h.finish(now), now)) for h in usage]
    + ci_spans + plan_spans)
```

A usage window can shut while a plan sits unratified — the intervals overlap, and the
number handed to `assess` is the **measure of the union**, never the sum. `holds.py`'s own
rule: "the overlap is counted ONCE … a reader can never be told the same four hours twice
under two names". With no plan hold the number is bit-for-bit what it is today, because
`union_seconds` over the unchanged intervals is unchanged; that identity is a test.

`plan_spans` is read under the same `plan_held` gate's status and kind, from
`pstore.all_assumptions(hold["planner_id"])` with the `hold` already in hand — so a
parentless row pays nothing, and the exclusion and the discount cannot disagree about which
planner they are talking about.

### 6. The user's attention is not lost — it was never on the child

The one thing an exclusion must not do is bury a decision the user owes. It does not here,
and the reason is structural rather than a promise:

- the pending assumptions stay on the PLANNER, which is where the single decision is owed
  and where it is already flagged — `jarvis wo review wo-3511ecc4`, one ruling for the
  whole feature however many children it has;
- the child's own `invariants.status_label` keeps printing the plan-hold sentence
  (`src/jarvis/invariants.py:1916`), naming the feature, the count and the exact command;
- `ops.waiting_on` keeps answering `plan_assumptions` with `stalled: False`, and
  `"plan_assumptions"` stays in `ops.USER_IS_NEEDED_WAITS` and `ops.FIX_USERS_MOVE`;
- the excluded row stays **visible** on `jarvis stuck` and `/stuck` with the cause named
  (§2).

What is removed is only the investigation: N children of one feature each buying a session
to report that the user has not reviewed one planner yet.

`invariants.true_blockers` keeps its deliberate silence on the plan hold — kn-143a46df: a
blocker named there arms INV-ATTENTION-MISSING, and the fix is to not open the
investigation, not to make its prompt read better.

### 7. Surfaces

- `cli.cmd_stuck` prints `row["reason"]`, which is now §2's sentence. No change.
- `src/jarvis/ui/templates/stuck.html` prints `{{ r.excluded or '—' }}` as a raw token. No
  change.
- **`src/jarvis/ui/templates/stuck.html` prose MUST change** where it enumerates the
  exclusions. It became false the moment the CI wait was added and becomes false again
  here. Add the unratified plan.
- **`stuck.assess`'s docstring MUST change** (§2).
- `holds.HOLD_CAUSES` is **not** extended — §9's first rejected alternative is why.

### 8. Tests the implementer writes

`tests/test_health_sweep.py` (it owns the sweep and already exercises `stuck.assess`
directly):

1. `test_a_plan_held_child_is_excluded_not_investigated` — a `pending`, `kind='worker'`
   child of a feature with a pending assumption on its planner, 45 minutes in status:
   `verdict.excluded == stuck.PLAN_ASSUMPTIONS_CAUSE`, `verdict.stuck is False`, the row is
   **present** in the `stuck_only=False` output (the anti-`continue` assertion), and no
   investigation is created by the sweep tick.
2. `test_the_planner_itself_is_still_judged` — the planner, `pending` with its own
   assumptions pending, 45 minutes in status: `stuck is True`, `excluded == ""`. The
   deadlock-avoiding asymmetry, pinned.
3. `test_a_non_worker_kind_and_an_unparented_pending_row_are_unaffected` — parametrised: a
   `kind='manager'` child and a work order with no `parent_id`, both `pending` past the
   threshold, both still `stuck is True`.
4. `test_a_ratified_plan_falls_back_inside_the_threshold` — the §1(2) half, and the test
   that FAILS if only the exclusion is implemented. The assumption is settled through
   `review_assumption` at T, the child is still `pending`, `now = T + 2min`, total time in
   status 45 minutes: `excluded == ""` (the hold is gone) and `stuck is False`, because
   `discounted_seconds` puts `active_seconds` under 30 minutes.
5. `test_a_usage_window_overlapping_the_plan_hold_is_counted_once` —
   `discounted_seconds` equals the union, strictly less than the sum; and with no plan hold
   at all, exactly today's figure.

`tests/test_project_store.py` (the pure function and the column):

6. `test_plan_hold_spans_clips_and_skips_an_undated_settlement` — spans clipped to
   `[since, now]`; a pending row ending at `now`; a settled row ending at `decided_at`; a
   row with `status='accepted'` and `decided_at IS NULL` yielding NO span; several
   assumptions, one raised before `since`; output sorted.
7. `test_review_assumption_stamps_decided_at_on_both_routes` — the user route and the
   auto-review route (`decided_by='neo'`) both leave a non-NULL `decided_at`.

`tests/test_feature_orders.py` (the drift pin, modelled on
`test_plan_hold_and_the_claim_query_agree` at line 732 and using the same `a_bare_feature`
matrix):

8. `test_the_sweep_exclusion_and_the_plan_hold_agree` — over `(held child, clear child,
   the planner, no feature)`, for each row `pending`: the new sweep exclusion fires exactly
   when `store.plan_hold(row) is not None`, which the existing pin already ties to the
   claim SQL. Three statements of one rule — the claim's SQL, `plan_hold`, the exclusion —
   and after this neither pair can drift alone.

### 9. Rejected alternatives

- **Add `pending` to `stuck.ACTIVITY_STATUSES`,** so a child with a bare `created` timeline
  is judged on silence. This is the fix a reviewer will propose, and it is the worst of
  them: a `pending` order's activity clock never restarts because nothing acts on a pending
  order at all, so every pending row would become permanently un-stuck and the 30-minute
  threshold would stop meaning anything — including for the planner in test 2 and for an
  order wedged behind a dead dependency.
- **Synthesise a hold inside `holds._episodes`** and let the existing `holds.held` call pick
  it up, as that module does for `VALIDATION_HELD_CAUSE`. Loses on blast radius, verbatim
  the CI spec's §8: `holds.held` is read by `jarvis inspect`, `Daemon.check_burning_turns`,
  the alarm thresholds and the feature rollups, so held minutes those reports call wall
  clock would change fleet-wide, with a new cause in `HOLD_CAUSES` and in the published
  `Hold.as_dict`. Worse here than there: `holds.held` clips to intervals where no worker
  turn was alive, and a pending child has no turns at all.
- **Raise the `pending` threshold.** Fixes the symptom for one review latency and breaks
  the one thing the threshold is for — a child genuinely never claimed. The wait is not
  slow, it is **held**, and those are different facts.
- **`continue` in `stuck_scan` when `plan_hold` is truthy.** One line, and kn-143a46df,
  kn-e91ff3a5: the row vanishes from both stuck surfaces and the decision leaves the pure
  predicate. It is the shape that produced this class of defect three times already.
- **Derive the hold's duration from the planner's timeline instead of a new column** — e.g.
  the `assumption_reviewed` event, or the planner's own status transitions. Rejected: it
  makes the sweep's arithmetic depend on event vocabulary written for other readers, two
  routes settle an assumption and only one of them is guaranteed to emit a timeline event,
  and the end of the interval is a property of the assumption ROW. A column on the row that
  owns the fact is both cheaper and checkable.
- **Exclude without discounting.** §1(2): the investigation arrives one sweep after the
  user rules, which is the most confusing possible moment for it to arrive.
- **A `fleet_health.plan_hold_*` catalog key.** A setting asks the user to configure how
  long they take to review their own plans.

### 10. Out of scope, explicitly

- The `pending` threshold default stays 30 minutes; no catalog key is added.
- `CLAIM_RULES`' `plan_assumption` SQL, `plan_hold` and `invariants.status_label` are
  **unchanged** — this spec reads them, it does not touch them.
- `ops.waiting_on`'s `"plan_assumptions"` literal and the two `ops` frozensets keep their
  literals; collapsing them onto `stuck.PLAN_ASSUMPTIONS_CAUSE` is a separate mechanical
  work order, tracked as https://github.com/gonandrap/agentic_os/issues/1099 (§1).
- `invariants.true_blockers`' deliberate silence is untouched.
- No backfill of `decided_at` for rows settled before it shipped. §4 is explicit that those
  under-discount, and a backfill would have to invent a timestamp.
- `VALIDATION_HELD_CAUSE` and `VALIDATION_AUTH_CAUSE` are still **not** in the exclusion
  set. The auth hold clears when a human runs `/login` on the order being judged — §"The
  problem"'s reconciliation is precisely about which order owes the action, and there it is
  the subject itself.
