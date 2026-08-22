# The message bus (`src/jarvis/bus.py`)

Added by wo-59eb924c (PR 118), the first piece of the validation-panel feature
(`docs/superpowers/specs/2026-08-08-validation-panel-design.md`). It had no callers when
it landed — later work orders in that feature post through it.

**The rule it exists for:** nothing addresses anything directly. A work order does not
talk to the project manager; a validation panel does not talk to a work order. Every
cross-entity message is an *envelope* posted to a ROLE about a SUBJECT (exactly one of a
work order or a feature order), and a router resolves who fills that role. The sender
never names a recipient and never learns who read it. A new participant costs a routing
rule, not an edit to everything that might want to reach it.

## Shape

| where | what |
|---|---|
| `project_store.SCHEMA` | the `envelopes` table (per-project DB). One CHECK only: exactly-one-of-two-parents |
| `project_store.ENVELOPE_ROLES / _KINDS / _STATES` | the vocabulary, as module tuples — **not** SQL CHECKs, because roles and kinds are designed to grow |
| `project_store.post_envelope / queued_envelopes / mark_envelope / bump_envelope_attempt / deliver_envelope / envelopes / manager_work_order` | every SQL the bus uses |
| `bus.Subject / ReviewFeedback / DeferralRequest / PAYLOADS / KINDS` | the typing at the boundary; `parse_payload` rebuilds a stored row |
| `bus.post / resolve / render / deliver` | queue, route, hand off |
| `daemon.Daemon.deliver_envelopes` | turns the queue each tick, just BEFORE `deliver_messages` |
| `invariants.check_envelopes_move` | INV-ENVELOPE-STUCK |
| `tests/test_bus.py` | 21 tests, including an AST layering check |

## The routing table, and it is the whole of it

| `to_role` | resolves to |
|---|---|
| `implementor` | the subject work order itself |
| `manager` | the `kind='manager'` work order under the subject's feature |

A work order in a TERMINAL status fills no role: it has no live session to resume.

## Facts that are easy to break

* `post` has **no `kind` parameter** — kind is derived from `type(payload)`, which makes a
  kind that disagrees with its payload unrepresentable rather than validated.
* `delivered_wo_id` is written by the ROUTER only; `post_envelope` has no parameter for it.
* **The router decides what happens when a role is unfilled**, never the sender: a
  `deferral_request` with no manager is filed as a backlog item by the router itself
  (today's behaviour); `review_feedback` with no implementor is `undeliverable`; a
  cancelled manager under an open feature is `undeliverable` + attention on the feature.
* Delivery rides the existing message queue (`queue_message`) — there is no second
  dispatch path. The insert and the state change are ONE transaction (`deliver_envelope`).
* `attempts` is bumped in its own autocommitted statement BEFORE that transaction, or a
  rollback would take the count with it and INV-ENVELOPE-STUCK would never fire.
* Layering: `bus.py` imports `project_store` and `central_store` and nothing above them.
  A test walks its AST (including function-body imports) to keep it that way.
* `manager` is in `WO_KINDS` (added by the validation layer's vocabulary, wo-3ce42dc7)
  but nothing CREATES one yet, so `manager_work_order` returns None in practice and the
  router treats it as an unfilled role. NOTE kn-52e51faf: `count_active` still has no
  kind filter, so the day a manager order is actually created it will spend a
  concurrency slot for the life of its feature.
