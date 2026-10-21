# An unanswered refusal must name itself

Work order wo-024e207b. GitHub issue 1051.

## The problem

A `needs_review` work order held only by `ops.refusal_answered` (src/jarvis/ops.py:5361)
has no attention reason of its own, and for two work-order kinds it has no exit either.
Both halves have to be fixed or the order parks for ever.

### (a) The hold has no sentence, so the generic one lies

`ops.land_when_cleared` (src/jarvis/ops.py:5341-5347) refuses to land an order whose last
user decision was a refusal:

```python
    if not refusal_answered(store, wo_id):
        # The flag and the guidance are `review_work_order`'s — this is the panel's
        # settle path arriving at a work order the user has since turned down, and all
        # it owes is not to land it.
        store.set_status(wo_id, "needs_review")
        return "needs_review"
```

`invariants.true_blockers` (src/jarvis/invariants.py:1051) has no case for that state.
The `needs_review` triage at the end of its `governed and wo["status"] == "needs_review"`
branch has four cases, and the fall-through catches it:

```python
        # 4. Nothing more specific: the worker stopped without finishing. Guarded,
        #    because a `needs_review` holding a pending assumption is doing exactly what
        #    that status is for, and this line would call it a worker that gave up.
        elif not pending:
            blockers.append(IDLE_NO_FINISH_BLOCKER)
```

`IDLE_NO_FINISH_BLOCKER` (src/jarvis/invariants.py:448) reads:

```
the worker stopped mid-task without `jarvis wo finish` — nothing it started is still
running; read its last message, then `jarvis wo send` or `jarvis wo done`
```

Every clause of that is wrong here. The worker did not stop mid-task: it delivered, the
user refused an assumption, and the delivery is older than the refusal. `jarvis wo done`
refuses while assumptions are pending and otherwise closes an order the user has just
turned down. `jarvis wo send` relaunches a worker which, for the two kinds below, has no
legal command to answer with.

The OS already owns the correct sentence one module away and shows it nowhere the user
looks — `autoreview.REFUSAL_UNANSWERED_REASON` (src/jarvis/autoreview.py:162):

```python
REFUSAL_UNANSWERED_REASON = ("you refused an assumption on this work order and the "
                             "worker has not delivered again since")
```

### (b) For `analyst` and `planner` the hold is unanswerable

`refusal_answered` clears on a `finished` event newer than the refusal (or on a passed
user-rework round, which needs a judgeable head these kinds do not have). Three kinds
were briefed that their submission verb IS their finish, so their worker re-delivers
through that verb — and for two of them the verb is destructive, and then refused.

`ops.submit_findings` (src/jarvis/ops.py:8841), the analyst's `jarvis io report`:

```python
        # The WHOLE document, replacing whatever was there: a resubmission discards every
        # decision already recorded on the old findings. The status refusal above is what
        # keeps that safe — nothing can be resubmitted once the order has left
        # `plan_review`, which it does as soon as the last finding is decided.
        store.update_feature_order(io_id, plan=db.to_json(report))
```

and the status guard above it (src/jarvis/ops.py:8862-8867) refuses any submission once
the improvement order has left `planning`/`plan_review`. `ops.submit_plan`
(src/jarvis/ops.py:9966) has the identical pair: the same status refusal at
src/jarvis/ops.py:9981-9986, and `store.update_feature_order(fo_id, plan=...)` at
src/jarvis/ops.py:10044 replacing the whole plan and filing a fresh Neo review question.

So: the only verb the worker was told it has either discards the user's recorded
decisions, or is refused outright. Nothing else writes a `finished` event. The order is
held by a condition no actor can clear.

### The live subject

wo-d2d981e8, the analyst for io-edacb3ea: parked 12.9 days in `needs_review`, $14.51
spent, 5 of its 6 findings already decided by the user. Its attention line is
`IDLE_NO_FINISH_BLOCKER`. Re-running `jarvis io report` would be refused (io-edacb3ea
left `plan_review` when the fifth finding was decided) and, had it been accepted, would
have thrown away five decisions.

### Root cause, stated plainly

