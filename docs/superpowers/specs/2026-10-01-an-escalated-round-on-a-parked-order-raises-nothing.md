# An escalated round on a parked order raises nothing

GitHub issue 902. Work order wo-a85f38f8. Fix ratified by Neo question 1186 — option (a)
only; the alternatives in §8 are recorded as refused, not open.

## The problem

A work order whose panel GAVE UP, and which is then parked behind its pull request, asks
the user for nothing and never merges. `needs_attention = 0`, for ever.

Live case in the issue: **wo-3615faf7 / PR 818, 47 hours parked, `needs_attention = 0`.**

The two halves, each sound on its own:

1. `invariants.validation_escalated` (`src/jarvis/invariants.py:1334`) means "the panel
   gave up on this and a human is owed". `true_blockers` consults it at exactly one site,
   `src/jarvis/invariants.py:1008`, inside the branch guarded by
   `if governed and wo["status"] == "needs_review"` (`src/jarvis/invariants.py:975`). An
   order whose assumptions are then accepted goes through `ops.land_when_cleared`
   (`src/jarvis/ops.py:5134`), whose closing comment says an escalated round LANDS — the
   user saying ship it anyway. It lands in `waiting_pr_merge`, where no branch reads
   `validation_escalated`. The blocker is not re-derived, and `true_blockers` is the only
   source of attention reasons (INV-ATTENTION-REASON rewrites anything it cannot derive),
   so the give-up is silenced for good.

2. On the merge side nothing picks it up. `automerge.decide`
   (`src/jarvis/automerge.py:405`) returns
   `_held(HELD_NOT_PASSED, f"round {n} is {validation_standing(round_row)[0]}", round_n=n)`
   every tick, because `store.validated_head` is `None` for any round that is not `passed`
   (`src/jarvis/project_store.py:4925`) and an escalated round never is. The one
   self-healing path, `automerge.only_the_head_moved` (`src/jarvis/automerge.py:445`), is
   reachable only behind `decision.code == automerge.HELD_SHA_MOVED`
   (`src/jarvis/daemon.py:5753`), and its docstring forbids the other reading in terms:
   "`HELD_NOT_PASSED` is a verdict the panel meant to stand, and nothing here may read it
   as a near miss."

So: the OS holds the merge correctly, declines to re-judge correctly, and tells nobody.
The order is in the one status that means "the OS is finishing this" while the OS has
nothing left to try.

The ROOT CAUSE is the one being fixed: the escalation blocker is bound to a STATUS
(`needs_review`) rather than to the FACT (the panel gave up and nothing has superseded
it), so a legal status transition drops it. The fix re-derives the fact in the second
status it can survive into. It does not make the blocker status-free in general — see §8.

## The fix

One new ranked blocker in `true_blockers`, for `wo["status"] == "waiting_pr_merge"`, fired
when BOTH:

1. `validation_escalated(store, wo)` is true, AND
2. the newest `automerge_held` event's payload `code` is `automerge.HELD_NOT_PASSED`.

Nothing else changes. No new event, no column, no re-judge.

### 1. The constant

New module-level constant beside the family it belongs to, immediately after
`VALIDATION_STUCK_BLOCKER` (`src/jarvis/invariants.py:371`) — it is that sentence's parked
twin and its comment refers back to it.

```python
#: THE SAME GIVE-UP, ONE STATUS LATER. `VALIDATION_STUCK_BLOCKER` above is raised only in
#: `needs_review`; accepting the assumptions lands the order in `waiting_pr_merge`
#: (`ops.land_when_cleared`) and nothing re-derives it there, so the panel's give-up went
#: silent for 47h on wo-3615faf7 (issue 902). A SEPARATE SENTENCE because the remedy is
#: different: in `needs_review` the user decides the work, here the work is decided and
#: the PULL REQUEST is what will not move — `jarvis wo review` has nothing to review and
#: would send them looking for a prompt that is not there.
#:
#: FREE OF ANY ELAPSED TIME AND OF THE SHA, on AUTOMERGE_DENIED_BLOCKER's rule:
#: `ack_attention` stores this verbatim and INV-ATTENTION-REASON compares it, so a reason
#: that ticked could never be acknowledged.
PARKED_GIVE_UP_BLOCKER = ("the panel gave up on this and no commit was ever accepted, so "
                          "the automatic merge will never arm — merge it yourself, "
                          "re-judge it (`jarvis validation force`), or give it another "
                          "round (`validation.max_rounds`)")
```

