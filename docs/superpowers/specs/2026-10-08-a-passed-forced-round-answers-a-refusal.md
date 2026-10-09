# A passed forced round answers a refusal

Work order wo-195d8700, GitHub issue 979. Decision ruled by Neo question 1474 — this spec
implements it, it does not re-open it.

## The problem

Two predicates over the same refusal disagree, and the work order parks on a lie.

- `ops.user_rework_pending` (src/jarvis/ops.py:5254) is True when the newest refusing
  `reviewed` event is newer than `store.last_judged_round`. It therefore lets exactly one
  round through per refusal and TREATS THAT ROUND AS THE JUDGEMENT OF THE USER'S REWORK.
- `ops.refusal_answered` (ops.py:5234) is finish-only: True only when a `finished` event is
  newer than the newest refusing `reviewed` event. The round `user_rework_pending` just
  bought counts for nothing.
- `ops.land_when_cleared` (ops.py:5177) calls `refusal_answered` at line 5215 and, on
  False, sets `needs_review` without a flag of its own.

Live case on the record: user refused an assumption; worker pushed rework commits to the
pull request but never ran `jarvis wo finish`; user ran
`jarvis validation force <wo> --reason "…"`; the round opened uncounted with
`uncounted_cause = ops.USER_REWORK_CAUSE` (ops.py:7115) and PASSED, every seat green, no
assumptions pending. Status went `validating` -> `needs_review` and the reconciler flagged
"the worker stopped mid-task without jarvis wo finish". The panel had already judged the
work; the nudge is noise, and the only exit the user was offered — force a round — had
just been taken and ignored.

## The fix

Widen `ops.refusal_answered` so the ONE round `user_rework_pending` granted can answer the
refusal, guarded on head movement. Nothing else moves: it is the single predicate both the
join (ops.py:5215) and `invariants.undeclared_delivery` (invariants.py:1220) already call,
so one body change reaches both, and no caller learns a new rule.

### The predicate

`refusals` and the no-refusal early return are unchanged. Let `cut` be
`float(refusals[-1]["ts"])`. True when EITHER:

- **(a)** a `finished` event is newer than `cut` — today's rule, byte for byte; or
- **(b)** the newest round in `store.validation_rounds(wo_id=wo_id)` whose outcome is in
  `COUNTED_VALIDATION_OUTCOMES` has `outcome == "passed"`,
  `str(row.get("uncounted_cause") or "") == ops.USER_REWORK_CAUSE`, and
  `float(row["ts"] or 0) > cut` — and the head guard below does not block it.

The NEWEST settled round, not "some round passed": that is `last_judged_round`'s rule
(project_store.py:4906) and it is what `user_rework_pending` reads, so the two predicates
are about the same row by construction. Walk the rows once locally rather than calling
`last_judged_round` twice — the guard needs the prior counted row from the same list, and
two fetches across the validator thread could straddle a round it opened mid-read
(`validated_head`'s one-read rule).

### The head guard on (b)

(b) does NOT answer when the head the forced round judged is recorded AND equals the head
recorded by the last counted round settled at or before `cut`. An unmoved head means the
panel re-judged the work the user turned down; a pass there is the refused decision landing
by the back door.

Reads, and why:

- forced round: `ProjectStore.validated_head(newest)` (project_store.py:4919) — the one
  home for "the commit the panel accepted", and its `carried_head_sha` preference is
  exactly right here, because a base merge the OS carried is not the worker answering.
- prior round: `ProjectStore.validated_head(before) or str(before["head_sha"] or "")`.
  `validated_head` returns None for a round that is not `passed`, and the guard's question
  is "what commit did the panel last LOOK at", not "what was it licensed to merge". Using
  `validated_head` first keeps the carry rule in its one home; the raw column is the
  fallback for a non-`passed` prior row only.

Unrecorded SHAs fall back to the cause and so still answer: `""` is never a match, the same
rule `invariants.judged_heads` states (invariants.py:1178). Pre-0.10.0 rounds recorded no
commit (kn-48dadcce), and that parked population is precisely what `validation force`
exists for — blocking it would leave those orders with no exit at all.

### Why cause-based, not purely head-based

A head-based rule ("the forced round judged a commit newer than the one judged before the
refusal") reads as UNANSWERED whenever either round recorded no commit — which is every
pre-0.10.0 round, and any order polled before `pr_head_oid` shipped. That inverts the fix
for the population it was written for. The cause is recorded on every uncounted round, is
written by the OS and not by a worker, and already carries the meaning "this round is the
judgement of a user's rework". The head is the guard on top, not the test.

### What does NOT change

- A `rejected` user-rework round leaves the refusal UNANSWERED. (b) requires `passed`.
- `uncounted_cause == ops.REBIND_CAUSE` NEVER answers a refusal. The OS demanded that
  merge; the worker never answered the user.
- `ops.user_rework_pending` is untouched — it already grants one round per refusal and
  self-terminates.
- `land_when_cleared` is untouched. Its line 5215 branch keeps its wording and its
  no-flag-of-its-own behaviour.

### `invariants.undeclared_delivery`

Keeps CALLING the widened predicate. Its docstring claim "`ops.refusal_answered` … stays
finish-only: the finish is the declaration, and widening it would let the panel judge a
submission nobody declared" is now false and must be replaced with: a finish OR a passed
user-rework round answers the refusal, because the user forcing that round IS the
declaration, and once a panel has passed the pushed head, nudging the worker for a
`wo finish` is noise. The body needs no change — a now-answered refusal returns False at
line 1236 before `judged_heads` is read.

## Rejected alternatives

1. **Have `validation force` write a `finished` event.** Fakes a worker declaration in the
   timeline, breaks `review_assumption`'s round rule (kn-82d853ca), and would make
   `jarvis wo show` claim the worker delivered when it did not.
2. **Flag it better instead** — keep parking, fix the attention wording. Leaves the user
   with no exit: they already did the only thing available.
3. **Head-only predicate.** See above: unrecorded SHAs invert it.
4. **Teach `land_when_cleared` the exception at the call site.** Second home for the rule;
   `undeclared_delivery` would keep nudging.

## Known gap, deliberate

If the prior counted round `rejected` a commit, the user then refused, and the forced round
PASSES that same unmoved commit, the guard still fires (prior head read via the `head_sha`
fallback) — intended. If the prior round recorded no head at all, (b) answers even on an
unmoved commit. That is the unrecorded-SHA fallback working as ruled; the root cause there
is the pre-0.10.0 rounds, not this predicate.

## Tests to pin

In `tests/` beside the existing `refusal_answered` coverage:

1. refusal, no finish, newest counted round `passed` + `USER_REWORK_CAUSE` + head moved
   -> answered; `land_when_cleared` lands instead of `needs_review`.
2. same, outcome `rejected` -> NOT answered.
3. same, `uncounted_cause = ops.REBIND_CAUSE` -> NOT answered.
4. unmoved head: forced round's `validated_head` equals the pre-refusal round's ->
   NOT answered.
5. unrecorded SHAs (`head_sha = ""` on either row) -> answered (fallback).
6. forced round settled BEFORE the refusal -> NOT answered.
7. `invariants.undeclared_delivery` returns False once case 1 holds, True in case 2.
8. carry: forced round with `carried_head_sha` set equal to the pre-refusal head ->
   NOT answered (proves `validated_head` is the read, not `head_sha`).
