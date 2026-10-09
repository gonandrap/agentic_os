# An undeclared delivery must heal itself

Issue #976, work order wo-f812739c. Closes the half of §2c of
docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md that shipped
a detector and no actor. Reuses the machinery of
docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md and
docs/superpowers/specs/2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md.
kn-681db233 point 3 applies (no count, no sha, no clock in a blocker sentence).

## 1. The problem

**The OS detects an undeclared delivery and nothing acts on it.** §2c shipped
`invariants.undeclared_delivery` (invariants.py:1221-1240) and
`invariants.check_undeclared_delivery` (:1243-1273). The predicate is right: the user
refused an assumption, so `ops.refusal_answered` (ops.py:5235) is False; the worker pushed,
so `wo["pr_head_oid"]` is not in `judged_heads` (:1178). The checker then writes a finding
and yields a `Violation` whose `repair` string is literally
`"raised a finding for the supervisor to judge"`. That is the whole of the response.

The supervisor cannot act either. The finding reaches `remedies.propose(..., "nudge")`,
which files a gate request; `catalog.RemedyConfig` (catalog.py:1307) "Ships with both off"
— `enabled` false AND `allowed` empty — on every project, and the `nudge` remedy needs a
live `self_heal` grant on top. So on the shipped fleet the path terminates in a Neo
question and then the user. Live record: the supervisor reported *"Nothing in my vocabulary
re-declares a delivery"* and escalated.

Meanwhile the sentence the user reads already claims the OS acted.
`autoreview.REFUSAL_UNDECLARED_REASON` (autoreview.py:159-161) ends **"— the OS has asked
it to declare them"**, rendered by `decide` (:917) and `decide_early` (:1121). Nothing has
asked it. tests/test_autoreview.py:138 asserts that clause is in the string, so the lie is
pinned.

Terminal state: the order sits in `needs_review` for ever, holding an unjudged head, a held
assumption and a false explanation. `ops.land_when_cleared` (ops.py:5215-5220) re-parks it
every time anything re-settles it.

Root cause, stated as §2c's own class does: **the OS has a read predicate and no actor for
it.** A condition the OS can name, whose remedy is one message to a session it still holds,
must not route to a person.

## 2. The fix

**A third `ops.PrRepair`.** The repair is "ask this worker to run `jarvis wo finish`", which
is one message to an idle session with an attempt budget — exactly what `PrRepair` is.
`PrRepair`'s own docstring states the lesson of issue #224: the CI half was missing *because
it looked like new machinery*. Everything this needs already exists — the attempt cap, the
five guards in `Daemon.heal_pull_request` (daemon.py:7708), the episode arithmetic in
`ProjectStore._this_episode` (project_store.py:3700), the unauthored message source, the
give-up flag, `pr_repair_origin`, the clear-on-recovery.

Rejected alternatives:

1. **Turn the `remedies` nudge on.** Loses on three counts: it needs a per-project config
   change plus a standing `self_heal` gate grant to act at all; the remedy fires off the
   supervisor's LLM judge, so the cure costs a model call per tick where the poll costs an
   indexed read; and it has no episode, so nothing bounds re-nudging or escalates a worker
   that ignores it.
2. **Widen `refusal_answered` to count commits.** Explicitly refused by §2c: the finish IS
   the declaration, and a panel judging a submission nobody declared is worse than the
   stall.
3. **Have the invariant queue the message itself.** `check_*` functions must stay readable
   under the read-only doctor proxy, and a nudge needs `session_id`, `queued_messages`,
   `worker_session.busy`, the open-round check and the gate check — all of which already
   live in `heal_pull_request` and none of which belong in `invariants`.
4. **A new attention blocker and no nudge.** That is today's behaviour with better wording.
   The pinned ruling is self-healing, not a clearer stall.

### 2.1 `invariants` — the names and the pairing