Shape and length are `SHA_MOVED_BLOCKER`'s: one fact, then the three routes. The routes
are the ones that exist in `waiting_pr_merge` and no others.

- `jarvis validation force <id> --reason "…"` opens a fresh round against the CURRENT
  pull request and records the commit — the only mechanism that can produce a
  `validated_head` here, since the OS has declined to open one itself.
- `validation.max_rounds` is honest in this arm, unlike the rebind arm (§4.5 of
  2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round): an escalation IS the
  counted budget running out, so raising it is the thing that buys another round.
- `jarvis wo review` is NOT offered. It is the `needs_review` wording. The assumptions are
  already decided — deciding them is what moved the order here — and `wo review` refuses
  with nothing pending. Offering it is the silent-relabelling failure kn-b6977de3 names:
  a true-sounding line pointing at the wrong command.
- Merging by hand is named FIRST among the routes because it is what the user usually
  wants: the work was accepted, only the binding is missing.

### 2. The predicate

```python
def parked_on_a_give_up(store: ProjectStore, wo: dict[str, Any]) -> bool:
```

Placed with its three siblings, after `automerge_denied` (`src/jarvis/invariants.py:667`),
in the "the derivation everything else is checked against" section. Shaped on
`rejudge_exhausted`: **two facts, derived, never stored.**

- DERIVED, for kn-089de524's reason. A flag written where the hold is written sits on the
  daemon's poll path and re-raises itself every tick, overwriting `jarvis wo ack`.
  Re-deriving here means INV-ATTENTION-MISSING raises it and `true_blockers`'s closing
  filter (`src/jarvis/invariants.py:1047`) honours the acknowledgement.
- SELF-CLEARING, by construction and without waiting for a poll. Fact 1 reads the LATEST
  round only, so the moment a later round passes or opens, `validation_escalated` is False
  and the blocker is gone on the same tick — it does not have to wait for the next
  `automerge_held` event to be rewritten. Fact 2 clears it when the hold changes code: the
  head moved, CI went red, the pull request closed. Each of those has its own sentence or
  its own machinery, and this one must not sit over them.
- Fact 2 reads the NEWEST hold only, `store.events_of_kind(wo["id"], "automerge_held")[-1]`,
  the same read `_parked_on_a_decline` and `automerge_denied` make. No `head_sha` is
  compared: `decide` emits `HELD_NOT_PASSED` with `round_n` and no `head_sha`
  (`src/jarvis/automerge.py:405`), so there is no commit on that payload to compare
  against, and there is deliberately nothing to compare it to — the fact is "nothing was
  accepted", which is about the ROUND, not about a commit.

THE HOLD GATE IS PART OF THE PREDICATE, NOT OF THE BRANCH. Both facts are conditions on
the same question — "is the give-up what is holding this merge" — and a predicate that
answered only half of it would be a function nobody could reuse and a name that lied. It
also keeps the two store reads in one place for the invariant checks that call the
predicate directly, exactly as `rejudge_exhausted` folds its own hold check in.

### 3. Why the hold gate at all

Without it the blocker fires on every parked order in a project running with
`validation.auto_merge` OFF — which is every project by default.

`Daemon.auto_merge` returns on `project.validation.auto_merge` before it reads anything
(`src/jarvis/daemon.py:5287`), so such a project writes NO `automerge_held` events ever.
There the escalated round is not what blocks anything: the pull request merges on GitHub,
the daemon sees the merge and closes the order. Flagging those would be telling the user
to intervene in a flow that is working, on every parked order they have.

