# An unreachable Neo question is not a question in flight

GitHub issue #788. Live case: wo-00bd1096 assumption #1, question 722. Ruling behind the
design: Neo question 736 — do not re-open it.

## The problem

An assumption asked about while Neo is unreachable is stuck for ever, and every surface
lies about it.

`neo_store.release_claim` (src/jarvis/neo_store.py:366) spends the retry ladder and writes
`status='failed'`, `answer_reason = UNREACHABLE_PREFIX + detail`. Nothing clears the
project-side pointer, and nothing re-derives from it:

1. `ops.assumption_ruling_line` src/jarvis/ops.py:3033 — the `autoreview_asked` branch
   renders `Asked Neo (question {question}), awaiting ruling`. No ruling is coming. The
   `autoreview_asked` event is append-only and immortal; the question it names is dead.
2. `ops.autoreview_state` src/jarvis/ops.py:2694, through `_autoreview_line`
   src/jarvis/ops.py:3160 — `assumption #{n} is with Neo (question {n})`. Same event, same
   lie, on `jarvis wo show`'s summary line and the work-order page.
3. `autoreview.decide` condition 6 (src/jarvis/autoreview.py:846-849),
   `decide_confirm`'s `confirm_question_id` gate (src/jarvis/autoreview.py:909-913) and
   `decide_early`'s condition 7 (src/jarvis/autoreview.py:995-998) all read the pointer as
   "already with Neo" and hold `HELD_ASKED`. The outage ends; the assumption is never
   re-asked, for the life of the work order.

The symptom the user sees is (1); the root cause is that a link written at ask time is
read as a present-tense claim about a row in another database that has since changed
status. Nothing anywhere re-checks it.

Second, narrower defect on the same fault line: `NeoStore.reclaim_stale`
(src/jarvis/neo_store.py:290) marks a STRANDED question `failed` with no hook at all.
`Daemon.neo_tick` src/jarvis/daemon.py:2864-2867 only `log.warning`s the ids. So the
inbox warning and the attention flag that `_neo_drain`'s `unreachable` closure
(src/jarvis/daemon.py:2964) posts on the release path are never posted on the stranded
path. Neo, question 736: "a 'you decide it' line that nobody is told about is the same bug
again."

## The fix

READ SIDE ONLY. The pointer stays. Two reasons, both load-bearing:

* the link is the EVIDENCE the render needs — the question id the user is sent to
  (`jarvis neo answer <qid>`, the question URL) and the last-error hint in
  `questions.answer_reason`. Clearing it leaves a "decide this yourself" line pointing at
  nothing;
* a write-side fix in the drain's unreachable hook misses `reclaim_stale` entirely
  (src/jarvis/neo_store.py:313, a bare UPDATE with no callback). Half the unreachable
  rows would never be cleaned.

Precedent, and the rule: kn-96f47efb — a present-tense claim derived from an append-only
event must be re-derived at the read side against the row it is about. That is exactly
what `_AUTOREVIEW_OWED_BY_USER` (src/jarvis/ops.py:2691, resolves "left with you" against
`assumptions.status`) and `_stale_panel_hold` (src/jarvis/ops.py:2824, resolves a panel
hold against the latest round) already do. This is the third instance, against
`questions.status`.

### 1. `ops._unreachable_asks(wo_id)` — new, beside `_stale_panel_hold` (src/jarvis/ops.py:2824)

`_stale_panel_hold`'s exact shape: a closure over one work order, LAZY and cached.

```
def _unreachable_asks(wo_id: str):
    """`qid -> {"question_id", "hint"} | None` for the asks whose question is dead."""
```

* opens `NeoStore` on FIRST call and not before — an order with no `autoreview_asked`
  ruling on any row pays nothing. Precedent for ops reaching into `neo.db`:
  `ops.delete_work_order` src/jarvis/ops.py:5452, `invariants.awaiting_neo`
  src/jarvis/invariants.py:1509;
* best-effort, failing TOWARDS today's behaviour: any exception opening or reading
  `neo.db` yields `None` for every id, so the line reads as it does now rather than
  crashing `jarvis wo show`. `awaiting_neo`'s documented direction;
* UNREACHABLE IS `status == 'failed'`, nothing else. `escalated` is Neo handing the
  question back with a decision and already reaches the assumption as an
  `autoreview_escalated` event through `Daemon._deliver_assumption_verdict`; `queued`,
  `answering` and `answered` are genuinely in flight or already delivered. `failed` on an
  assumption question has exactly two writers — `release_claim` and `reclaim_stale` — and
  both stamp `UNREACHABLE_PREFIX`;
* `hint` = `answer_reason` with `neo_store.UNREACHABLE_PREFIX` stripped, whitespace
  collapsed, 160 chars. Strip the prefix because the sentence around it already says Neo
  could not be reached, and printing it twice reads as a quoted status code.

### 2. `ops.assumptions_with_rulings` (src/jarvis/ops.py:2846) attaches the fact

