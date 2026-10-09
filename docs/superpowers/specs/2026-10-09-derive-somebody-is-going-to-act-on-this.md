# Derive "somebody is going to act on this"

Filed from `io-df4fa1e3`, accepted finding `no-actor-no-attention`. Feature order
`fo-f02d33af`.

## 1. The defect

The OS decides whether an open work order needs the user by matching the STATUS NAME
against a hand-maintained allow-list. It never checks the thing it actually means:
that some named daemon pass exists, is eligible, and will touch this row.

There are three such lists, and they disagree with each other:

| List | Where | What it gates |
|---|---|---|
| `PARKABLE_STATUSES` | `invariants.py:502` | where "nothing is in flight" is worth saying |
| `IN_FLIGHT_WAITS` / `SPOKEN_FOR_WAITS` | `invariants.py:534` / `541` | which `ops.waiting_on` verdicts suppress a park |
| `BLOCKED_STATUSES` | `invariants.py:77-96` | which statuses INV-ATTENTION-MISSING even looks at |

The list has been wrong twice, and both times the fix was to add one more name to it:

- `queued_message` — GitHub issue 43. 15 messages rotted across 4 work orders.
- `validating` — 3 orders invisibly stalled between 2 and 8 days.

`BLOCKED_STATUSES` carries the indictment in its own comment at `invariants.py:74-76`:
"This tuple must cover every status `true_blockers` can return a blocker for, or the
blocker is derived correctly and then never surfaced." Nothing enforces that. `validating`
is absent from it, and `tests/test_invariants.py:578` asserts the absence.

So the failure mode is not a missing name. It is that **the assertion "somebody is
going to act on this" is written down instead of derived**, and a written-down claim
cannot go stale loudly.

### What this feature changes

One derivation answers it: `ops.scheduled_actor` names the pass that will next touch an
open order, and why it is eligible. The readers consume that one answer. An open order
with no scheduled actor and no user-owed blocker becomes an attention item BY
CONSTRUCTION — not because someone remembered to list its status.

### What it must not do

Three prohibitions, from the feature order, which apply to every section below:

1. **MUST NOT** add `validating` to `PARKABLE_STATUSES`. That is the cheap fix, it is the
   third repetition of the defect, and the improvement order rejected it by name.
2. **MUST NOT** raise anything for a merely-live blocking dependency. A `pending` order
   waiting on a dependency that is still running is waiting correctly and is not an
   attention item. `jarvis wo unblock`'s documented behaviour — only an edge that can
   NEVER clear raises attention — is the rule to preserve.
3. **MUST NOT** regress the documented exemptions. Every exemption in
   `PARKABLE_STATUSES`' comment, in `parked_reason`'s body (`PR_REPAIR_SOURCES`,
   `_head_already_judged`, the `holds.held` overlap subtraction) and in
   `UNGOVERNED_ORIGINS` is load-bearing and stays.

## 2. The derivation: one contract, shared by every section

Every section from 3 onward builds on or consumes this. It is specified once, here.

In `src/jarvis/ops.py`:

```python
@dataclass(frozen=True)
class Actor:
    pass_name: str   # "Daemon.retry_paused_turns" — the daemon method, exactly as spelled
    why: str         # one sentence, re-derivable, naming the predicate that said yes
    predicate: str   # dotted path of the IMPORTED symbol, e.g. "worker_session.turn_pause"


def scheduled_actor(store: ProjectStore, wo: dict[str, Any],
                    now: float | None = None) -> Actor | None: ...
```

### What `None` means

`None` means **no daemon pass will touch this row**. It does NOT mean "it is held".

A fleet, project-concurrency or dispatch hold is a NAMED actor whose turn is DELAYED and
which resumes by itself. `scheduled_actor` answers WHICH pass and WHY ELIGIBLE, never
WHEN. The precedent is already in the tree: `Daemon._stuck_exclusion` keeps
`fleet_outage` / `fleet_paused` / `fleet_ramp` out of `stuck.assess` entirely rather than
folding them into the verdict.

### What it cannot see, and must not try to

- The daemon's in-memory sets — `Daemon.validating`, `worker_session.busy`. Callers hold
  a `ProjectStore` and nothing else.