The root cause is that `refusal_answered`'s clearing condition — a `finished` event — was
written for the `worker` kind and never reconciled with the kinds whose briefing forbids
`jarvis wo finish`. The fix below repairs exactly that: it does not change
`refusal_answered`, it gives the condition a reachable answer for every kind and gives
the state a sentence. Not in scope, and named so it is not rediscovered: the briefings
for `analyst`, `planner`, `manager` and `investigator` each re-state the finish contract
in their own prose with no shared constant behind them, so a fifth kind will get this
wrong again. That is a separate consolidation.

## The fix

Neo confirmed **Option A** on question 1725: the OS says what the hold is and names the
command that clears it, per kind.

**Option B is REJECTED** — inferring that a refusal is answered because the user later
ACCEPTED a different assumption on the same order. It needs no new sentence and would
have unparked wo-d2d981e8 on its own. It is refused because it invents consent: accepting
assumption 6 says nothing about the rework refused on assumption 3, and `refusal_answered`
exists precisely so that a later green signal cannot land a decision the user turned down
(its docstring, src/jarvis/ops.py:5364-5368). A rule that reads one decision as another is
the silent relabelling kn-78346a2d names, with the user's authority attached.

Also rejected: **widening `IDLE_NO_FINISH_BLOCKER`'s wording** to cover both states. One
sentence for two causes sends half the readers to the wrong command, and INV-ATTENTION-
REASON cannot tell the two apart to re-derive which was meant.

### 1. Two constants in `invariants`, composed from the autoreview one

Beside `IDLE_NO_FINISH_BLOCKER` (src/jarvis/invariants.py:448):

```python
REFUSAL_UNANSWERED_BLOCKER = (
    f"{REFUSAL_UNANSWERED_REASON} — read your refusal, then `jarvis wo send` telling it "
    f"what to change; it re-delivers with `jarvis wo finish`")
REFUSAL_UNANSWERED_DOC_BLOCKER = (
    f"{REFUSAL_UNANSWERED_REASON} — it delivered a document, not code, and resubmitting "
    f"that document would replace it and discard the decisions you have already "
    f"recorded on it; `jarvis wo send` telling it what to change, and it re-delivers "
    f"with `jarvis wo finish`, which records the delivery and does not touch the "
    f"document")
```

`REFUSAL_UNANSWERED_REASON` is IMPORTED at module scope,
`from .autoreview import REFUSAL_UNANSWERED_REASON`, beside the existing
`from .automerge import HELD_NOT_PASSED, HELD_SHA_MOVED` (src/jarvis/invariants.py:49).

* **Why imported, not re-spelled.** The same fact already reaches the user twice — as the
  autoreview hold on `jarvis wo show` and, after this, as the attention line. Two
  spellings of one sentence pass every behavioural test and drift anyway (kn-4ea33fe6).
  The near-miss at src/jarvis/invariants.py:1439, `UNDECLARED_DELIVERY_REASON`, is NOT a
  precedent for re-spelling: it is a different sentence for a different audience (the
  finding a supervisor judge and a gate reviewer read), and it stays as it is.
* **Why the import cannot cycle.** `autoreview` imports only `navigation`, `specs`,
  `catalog`, `stakes` at module scope (src/jarvis/autoreview.py:80-85); `specs` imports
  `db` and `sections`. None reaches `invariants`. Same leaf argument the `automerge`
  import already carries in a comment one line above.
* **Why TWO constants and not one with a slot.** `remedies.render_attention`
  (src/jarvis/remedies.py:262) interpolates only values matching `_ID_PATTERN`
  (src/jarvis/remedies.py:245) — record ids and `#<digits>`. `"analyst"` is not an id, so
  a per-kind suffix cannot travel through a declared slot; it would have to be refused.
  Two whole sentences, two template keys, zero interpolation, is the only shape that
  table permits.

### 2. The kind tuple, and its one home

```python
#: The kinds whose FIRST delivery used a verb of their own (`jarvis io report`,
#: `jarvis fo plan`), both of which REPLACE the stored document — so re-delivering
#: through them after a refusal discards the user's decisions on it.
DOCUMENT_DELIVERY_KINDS = ("planner", "analyst")
```