`HELD_NOT_PASSED` is the precise statement of "the give-up is the thing holding the
merge": it is the one hold code `decide` reaches when `validated_head` is empty and the
round is not `passed`. Gating on it scopes the blocker to that case and keeps the two
store reads off every other parked order — the same economy the neighbouring
`waiting_pr_merge` branches buy with their status gate (`src/jarvis/invariants.py:961`,
`src/jarvis/daemon.py:5290`).

### 4. Ordering, and why nothing co-occurs

Append the branch after the `automerge_denied` branch (`src/jarvis/invariants.py:973-975`),
last in the `waiting_pr_merge` family and above the `needs_review` triage:

```python
if wo["status"] == "waiting_pr_merge" and parked_on_a_give_up(store, wo):
    blockers.append(PARKED_GIVE_UP_BLOCKER)
```

The ordering is DOCUMENTARY, like the one above it: none of the four can co-occur, so the
rank never decides anything. The proof, from the code rather than asserted:

- vs `SHA_MOVED_BLOCKER` and `REBIND_EXHAUSTED_BLOCKER`: both go through
  `_parked_on_a_decline`, which returns False unless the newest hold's `code` is
  `HELD_SHA_MOVED` (`src/jarvis/invariants.py:651-652`). This one requires the newest
  hold's `code` to be `HELD_NOT_PASSED`. There is ONE newest hold and it carries ONE code.
  Exclusive.
- vs `AUTOMERGE_DENIED_BLOCKER`: it requires a non-empty `sha` with
  `store.validated_head(store.latest_validation_round(...)) == sha`
  (`src/jarvis/invariants.py:696-698`). `validated_head` returns `None` unless the latest
  round's outcome is `passed` (`src/jarvis/project_store.py:4925`), and this predicate
  requires that outcome to be `escalated`. Exclusive on fact 1 alone — fact 2 is not even
  needed for the proof.
- The same reading rules out a stale-hold race in the other direction: with the latest
  round escalated, `decide` cannot produce `HELD_SHA_MOVED` at all (it returns at
  `src/jarvis/automerge.py:388` before the sha comparison), so a `HELD_SHA_MOVED` newest
  hold beside an escalated latest round can only be a hold older than the round, and
  fact 2 excludes it.

Co-occurrence with the ASSUMPTIONS line is not a question here: a `waiting_pr_merge` order
has no pending assumptions by construction — `land_when_cleared` returns `needs_review`
while any are open (`src/jarvis/ops.py:5168`).

### 5. `governed` does not apply

The new branch is NOT gated on `governed`, matching all three `waiting_pr_merge` branches
above it and unlike the `needs_review` branch below.

`governed` exists for one reason (`src/jarvis/invariants.py:840-846`): two blockers hold a
session to a contract an INJECTED session never received — it cannot call
`jarvis wo finish`, so its ending is not a failure. Nothing in this blocker is about the
worker contract. It is a fact about a validation round and a merge hold, both of which
exist only because something DID deliver a pull request through the contract. An injected
session that reached `waiting_pr_merge` with an escalated round is in exactly the stall
this blocker describes, and suppressing it would be suppressing a true fact on the grounds
of who opened the session.

### 6. No `USER_REWORK_REFUSED_BLOCKER` split

The `needs_review` arm picks between two sentences on `user_rework_refused`
(`src/jarvis/invariants.py:1008-1011`). The parked arm uses ONE sentence and does not read
`user_rework_refused`.

The split exists upstream because the two give-ups ask the user for DIFFERENT things:
`USER_REWORK_REFUSED_BLOCKER` says "the rework you asked for was refused — accept it, send
it back, or close it", which is a decision about the work. In `waiting_pr_merge` the work
is already decided; what is stuck is the pull request, and the three routes out — merge by
hand, force a re-judge, raise the budget — are identical whatever bought the round. A
second sentence here would differ only in a cause the user can already read on the round
line and in `jarvis validation show`, at the cost of a second string that
INV-ATTENTION-REASON and `ack_attention` must both keep in step.

Adding the split later costs one `elif` and no change to the predicate; nothing here
forecloses it.

## Tests

