# An escalated round says why, and what to run next

Work order wo-c8a95ae5 · GitHub issue #1040 · jarvis 0.11.7

An escalated validation round records an explicit CAUSE CODE in its
`validation_escalated` event payload, and `ops.review_state` turns that cause plus
`wo["pr_url"]` into two sentences every surface prints: why the panel handed the order
over, and the exact command for THIS order's shape. A no-PR order is never pointed at
`jarvis validation force`.

Design settled by Neo question 1713: cause threaded through `Daemon._escalate` and
`ops.escalate_validation_round`, stored in the event payload (no `validation_rounds`
column, no migration), derived in `ops.review_state` only, and
`invariants.VALIDATION_STUCK_BLOCKER` left byte-identical.

## The problem

wo-3768437b sat in `needs_review` 4.8 days because the only word the OS gave its user was
"escalated", and that word covers eight different facts.

The order is an analyst under io-51469add; its deliverable is knowledge entries, so it has
NO pull request. Round 3 — the round after the user's own rework, marked
`uncounted (user_rework)` — was in substance an ordinary rejection: the tester seat vetoed
with concrete asks, the maintainer rejected too. `jarvis wo show` printed one line:

```
owed: round 3 gave up — One decision, and it settles the whole order
```

Two distinct failures follow from that line.

**1. "gave up" names no cause.** Eight call sites reach the same transition and the user
cannot tell them apart:

| site | fact |
|---|---|
| `Daemon._validate_work_order`, rejected arm | rejected, no round left (`the review was not satisfied after {max_rounds} rounds.`) |
| `Daemon._validate_work_order`, non-verdict arm | no verdict (`the review could not reach a verdict.`) |
| `Daemon._validate_work_order`, empty-submission guard | nothing to judge (`evidence_mod.nothing_to_judge(packet) == "escalate"`) |
| `Daemon._validate_work_order`, identical-fingerprint guard | submission identical to the preceding round |
| `Daemon._validation_refused` | prompt over the char ceiling, never sent |
| `Daemon._validation_outage` | validator unreachable `VALIDATION_OUTAGE_LIMIT` times running |
| `ops.submit_for_validation` | `BOUNCE_EXHAUSTED` — `BOUNCE_LIMIT = 2` send-backs changed nothing |

`Daemon._escalate` is a static delegate; every site funnels into
`ops.escalate_validation_round`, which writes
`add_event(wo_id, "validation_escalated", {"round": n, "round_id": round_id, "reason": reason})`.
The payload carries prose and no machine-readable cause, and
`invariants.validation_escalated` keys on nothing but
`latest["outcome"] == "escalated"`. So `ops.review_state` cannot
distinguish them either: its only escalation words are `"over the panel's objection"` in
`accept`, and `cli._readable_review` renders the rest as the fixed
`round {n} gave up`.

Some of those rows are a REJECTION being handed over; others are "nobody judged the work",
and a third group is "a round opened and no seat ever read the submission". The remedies
differ, and the surface collapses all of them into a phrase that reads as the second.

**2. No next command, and the one the user guessed is refused.** The user read "gave up"
as "the panel is unreachable", ran `jarvis validation force`, and was refused by
`ops.force_validation_refusal`:

```
<wo-id> carries no pull request, so a fresh round would read its worktree and record no
commit — which is the state this command exists to get out of.
```

The guess was not arbitrary. `invariants.PARKED_GIVE_UP_BLOCKER`
names `jarvis validation force` in the attention sentence of the SAME give-up one status
later, and nothing anywhere says the command depends on the order having a pull request.
`review_state` holds `accept` and `reject` sentences but no command string at all, so a
terminal reader has prose about what a dashboard BUTTON would do and no line to type.

**Root cause: the escalation transition is lossy.** The one home of the give-up
(`ops.escalate_validation_round`) discards which condition fired, so no reader downstream
can recover it, and no surface can derive a remedy from it. This spec fixes that root
cause. It does NOT fix issue #1039 (a round whose seats were all killed must open an
investigation) — open, owned by wo-fdd39919, cited only.

