# A message must not drop an owed review

wo-09c60d79 · GitHub issue 1032 · jarvis 0.11.4
Precedent: kn-c9a58876 and `docs/superpowers/specs/2026-10-01-an-escalated-round-on-a-parked-order-raises-nothing.md` (issue 902).

## The problem

An escalated round is a FACT ("the panel gave up and nobody has decided"). Five call sites
read it, every one of them through one predicate, and that predicate answers a different
question than its name:

```python
# src/jarvis/invariants.py:1634-1642
def validation_escalated(store, wo) -> bool:
    latest = store.latest_validation_round(wo_id=wo["id"])
    return bool(latest and latest["outcome"] == "escalated")
```

```sql
-- src/jarvis/project_store.py:5242-5246
SELECT * FROM validation_rounds WHERE wo_id=? ORDER BY round DESC LIMIT 1
```

`latest_validation_round` is the LAST ROW BY NUMBER, not the panel's last WORD. Any row
that is not a verdict — `pending`, `failed` (transport), `void` — becomes the answer the
moment it exists, and the predicate reports `False`. `ProjectStore.last_judged_round`
(project_store.py:5092-5103) is the question the five readers are asking, and says so:
"`latest_validation_round` is the wrong question … the latest row can be a `failed`
transport round or a `void`, neither of which asked the submitter for anything."

None of those rows DECIDES a give-up. `escalated` is only answered by a later COUNTED
outcome, by `jarvis wo review`, or by the user merging. So the fact survives and the
predicate loses it.

### What that cost on the reported order

Observed, in the issue's words: round 4 escalated, `jarvis wo show` printed
`review owed: "round 4 gave up"` (cli.py:264-272, rendering `ops.review_state`), the user
ran `jarvis wo send`, the worker replied without `jarvis wo finish`, and the order then sat
in `waiting_pr_merge` with `needs_attention 0`, no review block, no review form, and
`auto_merge: held — round 4 is escalated` still printed.

The writer is **`Daemon.settle_work_order`, daemon.py:6541** — `store.set_status(wo["id"],
back)` with `back == "waiting_pr_merge"`, computed at daemon.py:6536:

```python
back = ("needs_review" if invariants_mod.validation_escalated(store, fresh)
        else ops_mod.pr_repair_origin(...) or ops_mod.resumed_from(...)
        or "waiting_pr_merge")
```

There is no fourth writer. `waiting_pr_merge` is written at exactly three places in
`src/jarvis` — daemon.py:6541, `ops.land_finished` (ops.py:4899) and
`ops.resettle_after_repair` (ops.py:7132) — and the other two are ruled out on this route:
`land_finished` is reached from settlement only on the `else` arm, where
`routes_on_pull_request` is False and the status it computes is therefore `completed`; and
`resettle_after_repair` refuses an escalated order (ops.py:7120) and a turn the user opened
(ops.py:7113). The remaining route into `land_finished` with a URL is
`ops.land_when_cleared` (ops.py:5263: "`escalated` lands"), whose callers all need an OPEN
or a PASSED round — `ops.finish`, `_land_after_acceptance`, `Daemon._void`, the
no-validator path, the deferred landing, `invariants._passed_round_never_landed` — and this
turn opened none: nothing on the message-delivery path touches a round
(`ops.send_message` ops.py:1215-1242, `Daemon._deliver` daemon.py:4048-4119, which only
sets `running` and clears attention).

So daemon.py:6536 read `validation_escalated` as `False` while the panel's last word was
round 4 `escalated`. The three state shapes that produce that are exactly the three rows
`last_judged_round` walks past, and the record itself shows the inconsistency: the hold
sentence `round 4 is escalated` is `automerge.HELD_NOT_PASSED` (automerge.py:405-408),
written only in `waiting_pr_merge` (`Daemon.auto_merge` returns at daemon.py:7313 on any
other status) and rendered fresh only while the payload round is not BELOW the latest row
(`ops._automerge_hold_is_stale` ops.py:4193-4195) — i.e. `decide` and the settler read the
same subject and disagreed. Which of the three rows it was is not derivable from the
record after the fact, because **no writer of `waiting_pr_merge` stamps a `trigger`**,
though `ProjectStore.set_status` takes one and writes it to `wo_state_spans`
(project_store.py:2621-2638). Fix C below closes that for good; the fix does not depend on
knowing, because it is derived from the fact rather than from the route.

### Why nothing put it right afterwards

