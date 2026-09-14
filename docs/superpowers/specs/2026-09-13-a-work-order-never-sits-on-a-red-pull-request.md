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

### 3.1 One derivation site, because derived is not read

The two give-ups are derived together, from `invariants.PR_REPAIR_BLOCKERS`, in that
tuple's order. They were not: the red build was derived above the `needs_review` triage
and the conflict below it, which was harmless while the conflict only ever appeared in
`waiting_pr_merge` — a status the triage does not touch. Widening both onto
PR_REPAIR_STATUSES (§4) gave that asymmetry teeth. In the statuses it added, a conflict
give-up was appended *after* the panel-gave-up line, and `attention_reason` is one column
fed from `blockers[0]` (kn-d4d5a967): the blocker was computed on every reconcile tick
and never shown to anybody.

Derived-but-unread and never-derived are the same thing to the user, which makes this
issue #224's own bug at one remove — the OS knowing something about a pull request and
saying nothing. The general rule, and the reason it is written here rather than left to
the code: **two blockers that can be true at once must be ranked at one site.** Ranking
by where the `append` happens to sit distributes the decision across a function nobody
reads top to bottom.

Conflict ranks above the red build: a pull request that will not merge at all is not
waiting on its checks.

### 3.2 A refusal closes every episode

`ops.record_pr_closed` clears both repair episodes. Until it did, only the
open-and-mergeable branch of the poll ever closed one, so a red pull request that spent
its three attempts and was then shut without merging went on saying *"do not merge it as
it stands"* — above the news that nobody is going to. A true line hiding a truer one is
the shape kn-b6977de3 describes, and closing the episode at the closure is what makes
§3.1's ranking unnecessary rather than merely favourable: a refused pull request has no
give-up left to outrank its refusal, so `true_blockers` never has to choose between them.

Reopening therefore starts a fresh budget, which is the right answer: the fix the worker
never landed is three attempts away again, not zero.

## 4. `needs_review`, and the trap

Every status where a pull request can sit with nobody moving it is polled:
`invariants.PR_REPAIR_STATUSES = waiting_pr_merge, needs_review, waiting_input, failed`,
which `Daemon.PR_POLL_STATUSES` aliases. The in-flight statuses (`running`,
`dispatching`, `validating`) are out — something already owns those, and
`complete_merged` on one would end a work order out from under a worker still writing to
it. The terminal ones are out for a sharper reason: **an episode is only ever closed by a
poll**, so a user who reads the red build and merges anyway ends in `complete_merged`
with the episode still open, and a blocker derived on `pr_url` alone would leave a
finished work order saying "do not merge it as it stands" for ever.

### 4.1 A status set is not enough: the round owns the session

Excluding `validating` from the polled set was the whole of this guard until issue 212
landed, and issue 212 is precisely what made "the round machine owns this work order" and
"the status says `validating`" stop being the same sentence. `ops.land_when_cleared`
parks a work order in `needs_review` with its round running underneath, and
`Daemon.run_validation_rounds` selects over ROUNDS for exactly that reason — its own
docstring calls the status query "the whole of why validation waited on the user".

This poll selects over statuses, and `needs_review` is one of them. So both loops can
claim the same work order in the same moment: the round runner sending the panel's
feedback and re-running the turn while `heal_pull_request` sends a repair nudge. Two
writers to one worker session, and the branch head moving under the seats mid-round.
None of the first three guards can see it — the worker's session is idle while the panel
deliberates, so `worker_session.busy` says False.

So there is a fourth guard, and it is keyed off the round: `ProjectStore.validation_round_open`
is `work_orders_awaiting_validation` asked about one work order, off the same
`RUNNABLE_VALIDATION_OUTCOMES` and the same latest-round rule, so the two cannot answer
differently. **Deferred, never dropped** — nothing is written, no attempt is spent, and
the work order re-enters the poll on the tick after the round settles with the pull
request still red.

`rejected` is deliberately outside that set. It means the panel is waiting for the
SUBMITTER rather than deliberating: the worker is the one who acts next, and a red build
it is about to push over is exactly what it needs told. That window belongs to the
turn-in-flight and nudge-already-queued guards.

