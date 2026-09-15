# Neo decides an assumption, and says so on the record

*Design, 2026-09-15. Built in wo-26e52638. Sibling of
[2026-09-14-validated-auto-merge-design.md](2026-09-14-validated-auto-merge-design.md) and
deliberately built in its image: same flag shape, same pure-decision split, same
hold-with-a-reason, same record-first posture. Where the two differ, this document says
why.*

*Reading order: that spec (the mechanism this copies),
[2026-09-13-two-gates-not-a-chain.md](2026-09-13-two-gates-not-a-chain.md) (why the
assumption gate and the validation round are independent), `mem:work-order-lifecycle`.*

---

## 1. The problem

With `validation.enabled` and `validation.auto_merge` on, a work order already runs
worker → panel → gate → Neo → merged with nobody typing anything. Measured on 2026-09-14:
wo-45b377e6 was judged on 4afb5e7b, Neo approved the merge gate, and the OS merged PR #242
as 450be3f. Nobody touched it.

One stop is left, and it is a person by construction: a **pending assumption**.

* `jarvis wo review` is a decision the user owes.
* `jarvis wo ack` and `jarvis wo done` both REFUSE while one is outstanding.
* `ops.land_when_cleared` parks in `needs_review` while one is pending.
* `automerge.decide`'s condition 3 holds the merge on `pending_assumptions`.

So a work order that records one assumption waits for a human however green everything
else is. On this fleet almost every work order records at least one, and most of them are
"I used the branch name `feature/csv-export`" — bl-3eeab28a measured the shape: when the
worker contract was reworded around decision ownership, workers spontaneously stopped
recording 5 of 8 routine mechanical calls, because none of them is a decision in any
meaningful sense. That experiment was reverted, correctly: shrinking the audit trail is a
different question from routing decisions to Neo. **This is the other answer to the same
measurement** — keep recording all of them, and let Neo take the routine ones off the
user's desk.

## 2. What it must not do

Auto-accepting every assumption is not the feature; it is the failure mode. An assumption
is a worker saying "I had to decide something you did not specify, and here is what I
chose" — some are typography, some change the product. Four properties hold that line, and
every one of them fails toward the user.

### 2.1 The verdict space is ACCEPT or ESCALATE. There is no machine rejection.

Ruled by Neo, question 301. The alternative — mirroring `jarvis wo review --reject` — is
not "deciding an assumption" at all, it is **commissioning rework**: a rejection writes
guidance to a worker that finished long ago, restarting a turn on an order nobody asked to
reopen. That is the precise act `gates.apply_decision` fences `self_heal` and `auto_merge`
against, and it is a different authority from the one this feature is asking for.

So "this assumption is wrong" is something Neo cannot defend *accepting*, and it escalates
— carrying Neo's reading, so the user reads "Neo would have turned this down: …" and then
rejects it themselves with their own reasoning. **The user is strictly better informed than
they are today**, where an assumption reaches them with no reading on it at all.

The consequence is worth stating plainly: the user is still involved in every BAD
assumption. That is the correct division. The value being bought is removal from the
routine ones, which are the overwhelming majority.

### 2.2 Two independent nets catch a high-stakes assumption.

Neither relies on the other, because one is a regex that cannot read meaning and the other
is a model that cannot be relied on to volunteer its own doubt.

1. **`autoreview.HIGH_STAKES`**, matched against the assumption's text *before any model
   call*. Every entry is the code form of a clause `neo.PERSONA` already tells Neo to
   escalate on — production or live credentials, spending money, deleting or publishing
   anything, legal and personal-data matters — so it is that rule applied one layer
   earlier and **not a second vocabulary for it**. A match HOLDS: the assumption stays
   pending and the work order stays exactly where it is today, so a false positive costs
   the user precisely what every assumption costs them now.
2. **`stakes: high`**, which Neo must classify on every answer *separately from its
   verdict*, and which `autoreview.read_ruling` honours **over an acceptance**. A backstop
   a reviewer can wave through is not one — the same argument that makes the child cap
   override Neo on a plan (`Daemon._deliver_plan_verdict`).