1. **The status is terminal in practice.** `SETTLE_STATUSES = ("running", "idle",
   "waiting_input", "dispatching")` (daemon.py:281) — `waiting_pr_merge` is not swept, so
   the settler never re-derives `back`. One wrong write stands for ever.
2. **Issue 902's attention arm cannot see it.** `invariants.parked_on_a_give_up`
   (invariants.py:912-943) opens with `validation_escalated`, so the `waiting_pr_merge`
   branch at invariants.py:1254 is dead on the same reading. The 902 fix was correct and is
   disarmed by the predicate under it.
3. **The review control is status-gated.** `ops.review_state` ops.py:4074:
   `escalated = (wo["status"] == "needs_review" and validation_escalated(store, wo))`.
   No dict means no `review` key for `jarvis wo show` and no `placement`, which is what the
   dashboard work-order page renders the form on (`src/jarvis/ui/app.py` ~1612). It also
   reads the ROUND NUMBER from `latest_validation_round` (ops.py:4078), so the same row
   that flips the boolean would misname the round.

Root cause, stated once: **a fact about the panel is read through a query about row
numbers, and the status that carries the fact is written from that read and never
re-derived.** This is kn-c9a58876's general rule met from the other side — there the FACT
was gated on a STATUS at the call site; here the fact itself is mis-derived, so widening
call sites would have fixed nothing.

## The fix

Ending **(a)** of the issue: the escalated round STAYS owed — the order is in
`needs_review`, flagged, with the review form rendered — until the user decides it.

Ending (b) is rejected. "Supersede the escalation with a fresh round on the new head"
spends a round number and five seats on work nobody asked to be re-judged; `max_rounds` is
already exhausted, so `Daemon._validate_work_order` would escalate it straight back
(daemon.py:3064-3071) after paying for it; and a chat reply need not move the head at all,
so the OS would be opening a round over the commit the panel has already refused.

Widening `review_state` + the dashboard form to `waiting_pr_merge` is also rejected, on
kn-c9a58876's point 3: **`ops.review_work_order` would no-op there.** It acts only under
`if wo["status"] == "needs_review":` (ops.py:8154) — outside it nothing is reviewed and
nothing lands, while the call still writes a `reviewed` event and a Neo learning
(ops.py:8162, 8175-8181). A form naming a command that records a decision it did not take
is worse than no form.

### A. The predicate reads the panel's last word

`invariants.validation_escalated` (invariants.py:1634) keys on
`store.last_judged_round(wo_id=wo["id"])` instead of `latest_validation_round`. One line of
behaviour, and the docstring states the rule: an escalation is undone by a VERDICT, by
`jarvis wo review`, or by a merge — never by a row that judged nothing.

