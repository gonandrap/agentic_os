# A work order never sits on a red pull request

*wo-cba17617, 2026-09-13. Issue #224. The `needs_review` decision is Neo's, on question
275. Reverses two of the four "deliberately not built" items in
[2026-08-22-a-work-order-heals-its-own-pull-request.md](2026-08-22-a-work-order-heals-its-own-pull-request.md)
§7 — read that spec first; this one is an extension of its machinery, not a new one.*

## 1. The problem

wo-a6af01f0 escalated out of validation into `needs_review` behind PR #206. That pull
request had three failing unit jobs across 3.11/3.12/3.13 and a branch behind `main`,
and nothing in Jarvis noticed: no inbox entry, no attention item, no message to the
worker. The user found it by hand and relayed the failure themselves — which is the
manual step the conflict heal loop exists to remove, in the one state where it costs the
most, because the work order was on the user's list asking them to merge it.

Three gaps, and the middle one is why fixing either of the others alone changes nothing.

**A. The poll only polled one status.** `Daemon.poll_pull_requests` selected
`statuses=('waiting_pr_merge',)`. A work order in `needs_review` carrying a `pr_url` was
never polled at all.

**B. The poll never read CI, in any status.** `PR_FIELDS` was
`state,mergedAt,mergeable,baseRefName`. The loop branched on merged / closed-unmerged /
CONFLICTING / mergeable, so a pull request with green mergeability and red checks was
indistinguishable from a healthy one.

The irony worth not repeating: PR #206 itself ADDED check-run reading — into
`github.pr_artifact`, for the validation panel, on a separate field set. The OS could
see CI when it JUDGED a submission and still could not see it while it WAITED for the
merge.

**C. A branch merely BEHIND its base was never healed.** `heal_pr_conflict` fired only
on `mergeable == 'CONFLICTING'`.

## 2. One reader, two field sets

`PR_FIELDS` gains `statusCheckRollup` and `mergeStateStatus`. It does **not** converge
with `ARTIFACT_FIELDS`, and that is a decision rather than an omission: the poll runs
every ~2 minutes for every open pull request in the fleet, and the artifact set carries
`body`, `files` and a second `gh pr diff` round trip — a panel's worth of payload, to
answer three questions. `tests/test_github_artifact.py` asserts the two differ; it still
passes unchanged, because `body` is still absent here.

What DOES converge is the reader. `github.read_checks` is the only code in the OS that
interprets a check, and both `pr_view` and `pr_artifact` call it. The alternative —
a second reader in the poll — is how the OS ends up judging a submission by a standard
it does not police while it waits, which is the shape of the whole bug.

**The vocabulary is the substance of this section.** "Not green" is not "red":

| conclusion | meaning | nudges? |
|---|---|---|
| `SUCCESS` | passed | no |
| `FAILURE`, `TIMED_OUT`, `ACTION_REQUIRED`, `ERROR` | the code is wrong | **yes** |
| `CANCELLED` | fail-fast killed a sibling, or a human stopped the run | no |
| `SKIPPED`, `NEUTRAL`, `STALE` | did not judge this code | no |
| queued / in progress | has not finished telling anyone | no |
| no checks at all | this repository runs no CI | no |

`ERROR` is a legacy commit status's spelling of `FAILURE`; a repository can carry check
runs and commit statuses at once, and `read_checks` normalises both into the same two
keys. `github.RED_CONCLUSIONS` is the set, and `failing_checks` returns what IS failing
rather than a "not passing" verdict — the inverse phrasing is what would make a PENDING
run nudge somebody.

The live run this was built against (34741340610) is the argument for the CANCELLED row:
one job FAILED and fail-fast CANCELLED its two siblings. Reading it as three failures
would name two checks in the nudge that nobody broke.

## 3. One repair, two problems

`ops.PrRepair` is a descriptor: a name, a message template and a blocker. There are two
instances — `PR_CONFLICT` and `PR_CHECKS` — and everything else is shared code:

* `ops.nudge_pr_repair` queues the message and records the attempt;
* `ops.clear_pr_repair` closes the episode when the problem goes away;
* `Daemon.heal_pull_request` adds the same three guards `heal_pr_conflict` had (no
  session to resume, a nudge already queued, a turn already in flight — §3 of the
  2026-08-22 spec for why each would otherwise cost a duplicated turn or silently spend
  the budget);
* `invariants.PR_REPAIR_MAX_ATTEMPTS` is one cap for both.

Shared VALUE, separate BUDGETS. `ProjectStore._this_episode` counts each repair's nudges
since that repair's own `cleared`, so a branch that conflicted twice last week has spent
nothing of the red build's three attempts. They are different problems with different
fixes, and a shared counter would silently deny one of them its tries.

