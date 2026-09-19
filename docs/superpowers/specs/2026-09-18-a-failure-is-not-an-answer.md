# A failure is not an answer

**Status:** implemented (wo-3b2244d4)
**User ruling, 2026-09-18:** "if Neo crashed, then it shouldn't fallback to a default
decision, NEVER!. The escalation to Neo should be persisted and still be there to
whenever Neo can take it and properly answer it. Make sure no message (from Neo,
supervisor or anywhere) fallbacks to a default answer if there are problems."

## 1. The rule

A failure to reach a model, or to deliver a message, leaves the work **pending and
retryable**. It never produces a verdict, an answer, an approval, a rejection or a vote.

Two things are collapsed today that must never be:

* a model that **answered** and declined, abstained or escalated has made a judgement;
* a model that was **never reached** has made none.

`ClaudeCliError` is raised for both a launch failure and a refusal, so every rescue must
decide which it is looking at rather than assuming.

Retrying for ever is its own failure, so after bounded attempts the work does reach the
user — labelled **unreachable**, never **escalated**, and having delivered nothing on the
way.

### Retries need a backoff, or they are not retries

Every queue in the OS is drained on a daemon tick measured in seconds. A unit returned to
its queue with no delay is re-claimed on the next iteration, so a three-strike ladder is
spent in fifteen seconds against an outage measured in hours. That is GitHub issue #235,
which the validation panel already paid for once. Every release path here therefore
writes a `retry_after` alongside the attempt, and every claim query honours it.

A **usage-limit refusal** is the exception in the other direction: it states when it ends,
so it is held until that moment and spends no attempt at all.

## 2. Neo's queue — the case that produced the ruling

Question 388 reached the user as an escalation. Neo's `claude -p` call had failed with
rc=1, zero tokens in and out, `duration_api_ms` 0. The drain loop wrote `status='failed'`
at `attempts=0` — bypassing the retry machinery rather than exhausting it — and
synthesised `{"escalate": True, …}`, which it handed to the `deliver` hook.

`NeoStore.release_claim` replaces both. It is `reclaim_stale`'s decision taken at the
moment of the failure instead of fifteen minutes later, and the two agree on the
statuses, the ceiling and the give-up-before-requeue ordering.

`drain_queue` gains an `unreachable(question, detail)` hook, **separate from `deliver`**
because there is no verdict: nothing was decided, so there is nothing for `deliver`'s
per-kind branches to apply. It fires only once the retries are spent.

## 3. The panel — a seat nobody heard from

A seat that could not be reached abstains, contributes nothing to `arbitrate`, and the
chair is told it abstained. Silence never becomes a vote. But a `blast` or `record` seat
that was never reached also cannot exercise its **veto**, so the chair could pass what the
full panel would have blocked — the quorum shrinking silently.

Ruled by the user via Neo, question 436: **B for every kind.** If any seat in
`FORCES_ESCALATE` was never reached, there is no verdict, and `panel.decide` raises so
`drain_queue` re-queues the question.

Scoped to `abstained` — a transport fault. A seat with no definition in this build is
recorded `failed`, and is excluded: it was never reached and never will be, so treating it
as a fault would re-queue every question to exhaustion on a cadence.

## 4. The supervisor's alarm queue

`_transport_failure` returned `decision: "escalate"` and `_apply` wrote `status='failed'`
at `attempts=0`, out of the queue for good. `decision` is now empty — nothing decided it —
and `_apply` routes on a new `unreachable` key into `ProjectStore.release_alarm_claim`.

Its sibling `_failed_verdict` (a reply nobody could parse) is deliberately unchanged: the
model *was* reached, and that is a different bucket.

## 5. Dispatch

A work order whose first turn could not be launched was set to `failed` and flagged. A
blip in the transport became a terminal state on a work order nothing was wrong with.
`release_dispatch_claim` returns it to `pending` behind a backoff; only the ceiling is
terminal, and its attention reason says `claude` was unreachable and no turn ever ran.

## 6. Message delivery

`Daemon._deliver` marked every message `failed` on the first `ClaudeCliError`, so whatever
the user had just typed at a worker was silently discarded and nothing re-sent it — GitHub
issue 43's shape. `record_delivery_failure` holds the message `queued` behind a backoff
and only surfaces it, with the attention reason saying the worker never received it, once
the attempts are spent.

`deliverable_messages` is a separate reader rather than a filter inside `queued_messages`:
`invariants.stuck_message` must still see a held message, because that is precisely the
message it exists to notice.

## 7. What was already right, and is left alone

* `seats._run_seat` — a seat that fails abstains with `replied=False`; no vote.
* `Daemon._validation_outage` — retried, counted from the events so a daemon restart
  grants no fresh budget, escalating at three as "the validator was unreachable … Nobody
  has judged the work." **This is the exemplar the rest of the audit was measured against.**
* `Daemon._validation_held` — waits out a usage window, spends no attempt.
* `supervisor.review_health` — no finding, and the fingerprint is not recorded as
  reviewed, so the next tick looks again.
* `bus.deliver` — `attempts`, a ceiling, and INV-ENVELOPE-STUCK behind it.
* `digest` — records the failure to produce one. Cosmetic: it cannot influence a
  decision, and the rule is about what decides or delivers.
* `ops`' session listings — degrade to empty; read-only diagnostics.

## 8. Testing

`jarvis.testing.transport_faults` is the fault-injection harness: one named fault per
production shape — `rc1_empty` (the exact shape of question 388), `timeout`,
`empty_stdout`, `malformed_json`, `usage_limit`, `mid_stream_disconnect`. Every site in
this spec is driven through every applicable fault, asserting that nothing was decided,
nothing was delivered, the unit is still pending and retryable with `attempts`
incremented, a later successful call answers it properly, the user is asked for nothing
until the retries are spent, and the surfaced item then says **unreachable**.

`tests/test_transport_resilience.py` holds them, including the regression that reproduces
question 388 exactly, and the counterpart that a real escalation still escalates — so the
fix cannot pass by making the OS never escalate at all.