- The `ProjectSpec` / `project.max_concurrent`. Where a predicate genuinely needs project
  config, resolve it by path behind a status gate; `ops.auto_review_at` (`ops.py:13301`)
  is the established pattern.
- The tick's `fleet.Fleet` reading. **`fleet.current` (`fleet.py:326`) is banned from this
  path by name**: it opens every project store, and this runs per order per reconcile tick.

### Cost budget

`true_blockers` calls this for every work order on every reconcile tick. Ordering stays
cheapest-first exactly as `parked_reason`'s docstring already demands: free row checks
first, then indexed reads. Nothing in this path opens a session file, shells out, or
calls a model.

### The predicate rule

Each actor's eligibility test MUST be **that pass's own predicate, imported**. Never a
second opinion written beside it. A predicate the pass does not itself call is exactly
the defect class this feature deletes — two expressions of one rule, free to drift.

Consequently: an extracted predicate ships in the SAME child as the rewrite of the pass
that calls it. There is no "extract now, adopt later" step, because an extraction with no
consumer cannot be shown equal to the body it came from.

And: **a pass whose eligibility cannot be stated as a predicate is a finding to RECORD,
not a case to hard-code.** Record it with `jarvis wo assume` and, if it blocks the
section, `jarvis wo ask`. Do not inline the condition.

### The one new blocker string

Exactly ONE, defined in `invariants.py` beside the others — `NO_ACTOR_BLOCKER` — and
produced ONLY by `true_blockers`. INV-ATTENTION-REASON
(`invariants.check_attention_reason_is_true`, `invariants.py:2196-2238`) rewrites any
attention reason `true_blockers` cannot re-derive, so a blocker string produced anywhere
else is silently relabelled to the generic line on the next tick. This is the trap
`PR_CLOSED_BLOCKER` already documents.

It carries NO elapsed time and no clock-dependent text. `ProjectStore.ack_attention`
stores the reason verbatim and INV-ATTENTION-REASON compares it, so a ticking reason can
never be acknowledged — documented on `PARKED_BLOCKER` (`invariants.py:476-482`) and
`MESSAGE_STUCK_BLOCKER` (`invariants.py:510-515`).

### The rot-proofing shape

`scheduled_actor` has **no `else: return None` fallthrough on an unrecognised status**.
The registry is keyed by status such that a status added to `project_store.OPEN_STATUSES`
later fails the coverage pin on the day it ships, rather than quietly returning "no
actor" (or, worse, quietly returning an actor). The exact precedent to copy is
`stuck.assess`'s `fallback_seconds` (`src/jarvis/stuck.py:65-97`), whose docstring states
the same goal, pinned by `tests/test_catalog.py:685`
`test_every_open_status_has_a_threshold`.

### The population

`project_store.OPEN_STATUSES` (`project_store.py:65-66`), all nine members:

`pending`, `dispatching`, `running`, `idle`, `waiting_input`, `validating`,
`needs_review`, `waiting_pr_merge`, `budget_exhausted`.

Done, for the whole feature, is: every one of those nine can name an actor or raise a
reason, pinned by a test.

## 3. The actor registry over the already-importable predicates

Delivers `ops.scheduled_actor`, the `Actor` type, the registry, each pass's call-site
rewrite, and `tests/test_scheduled_actor.py`. **No reader is rewired here and no
behaviour changes** — after this section the answer exists and nothing acts on it yet.

`pending` is NOT in this section; it is section 4.

### The count, stated so the pin's table is not a guess

Of the nine, **six get a real `Actor` naming a daemon pass** — `dispatching`, `running`,
`idle`, `waiting_input`, `validating`, `waiting_pr_merge` — **one comes from section 4**
(`pending`), and **two get a documented `None`**: `needs_review` and `budget_exhausted`
have no daemon pass, by design, and their coverage comes entirely from `true_blockers`'
existing lines. Write that split into the registry explicitly. "No pass, and here is why"
is a registry entry; a missing key is not.

### Per status: the pass, and the predicate to import

All passes are `Daemon` methods called from `Daemon.tick` (`daemon.py:841-1075`).

