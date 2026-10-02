# A confirmation is not re-run on a settled assumption

Work order wo-5e69e8af. GitHub issue #907. Read-side shape settled by Neo question 1196
(Option A) — §3.3 implements that decision and does not reopen it.

Sits on top of `docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-
them.md` §6 (the `confirm_spent` hold) and
`docs/superpowers/specs/2026-09-26-a-panel-gave-up-hold-says-which-round-and-stops-when-it-
passes.md` (c) (`ops._panel_hold_is_stale`, the one place a hold's freshness is derived).
Both are on `main`. Neither covers this case.

## The problem

### 1.1 What three real orders said

`jarvis wo show` named an assumption Neo had already ACCEPTED, called it the user's, and
hid the ones genuinely pending:

| order | summary line's subject | actual state of the rows |
|---|---|---|
| wo-764454c5 | "assumption 4 is yours … question 1138 … no longer open" | #4 was the ONLY accepted row; #1-#3 pending |
| wo-8fd40f1a | the same about #4 (question 1129) | #2 and #4 accepted; #1 and #3 pending |
| wo-9f00e3b5 | the same about #6 (question 1143) | #6 accepted |

The sentence is `autoreview.HELD_CONFIRM_SPENT`'s, verbatim from
`src/jarvis/autoreview.py:1011-1014`:

> assumption #{n} is yours — the OS asked Neo to confirm its early reading (question
> {confirming}) and that question is no longer open

It is true of exactly one state: a confirmation question that closed without settling the
row. On these three orders the question closed BY SETTLING THE ROW — accepted — which is
the mechanism succeeding, reported as the user owing a decision.

### 1.2 The chain, three layers deep

**(i) The parked pass considers settled rows.** `Daemon._review_assumptions_of`
(`src/jarvis/daemon.py:6189`) loops over everything `store.all_assumptions(wo["id"])`
returned — settled rows included. The candidate filter above it
(`Daemon.auto_review`, `src/jarvis/daemon.py:5945-5952`) only requires that SOME row of
the order is pending, so one pending assumption drags every accepted sibling through the
pass. The confirmation branch at `src/jarvis/daemon.py:6192` is gated on
`not early and str(a.get("provisional_verdict") or "")` and on nothing else — never on
`status == 'pending'`. An accepted row keeps its `provisional_verdict`, so it takes that
branch for ever.

**(ii) `decide_confirm` has no settled-row guard.** `decide`
(`src/jarvis/autoreview.py:831`, guard at `:909-911`) and `decide_early`
(`src/jarvis/autoreview.py:1043`, guard at `:1099-1101`) both open with
`status != 'pending'` returning `_held(HELD_SETTLED, …)`. `decide_confirm`
(`src/jarvis/autoreview.py:939`) does not: it runs its own four gates FIRST and reaches
`decide` in full only at its tail call (`:1035-1040`). The second of those gates is
`confirm_question_id` already set (`:1009-1017`), and on an accepted row that id IS set and
the question is no longer open — so the pass short-circuits to `HELD_CONFIRM_SPENT` and
`decide`'s condition 3 is never reached. Every tick after the confirmation is accepted
writes that hold.

**(iii) The read side resolves no hold against the row.** `ops.autoreview_state`
(`src/jarvis/ops.py:3934`) takes the newest event by timestamp. It resolves a present-tense
claim against the assumption's current row for `_AUTOREVIEW_OWED_BY_USER` only
(`src/jarvis/ops.py:3931`, `autoreview_escalated` + `autoreview_unconfirmed`;
`src/jarvis/ops.py:3984-3989`) — not for `autoreview_held`. `autoreview_held`'s only
freshness filter is `_stale_panel_hold` (`src/jarvis/ops.py:4186`), whose codes tuple is
`(HELD_PANEL_GAVE_UP, HELD_STATUS)` (`:4200`). A `confirm_spent` hold therefore survives
every filter and, being newest, becomes the summary line.

### 1.3 Root cause, named

kn-a2ebbbdb exactly: a present-tense claim derived from an immutable timeline event. The
general fix is a `holds` table re-derived per reconcile tick, which the 2026-09-26 spec
already rejected as scope and this spec does not revisit. What IS in scope is that the
claim's subject here — the assumption row — is a queryable row with a `status` column, so
its freshness is derivable for free, and the two write-side layers that manufacture the
claim are both one condition short.

## The fix

Three parts. None of the three is sufficient; §3.4 says why each is load-bearing.

### 3.1 The parked pass skips rows that are not pending

In `Daemon._review_assumptions_of`, inside the `for a in assumptions` loop
(`src/jarvis/daemon.py:6189`) and BEFORE the confirmation branch at `:6192`:

```python
            if str(a.get("status") or "") != "pending":
                continue
```

It governs the whole loop body, not just the confirmation branch: the ask branch calls
`decide`, which would hold `HELD_SETTLED` on the same row and spend the per-row work
(`_question_liveness` is already hoisted, but `_stakes_reviewed` is not) to produce a hold
nobody records.

