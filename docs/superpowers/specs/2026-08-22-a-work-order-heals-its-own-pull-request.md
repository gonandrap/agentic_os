# A work order heals its own pull request

*wo-0b190c7b, 2026-08-22. Design decisions ruled by Neo on question 152.*

## 1. The problem

A work order that ends behind a pull request parks in `waiting_pr_merge` and the daemon
polls GitHub until the merge happens (`Daemon.poll_pull_requests`). That poll asks one
question — did this land? — and so it is blind to the one answer that stops the landing:
the branch conflicts with its base.

What the user does today is find the order, see the conflict on GitHub, and type
`jarvis wo send <id> "go and resolve the conflicts"`. Every parked order that sits long
enough eventually needs it, because main moves under it. The user's own account of why
that is worth removing:

> I have limited time in front of a computer, so if I review and all is green, I want to
> merge to move forward, I don't want to waste time asking to resolve conflicts.

The nudge is a message with no decision in it. Nobody weighs anything, nobody chooses
between options: GitHub says CONFLICTING, and the only possible next move is the worker
resolving them. That is the definition of something the OS should do itself.

## 2. What the poll reads now

`github.pr_view` asks `gh pr view --json state,mergedAt,mergeable,baseRefName` — the
same single call it always made, with two more fields on it. `mergeable` is GitHub's own
three-way answer:

| value | meaning | what the OS does |
|---|---|---|
| `CONFLICTING` | the branch cannot be merged into its base | §3 — heal it |
| `MERGEABLE` | it can | clear any conflict state (§4) |
| `UNKNOWN` | GitHub has not computed it yet | nothing |

`UNKNOWN` is not an error and must not be treated as one. GitHub computes mergeability
lazily and asking is what triggers the computation, so the first poll after a push
routinely returns it and the next one, two minutes later, has the real answer. The OS
acts on `CONFLICTING` alone; every other value including a missing field is a no-op.

`baseRefName` is only there so the message in §3 can name the branch to merge.

## 3. The heal loop

A parked work order whose pull request is CONFLICTING gets a message queued for its
worker, and from there the existing machinery does all of it:

1. `Daemon.poll_pull_requests` sees CONFLICTING and calls `ops.nudge_pr_conflict`.
2. That queues one message (§6) and records `pr_conflict_nudged`.
3. `Daemon.deliver_messages` delivers it on the next tick, which resumes the finished
   worker session **in its own worktree** and flips the work order to `running`.
4. The worker merges the base in, resolves, tests, pushes, and ends its turn.
5. `Daemon.settle_work_order` sees a done turn on a work order that already has a
   `result_summary` and a `pr_url`, and parks it back in `waiting_pr_merge`.
6. The next poll asks GitHub again.

Step 5 is why the worker must NOT call `jarvis wo finish` a second time, and the message
says so: the work order finished long ago and a second `finish` would open a fresh
validation round over a conflict resolution. Ending the turn is the whole protocol.

Step 3 is also what makes double-nudging structurally impossible: the poll only looks at
work orders in `waiting_pr_merge`, and a nudged one is `running` until its turn settles.
The guard on a queued message and a live turn in `Daemon.heal_pr_conflict` covers only
the seconds between queueing and delivery. `running` also means the work order holds one
of its project's concurrency slots again for the length of the repair, which is correct:
it is working.

The third guard is the session. `deliver_messages` skips a work order that has none, so
a nudge queued for one would sit undelivered for ever AND block every later nudge behind
it — the budget spent without a single attempt. Everything that reaches
`waiting_pr_merge` through `jarvis wo finish` has a session; a row hand-parked from a
terminal does not, and is left exactly as it was before this existed.

Nothing here runs git. The daemon does not merge, resolve or push — resolving a conflict
is a judgement about two versions of the code, and the worker is the process that holds
the context to make it. The daemon's job is to notice and to ask.

## 4. Attempts, and giving up

Three attempts, then the work order asks the user. `invariants.PR_CONFLICT_MAX_ATTEMPTS`.

The count is DERIVED FROM THE RECORD, not stored in a column:
`ProjectStore.pr_conflict_attempts` counts `pr_conflict_nudged` events written after the
most recent `pr_conflict_cleared`. There is no counter to get out of step with the
timeline, and the timeline is what the user reads when the flag fires.

`pr_conflict_cleared` is written the moment a poll sees the pull request MERGEABLE again
after any nudge. It resets the count, so a branch that conflicts again next week gets
three fresh attempts — the budget is per episode, not per work order lifetime.

Giving up flags attention with `PR_CONFLICT_BLOCKER`, and that is the first thing that
has ever made a `waiting_pr_merge` work order an attention item. Deliberate: the status
is silent because a merge queue is not a decision anyone owes, and a conflict the worker
could not resolve three times running IS one. The usual case is the trap kn-0a5c449c
describes — a stacked pull request whose base branch has itself been merged, which no
amount of merging fixes and which only the user can decide what to do about.

Silent infinite retry was the alternative and it is worse: it hides the one case where
the user is actually needed, behind a poll that looks like it is still working.

## 5. Why the blocker has to be re-derivable

`invariants.true_blockers` is the single source of attention reasons, and
INV-ATTENTION-REASON rewrites any flag it cannot derive. So the give-up state is not a
flag the poll raises and hopes survives; `true_blockers` re-derives it from
`pr_conflict_attempts` on every reconcile tick, exactly as PR_CLOSED_BLOCKER is
re-derived from `pr_state`.

That obligation has a second half: `waiting_pr_merge` joins `BLOCKED_STATUSES`, whose
comment already states the rule — a status absent from that tuple has its blockers
derived correctly and then never surfaced, because INV-ATTENTION-MISSING is what puts
the flag back. A parked work order with no conflict still returns no blockers, so the
status stays silent in the ordinary case.

## 6. What the record shows

Neo's addition to the ruling: when the flag fires, the user must be able to see what was
already tried without asking. Three signal-level timeline entries, all of them in
`timeline._describe`:

- `pr_conflict_nudged` — "Merge conflict — asked the worker to resolve it", attempt n of 3
- `pr_conflict_cleared` — "Merge conflict resolved"
- `pr_conflict_unresolved` — "Merge conflict the worker could not resolve — over to you"

The message itself is attributed to Jarvis, not to the user. Every other
`user_to_agent` message on a work order was written by a person or by their delegate
answering for them; this one was written by a poll, and rendering it as "You → worker"
would put words the user never typed in their mouth. Same rule that makes
`complete_merged` record `pr_merged` rather than `marked_done`: the record must not
claim the user did something they did not do.

## 7. What this deliberately does not do

**Failing CI checks are not healed.** `UNSTABLE` — a red check on a mergeable branch —
is left alone. Auto-fixing a failing test is a far larger autonomy step than the one
asked for, and it is the step where an OS that is wrong writes bad code into a pull
request the user is about to merge on trust.

**A branch merely BEHIND its base is not updated.** It does not block a merge in this
fleet, and updating it would burn a full worker turn — a whole conversation re-sent at
the cache-write rate — every time main moves.

**There is no per-project switch.** It only ever pushes to the work order's own pull
request branch, which is what workers already do unprompted, and a switch defaulting off
would recreate exactly the manual step this exists to remove.

**The user is never notified of a heal.** A merge notifies nobody (see the comment in
`poll_pull_requests`) for the same reason: an inbox entry per automatic repair is how an
inbox stops being read. Only the give-up in §4 reaches the user.