**`dispatching` and `running` — `Daemon.settle_turns`** (`daemon.py:5403`, settling via
`Daemon.settle_work_order`). The pass filter is
`statuses=("running", "idle", "waiting_input", "dispatching")` plus
`origin not in UNGOVERNED_ORIGINS` (`daemon.py:5420-5423`). The body branches on
`store.latest_turn(...)` and `worker_session.is_stalled(turn)`, both already importable.
The `turn is None` plus `updated_at <= 300` grace moves with the predicate: it is the only
thing distinguishing "just claimed" from "the daemon died between the two writes", so an
extraction that drops it turns a real hole into a claimed actor.

**`running` also — `Daemon.retry_paused_turns`** (`daemon.py:2057`). Already a shared
importable predicate with four readers by design:
`worker_session.turn_pause(store, wo_id).resumable` and `.due()`, gated by
`project_store.RETRY_SWEEP_STATUSES` (`project_store.py:339`). Import it; do not restate
it.

**`running` also — `Daemon.deliver_messages`** (`daemon.py:2246`). Already importable:
`worker_session.delivery_hold(store, wo)`, explicitly documented as "the one place that
decides them", and `invariants.stuck_message` already shares it. This is the
`queued_message` regression's own surface — it must be an actor here, derived, not a name
on a list.

**`idle` — `Daemon.deliver_envelopes`** (`daemon.py:2326`), through the bus. `idle` is a
feature-manager-only status; `Daemon.settle_turns` sets it (`daemon.py:5736-5737`). The
honest predicate is "its parent feature is not in `FO_TERMINAL_STATUSES`" — invariants
already holds that constant and `true_blockers` already raises the settled-feature blocker
for the other case. Two delivery routes, one predicate: ordinary bus envelopes, and
`bus.ChildrenLanded` (`bus.py:109-114`), which is "the only event that reaches an idle
manager from the reconciler rather than from a reviewer or a worker". The second arm is
`Daemon.settle_features` (`daemon.py:1335`) via `ops.revive_feature_manager`
(`daemon.py:1749-1751`). Written this way, the designed steady state of an idle manager is
a POSITIVE actor, not an exemption.

**`waiting_input` — `Daemon.retry_paused_turns`** (auth pause), **`Daemon.deliver_messages`**,
**the Neo drain** (`Daemon._neo_drain`, `daemon.py:3919`) and the gates. Mostly importable
already: `invariants.awaiting_neo(wo_id)`, `store.queued_messages`,
`store.pending_approvals`, `store.held_approvals`, `store.escalated_approvals`. One
promotion needed and nothing else: `invariants._waiting_on_neo_gate(store, wo)` is private
and becomes public in this section.

**`validating` — `Daemon.validation_tick`** (`daemon.py:2352`) and
**`Daemon.poll_pull_requests`** (`PR_POLL_STATUSES = PR_REPAIR_STATUSES + ("validating",)`,
`daemon.py:184`). The eligibility is keyed off the ROUND, not the status.

> **Do NOT reach it through `ProjectStore.work_orders_awaiting_validation`**
> (`project_store.py:5035`). That takes no `wo_id`: it is a project-wide list query, and
> `scheduled_actor` is called from `true_blockers` for every work order on every reconcile
> tick, so using it is O(n) project-wide queries per tick. It breaks section 2's cost
> budget. Read THIS ROW's own validation rounds, plus
> `project_store.validation_hold_until(events, round_no)` (`project_store.py:201-227`),
> which already takes ROWS rather than a store for exactly this reason.

`Daemon.validating` (the in-memory set) is unreachable and is treated as "a round is in
flight, so an actor exists" — see section 2's "what it cannot see".

> **`validating` with no round is the first new attention item.** The pass only
> `log.warning`s that case, and `validation_tick`'s own docstring says nothing else in the
> OS looks at it. That is precisely the hole the finding describes: the status name said
> "owned", and no round existed. `scheduled_actor` returns `None` for it. The flag that
> results is section 5's job.

