# An objection is withdrawn before the order stops

Work order `wo-66fc8296`, GitHub issue 1112, knowledge `kn-576351f8`.
Amends `2026-09-23-an-assumption-judged-while-the-worker-still-runs.md` §6.6 and §7 from
here; neither is edited in place (§9 below).

## The problem

`Daemon.withdraw_stale_objections` (src/jarvis/daemon.py:8441) tests NOT-RUNNING where
§6.6's own rule is TERMINAL:

```python
if store.get_work_order(wo_id)["status"] == "running":
    continue
for a in store.outstanding_objections(wo_id):
    self._withdraw_objection(store, wo_id, assumption=a)
```

`needs_review`, `validating`, `waiting_pr_merge` and `budget_exhausted` are all
non-running states a work order **comes back from**. §6.6's last paragraph says "never
withdraw on a status the order can come back from"; the code says the opposite, and the
belt-and-braces half (`Daemon.deliver_messages`, src/jarvis/daemon.py:2786-2793) repeats
the same predicate.

Nothing recovers the objection afterwards. `autoreview.decide_confirm`
(src/jarvis/autoreview.py:950) re-asks Neo only on `provisional_verdict == 'accept'`; an
`object` verdict falls through to the user (§7's three ways), so a withdrawn objection on
an order that keeps working is simply never said to anybody.

**Live evidence, `wo-a1d7b87c`:** objection filed 00:22, withdrawn 00:51 while the order
sat off `running`, further turns taken, round 2 delivered 02:45. Assumption #1 reached the
user as `1 assumption pending your review` with Neo's objection withdrawn, the worker
never told, and nothing on the review saying so.

**Root cause, named:** one predicate doing two jobs. `status != 'running'` is the correct
gate for DELIVERY — it is how `Daemon.deliver_messages` avoids starting a turn on a parked
order (§2's refusal) — and the wrong gate for WITHDRAWAL. §6.6 wrote one test for both.

Second-order defect, in scope because the repair creates it: `ops.objection_line`
(src/jarvis/ops.py:4863) hard-codes the string "the order stopped before it could be
delivered", so the moment a second withdrawal reason exists that renderer lies about it.

## The fix

Ruled by Neo for the user, **question 2008**, and this is the binding text:

> Not Option A. Section 7 stands: no second Neo call, no `decided_by=neo` on an objection.
> Ship (1) re-delivery, (2) A's disarm-any-queued-carrier under a new withdrawal reason,
> and (3) the missing piece — surface the objected still-pending assumption, with its
> objection reason and the delivered result summary, in the `needs_review` USER review
> payload. That kills the symptom at the boundary a `needs_review` order actually has,
> without Neo re-deciding what the user reserved. No new column, no new question kind.
> Section 7 is NOT amended in place: keep its text, append a one-line note pointing at
> this ruling, and put the delivery/re-surface mechanism in a new spec section.

**Question 2009**, the counting half:

> Option A, on `jarvis neo stats` beside the escalation rate. Derived read over the three
> existing ts columns plus the re-ask — no table, no new write path.

There is no re-ask under 2008, so the counts are objections sent / delivered / withdrawn,
withdrawn split by WHICH reason (§8).

No second Neo call appears anywhere below. No new column. No new Neo question kind.

## 1. Change 1 — withdrawal narrows to TERMINAL, non-terminal HOLDS

Three outcomes where there were two. `project_store.TERMINAL_STATUSES`
(src/jarvis/project_store.py:70) is the one list, in both places:

| order status | outcome |
|---|---|
| `running` | deliver, exactly as today |
| in `TERMINAL_STATUSES` (`completed`, `cancelled`, `failed`) | withdraw under `daemon.OBJECTION_WITHDRAWN_REASON`, unchanged |
| `needs_review` | disarm under the NEW reason (§2) |
| any other open status | **HOLD** — stays `queued`, nothing written, delivered at the next turn boundary |

`budget_exhausted` is deliberately not terminal (src/jarvis/project_store.py:55-65): the
user raises the budget, the same session carries on, and the objection goes out with it.
`validating` and `waiting_pr_merge` are the states the defect was measured on and are
holds, not withdrawals — an order comes back from both (panel rejection to `running`;
`CONFLICTING` asks the worker, a PR closed unmerged goes to `needs_review`). A PR that
merges makes the order `completed`, which is terminal, so the hold always ends.

**The two edits.** `withdraw_stale_objections` swaps its `== "running"` test for
`status not in TERMINAL_STATUSES and status != "needs_review" → continue`.
`deliver_messages`' pre-hold branch (src/jarvis/daemon.py:2786-2793) keeps its
not-running test for the HOLD and withdraws only on those two status sets. The pass keeps
its name: both outcomes still call `store.withdraw_objection` and still stamp
`objection_withdrawn_ts`; only the reason differs.

### 1.1 Where the hold is expressed, and why there

`worker_session.delivery_hold` (src/jarvis/worker_session.py:277) has **no work-order
status gate**, and `daemon.delivers_message` (src/jarvis/daemon.py:324) is that call plus
"are there queued messages". So the pre-hold branch in `deliver_messages` is the only
thing standing between a queued objection and a parked `needs_review` order with a
session and no turn in flight — delete or naively narrow it and the OS relaunches a parked
order, which is the exact act §2 refused.

A hold is also not free to be silent. `needs_review`, `waiting_pr_merge` and `failed` are
in `invariants.MESSAGE_STUCK_STATUSES` (src/jarvis/invariants.py:588), and
`invariants.stuck_message` (src/jarvis/invariants.py:1481) reports
`"the delivery pass has not attempted it"` whenever `delivery_hold` returns `None`. An
objection held for hours would become INV-MESSAGE-STUCK unless the hold is expressed where
that function reads it.

**Mechanism: one new per-message derivation, in `worker_session`.**

```python
def message_hold(store, wo, msg, now=None) -> DeliveryHold | None
```

* new code `worker_session.HOLD_OBJECTION_PARKED`, beside the five at
  src/jarvis/worker_session.py:249-253;
* armed when the message's envelope is `kind == 'assumption_objection'`
  (`store.envelope_for_message` — the read the pre-hold branch already does) AND
  `wo["status"] != "running"` AND the status is not terminal and not `needs_review`;
* `accounted=True`, `reason="the OS objected and the worker is not running — it goes out
  with the order's next turn"`. Accounted because nothing is wrong and nobody is waiting on
  the user: the order runs again and it is delivered, or it reaches `needs_review`/terminal
  and it is disarmed. §2 is what bounds it — an accounted hold with no terminator is a
  silent queue, which is why the disarm is necessary rather than optional;
* the tail is `return delivery_hold(store, wo, now=now)`, so every existing per-order hold
  still applies through one call and none is re-derived.

**Why `worker_session` and not `delivery_hold` itself.** `delivery_hold` is per work
order and sees no message; arming this branch there would hold the USER's own message to a
`needs_review` order too, and that revive is the documented behaviour of `jarvis wo send`.
It cannot go in `daemon` either: `invariants` must read the same function and `daemon`
imports `invariants`. `worker_session` is the module both callers already share for this
decision.

**Three readers, one derivation** (`stuck_message`'s own rule, "THE HOLD IS NOT RE-DERIVED
HERE"):

1. `Daemon.deliver_messages` calls `message_hold(store, wo, msg)` per message in the loop
   it already has, replacing its `delivers_message(store, wo, queued=[msg])` call;
2. `daemon.delivers_message` becomes
   `any(message_hold(store, wo, m, now=now) is None for m in msgs)` — "will a turn go
   out", which stays true for a user message queued behind a held objection and false when
   the objection is all there is. Without this, `ops.waiting_on` answers `queued_message`
   for an order no turn is coming for;
3. `invariants.stuck_message` swaps its one `delivery_hold(store, wo, now=now)` call for
   `message_hold(store, wo, oldest, now=now)`. It already holds `oldest`, and
   `accounted=True` makes the answer `None`.

A turn launched by the user's own message coalesces the whole queue, so an objection held
on a parked order is NOT dragged along: it is skipped per message, the order returns to
`running`, and the next boundary delivers it. One rule — an objection reaches a running
conversation or it reaches nobody.

## 2. Change 2 — the `needs_review` disarm

**`needs_review` only.** The boundary is "the order has DELIVERED and the user owes the
decision", and that is the single status where `ops.review_state` is non-`None` for a
pending assumption on a settled order.

Why not also `validating` / `waiting_pr_merge`: those are the states the order comes back
from, which is the whole point of change 1 — disarming there re-creates issue 1112 with a
longer status list. Why not hold at `needs_review` as well:

* the audience has changed. The user is about to rule on the assumption (§4). A machine
  objection arriving afterwards would be a second, unattributed instruction about a
  question the user has already answered — and on `wo review --reject` their own feedback
  is the guidance the worker gets;
* it is the only thing that can clear `decide_confirm`'s gate. `objections_outstanding`
  (src/jarvis/autoreview.py:1045) is per ORDER, not per assumption, so one objection left
  outstanding on assumption #1 holds the confirmation of accepted siblings #2-#5 for ever.
  Under 2008 there is no second Neo call to resolve it and `HELD_OBJECTED` hands the
  objected row to the user regardless. Today's withdrawal is what releases that gate; the
  disarm inherits the job.

**The reason is NEW and the old one must not be reused.**
`daemon.OBJECTION_WITHDRAWN_REASON` ("the order stopped before it could be delivered")
stays for the terminal case and is true only there. Add
`daemon.OBJECTION_HANDED_BACK_REASON = "the order delivered its result and the decision is
yours"`. `Daemon._withdraw_objection` (src/jarvis/daemon.py:8476) gains
`reason: str = OBJECTION_WITHDRAWN_REASON` and passes it to both places it already writes
the text — `store.mark_envelope(..., note=reason)` and the
`autoreview_objection_withdrawn` payload. Its disarm-before-stamp order is unchanged.

**Where the pass lives: `Daemon.withdraw_stale_objections`, same call site, unchanged
position** — after `auto_review` in the reconcile block (`Daemon.run`, kn-cc29cd1b; the
`tick` helper at tests/test_assumption_objection.py:376 encodes the same order). So the
accepted siblings confirm on the NEXT reconcile tick, exactly as today.

Not before `auto_review`: §7 already specifies that a non-empty outstanding list means
"not yet" and is retried on the next tick, and every shipped case relies on that. Moving
the disarm earlier would couple the confirmation pass to it same-tick for orders that have
nothing to do with this defect, to save one reconcile interval on a pass whose output then
waits for a human.

One pass, one candidate query (`work_orders_with_objections` in front of
`outstanding_objections`), two reasons.

## 3. Change 3 — the reason, read without a new column

`store.withdraw_objection` stamps `objection_withdrawn_ts` and nothing else. The reason
exists in two places already: the envelope note and the `autoreview_objection_withdrawn`
event payload.

**The envelope note is NOT the carrier.** `_withdraw_objection` writes the note only when
`envelope["state"] == "queued"`; in the case that matters the envelope has already
resolved to a `wo_messages` row (`delivered_msg_id` set, message still `queued`), so no
note is written at all. A renderer reading the note would render nothing for exactly the
rows this spec is about.

**The carrier is the event**, which is always written: `autoreview_objection_withdrawn`,
payload key `reason`.

Per kn-640e7f6c, an objection fact that is not on the assumption row is derived ONCE, at
the row-enrichment point, and attached to the row for the pure renderer to read:

* `ops.assumptions_with_rulings` (src/jarvis/ops.py:4592) already walks every kind in
  `AUTOREVIEW_EVENTS`, which includes `autoreview_objection_withdrawn`. While walking,
  capture the LAST such event per assumption id into a side map and attach
  `objection_withdrawn_reason` to the row — **independently of the `os_ruling` ranking**,
  because a later `autoreview_asked` or `autoreview_escalated` can outrank the withdrawal
  and `os_ruling` would then not be it. Asked only of rows with `objection_withdrawn_ts`
  set, the same discipline `objection_response` uses. **Zero extra queries**: the events
  are already in hand.
* `ops.objection_line` (src/jarvis/ops.py:4863) drops its hard-coded sentence and reads
  `a.get("objection_withdrawn_reason")`, falling back to `OBJECTION_WITHDRAWN_REASON` when
  empty. The fallback is a fact and not a guess: every row written before this ships was
  withdrawn for that reason.

Both surfaces take their rows from `assumptions_with_rulings` (`jarvis wo show` at
src/jarvis/cli.py:3739, the work-order page via `ui/app.py`), so the derivation reaches
both and the renderer stays pure. Three walks would be three chances to disagree.

## 4. Change 4 — the re-surface, in the user review payload

**What is already there, and must not be said twice:**

* `ops.provisional_line` (src/jarvis/ops.py:4838) already prints
  `objected to by the OS (Neo, <model>) at <ts> — <reason>` on an `object` verdict, on
  the pending branch of `ops.assumption_line` (src/jarvis/ops.py:4891) and in the page's
  assumptions block (src/jarvis/ui/templates/work_order.html:384-386);
* `ops.objection_line` already prints the end state, and after §3 prints the right reason;
* `work_orders.result_summary` is already on both surfaces — `work_order.html:203-204`,
  and the `wo show` payload carries it through `**wo` (src/jarvis/cli.py). **It is not
  copied anywhere by this spec.**

**What is missing: the link between them at the boundary.** A `needs_review` order says
`1 assumption pending your review` and nothing tells the user the OS objected to that
assumption, that the worker was never told, or that the result below is the thing the early
reading never saw.

**Mechanism: `ops.review_state` (src/jarvis/ops.py:4201) gains the objected rows.** It is
already the one derivation both surfaces render, already keyed on
`store.pending_assumptions`, already carries its sentences in Python because a phrase in
Jinja cannot reach a terminal, and already sets `placement = "assumptions"` for this case.

New keys, present only when the list is non-empty (the dict's own never-always rule):

```
"objected": [{"n": …, "reason": …, "delivery": "withdrawn" | "handed_back" | "delivered"}],
"objected_note": "the OS objected to assumption #1 and the worker was never told — "
                 "decide it against the result this order delivered",
```

`reason` and `delivery` come off the row (`provisional_reason`, and the three ts columns)
— structured, so a consumer reading only `review` is complete. **The rendered sentence
does not repeat the reason**: the assumption block two lines above prints it on both
surfaces. `delivery` distinguishes the three outcomes §1 now produces, which is the fact
the user cannot otherwise get.

Rendered by `src/jarvis/ui/templates/_review.html` (the shared macro `work_order.html:5`
imports, so both placements get it) and by the human branch of `jarvis wo show` where the
`review` key is assembled (src/jarvis/cli.py:3779). No new renderer, no new control, no
change to what the buttons do — one decision still settles the whole order.

## 5. Change 5 — the counts on `jarvis neo stats`

A derived read, no table and no new write path (question 2009).

* `ProjectStore.objection_counts(since: float | None = None) -> dict` — one scan of
  `assumptions` for `sent` / `delivered` / `withdrawn` over `objection_sent_ts`,
  `objection_delivered_ts`, `objection_withdrawn_ts`, plus one scan of events of kind
  `autoreview_objection_withdrawn` to split `withdrawn` by reason. Buckets keyed by exact
  match on the two `daemon` constants, anything else `other` (which is how a third reason
  added later shows up instead of being absorbed).
* read in `ops.neo_stats_report` inside the per-project loop that already opens a
  `ProjectStore` (src/jarvis/ops.py:16938), returned as `"objections": {...}` beside
  `"questions"`.
* rendered as ONE line beside the escalation rate: `src/jarvis/cli.py:5194`, and the
  `/neo/stats` page (`ui/app.py:1877` and its template).
* the report's zero rule applies unchanged: counts are a census and print `0`.

## 6. Rejected alternatives

* **Option A as filed — re-ask Neo at the boundary.** Refused by question 2008: no second
  Neo call, no `decided_by=neo` on an objection. §7 reserved the objected row for the user
  and a confirm call would take it back.
* **A new `objection_withdrawn_reason` column.** Refused by 2008 ("no new column") and
  unnecessary: the event payload already carries it and §3's enrichment point already
  reads those events.
* **A new Neo question kind for the hand-back.** Refused by 2008, and seven edits for
  nothing (kn-4edb0eb7).
* **Keep the NOT-RUNNING test and simply re-file a fresh objection when the order runs
  again.** Two records of one objection, a second envelope, and §6.1's "record, then send"
  invariant now has two records disagreeing about what was said.
* **Disarm at `validating` and `waiting_pr_merge` too.** Issue 1112 with a longer status
  list: both are states the order comes back from, and `wo-a1d7b87c` was in one of them.
* **Narrow the predicate and leave the hold implicit.** Measured consequence: an objection
  queued on a parked order becomes INV-MESSAGE-STUCK after `messaging.stuck_minutes` with
  the reason "the delivery pass has not attempted it" — a false defect report about the
  daemon (§1.1).
* **Add the status gate to `delivery_hold` itself.** It would hold the user's own message
  to a `needs_review` order, breaking the `jarvis wo send` revive.
* **Run the disarm before `auto_review`.** §2: changes confirmation timing fleet-wide to
  save one reconcile interval before a human decision.

## 7. What this does NOT fix

* **A short order still loses its objection**, and that is §6.4's honest outcome, not a
  defect: an order that reaches `completed` before any turn boundary withdraws under the
  old reason and the user sees it on the assumption. Change 1 buys the objection every
  boundary the order actually has; it does not invent one.
* **`decide_confirm`'s gate stays per order.** Making it per assumption is a bigger change
  than this defect justifies and §2's disarm makes the gate terminate; the sibling delay
  is one reconcile tick.
* **No attention is raised for a held objection.** §9's `undeliverable` rule is untouched:
  nothing failed.

## 8. The spec-document edits

**A new file, this one, and not a new section in the 2026-09-23 spec.** Recommendation and
reason: that document is the feature's own 600-line narrative and its §6.6 contains the
superseded predicate; a new section inside it would force every future reader to reconcile
two sections of one document. The repo's convention for a ruling that reverses an earlier
one is a dated file that amends from outside — `2026-09-25-a-decided-assumption-is-not-
left-with-you.md`, `2026-09-26-an-unreachable-neo-question-is-not-a-question-in-flight.md`,
`2026-09-28-a-dropped-confirmation-must-not-hold-an-assumption-for-ever.md` all amend the
same feature that way.

Edits to `docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-
runs.md`, and they are exactly two lines:

* **§7 keeps its text**, and gains one line at its end:
  `> Amended 2026-10-20 (Neo, question 2008): the withdrawal this section is ordered after now runs on TERMINAL statuses only, and a needs_review order disarms a queued objection under its own reason and re-surfaces it to the user — 2026-10-20-an-objection-is-withdrawn-before-the-order-stops.md.`
* **§6.6 keeps its text**, and gains the same shape of line. This one is not required by
  2008 and is required by the code: §6.6 is where "no longer `running`" is written down,
  and a reader who implements that sentence re-introduces issue 1112.

Nothing in either section is rewritten or deleted.

## 9. Tests

New, in `tests/test_assumption_objection.py` unless named otherwise.

**The pair that matters**, both driven at the tick's own granularity with the existing
`tick(daemon, project, store, poll=, reconcile=, drain=)` helper (line 376) and `ops.finish`
— never `store.set_status` (kn-cc29cd1b, kn-13e71e04):

1. `test_an_order_that_leaves_running_for_needs_review_keeps_its_objection` — objection
   filed, order delivers to `needs_review` through `ops.finish`, one tick: the objection is
   still outstanding, `objection_withdrawn_ts` is NULL, no message was delivered and no
   turn was launched. Then the disarm tick: withdrawn under
   `OBJECTION_HANDED_BACK_REASON`, carrier disarmed, status still `needs_review`.
2. `test_an_order_that_goes_terminal_with_an_objection_queued_still_gets_the_old_reason` —
   the negative control for the reason, asserting the exact string "the order stopped
   before it could be delivered".

Then:

3. `test_an_objection_rides_the_next_turn_after_a_rejected_round` — the measured case:
   `validating`, panel rejects, order runs again, the objection is DELIVERED and
   `objection_delivered_ts` is stamped. The regression test for `wo-a1d7b87c`.
4. `test_a_held_objection_is_never_a_stuck_message` — past `messaging.stuck_minutes` on a
   `needs_review` order, `invariants.stuck_message` returns `None` and no INV-MESSAGE-STUCK
   is raised.
5. `test_a_user_message_is_still_delivered_past_a_held_objection` — `delivers_message` is
   true, the turn goes out, and the objection is NOT in it.
6. `test_a_budget_exhausted_order_keeps_its_objection` — non-terminal, held, and delivered
   once the budget is raised.
7. `test_the_disarm_is_what_clears_the_confirm_gate_for_the_siblings` — one objected row
   and one accepted row; the accepted sibling confirms on the tick after the disarm and not
   before.
8. `test_objection_counts_split_withdrawals_by_reason` — one of each reason, asserting the
   two buckets and `other` empty (`ops.neo_stats_report`).

In `tests/test_assumption_objection_view.py`:

9. `test_a_handed_back_objection_says_the_decision_is_yours` — `objection_line` renders the
   new reason off the row, and the historical row with no event renders the old one.
10. `test_the_review_payload_names_the_objected_assumption` — `review_state["objected"]`,
    the note, and that the reason is printed exactly once on the page and once in
    `jarvis wo show`.

**Existing tests that assert the NOT-RUNNING predicate and must be updated:**

* `tests/test_assumption_objection.py::test_an_order_that_stops_with_an_objection_queued_ends_withdrawn`
  — sets `needs_review` via `store.set_status` and asserts the OLD reason. Split: the
  terminal half keeps the assertion with `cancelled`/`completed`; the `needs_review` half
  becomes test 1 above.
* `…::test_delivery_refuses_an_objection_to_an_order_that_stopped` — same, via
  `deliver_messages`. Must assert the HOLD for a non-terminal non-`needs_review` status
  (message still `queued`, nothing written) and the withdrawal for the other two.
* `…::test_an_order_still_running_keeps_its_objection_however_long_it_waited` — still
  true; extend its parametrisation to the held statuses so "age is not the trigger" covers
  them.
* `…::test_the_withdrawal_is_what_lets_the_confirmation_pass_run` — the cross-section
  joint. Nothing withdraws at `needs_review` any more, so it must drive the order to
  `needs_review` with `ops.finish` and assert that the DISARM (the same
  `withdraw_stale_objections` pass, in the same tick position) is what releases
  `HELD_OBJECTION_IN_FLIGHT`, with the sibling confirming one tick later as it does today
  — same tick count, new reason.
* `…::test_the_drain_never_files_against_an_order_that_is_not_running` — unchanged and
  must stay unchanged: §2's licence to FILE an objection still evaporates off `running`.
  Named here because it is the test a reader will reach for by mistake.
* `tests/test_assumption_objection_view.py::test_the_four_end_states_each_read_differently`
  — becomes five, and `test_a_row_written_before_the_columns_renders_exactly_as_before`
  plus `test_an_assumption_in_an_auto_review_off_project_grows_no_lines` are the historical
  guards on §3's fallback and §4's new keys.
