# A dropped confirmation must not hold an assumption for ever

GitHub issue #833. Work order wo-12d3a2e7. The ruling this spec implements is Neo's,
answering for the user, on **Neo question 937** — every clause of §4, §5 and §6 is from
there and the deviations are named where they occur.

Fixes the confirmation pass of
[2026-09-23-an-assumption-judged-while-the-worker-still-runs.md](2026-09-23-an-assumption-judged-while-the-worker-still-runs.md)
§7, and extends the freshness rule of
[2026-09-26-an-unreachable-neo-question-is-not-a-question-in-flight.md](2026-09-26-an-unreachable-neo-question-is-not-a-question-in-flight.md)
§4 to a second class of dead link.

## 1. The problem

**A confirmation question that is dropped at the settle site leaves the assumption linked
to a spent question for ever.** The pass never runs again, the assumption never settles,
and every surface goes on rendering the reason the confirmation was dropped — a sentence
about a state that ended seconds later — as a present-tense fact.

### 1.1 The production record: wo-9b70ddec, project jarvis_os

Epoch seconds, from the work order's own timeline and `neo.db`:

| ts | what happened |
|---|---|
| 1790588118 | `finished` |
| 1790588120 | status `validating`, then `needs_review` — **while validation round 1 was still OPEN** (`outcome` pending) |
| 1790588137 | the parked auto-review pass took the confirmation branch (`daemon.py` `_review_assumptions_of`, `autoreview.decide_confirm` armed, `autoreview.propose_confirmation`) and filed confirmation questions **920, 921, 922** for assumptions #1–#3, each carrying `provisional_verdict='accept'` |
| 1790588139 | round 1 **REJECTED** — the order went back to `running` |
| 1790588148 / 163 / 171 | the three verdicts arrived. `Daemon._deliver_assumption_verdict` re-ran `autoreview.decide` against freshly read state, got `HELD_STATUS` — *"the work order is running, not waiting on a review"* — marked each question `escalated` and DROPPED the ruling, **leaving `assumptions.confirm_question_id` set to the spent question** |
| 1790588643, 1790605368, 1790606460 | the order delivered three more times and sat at `needs_review`. On each of those ticks `decide_confirm`'s `confirming` gate returned `HELD_CONFIRMING` — *"assumption #N is already with Neo to confirm (question 920)"* |

Two independent defects produced that, and the second is what makes it permanent.

**(a) The confirmation was filed against a diff the panel was about to reject.**
`decide_confirm` inherits `decide`'s condition 2 (`status == 'needs_review'`) and nothing
more about the round: `round_outcome` is only consulted for `escalated`
(src/jarvis/autoreview.py:851, via `_panel_gave_up`). A round whose outcome is still
pending arms the pass. `decide`'s docstring licenses exactly that for the ASK pass —
"arming on `pending` is explicitly allowed, so this is reachable, not theoretical"
(src/jarvis/daemon.py:5904-5911) — and the confirmation pass took the licence without
re-examining it. 19 seconds of `needs_review` was enough to spend three model calls on a
result that did not survive.

**(b) The link is never cleared, so the gate that exists to stop a second question
becomes a permanent hold.** `decide_confirm`'s second gate
(src/jarvis/autoreview.py:923-927) excludes only `unreachable_question_ids`, and that set
is `status == 'failed'` and nothing else — `Daemon._unreachable_question_ids`
(src/jarvis/daemon.py:5479-5488), by the deliberate ruling of the 2026-09-26 spec §4 and
`ops._unreachable_asks`' docstring: *"`escalated` is Neo handing the question back WITH a
decision"*. A question the daemon itself marked `escalated` at the drop site
(src/jarvis/daemon.py:5941) is not `failed`, so it is not in the set, so the gate holds
for the life of the work order.

### 1.2 Why nothing on the record says so

`HELD_CONFIRMING` is on `_holds_not_recorded(early=False)`
(src/jarvis/daemon.py:404), suppressed for `HELD_ASKED`'s reason: *"the question IS filed,
which is the pass working"*. So **no `autoreview_held` event was written on any of those
three later ticks.** The newest `autoreview_held` on the record stays the settle-site one
from 1790588148, whose reason is `HELD_STATUS`'s sentence, and both renderers show the
newest event:

* `ops.assumptions_with_rulings` → `ops.assumption_ruling_line` renders
  `Held by the OS — the work order is running, not waiting on a review`;
