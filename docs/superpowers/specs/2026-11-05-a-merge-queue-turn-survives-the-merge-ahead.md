# A merge queue turn survives the merge ahead

Issue 1608, gap class `base-chase-livelock`. Rulings: Neo Q3672 on `wo-1f86f528`; kn-72d8d9f9.

## What is broken

Measured on wo-352c402c (PR 1604): round 2 passed, all 12 checks green, 4 `cause=behind` laps
and 4 `merge_queued` holds over 4.1h, 0 merges, then the livelock cap asked the user.
wo-abd62705 (PR 1491) paid 8 laps over 21.2h.

## Root cause

`automerge.merge_queued` held an order only while a lease existed on the exact base tip.
When the leader merged, the tip moved, the lease vanished and every waiter caught up and
re-ran CI. N queued orders paid N squared CI runs, and the slowest never reached the front.

## Fix

A head that does not contain main's tip still never merges. Each order pays one catch-up, at
the front, and none per sibling merge. This lives in `automerge.merge_queued`, because both
paths that spend a catch-up or a Neo question already pass through it.

- **Entry.** The earliest `merge_queued` hold since the latest round, read from
  `automerge_held` events. No schema change. Holds now carry `base` and `queued_behind`.
- **Order.** `(entered, wo_id)` per base branch. `merge_queued` keeps the same-tip lease and
  also holds an order while a live entry is ahead of it. An order with no entry is behind
  every entry, so new arrivals queue too.
- **Turn given up.** The entry is void when the order leaves `waiting_pr_merge`, its latest
  round is not `passed`, its latest request is denied or escalated, or it records a hold
  outside `TURN_KEEPING_HOLDS`. An exhausted, failed or unprovable catch-up counts as such a
  hold. `base_red` is one too, so a red-main fix never waits behind the queue.
- **Wedge.** An entry whose last hold is not `merge_queued` and is older than
  `gates.GRANT_TTL_SECONDS` is passed over. `merge_turn_passed_over` and one inbox row name
  the order, once per stale hold. A new hold puts it back in its place.
- **Bypass.** An expedited order, or one whose round has not passed, ignores position and
  obeys only the lease.

## Detector

`INV-BASE-CHASE-LIVELOCK`: a passed parked order with two `cause=behind` laps, each straight
after a `merge_queued` hold naming an order that has since merged.
