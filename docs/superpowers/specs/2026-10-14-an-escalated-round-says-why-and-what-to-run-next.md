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
| `src/jarvis/daemon.py:3070-3071` | rejected, no round left (`the review was not satisfied after {max_rounds} rounds.`) |
| `src/jarvis/daemon.py:3075-3076` | no verdict (`the review could not reach a verdict.`) |
| `src/jarvis/daemon.py:2933-2937` | nothing to judge (`evidence_mod.nothing_to_judge(packet) == "escalate"`) |
| `src/jarvis/daemon.py:2959-2965` | submission identical to the preceding round |
| `src/jarvis/daemon.py:3464-3468` | `_validation_refused` — prompt over the char ceiling, never sent |
| `src/jarvis/daemon.py:3493-3497` | `_validation_outage` — validator unreachable `VALIDATION_OUTAGE_LIMIT` times running |
| `src/jarvis/ops.py:5566` | `BOUNCE_EXHAUSTED` — `BOUNCE_LIMIT = 2` send-backs changed nothing |

`Daemon._escalate` (daemon.py:3268-3299) is a static delegate; every site funnels into
`ops.escalate_validation_round` (ops.py:5372-5393), which writes
`add_event(wo_id, "validation_escalated", {"round": n, "round_id": round_id, "reason": reason})`.
The payload carries prose and no machine-readable cause, and
`invariants.validation_escalated` (invariants.py:1642-1650) keys on nothing but
`latest["outcome"] == "escalated"`. So `ops.review_state` (ops.py:4050-4096) cannot
distinguish them either: its only escalation words are `"over the panel's objection"` in
`accept`, and `cli._readable_review` (cli.py:256-274) renders the rest as the fixed
`round {n} gave up`.

Four of those seven rows are a REJECTION being handed over. Three are "nobody judged the
work". The remedies are opposite, and the surface collapses both into a phrase that reads
as the second.

**2. No next command, and the one the user guessed is refused.** The user read "gave up"
as "the panel is unreachable", ran `jarvis validation force`, and was refused by
`ops.force_validation_refusal` (ops.py:5653-5657):

```
<wo-id> carries no pull request, so a fresh round would read its worktree and record no
commit — which is the state this command exists to get out of.
```

The guess was not arbitrary. `invariants.PARKED_GIVE_UP_BLOCKER` (invariants.py:416-419)
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

New constants at `src/jarvis/ops.py:7214-7223`, beside `REBIND_CAUSE` /
`USER_REWORK_CAUSE` / `RED_CI_CAUSE` — the same file-level block, because an escalation
cause is the same kind of fact about a round and a second home would drift.

Family `handover` — the panel REJECTED and is handing the decision over. Work was judged.

| constant | value | raised at |
|---|---|---|
| `ESCALATION_ROUNDS_SPENT` | `"rounds_spent"` | daemon.py:3064-3071, ordinary arm, `pos == max_rounds` |
| `ESCALATION_USER_REWORK_CEILING` | `"user_rework_ceiling"` | daemon.py:3064-3071 when `round_row["uncounted"]` and `uncounted_cause == ops.USER_REWORK_CAUSE` |
| `ESCALATION_REBINDS_SPENT` | `"rebinds_spent"` | daemon.py:3064-3071 when `as_rebind` and `rebinds >= ops.REBIND_MAX` |
| `ESCALATION_BOUNCE_EXHAUSTED` | `"bounce_exhausted"` | ops.py:5566 |
| `ESCALATION_NOTHING_TO_JUDGE` | `"nothing_to_judge"` | daemon.py:2933 |
| `ESCALATION_REPEAT_SUBMISSION` | `"repeat_submission"` | daemon.py:2959 |

Family `unjudged` — the panel COULD NOT JUDGE. Nobody has an opinion on the work.

| constant | value | raised at |
|---|---|---|
| `ESCALATION_NO_VERDICT` | `"no_verdict"` | daemon.py:3072-3076 |
| `ESCALATION_VALIDATOR_UNREACHABLE` | `"validator_unreachable"` | daemon.py:3493-3497 |
| `ESCALATION_PROMPT_REFUSED` | `"prompt_refused"` | daemon.py:3464-3468 |

Legacy family `unrecorded`, never written by any call site:
`ESCALATION_CAUSE_UNRECORDED = "unrecorded"`. Every `validation_escalated` event written
before this change lacks the field, and guessing a cause from its `reason` prose would put
a wrong sentence on an old order.