```python
PR_UNDECLARED_REPAIR = "undeclared"
PR_UNDECLARED_BLOCKER = ("the worker pushed commits answering your refusal and would not "
                         "declare them — nothing has been judged, so decide what to do "
                         "with the branch as it stands")
PR_REPAIR_SOURCES = (f"pr-{PR_CONFLICT_REPAIR}", f"pr-{PR_CHECKS_REPAIR}",
                     f"pr-{PR_UNDECLARED_REPAIR}")
PR_REPAIR_BLOCKERS = ((PR_CONFLICT_REPAIR, PR_CONFLICT_BLOCKER),
                      (PR_CHECKS_REPAIR, PR_CHECKS_BLOCKER),
                      (PR_UNDECLARED_REPAIR, PR_UNDECLARED_BLOCKER))
```

Blocker sentence carries no count, no sha and no elapsed time: `ack_attention` stores it
verbatim and INV-ATTENTION-REASON compares it.

Ranked LAST in `PR_REPAIR_BLOCKERS`. A branch that will not merge, and a branch that is
red, are facts about the artifact; this is a fact about the paperwork over an artifact that
may be fine. `true_blockers` feeds `attention_reason` from `blockers[0]` (kn-d4d5a967), and
of the three this is the one to say second.

`PR_REPAIR_STATUSES` is NOT widened. It is `("waiting_pr_merge", "needs_review",
"waiting_input", "failed")` (:204) and `UNDECLARED_DELIVERY_STATUSES` is `("needs_review",
"waiting_pr_merge", "waiting_input")` (:1211) — a strict subset. So the pairing invariant
stated beside `PR_REPAIR_STATUSES` (every status the poll nudges in is one `true_blockers`
derives in) holds with no edit, and the new blocker is derivable in every status its
detector can fire in. `failed` is in the wider set and the detector never selects it; that
costs one read and derives nothing, which is already true of the other two.

### 2.2 `ops` — the template, and the one way this repair differs in kind

```python
PR_UNDECLARED = PrRepair(invariants.PR_UNDECLARED_REPAIR, PR_UNDECLARED_NUDGE,
                         invariants.PR_UNDECLARED_BLOCKER)
PR_REPAIRS = (PR_CONFLICT, PR_CHECKS, PR_UNDECLARED)
```

`PR_UNDECLARED_NUDGE` is the only part of this work order that is not a copy. The other two
nudges end **"do NOT call `jarvis wo finish` again"** — those work orders already declared
themselves and a second finish would spend a round. This one's entire purpose is that the
worker MUST call it. The template says, in the shape the other two use (what is wrong, what
to do, what not to do, attempts left):

