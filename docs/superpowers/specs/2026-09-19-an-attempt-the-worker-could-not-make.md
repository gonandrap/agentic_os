# An attempt the worker could not make is not an attempt

GitHub issue #469. Extends
`2026-08-22-a-work-order-heals-its-own-pull-request.md` (the conflict loop) and
`2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md` (the red-build loop);
everything they say still holds.

## 1. What happened

A work order sat in `waiting_pr_merge` with an `auto_merge` gate **pending** — Neo's
call had failed on a usage limit, so it escalated and the user had not looked yet. The
base branch moved, the pull request went CONFLICTING, and `Daemon.poll_pull_requests`
asked the worker to resolve it. Three times in four minutes.

Every one of those turns died in about twenty seconds, because a pending gate is
exactly the thing that stops a worker touching anything:
`hooks.pending_turn_block` refuses every non-read Bash call and every write in the
session while a request is under review. The worker never read the conflict. The
poller counted all three refusals as attempts, spent the budget, wrote
`pr_conflict_unresolved` — "Merge conflict the worker could not resolve" — and stopped.
Six API calls and about $3 for nothing.

The gate was approved six hours later. That unblocks the worker and re-triggers
nothing: the budget is derived from the timeline and the timeline says three attempts
were made. The order sat with its blocker acknowledged, no attention flag, and nothing
in the OS that would ever nudge it again, until a human noticed and typed `jarvis wo
send`.

The OS's own supervisor had diagnosed it correctly at the time and recorded that it
could not clear it. Detection existed; the poller acting on it did not.

## 2. The rule

**A repair attempt is spent only when the worker is permitted to attempt the repair.**

## 3. The guard — do not nudge into a closed gate

`Daemon.heal_pull_request` gains a fifth guard, beside the four it already has:
`store.pending_approvals(wo_id)`. Not `open_approvals` — the predicate is the SAME one
`hooks.pending_turn_block` enforces, because that is the code that actually refuses the
worker's commands. An `awaiting_case` request blocks the END of a turn, not the work in
it, so a worker holding one can still resolve a conflict and is not excused.

The repair is deferred, never dropped: the poll runs every tick, the work order stays
in a polled status, and the tick after the verdict lands nudges normally. That is the
whole of "approving the gate lets conflict resolution resume" for anything gated from
here on — nothing was spent, so there is nothing to restore.

`pr_<repair>_deferred` records it on the timeline, **once per episode per request**.
Once, because the poll would otherwise write a row every two minutes for as long as the
gate is under review, which is how a timeline stops being read. The user's attention is
on the gate, which is the item that is actually theirs to clear.

## 4. The re-arm — give back a budget that was never spent

The guard fixes the future. Two cases survive it:

* work orders already stranded when this shipped — the one in issue #469 among them;
* the race: the nudge goes out, the worker files a gate request mid-turn, the turn ends
  against its own gate. The guard cannot see a request that did not exist yet.

So `Daemon.heal_pull_request` also asks, before nudging, whether the episode is one the
OS gave up on **without the worker ever getting a turn it could use**:

1. the episode has given up (`pr_<repair>_unresolved`), and
2. nothing is pending now (else the guard above holds anyway), and
3. **every** nudge in the episode went out while a gate request was open on the work
   order.

Then `pr_<repair>_rearmed` is written, the episode budget resets to zero, and the
give-up flag comes down. The same call then nudges — attempt 1 of 3, for real this
time.

**All three attempts, not some of them.** A partial refund would need the episode
boundary to be a number rather than an event, and the issue's ask is the whole-budget
case ("a budget burned entirely on gate refusals"). An episode where the worker got one
genuine try keeps its give-up: it says something true.

**The blocking window is `[pending_at, decided_at)`** — the moment the request started
refusing the worker's commands to the moment it was answered. `approvals.pending_at` is
a new column for exactly this: `ts` is when the request was FILED, which for one filed
`awaiting_case` is earlier, and `status` cannot recover the transition once the request
is decided. NULL means the request never went pending and therefore never refused
anybody: a held request nobody argued, and one abandoned unargued.

**The predicate is the guard's, asked of a past moment**, and that is the single most
important property in this section. `hooks.pending_turn_block` refuses a session's
commands while a request is `pending` and at no other time; §3 defers on exactly that;
this asks whether it was true when the nudge went out. The first version of this change
used `[ts, decided_at)` and excluded only abandoned requests, so the two predicates
disagreed about a held request: §3 would nudge (correctly — `awaiting_case` only refuses
the END of a turn), the worker would fail for real reasons, and the refund would hand
the budget back anyway. Every episode. Attention cleared each time. That is a silent
unattended burn, which is the thing issue #469 is about — so the two predicates must be
the same predicate, not two readings of "a gate was open".

Historical rows get `pending_at = ts` where the record supports it (`_backfill_pending_at`),
which is exact for anything filed straight into `pending` — the default, and every gate
a worker trips.

**It terminates, twice over.** After a re-arm the guard in §3 means no nudge can go out
under a gate, so a second give-up cannot satisfy condition 3. And independently of that
argument: `rearm_pr_repair` refuses outright if a `pr_<repair>_rearmed` event already
exists on the work order, so one refund per work order per repair is the hard ceiling.
The belt is an argument about two predicates in two files agreeing, and they disagreed
once already; the braces cost one indexed read. If the cap ever bites wrongly the work
order asks the user to resolve a conflict by hand, which is where the OS started.

## 5. The wording

`pr_conflict_unresolved` still reads "Merge conflict the worker could not resolve — over
to you". It overstated what happened in issue #469; under §3 it does not, because a
give-up now requires three turns the worker was allowed to take. The sentence is fixed
by making it true rather than by hedging it.

## 6. Where the episode boundary lives

`ProjectStore._this_episode` counted events since the last `pr_<repair>_cleared`. It now
counts since the later of `pr_<repair>_cleared` and `pr_<repair>_rearmed` — one place,
so `pr_repair_attempts`, `pr_repair_gave_up` and `pr_repair_origin` all reset together
and cannot disagree about which episode is current.

The read is added AFTER the early return on "no nudges at all", so the green pull
request — the overwhelmingly common case, whose cost
`test_a_green_pull_request_costs_one_call_three_reads_and_no_write` counts — pays
nothing for it.
