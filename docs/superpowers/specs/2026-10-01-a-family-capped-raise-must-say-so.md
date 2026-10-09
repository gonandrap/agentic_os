# A family-capped raise must say so

GitHub issue 903, "Raising an investigator's budget is accepted and has no effect".
Work order wo-ac7c1b8d. Scope approved by Neo on question 1187, option (b).

## The problem

**Root cause, one sentence:** `ProjectStore.list_feature_orders` defaults
`kind='feature'` (`src/jarvis/project_store.py:2919-2921`) — correctly, per its own
docstring and §2.4 of `docs/superpowers/specs/2026-09-23-improvement-orders.md` — and
callers on paths that must be KIND-AGNOSTIC left that default in place, so improvement and
investigation orders fall silently out of the recovery path, the listings and the status
payload. The contrast is `ProjectStore.flagged_feature_orders`
(`src/jarvis/project_store.py:2953-2968`), unfiltered by kind on purpose and saying why:
the kind filter belongs on the LISTINGS, never on the recovery or attention paths.

Second, independent half: on the one path that is kind-correct — a child telling the user
to raise its family — the explanation exists only on the SET path, is hardcoded to
`jarvis fo budget`, and never reaches the dashboard at all.

### The reported sequence, traced

1. `jarvis investigate create` stamps the family budget from
   `budget.investigation_default_for` (`src/jarvis/budget.py:651-668`), which falls back to
   `catalog.DEFAULT_INVESTIGATION_BUDGET_USD = 2.00` (`src/jarvis/catalog.py:172`). Family
   budget: $2.
2. `Daemon.plan_features` opens exactly one child, `kind='investigator'`, and moves the
   parent to **`planning`** — not `executing` (`src/jarvis/daemon.py:1208-1227`).
3. At dispatch `budget.reserve` cuts the child's slice from
   `Pool.slice_for_one_more()`. One unclaimed child, nothing held: the slice is the whole
   $2 (`src/jarvis/budget.py:280-288, 323-365`).
4. The turn overshoots to **$2.0036** — documented behaviour of `--max-budget-usd`, see the
   overshoot note at the top of `budget.py` — and the child parks in `budget_exhausted`.
5. The user raises the CHILD to $10 from the dashboard.
   `ops.set_work_order_budget` re-cuts via `budget.reserve`
   (`src/jarvis/ops.py:13689-13691`) exactly as designed (kn-c3a9a7c3). The family's
   `unreserved_usd` is `max(0, 2 - 2.0036) - 0 = 0.00`, so the re-cut writes
   `already_spent + 0 = $2.0036`. `budget.ceiling` takes the tighter of $10 (`work_order`)
   and $2.0036 (`feature`) (`src/jarvis/budget.py:405-430`): cap $2.0036, `source='feature'`,
   `exhausted` true. The order stays parked. **That arithmetic is correct.**
6. The dashboard route throws the returned note away
   (`src/jarvis/ui/app.py:1711-1731`), so the user is redirected to an unchanged page with
   no message. The budget WAS raised; nothing says why nothing happened.
7. The second step — add family money — **does not exist for an investigation parent**:
   `jarvis investigate` has no `budget` subcommand (`src/jarvis/cli.py:1053-1097`).
8. And if it did, it would still resume nothing: the only call site of
   `budget.feature_exhaustion` / `budget.escalate_feature` is the family-budget pass in
   `Daemon.settle_features` (`src/jarvis/daemon.py:1295-1310`), which is BOTH
   `kind='feature'`-filtered AND restricted to `statuses=("executing", FO_EXHAUSTED)` —
   and an investigation parent is in `planning`. Two independent misses. This is the bug
   itself, one level up.

Four live attention items with an empty `jarvis investigate list` is the same root cause
on a third path: `submit_verdict` settles a `WAITING_ON_USER` investigation to
**`completed`** and flags attention in the same transaction
(`src/jarvis/ops.py:8786-8792`), while `ops.list_feature_orders` filters to
`FO_OPEN_STATUSES` (`src/jarvis/ops.py:8021-8042`).

### Three corrections to the brief, from the code

