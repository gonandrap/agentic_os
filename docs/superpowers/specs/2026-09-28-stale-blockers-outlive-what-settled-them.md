# Stale blockers outlive what settled them

Issue #834, work order wo-35fc3de7. Third of the class after #786 and #813
(kn-7d54230d). Invariants from kn-681db233 apply, point 3 in particular.

## 1. The problem

Five independent defects, one shape: **a blocker the OS shows the user is derived from a
row that stopped being a current fact.** The derivation is re-run every reconcile tick, so
each keeps re-asserting itself; none can be acked away for good, because
`invariants.true_blockers` re-derives the same string the next tick.

The class is not "stale writes". Every one of these is a **read predicate that names the
wrong row**: it asks a question whose answer was true once, instead of asking the question
whose answer cannot go stale. Fixing the five sites without fixing the predicate shape
buys a sixth.

### 1a. An escalated `kind='question'` outlives the review that settled it

`invariants.check_neo_escalations_are_live` (invariants.py:2079) filters
`USER_HELD_Q_STATUSES` questions to `kind in (approval|plan|alarm|triage|assumption)`
(:2114-2116) and dispatches to five `_stale_*` handlers (:2134-2138). `kind='question'` —
the worker's own `jarvis wo ask` — is not in the set and has **no subject pointer at
all**: nothing in any store points back at it, so no handler could be written the way the
other five are (each reads its subject's current state).

Live evidence, wo-4beada49: Neo question 826, `kind='question'`, `status='escalated'`.
`ops.waiting_on` (ops.py:1119) reaches `awaiting_neo` at :1172, sees a USER-held status,
and returns `{"what": "neo_escalated"}` with `neo_question_blocker(question)` as the
detail — which becomes the order's attention reason. The user had already reviewed the
assumptions the question was about (#2, #3 accepted). Nothing can retire 826: Neo will not
re-answer an escalated row, and the user has no reason to answer a question they have
already answered another way.

`_stale_assumption_question` (invariants.py:2213) is the shape this is missing, and the
reason it cannot be copied: it reaches its subject through
`store.assumption_for_question(q["id"])`, and a `kind='question'` row has no such link.

Ranking is wrong too, independently of staleness. In `waiting_on`, `neo_escalated` (:1172)
sits **above** the `needs_review` status arm (:1249). An order in `needs_review` with an
open escalated question reports the question, not the review — the reverse of what the
user owes.

### 1b. `STALE_FINISH_BLOCKER` fires on any turn the allowlist has not learned

`invariants.parked_reason` (invariants.py:947) returns `STALE_FINISH_BLOCKER` when the
last `finished` event predates the last turn's `started_at` (:1006-1011), excused only by
`store.turn_opened_by(turn) in PR_REPAIR_SOURCES`.

Live evidence, wo-3312682f: turn 7 was a reply about a **gate dismissal** — not a PR
repair, so not in the set. The pull request head equals the commit the passing round
judged. Nothing is unfinished; the OS told the user to go and read a turn and
`jarvis wo send` a worker that has delivered everything it owes.

Root cause is the excuse's shape: `PR_REPAIR_SOURCES` is an allowlist of **why a turn was
opened**, and it is being used to answer **whether anything is undelivered**. Every new way
the OS itself opens a turn — gate replies, message delivery, remedies, anything added next
quarter — is a new false blocker, and the allowlist can only be extended after the false
blocker has been shipped and seen.

### 1c. An autoreview hold on a refusal that the worker answered with commits, not a finish

`ops.refusal_answered` (ops.py:4022) reads `finished` events only: a refusal is answered
iff `events_of_kind(wo_id, "finished")[-1].ts > refusals[-1].ts` (:4038-4039).

Live evidence, wo-dbea82cf: after the user's refusal the worker pushed 3 commits (head
`12d3d0d`) and never ran `jarvis wo finish`. So `refusal_answered` is False for ever, and
`autoreview.decide`/`decide_early` return `HELD_REFUSAL_UNANSWERED` — "you refused an
assumption on this work order and the worker has not delivered again since"
(autoreview.py:851-854, :1013-1016) — on every tick. `ops.land_when_cleared` re-parks it
in `needs_review` at ops.py:4001-4007. The order is parked on the user, and the sentence
the user reads is false: the worker HAS worked since; it just has not declared it.

The symptom is the hold. The root cause is that **the OS has no detector for an
undeclared delivery** — commits exist on the head with no `finished` — so nothing
self-heals it, which the pinned ruling requires.

### 1d1. A synthesised transport hold is never closed by the round that ended it

`holds._episodes` (holds.py:222) synthesises a `PAUSE_USAGE_LIMIT` hold from a
`validation_failed` event whose payload `cause == VALIDATION_HELD_CAUSE` (:252-254), and
the `PAUSE_AUTH` twin from `VALIDATION_AUTH_CAUSE` (:256-259). Both are closed **only** by
`validation_submitted`/`validation_forced` (:261-266). The round's own terminal events —
`validation_passed`, `validation_rejected`, `validation_escalated`, `validation_void`,
`validation_failed` — close only the keyed `VALIDATION` hold (`_CLOSE`, holds.py:138-142);
they do not close the unkeyed synthesised one.

Live evidence, wo-3615faf7: repeated `validation_failed`, last at ts 1790565181.02, then
`validation_passed` at 1790568634.82 and no further submission. The hold reads open 11.7h
later instead of 57m. wo-dbea82cf: 9.8h the same way.

A **second defect in the same three lines**: the synthesis writes
`open_holds[(PAUSE_USAGE_LIMIT, None)] = Hold(...)` unconditionally, so a repeat
`validation_failed(held)` **overwrites the open hold object without appending it to
`done`**. Every intermediate held episode is deleted from the record, so held time is
under-reported at the same time as the last episode runs for ever. Both halves have the
same fix.

Consequence beyond the "(still held)" line: `parked_reason` subtracts hold seconds from
the silence before it decides anything (invariants.py:993-1000), so an eternally open hold
**suppresses** genuine park blockers on the same order. This defect hides blockers as well
as inventing one.

### 1d2. `state_durations` reports a status the order is not in

`ops.state_durations` (ops.py:1643) derives `current_status` purely from
`store.state_spans(order_id)` (:1663, :1678-1680) and **never compares it with
`work_orders.status`**.

Live evidence, wo-3615faf7: `current_status='waiting_input'` (20.7h, trigger
`question_asked`) while `work_orders.status` is `'needs_review'`. `wo_state_spans` holds 3
rows (`pending`, `running`, `waiting_input`), the last at ts 1790532941.09; a `status`
timeline event exists at 1790568634.82.

The proven chain, against the code:

* `ProjectStore._record_span` (project_store.py:2355) is the only writer of
  `wo_state_spans`, and `set_status` (:2332) its only live caller.
* `add_event(wo_id, "status", …)` has **exactly one writer**: `set_status`, line 2347,
  one line after `_record_span` at :2346, with no branch between them. Verified by
  search — no other site in `src/jarvis` writes a `kind='status'` event. **So no path
  other than `set_status` can have written the event at 1790568634.82**, and a running
  build cannot produce a `status` event without the span beside it.
* Three sites bypass `set_status` with raw SQL: project_store.py:2148
  (`status='failed'`), :2155 (`status='pending'`), :2250 (`status='dispatching'`). Each
  writes neither a span nor a `status` event. Verified exhaustive: no
  `update_work_order(status=…)` caller exists outside `set_status`.
* Therefore the brief's first candidate — a bypass leaving the column at the target so a
  later `set_status` hits `if was == status: return` (:2344) — produces a gap with **no
  `status` event**. That is a real and reachable defect, and it is the shape to expect
  going forward, but **it is not what produced wo-3615faf7's evidence**.
* What fits the evidence: the `status` event at 1790568634.82 was written by a build
  whose `set_status` did not yet record spans — `wo_state_spans` shipped in
  docs/superpowers/specs/2026-09-27-time-in-each-state.md, one day before this order ran
  — by the **long-lived daemon process**, which keeps running the code it was started
  with across an upgrade, while a new-build CLI invocation constructed a `ProjectStore`
  in between and ran `_backfill_wo_spans` once. The backfill's three rows are exactly the
  status events up to 1790532941.09, which is why the last span ts equals the last
  pre-upgrade status event.
* And it never heals, which is the durable half: `_backfill_wo_spans`
  (project_store.py:1542) is **all-or-nothing per order**. It returns early when
  `covered >= total` (:1557) and skips any id already in `have` (:1559-1565). An order
  with a partial history is in `have` for ever, so its gap can never be filled by
  anything.

Two defects, then: spans can be missed (bypasses; and any writer outside this build), and
a missed span is permanent. On top of both, `state_durations` presents the stale open span
as present-tense truth rather than detecting the disagreement.

## 2. The fix

Common rule for every new derived string below, from kn-681db233 point 3: **invariant
under the clock and under the commit**. These become `attention_reason` and
`ProjectStore.ack_attention` stores them verbatim while INV-ATTENTION-REASON compares
them, so a sentence carrying elapsed time or a sha can never be acknowledged. No new
sentence in this spec carries either.

### 2a. A sixth `_stale_*` handler, plus a re-rank

**Handler.** Add `_stale_worker_question(store, q)` beside `_stale_assumption_question`
(invariants.py, after :2230), and add `"question"` to the kind filter at :2114-2116 and to
the dispatch dict at :2134-2138. Returns `(answer, why)` — `SUPERSEDE, NEVER DELETE`, via
the existing `neo.supersede` at :2144, which is already guarded on `OPEN_Q_STATUSES`
(neo_store.py:409) so a real verdict is never overwritten.

**The predicate, and why these rows.** Per Neo's ruling, a finish alone does not make the
question moot; only the user's review verdict on a delivery newer than the question does.
Read exactly two things, both from the project store's timeline:

```
moot  iff  exists r in events_of_kind(wo_id, "reviewed") with r.ts > q["ts"]
      and  exists f in events_of_kind(wo_id, "finished") with q["ts"] < f.ts < r.ts
```

* `q["ts"]` is the question's filing time (`questions.ts`, neo_store.py:140).
* The `reviewed` event **is the user's verdict and nothing else**. Verified: written at
  exactly one site, `ops.review_work_order` (ops.py:6409), whose only non-test callers are
  `cli.py:2769` and `ui/app.py:1539` — the CLI and the dashboard form, both the person.
  The autoreview path settles assumptions through `store.review_assumption(...,
  decided_by=…)` and writes **no** `reviewed` event, so a machine verdict can never
  supersede a question here. That is why this row, and not `assumptions.status`: an
  assumption row's status cannot say who decided it without a second read, and a
  `decided_by` column can be written by the OS.
* The `finished` between them is the DELIVERY the verdict was about. Without it, a review
  of an older delivery would retire a question the worker asked afterwards.
* Both are append-only timeline rows with immutable `ts`. Nothing rewrites them, so the
  predicate cannot go stale in either direction — the property the five existing handlers
  get from their subject's status column and this one cannot.
* A `wo_id` this project does not know, or a work order with no events: return `None`,
  leaving it alone. Same rule as every sibling (the checks run per project against an
  OS-wide `neo.db`).

Answer/why strings, free of clock and commit:
`SUPERSEDED — the user reviewed a delivery made after this question was asked` /
`the user reviewed work order <id> after it was asked, on a delivery newer than the question`.

**Rank.** In `ops.waiting_on`, an escalated `kind='question'` must rank **below** the
`needs_review` blocker. Do not reorder the whole `neo_escalated` arm — that would demote
`approval`/`alarm` escalations, which genuinely outrank a park. Instead, inside the
`question["status"] in USER_HELD_Q_STATUSES` branch (ops.py:1172-1175), when
`q["kind"] == "question"` **and** `wo["status"] == "needs_review"`, fall through to the
status arm at :1249 instead of returning. `invariants.true_blockers` already lists the
`needs_review` reasons first (invariants.py:811+), so the two agree by construction after
this, which is what `waiting_on`'s docstring promises.

### 2b. Judged-head equality replaces the allowlist as the general excuse

Establish the general predicate the brief asks for: **the stale-finish blocker is false
when the head the OS has already judged or delivered equals the current head.**

**Record the head locally, free.** `Daemon.poll_pull_requests` already reads `pr.head_oid`
on every poll of every parked order and throws it away unless something notable happened
(it survives only inside `automerge_held`/`automerge_failed`/`pr_merged`/base-update
payloads — daemon.py:5035, :6347, :5257). Add two columns on `work_orders` —
`pr_head_oid`, `pr_head_seen_at` — written by that same poll through `update_work_order`.
**No new network call, and no new timeline event per tick**: a per-tick event would grow
the timeline without bound and would be read as history rather than as a cache.

**The excuse, read locally in `parked_reason`.** At invariants.py:1006-1011, before
returning `STALE_FINISH_BLOCKER`:

```
judged  = { ProjectStore.validated_head(store.latest_validation_round(wo_id=…)),
            head_oid of the newest `pr_merged` event }  minus {None, ""}
current = wo["pr_head_oid"]
excuse iff current and judged and current in judged
```

`ProjectStore.validated_head` (project_store.py:4474) is the existing, documented
predicate and already prefers `carried_head_sha` over `head_sha`; reuse it rather than
reading the columns here (its docstring states the one-home rule).
`invariants.rejudge_exhausted` (:520-552) is the precedent: the same local
judged-vs-observed-head comparison, no network.

**When no commit is recorded, the blocker STANDS.** Pre-0.10.0 rounds recorded none
(kn-48dadcce; `jarvis validation force` exists for exactly that), and an order polled
before this ships has an empty `pr_head_oid`. Both make `excuse` false by the `and`s
above, so the check degrades to today's behaviour. Empty must never be read as equal —
`github.py:540-543` already makes that point about `head_oid`.

**`PR_REPAIR_SOURCES` stays, and this is a correction to the brief.** It cannot be
replaced by head equality: a conflict or checks repair **pushes**, so after it the head is
*not* the judged commit and head equality is correctly false, while the repair's own
instruction tells the worker not to finish. The two excuses answer different questions.
What changes is that the allowlist stops being the *only* excuse, so it stops having to
grow for every new OS-opened turn — which is the growth the brief objects to.

### 2c. `refusal_answered` stays finish-only; a detector nudges for the finish

Per Neo's ruling, **do not touch `ops.refusal_answered`** (ops.py:4022). The finish IS the
declaration; widening it to "commits exist" would let the panel judge a submission nobody
declared, and would make `wo finish`'s `--summary`/`--evidence` optional in practice.

**Detector.** New check in the reconcile path, beside the other per-order derivations, in
`invariants.py`, named `undeclared_delivery(store, wo)`. True when all of:

1. `store.events_of_kind(wo_id, "reviewed")` has a newest refusal (`accepted` false in the
   payload, same read as `refusal_answered` at ops.py:4034-4035);
2. no `finished` event after it (i.e. `ops.refusal_answered` is False — call it, do not
   re-derive it);
3. `wo["pr_head_oid"]` (2b's column) is non-empty and differs from the head recorded on
   the newest `finished` event's `pr_url` delivery — concretely, differs from
   `ProjectStore.validated_head(latest round)` and from the head the last delivery was
   judged at. Head movement after the refusal with no finish is the observation.

Condition 3 reuses 2b's column, so **this costs no network read of its own**; 2b is a
prerequisite of 2c and they ship together.

**Remedy.** The detector does not act. It raises a finding — `store.add_finding(wo_id,
kind="undeclared_delivery", source="invariant", …)` (project_store.py:3048; `add_alarm` is
the cost-alarm case and is fenced by `check_burning_turns`' dedupe, so do not reuse it).
The supervisor's existing judge path then reaches `remedies.propose(...,
remedy_id="nudge", ...)` (remedies.py:400), which files the `self_heal` approval plus a
`kind="approval"` Neo question, and `remedies.apply` runs the nudge only against a live
grant. Nothing new acts on a work order: the closed registry
(`REMEDIES`/`SHIPPED_REMEDIES`) is untouched, and `tests/test_remedies.py`'s AST pin
keeping every acting call inside a handler still holds.

**What the user sees while it is pending.** The hold sentence
`HELD_REFUSAL_UNANSWERED` (autoreview.py:852-854) is today's and is false in this case.
Give the hold a second reason, selected when `undeclared_delivery` is true:

> you refused an assumption on this work order, and the worker has pushed commits since
> without running `jarvis wo finish` — the OS has asked it to declare them

No count, no sha, no elapsed time: the number of commits and the head both move, and this
string is stored verbatim by `ack_attention`.

### 2d1. Every terminal event of a held round closes the synthesised hold

In `holds._episodes` (holds.py:252-266), two changes:

1. **Close on any round terminal.** The set is the `VALIDATION` closers already in
   `_CLOSE` — `validation_passed`, `validation_rejected`, `validation_escalated`,
   `validation_void`, `validation_failed` (holds.py:138-142) — plus the existing
   `validation_submitted`/`validation_forced`. All five terminals can follow a held round:
   the next round settles any of the four non-failed ways, and `validation_failed` is the
   repeat-hold case. Closers already run **before** openers on the same event
   (holds.py:229-247, and its docstring says why), so a `validation_failed(held)` closes
   the previous synthesised hold and opens a fresh one in the right order.
2. **Never overwrite an open synthesised hold.** Before assigning
   `open_holds[(cause, None)]`, pop any existing one and append it to `done`. This is what
   change 1 gives for free on the `validation_failed` path, but state it explicitly: the
   unkeyed slot must never lose an episode silently.

**The `PAUSE_AUTH` synthesis (`VALIDATION_AUTH_CAUSE`, holds.py:256-259) has the identical
hole** — verified: it is popped only in the same :261-266 block. Fix both in one table;
they differ by a constant.

### 2d2. Every status write records its span; `state_durations` refuses to disagree

**The fix** is at the writer: no status write without a span.

* project_store.py:2148 (`failed`) and :2155 (`pending`) go through `set_status` with
  their extra columns passed as `**extra` (`set_status` already forwards them to
  `update_work_order`, :2343) and a `trigger` naming the dispatch retry.
* :2250 is a conditional `UPDATE … WHERE id = (SELECT …)` — the atomic claim, and it must
  stay one statement. Do not route it through `set_status`; have `claim_next_pending`
  call `_record_span(id, "wo", "pending", "dispatching", trigger="claim")` immediately
  after a claim succeeds (`cur.rowcount == 1`), in the same transaction.
* Make `_record_span` the enforced chokepoint: assert in `set_status` that a transition
  writes both rows, and add an invariant check that every work order whose
  `wo_state_spans` tail `to_status` disagrees with `work_orders.status` is repaired by
  appending an `approximate=1` span at the order's `updated_at`. That is also what heals
  the rows already broken, which `_backfill_wo_spans` structurally cannot: relax its
  per-order all-or-nothing guard (project_store.py:1557, :1559-1565) to fill **gaps** —
  for each order, replay `status` events newer than its newest span — instead of skipping
  any order that has one.

**The belt** is at the reader: `ops.state_durations` (ops.py:1643) takes the order's
`status` column (it already holds the row for `fo`; take it for `wo` too) and, when the
open span's `to_status` disagrees, must not present the span as `current_status`. Report
the column's status, `current_status_since=None`, and add a note —
`the recorded spans do not reach this order's current status` — with no elapsed time in
it. A reader that silently believes a gap is why nobody noticed for 20.7h.

Fix at the writer, belt at the reader: the writer is the one that makes the data right;
the belt is what stops the next missed write becoming a false present-tense claim.

## 3. Surfaces that render these blockers

Change each derivation in its one home; these are the readers that must be checked, not
edited in parallel.

| Derivation | Readers |
|---|---|
| `ops.waiting_on` (2a rank) | `jarvis wo resume-auto`, `invariants.parked_reason` (:1002-1004 via `SPOKEN_FOR_WAITS`), `jarvis wo why`, dashboard `work_order.html` |
| `invariants.parked_reason` / `STALE_FINISH_BLOCKER` (2b) | `invariants.true_blockers` -> `attention_reason` -> `jarvis status`, `jarvis wo list/show`, notifications, dashboard |
| `invariants.true_blockers` (2a, 2b, 2c) | INV-ATTENTION-MISSING / INV-ATTENTION-REASON, `ack_attention`'s stored string, `jarvis doctor` |
| autoreview hold reason (2c) | `jarvis wo show` per assumption, `/neo`, the dashboard work-order page |
| `holds.held` (2d1) | `jarvis inspect`, `jarvis alarms`, `parked_reason`'s hold subtraction, `_diagnose_holds` (ops.py:1753) -> `jarvis wo why` (cli.py ~2733-2980), dashboard `_debug_diagnosis.html` |
| `ops.state_durations` (2d2) | `jarvis wo why`, `jarvis fo show`, dashboard `work_order.html` and `_debug_diagnosis.html` |

`jarvis wo why` reads `_diagnose_clock` (ops.py:1728), `_diagnose_holds` (:1753),
`_diagnose_os_calls` (:1786) and `_diagnose_commands` (:1831); 2d1 and 2d2 both land under
it, so the `wo why` output is the single place to re-read after this ships.

## 4. Tests — one per case

| Case | File | What it pins |
|---|---|---|
| 2a | `tests/test_stale_escalations.py` | an escalated `kind='question'`, then a `finished` and a user `ops.review_work_order` after it: the question supersedes and the attention reason stops naming it. A finish WITHOUT a review must leave it open (Neo's ruling, the half most likely to be lost). Ranking half in `tests/test_waiting_on_neo.py`: `needs_review` + open escalated worker question reports the review. |
| 2b | `tests/test_parked_work.py` | a `finished`, then a turn opened by a gate-dismissal reply, with `pr_head_oid` equal to the passing round's judged commit: no `STALE_FINISH_BLOCKER`. Second assertion, same test file: with no commit recorded on the round, the blocker still fires. |
| 2c | `tests/test_autoreview.py` | refusal, then head movement with no `finished`: `ops.refusal_answered` stays False (pinned deliberately), the finding is raised, the nudge is proposed through `remedies.propose`, and the hold sentence names the pushed-without-finishing case. |
| 2d1 | `tests/test_active_time.py` | `validation_failed(held)` x2 then `validation_passed`: two closed holds, none open, total equal to the two gaps. Mirror for `VALIDATION_AUTH_CAUSE` — `tests/test_validation_auth_hold.py` has the fixtures. |
| 2d2 | `tests/test_time_in_state.py` | a claim through `claim_next_pending` and a dispatch-retry `failed` both leave spans; and a store whose spans deliberately stop short reports the column's status with the disagreement note, not the stale span. |

`tests/test_invariants.py` for the new `undeclared_delivery` predicate in isolation.
`tests/test_wo_why.py` and `tests/test_ui_debug.py` for the two rendering surfaces.

## 5. Rejected alternatives

* **(a) Close the worker question on the finish.** The obvious fix, and Neo ruled against
  it: the worker asks, delivers, and the question is still the user's to settle — a finish
  is the worker's act, not the user's answer.
* **(a) Add a `subject_id` column to `questions` so `kind='question'` gets a pointer.**
  Schema churn for a link that has no subject to point at; the timeline already carries
  the fact.
* **(b) Extend `PR_REPAIR_SOURCES` with the gate-reply source.** Fixes wo-3312682f and
  nothing else; the next OS-opened turn kind ships the same false blocker again.
* **(b) Ask GitHub for the head inside `parked_reason`.** It runs for every work order on
  every reconcile tick; the function's own docstring makes the cost ordering load-bearing.
  The poll already has the answer for free.
* **(c) Widen `refusal_answered` to "commits exist since the refusal".** Neo's ruling: it
  would let the panel judge an undeclared submission.
* **(c) Raise the undeclared delivery to the user.** The pinned ruling is that the OS
  self-heals without a user where it can; a nudge is non-destructive and already gated.
* **(d1) Close the synthesised hold on a wall-clock timeout.** A clock-derived close makes
  the rendered figure disagree between two reads of an unchanged record.
* **(d2) Fix only the reader.** The spans are the record `jarvis inspect` and the
  dashboard both bill against; a reader-only fix leaves that record permanently wrong.
* **(d2) Fix only the writer.** Does not repair the rows already broken, and offers no
  defence against the next writer added.

## 6. Not covered, and what to verify first

* **Not covered:** any general mechanism preventing a sixth instance of this class.
  Each fix here names the row that cannot go stale, but nothing enforces that a new
  derived blocker does so. A lint or invariant over blocker derivations is the follow-up,
  not this order.
* **Not covered:** the daemon-survives-an-upgrade behaviour behind 1d2's chain. A
  long-lived daemon running pre-upgrade code while the CLI runs post-upgrade code is a
  general hazard; here it only has to stop corrupting spans.
* **Verify before implementing 2b:** that wo-3312682f actually has a locally recorded
  head to compare (an `automerge_held`/`automerge_failed`/`pr_merged` payload, or the new
  column once the poll has run). If it has none, the fix is correct but that order stays
  blocked until the next poll writes `pr_head_oid` — acceptable, and it is the safe side
  by design.
* **Verify before implementing 2d2:** query wo-3615faf7's `wo_events` for
  `kind='status'` against `wo_state_spans`. The chain in 1d2 predicts a `status` event at
  1790568634.82 with no span at any ts after 1790532941.09, and predicts that every span
  row's ts matches a `status` event ts exactly (the backfill signature). If instead a span
  row exists at 1790568634.82, the defect is in `state_spans`/`state_durations`' read and
  not in the writer, and 2d2's fix halves swap priority.
