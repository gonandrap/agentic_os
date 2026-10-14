# A red head is sent back before the panel

Work order wo-b4ffc787, GitHub issue 1025. Live case wo-72c3d20b / PR #1005 / fo-f02d33af.
Design decided by Neo on question 1653 — this spec writes that design down, it does not
re-open it.

Related: `2026-09-18-a-red-base-heals-itself.md` (red CI after the panel),
`2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md` (the uncounted
round and `uncounted_cause`), `2026-09-22-a-round-must-answer-the-list.md` (the bounce),
`2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md` (`PR_REPAIR_MAX_ATTEMPTS`).

## 1. The problem — four seats judged a head nothing had verified, and it cost the user

`Daemon._validate_work_order` (src/jarvis/daemon.py:2748) holds a round only while GitHub
is still RUNNING the checks:

```python
pending = evidence_mod.ci_pending(packet)
if pending and time.time() < float(round_row["ts"]) + CI_HOLD_DEADLINE_SECONDS:
    self._validation_ci_held(store, wo, round_id, n, pending)
```

`evidence.ci_pending` (src/jarvis/evidence.py:296) reads `status` against
`github.UNFINISHED_STATUSES` and is deliberately silent about `conclusion`. So when every
check has COMPLETED and some are RED, `pending` is `()`, nothing holds, and the round runs
the full ordinary path: the validator is built, four seats read a ~150k-char diff, and the
`tester` seat — whose mandate is to judge the declared evidence against the pull request's
check runs — rejects it for the one fact a single `gh` field already stated.

On wo-72c3d20b that rejection was round 3 of `max_rounds` 3, so `_validate_work_order`
took the `elif outcome == "rejected"` arm at daemon.py:2905 and escalated: `needs_review`,
an attention item, the user asked to decide something the OS could see mechanically.

Three distinct costs, in order of severity:

1. **A counted round spent on a machine check.** The budget `max_rounds` bounds exists to
   bound the worker's attempts at a JUDGEMENT. A red build is not a judgement.
2. **A user attention item** at the end of that, for a defect with a known remedy.
3. **Four seat calls** at panel prices for a one-line answer.

The root cause is not the tester seat and not `max_rounds`. It is that `ci_pending` is the
only CI fact the round machine consults, and it answers one of CI's three worlds. The
other two — green, and red — are both routed to the seats, and only one of them should be.

`ci_pending`'s own docstring currently argues for today's behaviour:

> A RED check is NOT pending: the answer is in, it is bad, and the round should open so a
> seat can say so.

That sentence is wrong on both halves and **the implementation must correct it**. A seat
adds nothing a machine check has not already said — the same reasoning
`evidence.nothing_to_judge` already applies to a staged release, which is VOIDED rather
than judged "because no seat can add anything to a machine check" (daemon.py:2783). And
saying it costs a counted round, which `ci_pending` was not written to notice because when
it shipped (wo-3676e2a0, issue 1020) the question in front of it was only the wait.

## 2. The shape in one paragraph

Between the `ci_pending` hold and the validator, one new branch: no check pending and at
least one check red means **seat nobody, reject the round UNCOUNTED, and send the worker
the failing check names over the ordinary reviewer→implementor bus envelope**. Bounded by
a catalog key, `validation.red_ci_returns`, default 3. Past the bound the round falls
through to the ordinary counted path and the seats judge the red head as they do today, so
red CI alone never escalates and a worker that cannot get CI green still reaches the user
by the existing route with the panel's words.

## 3. The fix — a third CI world, read in `evidence`, acted on in the round machine

### 3.1 `evidence.ci_failing`, beside `ci_pending`

A new pure function in src/jarvis/evidence.py, immediately after `ci_pending`:

```python
def ci_failing(packet: EvidencePacket) -> tuple[str, ...]:
    """The names of the checks that have FINISHED and say the code is wrong."""
```

Three reasons it belongs there and not in `daemon` or `github`:

* **It takes an `EvidencePacket`.** `github.failing_checks` takes a tuple of normalised
  check dicts and `PullRequest.failing` takes a `PullRequest`; neither can be handed a
  packet, whose `pr` is a plain dict built at evidence.py:614 (`"checks": [dict(c) for c
  in art.checks]`). `ci_pending` exists for exactly this reason and this is its pair.