For a row whose surviving newest ruling is `kind == "autoreview_asked"` and only then,
ask the closure about `payload["neo_question_id"]` and attach
`os_ruling["unreachable"] = {"question_id": …, "hint": …}` when it comes back. Same
discipline as `objection_response` and `_overtaken` in that function: derived for the
handful of rows that can carry it. Both links are covered by one branch because
`autoreview.propose_confirmation` (src/jarvis/autoreview.py:1286) writes
`autoreview_asked` too, with `confirm: True` in the payload.

`assumption_ruling_line` STAYS PURE and keeps reading only the row — it is a renderer on
two surfaces (`jarvis wo show` via cli.py, the work-order page via ui/app.py) and both
take their rows from `assumptions_with_rulings`, so one derivation reaches both. Issue
#712's lesson, already in that docstring.

### 3. The sentences

`ops.assumption_ruling_line`, `autoreview_asked` branch. With `unreachable` set, instead
of `Asked Neo (question N), awaiting ruling`:

> `Neo could not be reached (question 722) — nobody judged this{ to confirm its early
> reading}; you decide it: `jarvis neo answer 722 "…"` then `jarvis wo review
> wo-00bd1096` — last error: connection reset by peer`

* ` to confirm its early reading` only when the payload carries `confirm: True`; the
  early-reading fact itself is `provisional_line`'s and is not repeated;
* the `— last error: …` tail is dropped when `hint` is empty;
* `wo review` takes the row's `wo_id` column (assumptions carry it —
  src/jarvis/project_store.py:755).

`ops._autoreview_line`, `autoreview_asked` branch. Instead of `assumption #N is with Neo
(question N)`:

> `assumption #1 left with you — Neo could not be reached (question 722); nobody judged
> it`

`left with you —` deliberately, matching the two `_AUTOREVIEW_OWED_BY_USER` branches: this
is the same claim and it must sort into the same reading. It does NOT gain the
`resolved_status` suffix — that suffix resolves an event against a SETTLED row, and this
branch is only reachable while the row is pending (a settled row's ruling line comes off
`decided_reason`, src/jarvis/ops.py:3022).

Neither sentence contains "escalated" or "Neo decided". Pinned learning: a crash is not a
decision. The `autoreview_unconfirmed` wording ("Neo did not confirm…") is likewise NOT
reused — it asserts Neo formed a view.

### 4. Re-arm: one new pure parameter

`autoreview.decide`, `decide_confirm` and `decide_early` stay PURE — no store, no clock.
Each takes `unreachable_question_ids: Collection[int] = ()`:

* `decide` condition 6 becomes `if asked and asked != asked_question_id and asked not in
  unreachable_question_ids` (src/jarvis/autoreview.py:847). Condition 6's mandate is
  unchanged — ONE question per assumption — because a `failed` question is not a question
  any more; the ceiling was spent and no ruling can arrive on it. A link that is `queued`,
  `answering`, `answered` or `escalated` is absent from the set and holds exactly as today;
* `decide_confirm`'s `confirm_question_id` gate (src/jarvis/autoreview.py:909) takes the
  same clause, and forwards the set into its `decide` call at
  src/jarvis/autoreview.py:918. Both gates need it: the confirm pass is guarded by its own
  column and would otherwise hold for ever on a dead confirmation;
* `decide_early` condition 7 (src/jarvis/autoreview.py:995), identically. It can reach a
  dead early link on an order that is still `running`;
* the docstrings say what membership MEANS, in one sentence each, so the next reader does
  not have to infer that an id in the set is a question nobody will ever answer.

`Daemon._review_assumptions_of` (src/jarvis/daemon.py:5095) supplies it, once per work
order, beside the existing one-read-per-order block (`latest`, `answered`, `objecting`,
src/jarvis/daemon.py:5119-5126): for each row in `assumptions` carrying
`neo_question_id` or `confirm_question_id`, `neo_store.get(qid)` and keep the ids whose
`status == 'failed'`. The `neo_store` handle is already a parameter — `Daemon.auto_review`
opens it at src/jarvis/daemon.py:4959 on the thread that uses it — so this costs no
connection and at most two row reads per assumption per tick, on orders that have already
been asked about. Not derived inside `autoreview`: that module is pure by contract and
`ops._unreachable_asks` is the read-side twin for the surfaces.

Re-ask mechanics: an armed re-ask goes through `autoreview.propose` /
`propose_confirmation` unchanged, and `link_assumption_question` /
`link_assumption_confirmation` (src/jarvis/project_store.py:3915, :3961) are plain
UPDATEs, so the pointer moves to the new question and the old `failed` row stays in
`neo.db` as the record of the outage. Consequence, accepted: `assumption_for_question`
(src/jarvis/project_store.py:3919) stops resolving the old id — the back-link is a
pointer, not a history, and the timeline keeps both `autoreview_asked` events.