### 2.3 One question per assumption, ever.

Never a batch verdict over a list; a list invites one judgement over its easiest member.
The idempotency is `assumptions.neo_question_id`, and it is checked as a *condition of
`decide`* rather than as a silent early return in `propose`, so "already asked" is a hold
with a reason like every other. A question Neo escalated is therefore never re-asked: the
user holds it, and asking again every reconcile tick would be the OS lobbying them.

### 2.4 The record says the OS decided it.

**This is the single most important post-condition of the feature.** Five columns on
`assumptions` (§4), one shared renderer (`ops.assumption_line`) behind both the CLI and the
dashboard, and no path that writes a verdict without an attribution.

## 3. What it deliberately does not touch

* **`wo ack` and `wo done` need no special case, and this was checked rather than
  assumed.** Both refuse on *pending* assumptions (`ops.mark_done` → `release.py`'s
  `"still has assumptions pending your review"`, and `ops.ack_attention`). Once auto-review
  settles one it is `accepted`, not pending, so both paths simply stop refusing — which is
  the correct behaviour, because the decision they were protecting has been made and
  recorded. No guard was widened, narrowed or added. `tests/test_autoreview.py` pins it
  from both sides.
* **`automerge.decide` condition 3 is NOT weakened.** It still holds on a genuinely pending
  assumption. Once auto-review clears them the condition passes on its own, which is the
  whole interaction and it needs no code. The redundancy between it and condition 2 stays
  for [two-gates-not-a-chain](2026-09-13-two-gates-not-a-chain.md)'s reason.
* **Feature-order plan approval (`jarvis fo approve`) and the privileged-action gates are
  out of scope.** They are separate authorities with their own reviewers. The assumption
  path shares no code with either: it has its own Neo question kind, its own persona, its
  own decision function, and it never touches the `approvals` table.

## 4. The state

| fact | where |
|---|---|
| may the OS decide this project's assumptions | `ValidationConfig.auto_review`, per project, ships `false` at both levels, covered by `SAFETY_KEYS`' `*.validation.*` |
| who decided one | `assumptions.decided_by` — `''`/`user` for the person, `neo` for the OS |
| why | `assumptions.decided_reason` |
| which model reached it | `assumptions.decided_model`, from the transport's own report (`neo.answer_question` now returns `model`) |
| under which configuration | `assumptions.decided_config_version` |
| the deliberation | `assumptions.neo_question_id` → `jarvis neo show <qid>` |
| what the mechanism did last | `autoreview_asked` / `_accepted` / `_escalated` / `_held` events → `ops.autoreview_state` → one line on `jarvis wo show` and the work-order page |

`''` in `decided_by` means the user and not "unknown": before this shipped, the user was
the only thing that could write a verdict into that row. `ops.review_work_order` stamps
`user` explicitly going forward all the same — asserting the claim is what makes "not the
user" provable on the other one.

## 5. The decision, and where it is split

`autoreview.decide(assumption, wo, cfg, *, round_outcome, refusal_answered)` is **pure** —
dicts in, armed-or-held-with-a-reason out — for `automerge.decide`'s reason. Seven
conditions, all of which must hold:

1. `cfg.enabled and cfg.auto_review`;
2. the work order is parked in `needs_review`;
3. the assumption is still pending;
4. the validation panel has not GIVEN UP on this work order;
5. no refusal of the user's is outstanding (`ops.refusal_answered`);
6. it has not already been asked about;
7. `high_stakes_marker` finds nothing in its text.

Two of those are not in the obvious list and are the ones worth reading.

**Condition 2 — `needs_review`, the analogue of auto-merge's `waiting_pr_merge`.** An
assumption recorded mid-run is not judged, because the work it was part of does not exist
yet: a reviewer would be ruling on an intention, with no result summary and no diff. It is
also the only status that means the user owes a decision, so it is the only one where
taking that decision off them is worth anything.