* **Defect 4 needs a status fix as well as a kind fix.** The brief says the budget pass
  runs over a wider status set so a topped-up family can recover. It does — but
  `("executing", FO_EXHAUSTED)` is the FEATURE lifecycle. An improvement or investigation
  family runs in `planning` (`FO_STATUS_LABELS` renames it "analysing"/"investigating",
  `src/jarvis/project_store.py:473-478`) and never enters `executing` at all. Making the
  pass kind-agnostic without widening the statuses fixes nothing for the reported case.
* **Defect 5's surfaces are narrower than stated.** Nothing in `cli.py` or `ui/app.py`
  reads `os_status()["projects"][*]["feature_orders"]`: human `jarvis status` and the `/`
  page render `attention` and `open_work_orders` only. The payload's real consumers are
  `jarvis status --json` (`src/jarvis/cli.py:1552-1555`) and `GET /api/status`
  (`src/jarvis/ui/app.py:1666-1668`) — i.e. every machine reader, Jarvis's own opening
  pulse check included. The project page already renders all three kinds
  (`src/jarvis/ui/templates/project.html:65-104`); the `/` page renders none of them, and
  that is deliberate, not a defect.
* `jarvis io budget` routes to `ops.feature_order_budget` / `ops.set_feature_budget`
  **without a kind guard** (`src/jarvis/cli.py:3539-3546`), unlike every other `io`/
  `investigate` verb. Mirroring it "exactly" would mirror that gap.

### Evidence the brief's root cause is not re-derivable from tests

`tests/test_budget.py:1018-1033` pins the current note and asserts the literal string
`"jarvis fo budget"` — on a FEATURE parent, which is the one kind for which it is right.
No test exercises a non-feature family anywhere in the budget path. That is why six
kind leaks shipped green.

## The fix

Seven changes. Nothing in the allocator.

### 1. One note, composed once, used by both budget paths

Symptom: `ops.work_order_budget` (`src/jarvis/ops.py:13608-13647`) returns
`budget_usd=10.0`, `cap_usd=2.0036`, `cap_source='feature'`,
`feature_unreserved_usd=0.0` and no prose. The reporter quoted those four numbers and
could not assemble them.

Mechanism: extract the note `set_work_order_budget` composes inline
(`src/jarvis/ops.py:13696-13701`) into one private function in the budgets section of
`ops.py`, beside `_feature_unreserved` (`13712`), taking the ceiling, the spend, the
parent row and the unreserved remainder and returning the sentence. `work_order_budget`
adds it as a `note` key whenever `cap.source == 'feature'` and the cap binds; the show
path already computes `parent_pool`, so it needs no extra read.

Why there and not in `budget.py`: the sentence names CLI commands. `budget.py` is the
allocator and knows nothing about surfaces — `budget.status_note` is about one order's
own numbers. Why one home at all: the set path and the show path state the same fact, and
two wordings of it is how a user reading the panel and a user reading the CLI come to
believe different things.

### 2. The raise verb comes from the parent's kind

Symptom: the note hardcodes `` `jarvis fo budget` `` (`src/jarvis/ops.py:13701`). For a
child of an `io-`/`inv-` parent that command refuses with
`"is an improvement order, not a feature order"` — the OS telling the user to run a
command it will reject.

Mechanism: a table beside `_KIND_PHRASES` (`src/jarvis/ops.py:8095-8100`), mapping
`feature_orders.kind` to its raise verb: `jarvis fo budget`, `jarvis io budget`,
`jarvis investigate budget`. The note reads the parent row's `kind` through it.

Why a table and not a derivation: `_require_kind`'s own comment
(`src/jarvis/ops.py:8087-8089`) settles this — "a third kind read as 'a feature order' is
a message that names the wrong surface". Why that home: `_KIND_PHRASES` is the existing
kind-to-prose table and lives there; a second kind table in the budgets section is the
one that goes stale when a fourth kind lands. No kind-to-verb map exists today —
`FO_ID_PREFIXES` is id prefixes, `feature_status_label` is statuses, `_require_kind`
takes the verb as a caller-supplied argument.

### 3. `jarvis investigate budget`

Symptom: no such subcommand (`src/jarvis/cli.py:1053-1097`), so step 2 of the two-step
top-up is unreachable for an investigation. The command the fixed note will print must
exist.

Mechanism: a subparser mirroring `io budget` (`src/jarvis/cli.py:1038-1044`) —
positional `inv_id`, optional `amount`, `--clear`, `--project` — and a dispatch arm in
`cmd_investigate` mirroring `src/jarvis/cli.py:3539-3546`. Routes to the same
`ops.feature_order_budget` / `ops.set_feature_budget`: §2.6's reasoning holds unchanged
here, the family is the order plus its one child and the feature-order arithmetic is
already correct with one child.