In `invariants`, beside the two constants above — its only reader. NOT beside
`project_store.WO_KINDS` (src/jarvis/project_store.py:338): that tuple is the column
vocabulary, and a membership rule about `ops` submission verbs filed there invites readers
who want something else from it. kn-31f0f450's three-edit cost is for ADDING a kind and
does not apply; the test in §6 pins `DOCUMENT_DELIVERY_KINDS` as a subset of `WO_KINDS`,
so a renamed kind fails loudly instead of silently dropping a case.

`investigator` is deliberately absent. Its verb `jarvis investigate verdict` settles the
order with `close_out`, not `finish` (src/jarvis/ops.py:9577-9583), so it reaches
`completed` and never sits in `needs_review`; telling it to run `jarvis wo finish` would
re-open work that is already settled. `manager` is absent for the status reason: it is
`idle`, never `needs_review`, so the branch below cannot reach it.

### 3. The new case in `true_blockers`, between case 3 and case 4

Inside the existing `if governed and wo["status"] == "needs_review":` chain
(src/jarvis/invariants.py:1273-1333), after the `UNLANDED_BLOCKER` arm:

```python
        # 3b. The user refused an assumption and nothing has delivered since. Below the
        #     unlanded line — code stranded on a branch is the more urgent fact and
        #     names a different decision — and above the idle line, which would call a
        #     worker that DID deliver one that stopped mid-task.
        elif (not pending and PR_UNDECLARED_BLOCKER not in blockers
                and not refusal_answered(store, wo["id"])):
            blockers.append(REFUSAL_UNANSWERED_DOC_BLOCKER
                            if wo.get("kind") in DOCUMENT_DELIVERY_KINDS
                            else REFUSAL_UNANSWERED_BLOCKER)
```

with `from .ops import refusal_answered` inside the function body, the pattern the same
function already uses for `auto_review_at` (src/jarvis/invariants.py:1072).

* **Why here.** `true_blockers` is the single source of truth for attention reasons and
  INV-ATTENTION-REASON rewrites any flag it cannot re-derive. `refusal_answered` is a pure
  read of `reviewed` and `finished` events plus validation rounds, so the sentence
  re-derives identically on every tick. Nothing is interpolated into it, so the stored
  string compares equal (kn-681db233 point 3).
* **Cost.** The two event reads sit behind the status gate AND behind the `elif` chain, so
  they run only for a governed `needs_review` order with no pending assumption, no closed
  pull request, no panel give-up and no unlanded work. No other work order pays, matching
  the neighbouring branches.
* **Why the `PR_UNDECLARED_BLOCKER` guard.** That blocker (src/jarvis/invariants.py:294) is
  the same refusal WITH commits behind it and three failed OS nudges, appended earlier from
  `PR_REPAIR_BLOCKERS` and reachable in `needs_review`. Without the guard a worker in that
  state gets both lines, the second one weaker. Cases 1-3 need no guard: the `elif` chain
  already orders them.

### 4. The `_mentions_assumptions` tail must not fire on these

Both new sentences contain the word "assumption", so the tail at
src/jarvis/invariants.py:1322-1326 would read the list as nothing-but-assumption-reviews
and append `parked_reason`. For wo-d2d981e8 that resolves to `STALE_FINISH_BLOCKER`
(a `finished` event older than the last turn, no judged head), i.e. "the worker went back
to work after finishing and stopped again" stacked on a line that already names the exit —
a regression against today's behaviour, where `IDLE_NO_FINISH_BLOCKER` suppresses it.

```python
REFUSAL_HELD_BLOCKERS = frozenset({REFUSAL_UNANSWERED_BLOCKER,
                                   REFUSAL_UNANSWERED_DOC_BLOCKER})
...
    if all(_mentions_assumptions(b) and b not in REFUSAL_HELD_BLOCKERS
           for b in blockers):
```

The tail's own comment states the rule it is applying — an assumption review is the one
blocker that says who owes what without saying anything about the session behind it. These
two say plenty about the session, so they are not that.

### 5. `jarvis wo finish` as the sanctioned re-delivery