**`needs_review` — no daemon pass. The USER is the actor.** `true_blockers`' four-way
`needs_review` triage (PR_CLOSED, validation_escalated, work_unlanded, IDLE_NO_FINISH)
always yields a line for a governed order, so the existing "already flagged" exemption
holds by construction rather than by a list. An UNGOVERNED order (`UNGOVERNED_ORIGINS`:
`injected`, `adhoc`) in `needs_review` correctly derives neither an actor nor a blocker —
that is a documented exemption, which is why the coverage pin parametrises origin.

**`waiting_pr_merge` — `Daemon.poll_pull_requests`.** Half-inline today:
`PR_POLL_STATUSES` plus `wo.get("pr_url")` (`daemon.py:6060-6074`), plus
`ops.routes_on_pull_request(store, wo)` for rows carrying a `pr_url_recorded` event. Only
`routes_on_pull_request` is importable; the two-line `pr_url` filter is extracted here.

> **`waiting_pr_merge` with no `pr_url` is the second new attention item.** No pass will
> ever select it, and today it is silent.

**`budget_exhausted` — no pass, deliberately.** `project_store.NOT_RETRIED`
(`project_store.py:357`) excludes it from retries and `Daemon.settle_work_order` returns
early. A round already open may still finish, because
`work_orders_awaiting_validation` is `OPEN_STATUSES`-bounded. The actor is the USER, via
`ops.set_work_order_budget` ("raising it resumes it"), and `true_blockers` already raises
the budget blocker first. Already correct — register it, do not change it.

### Passes that are NOT actors, and must stay out of the registry

- `Daemon.hold_red_release` (`daemon.py:8658`) — it WRITES `retry_after` via
  `store.hold_dispatch`, so it folds into the `pending` answer in section 4. It is not a
  separate actor.
- `Daemon.plan_features` (`daemon.py:1233`) — it CREATES rows. Not an eligibility question
  about an existing row.
- `Daemon.remedy_tick` (`daemon.py:4484`) — acts only through an already-approved
  `remedies` grant. Not a scheduled touch.
- `Daemon.stuck_tick` (`daemon.py:4824`) — an OBSERVER. **Counting it as an actor would
  make every stuck order claim an owner and defeat this entire feature.**

### Origin is part of the predicates, not a guard around them

Several passes already decide on origin themselves: `Daemon.settle_turns`
(`daemon.py:5422`) and `Daemon.retry_paused_turns` (`daemon.py:2119`) both `continue` on
`wo["origin"] in UNGOVERNED_ORIGINS`. So an `adhoc` order in `running` or `dispatching`
genuinely has no actor from either pass — correctly, per `Daemon.retire_ungoverned` and
INV-ADHOC-LEGACY-RETIRED.

That means origin belongs INSIDE each imported predicate and **`scheduled_actor` must NOT
carry a blanket `if origin in UNGOVERNED_ORIGINS: return None` guard**. Such a guard would
reach the same answer for the wrong reason and would hide the next case.

`injected` is NOT the same as `adhoc` here. `Daemon.track_injected_sessions`
(`daemon.py:9131`) follows `origin == "injected"` rows in `("running", "waiting_input")` —
a real named pass — and `true_blockers`' `waiting_input` arm is not gated on `governed`, so
an injected `waiting_input` row raises its line anyway.

But `track_injected_sessions`' eligibility keys on the live `claude_cli` agents view
(`sessions_by_cwd`), which is not a row read and is not cheap. **By section 2's predicate
rule that is a finding to RECORD, not a case to hard-code.** Record it with
`jarvis wo assume`, stating that this pass's eligibility is not a row predicate and why.
Register `injected`'s `running` / `waiting_input` actor only if a row-only predicate can
honestly be extracted; otherwise the pair goes in the pin's exemption table carrying that
sentence as its reason.

### The coverage pin

New file `tests/test_scheduled_actor.py`. Parametrised over the PAIR
`(status, origin)` — `project_store.OPEN_STATUSES` × `("jarvis", "injected", "adhoc")`:
per case, build the minimal row and assert that `scheduled_actor(...) is not None` or
`true_blockers(...) != []`, **never both false**.

The exemptions live in a module-level table, not in a skip and not in a guard inside
`scheduled_actor`:

