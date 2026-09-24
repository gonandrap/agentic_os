# An assumption is judged while the worker is still typing

*Design, 2026-09-23. Feature order `fo-9bf2bf44`. Sibling of, and a deliberate REVERSAL
of one ruling in,
[2026-09-15-neo-decides-an-assumption.md](2026-09-15-neo-decides-an-assumption.md).*

*Reading order: that spec (the mechanism this extends — its §2 safety line is not
reopened here and every clause of it still holds),
[2026-09-13-two-gates-not-a-chain.md](2026-09-13-two-gates-not-a-chain.md),
[2026-09-16-an-idle-manager-is-not-waiting-on-you.md](2026-09-16-an-idle-manager-is-not-waiting-on-you.md)
(the shape of the attention fix in §9), `mem:work-order-lifecycle`, `mem:message-bus`.*

---

## 1. The problem

The sooner a worker learns that an assumption it made is wrong, the less it builds on it.

Today it learns at the earliest possible moment that is also the latest useful one:
never, during the run. `autoreview.decide`'s condition 2 requires the work order to be
parked in `needs_review`, and `Daemon.auto_review` lists `needs_review` and nothing else.
So every assumption a worker records mid-turn sits unjudged until the worker has
finished — and Neo's verdict space is ACCEPT or ESCALATE, so even then a disagreement
goes to the user and never reaches the worker at all.

Measured on wo-edf5c425, 2026-09-23: three assumptions recorded at ~13:15Z sat unjudged
for ~25 minutes while the worker ran the full test suite in the same turn. Had any one of
them been wrong, the worker would have built on it for the whole of that.

This feature judges an assumption when it is RECORDED, and sends Neo's disagreement to
the worker as guidance while it can still act on it.

### 1.1 This reverses a ruling that is currently in print

`src/jarvis/autoreview.py`'s condition 2 does not merely omit the running case, it
ARGUES against it: *"An assumption recorded mid-run is not judged, because the work it was
part of does not exist yet: a reviewer would be ruling on an intention, with no result
summary and no diff."*

That argument is correct and it is not overturned. It is answered: a mid-run verdict is
**provisional** and settles nothing. The reviewer really is ruling on an intention, so the
ruling buys exactly one thing — early guidance to the worker — and is re-confirmed against
the diff before it is ever allowed to settle the assumption (§7). What the old spec ruled
out was *deciding* on an intention. Deciding still happens at `needs_review`, unchanged.

`ASSUMPTION_REVIEWER_PERSONA` carries the same obsolete claim in the other direction — it
tells Neo that the worker has finished and that nothing it says reaches it. Under this
feature that is false, and a reviewer briefed with a false premise writes a verdict for
the wrong audience.

## 2. What it must not do

Every clause of the parent spec's §2 holds unchanged and is not reopened:

