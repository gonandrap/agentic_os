# Every autoreview claim is re-derived from its row

Issue #814. kn-96f47efb, kn-7e95aece.

## Problem

`ops.autoreview_state` and `ops.assumptions_with_rulings` render present-tense sentences
read back off append-only `autoreview_*` events. Four read-side patches filtered some of
them; nine still outlive the state they describe (`is with Neo`, `while the worker runs`,
`told the worker`, and the `autoreview_held` codes `asked`, `confirming`, `settled`,
`judged_early`, `refusal_unanswered`, `objection_in_flight`).

## Fix: one re-derived table

`ops._stale_ruling(store, wo_id)` replaces `_stale_panel_hold`. It answers, for any
`(kind, payload)`, whether the event's claim is FALSE of the current rows, reading each
row lazily and once. Both read sites drop a stale event for EVERY kind, not just
`autoreview_held`; an older true event, or none, is shown instead.

| Claim | Stale when |
|---|---|
| any hold, `asked`, `provisional`, `objected` | the assumption is no longer `pending` |
| hold `status`, `panel_gave_up` | unchanged (`_panel_hold_is_stale`) |
| hold `asked` | row's `neo_question_id` empty, or its question not `queued`/`answering` |
| hold `confirming` | same, on `confirm_question_id` |
| hold `judged_early` | row has no `provisional_verdict` |
| hold `refusal_unanswered` | `ops.refusal_answered` is now true |
| hold `objection_in_flight` | `store.outstanding_objections` is empty |
| `autoreview_asked` | row no longer links that question, or the question left Neo (`failed` stays: the unreachable sentence needs it) |
| `autoreview_provisional` | row has no `provisional_verdict` |
| `autoreview_objected` | row withdrawn, or `invariants.objection_undeliverable` |

An unreadable `neo.db` or a missing question is not evidence: the claim stands.

The provisional sentence says `before the worker delivered`, true while it runs and after,
instead of `while the worker runs`.

## Dedupe

No payload key becomes a freshness key: every check reads a current row, so
`Daemon._note_autoreview_held`'s dedupe key is unchanged (the kn-96f47efb trap does not
apply). A hold the read side drops and that later becomes true again renders again.

## Not covered

Codes `high_stakes`, `evidence_secret`, `disabled`, `unjudged`, `objected`, `confirm_spent`,
`round_open` keep their present behaviour.