## The fix

### 1. Cause taxonomy, in `ops` beside the `uncounted_cause` constants

New constants in `src/jarvis/ops.py`, beside `REBIND_CAUSE` /
`USER_REWORK_CAUSE` / `RED_CI_CAUSE` — the same file-level block, because an escalation
cause is the same kind of fact about a round and a second home would drift.

Family `handover` — the panel REJECTED and is handing the decision over. Work was judged.

| constant | value | raised at |
|---|---|---|
| `ESCALATION_ROUNDS_SPENT` | `"rounds_spent"` | `Daemon._validate_work_order`'s rejected-with-no-round-left arm, ordinary case, `pos == max_rounds` |
| `ESCALATION_USER_REWORK_CEILING` | `"user_rework_ceiling"` | the same arm when `round_row["uncounted"]` and `uncounted_cause == ops.USER_REWORK_CAUSE` |
| `ESCALATION_REBINDS_SPENT` | `"rebinds_spent"` | the same arm when `as_rebind` and `rebinds >= ops.REBIND_MAX` |

Family `not_reviewed` — a round opened and NO SEAT judged the submission, so nothing was
rejected. These three were `handover` in the first implementation, which worded a
non-decision as a rejection against their own `why` sentence; the review of round 1 of
wo-c8a95ae5 split them out (kn-bec32368: one escalation, one wording). The family decides
the VERB only — force-eligibility is `unjudged` plus a pull request (§4), so these stay
ineligible.

| constant | value | raised at |
|---|---|---|
| `ESCALATION_BOUNCE_EXHAUSTED` | `"bounce_exhausted"` | `ops.submit_for_validation` |
| `ESCALATION_NOTHING_TO_JUDGE` | `"nothing_to_judge"` | `Daemon._validate_work_order`'s empty-submission guard |
| `ESCALATION_REPEAT_SUBMISSION` | `"repeat_submission"` | `Daemon._validate_work_order`'s identical-fingerprint guard |

Family `unjudged` — the panel COULD NOT JUDGE. Nobody has an opinion on the work.

| constant | value | raised at |
|---|---|---|
| `ESCALATION_NO_VERDICT` | `"no_verdict"` | `Daemon._validate_work_order`'s non-verdict arm |
| `ESCALATION_VALIDATOR_UNREACHABLE` | `"validator_unreachable"` | `Daemon._validation_outage` |
| `ESCALATION_PROMPT_REFUSED` | `"prompt_refused"` | `Daemon._validation_refused` |

Legacy family `unrecorded`, never written by any call site:
`ESCALATION_CAUSE_UNRECORDED = "unrecorded"`. Every `validation_escalated` event written
before this change lacks the field, and guessing a cause from its `reason` prose would put
a wrong sentence on an old order.

`ESCALATION_FAMILY: dict[str, str]` maps each constant to `"handover"`, `"not_reviewed"`,
`"unjudged"` or `"unrecorded"`. One dict, so adding a cause without a family is a
`KeyError` in the first test that touches it rather than a blank sentence in production.

NO ESCALATED STATE MAY CONTRADICT ITSELF: a verb claiming a rejection beside a `why` that
denies anyone judged is the defect this taxonomy exists to remove, and a test drives every
cause and asserts both directions.

The three causes of that one arm are picked at that site, from values already computed
earlier in `Daemon._validate_work_order` — `uncounted`, `cause`, `as_rebind`, `rebinds`, `pos`,
`goes_back = (rebinds < ops.REBIND_MAX) if as_rebind else (pos < max_rounds)`. No new read:
the branch is reached only when `goes_back` is False, and which of the three sub-cases
turned it False is exactly `as_rebind` plus `cause`.

### 2. Threading, and the one write

