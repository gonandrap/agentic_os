# A conflict resolution the OS asked for costs no round

GitHub issue #824, wo-cc7b356f. Follow-up to #806 / PR #808, spec
`docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md` (the catch-up
carry), which named this case and left it open. Neo question 896 decided the mechanism;
§3 below is that ruling written down, not re-argued.

## 1. The problem

**When the OS's own conflict poll makes a worker resolve a merge with `main`, the head it
produces is re-judged as an ordinary round — and on a parked order that round is the one
the round machine refuses to spend, so the order strands on the user.**

The chain, with the code:

1. The catch-up carry proves two things (`Daemon._carry_catch_up`, src/jarvis/daemon.py:5880):
   proof (a), the head is the judged commit plus merges that added nothing unjudged
   (`ci.base_merge_chain`, src/jarvis/ci.py:275); proof (b), the pull request's own diff
   against its merge base is unchanged (`branchproof.diff_fingerprint`, called at
   daemon.py:5926-5927). A conflict RESOLUTION edits content, so proof (b) correctly fails
   (daemon.py:5932, `ops.PROOF_PATCH_ID`, ops.py:5490) and the verdict is not carried.
   That refusal is right and is not the defect.
2. Control falls through to `Daemon._rejudge_moved_head` (daemon.py:4990, method at
   daemon.py:6062) and `ops.rejudge_moved_head` (src/jarvis/ops.py:4370). That numbers the
   re-judge `counted_validation_rounds + 1` (ops.py:4409) and, at
   `nxt >= int(cfg.max_rounds)` (ops.py:4410), DECLINES: it writes
   `invariants.REJUDGE_DECLINED_EVENT` (ops.py:4413, constant at
   src/jarvis/invariants.py:286) and returns.
3. `invariants.rejudge_exhausted` (invariants.py:514) then derives
   `invariants.SHA_MOVED_BLOCKER` (invariants.py:299, raised at invariants.py:796) — an
   attention item telling the user to merge it themselves, force a round, or raise
   `validation.max_rounds`.

**Live evidence: wo-8736a5c5 / PR #794, 2026-09-27 ~21:55.** Rounds 2-4 passed. The OS's
own poll saw `CONFLICTING` and asked the worker to resolve the merge with `main`. The
worker resolved an additive conflict in `ci.py` and one semantic conflict in a test; head
`04ff2429f1`. The carry refused on proof (b) — correctly, that resolution is authored
content no seat read. The re-judge then declined with "round 5 of 3 would be the last",
and the order sat on the user's attention list until the operator ran
`jarvis validation force`.