Register both keys in `remedies.ATTENTION_TEMPLATES` (src/jarvis/remedies.py:229):

```python
    "refusal_unanswered": ("REFUSAL_UNANSWERED_BLOCKER", ()),
    "refusal_unanswered_document": ("REFUSAL_UNANSWERED_DOC_BLOCKER", ()),
```

`Param.choices=tuple(ATTENTION_TEMPLATES)`, so registration is the whole remedy-side
change.

**What `ops.finish` (src/jarvis/ops.py:6529) does to an `analyst` or `planner`, read line
by line. No guard is needed. The findings, in order of the checks:**

1. **Open gate requests** — kind-blind, correct.
2. **`unlanded_work` (src/jarvis/ops.py:5049)** — does not refuse either kind. An analyst
   authors nothing in its worktree, so `authorship` is empty. A planner HAS a commit (its
   design doc) but also has `pr_url` on its record, written by `_record_spec_pull_request`
   during `submit_plan` (src/jarvis/ops.py:9929), and `unlanded_work` returns empty
   `Authored()` whenever the record carries a pull request. Neither needs `--abandon`.
3. **The `finished` event** is written BEFORE anything settles (src/jarvis/ops.py:6615),
   which is exactly what `refusal_answered` dates against. This is the whole mechanism.
4. **`validation_applies` (src/jarvis/ops.py:8139)** returns True for both kinds —
   `exempt_from_validation` covers `investigator` only — so a round IS opened. It is
   UNCOUNTED: `user_rework_pending` (src/jarvis/ops.py:5409) is true by construction here
   (the refusal is newer than the last settled round), so `submit_for_validation` stamps
   `USER_REWORK_CAUSE` and spends no round budget (src/jarvis/ops.py:5650-5655).
5. **What the panel then does with an analyst** is the load-bearing detail. The packet has
   no files, but `_plan_effects` (src/jarvis/ops.py:15824) yields an ATTESTED side effect
   for it: `feature_order_for_planner` matches any `feature_orders` row by `plan_wo_id`
   (src/jarvis/project_store.py:3351-3364), including an improvement order, whose report
   lives in the same `plan` column. So `evidence.nothing_to_judge` (src/jarvis/evidence.py:
   425) returns `"void"`, not `"escalate"` — the round is voided by `Daemon._void` with
   `panel_cleared=True` and the order lands `completed`. Were that collector not to match,
   the packet would be empty, the round would escalate with
   `ESCALATION_NOTHING_TO_JUDGE`, and the order would come straight back to
   `needs_review` under `VALIDATION_STUCK_BLOCKER` — one dead end swapped for another.
   Test 11 pins it. A planner's round is a REAL one over its spec pull request, which is
   the same round its first delivery already got.
6. **`land_when_cleared`** then sees `refusal_answered` true and lands by the ordinary
   path. A planner settles `completed` rather than `waiting_pr_merge` because
   `_record_spec_pull_request` writes the pull request non-declaratively
   (src/jarvis/ops.py:9878-9883).
7. **No hook blocks it.** `hooks.finish_summary_decision` caps the summary length only;
   the narrowed `jarvis` verb surface (`INVESTIGATOR_JARVIS_MUTATIONS`,
   src/jarvis/hooks.py:1644) applies to `investigator` and there is no analyst or planner
   equivalent.

**Known wart, out of scope:** `_plan_effects`' summary says "it decomposes into 0 work
orders" for a findings report, because a report has `findings` and not `children`. No seat
ever reads it — the round is voided before any model call — so it is cosmetic. Worth one
follow-up issue, not a change here.

**The briefings are CORRECTED, not deleted.** The submission verb is still the finish for
the FIRST delivery; `jarvis wo finish` is the re-delivery after a refusal.

Analyst, src/jarvis/dispatch.py:894-898, currently:

> submit it with the command below, which IS your finish. Do not run `jarvis wo finish` —
> submitting the report settles this work order for you.

becomes, in substance: `jarvis io report` is your finish and you submit the report ONCE;
if the user refuses an assumption afterwards, do NOT resubmit the report — the OS refuses
it and it would replace the document and discard the decisions already recorded on it.
Answer the refusal in your session, then run `jarvis wo finish --summary "…"`, which
records the re-delivery and does not touch the report.