`ops.escalate_validation_round(store, wo, round_id, n, reason, *, cause: str)` — cause
KEYWORD-ONLY and REQUIRED. A default would let the next call site added be the next silent
`unrecorded`, and the defect is a missing field.

The payload becomes
`{"round": n, "round_id": round_id, "reason": reason, "cause": cause}`.
Nothing else in the function moves: `close_validation_round(round_id, "escalated", reason)`,
`set_status(wo_id, "needs_review")`,
`flag_attention(wo_id, VALIDATION_STUCK_BLOCKER)` and `add_notification(...)` stay as they
are. `ProjectStore.close_validation_round` is NOT touched and
`COUNTED_VALIDATION_OUTCOMES` is unchanged — the cause is not an outcome.

`Daemon._escalate(store, wo, round_id, n, reason, *, cause)` takes and forwards it; all
seven daemon call sites pass theirs. New read helper in `ops`:

```python
def escalation_cause(store, *, wo_id: str, round_id: int) -> str
```

Last `validation_escalated` event from `store.events_of_kind(wo_id, "validation_escalated")`
whose payload `round_id` matches, `payload.get("cause") or ESCALATION_CAUSE_UNRECORDED`.
Keyed on `round_id` and not on `round`, because a rebind spends a row number without
spending budget, so `round` is not unique on an order that had one.

### 3. Sentences and commands, derived in `ops.review_state` only

`ops.review_state` already reads
`latest = store.latest_validation_round(wo_id=wo["id"])` when `escalated`. It gains three
keys, all None/absent when `escalated` is False:

- `cause` — the constant's value.
- `family` — `"handover"`, `"not_reviewed"`, `"unjudged"` or `"unrecorded"`.
- `why` — one sentence, per cause. Table below.
- `next` — ordered list of command sentences, shape-correct per §4.

The existing keys — `pending`, `escalated`, `round`, `placement`, `scope`, `accept`,
`reject`, `strands` — keep their current wording byte-for-byte, with ONE exception, and
it is the rule this section now carries:

**A sentence may cite the panel's asks or the panel's objection only when
`family == "handover"`.** Every other family means no seat ever reached a verdict, so the
clause asserts an opinion nobody gave — the same contradiction the verb split fixed, one
sentence further down (Neo 1723). So the split is per family, in the two places that word
an action:

- `ops.review_state`'s `accept` — `handover` keeps `" over the panel's objection"`;
  `not_reviewed`, `unjudged` and `unrecorded` get `" without a panel verdict"`. The
  non-escalated branch is unchanged.
- `ops.escalation_next`'s two `jarvis wo review` lines — `handover` keeps "resumes the
  worker with the panel's asks as its guidance" and "lands it over the panel's objection";
  the other three families read "resumes the worker with your reason as its guidance" and
  "lands it without a panel verdict".

