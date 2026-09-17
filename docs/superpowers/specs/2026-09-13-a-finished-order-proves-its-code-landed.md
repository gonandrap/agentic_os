# A finished work order proves its code landed

GitHub issue #232. Companion to `2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md`
(#224) one level up: that one is "nothing reads CI before the merge", this one is
"nothing checks a merge ever happened".

## 1. The finding

A fleet-wide audit of all 209 work orders found SIX whose code exists only on a remote
branch and never reached `main`. Two are pull requests open for seven weeks carrying
~3,100 lines between them. Every one was found by a human going looking.

`completed` was reachable without any evidence that a branch merged. The OS took "the
worker says it is done" as done. Three routes got there:

| Mode | What happened | Orders |
|---|---|---|
| A | `jarvis wo finish` with a summary NAMING a draft PR in prose and no `--pr`, so `pr_url` stayed NULL and no poller ever watched it | 4 |
| B | worker went idle without finishing; the user closed it by ACCEPTING ITS ASSUMPTIONS, which completed it and wrote `result_summary` NULL and `pr_url` NULL over the fact that a commit existed | 2 |
| C | the first PR merged, the worker kept working, the tail was left uncommitted or in a second PR nobody merged | 3 |

Mode C is why an audit keyed on `pr_url` finds nothing: those orders HAVE one and it
points at a pull request that DID merge.

## 2. Two questions at two distances, and they must not be confused

`landing.authored` is the SETTLE-TIME predicate: has this worktree produced anything —
commits over its merge base, or uncommitted files? Exact, local, two `git` invocations,
no network, no heuristic. It runs on every `jarvis wo finish`, so it must cost nothing
and must be certain.

`landing.assess` is the AUDIT: is what this order produced on the default branch, months
later? That has no exact answer here (§3), so it is a ladder from exact to heuristic and
reports which rung answered. It costs a `git` call per touched file and runs on
`jarvis doctor` and the daemon's slow sweep, never on a worker's critical path.

## 3. The repository squash-merges, which kills every obvious test

A squash replays a branch's whole diff as ONE new commit with a new sha. So commit
reachability (`merge-base --is-ancestor`), the ahead-count (`rev-list --count base..br`)
and patch-ids all report EVERY branch in the fleet as unmerged — including the ~96 that
landed. A checker built on any of them flags everything, which is worse than flagging
nothing: it is switched off within a day and the sixth stranded order becomes the
seventh.

What survives a squash is CONTENT. `landing._coverage` measures what fraction of the
significant lines a branch added can be found in the default branch's copy of the same
file. Per FILE, never across the tree — searching the whole tree would score a moved
import as landed work.

The commit-subject test the original audit used (does `main` carry a commit titled
`[<wo-id>]`) is here too, as a CORROBORATING rung only. It is a false-negative machine:
only 96 of this repository's 173 `main` commits carry that prefix, because the convention
postdates half of them. It may say LANDED and it may say UNKNOWN; it may never say
STRANDED.

## 4. The thresholds were measured, not chosen

Run across every branch in this repository before a line of the checker was written. The
two populations separate with a wide gap either side of 0.5:

```
cov=  0.00  worktree-wo-cd73c537     <- issue #232, Mode C
cov=  0.03  worktree-wo-036e8be2     <- issue #232, PR #32 open 7 weeks
cov=  0.05  worktree-wo-cba17617     <- #224, genuinely unmerged
cov=  0.11  worktree-wo-1c205d10     <- issue #232, PR #26 open 7 weeks
cov=  0.12  rescue/wo-69a06ff4       <- issue #232, Mode B
        ...  (a gap: 0.22, 0.31, 0.44, 0.69)
cov=  0.79 .. 1.00                   <- ~90 branches that landed
```

`STRANDED_COVERAGE = 0.25` and `LANDED_COVERAGE = 0.75` sit well inside that gap rather
than on its edges, so a branch has to move a long way before its verdict changes. Between
them is `PARTIAL`, which is a real answer rather than a shrug: it is Mode C's exact shape,
where a first pull request merged and the work after it did not — `rescue/wo-0fea6edb`
scores 0.69 and is `PARTIAL` on coverage alone.

`rescue/wo-4576667e` is NOT, and the distinction is worth writing down because the first
draft of this spec got it wrong. It scores 0.87, which is above `LANDED_COVERAGE`, so the
thresholds alone call it `landed` — and that is what `assess` returns for it today,
measured, not argued. Coverage only reaches `PARTIAL` for a branch this well landed
through the second route into that verdict: the `dirty` clause, which demotes a `LANDED`
branch whose worktree still holds uncommitted work. Its worktree was deleted in the same
disk sweep that took the other 172, so there is no longer anything for that clause to
find. The measurement this file is built on stands; the claim that all three Mode C
orders show up as `PARTIAL` did not, and the honest version is that the coverage rung
catches the ones whose tail is big enough to move the number. Both routes are pinned by
tests (`tests/test_landing.py`), because a `PARTIAL` that quietly reads `LANDED` is
CACHED and never recomputed — a permanent silent all-clear, which is the exact failure
this whole file exists to prevent.

## 5. The negative controls are half the value

Most work orders produce no code: a planner whose deliverable is a plan, a knowledge-base
write, an investigation, a release. The audit found 60 such orders among 89 candidates —
a check without that exclusion has a 67% false-positive rate.

The exclusion is DERIVED, not listed. A branch with no commits over its base and a clean
worktree produced nothing, so there is nothing to land. A list of work-order kinds to skip
would rot the first time somebody invents a kind, and would be wrong anyway: an
investigation that does commit a script has produced something.

## 6. One predicate, three verbs

`ops.unlanded_work` is the whole rule and is deliberately narrow (Neo question 280):
commits or uncommitted files in the worktree, AND no pull request. Each landing then does
what its caller can act on:

- **`ops.finish` RAISES.** It is the moment the evidence is freshest and the one person
  who can act on it is listening. `--abandon "<why>"` is the way through and is meant to
  be cheap: an abandonment is a legitimate outcome, and what is being enforced is that
  the decision is WRITTEN DOWN, not that it is prevented. Mode A rides here — the refusal
  reads the summary and names any pull-request URL it finds in the prose.
- **`ops.land_finished` PARKS** at `needs_review` with `invariants.UNLANDED_BLOCKER`, and
  writes a `work_unlanded` event carrying the branch, the commit count and the files that
  were never committed. Two of its three callers are the daemon's round machine and
  `review_work_order`, where an exception would break a tick or a user's command. Every
  route to `completed` passes through it, which is why the check is there and not in each
  of them. **This is Mode B's fix**: accepting assumptions answers a question about
  assumptions and is no longer a closure verb for an order with unpushed commits — the
  mirror of the rule `wo ack` and `wo done` already follow.
- **`ops.mark_done` RECORDS AND PROCEEDS.** The one that does not refuse, because it is
  not silence: a user typing `jarvis wo done` over a pull request that will never merge
  is the documented exit, and refusing it would leave them no way to close the order.

### The pull request is read from the record, never from the caller's dict

`review_work_order` blanks `pr_url` before landing when the poll has already settled that
pull request — and for a MERGED one the code IS on the default branch. A landing that
trusted the blanked copy would refuse to complete a work order over the very commits that
landed it: "flags everything", arriving by the back door.

### The flag must be re-derivable

`invariants.true_blockers` derives `UNLANDED_BLOCKER` as the THIRD way to arrive at
`needs_review`, from `ProjectStore.work_unlanded_open` — a `work_unlanded` event newer
than any `finished`, `abandoned` or `pr_merged`. Without that, INV-ATTENTION-REASON
relabels it next tick as the generic "worker stopped without finishing", which is the
opposite of what is true. kn-eafe383a is this exact bug one level over.

## 7. The standing report

`INV-WORK-LANDED` sweeps `completed` work orders, hidden ones included (hiding drops a
record from listings; it does not mean the record may go on saying something untrue).

**It never asks GitHub.** Everything it needs about a pull request it reads off the work
order's own timeline — `pr_merged`, and the `head_oid` that event now carries, which is
what answers Mode C exactly. Never `pr_state`, which kn-dbc4971d records as stale by
construction with one permitted reader.

### The default branch has to be refreshed, and the property is narrower than "no network"

This section originally said "no network, ever", and meant it about re-asking `gh`. It
was also true of `git`, and that was a bug (issue #271). The sweep measures against
`origin/main`, a remote-tracking ref nothing in the OS ever moved, while the merge that
ends a work order is detected over the NETWORK — so the order completed with the local
ref still pointing at the commit before its squash, and the content test looked for the
branch's lines in a copy of the file that predates the merge. One false `STRANDED` per
merge, repeating every hour, on the checker whose own module docstring says a checker
that flags everything gets switched off within a day.

The property is now: **no network on the read-only path; one bounded fetch of the default
branch per project per repairing sweep.** `landing.refresh_base` does it — a single
explicit refspec, `--no-tags`, a timeout, non-fatal — and the daemon (`repair=True`) and
`jarvis doctor --repair` are the only callers that pass `allow_network=True`. A plain
`jarvis doctor` does not write to a repository, which is a promise `ops.run_doctor` makes
in print.

**And the guard is unconditional, because the refresh can fail.** `assess` takes
`base_current` and will not condemn a branch off a ref that was not refreshed THIS sweep:
a failed fetch, an offline machine and the read-only path all demote the coverage rung's
absence-derived verdicts — every `STRANDED`, and the `PARTIAL` of a middling score — to
`UNKNOWN` at the `stale-base` rung. Keyed on the evidence, not on the verdict's name: the
`PARTIAL` of a full score beside a dirty worktree rests on presence plus a fact about the
worktree, so staleness does not touch it and it is kept. Neither half is
sufficient alone. Without the refresh, the sweep still condemns a branch whenever the
network blips; without the guard, the false positive becomes a permanent blind spot,
since `UNKNOWN` is never cached and a fleet where nobody fetches would never confirm a
landing again — the "quietly stopped working" failure §4 calls worse than no checker.

Only absence is affected. `LANDED` stands whatever the base's age, because a branch only
grows and lines found on an out-of-date default branch are on the up-to-date one too; the
`pull-request-open` and `merged-tail` rungs never read the base at all. So a read-only
doctor still reports Mode C — a branch carrying commits after the sha that merged — and
what it withholds is the content measurement, which is the only thing staleness touches.

### The `merged-tail` rung is blind to the fleet that already exists

`head_oid` starts being written by `ops.complete_merged` from this change onwards. Every
`pr_merged` event already on a timeline — including the three Mode C orders that prompted
this — was written without one, and nothing backfills it: the sha is GitHub's answer to a
poll that has already happened, and re-asking for 209 work orders is a network sweep this
invariant exists to not be. So for the existing fleet the exact rung is skipped and those
orders fall through to `coverage` and to the `dirty` clause under it. The exact rung is
for the orders that merge from now on. This is a deliberate asymmetry, not an oversight:
a run of `unknown`s from a rung that cannot fire would be worse than a measurement that
can.

**And `coverage` does not catch all three of them.** `rescue/wo-0fea6edb` scores 0.69 and
is reported `PARTIAL`; `rescue/wo-4576667e` scores 0.87 and is reported `landed`, because
its tail is a small enough fraction of what the branch added to sit above
`LANDED_COVERAGE` and its worktree — the other thing that would demote it, via the `dirty`
clause — no longer exists to be read (§4). That is the honest cost of a heuristic rung
standing in for an exact one: for orders that merged before this shipped, a SMALL Mode C
tail is under the floor of what content can see. It is not a cost this change can pay off
without the network sweep it exists to avoid, and it shrinks to nothing as the fleet's
`pr_merged` events start carrying `head_oid`, which makes the exact rung answer instead.

The verdict is CACHED, and only the settled half. `landed` and `not-produced` are recorded
as a `landing_checked` event and never recomputed: a completed order's branch has stopped
moving. `stranded`, `partial` and `unknown` are re-derived every sweep, because those are
the ones a merge or a push resolves — a check that remembered its complaint would go on
making it after the user fixed the thing.

It runs in `invariants.SLOW_INVARIANTS`, off by default: `jarvis doctor` always runs it
(a human who typed the command is waiting for the answer), the daemon rations it to
`LANDING_SWEEP_EVERY_TICKS` — an hour, and a multiple of `RECONCILE_EVERY_TICKS` so the
two cadences line up instead of beating against each other. An hour is far inside the
window that matters: the orders this found had been stranded for seven weeks.

## 8. Out of scope

Rescuing the six. They are safe — four on origin as `rescue/<wo-id>` or their original
branches, two as open pull requests — and what to do with each is the user's call,
separately. This is about the seventh never happening.
