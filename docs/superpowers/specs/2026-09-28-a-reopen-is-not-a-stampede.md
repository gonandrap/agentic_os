# A reopen is not a stampede (issue #843)

## The incident

2026-09-28 21:00 PDT the Claude usage window reopened. Within ~60 s eleven work-order
turns resumed (wo-47b5e062, wo-5e0d3ec2, wo-8a68e024, wo-35fc3de7, wo-12d3a2e7,
wo-cc7b356f, wo-4ab73861, wo-b61f800c, wo-cdee6f9b, wo-3db50904, wo-604b5b99), several
spawning subagents. Every prompt cache had expired during the five-hour window, so each
resume re-wrote its whole conversation: 100-170k cache-write tokens per worker, 2.25M
cache-write plus 12.1M cache-read tokens in about two minutes. The fresh window was spent
within ten minutes, every turn parked again, and the same stampede waited at the next
reopen. The user stopped the fleet with `systemctl --user stop jarvis` and `kill`.

The caps were not the bug: `max_in_flight` was 15 and `max_concurrent` 10 at 21:00, so
eleven turns were within them. What was missing:

1. **Paused turns were never compacted.** `worker_session.compaction_due` excludes a
   paused turn, for two stated reasons: the relaunch nudge says the conversation is
   intact, and a compact turn behind a paused one erases the pause (`turn_pause` reads the
   latest turn). A usage-limit pause always outlives the cache TTL, so every relaunch
   after a window was a full cold re-write.
2. **Nothing distinguished a reopen from any other tick.** The steady-state cap is sized
   for steady state; a reopen is the one moment the whole backlog is due at once.
3. **Nothing noticed the loop.** A window spent again minutes after reopening is the
   signature, and it repeated silently.
4. **The user had no brake.** Only process-level stop and kill.

## The fix

### Compact before a cold relaunch (`worker_session.resume_compaction_due`, `compact_then_resume`)

Both refusals are answered rather than overridden. The relaunch is **queued as an
ordinary message before the compaction starts**, so no pause is erased: the compact turn
settles, and `deliver_messages` sends the queued relaunch into the summarised, warm
conversation on the next tick. The existing cost rule is kept: cache expired and the last
measured context over `os.compact_min_context`. A turn that reached the model is told its
conversation was compacted (`_nudge_after_compaction`) rather than that it is intact; a
turn refused before it ran is re-queued verbatim. A paused `/compact` is still re-sent as
is.

### The ramp (`fleet.ramp`, recorded by `fleet.announce`)

The tick on which an outage lifts records `usage_window_reopened_at`. For `RAMP_SECONDS`
(30 min) after it, at most `RAMP_CAP` (2) worker turns are in flight, whatever
`max_in_flight` says. It applies to every turn start: dispatch, paused-turn relaunch, and
message delivery.

### The breaker (`fleet._check_breaker`)

A new outage within `BREAKER_SECONDS` (30 min) of a reopen trips the breaker: one
critical inbox item with the timings, and the next ramp runs at `BREAKER_RAMP_CAP` (1)
for twice as long. The first outage that begins outside the breaker window clears it.

### The brake (`jarvis pause` / `jarvis resume`)

`jarvis pause [--allow ids] [--reason]` writes `fleet_paused` in the central store. While
it is set, only allow-listed orders may start a turn: `dispatch_pending` narrows the claim
(`claim_next_pending(only=…)`), and the relaunch, delivery and validation passes skip
everything else. Nothing is killed; turns in flight finish. `jarvis resume <ids>` adds to
the allow-list; `--all` lifts the pause. An unreadable pause record fails closed.
`jarvis status` shows the pause and any ramp in force.

### Deliveries are under the fleet

`deliver_messages` previously bound only `max_concurrent`. A delivery starts a turn like
any other, so it now honours the fleet's outage, pause, ramp and cap, and counts the
launch.

## Not in scope

OS-side model calls (Neo, the health sweep, digests) are not gated by the pause; they are
small next to worker turns and are tracked by the bounded-inputs and health-sweep work
(fo-ac00376e, issue #835). Making the ramp constants catalog settings is a follow-up.