Planner, src/jarvis/dispatch.py:735-736, the same correction with `jarvis fo plan` and
"the plan".

The manager line (src/jarvis/dispatch.py:1282) and the investigator line
(src/jarvis/dispatch.py:1083) are UNCHANGED: neither kind can reach this state (§2).

## Test plan

**tests/test_invariants.py**

1. `test_a_needs_review_order_held_only_by_an_unanswered_refusal_says_so` — `worker` kind,
   finish, assumption, `review --reject`, no later finish. `true_blockers == [REFUSAL_
   UNANSWERED_BLOCKER]`; `IDLE_NO_FINISH_BLOCKER not in` it.
2. `test_an_analyst_held_by_a_refusal_is_pointed_at_wo_finish` — same with `kind="analyst"`
   gives `REFUSAL_UNANSWERED_DOC_BLOCKER`; assert "`jarvis wo finish`" is in the sentence
   and "io report" is not. Repeat for `kind="planner"`.
3. `test_an_answered_refusal_still_derives_the_idle_line` — a `finished` event newer than
   the refusal gives `IDLE_NO_FINISH_BLOCKER`, unchanged. The no-regression test.
4. `test_unlanded_work_outranks_an_unanswered_refusal` — `work_unlanded_open` plus the
   refusal yields `UNLANDED_BLOCKER` and not the new line.
5. `test_a_pending_assumption_suppresses_the_refusal_line` — one pending assumption: the
   assumptions line only, proving the `not pending` guard.
6. `test_an_undeclared_delivery_is_not_said_twice` — `PR_UNDECLARED_BLOCKER` already
   appended: the new line is absent.
7. `test_the_refusal_line_does_not_drag_in_a_stale_finish` — §4's conditions (finish older
   than the last turn, no judged head, past the parked threshold): exactly one blocker.
8. `test_document_delivery_kinds_are_real_kinds` —
   `set(DOCUMENT_DELIVERY_KINDS) <= set(project_store.WO_KINDS)`, and `"investigator"` and
   `"manager"` are not in it.
9. `test_the_refusal_reason_is_re_derivable` — flag the new sentence, run the
   INV-ATTENTION-REASON check: no violation and the column is unchanged.

**tests/test_remedies.py**

10. `test_the_refusal_templates_render_verbatim` —
    `render_attention("refusal_unanswered") == invariants.REFUSAL_UNANSWERED_BLOCKER` and
    the same for `"refusal_unanswered_document"`; both keys appear in
    `tuple(ATTENTION_TEMPLATES)` and so in the remedy `Param.choices`.

**tests/test_findings_submit.py**

11. `test_wo_finish_after_a_refusal_leaves_the_report_untouched` — submit a report, decide
    some findings, refuse an assumption on the analyst, run `ops.finish`: the improvement
    order's `plan` column is byte-identical, every decided finding keeps its status,
    `refusal_answered` is now True, and the round the finish opened is uncounted with
    `USER_REWORK_CAUSE`.
12. `test_an_analyst_finish_is_voided_rather_than_escalated` — the packet for that finish
    carries the attested `plan_submitted` effect and `evidence.nothing_to_judge` returns
    `"void"`. This is §5 point 5; without it the fix is a second dead end.
13. `test_jarvis_io_report_is_still_refused_after_plan_review` — unchanged behaviour, kept
    explicit so nobody "helpfully" reopens the destructive path.

**tests/test_analyst_dispatch.py**

14. `test_the_analyst_briefing_names_wo_finish_as_the_redelivery` — the prompt still says
    `jarvis io report` is the finish, contains `jarvis wo finish` with "does not touch"
    nearby, and NO LONGER contains the bare "Do not run `jarvis wo finish`".

**tests/test_worker_brief.py** (which already builds the prompt via `_planner_prompt`,
line 606; tests/test_feature_orders.py:153 pins the `jarvis fo plan` line and must keep
passing)

15. `test_the_planner_briefing_names_wo_finish_as_the_redelivery` — the same three
    assertions for the planner.