* `ops.autoreview_state` → `ops._autoreview_line`'s fallthrough (src/jarvis/ops.py:3614)
  renders `⚙ auto-review: held — the work order is running, not waiting on a review`.

Hours later, on an order sitting in `needs_review`. `_stale_panel_hold` (src/jarvis/ops.py:3172)
is the existing cure for precisely this class of lie and it is scoped to
`HELD_PANEL_GAVE_UP` alone, so it does not reach this one.

### 1.3 And the assumption is owed to the user in one direction and hidden in the other

* **Owed**: it is `pending`, so `ops.review_state` counts it (`N pending assumption(s)`,
  and the review form on the work-order page), `ops.assumption_line` prints
  `#N pending your review`, and `jarvis wo ack` / `jarvis wo done` refuse.
* **Hidden**: `invariants.true_blockers` (src/jarvis/invariants.py:679-691) drops it from
  the attention reason whenever `_os_is_confirming(a)` is true, and that predicate
  (src/jarvis/invariants.py:651-661) is `provisional_verdict == 'accept'` and nothing else.
  It cannot tell a confirmation in flight from one abandoned in February. So the order is
  not flagged, is not on `jarvis status --attention`, and carries a stale sentence saying
  the OS is busy with it.

That combination is the worst of both: the user cannot close the order and is never told
it is waiting for them.

### 1.4 The same shape elsewhere, and the contrast case

* **wo-3312682f** assumption #3 and **wo-672bd388** assumptions #2–#5: same sequence —
  confirmation filed, status flipped under it, `HELD_STATUS` drop, link left set,
  `HELD_CONFIRMING` for ever.
* **wo-7c7347e1**: the confirmation ran to completion and the assumptions settled, because
  nothing flipped the status under it. That path must be byte-identical after this change.

### 1.5 Root cause

**A link written at ask time is read as a present-tense claim about a row in another
database, and the one code path that can invalidate it does not.** This is
kn-a2ebbbdb and the 2026-09-26 spec's own thesis, one column along: that spec fixed the
`failed` case and explicitly refused to clear links (its "Rejected alternatives", Neo
question 736) because a failed question is still the record of an ask. The case it did
not have evidence for is the one where **the OS itself** threw the ruling away — and there
the ask is not a record of anything pending, it is a spent call the OS has already
decided not to use.

§4 below therefore does NOT re-open Neo 736: nothing clears a link because a question
failed. It clears a link because the SETTLE SITE dropped the ruling for a reason that says
nothing about the user's judgement.

## 2. The fix

Four changes, three of them small and one of them a judgement call about vocabulary:

1. **§4** — the settle site clears `confirm_question_id` on a TRANSIENT drop only, and
   supersedes the spent question. The next parked tick re-asks against the final delivered
   diff.
2. **§5** — `decide_confirm`, and only `decide_confirm`, gains a visible hold for a
   validation round that is still OPEN. That is what filed the premature confirmation.
3. **§6** — `HELD_CONFIRMING` is SPLIT in two: the live one stays suppressed, the spent
   one is visible and reads as waiting on the user's answer to question N.
4. **§7** — `invariants._os_is_confirming` stops suppressing the attention blocker once
   the confirmation is no longer in flight.

Nothing in the ASK pass (`autoreview.decide`, `decide_early`) changes. No new column, no
new question kind, no new event kind.

## 3. What is NOT in scope

* **Re-judging the early verdict.** A dropped confirmation re-asks the CONFIRMATION
  question (`propose_confirmation`, the `provisional_*` reading quoted in it). The early
  reading is not re-formed; `decide_early`'s condition 4 still allows one early verdict
  per assumption for ever.
* **Retrying a confirmation the panel gave up on, the project disabled, or the stakes net
  caught.** Those stay linked and are the user's — §4.2.
* **A second confirmation after Neo answered one.** A confirmation Neo ESCALATED is a
  decision handed back; `autoreview_unconfirmed` already renders it and the link stays.
  Only a drop by our own code re-arms.
* **Making the ask pass wait for a validation round.** §5 is the confirmation pass only,
  and §5.3 argues why.

## 4. A dropped confirmation clears its link — on transient drops only

Site: `Daemon._deliver_assumption_verdict`, the `if not still.armed:` branch,
src/jarvis/daemon.py:5935-5950. It is the only place in the codebase that throws away a
ruling the OS paid for, and it already has everything needed: `confirming` (the boolean at
src/jarvis/daemon.py:5869), `still.code`, the project store and the Neo store.

