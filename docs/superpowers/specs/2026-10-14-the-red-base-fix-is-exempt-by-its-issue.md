# The red-base fix is exempt by its ISSUE, not only by one id

Work order wo-985f75a5, GitHub issue #1061. Lands ON TOP OF the unmerged PR #1058
(base `c830c9a`), whose code is already in this worktree — the two new fact keys
`issue_url`/`fix_wo_id` (spec
`docs/superpowers/specs/2026-10-14-a-red-main-dispatches-its-own-fix.md` §3) and the
existing one-id exemption (§5, §6) are preconditions, not deliverables.

## The problem

#1058's exemption is keyed on ONE id. `automerge.decide` holds every parked order under
`HELD_BASE_RED` unless

```python
base_red is not None and not (base_red.fix_wo_id and wo.get("id") == base_red.fix_wo_id)
```

(`src/jarvis/automerge.py:376-377`). `fix_wo_id` is written by exactly one writer: the
OS's own sweep, `Daemon.dispatch_base_fix` (`src/jarvis/daemon.py:7007`, the
`set_base_health` at `:7086`). So an order dispatched on the red-base ISSUE by any other
route carries an empty-or-other `fix_wo_id` and is held `base_red` by the very redness
its merge removes:

- `jarvis issues start` on the red-base issue,
- `jarvis bug report --expedite` filing that break,
- the user by hand, before the sweep wrote the key,
- a SECOND order on the same issue after the first was cancelled (the sweep clears
  `fix_wo_id` on a terminal fixer, `daemon.py:7044-7049`, and only rewrites it if the
  retry succeeds).

Measured: wo-e497639e, PR #1057. Validation round 1 passed at `7b653157f2`; GitHub
CLEAN/MERGEABLE; unit (3.13), evals and browser all SUCCESS. Sat in `waiting_pr_merge`
65 minutes with no attention flag and no applicable remedy. `main`'s newest red run is
still the failure at `c28b166c73`, so the stored fact never goes stale
(`Daemon._base_red` freshness gate, `daemon.py:7186-7190`) and the hold never lifts.
`gh pr update-branch` — the `red-base-inherited` gap class in `rules.py`, issue #793 — is
a no-op here: the base has not moved.

Root cause: the exemption identifies the remedy by DISPATCH BOOKKEEPING instead of by
what the remedy is FOR. The work order already records what it is for:
`work_orders.issue_url` (`src/jarvis/project_store.py:1356`, queried by
`ProjectStore.work_orders_for_issue`, `:2207`), and the fact already records the issue it
filed (#1058 §3).

## The fix

Issue linkage becomes a SECOND, sufficient way to be the remedy.

1. `automerge.BaseRed` gains `issue_url: str = ""`
   (`src/jarvis/automerge.py:189-208`), documented like `fix_wo_id`: the tracker issue
   the break is filed on, `""` when unattributed.
2. `Daemon._base_red` populates it off the stored fact —
   `issue_url=str(fact.get("issue_url") or "")` beside the existing `fix_wo_id` line
   (`daemon.py:7191-7196`). The key already exists on the fact; nothing new is stored.
3. `decide`'s base_red condition is skipped when the order being judged IS the remedy,
   now by either test:
   - `wo["id"] == base_red.fix_wo_id` (unchanged), OR
   - `base_red.issue_url` non-empty AND equal to `wo["issue_url"]`.

   BOTH guards require the stored value to be non-empty. An unattributed red base
   exempts nobody — in particular `"" == ""` against an order with no issue must NOT
   exempt.

Nothing else changes.

- The exemption skips ONLY the WORLD condition. Conditions 4-6 — round passed, judged
  head unmoved, GitHub OPEN/MERGEABLE/green/CLEAN (`automerge.py:411-447`) — are
  untouched, and they are what enforces issue #1061's "MERGEABLE with every required
  check green at the judged head". No new check is written for that requirement.
- `base_behind` is untouched (`automerge.py:401-407`): a fix branch that does not carry
  the base's tip is still caught up first.
- The hold REASON for every other parked order is unchanged — #1058 §5 already names the
  fixing order (`fixing = ...`, `automerge.py:380-381`).

### Why the predicate lives in `decide`, not at the call site

#1058 §6's reason, still binding. The condition table stays pure and unit-testable with
no store, no clock and no `gh`; and `base_red` reaches `decide` from FOUR call sites
(`daemon.py:7414`, `:7434`, `:7688`, `:7734`), so a predicate applied before the call
would have to be repeated four times and the copy that is not running is the copy that
rots.

### Known residual (accepted as benign, Neo question 1749)

If the fact's `issue_url` lands on a DUPLICATE issue — adoption matches on the run id
(`Daemon._adopt_base_red_issue`, called at `daemon.py:7064`) and a second red run on the
same break rolls that id over before the key is written — then: the sweep dispatches a
fresh order on the duplicate, that order is exempt by `fix_wo_id`, `main` still goes
green, and the older parked order waits ONE MORE MERGE rather than for ever. Not fixed
here.

### Rejected alternative

An EVIDENCE-based exemption: exempt any parked order whose head contains the red
`head_sha` and whose CI is green. Rejected — it needs an authoritative ancestry read,
because `ops.catch_up_needed` is documented wrong in both directions (under-reports on
GitHub's cache lag, over-reports for ever where `pr.base_oid` is absent;
`daemon.py:7675-7683`). That is a new `gh`/git call per tick plus a new field, for a case
the bookkeeping route already covers.

## Tests

`tests/test_base_red.py`, beside #1058's:

1. Held order exempt by issue linkage with `fix_wo_id` EMPTY — arms.
2. A DIFFERENT parked order, on another issue, still held `base_red`.
3. An order whose `issue_url` matches but whose CI is red — still held, on
   `HELD_CHECKS_FAILED`, NOT on `base_red`.
4. Empty `base_red.issue_url` exempts nobody, even when the work order's own `issue_url`
   is also empty (the `"" == ""` trap).
5. `Daemon._base_red` carries `issue_url` off the stored fact.

## Out of scope

Decided deliberately, not overlooked: the duplicate-issue residual above; any change to
how `dispatch_base_fix` picks or dedupes the issue; any new GitHub read; the hold wording
for non-remedy orders; `base_behind`.