**Condition 4 — the panel's give-up.** `ops.land_when_cleared` LANDS an `escalated` round;
its docstring says the only caller that can reach it with one is `review_work_order`, i.e.
the user saying ship it anyway. So clearing the assumptions under a give-up would have the
OS silently answer a *different* question — one the panel deliberately put in front of a
person. Nothing in the work order's brief anticipated this; it fell out of reading the
landing path, and it is the condition most likely to be dropped by a future refactor.

The second half is `read_ruling(verdict)`, also pure, and **acceptance is the narrow
path**: it needs two positive facts (Neo did not escalate, AND it ruled `approve`), so an
unparseable reply, a transport failure, a missing field or a model answering some other
question all land with the user by structure rather than by an exception handler someone
remembered to write.

## 6. Failure directions — every one ends in "the user decides it, as they do today"

| what breaks | what happens |
|---|---|
| the daemon is down | nothing asks. The assumption stays pending. |
| `os.neo.enabled` is off | `Daemon.auto_review` returns before asking: a question filed against a queue that does not drain is worse than no question. |
| `validation.enabled` goes off mid-flight | condition 1 re-reads both flags every tick; the pass stops fleet-wide. |
| Neo's call fails | `drain_queue` marks the question `failed` and delivers `escalate: True`; `read_ruling` requires an explicit approval, so nothing is accepted. |
| Neo's reply will not parse | `neo._unparseable_verdict` escalates. Same path. |
| Neo answers `approve` but marks `stakes: high` | `read_ruling` overrides it, the question is re-marked `escalated`, and the user decides. |
| Neo answers `deny` without escalating | escalates, carrying Neo's reason. |
| the user reviews it first | the delivery arm sees a non-pending assumption and drops the ruling; `invariants.check_neo_escalations_are_live` closes the question behind them. |
| the work order is deleted | `NeoStore.purge_work_order` takes its questions with it. |
| the panel gave up on the work order | condition 4 holds, every tick, with a reason on the record. |
| the user rejected an earlier assumption and the worker has not delivered again | condition 5 holds. |

There is no row where a failure produces an acceptance. That falls out of the structure —
an acceptance needs seven positive facts and then two more — rather than out of a rescue
clause.

## 7. Telling the user afterwards

**One inbox row, at `info`, when the OS settles the LAST pending assumption of a work
order.** Not per assumption, and not on an escalation. The argument:

* On an **escalation**, nothing changed for the user. The work order was on their attention
  list carrying "assumptions pending review" before the OS asked and it still is. A second
  announcement of an unchanged fact is the attention cost this feature exists to reduce —
  `_note_automerge_held`'s "the attention list is not a place to put ordinary".
* On an **acceptance**, something did change, and it would otherwise be invisible: the work
  order silently leaves their review list and (with auto-merge on) completes. This is a
  class of decision they used to make themselves, and the first time it happens they should
  find out from the OS rather than from a merged pull request.
* **Per work order, not per assumption**, because what changed for the user is the work
  order leaving their list — three rows for three assumptions would be three
  announcements of one event.
* The row names the correction path, which is the other half of making this reversible:
  `jarvis neo review <qid> --correct "…"` teaches Neo, and `jarvis neo retract` retires a
  learning that came out wrong. Neither needs a code change, and both already work for
  this kind because they key on the question and not on what it was about.

**No learning is recorded when the OS accepts.** `review_work_order` distils the USER's
feedback into a Neo learning, which is the point of asking for it; doing the same from
Neo's own output would be a ledger citing itself, which is how one early mistake becomes a
standing rule.

## 8. Cost

The pass runs on the reconcile cadence, not every tick — what it saves is measured in the
user's hours, not in seconds. An opted-out project pays **one attribute read** per project
per reconcile. An opted-in project pays one indexed listing of `needs_review` orders, and
then, per such order, one validation-round read and one refusal-history read (both read
ONCE per order and reused across its assumptions, so the validator running on another
thread cannot change the answer half way down a list meant to be judged against one state).

A work order with three assumptions costs three Neo calls, drained back-to-back and so
sharing a warm prompt prefix. They are not panel calls: `assumption` is not in
`DEFAULT_PANEL_KINDS`, so the single agent answers unless the user opts the kind in.