Why THERE and not in `autoreview`: `autoreview` is pure by contract (module docstring, and
the 2026-09-26 spec's third rejected alternative). Why not on the next tick's ask pass:
that pass cannot know WHY the link is spent without re-reading the question's status and
guessing at the code, and the drop site knows it for certain — the same
written-down-where-it-is-known rule as `propose`'s `early` flag (kn-e29d10fe).

### 4.1 The transient codes — the drop says nothing about the user's judgement

A code is TRANSIENT when both hold: it is a fact that can clear without the user doing
anything, and it carries no statement about whether this assumption is theirs to decide.

| code | why transient |
|---|---|
| `HELD_STATUS` | the order moved out of `needs_review` under the call — the observed cause of #833. It returns to `needs_review` on the next delivery, by itself, and the panel's rejection said nothing about the assumption |
| `HELD_REFUSAL_UNANSWERED` | "you refused an assumption and the worker has not delivered again since" is explicitly a NOT-YET: `ops.refusal_answered` flips the moment the worker delivers again. It is `decide`'s own retry-shaped condition |
| `HELD_OBJECTION_IN_FLIGHT` | already documented as "Means NOT YET and costs nothing: retried next tick" (`decide_confirm` docstring). §6.6 of the 2026-09-23 spec clears it without the user |

No fourth. Two candidates were examined and REJECTED:

* **`HELD_SETTLED`** — the row is no longer pending, so there is nothing to re-ask and the
  drop site returns earlier anyway (src/jarvis/daemon.py:5841). Clearing would be a write
  with no reader.
* **`HELD_EVIDENCE_SECRET`** — the diff carries something secret-shaped. It can clear (the
  next diff may not), but it is a statement that this assumption is the user's
  (`decide_evidence`: "assumption #N is yours"), and re-asking on a later delivery is a
  second chance to copy a secret into the question store. Held out deliberately; if a
  later work order wants it, it needs its own argument.

`HELD_DISABLED`, `HELD_UNJUDGED`, `HELD_OBJECTED`, `HELD_ASKED`, `HELD_CONFIRMING` and the
new codes of §5/§6 are unreachable at this site (the settle site calls `decide`, not
`decide_confirm`, and `decide`'s condition 6 is excluded by `asked_question_id`). The list
is therefore written as an ALLOWLIST — `autoreview.TRANSIENT_DROPS` — not as "everything
except the three below": an allowlist adds a code by an explicit edit, a blocklist adds
one by accident the day somebody writes a new hold.

```python
#: autoreview.py
TRANSIENT_DROPS = frozenset({HELD_STATUS, HELD_REFUSAL_UNANSWERED,
                             HELD_OBJECTION_IN_FLIGHT})
```

In `autoreview` rather than `daemon` so the codes and the judgement about them live beside
each other, and so the set is unit-testable without a database.

### 4.2 The codes that STAY LINKED

`HELD_HIGH_STAKES`, `HELD_PANEL_GAVE_UP`, `HELD_DISABLED` (unreachable here today, listed
for the day it is not) — and anything else absent from `TRANSIENT_DROPS`.

Each means the assumption is the USER'S. Re-asking would spend a model call per assumption
per reconcile tick for the rest of the work order's life, to be refused every time by the
same net — and it would do it while the user's decision sits there unanswered, which is the
OS lobbying them (`propose`'s docstring, the same argument for never re-asking an escalated
question).

### 4.3 The three writes, in this order

On a transient drop, and after the existing `autoreview_escalated` event is written so the
record of the drop is complete first:

1. `neo_store.supersede(q["id"], answer, why)` **instead of** the existing
   `neo_store.mark(q["id"], "escalated", …)`. `escalated` means the user must answer, and
   after step 2 nobody can: the link is gone. `supersede` is guarded on `OPEN_Q_STATUSES`
   and writes `answered_by='os'`, keeping it out of every review surface
   (src/jarvis/neo_store.py:409-433). **This is load-bearing, not tidying**:
   `invariants._stale_assumption_question` resolves a stranded question through
   `store.assumption_for_question`, which after step 2 returns `None`, and a `None` there
   is deliberately LEFT ALONE (that is how another project's rows are skipped). So a
   cleared link plus an `escalated` question is a row stuck in `ops._neo_attention` for
   ever — INV-NEO-ESCALATION-STALE's exact failure, re-introduced by the fix. The `answer`
   text names the reason and says a fresh confirmation will be asked.
2. `store.clear_assumption_confirmation(assumption["id"])` — a new one-line verb beside
   `link_assumption_confirmation` (src/jarvis/project_store.py:4231), `SET
   confirm_question_id=NULL`. A separate verb rather than `link_assumption_confirmation(aid,
   None)`: the parameter is typed `int` and a nullable overload would let the ask path
   clear a link by passing a falsy question id.
3. `store.add_event(wo_id, "autoreview_held", …)` is already written by the existing
   `_note_autoreview_held(…, settling=True)` call and keeps its `settling` semantics: the
   settle site records every code, including `status`. Unchanged.

The spent question id is NOT lost by step 2: it is on the `autoreview_asked` event
(`propose_confirmation`, with `confirm: True`), on the `autoreview_escalated` event
(`neo_question_id`, `dropped`), and on the hold event. `ops.assumptions_with_rulings`
reads all three.

`confirming` is computed at src/jarvis/daemon.py:5869, before any of this, so the routing
of the verdict is unaffected. On a NON-confirmation drop (`confirming == False`) nothing
here runs at all: `neo_question_id` keeps its 2026-09-26 semantics.

### 4.4 What the next tick then does

`_review_assumptions_of` takes the confirmation branch again (the row still carries
`provisional_verdict='accept'`), `decide_confirm`'s gate sees no link, `decide`'s
conditions are re-run against the NOW state — `needs_review`, the round resolved, no
refusal outstanding — and `propose_confirmation` files a new question against the FINAL
delivered diff. One `autoreview_asked` event per attempt, which is the record of exactly
what happened.

**The bound on re-asking is the drop itself.** Each re-ask can only be dropped again by a
transient code, and each transient code is a state that has to CHANGE for the re-ask to
have been armed in the first place. The pathological case — an order that delivers, is
rejected, delivers again, forever — spends one confirmation per delivery, which is the
same order of cost as the ask pass and is bounded by `validation.max_rounds`.

## 5. A validation round that is still open holds the confirmation, visibly

New code, `decide_confirm` only:

```python
HELD_ROUND_OPEN = "round_open"
```

### 5.1 The gate

In `decide_confirm`, after the `confirming` gate and before `objections_outstanding`: held
when a round EXISTS and its outcome is unresolved — empty or pending. The caller already
reads the round once per work order (`latest = store.latest_validation_round(...)`,
src/jarvis/daemon.py:5514) and already passes `round_outcome`, `round_n` and
`round_reason`; the gate needs one more fact, "a round exists and it has not resolved",
which is `round_n > 0 and not round_outcome`. No new argument and no new read: a
`round_outcome` of `''` with a `round_n` of 0 is "no round at all" and does NOT hold —
a project with validation off must behave exactly as today.

The reason names the round: `the validation panel has not finished round N — the result
this confirms against may be about to be sent back`.

### 5.2 Why the confirmation pass and not the ask pass

`decide`'s docstring states the licence explicitly ("arming on `pending` is explicitly
allowed"), and it is right there: the ask pass judges the ASSUMPTION — a sentence the
worker wrote, which a panel verdict does not change. It costs one model call and settles
nothing that the settle-site re-check does not re-examine.

The confirmation pass judges the DELIVERED RESULT: `_confirm_question` interpolates the
diff and `result_summary` and instructs the reviewer to rule against them
(src/jarvis/autoreview.py:1260-1282). An open round means that result may be about to be
sent back — and when it is, the diff the reviewer read is not the diff that ships. That is
not a wasted call, it is a call on the wrong evidence. Confirming against a superseded
diff is the failure the whole two-pass design exists to prevent (2026-09-23 spec §7: "the
intention has become a diff … and only then may it settle").

`decide_early` is untouched for the same reason, one step further: it has no diff at all.

### 5.3 The hold is RECORDED, not suppressed

`HELD_ROUND_OPEN` does NOT go on `_holds_not_recorded`. Every entry on that list is a
mechanism that was "never a candidate" (src/jarvis/daemon.py:374-398); this one is a row
the OS looked at, could have confirmed, and chose not to yet — kn-22ba6087's case, like
`HELD_OBJECTION_IN_FLIGHT` beside it. It is written once per (assumption, code, round) by
`_hold_is_news`' key, so a long round costs one event, and the round in the key means the
NEXT round's hold is written too.

It renders through the existing `autoreview_held` branches with no new renderer:
`Held by the OS — the validation panel has not finished round 2 …`.

## 6. `HELD_CONFIRMING` splits in two

**Recommendation taken: SPLIT.** Two codes, because one code with a conditional
suppression would make `_holds_not_recorded`'s answer depend on state it cannot see — that
function takes a bool and returns a tuple of codes, and every caller of
`_note_autoreview_held` passes the tuple, not the row.

```python
#: the confirmation is genuinely with Neo — the pass working. SUPPRESSED, as today.
HELD_CONFIRMING = "confirming"
#: the link points at a question that is no longer open. VISIBLE: it is the user's.
HELD_CONFIRM_SPENT = "confirm_spent"
```

`decide_confirm`'s gate needs one more fact to tell them apart, and it must stay pure. A
new keyword, `confirmation_open: bool = True`, derived by the caller exactly as
`unreachable_question_ids` is:

```python
def decide_confirm(..., unreachable_question_ids=(), confirmation_open: bool = True, ...)
```

* link set, not unreachable, `confirmation_open` → `HELD_CONFIRMING`, suppressed. Today's
  behaviour, and the default keeps every existing caller and every existing unit test on
  it.
* link set, not unreachable, NOT open → `HELD_CONFIRM_SPENT`, recorded, reason:
  `assumption #N is yours — the OS asked Neo to confirm its early reading (question 920)
  and that question is no longer open`.
* link in `unreachable_question_ids` (`failed`) → armed, exactly as the 2026-09-26 spec §4
  requires. Unchanged, and checked FIRST so a failed question is re-asked rather than
  reported as spent.

The daemon derives `confirmation_open` in `_review_assumptions_of`, from the
`neo_store.get(qid)` read `_unreachable_question_ids` already performs — one method
returning both facts, so the tick costs no extra row read: open means
`status in neo_store.NEO_HELD_Q_STATUSES` (`queued`, `answering`). `escalated` and
`answered` are NOT open: Neo is finished with the question either way, and on `escalated`
the assumption is the user's.

After §4, `HELD_CONFIRM_SPENT` is reachable only from a NON-transient drop or from Neo
escalating the confirmation. That is exactly the set of rows that are the user's and have
no other line saying so — which is why it is visible.

### 6.1 Readers checked before adding a code

Every reader that keys on `payload["code"]`:

* `daemon._holds_not_recorded` — the suppression list. Takes `HELD_CONFIRMING`, not
  `HELD_CONFIRM_SPENT`.
* `daemon._hold_is_news` / `_note_autoreview_held` — the dedupe key is
  `(assumption_id, code, round)`. A new code is a new key, so the first spent hold is
  written and the rest of the ticks are silent; the transition
  `CONFIRMING -> CONFIRM_SPENT` is a key change and IS written, which is the whole point
  (`_hold_is_news`' newest-only comparison, issue #782).
* `ops._panel_hold_is_stale` / `_stale_panel_hold` — both return early unless the code is
  `HELD_PANEL_GAVE_UP` (src/jarvis/ops.py:3164, 3185). A new code passes through
  untouched, which is correct: a spent-confirmation hold has no round to go stale against.
* `cli._readable_autoreview.gave_up` — same single-code comparison
  (src/jarvis/cli.py:200), so no `jarvis validation show` pointer is appended. Correct:
  this hold is not about a round.
* `ui/templates/work_order.html:277` — `a.os_ruling.code == 'panel_gave_up'` for the round
  anchor. Unaffected.

No reader enumerates the codes, so nothing breaks on a new one. `ops._autoreview_line`'s
and `assumption_ruling_line`'s `autoreview_held` branches are code-AGNOSTIC — they print
`reason` — which is why the fix is a reason and a visibility decision rather than a new
renderer.

### 6.2 The other half of ruling 3: no surface may show the stale reason

With §4 and §6 in place the stale sentence is displaced by a newer event in both
renderers, because both take the NEWEST event per subject and both new holds are newer
than the settle-site one:

* transient drop → the next tick writes `autoreview_asked` (newer ts) → the line becomes
  `Asked Neo (question 947), awaiting ruling`;
* non-transient drop → the next tick writes `autoreview_held` with
  `HELD_CONFIRM_SPENT` (newer ts, same rank 5 for `autoreview_held`, and
  `assumptions_with_rulings` compares `(ts, _RULING_RANK[kind])` so the later ts wins) →
  the line becomes `Held by the OS — assumption #N is yours … (question 920) …`.

No freshness predicate is added for `HELD_STATUS`. A third `_stale_*` derivation was
considered and REJECTED in §9: suppressing the sentence leaves a row reading
"nothing has looked at this", and the honest fix is to write the event that IS true.

## 7. The attention blocker stops lying in the other direction

`invariants._os_is_confirming` (src/jarvis/invariants.py:651-661) must mean what its name
says. Today `provisional_verdict == 'accept'` suppresses the
`N assumption(s) pending your review` blocker for ever — including on every row in §1.1,
which is why those orders were never flagged.

It gains the same fact §6 derives: the accept branch is true only while the confirmation is
genuinely in flight, i.e. **no confirmation has been dropped and left linked**. Locally
derivable with no model call and no new column:

* `confirm_question_id` NULL → a confirmation is still to come (or was cleared by §4 and
  will be re-asked on the next tick) → suppress, as today;
* `confirm_question_id` set and that question `queued`/`answering` → in flight →
  suppress;
* `confirm_question_id` set and that question `escalated`/`answered`/`failed` → the OS is
  NOT confirming → **do not suppress**: the blocker appears, the order is flagged, the user
  sees it.

The question read is cross-DB. That is `invariants.awaiting_neo`'s documented precedent
(src/jarvis/invariants.py:1504-1534) and `ops._unreachable_asks`' — same best-effort rule,
same failure direction: an unreadable `neo.db` must fail TOWARD the user, so an
unresolvable link does NOT suppress. It costs at most one row read per pending assumption
that carries a confirmation link, on an order already in `needs_review`.

The `object` branch of `_os_is_confirming` is untouched: it is about a delivered objection
and the worker, not about a confirmation.

## 8. Readers — the full test surface

Neo's condition on question 937 was "test it on every reader". Each row states what it
shows for a dropped confirmation BEFORE and AFTER.

| # | reader | before | after |
|---|---|---|---|
| 1 | `ops.assumptions_with_rulings` (src/jarvis/ops.py:3246) — the ONE enrichment point, feeds both surfaces | newest event is the settle-site `autoreview_held` with `HELD_STATUS`, for ever | transient: newest is the re-ask `autoreview_asked`, then `autoreview_confirmed`. Non-transient: newest is `HELD_CONFIRM_SPENT` |
| 2 | `ops.assumption_ruling_line` (src/jarvis/ops.py:3425) | `Held by the OS — the work order is running, not waiting on a review` | `Asked Neo (question 947), awaiting ruling`, or `Held by the OS — assumption #3 is yours — the OS asked Neo to confirm … (question 920) and that question is no longer open` |
| 3 | `ops.autoreview_state` + `ops._autoreview_line` (src/jarvis/ops.py:2986, 3569) — the `⚙ auto-review:` line | `held — the work order is running, not waiting on a review` | `assumption #3 is with Neo (question 947)` / `held — assumption #3 is yours …` |
| 4 | `ops._RULING_RANK` (src/jarvis/ops.py:3135) | ranks the existing nine kinds | UNCHANGED — no new event kind. The tie-break is the `(ts, rank)` tuple of §6.2, and the re-ask's `autoreview_asked` (rank 1) wins on ts as designed |
| 5 | `ops._panel_hold_is_stale`, `_stale_panel_hold` (src/jarvis/ops.py:3142, 3172) | drop a stale `panel_gave_up` hold; ignore every other code | UNCHANGED, and asserted so: a `HELD_CONFIRM_SPENT` hold must not be dropped by them |
| 6 | `ops._unreachable_asks` (src/jarvis/ops.py:3194) | `failed` only; an `escalated` confirmation reads as in flight | UNCHANGED. §6's "open" question is a DIFFERENT predicate for a different purpose (a decision gate, not a rendering), and both stay one-fact-one-place |
| 7 | `ops.assumption_line`, `ops.provisional_line` (src/jarvis/ops.py:3543, 3491) | `#3 pending your review: … — Held by the OS — …; provisionally accepted by the OS (Neo, sonnet) …` | the provisional half is unchanged (it is a column and cannot go stale); the ruling half is #2's |
| 8 | `cli._readable_autoreview` (src/jarvis/cli.py:179) and the `jarvis wo show` rendering | prints #2 and #3; no round pointer (code is not `panel_gave_up`) | same shape, current sentence. No pointer for the new codes |
| 9 | the work-order dashboard page (`ui/app.py:1133`, `ui/templates/work_order.html:88`, `265-279`) | `⚙ auto-review:` stale line; no `question →` link on a held row (a hold payload carries no `neo_question_id`) | current line. **The question id must be IN THE REASON TEXT** for `HELD_CONFIRM_SPENT`, since the template's link is driven by `os_ruling.neo_question_id` and a hold has none — this is why §6's reason names question 920 in prose |
| 10 | `/neo` and `/neo/question/<id>` via `ops._neo_attention` | question 920 sits `escalated` for ever, asking the user to answer a confirmation whose subject the OS abandoned | transient: `supersede`d (`answered_by='os'`) and off every review surface (§4.3). Non-transient: stays `escalated` and IS the user's — correct, and the link that lets `invariants._stale_assumption_question` close it when they review survives |
| 11 | `invariants.true_blockers` + `_os_is_confirming` (src/jarvis/invariants.py:664, 651) | blocker SUPPRESSED for ever; the order is not on the attention list | suppressed only while a confirmation is in flight (§7) |
| 12 | `invariants.check_attention_reason_is_true` (INV-ATTENTION-REASON) | re-derives from `true_blockers`, so it agrees with the lie | agrees with the corrected derivation; a stale "0 assumptions" reason is repaired as it is today |
| 13 | `invariants.check_neo_escalations_are_live` + `_stale_assumption_question` (src/jarvis/invariants.py:2079, 2213) | closes an escalated assumption question once the assumption settles, resolving it through `assumption_for_question` | UNCHANGED, and it is the reason §4.3 supersedes before clearing: a cleared link makes the row unresolvable, and unresolvable is deliberately left alone |
| 14 | `ops.review_state` (src/jarvis/ops.py:3046) | counts the pending row; the page offers the form | unchanged — the row IS pending either way. The fix does not change what a review does, only whether the user is told to make one |
| 15 | `project_store.assumption_for_question` (src/jarvis/project_store.py:4189) | resolves either column | after a clear, the spent question resolves to nothing. Acceptable precisely because §4.3 closed it first; a test pins that pair |

## 9. Rejected alternatives

* **Add `escalated` to `_unreachable_question_ids`.** The one-line fix, and it is wrong in
  both directions. It would re-ask a confirmation Neo ESCALATED — a decision handed back to
  the user — every reconcile tick, and it would contradict the 2026-09-26 spec §4 and
  `ops._unreachable_asks`' explicit ruling that `escalated` is a decision, not an outage.
  The distinction that matters is not the question's status, it is WHO dropped the ruling.
* **Clear the link whenever the question is not open.** Collapses §4.1 and §4.2 into one
  rule and therefore re-asks a high-stakes confirmation for ever: one model call per
  assumption per tick, each refused by the same net. Neo 937 refused it by name.
* **Clear the link at ask time on every parked tick** (treat the column as a per-tick
  lock). Loses the one-question-per-pass invariant `confirm_question_id` exists for
  (`decide_confirm` docstring: "THIS, AND NOT CONDITION 6, IS WHAT KEEPS ONE QUESTION PER
  ASSUMPTION PER PASS HERE") and would file a second confirmation while the first is being
  answered.
* **Suppress the stale sentence with a `_stale_status_hold` predicate**, the shape of
  `_panel_hold_is_stale`. Fixes the rendering and nothing else: the assumption still never
  settles, and the row falls back to "nothing has looked at this", which understates a row
  the OS spent three model calls on. A rendering cure for a state-machine defect.
* **Make `decide_confirm` wait for `needs_review` to be STABLE** (a dwell time before
  confirming). Unfalsifiable — there is no duration after which a panel cannot reject —
  and it delays every confirmation on the fleet to fix a case §5's one-line gate answers
  exactly.
* **Hold the confirmation until the round PASSES.** Stronger than §5 and too strong: a
  round that ESCALATED is already `_panel_gave_up`'s, and an order whose validation is
  disabled has no round at all. "Open" is the precise fact; "passed" would switch the
  feature off for every project that does not run the panel.
* **A `confirm_attempts` column and a retry ceiling.** No column is needed: §4.4's bound is
  structural, and the attempts are already countable from the `autoreview_asked` events.

## 10. Tests

One test per cited instance shape, in the file that owns the surface. Pure-decision tests
go beside their existing siblings; the drop site is daemon-level.

### 10.1 `tests/test_autoreview_confirm.py` — the confirmation pass, pure and daemon

1. **(a) the wo-9b70ddec / wo-672bd388 / wo-3312682f shape.** Daemon-level, in the
   `# -- the daemon: what comes back` section beside
   `test_a_confirmed_assumption_settles_exactly_as_an_ordinary_acceptance_does`: file a
   confirmation, flip the order to `running` under it, drain the verdict. Assert the row's
   `confirm_question_id is None`, the question is closed with `answered_by='os'` (NOT
   `escalated`), then put the order back to `needs_review`, tick, and assert a SECOND
   confirmation question exists carrying the final diff, and that draining it settles the
   assumption `accepted` / `decided_by='neo'` with one `autoreview_confirmed` event.
2. **(b) high stakes stays linked.** Same setup, drop with `HELD_HIGH_STAKES`: the link is
   unchanged, the question is `escalated`, the next two ticks file NO new question, and the
   newest `autoreview_held` is `HELD_CONFIRM_SPENT` whose reason contains the question id
   and does NOT contain "not waiting on a review". Assert `ops.assumption_ruling_line` and
   `ops.autoreview_state(...)["line"]` both render that, and that a third tick writes no
   further event (the dedupe still bounds it).
3. **Pure:** `TRANSIENT_DROPS` membership test naming all three codes and asserting
   `HELD_HIGH_STAKES`, `HELD_PANEL_GAVE_UP`, `HELD_DISABLED`, `HELD_EVIDENCE_SECRET`,
   `HELD_SETTLED` are absent — the allowlist, asserted as a whole so a future addition is
   a visible edit.
4. **Pure:** `confirmation_open=False` on a linked row yields `HELD_CONFIRM_SPENT`;
   `True` yields `HELD_CONFIRMING` (the existing
   `test_a_confirmation_already_filed_is_not_filed_twice` must pass UNCHANGED, which is
   what the default proves); `unreachable_question_ids` still wins over both
   (`test_a_confirmation_nobody_will_ever_answer_is_asked_again`, unchanged).
5. **(c) the open round.** Pure: `round_n=1, round_outcome=""` → `HELD_ROUND_OPEN` naming
   round 1; `round_outcome="passed"` → armed; no round at all (`round_n=0`) → armed.
   Daemon-level: a work order at `needs_review` with an unresolved round files NO
   confirmation question and DOES write the hold event; resolve the round, tick, and the
   question is filed.
6. **(d) the wo-7c7347e1 contrast.** The existing happy-path tests
   (`test_the_real_collector_still_reaches_the_question`,
   `test_running_the_confirmation_pass_twice_asks_once`,
   `test_a_confirmed_assumption_settles_exactly_as_an_ordinary_acceptance_does`) must pass
   byte-unchanged; add one explicit pin that a confirmation whose status never flips
   settles with exactly ONE `autoreview_asked` (`confirm: True`) event and no hold event of
   either new code.

### 10.2 `tests/test_autoreview.py` — the ask pass is untouched

7. `decide` and `decide_early` on an OPEN round still arm — the licence of §5.2, pinned so
   a later reader cannot "fix" the asymmetry by symmetry.
8. `decide`'s signature gains nothing: a test that `HELD_ROUND_OPEN` is unreachable from
   either ask function.

### 10.3 `tests/test_assumption_early_state.py` — the column

9. `clear_assumption_confirmation` sets the column back to NULL, `get_assumption` reads it
   back, and the row's `provisional_*` columns and `status` are untouched (the twin of the
   existing `link_assumption_confirmation` assertion at line 69-73).

### 10.4 `tests/test_invariants.py` — the attention half

10. `_os_is_confirming` / `true_blockers`: a pending row with `provisional_verdict='accept'`
    and a `queued` confirmation question suppresses the blocker; the SAME row with the
    question `escalated` does not, and `check_attention_reason_is_true` then repairs the
    order's reason to name the assumption. Plus the fail-toward-the-user case: an
    unreadable question id does not suppress.
11. `check_neo_escalations_are_live` after a transient drop: the superseded question is
    absent from `USER_HELD_Q_STATUSES`, so the invariant reports nothing and no row is
    stranded — the pair §4.3 exists to protect.

### 10.5 `tests/test_early_review.py`

12. Unchanged, and asserted so: the early pass files no confirmation and leaves
    `confirm_question_id is None` (existing assertion at line 316) — the regression guard
    that §4 never runs on the early path.