Why there and nowhere else: all five readers already go through it — daemon.py:6536
(the writer), invariants.py:937 (`parked_on_a_give_up` → the attention arm at
invariants.py:1254), invariants.py:1287 (the `needs_review` arm,
`VALIDATION_STUCK_BLOCKER` / `USER_REWORK_REFUSED_BLOCKER`), ops.py:4074 (`review_state`)
and ops.py:7120 (`resettle_after_repair`'s refusal). Fixing the predicate repairs the
status, the flag, the `wo show` block, the dashboard form and the repair refusal from one
edit. Putting the rule in any one call site would leave the other four as four more places
to forget.

`ops.review_state` ops.py:4078 takes the same row for its round number
(`store.last_judged_round(...)`), so `round 4 gave up` keeps naming round 4 when a
`pending` row numbered 5 exists beside it.

Deliberately NOT touched: `ops.land_when_cleared` (ops.py:5258) and
`Daemon.auto_merge` / `automerge.decide` keep reading `latest_validation_round`. Both ask a
genuine question about the LATEST ROW — "is a round in flight right now" and "which commit
did the newest round accept" — and `ProjectStore.validated_head`'s note
(project_store.py:5115-5119) is the standing argument for that.

### B. The repair for state already written

Fix A stops new ones. Orders already parked need a write, because of §1 above — nothing
re-derives the status. A third arm of INV-VALIDATION-STRANDED, beside
`_passed_round_never_landed` (invariants.py:3366), same shape:
`_give_up_parked_in_the_merge_queue(store)`.

Predicate, per `status='waiting_pr_merge'` work order, in this order:

1. `parked_on_a_give_up(store, wo)` — REUSED WHOLE, never re-derived. That is both facts of
   kn-c9a58876 at once: the panel's last word is `escalated`, AND the newest
   `automerge_held` is `HELD_NOT_PASSED`.
2. `not store.pending_assumptions(wo["id"])` — that decision is the `needs_review` arm's
   and outranks this one; it is also unreachable here by construction
   (`land_when_cleared` cannot park an order with one).
3. `worker_session.busy(store, wo["id"]) is None` and `not store.queued_messages(...)` —
   the discipline daemon.py:3045 and daemon.py:6353 already follow: no status is written
   under a live turn.

Repair: `store.set_status(wo["id"], "needs_review", trigger="give_up_reparked")` plus a
`give_up_reparked` event carrying the round id and `was`. **No `flag_attention` here** —
kn-089de524, and the 902 arm's own rule: `check_blocked_work_is_surfaced` raises it from
`true_blockers`, which is the only path that honours `jarvis wo ack`. Yield a
`Violation(repaired=True, repair="status set to needs_review; the give-up is owed again")`.
`readonly` (`jarvis doctor` with no `--repair`) describes it and writes nothing, as
`_passed_round_never_landed` does at invariants.py:3411-3421.

**Fact 2 (the hold) stays load-bearing, and it is what bounds the repair.** A project with
`validation.auto_merge` off writes no hold at all (`Daemon.auto_merge` returns at
daemon.py:7311), so `parked_on_a_give_up` is False and the repair does not fire. That is
correct and not a gap: there the user merges on GitHub, nothing is held by the escalation,
and `Daemon.poll_pull_requests` writes `pr_merged` ONLY while an order is parked in
`waiting_pr_merge` (daemon.py:7178-7179 states it) — so repairing the status would take a
healthy pull request off the unattended-completion path to render a form that
`review_work_order` would no-op on anyway. What such a project loses is the review form in
`waiting_pr_merge`; the deeper limit is `review_work_order`'s own `needs_review` gate, and
lifting it (accept → `land_when_cleared` from the merge queue) is **out of scope** here.

The repair's one real cost, stated: an order moved back to `needs_review` no longer
auto-completes when its pull request merges (same daemon.py:7178-7179). That is the price
of the ending the issue asks for, and the loop closes — `jarvis wo review --accept` runs
`_land_after_acceptance` → `land_when_cleared` → `escalated` lands (ops.py:5263) →
`waiting_pr_merge`, where the poll completes it.

### C. Every writer of `waiting_pr_merge` stamps its route

`ProjectStore.set_status` takes `trigger` and records it on `wo_state_spans`
(project_store.py:2621-2638); ops.py:5076 and ops.py:5139 already use it. Add one at each
of the three writers: daemon.py:6541 (`trigger="turn_settled"`), ops.py:4899
(`trigger="landed"`), ops.py:7132 (already event-logged; `trigger="pr_repair_resettled"`).
Pure instrumentation, no behaviour — and it is the evidence this spec could not get from
the live record: the next time an order appears in a status nobody can account for,
`jarvis wo show`'s spans name the branch that wrote it.

### Mutual exclusion and new false positives

The only behaviour change in A is for state shapes where a NON-COUNTED row sits after an
escalation. There are exactly three, and in each the give-up is still undecided by anyone:

| after the escalation | before | after | right? |
|---|---|---|---|
| `pending` round (a forced or rebind round in flight) | not escalated | escalated | yes — nobody has judged it yet |
| `failed` round (transport outage, no validator) | not escalated | escalated | yes — `counted_validation_rounds` already calls it no judgement |
| `void` round (nothing for a reviewer) | not escalated | escalated | yes — `Daemon._void` settles the UNIT, and where it lands the order the status is not `waiting_pr_merge` |
| `passed` / `rejected` round | not escalated | not escalated | unchanged — both are counted, so they become the last word |

No surface gains a false flag from the `pending` row, because an order with a round in
flight is in `validating` (ops.py:5581) and every arm is gated elsewhere: the
`needs_review` arm on `needs_review` (invariants.py:1256), `parked_on_a_give_up` on
`waiting_pr_merge` (invariants.py:1254), and `Daemon.settle_work_order` returns for
`validating` before reaching `back` (daemon.py:6337-6359). The one reachable co-occurrence
— `needs_review` with a pending round beside it, issue 212 — is already handled: the arm at
invariants.py:1288 is deliberately unguarded by `pending`, and `review_state`'s `placement`
resolves the two controls to one (ops.py:4088).

B cannot co-occur with the four `waiting_pr_merge` blockers. It fires only where
`parked_on_a_give_up` is True, which §4 of the 2026-10-01 spec already proves is exclusive
with `rejudge_exhausted`, `rebind_exhausted` and `automerge_denied` (one newest hold, one
code). And it is self-extinguishing: after the repair the status is `needs_review`, so
PARKED_GIVE_UP_BLOCKER's branch is unreachable and `VALIDATION_STUCK_BLOCKER` is the
carrier — one sentence, not two. The stale hold then renders as stale rather than
present-tense, via `_automerge_hold_is_stale`'s `"status"` answer (ops.py:4188-4190). B is
idempotent: a second pass finds no `waiting_pr_merge` row.

### Targeted tests

No full-suite run. One command:

```
uv run pytest tests/test_invariants.py tests/test_validation_loop.py \
  tests/test_validation_surfaces.py tests/test_automerge.py \
  tests/test_repair_resettle.py -q
```

**`tests/test_invariants.py`** — extend `_parked_on_an_escalation` (tests/test_invariants.py:1484)
with a parameter that opens one more round AFTER the escalation, outcome given; keep rows
1-9 passing unchanged.

1. `test_a_pending_round_after_a_give_up_does_not_drop_it` — `validation_escalated` True,
   `parked_on_a_give_up` True, `PARKED_GIVE_UP_BLOCKER in true_blockers`. Fails today.
2. `test_a_failed_transport_round_after_a_give_up_does_not_drop_it` — same, `outcome="failed"`.
3. `test_a_void_round_after_a_give_up_does_not_drop_it` — same, `outcome="void"`.
4. `test_a_later_round_that_passes_clears_it_without_a_new_hold` — EXISTING row 6
   (tests/test_invariants.py:1553) must still pass: a counted outcome is the last word.
5. `test_a_give_up_parked_in_the_merge_queue_is_put_back_for_review` — B: run the checker,
   assert `status == "needs_review"`, a `give_up_reparked` event, `needs_attention` still
   down, then `check_blocked_work_is_surfaced` raises `VALIDATION_STUCK_BLOCKER`.
6. `test_a_project_merging_by_hand_is_left_in_the_merge_queue` — `hold=None`: no move, no
   violation (the twin of existing row 3, tests/test_invariants.py:1525).
7. `test_the_repair_writes_nothing_under_a_live_turn` — a turn in flight: no move.
8. `test_the_repair_is_idempotent` — second pass yields no violation.

**`tests/test_validation_loop.py`** — beside `test_a_give_up_survives_the_turn_that_follows_it`
(tests/test_validation_loop.py:1834, which covers the plain route and passes):

9. `test_a_give_up_survives_a_message_turn_after_a_round_that_judged_nothing` — the issue's
   order end to end. Escalate, open a round and settle it `void`/`failed`, `queue_message`,
   hold the turn, release, `_settle`, tick: assert `status == "needs_review"` (and
   explicitly `!= "waiting_pr_merge"`), `VALIDATION_STUCK_BLOCKER in true_blockers`.

**`tests/test_validation_surfaces.py`** — beside
`test_review_state_owes_a_decision_on_an_escalated_round` (tests/test_validation_surfaces.py:463):

10. `test_the_review_control_names_the_round_that_gave_up_not_the_row_after_it` —
    `review_state(...)["escalated"]` True and `["round"] == 4` with a `pending` row
    numbered 5 present.

**`tests/test_automerge.py`** (around tests/test_automerge.py:1920-1950):

11. `test_the_parked_sentence_and_the_needs_review_sentence_never_co_occur` — after B,
    `PARKED_GIVE_UP_BLOCKER not in true_blockers` and `VALIDATION_STUCK_BLOCKER in` it.

**`tests/test_repair_resettle.py`**:

12. `test_a_repair_does_not_resettle_over_a_give_up_hidden_by_a_later_round` —
    `resettle_after_repair` returns False when the last judged round escalated and a
    `pending`/`failed` row sits after it (ops.py:7120 must not fail open).

### Not covered here

* Lifting `ops.review_work_order`'s `needs_review` gate so a review can land an order from
  `waiting_pr_merge` — the reason a hand-merging project keeps no form. Argued above; a
  separate order.
* `latest_validation_round`'s `ORDER BY round DESC` has no tie-break, and a round number is
  reusable (`numbered_validation_rounds` excludes `pending`/`failed`, project_store.py:4984-4986),
  so two rows can share a number and the row returned is scan order. Not reached by this
  fix and not provoked by the reported order; file it if a surface is ever seen naming the
  wrong round.
* Feature-order rounds (`fo_id`). `last_judged_round` takes both subjects, so A is
  subject-agnostic, but nothing in the feature loop was measured here.