`ESCALATION_FAMILY: dict[str, str]` maps each constant to `"handover"`, `"unjudged"` or
`"unrecorded"`. One dict, so adding a cause without a family is a `KeyError` in the first
test that touches it rather than a blank sentence in production.

The three daemon.py:3064-3071 causes are picked at that site, from values already computed
at daemon.py:2826-2840 — `uncounted`, `cause`, `as_rebind`, `rebinds`, `pos`,
`goes_back = (rebinds < ops.REBIND_MAX) if as_rebind else (pos < max_rounds)`. No new read:
the branch is reached only when `goes_back` is False, and which of the three sub-cases
turned it False is exactly `as_rebind` plus `cause`.

### 2. Threading, and the one write

`ops.escalate_validation_round(store, wo, round_id, n, reason, *, cause: str)` — cause
KEYWORD-ONLY and REQUIRED. A default would let the next call site added be the next silent
`unrecorded`, and the defect is a missing field.

The payload becomes
`{"round": n, "round_id": round_id, "reason": reason, "cause": cause}` (ops.py:5385-5386).
Nothing else in the function moves: `close_validation_round(round_id, "escalated", reason)`,
`set_status(wo_id, "needs_review")`,
`flag_attention(wo_id, VALIDATION_STUCK_BLOCKER)` and `add_notification(...)` stay as they
are. `ProjectStore.close_validation_round` (project_store.py:5157-5172) is NOT touched and
`COUNTED_VALIDATION_OUTCOMES` is unchanged — the cause is not an outcome.

`Daemon._escalate(store, wo, round_id, n, reason, *, cause)` takes and forwards it; all
seven daemon call sites pass theirs. New read helper in `ops`:

```python
def escalation_cause(store, *, wo_id: str, round_id: int) -> str
```

Last `validation_escalated` event from `store.events_of_kind(wo_id, "validation_escalated")`
whose payload `round_id` matches, `payload.get("cause") or ESCALATION_CAUSE_UNRECORDED`.
Keyed on `round_id` and not on `round`, because a rebind spends a row number without
spending budget, so `round` is not unique on an order that had one (daemon.py:2819-2825).

### 3. Sentences and commands, derived in `ops.review_state` only

`ops.review_state` (ops.py:4050-4096) already reads
`latest = store.latest_validation_round(wo_id=wo["id"])` when `escalated`. It gains three
keys, all None/absent when `escalated` is False:

- `cause` — the constant's value.
- `family` — `"handover"`, `"unjudged"` or `"unrecorded"`.
- `why` — one sentence, per cause. Table below.
- `next` — ordered list of command sentences, shape-correct per §4.

The existing keys — `pending`, `escalated`, `round`, `placement`, `scope`, `accept`,
`reject`, `strands` — keep their current wording byte-for-byte. The dashboard and
`jarvis wo show` already print them and this change adds lines; it does not re-word
decided ones.

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

`cli._readable_review` (cli.py:256-274) currently hardcodes `round {n} gave up`. It stops:
the `where` clause becomes `round {n} {verb}`, with `verb` from `ops.review_state` —
`was rejected and handed to you` for `handover`, `could not be judged` for `unjudged`,
`was escalated` for `unrecorded`. Written in `ops` with the rest, on `_readable_review`'s
own documented rule (cli.py:261-263): a phrase in the CLI could not reach the dashboard.

### 4. The commands, from `pr_url` and family

`next` is built from `str(wo.get("pr_url") or "")` and `family`. Three entries at most, in
the order a reader can act:

Every escalated order, both families:

1. `jarvis wo review {wo_id} --reject --feedback "<what to change>"` — resumes the worker
   with the panel's asks as its guidance.
2. `jarvis wo review {wo_id} --feedback "<why>"` — lands it over the panel's objection.

Added THIRD and only when `family == "unjudged"` AND `pr_url` is non-empty:

3. `jarvis validation force {wo_id} --reason "<why>"` — judges the current pull request
   again; worth it only because no seat ever judged this one.

Never offered otherwise, and for two separate reasons.

- No `pr_url`: `ops.force_validation_refusal` (ops.py:5620-5670) is the one home of the
  rule and refuses at 5653-5657. Offering a command the next line of the OS refuses is the
  defect in issue #1040.
- `family == "handover"`: the panel DID judge. A forced round would re-run the same seats
  on the same unchanged work and spend a round number doing it.

`needs_review` is in `FORCEABLE_STATUSES` (ops.py:5646), so the status half never excludes
entry 3; `pr_url` and family are the whole test. `review_state` does NOT call
`force_validation_refusal` — that takes a resolved `cfg` (ops.py:5620-5621) and
`review_state` has no config and must stay free of one. The two predicates are allowed to
differ in strictness in ONE direction only, and a test pins it: whenever `next` offers
`validation force`, `force_validation_refusal` must not refuse for want of a pull request.