**NOTHING LEAVES THE TIMELINE BY SKIPPING.** `HELD_SETTLED` is in the `shared` tuple of
`_holds_not_recorded` (`src/jarvis/daemon.py:439`), so the hold a settled row would have
produced was already suppressed on both passes. The skip changes no recorded event; it
removes the row from a pass that has nothing to do on it, and it is what stops the
`provisional_verdict` branch being entered on a row whose verdict was already honoured.

The candidate filter at `src/jarvis/daemon.py:5945-5952` is NOT touched. Its job is "does
this order have anything to do", which is still true of an order with one pending row, and
tightening it per-row there would duplicate the test this pass now makes.

### 3.2 `decide_confirm` opens with the guard its two siblings have

First statement after the `fields` dict is built in `decide_confirm`
(`src/jarvis/autoreview.py:1003-1005`), ahead of all four gates:

```python
    if str(assumption.get("status") or "") != "pending":
        return _held(HELD_SETTLED,
                     f"assumption #{n} is already {assumption.get('status')}", **fields)
```

Byte-for-byte the clause `decide` carries at `:909-911`. It must come BEFORE the gates, not
after: the `confirm_question_id` gate at `:1009` is what short-circuits today, so a guard
placed after it never runs on the defect's own path.

This is the layer that holds whoever the caller is. `decide_confirm` is pure and public, and
`Daemon._deliver_assumption_verdict` re-runs the decision functions against freshly read
state immediately before accepting (`decide`'s docstring, `src/jarvis/autoreview.py:880-882`)
— a second call site, with its own race window, that must get the same answer. A function
whose correctness depends on its one current caller filtering its input is a defect waiting
for a second caller.

### 3.3 One code-agnostic staleness clause, in `_panel_hold_is_stale`

Ruled by Neo question 1196, Option A. `ops._panel_hold_is_stale`
(`src/jarvis/ops.py:4143`) gains a clause that fires on ANY `autoreview_held` code: a hold
whose `assumption_id` names a row that is no longer `pending` is stale. It needs the row's
status, so the signature grows one argument — the assumption row (or `None`) — beside the
round it already takes, and the clause is tested before the per-code ones:

* a payload with no `assumption_id` is an ORDER-LEVEL hold (`HELD_DISABLED`, `HELD_STATUS`,
  `HELD_PANEL_GAVE_UP` as written by the order-level sites) and is unaffected — the clause
  cannot answer about a row that was never named;
* a payload naming a row whose `status` is not `pending`: stale, whatever the code;
* otherwise: today's two clauses, unchanged, including the legacy-payload reading the
  2026-09-26 spec §(c) settled.

Code-agnostic, and deliberately so. `confirm_spent` is the one measured here, but every
per-assumption hold code makes a claim about a row — `high_stakes`, `objected`,
`refusal_unanswered` — and each one is equally untrue once that row is decided. Enumerating
codes would mean a new hold code silently inherits the defect.

`_stale_panel_hold` (`src/jarvis/ops.py:4186`) is where the read happens, and its laziness
discipline is kept exactly:

* the `codes` early return at `:4200` can no longer reject every other code outright — it
  becomes the gate on the ROUND/STATUS clauses only, after the assumption clause;
* the assumption row is read only when a hold carrying a non-zero `assumption_id` actually
  turns up, via `store.get_assumption(aid)`, so an order with no per-assumption hold pays
  nothing;
* the cache is keyed BY `assumption_id`, not the single-slot `cache["status"]` the round and
  the work-order status use. `assumptions_with_rulings` builds one `stale` closure per order
  (`src/jarvis/ops.py:4295`) and runs it over every row's events, so a single slot would
  answer the second assumption with the first one's status.

Both readers of the helper get the fact from this one authority: `autoreview_state`
(`src/jarvis/ops.py:3955`, 3963-3965) and `assumptions_with_rulings`
(`src/jarvis/ops.py:4295`, 4307-4308). A second derivation inside `autoreview_state` —
extending `_AUTOREVIEW_OWED_BY_USER` to cover `autoreview_held`, say — is GitHub issue
#712's "derived here and once" rule broken, and it would leave the assumption LIST still
rendering the stale sentence while the summary line had stopped.

What "dropped" means is already defined by each reader and does not change:
`autoreview_state` walks the kind newest-backwards to the first surviving row and
contributes no candidate when all are stale, so the line describes a genuinely pending
assumption, or is `None` when the order has nothing else; `assumptions_with_rulings`
`continue`s the payload, so the row falls back to its next-best event and to
`os_ruling = None` when the hold was the only one.

One consequence, accepted: a SETTLED row whose only `autoreview_held` was its last event now
renders as nothing-having-looked-at-it rather than held. That is the 2026-09-26 spec's own
trade — not-looked-at understates, the stale sentence lies — and it costs nothing here,
because what the mechanism DID to a settled row is an `autoreview_accepted` /
`autoreview_confirmed` event, which is not an `autoreview_held` and is not touched, and
because the attribution a settled row is read for lives on `assumptions.decided_by`.

### 3.4 Why all three, and not one

* §3.1 alone: the pass stops producing the event, and the three orders already carrying it
  keep saying it — there is no later event to overtake it, because the pass that would have
  written one now skips the row. It also leaves `decide_confirm` wrong for its second caller.
* §3.2 alone: the same, plus it is a hold code (`HELD_SETTLED`) that `_holds_not_recorded`
  suppresses, so the pass would go on paying per-row work to produce a suppressed hold.
* §3.3 alone: the bogus event keeps being written every tick and is merely hidden. Hiding is
  the honest answer for an event already on an append-only timeline; it is the wrong answer
  for one the OS is still manufacturing, and `jarvis validation show` and the timeline would
  fill with it.

Write side and read side fix different failures: §3.1 and §3.2 stop NEW bogus events, on the
two layers that each need it for their own reason — the pass that should not consider the
row, and the pure function that must never answer about a settled row whoever calls it. §3.3
is what repairs the three orders already carrying the event, and any hold whose row settles
after the hold was written, which no write-side change can reach.

## Rejected alternatives

* **Only the read side (§3.3).** The cheap fix, and the one a reviewer will propose because
  it alone repairs the three observed orders. Rejected: the daemon would keep writing a
  false event every tick for the life of the order, and `decide_confirm` would stay a pure
  function that returns a wrong answer on a settled row.
* **Only the write side (§3.1 + §3.2).** Leaves wo-764454c5, wo-8fd40f1a and wo-9f00e3b5
  saying it for ever, and every order whose row settles between the hold being written and
  being read.
* **Tighten the candidate filter at `src/jarvis/daemon.py:5949` to skip settled rows.** It
  already does (`a["status"] == "pending"`) — for the purpose of deciding whether the ORDER
  is a candidate. The rows it passes on are deliberately `all_assumptions`, because the
  question a confirmation asks carries the siblings (`autoreview.propose_confirmation`'s
  `assumptions` argument, `src/jarvis/daemon.py:6232-6233`) and a settled sibling is context
  the question needs. Filtering the list would silently change what Neo is shown.
* **Make the `confirm_spent` wording conditional — "unless the row was accepted".** Patches
  one sentence of one code at the write side, leaves the event written, and leaves the other
  per-assumption hold codes with the same latent claim.
* **Resolve `autoreview_held` inside `autoreview_state`'s `_AUTOREVIEW_OWED_BY_USER`
  branch.** Two readers deriving freshness apart: issue #712 verbatim, and
  `assumptions_with_rulings` exists because of it.
* **Delete or rewrite the stale events on the three orders.** The timeline is append-only
  and the hold did happen. The claim is withdrawn; the history stays.
* **A `holds` table re-derived per tick.** The real root cause for all hold codes, rejected
  as scope by the 2026-09-26 spec and still out of scope: schema change, migration and a new
  reconciler post-condition, for a claim whose subject is already a queryable row.

## Tests

The fixture every new test needs is one order at `needs_review` where Neo ACCEPTED one
assumption through the confirmation path while others are still pending — which is the three
observed orders' shape and which no existing test builds. Home: `tests/test_autoreview_
confirm.py` for (i) and (ii), beside the `HELD_CONFIRM_SPENT` assertions at `:105-113`;
`tests/test_autoreview.py`'s surfaces section for (iii).

1. **The parked pass writes no hold about the accepted row.** Accept one assumption via the
   confirmation path, leave two pending, run the pass: no `autoreview_held` event whose
   payload `assumption_id` is the accepted row's — and, in the same test, the pending rows
   are still processed (an event about one of them, or the pass's own ask), so the test says
   the skip is per-row and not a dead pass.
2. **`decide_confirm` returns `HELD_SETTLED` on a settled row.** A pure call, no daemon: a
   row with `status='accepted'`, `provisional_verdict='accept'` and a `confirm_question_id`
   whose question is no longer open (`confirmation_open=False`) — the exact input that
   produces `HELD_CONFIRM_SPENT` today. Assert `code == HELD_SETTLED`, and keep a sibling
   call with `status='pending'` asserting `HELD_CONFIRM_SPENT` still fires, so the test says
   where the guard does NOT reach.
3. **`autoreview_state` skips an already-written `confirm_spent` hold.** Write the hold by
   hand about the accepted row — this is the three orders' stored state and the only way to
   reproduce it once (1) and (2) ship — then assert the returned line describes a genuinely
   pending assumption, or is `None` when nothing else survives. Assert the
   `autoreview_held` event is still on the timeline: the claim is withdrawn, the history
   kept.
4. **`assumptions_with_rulings` drops the same hold, and the cache is per assumption.** One
   order, a stale hold about an accepted row AND a live hold about a pending row: the
   accepted row's `os_ruling` is not the hold, the pending row's IS. This is the test that
   fails if §3.3's cache keeps a single status slot.
5. **An order-level hold is unaffected.** A payload with no `assumption_id` (a `disabled`
   or order-level `panel_gave_up` hold) still renders while its order-level condition holds.

Existing tests to check rather than assume: `tests/test_autoreview_confirm.py:105-113`
asserts on `HELD_*` codes with a pending row, so §3.2 does not touch it; anything asserting
that the confirmation branch runs over `all_assumptions` must be read against §3.1's skip
before it is changed, because a test that built its fixture with a settled row carrying a
`provisional_verdict` was encoding the defect.
