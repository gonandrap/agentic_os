# INV-WORK-LANDED judges the pull request, not the diff

**Date:** 2026-09-18
**Supersedes:** §7 of `2026-09-13-a-finished-order-proves-its-code-landed.md`
**Work order:** wo-16a488ee

## 1. The ruling

The user, after reading a live report:

> checking branches for which no PR were created shouldn't be checked by the invariant,
> instead, should be rejected during the validation. The invariant only should focus on
> making sure that the PR for that order has landed in main if the order is completed.

## 2. The evidence that produced it

INV-WORK-LANDED reported seven completed `jarvis_os` orders as unlanded. Checked by hand
against git and GitHub on 2026-09-18, **five were false positives** and none was work that
needed rescuing.

| Order | Reported | Truth |
|---|---|---|
| wo-5eedc84d | 44% | PR #42 MERGED 2026-07-29 |
| wo-78e2cc3d | 31% | PR #69 MERGED 2026-08-03 |
| wo-f1ce0f24 | 72% | PR #226 MERGED 2026-09-14 |
| wo-0fea6edb | 69%, "cost_wo.html missing entirely" | redone by wo-4576667e; the template landed as `bill.html` (#112, #114) |
| wo-69a06ff4 | 12% | PR #231 CLOSED, titled "superseded, not for merge" |
| wo-cd73c537 | 2% | the commits are real and are in PR #116, which is OPEN; the check read the recorded #81, merged |
| wo-752eced8 | — | PR #267 merged; the only finding was one uncommitted file in a leftover worktree |

Four distinct defects, all downstream of measuring CONTENT instead of asking about the
pull request:

1. `landing._coverage` counted an added line as landed only if its exact text was still in
   the default branch's CURRENT copy of that file. Content survives a squash merge — which
   is why it was chosen — and does not survive a refactor. That alone produced 44%, 31%
   and 72%.
2. A renamed or superseded file read as "missing entirely".
3. The pull-request rungs only knew the `pr_url` the work order recorded, so a later pull
   request on the same branch was invisible.
4. A pull request closed without merging is an abandon decision the check could not see.

## 3. The predicate

**Scope: completed work orders that HAVE a pull request.** That is the whole population.

| GitHub says | Verdict | Reported |
|---|---|---|
| merged | `landed` | no |
| open | `awaiting-merge` | yes — "delivered and waiting on a merge", remedy is merge-shaped |
| closed, unmerged | `refused` | yes — "delivered and refused" |
| no pull request | `no-pull-request` | **no. Out of scope, silent.** |

Two pull requests on one branch resolve in this order — **open beats merged beats
closed** — and the order is load-bearing rather than a tie-break:

* open over merged, because wo-cd73c537 merged #81 and then had #116 opened carrying the
  rest of the work. An earlier merge does not deliver what is still unmerged.
* merged over closed, because a follow-up opened against a landed branch and then
  abandoned is an ordinary week, and it does not un-land the merge. A "newest wins" rule
  gets this one wrong, which is why the rule is not "newest wins".

## 4. `pr_url` is not the population filter

The obvious scoping — `if not wo["pr_url"]: skip` — is wrong in both directions, and both
are live in the production records:

* **NULL where a pull request exists.** wo-5eedc84d has `pr_url` NULL and merged #42; its
  worker finished before `--pr` was enforced. Skipping on the column would silently exempt
  exactly the orders this invariant exists for — a worse failure than the noise, because
  nothing shows it happening.
* **Stale where a newer one is live.** wo-cd73c537 records #81, merged, while #116 is
  open.

So the question is asked of the **branch**, which is what the work is actually on.
`landing.branches_for` names every branch whose name carries the work-order id — the
worktree's own HEAD first, then local and remote refs, remote names stripped to the branch
GitHub knows — and returns ALL of them, because wo-f1ce0f24 used two
(`worktree-wo-f1ce0f24` → #225, `worktree-wo-f1ce0f24-memory` → #226) and either alone
reports on half the work.

## 5. How the fact reaches the timeline

`invariants.py` states as a hard rule that it never calls `gh`. That rule is kept, and the
round trip moves to the daemon.

**`Daemon.discover_pull_requests`**, on the existing `LANDING_SWEEP_EVERY_TICKS` cadence
and immediately before `check_invariants` in the same tick: for each completed order not
already excused, `landing.branches_for` locally, then one `github.pr_list_for_branch` per
branch, then `landing.judge`, then one `pr_discovered` event carrying the verdict and every
pull request seen.

**`invariants.check_work_lands`** reads the newest `pr_discovered` event and nothing else.
It now runs no subprocess at all.

Alternatives considered and rejected:

* *Extend what a worker records on `finish`.* Fixes nothing already on the timeline, and
  the stale case (a pull request opened after the order settled) is by construction
  outside any worker's knowledge.
* *A deliberate exception to the no-`gh` rule for this one check.* Would put a network
  round trip per completed order inside `check_project`, which `jarvis doctor` calls
  synchronously and the daemon calls on every reconcile tick.

### Cost, bounded three ways

* **Cadence** — an hour (`LANDING_SWEEP_EVERY_TICKS`).
* **`PR_DISCOVERY_PER_SWEEP = 25`** — the cap is about the TICK. A project with two
  hundred cold completed orders fills in over about eight sweeps instead of spending two
  hundred round trips inside one. Newest first, which is `list_work_orders`' own order:
  the recently completed orders are the ones a merge is still plausibly coming for.
* **`PR_DISCOVERY_TTL_SECONDS` = 7 days** — a settled answer is re-asked, not kept for
  ever. wo-cd73c537 is why: #116 was opened after #81 merged, so an answer cached at merge
  time would have hidden the exact shape this was rebuilt for. Unsettled answers are
  re-asked every sweep, because those are the ones a merge resolves.

An order with no branch costs nothing at all — no `gh` call — which covers the 60-of-89
planners, investigations and knowledge-base writes the old check needed a whole
`not-produced` rung to exclude.

A `gh` that cannot answer stops the pass and leaves the previous record standing, rather
than continuing per work order. The failure is `gh` itself, identically for every order,
and an empty answer read as a fact would be recorded as `no-pull-request` — which is
silent.

## 6. What this gives up, deliberately

* **An order that delivered nothing.** wo-5a6b2d6d is completed; its only product, a
  config-console design document, exists solely as a WIP commit on `rescue/wo-5a6b2d6d`,
  on no branch that ever merged, with no pull request. Nothing reports it now. The user
  accepts this: the place to catch an order settling with nothing delivered is the
  validation round that let it settle, and that is the sibling work order.
* **Issue #232's Mode C** — a first pull request merges and the worker keeps going. The
  `merged-tail` rung measured the branch against the sha GitHub merged; the pull request
  merged, so the invariant is satisfied, and commits pushed to the branch afterwards are
  no longer this check's business. wo-752eced8 is the live example.
* **A project the daemon has never swept** reports nothing here, because discovery has not
  run. That is the price of keeping `gh` out of the invariant, and it is the same silence
  as "no pull request" rather than a guess.

## 7. Removed

`landing._coverage`, `_significant`, `_subject_landed`, `_count`, `_ref_for`, `assess`,
`refresh_base`, `_fetch`, `_cause`, `_scrub`, `LANDED_COVERAGE`, `STRANDED_COVERAGE`,
`SIGNIFICANT_CHARS`, `FETCH_TIMEOUT_SECONDS`, the `STRANDED` / `PARTIAL` / `NOT_PRODUCED` /
`UNKNOWN` verdicts, `SETTLED_VERDICTS`, and the `landing_checked` cache. Nothing else
called any of them; `jarvis doctor` reached them only through `check_work_lands`.

`authored()` and everything §§1-6 of the superseded spec describe are untouched — the
settle-time refusal is a different question at a different cost, and it was never the one
producing false positives.

`head_oid` on the `pr_merged` event stays, and is now read by nothing. It is the only
record of which commit a merge landed, and `ops.complete_merged`'s contract is that its
event says what happened.