```python
#: (status, origin) pairs that correctly derive NEITHER an actor nor a blocker,
#: each mapped to the one sentence saying why.
EXPECT_NEITHER: dict[tuple[str, str], str] = {...}
```

The table **bites in both directions**: for a pair in `EXPECT_NEITHER` the test asserts
both sides are empty AND FAILS if either side starts deriving something. An exemption that
quietly became a real answer must break the test, or the table rots into a list of names —
which is the defect this feature exists to delete, one level up.

`pending` is section 4's, so in this section its parameter is marked
`pytest.mark.xfail(strict=True, reason="pending's actor lands in the claim-SQL child")`.
Strict, not `skip`: section 4 removing the marker is part of section 4's done, and a
`skip` left behind would rot silently.

Two more pins, same file:

1. The population pin above. It fails on the day a status OR an origin is added, which is
   the whole point.
2. An AST-or-import pin holding the predicate rule from section 2: each registry entry's
   `predicate` must resolve to a symbol IMPORTED from the pass's own module, not a lambda
   or a condition defined in `ops`. `tests/test_remedies.py` already does this kind of AST
   walk; `tests/test_stable_prefix.py` is the precedent for a test holding two things that
   must agree.

## 4. `pending`: one claimability rule, two queries, same SQL text

Delivers the ninth status, plus a reader that can explain a stuck `pending` order —
something `ops.blocked_by` and `store.plan_hold` only half do today.

**Needs section 3.** The SQL work here — the shared WHERE fragment, the one-statement
UPDATE, `why_not_claimable` — is independent of it, but the two things this section ALSO
does are consumption of section 3's code: registering `pending`'s entry in the status
registry, and removing the strict-xfail marker from `tests/test_scheduled_actor.py`.
Neither the registry, the `Actor` type nor that test file exists until section 3 lands, so
a branch cut before it would leave this section's worker re-creating them — the duplicate
expression of one rule that this whole feature exists to delete.

`ProjectStore.claim_next_pending` (`project_store.py:2404-2494`) is ONE atomic
`UPDATE … WHERE id = (SELECT …)` carrying all four refusals:

1. unsatisfied `depends_on` (a `json_each` subquery),
2. the feature's `max_parallel` cap over `ACTIVE_STATUSES`,
3. a planner assumption pending — `kind='worker'` plus `assumptions.status='pending'`,
4. `retry_after` (written by `Daemon.hold_red_release` via `store.hold_dispatch`).

Only two have importable readers: `ops.blocked_by` (used at `health.py:187`) and
`store.plan_hold(wo)` (used in `ops.waiting_on`). The `max_parallel` cap has none.

### The shape

Lift the WHERE clause into a SHARED SQL FRAGMENT consumed by both the atomic UPDATE and a
new read-only `why_not_claimable(store, wo) -> str | None`. Then register `pending`'s
actor — `Daemon.dispatch_pending` (`daemon.py:1169`) — on that reader.

Two hard constraints:

- **The UPDATE stays ONE statement.** Its atomicity is why two daemon ticks cannot claim
  the same row; nothing here may split it into a SELECT then an UPDATE.
- **The read-only form is a second query over the SAME TEXT, never a Python
  reimplementation of it.** Two WHERE clauses that must agree is the exact defect class
  this feature exists to delete, so producing one here would be self-defeating.

### Why the hold lives in the SQL and not in the caller

`kn-3bde1bf2`, and it is not restated here: read it. In short, `Daemon.dispatch_pending`
is a `while` loop over `claim_next_pending`, and by the time the caller sees a row that row
is already `dispatching`. The exemption that avoids a permanent deadlock is
`w.kind='worker'`. The test worth copying from that entry: make the HELD row the OLDEST and
assert two younger unblocked ones are claimed on the same call.

### The prohibition that bites hardest here

A `pending` order whose dependency is still LIVE is waiting correctly. `why_not_claimable`
returns a reason for it, and that reason is NOT a blocker and raises NOTHING. Only an edge
that can never clear does, which is already `true_blockers`' `dead_dependencies` arm. If
this section makes a live dependency raise attention, it has broken the feature order's
second MUST NOT and every `--depends-on` user.

