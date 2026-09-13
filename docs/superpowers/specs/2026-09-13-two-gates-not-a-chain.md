# Two gates, not a chain

*2026-09-13 — wo-b304c02a, GitHub issue 212.*

## The problem

A work order that files assumptions is not judged until the user has accepted them, and
when it finally is, the panel is shown no assumption at all.

`ops.finish` branched the two apart by construction: pending assumptions went to
`needs_review` and skipped validation entirely, `ops.review_work_order` was the catch-up
for the branch never taken, and `_validates_on_review` existed solely to plug that hole.
Observed on wo-4fc128ca: the worker finished at 1789237820, the round opened at
1789240489 — 44 minutes of dead wait, ended by a human waking up. The PR and the whole
diff existed the entire time.

The two slowest gates in the lifecycle were added rather than overlapped, on exactly the
work orders that are hardest — the ones that had to assume something.

## The rule

**An assumption review and a validation round are two independent judgements over one
artifact.** They open together and they are joined, never chained. The work order lands
when BOTH have cleared.

## The design

### 1. `finish` opens the round and parks for the user in the same call

Validation is opened whenever the panel is on, whether or not assumptions are pending.
The status that follows is decided by the join, not by the branch that ran.

### 2. The join — `ops.land_when_cleared`

The one place that decides where a finished work order sits, consulted by every route
that could end it: `finish`, `review_work_order`, and both settle paths in
`Daemon._validate_work_order`.

| the user's gate                     | latest round                  | status          |
|-------------------------------------|-------------------------------|-----------------|
| an assumption pending               | anything                      | `needs_review`  |
| a refusal the worker has not answered | anything                    | `needs_review`  |
| clear                               | `pending`/`failed`/`rejected` | `validating`    |
| clear                               | `passed`/`escalated`/none     | `land_finished` |

`escalated` LANDS. The panel gave up and put the work order in front of the user, and
the only route that can reach the join with one is `review_work_order` — the user saying
ship it anyway, which is the whole exit from a give-up.

**`needs_review` wins while assumptions are pending.** It is the only status that says
the user owes a decision; `validating` is deliberately silent and raises no attention
(`invariants.BLOCKED_STATUSES`). The round is still running underneath, and
`invariants.status_label` says so.

`panel_cleared=True` is the one override, for a caller that has just settled the panel's
half itself and must not re-read the round it wrote: the no-validator path closes its
round `failed` — never `passed`, because nobody judged the work — and that outcome
otherwise reads as "still in flight".

### 3. The round machine keys off the ROUND, not the status

`Daemon.validation_tick` queried `statuses=("validating",)`, which can no longer find a
round whose work order is parked in `needs_review`.
`ProjectStore.work_orders_awaiting_validation` replaces it: the latest round is
`pending` or `failed`, and the work order is open. Same query cost — one indexed lookup
that finds nothing on a quiet fleet.

INV-VALIDATION-STRANDED moves the same way, for the same reason: it watched
`status='validating'`, so a round abandoned under `needs_review` would sit for ever. It
is bounded to `OPEN_STATUSES` so that a cancelled work order's open round is never
handed back to the machine.

### 4. The packet carries the assumptions, and the judge may reject over one

`EvidencePacket.assumptions` is every assumption the work order ever filed, with its
review state. All of them, not just the pending ones: a decision the user accepted in
round 1 is still a decision embodied in the diff the panel is judging in round 2.

The store read is the caller's — `evidence.py` may not touch a database (see its module
docstring), so the two round-opening sites pass the rows in.

**Strength (a) of the three the issue drafted: the judge may reject the round citing a
bad assumption**, and a wrong call is a defect like any other. It gets no new power to
decide the assumption itself — that decision stays the user's. The per-assumption
sound/questionable recommendation (strength (b)) is a surface of its own and is filed
separately.

**Assumption texts are in the fingerprint**, mixed in only when there are any, so every
work order without one fingerprints exactly as before. Their review STATE is not: the
user accepting an assumption must not make an unchanged submission look new.

### 5. A rejection while the user is still deciding goes to the worker immediately

Decided by the user before this work order was written. Full parallelism: the worker
starts revising while the user is still deciding. Nothing in `Daemon._reject` needed to
change — it posts to the `implementor` role over the bus and `deliver_messages` filters
on the session, never on the status.

`Daemon._reject` and `Daemon._escalate` are unchanged, and both were checked against
the new concurrency rather than assumed safe:

- A REJECTION leaves the status alone, so the `needs_review` park survives the verdict.
  The feedback then travels to the worker and `Daemon._deliver` un-parks it — clearing
  the attention flag, which erases "assumptions pending review" outright. `true_blockers`
  re-derives it from the assumption row on the next reconcile, so the erasure is a blink
  rather than a loss. INV-ATTENTION-MISSING is what puts it back.
- An ESCALATION writes an attention flag of its OWN, and that one needed a change:
  `invariants.true_blockers` dropped `VALIDATION_STUCK_BLOCKER` whenever an assumption
  was pending, and it is now the one of that branch's three that survives a pending
  assumption. Before this, a panel that gave up while the user was still deciding was
  never mentioned to them — and never would be, because accepting the assumption lands
  the work order and the blocker is re-derived from `needs_review`.

The mirror of it is a hole parallelism opens, and `ops._refusal_answered` is what closes
it: the USER refuses an assumption, the worker starts revising, and the round already in
flight passes the code as it stood. "Nothing is pending" is true — a refused assumption
is not pending — and landing there would ship the very decision the user turned down. So
the user's gate is clear only when no assumption is pending AND the worker has delivered
again since the last refusal, dated on the `finished` event (the boundary kn-82d853ca
already uses for an assumption round). It is why `finish` records that event before it
settles anything.

### 6. Staleness

An accepted assumption stays accepted across a rewrite of the code it is about. An
assumption is a decision about an approach, not about a byte range, and any genuinely
new call the revision makes arrives as a new assumption row on the next `finish`.