* **The verdict space for a SETTLEMENT is still ACCEPT or ESCALATE.** There is no machine
  rejection of an assumption. What this feature adds is not a rejection — it is
  **guidance sent to a worker that is still running**, which is the precise thing the
  parent spec said a rejection could not be (*"commissioning rework: it writes guidance
  to a worker that finished long ago and restarts a turn on an order nobody asked to
  reopen"*). A running worker has asked for nothing to be reopened; it is mid-task, and
  telling it something mid-task starts no turn it was not already taking. That distinction
  is the whole licence for this feature, and it evaporates the moment the order is not
  running — so an objection is never filed against a settled order, **and an objection
  already filed but not yet delivered is WITHDRAWN when the order leaves `running`
  (§6.6) rather than delivered late.** A queued objection that arrives after the worker
  stopped would start a turn on an order nobody asked to reopen, which is the exact thing
  the parent spec refused. There is no third option: an undelivered objection is either
  delivered while the order runs or withdrawn, and one of the two is always recorded.
* **Both high-stakes nets stay armed, in both passes.** `autoreview.HIGH_STAKES` matched
  against the text before any model call, and `ROUTINE_STAKES` read as an ALLOWLIST on
  the reply. A high-stakes assumption is never judged early, never objected to, and never
  quoted into a sibling list (`autoreview.sibling_line`).
* **One Neo question per assumption per PASS.** Two passes now exist and each may ask once:
  `assumptions.neo_question_id` for the early pass, `assumptions.confirm_question_id` for
  the confirmation pass (§4). Neither pass may ask twice.
* **Fail closed.** Anything unparseable, any transport failure, any model that was never
  reached: the assumption stays pending, nothing is objected to, and the user decides as
  they do today. A failure is never an answer — see the pinned fleet learning and
  [2026-09-18-a-failure-is-not-an-answer.md](2026-09-18-a-failure-is-not-an-answer.md).
* **The record says the OS did it.** Every provisional verdict, every objection and every
  confirmation is attributed, with the model and the config version, through the one
  renderer (§4, §8).

And one new rule, which is the axis the whole feature turns on:

> **A PROVISIONAL VERDICT SETTLES NOTHING.** It does not change `assumptions.status`, it
> does not call `ops.accept_assumption`, it does not reach `ops.land_when_cleared`, and it
> does not clear a single condition that `automerge.decide` or `ops.mark_done` reads. An
> assumption with a provisional approval on it is, to every existing caller in the
> codebase, byte-for-byte a pending assumption.

## 3. The spike: can a message reach a headless worker mid-turn

**This section is a research task and its deliverable is evidence, not a yes.** It decides
the transport §6 uses, and nothing else in the feature waits on it.

### 3.1 What is true today, and why it is the problem

`worker_session.delivery_hold` holds every queued message for a work order whose turn is
in flight — the `busy(store, wo["id"])` branch, returning `HOLD_TURN_IN_FLIGHT` with the
reason *"a turn is already in flight"*. `Daemon.deliver_messages` honours that hold, so the
earliest a message can reach a worker is the next turn boundary. It then arrives as a
FRESH TURN, which re-sends the whole accumulated conversation at the 1.25x cache-write
rate — ~12% of this project's entire token spend goes on exactly that boundary.

So today an objection is both late and expensive. That is the honest fallback, and it is
good enough to ship the feature on; the spike asks whether there is something better.

### 3.2 The claim to test

Claude Code native cross-session messaging (`SendMessage`, CC 2.1.224+) is documented to
deliver into a receiver that is MID-TURN, between its tool calls, with no new turn and no
context re-write. Jarvis workers are already addressable: `dispatch` names each session
`[WO <id>] <title>`.

Four unknowns, none of them answered by the release notes:

1. **Does mid-turn delivery work under `-p`?** Workers are headless.
2. **Is `SendMessage` — or the inbox socket a `-p` session is said to bind while
   running — actually available in that mode?**
3. **Does a delivered message appear in the receiver's transcript?** If it does not,
   `usage.read_session`, `bill` and `jarvis inspect` all go blind on it, and an objection
   that cost tokens would be invisible to every cost surface the OS has.
4. **Does the receiver learn WHO sent it, and can it refuse an unknown sender?** §6.4
   requires this: the peer path opens an inbound channel into a running worker, and
   guidance is the most valuable thing on the machine to be able to forge. A "yes" on
   unknowns 1–3 with a "no" here still means the peer transport does not ship.

### 3.3 How to test it so the answer is about the right process

Reproduce the argv a real worker runs under, from `claude_cli.turn_args()` /
`spawn_turn()`: `claude -p --output-format json --resume <uuid> -n "[WO <id>] …" …`,
detached with `start_new_session=True` and `stdin=DEVNULL`. A spike run in a friendlier
shape answers a question nobody asked. Also check the message survives `--autocompact`.

**This measurement cannot run under `pytest`, and must not try.** The root `conftest.py`
gate points `JARVIS_CLAUDE_BIN` at a stub that exits 1, precisely so no test can reach the
real binary. So this is a hand-run script, executed from the worktree, and its output is
the evidence. A worker that fights the gate ends up measuring the stub and reporting its
behaviour as the answer.

**The bar for a positive result is a batch of at least five trials in which every one
delivers mid-turn AND appears in the receiver's transcript.** Any mixed batch is `not
reliable`, which §3.4 treats exactly as `does not`. Report the trial count and the success
count, not a narrative.

Check the settings that switch the feature off, because a fleet with any of them set gets
the fallback and must not silently get nothing: `DISABLE_TELEMETRY`, `DO_NOT_TRACK`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_GROWTHBOOK`, and the
`crossSessionInbound` setting (`accept` / `hold` / `refuse`).

### 3.4 Outcomes

**"Inconclusive" is an acceptable and expected result.** "Works sometimes" is not a
transport, and reporting it as one would be worse than reporting a failure. Three
outcomes: it works (§6 ships the peer path with the queue as fallback); it does not (§6
ships the queue path alone); it is not reliable (same as "it does not", recorded as such).

The findings go in a Serena memory (`write_memory`, amending
`cross-session-messaging-facts`, whose "unverified — spike needed" list is exactly these
three unknowns) and back into this section of this file, so the next reader gets the
measurement and not the question. A measurement of an external tool expires when that tool
ships a version: date it and name the CC version measured (kn-df5574d3).

## 4. The state

Everything the other sections read and write. Additive columns on `assumptions`, store
verbs beside the existing ones, event kinds, and the renderers — delivered with **no
caller yet**, so the five sections behind it land in parallel.

### 4.1 `assumptions.status` does not gain a value

This is the single most dangerous edit available in this feature and it is forbidden.
`pending` is load-bearing at, at least: `ProjectStore.pending_assumptions`,
`invariants.true_blockers` (the first blocker it appends), `automerge.decide` condition 3,
`ops.ack_attention`'s and `ops.mark_done`'s refusals, `autoreview.decide` condition 3,
`Daemon._deliver_assumption_verdict`'s already-settled drop, and the project summary
counts. A status value of `provisional` would read as NOT PENDING at every one of those,
silently opening the user's gate — which the parent spec names as the feature's failure
mode. Provisional state lives in columns; `status` stays `pending` until §7 confirms.

### 4.2 Columns

All of them in `ProjectStore.ADDED_COLUMNS["assumptions"]`, which is the only path that
reaches a live database. The `assumptions` block in `SCHEMA` is for fresh databases and
already says so: a column added there alone works on every test fixture and on no real
project (kn-c712a5d6 — and a column is untested until a test writes it AND a test reads a
row that predates it).

| column | type | meaning |
|---|---|---|
| `provisional_verdict` | `TEXT NOT NULL DEFAULT ''` | `''` \| `accept` \| `object` |
| `provisional_reason` | `TEXT NOT NULL DEFAULT ''` | Neo's one line |
| `provisional_model` | `TEXT NOT NULL DEFAULT ''` | from the transport's own report |
| `provisional_stakes` | `TEXT NOT NULL DEFAULT ''` | as classified, or `unclassified` |
| `provisional_ts` | `REAL` | when the early verdict was formed |
| `provisional_config_version` | `TEXT` | the configuration it was formed under |
| `confirm_question_id` | `INTEGER` | the SECOND Neo question, asked at delivery (§7) |
| `objection_envelope_id` | `INTEGER` | the `envelopes` row the objection IS (§6.2) |
| `objection_transport` | `TEXT NOT NULL DEFAULT ''` | `''` \| `queue` \| `peer` |
| `objection_sent_ts` | `REAL` | when the OS handed it to the transport |
| `objection_delivered_ts` | `REAL` | when the worker actually received it, or NULL |
| `objection_withdrawn_ts` | `REAL` | when the objection was withdrawn undelivered (§6.6) |

`objection_sent_ts`, `objection_delivered_ts` and `objection_withdrawn_ts` are three facts
and not one: the user's requirement is to see *how and when it was delivered, and whether
it was delivered*, and a single boolean cannot tell "sent, still in flight" from "sent,
never arrived" from "withdrawn, the worker stopped first" — and §9 raises attention on
exactly one of those three.

**The identity is the ENVELOPE, not the message row, and the reason is timing.**
`bus.post` returns an envelope id; the `wo_messages` row does not exist until
`Daemon.deliver_envelopes` resolves that envelope on a later tick, so a
`objection_msg_id` column could not be filled at the moment the objection is recorded —
and §6.1 requires the record to be complete before anything is sent. One additive column
closes the gap for everyone: `envelopes.delivered_msg_id`, in
`ProjectStore.ADDED_COLUMNS["envelopes"]`, written where `bus.deliver` calls
`queue_message`. An envelope today cannot say what it became; §8 and §9 both need to
follow that link, and so will the next reader of the bus.

### 4.3 Store verbs

Beside `review_assumption`, `link_assumption_question` and `assumption_for_question` in
`project_store.py`. Names are the interface; the sections behind this one call them:

* `record_provisional(assumption_id, *, verdict, reason, model, stakes, config_version)`
* `link_assumption_confirmation(assumption_id, question_id)`
* `record_objection(assumption_id, *, envelope_id, transport, sent_ts)`,
  `mark_objection_delivered(assumption_id, ts)` and
  `withdraw_objection(assumption_id, ts)`
* `outstanding_objections(wo_id)` — the assumptions whose objection is neither delivered
  nor withdrawn. §6.6 withdraws from this list and §7 refuses to run while it is non-empty,
  so both sections read one query and cannot disagree about what "outstanding" means.
* `assumption_for_question(question_id)` **must resolve BOTH columns.** It is a single-row
  lookup by question id today and the confirmation question would resolve to nothing.
* `all_assumptions(wo_id)` carries every new column through, because it is what both
  surfaces in §8 read.

### 4.4 Event kinds and the renderers

Five kinds, added to `ops.AUTOREVIEW_EVENTS` (and `timeline`'s label/debug tables as the
existing ones are) so that no section invents its own: `autoreview_provisional`,
`autoreview_objected`, `autoreview_objection_withdrawn`, `autoreview_confirmed`,
`autoreview_unconfirmed`.

`ops.assumption_line` and `ops.assumption_decider` are widened here, once. They exist
because the attribution must have exactly ONE renderer — `work_order.html` says so in a
comment — and two renderers means the OS credits a machine verdict to the user on one of
them.

## 5. Early review: judging an assumption while the order runs

### 5.1 A separate decision function

`autoreview.decide_early(assumption, wo, cfg, *, round_outcome, refusal_answered,
asked_question_id=None)`, pure, beside `decide` and sharing its nets.

**`autoreview.decide` is NOT modified, and its condition 2 is not relaxed.** That function
is asked twice (parent spec §5.1) and its second call — in
`Daemon._deliver_assumption_verdict`, against freshly read state — is the only guard on
`ops.accept_assumption` → `_land_after_acceptance` → `ops.land_when_cleared`. Loosen
`status != "needs_review"` inside it and a RUNNING work order can land. Two functions, two
failure directions.

`decide_early`'s conditions: `cfg.enabled and cfg.auto_review`; the work order is
`running`; the assumption is pending with no provisional verdict yet; the panel has not
given up; no refusal of the user's is outstanding; it has not already been asked about
(`neo_question_id`); and `high_stakes_marker` finds nothing in its text. Held reasons reuse
the existing `HELD_*` tokens and add what the running case needs.

### 5.2 The ask pass

`Daemon.auto_review` gains a second candidate list: work orders in `running` whose
assumptions are pending and unjudged, decided by `decide_early`. The `needs_review` list
and everything it does are untouched.

**`Daemon._note_autoreview_held` suppresses four codes** — `disabled`, `status`, `settled`,
`asked` — and its stated reason for suppressing `status` is that it is *"unreachable from
this pass, which lists `needs_review` only"*. The moment this section lists running orders
that sentence is false, and the suppression can hide a real hold. Re-derive the suppression
list per pass rather than sharing one.

### 5.3 The two outcomes

Read through `autoreview.read_ruling` exactly as today, with both nets armed.

* **Neo AGREES** → `record_provisional(verdict="accept", …)` plus an
  `autoreview_provisional` event. Nothing settles. §7 decides it later.
* **Neo DISAGREES** → `record_provisional(verdict="object", …)` and **stop there**. §5
  forms the verdict and writes it down; it does not file or send anything. Turning a
  recorded `object` verdict into a filed, sent objection is §6's, hook and all — see §6.1.
  **The seam is the recorded verdict, not a function call**, so the two sections meet in
  the database rather than in an import, and each can be tested without the other.
* **Anything else** (escalate, unparseable, non-routine stakes, transport failure) →
  exactly today's behaviour: no provisional verdict, the assumption stays pending, and the
  user decides at `needs_review`. An early pass that cannot form a verdict costs nothing
  and changes nothing.

### 5.4 The prose that must change with the code

`autoreview.py`'s module docstring, `decide`'s condition-2 docstring,
`ASSUMPTION_REVIEWER_PERSONA`, and §5/§6 of
[2026-09-15-neo-decides-an-assumption.md](2026-09-15-neo-decides-an-assumption.md). All
four currently assert that a mid-run assumption is not judged and that nothing the
reviewer says reaches the worker. Leave them and the code contradicts itself in the file a
reviewer opens first. The persona in particular is a briefing: Neo must be told it is
ruling on an intention, that its objection will reach a worker that is still typing, and
that it should write for that reader.

## 6. The objection: recorded first, sent second

### 6.1 Record, then send. Never the other way round

`ops.file_assumption_objection(store, project_path, wo, assumption, *, reason, model,
question_id) -> dict` returning `{"envelope_id", "transport", "sent_ts"}`.

**And the hook that calls it is §6's too.** `Daemon.auto_review`'s early pass, having run
§5, reads back the assumptions carrying `provisional_verdict='object'` with no objection
recorded, and files one for each. One hook, in one section: if §5 called the filer
directly and §6 also installed a hook, the drain would file twice, and the two children
would collide on the same lines of `daemon.py`.

Order of operations, and it is not negotiable: the timeline event and the envelope row
exist in the database BEFORE anything touches a wire. **The objection must never exist
only on the wire.** A send that fails leaves a complete record of what was said and that
it did not arrive; a send that succeeds after a crash leaves a worker acting on guidance
the record cannot explain.

### 6.2 It travels over the bus

`bus.post(store, subject=Subject(wo_id=…), from_role="reviewer", to_role="implementor",
payload=…)`. The bus exists precisely so that no entity addresses another directly; its
delivery already rides `store.queue_message` in one transaction, and
`Daemon.deliver_envelopes` already turns the queue. A direct `queue_message` call is the
shortcut the bus was built to stop.

`bus.post` returns the ENVELOPE id and nothing else — it never resolves, on purpose — so
that is what §4.2 stores and what `envelopes.delivered_msg_id` links forward to a
`wo_messages` row once the daemon turns the queue.

`ReviewFeedback` is the wrong payload — its `round` and `outcome` fields describe a
validation round and an objection is not one. Add `AssumptionObjection(assumption_n: int,
reason: str, question_id: int | None = None)` to `bus.PAYLOADS` and
`project_store.ENVELOPE_KINDS`. A test walks `PAYLOADS` against the kinds tuple, so the
two cannot drift.

### 6.3 Two side effects that must NOT be inherited

* **`wo_messages.authored_by` stays `''`.** Only the user's own words are ever stamped
  (`ops.user_authorship`), and an objection from Neo stamped as the user is a lie in the
  record that a worker will read as an instruction from a person.
* **`ops.send_message`'s `clear_attention` must not be copied.** That is the precedent for
  the queue call, but its attention side effect belongs to a user typing, not to the OS
  objecting — and what a running order's attention should say is §9's subject.

### 6.4 The transport, and the fallback that always exists

The queue path ships regardless: record, post to the bus, `transport="queue"`, delivered
at the next turn boundary by `Daemon.deliver_messages`. This is the whole feature working,
late — **and for a headless worker "late" is often "never", because its next turn boundary
is usually the end of its run.** That is not a defect to be papered over; it is why §6.6
exists and why the queue path's honest outcome on a short order is a withdrawal rather
than a late delivery.

If §3 came back positive, the same function delivers via peer messaging when the worker is
mid-turn — `transport="peer"`, `objection_delivered_ts` stamped from the send's own
report — and falls back to the queue on any error, on any of the disabling settings being
set, and whenever the worker is not actually running. **One call site, one function**: the
peer path changes how an already-recorded objection travels and nothing about how it is
authored. The single line that forbids mid-turn delivery today is
`worker_session.delivery_hold`'s `HOLD_TURN_IN_FLIGHT` branch, and it is the only hold
this section may touch — the budget, no-session and retry holds all still apply, because a
peer message into a worker with no money or no session is no more deliverable than a
queued one.

**The peer sender is an injectable parameter, and that is a testability requirement rather
than a style preference.** The test fake has no inbox and cannot receive a mid-turn
message, so no test in this repository can assert that a peer message actually arrived.
What a test CAN prove is selection and fallback: that a busy worker selects
`transport="peer"` and calls the injected sender, and that the sender raising — or any of
§3.3's disabling settings being set — falls back to `transport="queue"` with an identical
recorded message. Hard-wire the send and the peer half of this section ships unverified.

**The peer path opens an inbound channel into a running worker, and §3 must report whether
that channel says who is talking.** A worker that accepts a mid-turn message accepts it
from whatever can reach its inbox socket, and an objection is guidance a worker acts on —
the most valuable thing on the machine to be able to forge. So §3 answers a fourth
question alongside its three: **does the receiver learn the sender's identity, and can a
receiver refuse an unknown one** (`crossSessionInbound`'s `accept` / `hold` / `refuse`)?
If it cannot, the peer transport does not ship, whatever the delivery trials said — the
queue path is not a degraded mode here, it is the one whose sender is the daemon by
construction. If it can, every objection carries a sender the worker can check and the
worker's briefing tells it to act only on a message from the OS. The standing
`PreToolUse` allowlist on `SendMessage` is filed on the backlog and is a fleet-wide
policy, not this feature's to build.

### 6.5 What the worker did about it

`objection_delivered_ts` says it arrived. What the worker DID is read from the record that
already exists: the events on the work order's timeline after that timestamp, and any
later assumption or `wo finish` summary. Nothing new is stored for this; §8 renders it by
reading forward from the delivery.

### 6.6 An undelivered objection is withdrawn when the order stops running

The one rule for the case §2's licence does not cover, and every other section defers to
it.

**Trigger.** On a reconcile tick, an assumption is in `outstanding_objections(wo_id)`
(objection recorded, `objection_delivered_ts` NULL, `objection_withdrawn_ts` NULL) and its
work order is no longer `running`.

**Action, in this order.** Mark the objection's carrier undeliverable, so it can never
become a turn: the envelope if it is still `queued`, and the `wo_messages` row it resolved
to if it already has one, both moved to a **`withdrawn`** state. Then stamp
`objection_withdrawn_ts` and write `autoreview_objection_withdrawn`, naming the assumption
and the reason ("the order stopped before it could be delivered"). The message content is
never erased: withdrawal stops a delivery, it does not unsay the objection, and §8 still
shows it in full.

`wo_messages.status` gains `withdrawn` beside `queued | delivered | failed`, and that is
safe in the way §4.1's status change would not have been: every queue reader selects
`status='queued'`, so a withdrawn row simply stops being deliverable and stops being
`invariants.stuck_message`'s problem. Whoever does this greps every reader of that column
and of `envelopes.state` and confirms it, rather than assuming it.

**Belt as well as braces.** `Daemon.deliver_messages` also refuses to deliver an
`AssumptionObjection` to a work order that is not `running`, and marks it withdrawn there.
The tick that withdraws and the pass that delivers are two passes over the same rows and
they race; the delivery side is the one that must lose.

**Never withdraw a delivered objection**, and never withdraw on a status the order can
come back from within the same tick. The test that matters is the pair: an order that
stops with an objection still queued ends `withdrawn` and the worker is never sent a turn;
an order still running keeps it outstanding, however long it has been queued.

## 7. Confirmation at delivery, before anything settles

A provisional approval is an opinion about an intention. At `needs_review` the intention
has become a diff and a result summary, and only then may it settle.

**Ruled by Neo for the user, 2026-09-23, question 549: this is a SECOND MODEL CALL.** The
cheaper design — confirm in code, re-ask only if something changed — was put to it and
refused, on the ground that a mid-turn verdict never had a result summary in the first
place, so "something changed" is always true and the cheap path either degenerates into
the expensive one or confirms a diff that no model ever read. That would defeat the
requirement exactly.

**It runs after §6.6, never beside it.** A work order arriving at `needs_review` left
`running` to get there, so every outstanding objection on it is withdrawn on that same
transition. The confirmation pass is therefore gated on `outstanding_objections(wo_id)`
being empty, and a non-empty list means "not yet" and costs nothing: it is retried on the
next tick, after the withdrawal has run. Without that gate the two passes race on one
assumption — one settling it while the other still has a message in flight to the worker
about it — which is the contradiction this ordering exists to remove.

So, when a work order with provisional verdicts reaches `needs_review`:

1. **`autoreview.decide` runs first, unchanged**, and every one of its seven conditions
   must hold. A provisional verdict is not a ticket past any of them.
2. A second Neo question is asked, `kind="assumption"` — **the existing kind, reused.**
   A new Neo question kind is SEVEN edits and none of them are in this feature's scope
   (kn-4edb0eb7, and the comment at `autoreview.QUESTION_KIND`). It carries the
   assumption, the provisional verdict and its reason, and the diff and result summary the
   early pass did not have. It is linked through `confirm_question_id`, which is why that
   column is separate: `neo_question_id` already points at the early question, and one
   question per assumption per pass is the invariant being preserved, not broken.
3. `read_ruling` reads the reply with both nets armed. **Confirmed** → the existing
   `ops.accept_assumption` settles it exactly as today, with `decided_by=neo`, and
   `Daemon._deliver_assumption_verdict`'s re-run of the whole condition table against
   freshly read state still guards the settle. **Withdrawn, or anything else** → an
   `autoreview_unconfirmed` event, the question marked `escalated`, and the assumption is
   the user's, carrying both readings: what Neo thought while the work ran and what it
   thought once it saw the result.

A provisional verdict of `object` never settles anything here, and **no confirmation
question is asked on one** — there is nothing to confirm, because nothing was ever
approved. It is the user's, three ways:

* **delivered.** The worker was told; either it acted, in which case the assumption it
  re-records is a new row judged on its own, or it did not, in which case the user sees the
  objection and the unchanged assumption side by side and decides.
* **withdrawn (§6.6).** The worker was never told. The user sees the objection, marked
  withdrawn, and decides with it in front of them — which is exactly today's behaviour
  plus Neo's reading, and strictly better than the worker receiving guidance after it
  stopped.
* **undeliverable (§9).** The same, and it additionally raises attention, because a
  transport that failed is a fact about the OS rather than about the assumption.

## 8. The assumptions view

One section on the work order's dashboard page and the same facts on `jarvis wo show`.
**One child owns both, and one renderer serves both** — `ops.assumption_line` and
`ops.assumption_decider`, already shared for exactly this reason. Two renderers is how a
machine verdict gets credited to the user on one surface.

Per assumption, the user's explicit requirement, in this order:

1. its text and its status;
2. Neo's verdict and its reasoning — the provisional one, with its model and timestamp;
3. **the objection sent to the worker**, in full: how it was delivered (`peer` or
   `queue`), when it was sent, and which of **four** states it ended in — *in flight*,
   *delivered at <ts>*, *withdrawn at <ts>, the order stopped first* (§6.6), or
   *undeliverable* (§9). They are four different things to a user and rendering any two of
   them the same is the defect. *Every Neo → worker objection is captured and visible
   here, delivered or not* — that is the user's stated requirement and the acceptance test
   for this section;
4. what the worker did in response — read forward from `objection_delivered_ts` (§6.5);
5. the provisional approval and whether it was confirmed at delivery (§7), or withdrawn.

**This section also owns the "waiting on the worker since …" line**, for an assumption
whose objection was delivered and which the worker has not yet answered. §9 explains why
it cannot live in attention derivation: `invariants.true_blockers` returns only what the
USER owes, and this is the one state in the feature that is owed by somebody else. It is a
fact about the assumption, so it belongs beside the assumption — here, or on
`ops.autoreview_state`'s line, but in exactly one of them.

Touches `cli._readable_autoreview`, the `wo show` payload assembly in `cli.py`, the
assumptions block in `ui/templates/work_order.html`, and the `assumption_decider` jinja
global in `ui/app.py`.

**The historical row is the test that matters.** Every assumption written before this
ships has all the new columns empty, and must render as it does today — not as an
objection that was never delivered. Same for an assumption in an `auto_review`-off
project, which is most of the fleet.

## 9. Attention while the order runs

A running order in an `auto_review` project must not read "Needs you" merely because
assumptions are pending — nothing is waiting on the user, and a `Needs you` that is not
true is the attention cost this whole feature exists to reduce.

**This section is deliberately narrow, and the reason is outside this repository.**
GitHub issue [#711](https://github.com/gonandrap/agentic_os/issues/711) is open, labelled
`in progress`, and work order `wo-99d3d318` is live on it with exactly that title and
exactly that expected behaviour. It owns the general fix. Neo ruled for the user
(question 547) that this section covers **only the states this feature invents** and
**waits for #711 to merge**, building on its attention derivation rather than editing the
same lines beside it.

What is left once #711 has landed, and it is all of it:

* an assumption carrying a **provisional approval** raises no blocker. **Anchor this at
  `needs_review`, not at `running`.** A work order parked in `needs_review` whose
  assumption is pending with a provisional approval on it is one the OS is actively
  confirming (§7) and the user owes nothing — a state that is genuinely new, that #711
  cannot reach, and that is testable the day this lands. Anchored at `running` instead,
  the row may be VACUOUS: if #711 removes the assumptions blocker for running orders
  wholesale, the test passes against an empty diff, which is the worst kind of green. The
  same test must carry its **negative control** — the identical row with
  `provisional_verdict` empty still produces the blocker — so that a deleted line cannot
  pass it either.
* an assumption with an **objection sent and delivered** raises no blocker: it is waiting
  on the worker, not on the user. **The "waiting on the worker since …" line is NOT
  rendered here.** `true_blockers` returns only what the user owes, and its `[0]` IS the
  attention reason — so a state that correctly raises no blocker has no reason line left
  to carry a sentence. That line belongs to §8's assumptions block, which owns it.
* an assumption whose objection is **UNDELIVERABLE** IS the user's problem, and must raise
  attention — it is the one new row in this section that adds a blocker rather than
  removing one. **Undeliverable is a state, not an elapsed time**, and the trigger is
  written so that normal queue operation cannot reach it: the objection's `wo_messages`
  row is `failed` (`mark_message_failed`, retries exhausted), or its envelope is in a
  terminal state that is not `delivered`. A peer send that raised is NOT this — it fell
  back to the queue and the queue is trying.

  Three states explicitly do NOT raise it, and a test asserts each: an objection still
  `queued` on a running order (this is the normal case for the whole length of a turn, and
  raising here would reverse part 3 of the brief and light "Needs you" on every objection
  the OS ever sent); an objection `withdrawn` under §6.6 (nothing failed — the order
  stopped, which it is allowed to do, and §8 shows it); and `objection_delivered_ts` being
  NULL by itself, which is the ambiguous fact and must never be read as the trigger. There
  is deliberately **no deadline**: a timeout would put a clock on how long a worker may
  think, and the OS has no basis for that number.

`invariants.true_blockers` is the single source of truth and the only place this may be
done. `check_attention_reason_is_true` (INV-ATTENTION-REASON) REWRITES on the next tick any
reason it cannot re-derive, and it is strict about assumption reasons where it is lenient
elsewhere — so a label changed anywhere else is erased within a tick. Whoever does this
also touches `_mentions_assumptions` (the parked-reason fallback and `acknowledged` both
read it), `check_no_phantom_attention` and INV-ATTENTION-MISSING.
[2026-09-16-an-idle-manager-is-not-waiting-on-you.md](2026-09-16-an-idle-manager-is-not-waiting-on-you.md)
is the same shape of fix and is the precedent to follow.

## 10. Failure directions

Every row ends where the parent spec's do: the user decides it, as they do today.

| what breaks | what happens |
|---|---|
| the daemon is down | nothing asks; the assumption stays pending, exactly as now |
| `os.neo.enabled` is off | no early pass, as with the existing one |
| `validation.auto_review` is off | no early pass, no objection; this is most of the fleet |
| the spike failed, or the peer channel cannot identify its sender | `transport="queue"`; the objection arrives at the next turn boundary, or is withdrawn if there is not one |
| peer send raises | fall back to the queue, same row, `transport` records which was used |
| the objection is queued and the worker is still running | nothing: it is in flight, §8 says so, §9 raises nothing |
| the worker finishes before the objection lands | §6.6 withdraws it: the carrier is marked `withdrawn` and NO turn is ever started on the settled order; §8 shows the objection and that it was withdrawn; §7 asks no confirmation question and the user decides |
| the objection's carrier fails outright (retries exhausted, or a terminal envelope state) | undeliverable: §9 raises attention, §8 shows it, the assumption is the user's |
| the order stops between the withdrawal tick and the delivery pass | the delivery pass refuses an objection to a non-`running` order and withdraws it there (§6.6); the two passes race and delivery always loses |
| Neo's early call fails or will not parse | no provisional verdict; the assumption is judged at `needs_review` as today |
| Neo objects, the worker ignores it | the assumption is never confirmed early; §7 asks again with the diff, and the user sees both readings |
| the confirmation call fails | nothing settles; `autoreview_unconfirmed`, the user decides |
| the text is high-stakes | neither pass runs, and it is withheld from every sibling list |
| the order is cancelled mid-flight | `decide` is re-run against freshly read state before any settle and drops the ruling |
| a row predates this feature | every new column is empty and every surface renders it as it does today |

## 11. Cost

One Neo call per assumption at record time, plus one at delivery. Both are
`kind="assumption"`, which is not in `DEFAULT_PANEL_KINDS`, so a single agent answers and
neither is a panel. Both appear under the `jarvis` column of `jarvis cost <wo-id>` with no
new plumbing — that column already records every Neo answer as it happens.

Against that: the early call can save a worker from building on a wrong assumption for the
length of a turn (25 minutes, measured), and the peer transport — if §3 came back
positive — saves one full turn boundary per objection, which is a whole-conversation
re-write at the 1.25x cache-write rate.

## Agent profile

You are a Jarvis OS engineer working on the machinery by which the OS judges a worker's
assumptions. You are working in the DEV checkout of `agentic_os`, in your own git
worktree, on one section of the design document above. You do the work yourself — you do
not create work orders for it.

**What you must know about this codebase.**

The OS is a Python package in `src/jarvis/`. Serena is activated and the code map is
committed: read the memories `codebase-map`, `work-order-lifecycle` and
`feature-orders` BEFORE exploring, and use `find_symbol` and `find_referencing_symbols`
rather than grepping for symbols. Rediscovering the architecture is the most expensive
thing you can do with your context, and it has already been paid for.

The shape this feature lives in is a deliberate one, copied from `automerge.py` and
`autoreview.py`: a per-project flag that ships `false` at both levels, a PURE decision
function whose conditions are enumerated and unit-testable with no network, a thin daemon
half that owns the database and the model call, and a hold recorded once per (subject,
reason) and rendered as one line a person can read. Match it. A second vocabulary for the
same idea is the defect.

Three storage facts that bite: only `ProjectStore.ADDED_COLUMNS` reaches a live database
(the `SCHEMA` block is for fresh ones and will pass every test while working on no real
project); a new column is untested until one test writes it and another reads a row that
predates it; and a sqlite connection belongs to the thread that made it.

**Conventions you follow.** Tests with `uv run pytest tests/ evals/` (`uv sync --extra dev`
first in a fresh worktree). Branch, then a pull request against `main`; `main` is never
committed to directly. The house style — `caveman` and `i-have-adhd` — governs every byte
you write, including the PR body, the commit message and your finish summary. Docstrings in
this codebase carry the ARGUMENT for a decision, not a description of the code; write them
that way, because the next reader is deciding whether to undo what you did.

**Traps to avoid, in the order they will bite you.**

1. **Never add a value to `assumptions.status`.** §4.1 lists the eight-plus call sites that
   read `pending` as "the user owes a decision". Provisional state lives in columns.
2. **Never relax `autoreview.decide`'s condition 2.** It is the only guard on the
   irreversible settle path. Early review is a separate function.
3. **Record before you send.** A message that exists only on a wire is a message the record
   cannot explain.
4. **Never stamp `wo_messages.authored_by` for anything but the user's own words.**
5. **Never call `ops.accept_assumption` for a provisional verdict** — it stamps
   `decided_by`, writes `autoreview_accepted` and lands the order behind it.
6. **A new Neo question kind is seven edits, not one.** Reuse `kind="assumption"`.
7. **Attention is reconciler-derived.** `invariants.true_blockers` is the only place;
   INV-ATTENTION-REASON rewrites anything it cannot re-derive within one tick.
8. **A failure is never an answer.** A model that was never reached has made no judgement,
   and synthesising one from a crash is the defect the fleet has a pinned learning about.

**What you must never do.** Do not widen the scope of your section into a sibling's — the
design document says who owns what and the seams are function signatures and column names,
which you may rely on and must not rename. Do not merge your own pull request or cut a
release; those are gated actions and you request them, you do not take them. Do not report
work as done that you have not run the suite against. If you are in doubt about a decision
rather than about a fact, `jarvis wo ask` your work order and end your turn — Neo answers
within about a minute, and a guess recorded as an assumption is more expensive than a
question.