### 5. Where each sentence surfaces

| surface | how |
|---|---|
| `jarvis wo show` human | `cli._readable_review` (cli.py:256-274) prints `why:` after the `owed:` line, then one `next:` line per command |
| `jarvis wo show --json` | unchanged mechanism at cli.py:3691 — the dict gains `cause`, `family`, `why`, `next` |
| `jarvis validation show` | `ops.validation_view` (ops.py:4786-4807) already holds the WO row and an open store: it adds `review = ops.review_state(store, row)` for work orders only, and cli.py:5534's round loop prints the same lines after the last round |
| work-order page | `ops.review_state` is already read at `src/jarvis/ui/app.py:1615`; `src/jarvis/ui/templates/_review.html` `review_form` renders `review.why` and `review.next` as two more `<div class="row"><span class="sub">` rows beside the existing `scope` / `accept` / `reject` rows |

Still EXACTLY ONE form per page, still guarded by `review.placement`, and still every
sentence from `ops.review_state` (`_review.html:6-15`). `next` is rendered as text, not as
buttons: a dashboard reader with a terminal open needs the string, and a button for
`validation force` would duplicate the control the work-order page already has.

The line-builder itself moves into `ops.review_lines(state) -> list[str]` so the two CLI
surfaces cannot word one decision two ways; `cli._readable_review` keeps its
`--json`-preserves-the-dict trick and calls it.

### 6. Attention stays byte-identical

`invariants.VALIDATION_STUCK_BLOCKER` (invariants.py:402-403) is unchanged — same bytes,
same single sentence for every cause. `invariants.PARKED_GIVE_UP_BLOCKER`
(invariants.py:416-419) is unchanged too: it applies one status later
(`waiting_pr_merge`), where a pull request exists by construction, so its
`jarvis validation force` pointer is already shape-correct.

No per-cause attention sentence. `invariants.true_blockers` must re-derive every attention
reason byte-for-byte or INV-ATTENTION-REASON rewrites it and the flag becomes un-ackable
(daemon.py:3273-3276); a per-cause reason would need the cause re-read inside
`true_blockers` on every reconcile tick, and any drift silently breaks `jarvis wo ack`.
`invariants.validation_escalated` (invariants.py:1642-1650) keeps keying on
`outcome == "escalated"` only.

## Rejected alternatives

1. **A `cause` column on `validation_rounds`.** Loses: a migration for a field only the
   escalation path writes and only one projection reads, when the event payload is already
   JSON and already carries `round`, `round_id` and `reason`. Settled by Neo 1713.
2. **Derive the cause from the `reason` prose.** Loses: the reason is the VALIDATOR's text
   whenever one exists (`reason or f"the review was not satisfied after {max_rounds} rounds."`,
   daemon.py:3070-3071), so the OS's fallback string is present only when the panel said
   nothing. A regex over model prose is a classifier, and a wrong cause prints a wrong
   command.
3. **A per-cause attention sentence.** Loses: §6 — INV-ATTENTION-REASON.
4. **Widen `force_validation_refusal` to allow a no-PR force.** Loses: the refusal is
   correct. A worktree round records no commit, which is the state the command exists to
   leave (ops.py:5653-5657). The defect is the OS recommending it, not the OS refusing it.
5. **Word the cause in `cli` and in the Jinja macro.** Loses: the two surfaces drift, and
   the terminal is the surface the user in #1040 was reading (`_review.html:12-15`).
6. **Default `cause=ESCALATION_CAUSE_UNRECORDED` on the keyword argument.** Loses: the
   next call site added forgets it and the surface silently reverts to the vague sentence
   for new orders. Legacy is a READ-time fallback; it is never a write.

## Tests, in TDD order

`tests/test_validation_surfaces.py` owns the review-control surfaces. The fixture
`escalated(project, title=...)` at line 449 calls `ops.escalate_validation_round` with 5
POSITIONAL arguments; it gains `cause=ops.ESCALATION_ROUNDS_SPENT` as a default and a
`cause=` parameter so each test picks its own. That is the only existing-test change —
the assertions at lines 464, 483, 503 and 518 pass unmodified (none asserts the string
`gave up`).

Now:

1. `test_escalation_records_its_cause_in_the_event_payload` — one parametrized case per
   constant in `ESCALATION_FAMILY` except `unrecorded`: drive the real call site, assert
   the `validation_escalated` payload `cause`, and assert every cause has a family.
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