The kind guard: add it in `ops`, where every other `investigate` verb's guard lives
(`_require_kind(fo, "investigation", …)` at `src/jarvis/ops.py:8652`, `8691`, `8725`), as
two thin wrappers shaped exactly like `cancel_investigation_order`
(`src/jarvis/ops.py:8690-8692`): guard, then delegate. Put the guard in the CLI instead
and `/api` plus any future caller gets no guard.

Fix `io budget`'s missing guard the same way, in the same change. It is one line per
verb and the alternative is shipping a new surface that copies a known gap. Called out
here so a reviewer sees it as deliberate.

### 4. Split `Daemon.settle_features` into two passes

Symptom: one loop at `src/jarvis/daemon.py:1295-1336` carries two jobs with incompatible
scopes. The budget pass needs every kind and the statuses each kind actually runs in; the
child-settlement pass below it is feature-only BY LIFECYCLE — an investigation settles
from its verdict (`src/jarvis/ops.py:8786`), an improvement order from its findings
review, so running `dead_feature_children` / "all children completed" over them would
settle them wrongly and destroy a verdict in flight. Today the budget pass pays for the
settlement pass's filter: no improvement or investigation family can ever escalate to, or
leave, `budget_exhausted`.

Mechanism: two loops.

* **Budget pass:** `kind=None`, statuses `("executing", "planning", FO_EXHAUSTED)`. One
  query, same shape. `budget.feature_exhaustion`, `budget.pool`, `budget.escalate_feature`
  and `budget.feature_reason` are already kind-safe — verified: they read `budget_usd` and
  `_family`'s child rows and branch on nothing else
  (`src/jarvis/budget.py:290-319, 497-507, 559-569`). `planning` is in `FO_OPEN_STATUSES`
  already, so no status-set constant changes.
* **Settlement pass:** `kind='feature'` **written explicitly**, statuses
  `("executing",)`, with the lifecycle reason in a comment. The current correctness rests
  on a default; a reader cannot tell a deliberate feature-only pass from a leak, which is
  how this bug got here.

**The resting status after a top-up must be kind-derived, and `executing` is wrong for two
of the three kinds.** `src/jarvis/daemon.py:1308` writes `set_feature_status(fo['id'],
'executing')`. For an improvement or investigation order that is a status its lifecycle
never enters: `plan_features` sets `planning` and the order settles out of it directly, so
`executing` would be rendered raw by `feature_status_label` (no override exists), would
drop it out of `_live_investigation`'s and the project page's reads if they ever narrow,
and lies about which phase it is in. Correct: restore `planning` for `improvement` and
`investigation`, `executing` for `feature` — one mapping, next to the pass. `assert status
in FO_STATUSES` (`src/jarvis/project_store.py:2998`) will not catch this: `executing` is a
legal value for the table, just not for the row.

### 5. `os_status`'s `feature_orders` payload carries every kind

Symptom: `src/jarvis/ops.py:608` leaves the default in place, so improvement and
investigation orders reach no machine reader of `os_status` — `jarvis status --json` and
`/api/status`. The attention strip in the same function does see them, via
`flagged_feature_orders` at `src/jarvis/ops.py:617-618`. Hence the reporter's four
attention items against an empty listing.

Mechanism: **one merged list with a `kind` label**, not three. The existing payload dict
(`src/jarvis/ops.py:664-669`) gains `kind` and `status_label` — the latter through
`feature_status_label(fo['kind'], …)`, the same single mapping the attention strip already
uses at `src/jarvis/ops.py:651-652`. Read with `kind=None`.

Why merged: three keys in the JSON means every consumer must learn three, and the next
kind breaks each one again — whereas a `kind` field is already how the attention strip
carries this and already how the rows are discriminated in the database. Why a label and
not the raw status: `planning` means "analysing" for an io and "investigating" for an inv,
and a JSON payload that says `planning` for all three is the leak in a different shape.

Agreement between `/` and `jarvis status`: both read `attention`, which already shows all
three kinds, and neither renders `feature_orders`. Nothing to reconcile. Do NOT add a
feature-order section to the `/` page or to human `jarvis status` under this work order —
the project page is where those lists live by design
(`src/jarvis/ui/templates/project.html:65-104`), and a strip that names everything stops
being read.