### If the fragment cannot be shared

If SQLite will not take the WHERE clause as a shared fragment across the two query shapes,
**record a finding and stop** — `jarvis wo assume`, then `jarvis wo ask`. Do not
reimplement the clause in Python and do not expand this section's scope. Section 2's
predicate rule says so in general; this is the one place it is most likely to be tested.

## 5. The two readers, and the death of `BLOCKED_STATUSES`

The section that makes sections 3 and 4 do something. Before it, they are inert.

Needs section 3 (`idle`, `validating`, `waiting_pr_merge` actors) and section 4
(`pending`). Three sites must agree about one answer and they share one test file, so they
are one unit of work: rewire `parked_reason` alone and `true_blockers` still gates it
behind `all(_mentions_assumptions(b))`; rewire `true_blockers` alone and it has two sources
for one question.

### 5a. `invariants.parked_reason` reads the verdict

`parked_reason` (`invariants.py:1475-1550`) already says in its own docstring: "THE
PREDICATE IS `ops.waiting_on`, NOT A SECOND OPINION." Keep the principle, change the
predicate: it reads `ops.scheduled_actor` and stops consulting `SPOKEN_FOR_WAITS` /
`IN_FLIGHT_WAITS`. That cuts the `parked_reason` to `ops.waiting_on` edge, which is what
lets section 6 exist without a cycle.

`PARKABLE_STATUSES` stops being the gate. `validating` is **NOT** added to it — the whole
list stops being consulted for this decision, which is the difference between inverting the
defect and repeating it a third time.

Everything else in `parked_reason` stays and must be shown to stay: `UNGOVERNED_ORIGINS`
returns `None`; the latest turn must be `done` with an `ended_at`; `_parked_minutes`
disabled or under threshold returns `None`; the `holds.held` overlap is subtracted before
the threshold is re-checked; the stale-finish check keeps its `PR_REPAIR_SOURCES` and
`_head_already_judged` exemptions and still returns `STALE_FINISH_BLOCKER`; and
`needs_review` still returns `None` rather than `PARKED_BLOCKER`.

### 5b. `invariants.true_blockers` gains the one new arm

`true_blockers` (`invariants.py:974-1245`) is "the single source of truth for what does this
work order want from me", ordered most-actionable-first. It gains ONE arm: an open row with
no scheduled actor and no user-owed blocker raises `NO_ACTOR_BLOCKER`. Nothing else is
reordered — the budget arm stays first for `budget_exhausted`, the `needs_review` triage
stays ahead of it, and the arm sits where a derived "nobody owns this" belongs: after every
blocker that can name what the user must DO.

The reason text names the status and the fact that no pass selects the row. It carries no
elapsed time, per section 2.

### 5c. The gate: `BLOCKED_STATUSES` dies

`invariants.check_blocked_work_is_surfaced` / INV-ATTENTION-MISSING
(`invariants.py:2460-2484`) selects `statuses=BLOCKED_STATUSES` (`invariants.py:77-96`),
and `validating` is ABSENT from that tuple. So a blocker derived correctly for a
`validating` order is **never surfaced**, and
`tests/test_invariants.py:579` — `assert "validating" not in BLOCKED_STATUSES`, inside
`test_a_validating_work_order_is_silent_until_the_panel_gives_up` at
`tests/test_invariants.py:563` — asserts the absence outright.

The gate becomes `project_store.OPEN_STATUSES` plus `failed`, and the list is DELETED. Its
own comment (`invariants.py:74-76`) stated the invariant it kept breaking; after this there
is no second list to keep in step, so there is nothing to break.

Line 579 is rewritten to assert the opposite of what it asserts today: that a `validating`
order with no round IS surfaced. That edit is expected and is this section's headline, not
an accident to be worked around.

**Line 578 is a DIFFERENT assertion and must stay green verbatim**:
`true_blockers(...) == []` for a `validating` row WITH an open round. The pairing of the
two lines IS the test, as its docstring says — deleting the wrong half would remove the
only thing stopping this feature from flagging every order the panel is actively judging.

### 5d. End to end

