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

## 3. The second ruling: rely on the other invariant, do not re-derive it

The first implementation of this spec solved the unreliable-`pr_url` problem inside this
check, by discovering pull requests from the work order's BRANCHES. The user struck that
out the same day:

> what you explain about pr_url not correctly reflecting the work done is a bug. The
> invariant should rely on other invariants. If there is any code change made in an order,
> then pr_url must not be empty, and the invariant should rely on that pr_url to check the
> change lands on main once the order gets completed.

So the column is made trustworthy **at its source** and this check reads it:

| Invariant | Holds | Work order |
|---|---|---|
| INV-PR-RECORDED | an order that CHANGED CODE may not settle with an empty `pr_url` | wo-2005a89b |
| INV-WORK-LANDED | once completed, the pull request it recorded must have merged | this one |

A reader of either check alone must be able to see the other half, so the chain is written
into both docstrings by name — `invariants.check_work_lands` and `landing`'s module
docstring from this end.

## 4. The predicate

**Scope: completed work orders with a non-empty `pr_url`.** That is the whole population,
and it is now exactly what it looks like.

| GitHub says about the recorded pull request | Verdict | Reported |
|---|---|---|
| merged | `landed` | no |
| open | `awaiting-merge` | yes — "delivered and waiting on a merge", remedy is merge-shaped |
| closed, unmerged | `refused` | yes — "delivered and refused" |
| anything else | `refused` | yes — an unrecognised state is a reason to look, not to fall silent |
| *(no `pr_url` on the order)* | `no-pull-request` | **no. Out of scope, silent.** |

There is no resolution order any more, because there is no longer a set of pull requests
to resolve: one order, one recorded url, one state.

## 5. How the fact reaches the timeline

`invariants.py` states as a hard rule that it never calls `gh`. That rule is kept, and the
round trip moves to the daemon.

**`Daemon.refresh_landings`**, on the existing `LANDING_SWEEP_EVERY_TICKS` cadence and
immediately before `check_invariants` in the same tick: for each completed order that has
a `pr_url` and is not already excused, one `github.pr_view`, then `landing.judge`, then one
`landing_seen` event carrying the verdict.

**`invariants.check_work_lands`** reads the newest `landing_seen` event and nothing else.
It runs no subprocess at all.

**Why it asks at all, rather than reading the timeline.** `Daemon.poll_pull_requests`
writes `pr_merged` only while an order is parked in `waiting_pr_merge`. An order that
reached `completed` any other way — reviewed, marked done, finished before that poll
existed — has a `pr_url` and no event about it, which is most of the live records.
Inferring "no `pr_merged` event" as "did not merge" would reproduce the false positives
§2 is about.

Alternatives considered and rejected:

* *Extend what a worker records on `finish`.* Fixes nothing already on a timeline.
* *A deliberate exception to the no-`gh` rule for this one check.* Would put a network
  round trip per completed order inside `check_project`, which `jarvis doctor` calls
  synchronously and the daemon calls on every reconcile tick.

### Cost, bounded three ways

* **Cadence** — an hour (`LANDING_SWEEP_EVERY_TICKS`).
* **`LANDING_REFRESH_PER_SWEEP = 25`** — the cap is about the TICK. A project with two
  hundred cold completed orders fills in over about eight sweeps instead of spending two
  hundred round trips inside one. Newest first, which is `list_work_orders`' own order:
  the recently completed orders are the ones a merge is still plausibly coming for.
* **`LANDING_REFRESH_TTL_SECONDS` = 7 days** — a settled reading is re-asked, not kept for
  ever. `landed` is the verdict that SILENCES the check, so the one answer nobody would
  ever re-read is the one that would hide a revert. Unsettled readings are re-asked every
  sweep, because those are the ones a merge resolves.

An order with no `pr_url` costs nothing at all — no `gh` call — which covers the 60-of-89
planners, investigations and knowledge-base writes the old check needed a whole
`not-produced` rung to exclude.

A pull request `gh` cannot read records nothing, so the previous reading stands and its
violation keeps being reported. Reading a failure as an empty answer would be the SILENT
verdict, which would exempt orders invisibly. The pass continues to the next order rather
than stopping: each one asks about a different url, so an unreadable pull request is a
fact about that url.

### An audit with no data must not render as a clean bill of health

Review round 1's finding, and the sharpest thing in this change. Because the refresh is the
daemon's, `check_work_lands` has no data until the first sweep — and it was right to stay
silent about an order it had not looked at, but silence is what `jarvis doctor` renders as
`✓ all OS invariants hold`. A project with genuinely unmerged work read identically to a
clean one, which is worse than any false positive: nothing shows it happening.

So the same function yields a second violation, **`INV-LANDING-AUDIT-FRESH`**: one
project-level line, no `wo_id`, naming how many orders in the population have never been
read or were last read more than `landing.FRESH_FOR_SECONDS` ago. It shrinks as the sweep
fills in, disappears in steady state, and stands for ever on a project whose daemon is not
running — which is exactly what is true. Counting *stale* readings and not just missing
ones is the same defect a week later: `landed` silences the check, so a dead daemon would
otherwise go on reporting nothing off arbitrarily old answers.