### 6. A flagged row lists whatever its status

Symptom: `ops.list_feature_orders` (`src/jarvis/ops.py:8021-8042`) filters to
`FO_OPEN_STATUSES` unless `include_settled`, so `jarvis investigate list` prints "no
investigations" for a `completed` + `needs_attention` investigation — the exact row
`submit_verdict` writes for `WAITING_ON_USER` (`src/jarvis/ops.py:8786-8792`).

Mechanism: in `ops.list_feature_orders`, when `include_settled` is false, union the open
rows with `store.flagged_feature_orders()` filtered to the requested `kind`, de-duplicated
by id, order preserved (`created_at DESC`). `--all` is unchanged. The rule: **the default
listing is the open ones PLUS any flagged row whatever its status.**

Why there and not in `ProjectStore`: the store's two methods are each honest about one
question, and `flagged_feature_orders`' docstring is the argument for this fix already
("`failed` is a SETTLED status and it is also the one a feature order raises its flag in —
listing only the open ones would drop the flag on the floor at the exact moment it means
the most"). The same argument reaches the listing: a flag nobody can list is a flag nobody
can act on.

`jarvis fo list` and `jarvis io list` get it for free and must: all three route through
this one function (`src/jarvis/cli.py:3325`, `3576`, and `ops.list_improvement_orders`).
A feature order's `failed` + flagged row is the identical case.

### 7. The dashboard shows the note

Symptom: `set_wo_budget` (`src/jarvis/ui/app.py:1711-1731`) discards the dict
`ops.set_work_order_budget` returns and redirects bare; only an exception becomes
`?error=`. The reporter's literal path produced a silent page.

Mechanism: a **separate non-error channel**. `base.html:282-283` renders
`request.query_params.get('error')` into a red `.error-flash` on every page; add a sibling
`?note=` block there with a neutral tone, and have `set_wo_budget` redirect with
`?note=<quote(result["note"])>` when the result carries one (and `resumed: false`).

Why not `?error=`: the budget WAS raised and written. A red ✗ saying so would be the
second false statement on this page, and the user who sees it will raise the number
again instead of raising the family's. A refusal is not an error.

Why `base.html` and not the work-order template: `set_fo_budget`
(`src/jarvis/ui/app.py:1733-1748`) has the identical shape and the same obligation —
`ops.set_feature_budget` returns `exhausted_children`, which is **the whole instruction to
the user** (`src/jarvis/ops.py:13801-13804`: `jarvis wo budget <child> <amount>` re-cuts
the child's slice out of the money just added). Both routes ride the one channel; so does
the next POST that owes the user a sentence.

## What must NOT change

A reviewer's first instinct on this issue is to make the child's own budget win the
`min`. It is wrong, and it is the only change here that could lose money.

1. **`budget.ceiling` takes the tighter of the two caps** (`src/jarvis/budget.py:405-430`).
   Its docstring states the invariant: the reservation is the family's statement about this
   child, the budget is the user's, and neither entitles the order to the other's dollars.
   Letting the child's own number win means a $2 family can spend $10 — the family budget
   stops being a bound at all, which is the invariant in kn-0a8f0bdf.
2. **`budget.reserve` writes `already_spent + share`, not `share`**
   (`src/jarvis/budget.py:339-344`). Cutting the share alone prices the child's past twice
   and leaves the family holding money it has already accounted for.
3. **The re-cut on a child top-up stays** (`src/jarvis/ops.py:13689-13691`, kn-c3a9a7c3).
   Without it a slice-exhausted child could never be raised out of `budget_exhausted` by
   any number the user typed on it.
4. **`list_feature_orders`' `kind='feature'` default stays**
   (`src/jarvis/project_store.py:2919-2925`). It is the whole reason this bug is six small
   leaks and not a silent data mix: defaulting to "everything" makes the next kind's
   arrival invisible. The fix is to name `kind` at the kind-agnostic callers, never to
   flip the default.
5. **The two-step top-up stays two steps.** `jarvis fo|io|investigate budget` adds family
   money; `jarvis wo budget <child>` spends it on the child the user picked. Auto-funding
   children from the family end would have to guess the split — and the guess is exactly
   what the user came to make (`src/jarvis/ops.py:13795-13804`,
   `src/jarvis/daemon.py:1302-1307`). What is broken is that nothing SAYS so.
6. **The settlement pass stays feature-only.** See defect 4.

## Not in scope

Issue 903 also reports a settled investigation keeping a budget-flagged child. Neo split
that out; it is filed as **io-b521d17b** and must not be touched here. No change under
this work order may clear or re-derive a child's flag from a settled parent.

Also out: any new feature-order section on the `/` dashboard or in human `jarvis status`
(see defect 5), and `budget.py` itself — no allocator change is part of this fix.

## Test obligations

Every item below must FAIL on `main` before the change. Houses:
`tests/test_budget.py` (the budget surfaces and the family allocator),
`tests/test_investigation_orders.py`, `tests/test_improvement_orders.py`,
`tests/test_feature_orders.py` (the settle loop), `tests/test_ui.py` (UI routes). Fixtures
to reuse: `a_feature`, `tick_until_reserved`, `bill_the_turn`, `tick_until_parked` in
`tests/test_budget.py`.

1. **An investigation family can leave `budget_exhausted`.** Build an inv order in
   `planning` with one investigator, bill past the family budget, tick: parent reaches
   `budget_exhausted`. Raise the family, tick: parent is back in **`planning`**, not
   `executing`, and its flag is down. Fails twice today — the escalation never happens and
   the recovery never happens. (defect 4; `tests/test_budget.py`, beside
   `test_topping_up_a_feature_puts_it_back_to_executing:651`.)
2. **The settlement pass does not reach a non-feature family.** An investigation whose only
   child is `failed` is NOT set to `failed` by `settle_features` — its verdict path owns
   that. Negative control for the split; passes today by accident, must pass by
   construction. (defect 4.)
3. **The note names the parent's own verb.** Child of an `io-` parent with a broke family:
   `set_work_order_budget` returns a note containing `jarvis io budget`; child of an `inv-`
   parent: `jarvis investigate budget`. Update
   `test_topping_a_child_up_with_a_broke_feature_says_to_raise_the_feature:1018` to assert
   the feature case stays `jarvis fo budget` — it is the negative control. (defect 2.)
4. **The show path explains the cap.** `ops.work_order_budget` on the parked child of a
   broke family returns a `note` naming the family's slice, the family budget, the
   unreserved remainder and the raise verb — and it is the SAME string
   `set_work_order_budget` returns for the same row. Assert equality, not two
   substring checks: that is what pins "one home". (defect 1.)
5. **`jarvis investigate budget` exists, shows and sets**, refuses a `wo-`/`fo-`/`io-` id
   with the kind refusal naming the command that would work, and a raise through it is
   what makes obligation 1's recovery reachable end to end. Same guard assertion for
   `jarvis io budget`. (defect 3; `tests/test_investigation_orders.py`,
   `tests/test_improvement_orders.py`.)
6. **A settled, flagged row lists.** `submit_verdict` with `WAITING_ON_USER`, then
   `jarvis investigate list` with no `--all` prints that row; a settled UNflagged
   investigation still does not. Same pair for a `failed` + flagged feature order through
   `jarvis fo list`, and for an improvement order. (defect 6.)
7. **`os_status` carries all three kinds** in one `feature_orders` list, each row with
   `kind` and a kind-aware `status_label` — an `io-` row in `planning` reads `analysing`.
   (defect 5; assert on `ops.os_status()` directly, plus one `GET /api/status` check in
   `tests/test_ui.py`.)
8. **The dashboard shows the note and does not call it an error.** POST
   `/wo/{name}/{wo_id}/budget` raising a child of a broke family redirects with `note=`
   and no `error=`, the note names the family's raise verb, and the rendered page contains
   it. Same for POST `/fo/{name}/{fo_id}/budget` carrying `exhausted_children`. (defect 7;
   `tests/test_ui.py`.)
9. **The guard against the next leak.** `tests/test_improvement_orders.py:106-113` already
   asserts no negated kind filter exists. Extend that idea: assert the family-budget pass
   in `settle_features` passes `kind=None` explicitly and the settlement pass passes
   `kind="feature"` explicitly — neither by default. A source-level assertion, like the
   existing one, because the defect is a missing argument and no behavioural test can see
   an argument that is absent on a path no fixture covers.