The general shape, and the third time this work order met it: **widening a selection
invalidates guards justified by the old narrowness, including guards in code you never
touched** (kn-b6977de3). Here the old narrowness was somebody else's — issue 212 widened
what a round could sit under, and this branch widened what the poll looks at, and neither
change is wrong alone.

ONE tuple, in one place, because the two halves have to agree. A status the poll nudges
in but `true_blockers` does not derive for raises a give-up flag nothing can re-derive,
and INV-ATTENTION-REASON relabels it on the next tick. That is why **both** repair
blockers moved onto this set: `PR_CONFLICT_BLOCKER` was gated on `waiting_pr_merge`, which
was correct only while the poll looked nowhere else. Widening a poll obliges you to widen
every blocker it can raise.

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

One consequence of widening the poll: `record_pr_closed` needs a guard. Before, moving a
work order to `needs_review` took it out of the polled set, so re-running was
unreachable; now it stays in, and an unguarded call writes a `pr_closed` event and
re-flags the user every couple of minutes for ever.

**The guard is derived from the timeline, not from `pr_state`.** That column is stale by
construction with exactly one permitted reader (kn-dbc4971d), and nothing ever cleared
it — so a guard reading it would latch on the first closure, and a pull request closed,
reopened and closed again would never tell the user the second time. That is this bug's
own silence, reintroduced by the guard against it.
`ProjectStore.pr_closure_told` is the same episode arithmetic as the repairs: a
`pr_closed` newer than the newest `pr_reopened`.

That needs a re-arming half, so `Daemon._note_reopened` writes `pr_reopened` when the
poll sees an open pull request the record still calls refused — closing the gap
kn-a94cbd68 filed. It also clears `pr_state`, because `PR_CLOSED_BLOCKER` is derived from
that column and leaving it would keep asserting a refusal that has been withdrawn; the
poll is the column's only writer, so this is the writer finally clearing what it wrote.
The attention reason is **relabelled from `true_blockers`, not cleared** — clearing would
drop the flag until INV-ATTENTION-MISSING put it back, logging a violation every time the
code worked.

The STATUS is deliberately untouched. Reopening a pull request is a button press, not a
decision: the work was refused, what to do about that is still the user's, and moving the
order back to the merge queue would take the item off their list on GitHub's say-so.

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
push to make, so the re-run happens against what would actually land.

**Say plainly what that does and does not cover.** `PR_BEHIND_NOTE` is only ever
formatted into the failing-checks nudge. The conflict branch does not pass it — a
conflicting branch is about to be merged with its base anyway, which is the cure for
BEHIND as well — and the green branch nudges nobody at all. **So a pull request that is
green, non-conflicting and merely behind is reported to no one by Jarvis.** The user
learns it from GitHub, on the merge page, where the "Update branch" button is already
sitting next to the refusal. That is the cheapest possible remedy and it is in front of
them at the exact moment it matters; a second telling in `jarvis status` would be a
notification per movement of `main` about something one click already fixes, and an
attention item per movement of `main` is how the attention strip stops being read.

That is a decision rather than an omission, and it is the one place this spec is content
to leave a user uninformed — so the fact that made it worth re-checking belongs here too.
§7 of the 2026-08-22 spec claimed BEHIND "does not block a merge in this fleet". **That
is wrong**: the `protect-main` ruleset sets `strict_required_status_checks_policy: true`,
so a behind branch genuinely cannot merge. The old spec reached the same conclusion from
a false premise; this one reaches it from GitHub already owning the report.

## 6. What the record shows

`timeline._describe` gains the matching three, in the words a red build wants rather
than a conflict's: `pr_checks_nudged` ("Failing checks — asked the worker to fix them",
with the names and the attempt), `pr_checks_cleared`, `pr_checks_unresolved`. The
message source `pr-checks` joins `timeline.UNAUTHORED_SOURCES`, so it renders as
"Jarvis → worker": a poll wrote it, and the record must not claim the user typed it.

The user is not notified of a heal, for the reason a merge notifies nobody. Only the
give-up in §3 reaches them.
