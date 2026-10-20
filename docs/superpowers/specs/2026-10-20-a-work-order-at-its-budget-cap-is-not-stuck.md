# A work order at its budget cap is not stuck

Work order wo-71b2ecad, issue 1114.

Next member of the exclude-and-discount family, after the usage window, the CI hold
(`docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md`),
the in-flight turn (2026-10-14, which took the exclusion alone), the red base
(2026-10-15) and the unratified plan
(`docs/superpowers/specs/2026-10-15-a-child-held-by-its-features-unratified-plan-is-not-stuck.md`).
Same defect class, same two-part remedy, and the general rule is kn-55f258cd's.

## The problem

The OS already declares `budget_exhausted` a USER-ACTOR status and the sweep cannot
express that. `ops.SCHEDULED_ACTORS` (`src/jarvis/ops.py:17400`):

```python
    "budget_exhausted": ActorPasses(none_because=(
        "no daemon pass, by design — the actor is the user via "
        "`ops.set_work_order_budget`, because raising the budget resumes the order")),
```

`ops.stuck_scan` (`src/jarvis/ops.py:9857`) has no arm for it. Its `excluded_cause` chain
(`ops.py:9977-9985`) carries five members and only five — the usage window,
`project_store.VALIDATION_CI_CAUSE` (`validating`), `stuck.BASE_RED_CAUSE`
(`waiting_pr_merge`), `stuck.LIVE_DEPENDENCY` (`pending`) and
`stuck.PLAN_ASSUMPTION_CAUSE` (`pending`) — and the discounted-seconds union
(`ops.py:9948-9950`) carries usage holds, `ci_spans`, `base_red_spans` and `plan_spans`
and no budget span.

So a parked order is judged on raw time in status: `budget_exhausted` is not in
`stuck.ACTIVITY_STATUSES` (`stuck.py:28`, `("running",)`), and
`catalog.DEFAULT_FLEET_HEALTH_THRESHOLDS` (`catalog.py:1075-1083`) does not name it, so it
falls to `DEFAULT_FLEET_HEALTH_FALLBACK_MINUTES = 30` (`catalog.py:1089`) — whose own
comment names `budget_exhausted` as a status it covers. Every parked order crosses 30
minutes by arithmetic, and the user is the only actor who can clear it, so it crosses
again on every sweep until they do.

Live case: **wo-4091ba42** sat at $50.16 of a $50.00 cap with Neo escalations **1962** and
**2006** already flagged against it, and bought investigation **inv-392bc511** — a model
session to be told the OS stopped the order on purpose.

Root cause: the hold family in `excluded_cause` is enumerated by hand and the status the OS
itself documents as user-actor was never added. Not a threshold problem — raising the
fallback stops reporting every other unnamed open status with it.

## The fix

Both halves or neither (kn-0a222b38, kn-fe8f7d5b): the status is judged on TIME IN
STATUS, so an exclusion alone only POSTPONES the defect to the first sweep after the user
raises the cap, which then sees the whole parked span in one status span and buys the
investigation the exclusion existed to prevent.

### 1. The exclusion

New constant in `src/jarvis/stuck.py`, beside `PLAN_ASSUMPTION_CAUSE` (`stuck.py:51`):

```python
#: §1 of docs/superpowers/specs/2026-10-20-a-work-order-at-its-budget-cap-is-not-stuck.md.
BUDGET_CAP_CAUSE = "its budget cap"
```

It rides on `Verdict.excluded` through the pure predicate `stuck.assess`
(`stuck.py:118`). **Never a `continue` at the `ops.stuck_scan` call site** (kn-143a46df,
kn-e91ff3a5): a call-site filter has no unit test over the predicate and produces no
`Verdict` row, so the parked order would vanish from `jarvis stuck` and `/stuck` instead of
appearing there with its reason. `assess` itself needs no change beyond the constant — the
existing `elif excluded_cause:` branch composes the sentence and still populates
`active_seconds` and `threshold_seconds`.

Resolved in `ops.stuck_scan` from the row and nothing else:

```python
                            else stuck.BUDGET_CAP_CAUSE
                            if wo["status"] == budget.EXHAUSTED
```

**Scoped explicitly to `status == budget.EXHAUSTED`** (`budget.py:83`), per kn-6e836793,
and **keyed on the STATUS and never on pending assumptions** — kn-fe8f7d5b stands: an
assumption owed in any other status is a wait the user owes and stays judged.