* **Same two non-answers, same `()`.** `packet.source != "pull_request"`, no `packet.pr`,
  no checks configured, and all-green all return `()`. The round machine reads "send it
  back" from this, and must not be handed a send-back nothing happened for.
* **`evidence` may not touch a store** (evidence.py:184) and this needs none.

**The RED predicate is not re-derived.** The body normalises each check dict and calls
`github.failing_checks`, so `github.RED_CONCLUSIONS` stays the one definition of red in
the OS — the whole point of `github.red_checks`' docstring ("which checks are red" having
two answers is the shape of issue #224). Normalising with `.get(...) or ""` rather than
passing `packet.pr["checks"]` straight through is deliberate: `failing_checks` subscripts
`c["conclusion"]`, and hand-built packets in tests and injected validators are legitimate.

`ci_pending`'s docstring loses its last two sentences and gains a pointer to `ci_failing`
and to this spec. The `status`/`conclusion` distinction stays — it is still true and still
load-bearing.

### 3.2 The branch, and where it goes

In `Daemon._validate_work_order`, immediately after the `ci_pending` hold at
daemon.py:2748-2753 and before `validator = (...)` at :2755:

```python
failing = evidence_mod.ci_failing(packet)          # empty while anything is pending
if not pending and failing and returns < cfg.red_ci_returns:
    self._validation_red_ci(store, wo, round_id, n, failing, pr_url=...)
    return
```