**Root cause.** `validation.max_rounds` bounds ONE thing — the worker's rework loop against
the panel's rejections. `ops.rejudge_moved_head`'s own docstring states that reading
(ops.py:4386-4394: "`max_rounds` is where a rejection stops going back to a worker and
starts going to the user"). But the counter it reads is the only counter the round machine
has, so a judgement the OS itself caused — by demanding a merge whose resolution it then
refuses to carry — is charged to the worker's rework budget. The budget is being spent on
work the budget is not about. The predecessor spec closed the half where nothing needed
judging; this is the half where something does and nobody should be billed a rework round
for it.

**Second, smaller defect, same cause.** The daemon routes a rejection on the round NUMBER:
`elif outcome == "rejected" and n < max_rounds` (daemon.py:2054, with
`n, max_rounds = int(round_row["round"]), int(cfg.max_rounds)` at daemon.py:1878). Number
and budget position are the same integer today only because every round counts. The moment
one does not, that line sends a rejection to the user purely because the row is numbered 5.

## 2. Rejected alternatives

* **Raise `max_rounds`, or let the user do it.** That is today's advice
  (`SHA_MOVED_BLOCKER`, invariants.py:299) and it is the bug: it widens the worker's rework
  loop fleet-wide to pay for one merge the OS demanded, and it needs a human on every
  occurrence.
* **Exempt every OS-forced round from the budget** (`REJUDGE_BY_OS`, ops.py:4339). Too
  wide. An OS re-judge also fires on a head a worker PUSHED — authored content arriving
  outside the round loop — and that is exactly what the budget should bound.
* **Carry the verdict anyway when the merge's diff differs "only a little".** There is no
  such predicate. The predecessor spec §3.3 already settled that a conflict resolution is
  authored content and deserves a real panel; anything softer merges unread code.
* **Let `automerge` merge it on the old verdict and file the divergence as a follow-up.**
  Breaks the one invariant this whole area exists to keep: nothing merges a commit no round
  bound (`ProjectStore.validated_head`, src/jarvis/project_store.py:4436).
* **Count the rebind but refund it on a pass.** A ledger that can go backwards; every "why
  is this at round 4 of 3" reading gets a second, mutable answer.
* **Keep `counted_validation_rounds` as the numbering formula and give a rebind a
  fractional or negative number.** The unique index is `(wo_id, round)` on an INTEGER
  column (project_store.py:822, 860); the idempotent insert (project_store.py:4337-4344)
  depends on that key meaning one thing.

## 3. What a rebind round is (decided, Neo question 896)

A **rebind round** is a real panel round the OS opens when:

* the head moved off the judged commit (`automerge.HELD_SHA_MOVED`), and
* proof (a) HOLDS — every commit between judged and head is a merge that added nothing
  unjudged (`ci.base_merge_chain` returns a non-empty chain), and
* proof (b) FAILS — the two `diff_fingerprint` values differ, i.e. the merge's conflict
  resolution edited the pull request's own content.

It judges the changed content for real. It is never a carry. It **never counts against
`validation.max_rounds`**, passed or rejected. A small `REBIND_MAX` bounds it. A rejected
rebind goes to the WORKER while rebind budget remains, whatever the counted budget says;
when the budget is spent it goes to the user. One rebind per head commit, by the existing
dedupe (`ops.rejudged_heads`, ops.py:4342).

## 4. The fix

### 4.1 Numbering: a settled-rounds count, separate from the budget

`ops.submit_for_validation` numbers a round `counted_validation_rounds + 1` (ops.py:4131),
and that is deliberate: `pending` and `failed` rounds are EXCLUDED so a retried submission
re-derives its own number, hits the idempotent insert (project_store.py:4337) and reuses
its row. An uncounted rebind breaks it in the other direction — the next ordinary
submission would re-derive the rebind's number and be handed the rebind's settled row,
parking the work order in `validating` for ever. `ops.force_validation`'s docstring already
names this trap verbatim (ops.py:4281-4285).

Split the two jobs the counter is doing.

**New column** `validation_rounds.uncounted INTEGER NOT NULL DEFAULT 0`, added in the
`CREATE TABLE` (beside `forced_reason`, project_store.py:841) AND in `ADDED_COLUMNS`
(project_store.py:1277 block) — this table already ships, so a live database gets it only
there. `0` means "an ordinary round", the honest reading of every row written before the
column existed. NOT NULL so there is one spelling of "counts".

**New store method** `ProjectStore.numbered_validation_rounds(*, wo_id=None, fo_id=None)
-> int` — "how many round NUMBERS this subject has already spent". Placed beside
`counted_validation_rounds` (project_store.py:4347). Its predicate:

```
outcome IN COUNTED_VALIDATION_OUTCOMES
  OR (uncounted=1 AND outcome NOT IN RUNNABLE_VALIDATION_OUTCOMES)
```

`COUNTED_VALIDATION_OUTCOMES` is project_store.py:105, `RUNNABLE_VALIDATION_OUTCOMES`
project_store.py:108. The second clause is what "SETTLED but uncounted" means. `pending`
and `failed` stay excluded on both sides, so retry-idempotency is unchanged for every
existing row — on a database with no rebind in it this method returns exactly what
`counted_validation_rounds` returns, which is the migration argument.

**`counted_validation_rounds` gains `AND uncounted=0`** (project_store.py:4360-4364). It
keeps its name and its meaning: rounds the SUBMITTER spent.

Call sites that MUST switch to `numbered_validation_rounds` — both are numbering:

* `ops.submit_for_validation`, ops.py:4131.
* `ops.submit_feature_for_validation`, ops.py:4614 (`fo_id=`). A feature round can never be
  uncounted today, so the value is identical by construction; it switches anyway, because
  "how a round is numbered" must be one expression and not two that agree by accident.

Call site that MUST NOT switch:

* `ops.rejudge_moved_head`, ops.py:4409. That `nxt` is the BUDGET POSITION and the
  `>= cfg.max_rounds` test at ops.py:4410 is a budget test. It is also reported as
  `next_round` in the decline event and rendered as "round {next_round} of {max_rounds}
  would be the last" (src/jarvis/timeline.py:574). After this change a budget position and
  a row number can differ; the sentence is about the budget and stays budget-based. Rename
  the local to `budget_nxt` so a later reader cannot mistake it for a row number.

No test, invariant or renderer that asserts on `counted_validation_rounds` changes meaning:
every existing assertion is about rounds a submitter spent.

### 4.2 Where the rebind fact comes from

`Daemon._carry_catch_up` (daemon.py:5880) today refuses on proof (b) at daemon.py:5932 and
RETURNS — so `ci.base_merge_chain` (called at daemon.py:5938) is never reached in exactly
the case that matters. Two constraints on the reordering: the read budget (a parked pull
request is polled every ~2 minutes and the walk is up to `ci.CHAIN_LIMIT` = 10 `gh api`
calls, ci.py:272), and the division of labour the method's own docstring states — `gh`/`git`
in the daemon, the rule in `ops`, unit-testable with no network.

**Do not move the chain walk before the fingerprints.** That would pay up to 10 API calls
per tick on every pull request whose diff genuinely changed, for ever. Instead, on the
proof-(b) mismatch, ask a cheap store-only question first and walk only if the answer is
yes:

**New in `ops`, pure store reads, no network, no `gh`:**

```
ops.REBIND_MAX = 2
ops.rebind_possible(store, wo, *, head, project, cfg) -> bool
```

True only when every one of these holds — the same guards `rejudge_moved_head` applies at
ops.py:4402-4407, asked early so the walk is not paid for a rebind that could not be
opened:

1. `head` is non-empty and not in `rejudged_heads(store, wo_id)` (ops.py:4342) — one rebind
   per head commit;
2. `head` not in `rejudged_heads(store, wo_id, declined=True)`;
3. `not worker_session.busy(store, wo_id)` and `not store.queued_messages(wo_id)`;
4. `force_validation_refusal(store, wo, project=project, cfg=cfg) is None` (ops.py:4204);
5. `store.uncounted_validation_rounds(wo_id=wo_id) < REBIND_MAX` — second new store
   method, `SELECT COUNT(*) ... WHERE uncounted=1`, beside the other two.

`_carry_catch_up`'s **return type changes from `bool` to a small frozen dataclass**
`daemon.CarryOutcome(carried: bool, rebind: bool)` — `carried` is what the caller already
used, `rebind` is "proof (a) held and proof (b) did not, and a rebind may be opened". A
bare bool cannot express three states, and the daemon already has this shape: the method's
own `refuse()` helper (daemon.py:5914) exists because "no reader can recover the proof from
a bare None".

Restructured body of `_carry_catch_up`, changes only:

* the proof-(b) mismatch branch (daemon.py:5932-5936) no longer returns. It becomes:
  record the refusal exactly as now — `ops.record_carry_refusal(..., proof=ops.PROOF_PATCH_ID, ...)`,
  ops.py:5830, whose `(head, proof)` dedupe via `carry_refusal_told` (ops.py:5813) is
  **unchanged and must stay so**: the refusal is still true and still said once — then, if
  and only if `ops.rebind_possible(...)`, run the same `ci.base_merge_chain` call that
  already sits at daemon.py:5938 inside its existing `try`, and return
  `CarryOutcome(carried=False, rebind=bool(chain))`. A chain of `()`, a `GitHubError`, or
  any other exception returns `CarryOutcome(False, False)` — the pull request stays exactly
  where it was, `carry_validated_head`'s failure direction.
* every other `return False` becomes `return CarryOutcome(False, False)`; the success path
  returns `CarryOutcome(True, False)`.

No second `gh` read is added on the happy path, and none at all on a tick where a rebind
could not be opened.

**Carrying it into the re-judge.** In `Daemon.auto_merge` (daemon.py:4962-4991):

```
outcome = CarryOutcome(False, False)
if not record_only and decision.code == automerge.HELD_SHA_MOVED:
    outcome = self._carry_catch_up(project, store, wo, pr, decision)
    if outcome.carried:
        ... re-read the round and re-decide, exactly as daemon.py:4971-4975 ...
```

and at daemon.py:4986-4990 pass it through:
`self._rejudge_moved_head(project, store, wo, decision, rebind=outcome.rebind)`, which
forwards `rebind=` to `ops.rejudge_moved_head`.

**`automerge.only_the_head_moved` (daemon.py:4987) still guards the rebind.** A rebind is
free of the round budget but not of money — five seats — and a resolution that broke CI
should hold on `checks_not_green` rather than buy a panel. Keeping the guard also means no
new arm in the poll.

### 4.3 The round-cap branch

In `ops.rejudge_moved_head` (ops.py:4370), new keyword `rebind: bool = False`. The guards
at ops.py:4402-4407 are untouched — they apply to both kinds, and `rebind_possible` asked
the same questions on a stale read one step earlier, so this is the authoritative pass.

Replace the budget branch (ops.py:4409-4419) with:

```
budget_nxt = store.counted_validation_rounds(wo_id=wo_id) + 1
if rebind:
    used = store.uncounted_validation_rounds(wo_id=wo_id)
    if used >= REBIND_MAX:
        if head in rejudged_heads(store, wo_id, declined=True):
            return None                 # said once per commit, not once per tick
        store.add_event(wo_id, invariants.REJUDGE_DECLINED_EVENT,
                        {"head_sha": head, "judged_sha": judged, "round": round_n,
                         "cause": REBIND_EXHAUSTED, "rebinds": used,
                         "rebind_max": REBIND_MAX})
        return {"wo_id": wo_id, "declined": True, "cause": REBIND_EXHAUSTED,
                "head_sha": head, "judged_sha": judged, "rebinds": used,
                "rebind_max": REBIND_MAX}
elif budget_nxt >= int(cfg.max_rounds):
    ... today's decline, unchanged, with cause=REJUDGE_BUDGET_SPENT added ...
```

`ops.REBIND_EXHAUSTED = "rebind_exhausted"` and `ops.REJUDGE_BUDGET_SPENT = "budget"`, the
`PROOF_*` discipline (ops.py:5489-5492): one value per condition so a reader can tell the
two declines apart. `cause` is written on BOTH so a payload without one is unambiguously a
pre-change row.

**`cfg.max_rounds` is not consulted at all on the rebind arm.** Structural, like
`carry_merge_chain`'s refusal to take a `cfg` parameter (ops.py:5743 docstring): the live
order's recovery depends on no round accounting reaching this path.

The submission itself is the existing call at ops.py:4422-4425 with two additions:
`uncounted=rebind` (a new keyword on `submit_for_validation`, forwarded to
`ProjectStore.open_validation_round`, project_store.py:4297) and a different
`forced_reason`:

```
REBIND_FORCED_REASON = (
    "the OS re-judged this itself, outside the round budget: round {n} passed on "
    "{judged}, and the head is now {head} — the merge the OS asked for resolved a "
    "conflict, so the content changed and a seat has to read it. This round does not "
    "count against validation.max_rounds (rebind {used} of {max})")
```

That string is the whole of the `jarvis validation show` / timeline answer: `ops.round_line`
(ops.py:2042, rendered at ops.py:2072) prints `forced_reason` verbatim on every surface, and
`timeline._describe`'s `validation_forced` branch (timeline.py:517-535) prints
`payload["reason"]` verbatim under the label "Validation forced by the OS — round N".

The `validation_forced` event (ops.py:4425-4429) gains `"rebind": True` beside the existing
`by: REJUDGE_BY_OS`, so `rejudged_heads`' per-head dedupe (ops.py:4362-4365) keeps working
unchanged AND a reader can count rebinds from events as well as from rows.

**One more renderer, or the record lies.** A rebind numbered 5 under `max_rounds = 3` reads
as the very bug this spec is about. `ops.round_line` (ops.py:2068) must mark it:
`round {n} · uncounted` when `rnd.get("uncounted")`. That needs `"uncounted"` added to the
key tuple in `ops.validation_rounds` (ops.py:2839-2841); the deliberation projection at
ops.py:3603 spreads the whole row, so it carries it already.

### 4.4 Rejection routing

`Daemon._validate_work_order` computes `n, max_rounds = int(round_row["round"]),
int(cfg.max_rounds)` at daemon.py:1878 and routes at daemon.py:2054
(`elif outcome == "rejected" and n < max_rounds`). A rebind at a row number past the cap
takes the `elif outcome == "rejected"` arm at daemon.py:2063 and escalates to the user —
wrong.

Replace the condition at daemon.py:2054 with a predicate that asks the right question:

```
uncounted = bool(round_row["uncounted"])
rebinds = store.uncounted_validation_rounds(wo_id=wo_id) if uncounted else 0
goes_back = (rebinds < ops.REBIND_MAX) if uncounted else (n < max_rounds)
...
elif outcome == "rejected" and goes_back:
```

`rebinds` counts the round being settled, so `rebinds < REBIND_MAX` means "there is another
rebind left after this one" — the loop is: rebind rejects, the worker fixes, the head moves
again, and that head's re-judge is ANOTHER rebind (§4.2 re-derives `rebind` from the new
head's own chain, it is not inherited). At `rebinds == REBIND_MAX` the `elif` at
daemon.py:2063 runs unchanged and `_escalate` (daemon.py:2211) asks the user.

`Daemon._reject` (daemon.py:2155) takes `n` and `max_rounds` only to format
`REVIEW_FEEDBACK` (daemon.py:230, "REVIEW FEEDBACK (round {n} of {max})") and the
`validation_rejected` event's `of` field (daemon.py:2173). "round 5 of 3" is wrong for a
rebind. Add a keyword `rebind: tuple[int, int] | None = None` — `(used, REBIND_MAX)` — and
when it is set, post `REBIND_FEEDBACK` instead:

```
REBIND_FEEDBACK = """REVIEW FEEDBACK (re-judgement {used} of {max}, no round spent)
{reason}

This is not one of your {max_rounds} review rounds. The OS asked you to merge the base
branch in, your resolution changed what this pull request contributes, and a seat has read
it. Fix what is above and run `jarvis wo finish {wo_id} --summary "..." --evidence "..."`
again."""
```

Shaped on `ops.BOUNCE_FEEDBACK` (ops.py:4001), which already has to say "no round was
spent" and already explains why the submitter has not lost an attempt. The
`validation_rejected` event gets `{"uncounted": True, "of": REBIND_MAX}` so the timeline
does not print "round 5 of 3" either.

The feature loop (daemon.py:2511, 2639, 2661) is untouched: no feature round is ever a
rebind.

A rejected rebind reaches a worker over the bus exactly as an ordinary rejection does
(`_reject` posts to the ROLE `implementor`, daemon.py:2175) — no new exposure: an OS-forced
round below the cap already routes that way today on a parked order.

### 4.5 Attention

Checked, not assumed.

**While the rebind runs:** `ops.submit_for_validation` sets the status to `validating` and
calls `store.clear_attention` (ops.py:4166-4170). So the stored flag goes DOWN the moment
the rebind opens, with no new code — unlike the catch-up carry, which needed
`_carry_round_onto` (ops.py:5624) to lower it explicitly because no invariant lowers a flag
on a non-terminal status (`check_attention_reason_is_true`, invariants.py:1600, only
RELABELS when `true_blockers` is non-empty; `check_no_phantom_attention`, invariants.py:1801,
iterates `TERMINAL_STATUSES` only). Nothing re-raises it either: `SHA_MOVED_BLOCKER` is
gated on `wo["status"] == "waiting_pr_merge"` (invariants.py:796) and the order is
`validating`.

**Once the rebind passes:** `land_when_cleared` parks it back in `waiting_pr_merge`, so the
gate at invariants.py:796 opens again and `rejudge_exhausted` (invariants.py:514) is
re-asked. Its first three clauses still hold — the old `REJUDGE_DECLINED_EVENT` for that
head is still on the record (nothing deletes events) and the newest `automerge_held` event
is still `sha_moved` on that head, because a hold is only written when a merge is declined.
**The last clause is what clears it** (invariants.py:545-546): `store.validated_head` of the
latest round is now that head, because the rebind recorded it via `set_validation_head`
(daemon.py:1895, column at project_store.py:834) and passed. So `rejudge_exhausted` returns
False and `SHA_MOVED_BLOCKER` stops being derived. **An older decline on the same head
cannot leave a stale blocker up** — that clause exists for precisely this case and its
comment says so (invariants.py:541-544, "the user forces the round the machine left them,
it passes, and this would still be flagging them about it"). No change to
`rejudge_exhausted` is needed for the passing path.

**Once `REBIND_MAX` is spent:** a decline is written and `rejudge_exhausted` fires again —
but `SHA_MOVED_BLOCKER`'s text (invariants.py:299) is then FALSE: it says no rounds are
left and offers `validation.max_rounds` as the remedy, and raising `max_rounds` would do
nothing, because the rebind arm never reads it. The issue asks that `attention_reason`
reflect the live cause, so:

* `invariants.rejudge_exhausted` gains a filter: a decline whose payload
  `cause == ops.REBIND_EXHAUSTED` does not satisfy its first clause (invariants.py:527).
* **New** `invariants.rebind_exhausted(store, wo)` — the same three-fact shape, reading only
  declines with that cause — and **new** `REBIND_EXHAUSTED_BLOCKER`:

```
"the OS re-judged the merge it asked for {max} time(s) and the panel still refuses it —
 read the review and decide: merge it yourself, or send the worker back"
```

raised beside `SHA_MOVED_BLOCKER` at invariants.py:796, under the same
`status == "waiting_pr_merge"` gate. The two cannot both fire: the decline dedupe at
ops.py:4411 is per head across both causes, so one head carries one decline with one cause.
INV-ATTENTION-REASON is satisfied because `true_blockers` re-derives the new sentence.

`timeline._describe`'s `validation_rejudge_declined` branch (timeline.py:570-575) branches
on `cause`: the existing "round {next_round} of {max_rounds} would be the last" stays for
the budget decline; the rebind decline reads *"the head is now {head} and the OS has already
re-judged this merge {rebinds} time(s) — the limit"*, under the same label.

### 4.6 Config: a constant, not a config key

`ops.REBIND_MAX = 2`, a module constant beside `ops.CATCH_UP_MAX` (ops.py:5484).

The codebase's precedent is unambiguous: every bound on a loop the OS runs on ITSELF is a
constant — `ops.CATCH_UP_MAX` (3, ops.py:5484), `invariants.PR_REPAIR_MAX_ATTEMPTS` (3,
invariants.py:179), `ci.CHAIN_LIMIT` (10, ci.py:272), `ops.BOUNCE_LIMIT` (2, ops.py:3996).
`ValidationConfig` (src/jarvis/catalog.py:428) holds what a PROJECT decides — whether the
panel runs, whether the OS may merge, how many rework rounds a worker gets — and its
docstring puts every new field behind a measurement (catalog.py:431-447). A rebind bound is
not a project's policy; it is how many times the OS may re-judge its own repair before
admitting it cannot fix this one. Making it a key would also invite exactly the confusion
this spec removes: two round budgets to reason about instead of one budget and one
self-repair bound.

**Value 2**, not 3: each rebind is a full five-seat panel outside the budget the user
configured, and two is the smallest number that still allows one worker fix turn after the
first rejection.

## 5. Tests

Targeted tests only; CI runs the suite.

**`tests/test_rejudge_moved_head.py`** — owner of the round-spending policy:

1. `test_a_conflict_resolving_merge_past_the_cap_is_re_judged_without_spending_a_round` —
   an order with `counted_validation_rounds == max_rounds`, head moved by a merge of `main`
   whose resolution changed the diff: a round opens (`validation_forced` with
   `by == REJUDGE_BY_OS` and `rebind is True`), `uncounted == 1` on the row, and
   `counted_validation_rounds` is unchanged after it settles. This is wo-8736a5c5 / PR #794.
2. `test_a_passing_rebind_arms_the_merge` — the rebind passes, `validated_head` is the new
   head, `automerge.decide` arms and the `AUTOMERGE_GATE` request is filed; no
   `SHA_MOVED_BLOCKER` on the next reconcile tick and no ack was needed.
3. `test_a_rejected_rebind_goes_to_the_worker` — rejected at a row number past
   `max_rounds`: an envelope to role `implementor` carrying `REBIND_FEEDBACK`, the work
   order is not `needs_review`, and the message does NOT say "of {max_rounds}".
4. `test_rebind_max_exhausted_goes_to_the_user` — after `REBIND_MAX` rebinds the next
   moved head writes `REJUDGE_DECLINED_EVENT` with `cause == ops.REBIND_EXHAUSTED`,
   `true_blockers` yields `REBIND_EXHAUSTED_BLOCKER` and NOT `SHA_MOVED_BLOCKER`, and
   raising `validation.max_rounds` does not restart it.
5. `test_a_non_merge_head_change_past_the_cap_still_goes_to_the_user` — a worker PUSH past
   the cap: `base_merge_chain` returns `()`, no rebind, the existing
   `REJUDGE_DECLINED_EVENT` with `cause == ops.REJUDGE_BUDGET_SPENT` and today's
   `SHA_MOVED_BLOCKER`, byte-for-byte the current behaviour.

**`tests/test_validation_rounds.py`** (or `tests/test_forced_validation.py`, which already
owns `test_the_round_is_numbered_and_counted_like_any_other`, :385):

6. `test_an_uncounted_round_does_not_collide_with_the_next_ordinary_round` — settle a
   rebind as round N, then submit ordinarily: the new round is N+1 with a NEW row id, the
   rebind's row is untouched, and the work order does not park in `validating`. Plus: with
   no rebind on record, `numbered_validation_rounds == counted_validation_rounds` for
   every combination of `pending`/`failed`/`void`/counted outcomes — the migration claim.
7. `test_a_retried_submission_still_reuses_its_number` — the existing idempotency, asserted
   against the new formula.

**`tests/test_base_heal.py`** — owner of `_carry_catch_up` and the fake-`gh` parentage
fixture:

8. `test_a_changed_diff_walks_the_chain_only_when_a_rebind_could_open` — proof (b) fails
   and `rebind_possible` is False (budget spent / head already re-judged / worker busy):
   zero extra `gh api` calls on that tick (assert on `fake_gh.calls`), one
   `CARRY_REFUSED_EVENT` with `proof == "patch_id"`, and the `(head, proof)` dedupe still
   writes it once across three ticks.
9. `test_a_changed_diff_on_a_proved_chain_reports_a_rebind` — `_carry_catch_up` returns
   `CarryOutcome(carried=False, rebind=True)`, and still refuses the carry.

**`tests/test_timeline.py`** — 10. both decline sentences render off `cause`, and a rebind's
`validation_forced` reads "Validation forced by the OS" with the "does not count" clause.

**`tests/test_invariants.py`** — 11. `rejudge_exhausted` ignores a rebind-caused decline and
`rebind_exhausted` reads exactly those.

## 6. Scope

**Deliberately not covered.** The conflict poll itself (how the OS asks a worker to resolve
`CONFLICTING`); feature-order rounds (no rebind path, `fo_id` numbering switches for
consistency only); any change to what proof (a) or proof (b) accept — this spec changes
what happens AFTER they answer, nothing about the answers; `validation.max_rounds`
semantics for ordinary rounds; backfilling `uncounted` on existing rows (`0` is correct for
every one of them).

**Open, and the implementer should confirm rather than assume.** `void` rounds are excluded
from both counters, so their numbers are already reused today and the idempotent insert
would hand the next submission a `void` row. That is a pre-existing defect, untouched here
and deliberately not fixed by widening `numbered_validation_rounds` — widening it would
change numbering for rows that exist on live databases. If test 6's void case shows it
biting, file it separately.