**Its own `EXCLUSION_REASON` entry**, because `DEFAULT_EXCLUSION_REASON` ("held by
{cause}, which clears by itself") is FALSE here, exactly as it was for
`PLAN_ASSUMPTION_CAUSE`: nothing resumes a parked order but the user raising the cap. The
sentence names that move — `jarvis wo budget <id> <amount>` — and that closing it is the
other answer.

**Why a user-cleared hold may be excluded at all.** kn-fe8f7d5b bars an exclusion that
HIDES a wait somebody owes. Nothing is hidden here: the order keeps its attention flag,
`invariants.true_blockers` names the money (`invariants.py`, the `budget.EXHAUSTED` branch
calling `budget_blocker`, `invariants.py:127`), and `budget_exhausted` is
`ui.app.FEATURED_STATUSES[0]` (`src/jarvis/ui/app.py:165`) — first row of the dashboard.
What the exclusion stops is buying a model session to be told the OS is working as
designed.

### 2. The discount, and its span source

**The span source already exists and records BOTH ENDS** — the one thing this member of
the family did not have to build, in contrast with kn-2579843f / kn-84fd9da7 where the
owning module wrote no closing event. `holds._OPEN` maps `budget_exhausted` to cause
`holds.BUDGET` (`holds.py:117`) and `holds._CLOSE` maps `budget_resumed` to it
(`holds.py:143`), both unkeyed. So `holds.held(pstore, wo["id"], now=now)` — **already
called at `ops.py:9901`** for the usage spans — yields the episode, and `stuck_scan`
simply never unions it.

```python
        budget_spans = ([(max(h.started, since), min(h.finish(now), now))
                         for h in spans if h.cause == holds.BUDGET]
                        if wo["status"] == budget.EXHAUSTED else [])
```

joined to the existing union (`ops.py:9948`). **A union and never a sum**
(`holds.union_seconds`): a usage window can shut while an order is parked, and summing
would report the same minutes twice under two names.

No new read, no new store, no new accessor. Same status gate as the exclusion, so no other
order pays for the filter.

**A CLOSED episode survives the raise**, which is kn-6e30d561's requirement that the
discount outlive the exclusion: `budget_resumed` ends the episode and the pair stays in the
timeline, so the first sweep after the resume still discounts the parked hours.

**`holds._outside` clips worker-turn seconds out of the span** (`holds.py:338`). Correct
and harmless here: a parked order runs no turn, so there is nothing to clip. Degradation is
in the under-discount direction in every failure — an episode whose opener predates this
release is still in the timeline (the events are not new), and an unreadable payload yields
no span rather than one running to `now`.

### 3. Precedence

Below the fleet usage window — a fact about the ACCOUNT outranks a fact about one order's
money — and above `LIVE_DEPENDENCY`.

**Collisions are impossible by construction except with the usage window**, so the
placement is documentary rather than load-bearing, and the comment must say so: the other
arms are scoped to `validating`, `waiting_pr_merge` and `pending`, and an order has one
status. The usage window is the real case — the account can be spent while an order is
parked — and it wins, as it does for every other member.

### 4. The attention line

Second half of the issue, ruled by Neo on question **2018** of wo-71b2ecad.

In `invariants.true_blockers` (`invariants.py:1151`), a `budget_exhausted` order with
assumptions owed must emit **ONE COMPOUND LINE that REPLACES both** the
`"N assumption(s) pending your review"` line (`invariants.py:1177-1179`) and the
`budget_blocker` line: the raise first, then `"N assumption(s) also await you"`. The
assumptions line is not appended at all in this status; the compound is the only line.

Rejected, both on the record:

* **(a) reorder `budget_blocker` above the assumptions line.** A surface that prints only
  `reason[0]` would then hide half the wait — kn-385459bb's lineage, all-N-visible.
* **(c) bolt a budget clause onto the assumptions line.** Leads with the smaller fact; the
  money is what the order is parked on.

In every other status both lines stay exactly as they are, and the overall ranking is
unchanged.

**The `_mentions_assumptions` tail is NOT modified, and needs no guard.** `true_blockers`'
tail (`invariants.py:1455-1460`) appends `parked_reason` when EVERY blocker
`_mentions_assumptions` (`invariants.py:2319` — substring `"assumption"`), and the compound
does mention them, so the tail's condition is satisfied. It still cannot append anything:
`parked_reason` (`invariants.py:1696`) returns `None` immediately for any status outside
`invariants.PARKABLE_STATUSES` (`invariants.py:561`,
`("dispatching", "running", "waiting_input", "needs_review")`) — the early return at
`invariants.py:1723-1724` — and `budget_exhausted` is not in that tuple. So no parked
sentence can reach a parked order however the compound is worded, and no second, weaker
line can appear beside it.

**No prefix constant and no `startswith` exemption.** A guard that cannot fire is dead
code, and worse than dead here: it would assert a hazard the status machine already
forecloses, so the next reader would believe the tail reaches this status. The absence is
held by test 7 in §7 instead, which asserts the OUTCOME rather than a guard.

### 5. The legend

One line, in `src/jarvis/ui/templates/stuck.html`. Lines 42-48 enumerate the excluded
holds in prose — "the account's usage window, a fleet pause, the reopen ramp, or a
validation round waiting for GitHub … a worker whose turn is in flight, and a merge the OS
is holding itself because the default branch is red" — and will now be one short, so the
page would list a cause its own legend denies (the red-base spec's §4 rule). The clause to
add names the order parked at its budget cap, waiting on the user to raise it.

`stuck.assess`'s docstring and `stuck.py`'s `excluded_cause` paragraph take the same
addition, for the same reason.

### 6. Deliberately not done

* **No new catalog key and no new threshold.** The discount is arithmetic over holds the OS
  already writes (kn-fe8f7d5b point 4). kn-1cec46b5 — every fleet-health threshold is a
  catalog key and none may be a module constant — is satisfied by adding none.
* **No event, no notification, no new flag** (kn-1e65d22d rule 2). wo-4091ba42's debt is
  already surfaced by Neo escalations 1962 and 2006; writing anything here would re-notify
  a wait the user has already been shown.
* **No `continue`** in `ops.stuck_scan` (§1).
* **No change to the `_mentions_assumptions` tail** and no prefix constant (§4).
* **No reordering** of `true_blockers` and no change to its ranking.
* **No change to `needs_review`** or to any other status: the assumptions line, the
  `budget_blocker` line and the judgement of every other status are untouched.
* **The plan hold's absence from the same `stuck.html` legend is left alone.** It is a real
  gap — `PLAN_ASSUMPTION_CAUSE` shipped without the legend clause — and it is not this
  work order's, so fixing it here would put an unrelated edit in this diff.
* **`budget.escalate`, `ops.set_work_order_budget` and the resume path are untouched.**
  They already do the right thing; the defect was that the sweep did not read the status.
* **`fleet_health.cooldown_minutes`, `max_per_day` and the fingerprint are untouched** —
  the repeat-per-sweep symptom is cured by not flagging the order at all.

### 7. The tests that prove it

Extend `tests/test_health_sweep.py` (the sweep's exclusion+discount home, with
`tests/test_validation_ci_hold.py` as the pair's precedent) and the budget-blocker tests in
`tests/test_invariants.py`. No new file.

1. **Parked and excluded with the named cause.** `budget_exhausted` for 2h, a
   `budget_exhausted` event on the timeline: `verdict.stuck is False`,
   `verdict.excluded == stuck.BUDGET_CAP_CAUSE`, and `verdict.reason` is
   `EXCLUSION_REASON[BUDGET_CAP_CAUSE]` — asserted to be NOT
   `DEFAULT_EXCLUSION_REASON`, and to contain `jarvis wo budget`.
2. **The status is the key, not the assumptions.** Same row with no pending assumption:
   still excluded. A `needs_review` order with assumptions owed and the same spent cap:
   `stuck is True`, `excluded == ""`. kn-fe8f7d5b's regression guard.
3. **The discount outlives the exclusion.** 2h parked, `budget_resumed` written and the
   status moved to `pending` 5 minutes ago: `excluded == ""`, `stuck is False`, and
   `active_seconds` asserted near 300s — the number is the claim (kn-6e30d561).
4. **Union, not sum.** A usage hold wholly inside the budget episode: `discounted` equals
   the union, not the total.
5. **No `continue`.** With `stuck_only=False` the parked order is PRESENT in
   `stuck_scan`'s rows and in `ops.stuck_report`, carrying `excluded` — kn-143a46df's
   structural guard. Plus: `Daemon._stuck_candidates` and `ops.stuck_report` agree over the
   same store and clock (§7's one-derivation invariant).
6. **One compound line.** `budget_exhausted` with 3 pending assumptions:
   `true_blockers(...)` contains exactly one line, it starts `"budget spent — "`, it
   contains `"3 assumption"` and `"also await you"`, and NO separate
   `"pending your review"` line is present. Paired with: a `needs_review` order with 3
   pending assumptions is unchanged.
7. **No parked sentence beside the compound.** The case above asserts the list has exactly
   one entry and that `parked_reason`'s sentence is absent — the regression guard for §4's
   reasoning, asserted as an outcome of `PARKABLE_STATUSES` rather than as a guard's
   behaviour. Paired with: a `budget_exhausted` order with NO assumptions gets the plain
   `budget_blocker` line and nothing else.
8. **No new write.** The sweep over a parked order writes no timeline event and sends no
   notification: event count and the inbox unchanged across a sweep (kn-1e65d22d rule 2).
