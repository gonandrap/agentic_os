# A moved head re-judges itself

Issue #493. Work order wo-f302361d.

## 1. The stall

`automerge.decide` condition 5 merges only a commit the panel judged. Anything that
moves the head after a passing round therefore invalidates the verdict, and the
commonest mover is the OS itself: the conflict poll nudges the worker, the worker pushes
a merge commit, `pr_conflict_cleared` is recorded, and the head is now a commit no round
has read.

Nothing re-opens a round for it. `ops.force_validation` has two callers and both are a
person (`jarvis validation force`, the dashboard button). The order sits in
`waiting_pr_merge`, which deliberately carries no attention flag, so the stall is also
silent. Measured on wo-2005a89b: green, mergeable, CLEAN, unmerged and unreported for
over thirty minutes.

The manual escape hatch was built for the 0.10.0 `sha_unrecorded` backfill and the
automatic trigger was not. This adds the trigger.

## 2. What the OS does instead

On any reconcile tick whose `automerge.decide` holds on `sha_moved`, the daemon opens a
fresh validation round against the current head itself — the same round
`jarvis validation force` opens, with a reason the OS wrote rather than a person's.

## 3. The guards

All must hold, and each removes a way of spending a round number badly.

1. **Not `record_only`.** The repair branches of `poll_pull_requests` call `auto_merge`
   only to keep the hold line honest; a pull request the OS is nudging a worker about is
   not one to judge.
2. **`automerge.only_the_head_moved`.** The hold must be the *only* thing left: the pull
   request is open, mergeable, green and CLEAN with the live head substituted for the
   judged one. Without this the OS would judge commits whose CI is still running and
   re-judge again when that CI comes back red.
3. **`ops.force_validation_refusal` returns None.** The person's rule, unchanged and
   shared: the panel is on, the order is in `FORCEABLE_STATUSES`, it carries a pull
   request, and the round machine owns nothing.
4. **No turn in flight and no queued message.** `worker_session.busy` and
   `store.queued_messages` — a branch somebody is still typing into is not settled.
5. **Once per head commit.** Deduped on the head sha across both outcomes below, the
   same key shape `Daemon._note_automerge_held` uses. A branch that keeps moving gets one
   round per commit, never one per tick.
6. **The last round stays for a person.** If the round it would open is the one that
   reaches `validation.max_rounds`, it opens nothing: a rejection there escalates with no
   worker left to fix it, and a budget spent by the machine is a budget the user never
   had.

`sha_unrecorded` is NOT a trigger (Neo, question 474): a finite pre-0.10.0 population
with no loop to close, and it stays `jarvis validation force`'s.

## 4. When it will not: the attention flag

Guard 6 is the one stall this cannot heal, so it is the one that has to be seen. The
decline is recorded as `validation_rejudge_declined` (once per head) and
`invariants.true_blockers` re-derives `SHA_MOVED_BLOCKER` from it while the newest hold
is still `sha_moved` on that same commit AND nothing has since bound a verdict to it —
an armed merge writes no hold, so without that last clause the flag would outlive the
stall. Derived and not written, on kn-089de524's rule: a flag raised at the write site
re-raises itself every tick and overwrites the user's `jarvis wo ack`.

**The decline suppresses the event, not the remedy.** Guard 5's key is split by
permanence: a commit the OS has judged is judged for ever, but one it declined was
declined against a budget the user can raise — so `validation.max_rounds` going up makes
the next tick re-judge that same commit. Otherwise the user's own fix would still leave
the order parked behind a command they had to remember to run, which is the manual step
this whole change removes.

## 5. The record

A forced round already carries `forced_reason`; an OS-forced one carries a machine
sentence naming both commits, and its `validation_forced` event carries `by: "os"` so
the timeline says "re-judged by the OS" and never "forced by hand". A verdict a person
did not ask for must not read afterwards as one they did.

## 6. Out of scope

A second route to the same symptom — `ops.land_finished` parking an order over a
REJECTED round (wo-7e08ac40, hold `HELD_NOT_PASSED`) — is a different cause and is
tracked separately. Nothing here fires on `HELD_NOT_PASSED`: auto-reopening a genuine
rejection would burn round numbers against a verdict the panel meant to stand.