This section proves the whole feature, in `tests/test_invariants.py`: a `validating` order
with no validation round, and a `waiting_pr_merge` order with no `pr_url`, each becomes an
attention item with a reason INV-ATTENTION-REASON can re-derive — and `jarvis wo ack`
clears it and it stays cleared.

The cost budget from section 2 is this section's to hold: `true_blockers` runs for every
work order on every reconcile tick, so the new arm is reached only after the free row
checks, and `scheduled_actor` is called ONCE per order per derivation, not once per arm.

## 6. `ops.waiting_on` rebased on the verdict

Needs section 5, which cut the edge from `parked_reason` to `waiting_on`. Doing this
earlier would mean rewriting the same function twice, and doing it while `parked_reason`
still reads `SPOKEN_FOR_WAITS` would create a cycle.

`ops.waiting_on` (`ops.py:1391-1547`) answers a strictly LARGER question than
`scheduled_actor` — "what is this waiting for, including things the user does not own" —
and it owns the user-facing `detail` sentences and the `stalled` claim. It is therefore
REBASED on the actor verdict, not folded into it: `scheduled_actor` must not acquire CLI
prose, because `true_blockers` calls it per order per tick.

The `what` slug becomes a projection of the actor verdict plus the user-owed arms
`waiting_on` adds on top. `IN_FLIGHT_WAITS` / `SPOKEN_FOR_WAITS` are no longer inputs to
`parked_reason` (section 5 already stopped reading them) and survive, if at all, only as
`resume_in_auto`'s `NUDGE_IS_WRONG` key set.

### The constraint that makes this its own section

**Every existing `what` slug keeps its EXACT string.** `assumptions`, `gate_escalated`,
`gate_with_neo`, `gate_held`, `neo_escalated`, `neo_question`, `message_stuck`,
`queued_message`, `signin`, `retry_pending`, `turn_running`, `validating`,
`plan_assumptions`, `failed`, `completed`, `cancelled`, `waiting_pr_merge`,
`needs_review`, `pending`, `manager_idle`, `prompt`.

`NUDGE_IS_WRONG` is a DICT LOOKUP, so a renamed slug does not fail — it falls through
quietly, and `jarvis wo resume-auto` starts nudging workers that are waiting correctly,
re-sending each one's whole conversation. That is the regression GitHub issue 100 was.
The slug set and `NUDGE_IS_WRONG` are therefore changed in this section together or not at
all.

Blast radius to check and keep working: `ops.resume_in_auto` (`ops.py:1570`, reads
`wait["what"]` and `wait["stalled"]`), `ops.os_status` (`ops.py:2374-2385`, the dashboard's
`open_work_orders[].parked`), `ops.py:2841`, `ui/app.py:1550-1555`.

## 7. The stillness alarm clears on a name

Needs sections 3 and 5. Small, and deliberately separate.

`supervisor.build_evidence` (`src/jarvis/supervisor.py:592-686`) builds the evidence packet
the stillness judge reads. Its work-order arm's `blocked:` line comes from
`health.blocker(pstore, subject, cfg)` (`health.py:145-158`, dispatching to
`_work_order_blocker` at `health.py:161-208`, which calls
`invariants.true_blockers(..., now=db.now())`).

The packet gains the named pass that will next touch the subject, and why — so a judge
deciding whether stillness is a problem can clear it on a NAME instead of inferring from a
status.

### Three constraints, each a reason this is not a rider on another section

1. **The packet's bytes are a cached prompt prefix, and this is a FULL invalidation, not a
   marginal one.** Most open orders have an actor, so a positive line changes the packet
   for nearly all of them. `build_evidence`'s docstring warns against a second
   implementation, and `tests/test_supervisor.py:852`
   `test_the_work_order_packet_is_byte_for_byte_what_it_has_always_been` holds
   `EXPECTED_WORK_ORDER_PACKET` as a literal precisely so this cannot happen by accident.
   That literal is updated ONCE, in this section and nowhere else, and the new line sits at
   a FIXED index relative to the `this session is …` line — the precedent is
   `tests/test_supervisor.py:909-911` — so the prefix stays cacheable across reviews.
   Mixing a cache-prefix change into a child that also edits attention logic would put a
   spend change and a correctness change under one review.