`FRESH_FOR_SECONDS` and `REFRESH_PER_SWEEP` therefore live in `landing`, not beside the
daemon's cadences. Both halves need them — one to decide when to re-ask, the other to
decide when it has no answer and to say how fast the backlog drains — and two copies of
that number would let the audit go quiet at the exact moment it stopped knowing anything.

## 5b. The remedy has to CLEAR the alert, and for months it did not

Found while trying to clear the eight live `jarvis_os` alerts by hand on 2026-09-18. Both
routes out of this violation were no-ops for most of the orders it fires on.

**`jarvis wo done`.** `ops.mark_done` wrote its `work_unlanded` exclusion only when
`ops.unlanded_work` reported something, and that reads the WORKTREE while this check reads
the PULL REQUEST. Five of the eight alerted orders had no worktree left on disk. Worse,
`unlanded_work` answers "nothing" for ANY order carrying a `pr_url` — so across the whole
of this check's narrowed population it could never record an exclusion at all. Fixed with
`ops.unmerged_pull_request`: the order's recorded pull request, unless a `pr_merged` event
or a settled `landing_seen` says it merged. No round trip — this runs inside a CLI command
somebody is waiting on. Nothing having looked yet reads as unmerged, which over-records
rather than under-records, and `work_unlanded_open`'s episode arithmetic retires it when a
`pr_merged` lands.

**`jarvis wo finish --abandon`.** The abandonment itself was always written. What went
wrong is everything after it: `finish` submitted the order for validation, the panel
answered "this submission changes no files and records no other durable effect", the order
was escalated to `needs_review` and an attention item appeared. The user cleared one alert
and was handed a different one (`wo-5eedc84d`, 2026-09-18). `--abandon` on an order whose
status is already terminal now records and returns — recording a decision about finished
work is not a delivery. A LIVE order abandoning still opens a round, because there the
panel has something to look at; both halves have a test.
## 6. What this gives up, deliberately

* **An order that delivered nothing.** wo-5a6b2d6d is completed; its only product, a
  config-console design document, exists solely as a WIP commit on `rescue/wo-5a6b2d6d`,
  on no branch that ever merged, with no pull request. Nothing reports it now. The user
  accepts this: the place to catch an order settling with nothing delivered is the
  validation round that let it settle, and that is the sibling work order.
* **An order whose `pr_url` names the wrong pull request.** wo-cd73c537 records a merged
  #81 while #116 carries the rest of its work and is open; this check reads #81, sees
  MERGED and says nothing. A known consequence of the layering in §3, not a defect of this
  check: a `pr_url` that does not reflect the work is a RECORDING defect, and recording is
  INV-PR-RECORDED's half. Asserted as a test, so it cannot turn into a surprise.
* **An order the daemon has never asked about.** No verdict until the first sweep — but
  it is COUNTED and reported by `INV-LANDING-AUDIT-FRESH` rather than passed over, so the
  absence of data is visible. See §5.
* **Issue #232's Mode C** — a first pull request merges and the worker keeps going. The
  `merged-tail` rung measured the branch against the sha GitHub merged; the pull request
  merged, so the invariant is satisfied, and commits pushed to the branch afterwards are
  no longer this check's business. wo-752eced8 is the live example.

## 7. Removed

`landing._coverage`, `_significant`, `_subject_landed`, `_count`, `_ref_for`, `assess`,
`refresh_base`, `_fetch`, `_cause`, `_scrub`, `LANDED_COVERAGE`, `STRANDED_COVERAGE`,
`SIGNIFICANT_CHARS`, `FETCH_TIMEOUT_SECONDS`, the `STRANDED` / `PARTIAL` / `NOT_PRODUCED` /
`UNKNOWN` verdicts, `SETTLED_VERDICTS`, and the `landing_checked` cache. Nothing else
called any of them; `jarvis doctor` reached them only through `check_work_lands`.

Nothing was added to `github.py`. The first implementation of this spec added
`pr_list_for_branch`, `BranchPullRequest`, `BRANCH_RE` and a third entry in
`READ_ONLY_VERBS`; §3 removed the need for all of it, and the module is unchanged from
`main`.

**`landing._scrub` and `_CREDENTIALS_RE` are KEPT**, though the code that motivated them
(`_fetch`) is gone and no caller of `_git` touches a remote today. Review round 1 caught
that deleting them left the fleet with no credential scrub anywhere and no assertion that
a token in an `origin` URL never reaches a log — while `_git` still pipes git's stderr
into a `log.warning`. Four lines, on the function rather than on this month's callers, and
two tests: a unit assertion against the verbatim authentication-failure string git does
NOT self-redact, and an end-to-end one driving `_git` at an unreachable remote. The scrub
runs BEFORE the 200-character truncation, because the URL is on the first line.

`authored()` and everything §§1-6 of the superseded spec describe are untouched — the
settle-time refusal is a different question at a different cost, and it was never the one
producing false positives.

`head_oid` on the `pr_merged` event stays, and is now read by nothing. It is the only
record of which commit a merge landed, and `ops.complete_merged`'s contract is that its
event says what happened.