`not pending` is explicit and is not an optimisation: a pull request with one red check and
one still queued has not finished reporting, the queued one may change what the worker
has to fix, and the round is already held by the clause above — but the hold lapses at
`CI_HOLD_DEADLINE_SECONDS`, and after that lapse a partially-reported pull request must
reach the seats (the fail-SAFE direction `CI_HOLD_DEADLINE_SECONDS`' comment argues for),
not be returned on an incomplete list of names.

**AFTER `store.set_validation_head` / `set_validation_file_shas`** (:2727, :2731), for the
reason the `ci_pending` hold is also after them: the head and the file map are facts about
the PACKET, true whatever happens next, and a return that recorded neither could not later
say which commit it returned or be diffed against by the next submission.

**BEFORE the validator is built**, because the validator is the expense this exists to
avoid, and because `nothing_to_judge`, `_repeat_submission` and the seats all read a
submission the OS has already decided not to judge.

Not before the `ci_pending` hold: a red-and-still-running pull request is a wait, and
ordering this first would convert every wait whose first check failed into a return.

### 3.3 `Daemon._validation_red_ci` — a settle, not a hold

New method beside `_validation_ci_held`, and it closes the round through the existing
`_reject` path:

* `store.close_validation_round(round_id, "rejected", reason)` — `_reject` already does
  this; `hold_cause` stays None, because nothing is holding the round.
* The round row is marked `uncounted=1`, `uncounted_cause = ops.RED_CI_CAUSE` (`"red_ci"`,
  a new constant beside `REBIND_CAUSE` and `USER_REWORK_CAUSE` in src/jarvis/ops.py:7205).
  The round was OPENED counted — `ops.submit_for_validation` had no way to know CI would
  be red — so the implementation needs a store write that marks an open round uncounted
  (`ProjectStore.mark_validation_round_uncounted(round_id, cause)` or an optional
  `uncounted_cause=` on `close_validation_round`; either is fine, one home for the UPDATE).
  Getting this wrong in the other direction is the whole defect: a counted close here
  would spend the round this spec exists to save.
* `_reject` is extended with the new cause, which selects a new feedback template beside
  `REVIEW_FEEDBACK` / `REBIND_FEEDBACK` / `USER_REWORK_FEEDBACK` (daemon.py:385-416) and
  puts `"uncounted": True, "uncounted_cause": "red_ci"` on the `validation_rejected`
  event. `_reject`'s existing `if rebind is not None or rework` guard must learn the third
  cause, or the event would say `uncounted_cause` without `uncounted` and every surface
  reading the pair would disagree with the row.
* The envelope goes out through `bus.post(... from_role="reviewer", to_role="implementor",
  payload=bus.ReviewFeedback(...))` — unchanged, and that sharing is the point: the
  router decides what fills the implementor role, so this works for anything that
  resubmits.

### 3.4 The envelope: a reference, never a payload

`RED_CI_FEEDBACK`, shaped on `USER_REWORK_FEEDBACK` (no round number — none was spent, and
a number here would teach the worker it has fewer attempts than it has), and it must:

* **name the failing checks**, truncated the way `_validation_ci_held` truncates pending
  names (`", ".join(names[:6])` plus `" …"`);
* **point at the logs rather than carry them**: `gh pr checks <pr_url>`. No log text, no
  job output, no excerpt. A failure log is unbounded, model-hostile input the worker can
  fetch itself, and `2026-09-26-bounded-model-inputs.md` is the standing rule;
* **say no round was spent**, and that the next submission is judged normally;
* **say what the two legitimate remedies are**, because the live case is the second one:
  fix the build, or — when the branch cannot pass alone because it depends on an unmerged
  sibling — stack onto that sibling's branch or wait for it to merge and resubmit. This
  wording is the whole of the OS's answer to issue 1025's gap 2 (§6.2).

The round's stored `reason` is the same sentence minus the instructions, by `_reject`'s
existing split: the reason is the record, the template is what the submitter reads.

## 4. The cap is a catalog key

`validation.red_ci_returns`, resolvable fleet-wide and per project exactly like
`validation.max_rounds`. Neo's standing rule — a bound the user may want to move is
configuration, not a module constant (cf. `RELEASE_REFILE_WINDOW_SECONDS`,
`FALSE_POSITIVE_DISARM`). Four touch points, all of them existing:

1. `catalog.DEFAULT_VALIDATION_RED_CI_RETURNS = 3`, beside
   `DEFAULT_VALIDATION_MAX_ROUNDS` (src/jarvis/catalog.py:531).
2. A `red_ci_returns: int` field on `catalog.ValidationConfig`.
3. `catalog._parse_validation` (catalog.py:2060): `int(raw.get("red_ci_returns",
   base.red_ci_returns))`, refused with `_err(f"{where}.red_ci_returns must be >= 0")`.
   **`>= 0`, not `>= 1`**, and `0` is a legal, meaningful setting: never return, send
   every red head to the seats — today's behaviour exactly, which is what a project that
   disagrees with this feature needs and what makes the regression test in §7 expressible
   as configuration rather than as a patch. `max_rounds` is `>= 1` because a unit with
   zero rounds can never be judged at all; zero returns is not that.
4. Nothing in `config_version.validation_config_from_resolved` (config_version.py:183) —
   it walks `fields(catalog.ValidationConfig)`, so a new field is carried into the stamped
   historical read for free. This matters: `Daemon._round_config` judges a round under the
   version it was OPENED under, so the bound a round is returned against is the bound that
   was live when the worker submitted.

**Default 3, and the argument is not freshly invented.** This is the same problem
`invariants.PR_REPAIR_MAX_ATTEMPTS = 3` already bounds — "how many times the OS asks a
worker to fix its own red build before it asks the user instead" — one authority earlier
in the lifecycle. Two different numbers for one question would be a defect; the same
number derived independently is the honest statement that they are the same question asked
in two states.

**Counting the episode:** `store.uncounted_validation_rounds(wo_id=wo_id,
cause=ops.RED_CI_CAUSE)`, the identical arithmetic the rebind cap uses at daemon.py:2706.
That method counts every row settled or not, filtered to one cause — which is exactly the
separation `uncounted_cause` exists for (wo-299daf2e, Neo question 973): a red-CI return
must not eat the free round a user's rework needs, and a rebind must not eat a red-CI
return.

**Past the cap, fall through.** No escalation, no refusal, no new terminal state: the
branch's condition simply goes false and the round continues into the validator as an
ordinary counted round. So:

* red CI **alone** can never produce a `needs_review`;
* a worker that cannot get CI green reaches the user by the route that already exists,
  after the seats have read the work, with the panel's words rather than a check name;
* `max_rounds` keeps governing the escalation, so the escalation surfaces, the attention
  reason and `invariants.VALIDATION_STUCK_BLOCKER` need no change.

## 5. The guards this new way to settle a round must answer

kn-559478c4's general rule: a new terminal path through the round machine must be walked
past every reader that keys on how a round settled. kn-48dadcce on the two counters.

### 5.1 `Daemon._repeat_submission` — the exemption this feature cannot ship without

The live case: wo-72c3d20b's branch was correct and could not pass CI alone, because it
needed sibling PR #1023 merged first. The right worker behaviour after a red-CI return is
**to wait and resubmit the same branch**. `_repeat_submission` (daemon.py:2956) compares
this round's `fingerprint` to the immediately preceding round's, matches, and
`_escalate`s with "identical to round N" — turning the fix into the very attention item
this spec removes, one round later.

**Rule: a submission whose immediately preceding round was a red-CI return is exempt from
the repeat guard.** The `previous` row `_validate_work_order` already fetches at
daemon.py:2800 via `_preceding_round` is the input; the exemption is
`str(previous["uncounted_cause"] or "") == ops.RED_CI_CAUSE`.

Keyed on **this submission's PRECEDING round**, deliberately:

* not on "any red_ci round in this work order's history" — that would disarm the repeat
  guard for the rest of the order's life, and an unchanged resubmission three rounds later
  is exactly the stall the guard exists for;
* not on the CURRENT round's packet being green — the packet is collected before any
  verdict and a worker may resubmit while CI is still running;
* not on `last_judged_round` — `_repeat_submission` is a statement about consecutive
  submissions and must stay one (its own docstring: comparing against every earlier round
  "would punish a submitter that was told to go back to a shape it had already tried").

**Two in a row is pinned, not accidental.** Red CI return 1, worker waits, resubmits
unchanged, CI is still red (the sibling has not merged), return 2 — the preceding round is
again a red_ci return, so again exempt, and the `red_ci_returns` bound is what stops the
loop rather than the repeat guard. That is correct: the repeat guard protects the SEATS
from re-reading identical work, and these returns never reach a seat. At the bound the
round falls through and the repeat guard is live again on the same unchanged
fingerprint — so an unchanged third submission against red CI escalates as "identical to
round N", with the budget intact. Pin that whole sequence in one test.

### 5.2 `ops.submit_for_validation`'s rebind derivation — no promotion

ops.py:5512-5520 promotes an ordinary submission to `uncounted` when `last_judged_round`
is a rejected uncounted round **whose cause is `REBIND_CAUSE`**.

**A red-CI return must NOT be promoted.** The reasoning that justifies promoting after a
rebind is a budget argument: the OS demanded a merge, the worker spent a round complying,
so the fix is free. Nothing here was spent — the return cost zero seats and zero counted
rounds — so the fix is an ordinary counted round, and promoting it would hand a worker an
unbounded supply of free rounds by pushing a red commit.

The existing `== REBIND_CAUSE` filter already gives this, and the `elif
user_rework_pending(...)` arm is unreachable for a red_ci round because no user rejection
is involved. **No code change in `ops.submit_for_validation`** — but one test pins it, for
the reason Neo question 973 exists: this filter was once unfiltered, and the behaviour we
are relying on is a consequence of someone else's bug fix.

### 5.3 `ops.unanswered_paths` / `BOUNCE_LIMIT` — it can be the last judged round, and that is harmless by construction

A red_ci round closes `rejected`, and `"rejected"` is in
`project_store.COUNTED_VALIDATION_OUTCOMES`, so **yes: `store.last_judged_round` can
return it**, and `ops.unanswered_paths` (ops.py:5410) will read it on the next submission.

Read the code and it fails open:

* `unanswered_paths` collects blockers from `store.validation_opinions(previous["id"])`. A
  red_ci round has **no opinion rows** — no seat ran — so `raised` is `[]`.
* `validation.unanswered_submission` (validation.py:855) then computes `cited =
  cited_paths([])`, which is empty, and returns `None` on `if not cited`. Its docstring
  states this contract: "blockers that cited no path at all … EVERY UNCERTAINTY RETURNS
  None, which means let the panel judge."

So no bounce fires, `ops.consecutive_bounces` sees nothing, `BOUNCE_LIMIT` is untouched.
**Decision: leave it reachable, change nothing, and pin it.** The alternative — teaching
`last_judged_round` or `unanswered_paths` to skip red_ci rounds — would add a cause filter
to a method three other callers share in order to buy a behaviour the data already
guarantees, and a check-name reason genuinely cites no paths, which is the case the
fail-open was written for. The pin is the point: the no-bounce property holds because a
red_ci round has no opinions, and a future change that gave this path a synthetic opinion
row would break it silently.

### 5.4 `ops.prior_round_history` — yes, the seats see it

`prior_round_history` (ops.py:6048) filters on `COUNTED_VALIDATION_OUTCOMES`, so a red_ci
round **does** reach the next round's packet as history, with `blockers: []`, its
`head_sha`, and its stored reason.

**Decision: correct, no change.** The docstring's exclusion argument is about rounds whose
reason is "about the OS rather than about the code" — `pending` and `failed`. A red-CI
return's reason is about the SUBMISSION: its checks were red, and these were their names.
A seat reading "round 2 was returned without a panel because `test (3.12)` was failing" is
better informed, not misinformed, and the `blockers: []` keeps it out of the
answered-the-list machinery.

One wording obligation follows: the round's stored reason must be self-describing — it
says the checks were red and that no panel judged it — so a seat cannot read it as a
quality finding a previous seat made.

## 6. Scope

### 6.1 Work-order rounds only

The branch lives in `_validate_work_order` and cannot fire in `Daemon.validate_features`:
`ci_failing` returns `()` unless `packet.source == "pull_request"`, and a feature round's
packet is collected by `evidence.collect_feature_evidence`, which does not produce that
source. Nothing is added to the feature loop — not even a guard — because the guard is the
helper's own first clause, in one place, for both loops.

If feature rounds ever carry a pull request, three things would have to be decided before
this branch could be copied: where the envelope goes (a manager does not fix code — cf.
`FEATURE_REVIEW_FEEDBACK`, daemon.py:425), which carrier the event takes (a feature's
events live on its manager's timeline via `ops.feature_event`), and whether
`red_ci_returns` is counted per feature or per child.

### 6.2 Rejected alternatives

**Detect the dependency from the worker's prose.** Issue 1025's gap 2 is that wo-72c3d20b
declared "this cannot pass CI until #1023 merges" only in its `result_summary`. Rejected,
and Neo confirmed it on question 1653: no prose detection anywhere, and **no new refusal
in `ops.submit_for_validation`**. Any such detector is a classifier over free text, i.e. a
second judge of a submission sitting in front of the panel, exactly what
`validation.unanswered_submission` is written to never become ("not a second reviewer and
deliberately incapable of becoming one"). The mechanical red-CI return already covers the
case — the branch is red, it is returned, no round is spent — and §3.4's wording is what
tells the worker to stack its base or wait. A worker that must declare a dependency has
`--depends-on` at creation time.

**Let the existing PR repair handle it.** `ops.PrRepair` / `PR_CHECKS_REPAIR` already
nudges a worker about a red build. It cannot reach this case: it is driven by
`Daemon.poll_pull_requests` over `invariants.PR_REPAIR_STATUSES` (`waiting_pr_merge`,
`needs_review`, `waiting_input`, `failed`), and a work order waiting on a round is
`validating`. Adding `validating` to that tuple would put the poll and the round machine
in charge of one pull request at once — two writers nudging one worker — which is why the
bound is shared (§4) and the mechanism is not.

**Hold the round instead of settling it.** §6.3.

**A module constant for the cap.** Refused by Neo's standing rule; and the stamped-config
read (§4.4) means a catalog key is also the only way the bound a round is judged under
matches the bound that was live when it was submitted.

**Void the round, as `nothing_to_judge` does for a staged release.** `void` says nobody
needed to judge this and nobody has to act. Here the WORKER has to act, and `void` sends
no feedback envelope — the work order would sit with a green record and a red branch.

### 6.3 Why this settles and does not hold

The `ci_pending` case is a **wait the OS owns**: nothing is billed, the round closes
`failed` (which is `RUNNABLE` and invisible to `counted_validation_rounds`), `reopens_at`
goes on the event, and the next `validation_tick` picks the same round up again. Nobody
outside the daemon has anything to do.

A red head is not a wait. **The WORKER has to act** — push a fix, restack the branch, or
wait for a sibling and resubmit — and a held round holds no worker: it sends no envelope,
flips nothing back to `running`, and would sit re-closing itself every 60 seconds until
`CI_HOLD_DEADLINE_SECONDS` lapsed and the seats read the red head anyway. That is the
status quo with 45 minutes added.

Consequence worth stating: **because it settles rather than holds, none of the hold
accounting changes.** `holds.union_seconds`, `stuck.excluded_cause` and the
`validation_ci_hold_spans` discount (kn-0a222b38) are untouched; `hold_cause` stays NULL
on the round; `RED_CI_CAUSE` is an `uncounted_cause`, NOT a member of
`VALIDATION_HOLDING_CAUSES`, and `close_validation_round`'s assertion on that vocabulary
is what keeps the two from being confused.

## 7. The record

The `validation_rejected` event carries `uncounted: true`, `uncounted_cause: "red_ci"`,
the round NUMBER in `"round"` (unchanged — `holds.py` correlates it with
`validation_submitted`, and a budget position there would leave a hold open for ever),
`of`, and `budget_round`. Add the failing check names to the event payload, truncated like
the envelope's: the record must be able to say what it returned for without re-querying
GitHub, whose check list has moved on by then.

What the surfaces render, with no change to any of them:

* `jarvis wo show` — the round reads `rejected` via `project_store.validation_standing`
  (`hold_cause` is NULL, so the plain `rejected` standing applies), with the check-name
  reason. The timeline renders the `validation_rejected` event; the existing uncounted
  wording must cover the third cause so the round does not read as a spent one.
* `jarvis validation show` — `ops.validation_detail` attaches `"opinions":
  store.validation_opinions(rnd["id"])`, which is `[]` here. **A round with no seats is
  not new**: an outage round and a no-validator round both have none today, so the
  renderers already tolerate it. What the user sees is a round with a reason and no
  deliberation, which is the truth — the reason names the checks and points at
  `gh pr checks`, so the surface is not a dead end.
* Attention: none. `ops.submit_for_validation` already cleared the flag, and a return is
  not a give-up.

## 8. Test obligations

1. **A completed-red pull request seats nobody and spends no counted round.** An injected
   validator that would raise if called; assert zero calls, `counted_validation_rounds`
   unchanged, the round `rejected` with `uncounted=1` and `uncounted_cause="red_ci"`, and
   the work order back to `running` once the envelope is delivered.
2. **The envelope names the failing checks** and contains `gh pr checks <pr_url>`, and
   contains NO log text. Assert the check names are in the delivered message, and that it
   says no round was spent.
3. **Green and still-pending behaviour unchanged** — a regression run of
   tests/test_validation_ci_hold.py plus a green-CI case that reaches the seats, and a
   mixed case (one red, one `IN_PROGRESS`) that **holds**, not returns.
4. **Past the cap the seats judge it.** With `red_ci_returns` returns already recorded, the
   same red packet reaches the validator, spends a counted round, and at `max_rounds`
   escalates by the existing route.
5. **The repeat-submission exemption**, as the three-step sequence of §5.1: exempt, exempt
   again, then — past the cap — escalated as "identical to round N".
6. **No rebind promotion**: a submission after a red_ci return opens a round with
   `uncounted=0`.
7. **No bounce**: a submission after a red_ci return is not sent back by
   `unanswered_paths`, and `consecutive_bounces` is 0.
8. **The catalog key** parses fleet-wide and per project (a project naming only
   `red_ci_returns` inherits the rest), `0` is accepted and disables the branch, `-1` is a
   `CatalogError` naming the key, and a round stamped with an older config version is
   judged under the bound it was opened with.