Loop safety: a re-ask that also goes unreachable spends the ladder again and lands
`failed` again, so the render returns to the "you decide it" line and the next tick's
`decide` re-arms at most once per tick per assumption. No event spam beyond one
`autoreview_asked` per re-ask, which is the honest record of a second attempt. Nothing
accelerates: the ladder and `MAX_ANSWER_ATTEMPTS` are untouched, so an outage measured in
hours produces a handful of attempts, not a spin.

### 5. `reclaim_stale`'s silent path

Refactor, do not duplicate. The `unreachable` closure inside `Daemon._neo_drain`
(src/jarvis/daemon.py:2964-2992) becomes one method:

```
def _note_question_unreachable(self, central: CentralStore, q: dict,
                              detail: str) -> None:
```

Body is today's closure verbatim — the inbox warning whose text states NOBODY HAS JUDGED
THIS and that this is not an escalation, plus `pstore.flag_attention` with
`invariants.neo_question_blocker({**q, "status": "failed"})` (that function, at
src/jarvis/invariants.py:1569, is what keeps the flag re-derivable by `true_blockers` —
kn-78346a2d). The project path comes from `self.catalog.projects` instead of the closure's
`paths` map. `_neo_drain`'s `unreachable=` callback becomes a one-line lambda onto it, so
the drain path is byte-identical in behaviour.

`Daemon.neo_tick` (src/jarvis/daemon.py:2864) then routes `stale["failed"]` through it:
for each id, `store.get(id)`, `detail` = `answer_reason` with `UNREACHABLE_PREFIX`
stripped, one `CentralStore` opened for the batch and closed in a `finally`. Inside the
existing `try`, so `neo_tick`'s connection hygiene is unchanged, and it must not raise
past the tick — wrap in `except Exception: log.exception` for `auto_review`'s reason: a
notification failure must not cost the drain.

No duplicate inbox rows: `reclaim_stale`'s UPDATE matches `status='answering'`, so an id
is returned in `failed` exactly once in its life. The drain path cannot double up either —
`release_claim` writes `failed` and returns `"unreachable"` on the same call. The two
paths are mutually exclusive by status.

## Rejected alternatives

* **Clear `neo_question_id` / `confirm_question_id` when a question fails.** The obvious
  fix, and it loses twice — see the two reasons at the top of the fix. Ruled out by Neo
  question 736.
* **Mirror question status into an `assumptions` column.** Drifts the first time the
  daemon dies between the two writes, which is `invariants.awaiting_neo`'s documented
  reason for reading cross-DB instead.
* **Let `autoreview.decide` open `NeoStore` itself.** Kills the purity the whole
  condition table's unit tests rest on (src/jarvis/autoreview.py:770).
* **Synthesise an `autoreview_escalated` event on failure.** Writes a decision Neo never
  made — the exact defect
  docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md removed for question
  388.
* **An invariant check that flips the surfaces.** A new `autoreview_*` event kind to
  overwrite the ask would need a branch in `AUTOREVIEW_EVENTS`, `_autoreview_line`,
  `assumption_ruling_line` and `ops.timeline` (kn-3f133363), and would still be a
  past-tense event making a present-tense claim — the same bug one layer along.

## Tests

1. `tests/test_autoreview.py` — an assumption linked to a `failed` question renders as a
   hold, not `awaiting ruling`: assert the new sentence and assert the string
   `awaiting ruling` is absent. Twice: once for the ask link, once for a `confirm: True`
   payload.
2. Same file — `ops.autoreview_state`'s line says `left with you` and names the question
   for an unreachable ask.
3. `tests/test_autoreview.py` / `tests/test_autoreview_confirm.py` — `decide`,
   `decide_confirm` and `decide_early` ARM when the link's id is in
   `unreachable_question_ids`, and HOLD `HELD_ASKED` / `HELD_CONFIRMING` when it is not.
4. Same — a `queued`, `answering`, `answered` or `escalated` link still holds: build the
   set from a real `NeoStore` row per status and assert it is empty for all four.
5. `tests/test_transport_resilience.py` — `reclaim_stale`'s `failed` ids produce the inbox
   warning (level `warning`, body containing `NOBODY HAS JUDGED THIS`) and the attention
   flag equal to `invariants.neo_question_blocker`; and running `neo_tick` twice over the
   same question adds exactly one inbox row.
6. `tests/test_waiting_on_neo.py:195` documents the silent path in prose — update it to
   assert the hook now fires.

## Out of scope

* An `answered` question whose verdict was never delivered (drain died between
  `record_answer` and `deliver`). Different root cause, different fix; nothing here
  touches it.
* Non-assumption kinds (`approval`, `plan`, `alarm`, `triage`) whose subject rows hold
  their own question pointers. §5 fixes their NOTIFICATION, which is the shared half; a
  gate or plan rendering a dead link as in-flight, if it does, is its own issue.
* Nothing changes in the retry ladder, `MAX_ANSWER_ATTEMPTS` or
  `STALE_ANSWERING_SECONDS`.