2. **`health.blocker` must stay PURE** (`health.py:152-154`). It also feeds dedupe and
   `due`, so a verdict added here changes what RE-ASSERTS. That has to be reasoned about on
   its own.
3. **The new line is POSITIVE content only.** The `blocked:` line is deliberately ABSENT
   when there is no blocker, "because a line asserting the absence of a blocker is a claim
   the judge would weigh". So the packet says "the named pass that will next touch this,
   and why" when an actor EXISTS, and says NOTHING — emits no line at all — when
   `scheduled_actor` returns `None`. Never `scheduled_actor: none`.

This section does NOT re-rank `health.BLOCKERS`, does not touch
`health_reassert_blockers` (`health.py:172`), and does not widen
`supervisor.BLOCKED_SENTENCES` — `tests/test_supervisor.py:914` asserts
`set(BLOCKED_SENTENCES) == {*health.BLOCKERS, health.CHILDREN}`, so the new line is its
own thing and not a blocker sentence. The catalog rework is backlogged.

## 8. Out of scope

Each of these is pushed out deliberately, not forgotten.

1. **The passed-round deadlock.** `Daemon.settle_work_order`'s `validating` early return —
   cited by SYMBOL, because `io-df4fa1e3` and the architect's reading give two different
   line numbers for it and one is stale. It is a separate pending finding with its own
   proposed orders. This feature makes the stall VISIBLE only; fixing the return changes
   settlement and its test fixtures would fight section 5's.
2. **`validating` into `PARKABLE_STATUSES`.** The feature order's explicit MUST NOT,
   restated in sections 2, 3 and 5 because it is the fix a worker will reach for.
3. **`health.BLOCKERS` rework / `health_reassert_blockers`** — `bl-c423c63e`.
4. **Fleet-hold arithmetic** — `fleet.Fleet`, the post-reopen ramp, the `holds.held`
   overlap maths. Already correct, already exempted, and per section 2 it stays outside
   `scheduled_actor` entirely.
5. **`jarvis doctor` / dashboard surfacing of the actor verdict** — `bl-1f2261d9`. New UI.
   The finding asks for the attention flag, and section 5 delivers it.

## Agent profile

You are a Jarvis OS engineer working on the OS's own attention derivation — the code that
decides whether an open work order needs the user. You are working inside
`src/jarvis/`: `ops.py`, `invariants.py`, `project_store.py`, `daemon.py`, `health.py`,
`supervisor.py`.

Your bias is derivation over declaration. When you find a decision expressed as a
hand-maintained list of names, you treat the list as the bug and the derivation as the fix
— and you do not add a name to it to make a symptom go away, even when that would pass the
test in front of you. You have read why: this list has been wrong twice, and both times it
was patched with one more name.

You hold one rule above convenience: **a rule has exactly one expression.** If a pass
decides eligibility, that pass's own predicate is what every other reader imports. You
never write a second condition beside an existing one, however small, however obviously
equivalent — two expressions of one rule are free to drift, and drifting silently is the
entire failure you are here to delete. When a pass's eligibility genuinely cannot be
stated as a predicate, you RECORD that as a finding (`jarvis wo assume`, then
`jarvis wo ask`) and stop. You do not hard-code the case.

You are economical about cost in a specific, non-abstract way: the code you touch runs for
every work order on every reconcile tick. You order checks cheapest-first — free row
fields, then indexed reads — and you never put a session-file read, a subprocess or a
model call in that path. You know which function is banned from it by name and you check
before adding a caller.

You treat an exemption in a comment as load-bearing until you can show otherwise. The
docstrings and comments in `invariants.py` and `project_store.py` record decisions with
reasons; you read the reason before changing the code, and if you remove an exemption you
say which reason stopped applying.

You work test-first, and the test you write first is the one that fails for the right
reason. A coverage pin that passes because of a fallthrough is worse than no pin, so you
build registries that have no default case and you check that adding a new member breaks
the test. You name the precedent you copied.

You do not run the full test suite. You run the targeted files, you quote the shortest
decisive line of output, and you let CI run the rest. When you report, you lead with what
is true and you state plainly what you did NOT do.