`tests/test_invariants.py` for the table, beside the existing `rebind_exhausted` cases at
`tests/test_invariants.py:1267`; the end-to-end parked case in `tests/test_automerge.py`
beside the `AUTOMERGE_DENIED_BLOCKER` tests at `tests/test_automerge.py:1840`.

| # | Case | Expected |
|---|---|---|
| 1 | `waiting_pr_merge`, latest round `escalated`, newest hold `HELD_NOT_PASSED` | `parked_on_a_give_up` True; `PARKED_GIVE_UP_BLOCKER` in `true_blockers`; after `check_blocked_work_is_surfaced`, `needs_attention` and `attention_reason == PARKED_GIVE_UP_BLOCKER` |
| 2 | Same, newest hold `HELD_CHECKS_RUNNING` (any other code) | False, blocker absent |
| 3 | Same, no `automerge_held` event at all (`auto_merge` off) | False, blocker absent |
| 4 | Newest hold `HELD_NOT_PASSED`, latest round `pending` | False — `validation_escalated` is False |
| 5 | Newest hold `HELD_NOT_PASSED`, latest round `passed` | False |
| 6 | Case 1, then a later round settles `passed` | blocker gone on the next `true_blockers` call, WITHOUT a new hold event being written |
| 7 | Case 1, then `jarvis wo ack` | `true_blockers` no longer returns it; `check_attention_reason_is_true` and `check_blocked_work_is_surfaced` leave the flag down |
| 8 | Case 1 with status `needs_review` instead | the new blocker absent, `VALIDATION_STUCK_BLOCKER` present — the upstream arm is untouched |
| 9 | Mutual exclusion, `sha_moved`: a decline plus newest hold `HELD_SHA_MOVED` | `SHA_MOVED_BLOCKER` present, `PARKED_GIVE_UP_BLOCKER` absent |
| 10 | Mutual exclusion, denial: latest round `passed`, `automerge_decided` `denied` on the judged sha | `AUTOMERGE_DENIED_BLOCKER` present, `PARKED_GIVE_UP_BLOCKER` absent |
| 11 | The text itself | contains `jarvis validation force` and `validation.max_rounds`; contains NO `jarvis wo review`; no sha, no elapsed time (AUTOMERGE_DENIED_BLOCKER's test at `tests/test_automerge.py:1849` is the pattern) |

## Rejected alternatives

- **Re-judge it automatically** — let `only_the_head_moved`, or a sibling of it, treat
  `HELD_NOT_PASSED` as a near miss. Refused, and this is the one the reviewer will ask
  about. It contradicts that function's documented invariant in terms: an escalation is a
  verdict the panel MEANT to stand, not a stale binding. And on the live case it would not
  even fire: `ops.rejudge_moved_head` declines on `max_rounds` with the budget spent,
  writing a `REJUDGE_DECLINED_EVENT` that `invariants.rejudge_exhausted` then ignores —
  that predicate requires the newest hold to be `HELD_SHA_MOVED`. One silent stall traded
  for another.
- **Drop the status gate and derive the escalation blocker in every status.** It would
  catch this, and it would flag `validating` and `running` orders whose previous round
  escalated and whose next one is in flight — the OS working, reported as the user's
  problem. The give-up is only the user's business when nothing automatic is left.
- **Make `land_when_cleared` refuse to park an escalated order, keeping it in
  `needs_review`.** It inverts a deliberate decision (`src/jarvis/ops.py:5184-5189`): the
  user accepting IS the exit from a give-up, and a pull request that may yet merge belongs
  in `waiting_pr_merge`. It would also leave the auto_merge-off projects, where the merge
  does happen, parked in `needs_review` for nothing.
- **An inbox row instead of a blocker.** The inbox is acknowledged and gone; this is a
  standing condition that must stay on the attention list until someone moves the pull
  request, and must self-clear when they do.

## Out of scope

- The 47 hours already lost on wo-3615faf7. The fix raises the blocker on the next
  reconcile tick for any order currently in this state; no migration, no backfill.
- Any change to `automerge.decide`, to the hold codes, or to the re-judge machinery.
- The dashboard. It renders `attention_reason`, so it inherits the sentence.