`reject` asserts no panel opinion already ("with your reason as the guidance it is sent
back with") and is unchanged for every family. `unrecorded` takes the neutral wording
because a legacy event with no cause may have been an unjudged round, which would make the
panel clause false. `strands`, `scope` and `placement` cite nothing and are untouched.

`why` per cause:

| cause | sentence |
|---|---|
| `rounds_spent` | `the panel rejected this and had no round left: round {n} was its last (validation.max_rounds).` |
| `user_rework_ceiling` | `the panel rejected this. Round {n} was the round after your own rejection, judged at the round budget's ceiling, so the rejection had no round left to send back — nobody abandoned it, and its asks are in the round below.` |
| `rebinds_spent` | `the panel rejected a merge the OS asked for itself, {REBIND_MAX} times running.` |
| `bounce_exhausted` | `the submitter was sent back {BOUNCE_LIMIT} times running for changing nothing the panel asked about, and changed none of them. Nobody judged the latest submission.` |
| `nothing_to_judge` | `the submission changed no files and recorded no other durable effect, so no seat judged it.` |
| `repeat_submission` | `the submission was identical to the preceding round, so the panel had nothing new to judge.` |
| `no_verdict` | `the panel reached no verdict. Nobody has judged the work.` |
| `validator_unreachable` | `the panel could not be reached, so nobody has judged the work.` |
| `prompt_refused` | `the OS built a seat prompt over its character ceiling and never sent it, so nobody has judged the work.` |
| `unrecorded` | `this round was escalated before the OS recorded which condition fired; the reason on the round below is what the panel said.` |

`user_rework_ceiling` is the issue's exact case and the reason the wording rule is in this
spec: it must not read as "the panel gave up". A rejection with concrete asks and a spent
budget is a decision, not an abandonment.

`cli._readable_review` currently hardcodes `round {n} gave up`. It stops:
the `where` clause becomes `round {n} {verb}`, with `verb` from `ops.review_state` —
`was rejected and handed to you` for `handover`, `was not reviewed` for `not_reviewed`,
`could not be judged` for `unjudged`, `was escalated` for `unrecorded`. Written in `ops` with the rest, on `_readable_review`'s
own documented rule: a phrase in the CLI could not reach the dashboard.

### 4. The commands, from `pr_url` and family

`next` is built from `str(wo.get("pr_url") or "")` and `family`. Three entries at most, in
the order a reader can act:

Every escalated order; both lines are WORDED PER FAMILY on §3's rule — only `handover`
cites the panel.

`handover`:

1. `jarvis wo review {wo_id} --reject --feedback "<what to change>"` — resumes the worker
   with the panel's asks as its guidance.
2. `jarvis wo review {wo_id} --feedback "<why>"` — lands it over the panel's objection.

`not_reviewed`, `unjudged`, `unrecorded`:

1. `jarvis wo review {wo_id} --reject --feedback "<what to change>"` — resumes the worker
   with your reason as its guidance.
2. `jarvis wo review {wo_id} --feedback "<why>"` — lands it without a panel verdict.

Added THIRD and only when `family == "unjudged"` AND `pr_url` is non-empty:

3. `jarvis validation force {wo_id} --reason "<why>"` — judges the current pull request
   again; worth it only because no seat ever judged this one.

Never offered otherwise, and for two separate reasons.

- No `pr_url`: `ops.force_validation_refusal` is the one home of the
  rule and refuses there. Offering a command the next line of the OS refuses is the
  defect in issue #1040.
- `family == "handover"`: the panel DID judge. A forced round would re-run the same seats
  on the same unchanged work and spend a round number doing it.
- `family == "not_reviewed"`: there is nothing to judge until the SUBMISSION changes — an
  empty diff, a byte-identical resubmission, or a bounce loop the submitter never answered.
  Forcing a round would reach the same guard again.

`needs_review` is in `ops.FORCEABLE_STATUSES`, so the status half never excludes
entry 3; `pr_url` and family are the whole test. `review_state` does NOT call
`force_validation_refusal` — that takes a resolved `cfg` and
`review_state` has no config and must stay free of one. The two predicates are allowed to
differ in strictness in ONE direction only, and a test pins it: whenever `next` offers
`validation force`, `force_validation_refusal` must not refuse for want of a pull request.

### 5. Where each sentence surfaces

| surface | how |
|---|---|
| `jarvis wo show` human | `cli._readable_review` prints `why:` after the `owed:` line, then one `next:` line per command |
| `jarvis wo show --json` | unchanged mechanism in `cli._wo_show` — the dict gains `cause`, `family`, `why`, `next` |
| `jarvis validation show` | `ops.validation_view` already holds the WO row and an open store: it adds `review = ops.review_state(store, row)` for work orders only, and `cli`'s round loop prints the same lines after the last round |
| work-order page | `ops.review_state` is already read by the work-order page handler in `src/jarvis/ui/app.py`; `src/jarvis/ui/templates/_review.html` `review_form` renders `review.why` and `review.next` as two more `<div class="row"><span class="sub">` rows beside the existing `scope` / `accept` / `reject` rows |

Still EXACTLY ONE form per page, still guarded by `review.placement`, and still every
sentence from `ops.review_state` (see `_review.html`'s header comment). `next` is rendered as text, not as
buttons: a dashboard reader with a terminal open needs the string, and a button for
`validation force` would duplicate the control the work-order page already has.

The line-builder itself moves into `ops.review_lines(state) -> list[str]` so the two CLI
surfaces cannot word one decision two ways; `cli._readable_review` keeps its
`--json`-preserves-the-dict trick and calls it.

### 6. Attention stays byte-identical

`invariants.VALIDATION_STUCK_BLOCKER` is unchanged — same bytes,
same single sentence for every cause. `invariants.PARKED_GIVE_UP_BLOCKER` is unchanged too: it applies one status later
(`waiting_pr_merge`), where a pull request exists by construction, so its
`jarvis validation force` pointer is already shape-correct.

No per-cause attention sentence. `invariants.true_blockers` must re-derive every attention
reason byte-for-byte or INV-ATTENTION-REASON rewrites it and the flag becomes un-ackable
(see `Daemon`'s reconcile pass); a per-cause reason would need the cause re-read inside
`true_blockers` on every reconcile tick, and any drift silently breaks `jarvis wo ack`.
`invariants.validation_escalated` keeps keying on
`outcome == "escalated"` only.

## Rejected alternatives

1. **A `cause` column on `validation_rounds`.** Loses: a migration for a field only the
   escalation path writes and only one projection reads, when the event payload is already
   JSON and already carries `round`, `round_id` and `reason`. Settled by Neo 1713.
2. **Derive the cause from the `reason` prose.** Loses: the reason is the VALIDATOR's text
   whenever one exists (`reason or f"the review was not satisfied after {max_rounds} rounds."`,
   in `Daemon._validate_work_order`'s rejected arm), so the OS's fallback string is present only when the panel said
   nothing. A regex over model prose is a classifier, and a wrong cause prints a wrong
   command.
3. **A per-cause attention sentence.** Loses: §6 — INV-ATTENTION-REASON.
4. **Widen `force_validation_refusal` to allow a no-PR force.** Loses: the refusal is
   correct. A worktree round records no commit, which is the state the command exists to
   leave. The defect is the OS recommending it, not the OS refusing it.
5. **Word the cause in `cli` and in the Jinja macro.** Loses: the two surfaces drift, and
   the terminal is the surface the user in #1040 was reading (see `_review.html`'s header comment).
6. **Default `cause=ESCALATION_CAUSE_UNRECORDED` on the keyword argument.** Loses: the
   next call site added forgets it and the surface silently reverts to the vague sentence
   for new orders. Legacy is a READ-time fallback; it is never a write.

## Tests, in TDD order

`tests/test_validation_surfaces.py` owns the review-control surfaces. The fixture
`escalated(project, title=...)` calls `ops.escalate_validation_round` with 5
POSITIONAL arguments; it gains `cause=ops.ESCALATION_ROUNDS_SPENT` as a default and a
`cause=` parameter so each test picks its own. That is the only existing-test change —
its existing assertions pass unmodified (none asserts the string `gave up`).

Now:

1. `test_escalation_records_its_cause_in_the_event_payload` — one parametrized case per
   constant in `ESCALATION_FAMILY` except `unrecorded`: assert the `validation_escalated`
   payload `cause`, and assert every cause has a family. It uses the staged `escalated`
   fixture and pins THE MAPPING only; WHICH cause the daemon picks is a separate question,
   driven at the real call site by the tests below plus the named tests in
   tests/test_validation_loop.py and tests/test_validation_bounce.py. `prompt_refused`,
   `repeat_submission` and `no_verdict` have no real call site reachable from a test, and
   the fixture test's docstring says so rather than implying coverage it has not got.
2. `test_a_rejection_on_a_user_rework_round_at_the_ceiling_says_so` — the #1040 shape:
   `max_rounds` reached, round 3 `uncounted` with `uncounted_cause == ops.USER_REWORK_CAUSE`,
   outcome `rejected`. Assert cause `user_rework_ceiling`, family `handover`, and that
   `why` contains `had no round left to send back` and NOT `gave up`.
3. `test_a_no_pr_escalated_order_is_never_pointed_at_validation_force` — the regression.
   No `pr_url`, for BOTH families, across all three surfaces: `jarvis wo show`,
   `jarvis wo show --json` serialized, `jarvis validation show`, and the rendered
   work-order page. Assert the string `validation force` is absent from each, and that
   `jarvis wo review` with `--reject` is present.
4. `test_a_legacy_escalated_event_with_no_cause_field_reads_as_unrecorded` — write the
   pre-change payload shape (`round`, `round_id`, `reason` only), assert
   `review_state["cause"] == ops.ESCALATION_CAUSE_UNRECORDED`, family `"unrecorded"`, a
   `why` that promises nothing about which condition fired, no crash, and no
   `validation force` in `next`.
5. `test_validation_stuck_blocker_is_unchanged_for_every_cause` — escalate once per cause
   and assert the attention reason equals `invariants.VALIDATION_STUCK_BLOCKER` exactly,
   then that `jarvis wo ack` clears the flag (the un-ackable failure mode).

Later:

6. `test_validation_force_is_offered_only_when_the_refusal_would_allow_it` — the §4
   one-direction pin: for every (family, `pr_url` present/absent) combination, if `next`
   contains `validation force` then `ops.force_validation_refusal` returns None or refuses
   for a reason other than the missing pull request.
7. `test_the_page_still_renders_exactly_one_review_form` — `_review.html`'s structural
   guarantee, with the two new rows present (`tests/test_ui.py`).
8. `test_validation_show_prints_the_owed_decision_once` — after the rounds, not per round.
9. `test_no_escalation_claims_a_rejection_while_denying_anyone_judged` — every cause, both
   directions: a verb containing `rejected` never pairs with a `why` containing "nobody
   judged" / "no seat judged" / "nothing new to judge" / "could not be reached" / "reached
   no verdict", and a `not_reviewed`/`unjudged` verb never contains `rejected`.
10. `test_a_rejection_with_the_counted_budget_spent_says_rounds_spent` and
    `test_a_rejected_rebind_with_the_rebind_budget_spent_says_rebinds_spent` — the other
    two arms of the three-way cause choice, through the real
    `Daemon._validate_work_order`, beside test 2's `user_rework_ceiling`.
11. `test_the_review_rows_escape_everything_they_are_handed` — `review.why` and
    `review.next` are placeholders with no escaping of their own, so the autoescape they
    depend on is pinned against the environment the app builds (`tests/test_ui.py`).

## Not covered

- Issue #1039 (a round whose seats were all killed must open an investigation) —
  wo-fdd39919 owns it. The `no_verdict` and `validator_unreachable` causes named here are
  the surface wording only; nothing in this spec opens an investigation.
- `jarvis validation force`'s own refusal sentences. Unchanged.
- Feature-order escalations. `ops.review_state` is work-order-only and stays so; §5 adds
  the key in `validation_view` for work orders only.
- The 4.8-day delay itself. No new alarm, no new timeout: this spec makes the decision
  legible, not faster. If escalated orders keep sitting, that is a separate staleness
  measurement on `needs_review`.

## Open question

`ops.review_lines` is a new shared formatter and `cli._readable_review` is its only
current caller besides `jarvis validation show`. If the implementer finds the two surfaces
genuinely want different leading labels (`owed:` vs a round-local header), splitting the
label from the sentences is acceptable — the sentences themselves must still come from
`ops`.