- the user REFUSED an assumption on this work order (name the refusal, not the branch: the
  worker's own reading of why it was woken has to match the record);
- you pushed commits since, and the head is one nothing has declared, so the panel may not
  judge them and nobody is reading them;
- run `jarvis wo finish <wo-id> --summary "..." --pr <url> --evidence "..."` — say in the
  summary what the refusal asked for and what you changed;
- then END YOUR TURN. Do not push anything new to answer this message: the declaration is
  the whole ask;
- `This is attempt {attempt} of {max_attempts}.` — same trailer, via
  `nudge_pr_repair`'s `attempt`/`max_attempts` fields, so no new formatting contract.

**It concatenates `ops.TARGETED_TESTS_LINE`.** Decided, not open. Two reasons, either
sufficient: (a) tests/test_repair_resettle.py:432
`test_every_repair_nudge_asks_for_targeted_tests_only` and :440
`test_every_repair_nudge_still_renders` are parametrized over `ops.PR_REPAIRS`, so a third
descriptor is covered the moment it exists and a template without the line fails; (b) on
the merits — `jarvis wo finish --evidence` asks the worker what it ran, so this nudge
touches the targeted-tests ruling (kn-932c7f0e, kn-356c724b), and the dispatch brief that
states the same rule is NOT inherited into a turn the OS opens. Place it in the
`--evidence` clause ("state what you ran; " + `TARGETED_TESTS_LINE`), not in a "fix it"
clause this repair does not have.

`PR_REPAIR_SOURCES` is not optional: tests/test_repair_resettle.py:310 asserts
`invariants.PR_REPAIR_SOURCES == tuple(r.source for r in ops.PR_REPAIRS)`.

### 2.3 `daemon.poll_pull_requests` — an independent block, not another `elif`

The conflicting/failing/green chain is a decision about the pull request's state and is
mutually exclusive by construction. An undeclared delivery is orthogonal: it can be true
over a green PR, a red one or a conflicting one. So it goes AFTER that chain, inside the
same `try`, still inside the `wo["status"] in PR_REPAIR_STATUSES` bound (the chain's first
arm, daemon.py, `elif wo["status"] not in PR_REPAIR_STATUSES: pass`, already excludes
`validating` by falling through to nothing — the new block must repeat the status test
explicitly rather than rely on position, since the `merged`/`closed_unmerged` arms return
through the same bottom).

```
# after the conflict/checks/green chain, same try:
if wo["status"] in PR_REPAIR_STATUSES and invariants.undeclared_delivery(store, row):
    self.heal_pull_request(project, store, row, ops.PR_UNDECLARED,
                           "delivered without declaring it")
elif wo["status"] in PR_REPAIR_STATUSES:
    ops.clear_pr_repair(store, row, ops.PR_UNDECLARED)
```

`row` is NOT `wo`. The head cache is written earlier in the loop with
`store.update_work_order(wo["id"], pr_head_oid=pr.head_oid, ...)` and the local `wo` dict
is never refreshed, so `undeclared_delivery` reading `wo["pr_head_oid"]` would judge the
PREVIOUS head — on the first tick after a push, the very tick this exists for, it would read
the already-judged head and decline. Pass `{**wo, "pr_head_oid": pr.head_oid or
wo.get("pr_head_oid") or ""}`. An overlay, not a re-read: a `get_work_order` here would buy
a row lookup on every polled pull request in the fleet.

**Order and mutual exclusion matter, and they are bought by an existing guard, not by an
`elif`.** `heal_pull_request` returns early on `store.queued_messages(wo["id"])`. A red or
conflicting pull request is nudged by the chain above, which queues a message, so this block
calls `heal_pull_request` and it declines in the same tick — no double nudge, no attempt
spent, nothing said twice. The undeclared nudge lands on the first tick where the build is
green and no repair message is queued. That is the right order: a worker asked to declare a
head that CI is about to reject would declare the wrong commit.

The same guard means the two episodes never interleave their messages, and because each
episode counts its own attempts (`_this_episode` keys on the repair name), a conflict that
burned three attempts leaves this one's budget untouched.

### 2.4 `check_undeclared_delivery` stops raising while the OS is handling it

The finding is the GIVE-UP's. `nudge_pr_repair` already flags `repair.blocker` past
`PR_REPAIR_MAX_ATTEMPTS` (ops.py:6854-6860), so a user told twice — once by a finding, once
by the blocker — is told the same thing by two mechanisms with different ack paths.

Exact predicate, inserted after the existing `if not undeclared_delivery(...): continue`:

```python
# THE OS IS ON IT: the nudge is the response, and the give-up is what reaches the user.
if wo.get("session_id") and not store.pr_repair_gave_up(wo["id"],
                                                        PR_UNDECLARED_REPAIR):
    continue
```

Two clauses, each load-bearing:

- `session_id` — `heal_pull_request` returns on `if not wo.get("session_id"): return`
  before anything else. An order whose worker session is gone can never be nudged, so
  suppressing the finding there would be silence for ever. This clause is also what keeps
  the shipped §2c tests passing unchanged: `_refused_then_pushed`
  (tests/test_invariants.py:1544) sets no `session_id`, so
  `test_the_detector_raises_a_finding_once_and_never_an_alarm` (:1599) and
  tests/test_remedies.py:443 still see the finding.
- NOT gave-up — `pr_repair_gave_up` is "has the OS said it is stopping", not "are the
  attempts spent" (project_store.py:3757), and it is episode-scoped, so a cleared-then-
  reopened case starts silent again.

Deliberately NOT in the predicate: attempts `== 0`. An order with a session and a live
detector is nudged on the next poll tick (two minutes), and a finding raised in that window
would be raised and then be answered by the OS, which is the noise the whole change removes.

Deliberately NOT handled: a repair deferred indefinitely by a pending gate
(`defer_pr_repair`). No give-up is written, so this stays silent for as long as the review
takes. That is `defer_pr_repair`'s own ruling — the attention a gate deserves is the gate's,
and an escalated gate is already an item.

`autoreview.REFUSAL_UNDECLARED_REASON` is left exactly as it is. Its closing clause "— the
OS has asked it to declare them" becomes TRUE when this ships; it is the reason the string
was written that way, and tests/test_autoreview.py:138 keeps it.

### 2.5 End to end, link by link

1. Poll tick: detector true, PR green, nothing queued, no open round, no pending gate,
   session present. `heal_pull_request` -> `nudge_pr_repair` queues the message under source
   `pr-undeclared` (rendered as Jarvis, not the user, by `timeline.UNAUTHORED_SOURCES`) and
   writes `pr_undeclared_nudged` with `was=<status>`, usually `needs_review`.
2. Next tick delivers it; the worker resumes its existing session and runs
   `jarvis wo finish`.
3. `ops.finish` (ops.py:6348) writes the `finished` event at :6436 — BEFORE it settles
   anything, which is the documented ordering `refusal_answered` depends on. From that
   instant `refusal_answered` is True and `undeclared_delivery` is False.
4. `validation_applies` true -> `submit_for_validation` (:5399) opens a fresh round over the
   NEW head. `user_rework_pending` (:5468) is True here by construction — the refusal is
   newer than the newest settled round — so the round is `uncounted` with
   `USER_REWORK_CAUSE` and spends no budget.
5. **`max_rounds` cannot refuse it.** Nothing about opening a round consults
   `cfg.max_rounds`: it is a settle-time branch in `Daemon._validate_work_order`
   (ops.py:5625-5630 states this). The only open-time refusal is the BOUNCE
   (`unanswered_paths`), which needs a previous round that named paths; a declaration after
   a USER refusal can hit it, and if it does the existing `BOUNCE_LIMIT` ceiling escalates
   to the user with a round on record. Unchanged behaviour, stated so nobody adds a second
   bound.
6. `land_when_cleared` parks it `validating`.
7. `Daemon.settle_work_order`'s `if wo["status"] == "validating": return` (daemon.py:5037)
   fires before the branch that reads `ops.pr_repair_origin`. **So the episode's
   `was=needs_review` can never clobber the round**: the only reader of the origin snapshot
   is unreachable while the round is open, and by the time the round settles the episode is
   cleared (step 8) and `pr_repair_origin` returns None.
8. Next poll tick: detector false -> `clear_pr_repair(store, row, ops.PR_UNDECLARED)` writes
   `pr_undeclared_cleared`, takes down the give-up flag if and only if
   `attention_reason == PR_UNDECLARED_BLOCKER`, and calls `resettle_after_repair`.

**Validation OFF on the project:** `validation_applies` false, so `finish` calls
`land_when_cleared(..., panel_cleared=True)`. The refused assumption is settled, not
pending, so `pending_assumptions` is empty; `refusal_answered` is now True; no round is
read; `land_finished` parks it `waiting_pr_merge` with the pull request on the open list.
The declaration still works and the episode still clears on the next tick. The difference is
only that nobody judges the new head, which is what that project asked for.

**PR closed unmerged mid-episode:** `ops.complete_pr_closed` already loops
`for repair in PR_REPAIRS: clear_pr_repair(...)` (ops.py:6714). Membership in `PR_REPAIRS`
is the whole of that fix; no edit there.

### 2.6 The green-pull-request read budget

Hard constraint. `poll_pull_requests`' docstring states the per-pull-request cost in prose
and tests/test_pr_checks.py:266
`test_a_green_pull_request_costs_one_call_four_reads_and_no_write` reads the statements off
the connection.

The new block's reads, in order, on a GREEN pull request:

1. `invariants.undeclared_delivery` -> `ops.refusal_answered` ->
   `events_of_kind(wo_id, "reviewed")`. **One `wo_events` read, and on a work order that has
   never had a refusal it returns True immediately and the function stops.** This is why the
   cheap short-circuit is first: no `finished` read, no `judged_heads`, and so no
   `validation_rounds` read on the common path.
2. `ops.clear_pr_repair` -> `pr_repair_attempts` -> `_this_episode` ->
   `events_of_kind(wo_id, "pr_undeclared_nudged")`. One `wo_events` read; the second read
   inside `_this_episode` happens only when rows came back, so an order never nudged pays
   one.

Counts to write into the spec, the docstring and the tests:

- **Common case — green PR, no refusal ever: 5 reads become 7.** Both new reads are
  `wo_events`; the non-`wo_events` statement count is unchanged at 1.
- **Refused and already declared (the recovered case): 8.** `reviewed` + `finished` +
  the episode read; still no `validation_rounds` read, because `refusal_answered` returns
  True before `judged_heads`.
- **Refused and undeclared:** `reviewed` + `finished` + `pr_merged` = 3 `wo_events`, plus
  ONE `validation_rounds` read inside `judged_heads`, and then the nudge writes. Not a green
  no-write tick, so it is outside the budget this test pins.

Edits required:

- The docstring sentence beginning **"THE LAST ONE IS THE BUDGET, because it is the
  overwhelmingly common case: one `gh` call and FOUR indexed reads per pull request"** and
  the list that follows ("...and is a 'waiting for the base' note still up") become SIX,
  with the two new questions named: *is this delivery undeclared*, and *is an undeclared
  episode open*. The following paragraph, which explains that the sentence once said "one
  indexed read" and had drifted, stays — it is the reason the count is kept honest.
- The sixth read is paid by EVERY polled pull request, including a project with no refusals
  anywhere, and the spec says so plainly rather than claiming it is conditional: there is no
  cheaper way to ask "has this order ever had a refusal" than the `reviewed` read.
- tests/test_pr_checks.py:302 `== 5` -> `== 7`, and the test NAME
  (`test_a_green_pull_request_costs_one_call_four_reads_and_no_write`) ->
  `..._six_reads_...` with its docstring's "four" updated.
- tests/test_pr_checks.py:334 `== 4 * 2 + 1` -> `== 6 * 2 + 1`, and the docstring
  arithmetic sentence `4 * pull requests + 1` -> `6 * …`. This is the test that proves the
  new reads are per pull request and not per step, so it must move with the other.
- tests/test_pr_checks.py:359 (`test_the_automatic_merge_costs_a_project_that_has_not_opted
  _in_nothing`) `== 5` -> `== 7`; its `not [s for s in sql if "validation_rounds" in s]`
  assertion is the one that proves the short-circuit ordering and must stay.
- tests/test_pr_checks.py:400 (`test_an_opted_in_project_declares_what_the_automatic_merge_
  costs_it`) `== 6` -> `== 8`; `validation_rounds == 1` and `assumptions == 1` unchanged —
  the new block adds neither, again because of the short-circuit.
- tests/test_pr_checks.py:506 (`test_an_opted_in_project_pays_nothing_for_an_order_awaiting_
  a_person`) `== 5` -> `== 7`; line 507's "no `validation_rounds`" holds only because the
  `reviewing` fixture has no refusal, and the docstring should say so.

## 3. Tests

New, in tests/test_repair_resettle.py unless noted:

1. **Detector true, green PR -> nudge.** A refused-then-pushed order WITH a `session_id`:
   one poll queues a message with source `pr-undeclared`, a `pr_undeclared_nudged` event
   with `was="needs_review"`, and the message contains `jarvis wo finish` and
   `--evidence`.
2. **Three attempts then give-up.** Three polls (clearing the queued message between) spend
   1/2/3, the fourth writes `pr_undeclared_unresolved` once and flags
   `PR_UNDECLARED_BLOCKER`; `true_blockers` derives that string, and a fifth poll adds
   nothing.
3. **Declaration clears it.** After the nudge, run `ops.finish(..., pr_url=...)`; the next
   poll writes `pr_undeclared_cleared` and `pr_repair_attempts` is 0. Assert the status is
   `validating` (validation on) and that `ops.pr_repair_origin` is None afterwards — the
   §2.5 step-7 argument, pinned.
4. **No finding while an episode is live** (tests/test_invariants.py): a refused-then-pushed
   order with a `session_id` and no give-up yields no `INV-UNDECLARED-DELIVERY` violation
   and writes no finding.
5. **Finding on give-up** (tests/test_invariants.py): same order plus a
   `pr_undeclared_unresolved` event -> the finding is raised exactly once, as today.
6. **Still a finding with no session** (tests/test_invariants.py): the shipped
   `_refused_then_pushed` order, unchanged, still raises. This is
   `test_the_detector_raises_a_finding_once_and_never_an_alarm` at :1599 — it must keep
   passing untouched, and that is the assertion that the suppression is scoped.
7. **No double nudge when the PR is also red.** Red build AND detector true: one poll
   queues exactly ONE message, its source is `pr-checks`, `pr_undeclared_nudged` does not
   exist, and `pr_repair_attempts(..., "undeclared")` is 0. Then make it green and clear the
   queue: the next poll nudges `pr-undeclared`. This is the ordering claim of §2.3.
8. **Validation-off declaration lands** (tests/test_validation_config.py or alongside test
   3): project with `validation.enabled` false -> the nudged worker's `finish` parks
   `waiting_pr_merge`, and the next poll clears the episode.

Registry-style assertions that cover the third descriptor the moment it exists, and that
must be run rather than re-written:

- tests/test_repair_resettle.py:310 — `PR_REPAIR_SOURCES == tuple(r.source for r in
  PR_REPAIRS)`. Fails until §2.1's `PR_REPAIR_SOURCES` edit lands.
- tests/test_repair_resettle.py:432 `test_every_repair_nudge_asks_for_targeted_tests_only`
  and :440 `test_every_repair_nudge_still_renders` — parametrized over `ops.PR_REPAIRS`.
  The second renders the template with the standard field set, so `PR_UNDECLARED_NUDGE` may
  introduce NO new format field beyond `url`/`attempt`/`max_attempts`.
- tests/test_pr_checks.py statement-counting tests — the five edits listed in §2.6.

Add one new registry test: `PR_REPAIR_BLOCKERS` names every member of `ops.PR_REPAIRS`
(`tuple(r.name for r in PR_REPAIRS) == tuple(n for n, _ in PR_REPAIR_BLOCKERS)`). A fourth
repair added to one tuple and not the other derives a give-up nothing can surface, which is
the exact asymmetry the comment at invariants.py:275-286 records as already having cost a
work order.

## 4. Out of scope

- `remedies` stays off and the `nudge` remedy is not touched. This change makes the poll the
  actor for THIS condition; the supervisor's vocabulary is a different question.
- The `waiting_input` arm is nudged with the same message and no special wording. An order
  parked on a question is a case `heal_pull_request`'s guards already handle (a pending gate
  defers; a Neo question does not), and inventing a second template for it would be the copy
  issue #224 is about.
- No change to `judged_heads`, `refusal_answered` or `UNDECLARED_DELIVERY_STATUSES`.