Nothing here runs git or writes to GitHub. The daemon notices and asks; the worker is
the process holding the context to decide what a fix looks like.

## 4. `needs_review`, and the trap

Every status where a pull request can sit with nobody moving it is polled:
`PR_POLL_STATUSES = waiting_pr_merge, needs_review, waiting_input, failed`. The in-flight
statuses (`running`, `dispatching`, `validating`) are out — something already owns those,
and `complete_merged` on one would end a work order out from under a worker still writing
to it.

**A red build on a `needs_review` work order means the USER is the one being asked to
merge something broken.** So the nudge has to reach the worker without the item leaving
the user's list — and two existing behaviours would have taken it off:

1. `Daemon._deliver` sets any work order it delivers to `running` and clears attention.
2. `Daemon.settle_work_order` parks any done turn carrying a summary and a `pr_url` into
   `waiting_pr_merge`.

Together those end the repair by silently downgrading a review item into a merge-queue
entry — the OS looking like it had handled something it had only half handled.

Neo (question 275) chose the smallest seam: **the repair turn returns the work order to
the status it came from.** `nudge_pr_repair` records `was` on the nudged event, and
`settle_work_order` restores it instead of unconditionally parking. Only an OPEN episode
answers — `ProjectStore.pr_repair_origin` — because a cleared episode is a problem that
is over and its origin must not outlive it. Derived from the timeline for the reason the
attempt count is: no column to drift from what the user reads.

The attention flag is NOT carried through the repair. It comes back with the status,
because `true_blockers` re-derives a `needs_review` order's reason from its own record
and INV-ATTENTION-MISSING puts the flag back. The blind window is the repair turn plus
one reconcile tick, and it is bounded. The rejected alternative was exempting unauthored
messages from `_deliver`'s `clear_attention`: `running` derives no such blocker, so
INV-ATTENTION-REASON would fight it every tick unless `true_blockers` also learned what
an in-flight repair is — a second piece of state to keep in step, which is the thing the
timeline-derived design exists to avoid.

`PR_CHECKS_BLOCKER` is raised only on the give-up, and it is derived in `true_blockers`
**above** the `needs_review` triage. `attention_reason` is one column fed from
`blockers[0]` (kn-d4d5a967), so ranking it below would mean the user never reads it —
and it is the fact that changes what they do next, on the same precedent that ranks a
closed pull request above the panel's verdict. The status still says `needs_review`,
which is the rest of the story.

One consequence of widening the poll: `record_pr_closed` is now guarded on
`pr_state != 'CLOSED'`. Before, moving a work order to `needs_review` took it out of the
polled set, so re-running was unreachable; now it stays in, and an unguarded call would
write a `pr_closed` event every couple of minutes for ever.

## 5. BEHIND: reported, never rebased

**Decision: the OS says so and the worker acts; the OS never updates the branch itself.**
Three reasons, and the first is the one that matters:

1. `github.READ_ONLY_VERBS` is load-bearing. The panel's blind review rests on this
   module being unable to write to GitHub — a seat that could comment on a pull request
   could talk to the implementor it is judging (Neo question 251). `gh pr update-branch`
   is a write verb; adding it to buy a rebase would trade away a security property for a
   convenience.
2. A branch update is a history rewrite on a branch a worker may still be sitting on.
3. §7 of the 2026-08-22 spec priced it: a full worker turn — the whole conversation
   re-sent at the cache-write rate — every time `main` moves.

So BEHIND never causes a nudge. It rides along in one that was going out anyway
(`PR_BEHIND_NOTE`), where it costs nothing: the worker is already in its worktree with a
push to make.

That spec's §7 claimed BEHIND "does not block a merge in this fleet". **That is now
wrong** and was worth checking rather than inheriting: the `protect-main` ruleset sets
`strict_required_status_checks_policy: true`, so a behind branch cannot merge. Reporting
it is therefore not cosmetic — it is the difference between the user pressing merge and
the user being told why they cannot.

## 6. What the record shows

`timeline._describe` gains the matching three, in the words a red build wants rather
than a conflict's: `pr_checks_nudged` ("Failing checks — asked the worker to fix them",
with the names and the attempt), `pr_checks_cleared`, `pr_checks_unresolved`. The
message source `pr-checks` joins `timeline.UNAUTHORED_SOURCES`, so it renders as
"Jarvis → worker": a poll wrote it, and the record must not claim the user typed it.

The user is not notified of a heal, for the reason a merge notifies nobody. Only the
give-up in §3 reaches them.
