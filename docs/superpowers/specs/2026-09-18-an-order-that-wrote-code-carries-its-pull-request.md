# An order that wrote code must carry its pull request

2026-09-18. Work order wo-2005a89b. The other half of wo-16a488ee
(`2026-09-18-the-landing-invariant-judges-the-pull-request.md`).

## 1. The ruling

> "checking branches for which no PR were created shouldn't be checked by the invariant,
> instead, should be rejected during the validation"

> "what you explain about pr_url not correctly reflecting the work done is a bug. The
> invariant should rely on other invariants. If there is any code change made in an
> order, then pr_url must not be empty, and the invariant should rely on that pr_url to
> check the change lands on main once the order gets completed"

wo-16a488ee narrows INV-WORK-LANDED to judging the recorded pull request and nothing
else. That narrowing is safe only if something guarantees the pull request is recorded.
This is that guarantee, and the two are a chain:

    INV-PR-RECORDED   an order that wrote code carries a pr_url (or a written decision)
    INV-WORK-LANDED   that pr_url merged

Read either alone and its scope looks like a hole. Both docstrings say so.

## 2. The evidence

Verified against the live `jarvis_os` records on 2026-09-18:

| order | shape |
|---|---|
| wo-5eedc84d | `pr_url` NULL; PR #42 existed throughout and merged 2026-07-29 |
| wo-cd73c537 | `pr_url` records #81 (merged) while #116 is open with three unmerged commits |
| wo-5a6b2d6d | completed over a WIP commit on `rescue/wo-5a6b2d6d` with no pull request, ever |

All three settled before the issue #232 guard shipped (2026-09-13), so the guard is young
rather than absent. What remained open is §3.

## 3. The settlement routes, and which were unguarded

`ops.unlanded_work` is the predicate; each route picks the verb its audience can act on.

| route | verb | before |
|---|---|---|
| `ops.finish` | RAISES — the worker is listening | guarded |
| `ops.land_finished` (round machine PASS, `review_work_order`, `accept_assumption`) | PARKS at `needs_review` | guarded |
| `ops.mark_done` | RECORDS and proceeds — the user typing it IS the decision | guarded |
| `Daemon.settle_work_order`, the turn-settling branch | — | **UNGUARDED** |
| `Daemon._close_feature_manager` | — | **UNGUARDED** |
| `retire_ungoverned`, INV-ADHOC-* | — | out of scope: no worker contract |

The reconciler branch completed any order whose turn ended with a `result_summary` and no
`pr_url`, straight to `completed`, never asking `land_finished` — whose docstring claims
every route to `completed` passes through it. It also UNPARKED: `park_unlanded` leaves
`needs_review`, and the next tick over the same done turn completed the order it had just
held. It now calls `land_finished`, which also closes the backlog item — the drift that
function exists to prevent.

`_close_feature_manager` records rather than parks: the feature is over, and parking its
manager would put an attention item on a settled feature.

### 3a. The park has to be idempotent, and it was not

Routing the reconciler through `land_finished` is only half a fix. That branch re-derives
from the *latest* turn on every tick, which is why it unparked in the first place — so it
reaches `park_unlanded` again on the tick after a park, over the same done turn, with
nothing changed. `park_unlanded` wrote unconditionally, which trades "unparks and
completes" for a fresh `work_unlanded` and a fresh `attention` every tick: the
renotify-on-every-restart shape rule 3 of `invariants.py` exists to forbid. Measured
before the fix: five ticks, five `work_unlanded` events.

`park_unlanded` is now keyed on the EPISODE — `work_unlanded_open`, which already means
"parked, with no `finished`, `abandoned` or `pr_merged` since". Not on "has this order
ever been parked", which would be the same defect one step along: the order would
silently stop being recorded as unlanded the moment it had been once. A re-delivery
writes one of those three events, so the next park is a new episode and does record.
Both directions have a test, and the wrong key fails the second one.

## 4. How the invariant knows, months later

Neo question 429. `landing.authored` is exact only while the worktree exists, and a
worktree is deleted long before anyone audits — which is how wo-5a6b2d6d stayed invisible.
So every settlement writes the `Authored` record onto the event it already writes, and
INV-PR-RECORDED is a pure timeline read: `INVARIANTS`, not `SLOW_INVARIANTS`, no
subprocess, no `gh`.

Rejected: deriving the population from the repository at check time (`branches_for` plus a
commit count). It answers for historical orders, but it is a per-order walk on every
sweep, and in a squash-merging repository the count stays above zero forever — re-importing
the content reasoning the user had just ruled out of INV-WORK-LANDED.

`landing.authored_in` keeps **"nobody looked"** distinct from **"produced nothing"**.
Collapsing them would let an unreadable settling read afterwards as an exoneration.

## 5. What this gives up, stated rather than hidden

- **Orders settled before this ships are structurally silent**, including all three above.
  The user accepted that explicitly: those are being closed by hand, and this exists to
  stop the next one. It is also what keeps a fleet's whole settled backlog from lighting
  up on upgrade.
- **A STALE `pr_url` is out of scope.** wo-cd73c537 records a merged #81 while #116 is
  open. This predicate asks whether an identifier is PRESENT — a fact the record holds.
  Asking whether it is CURRENT is a `gh` round trip per settled order and belongs to
  `Daemon.discover_pull_requests` (wo-16a488ee). Filed as `bl-2aabaee8`.
- **Not repairable.** An empty `pr_url` has two resolutions — find the pull request that
  exists, or record that none ever will — and nothing in the database distinguishes them.
  Discovery is what closes the gap, and it is the daemon's.

## 6. Acceptance

`tests/test_pr_recorded.py`, one test per route plus the negative control that decides
whether it ships: an order that produced no code settles exactly as before.
