# A verdict bound to no commit re-judges itself

- Work order: wo-d1caedb0 (jarvis_os)
- Issue: https://github.com/gonandrap/agentic_os/issues/1095
- Design ruling: Neo question 1938, option A. Settled — this spec implements it, it does
  not re-open it.
- Line numbers are 1-based and read against this worktree
  (`/home/gonzalo/workspace/agentic_os/.claude/worktrees/wo-d1caedb0`).

## The problem

A passed validation round that recorded no judged commit parks its pull request in
`waiting_pr_merge` for ever, and tells the user nothing. Two independent defects, one
chain.

### Chain

1. `evidence.judged_head` returns `""` when the packet has no pull request behind it —
   including the case where the PR fetch FAILED and fell back to the worktree
   (`EvidencePacket.pr_error`, `src/jarvis/evidence.py:426`, and its docstring at 436-439
   naming `""` the fail-closed value). `project_store.set_validation_head` stores that, so
   the round settles `passed` with `head_sha = ''`.
2. `automerge.decide` takes its `if not judged:` branch (`src/jarvis/automerge.py:410`)
   and, because `outcome == "passed"`, returns `HELD_SHA_UNRECORDED` at
   `src/jarvis/automerge.py:417` with `head_sha` = the LIVE PR head and `judged_sha`
   empty.
3. `Daemon.auto_merge`'s self-heal arm (`src/jarvis/daemon.py:7953-7964`) re-judges only
   `decision.code == automerge.HELD_SHA_MOVED`. `HELD_SHA_UNRECORDED` falls to
   `_note_automerge_held` and `return`. Nothing re-opens a round, ever. The hold is
   re-derived every poll tick; the only exit is a human `jarvis validation force`.
4. Second defect — the issue only hints at it. `invariants._parked_on_a_decline`
   (`src/jarvis/invariants.py:872`) requires the newest `automerge_held` payload's code to
   be `HELD_SHA_MOVED` (`src/jarvis/invariants.py:890`). So even if the re-judge arm
   existed and DECLINED on a spent round budget, `true_blockers`
   (`src/jarvis/invariants.py:1274-1279`) would derive no sentence: the decline event is
   written, and the user is never told the remedy is `validation.max_rounds`. That is the
   issue's "nothing tells the user that is what is needed".

### Evidence

- wo-5a7f71b5 / PR #1091: over an hour in `waiting_pr_merge`, PR OPEN / MERGEABLE / CLEAN
  at `60c2e976`, latest round passed, `head_sha` empty, `automerge_held` re-derived on
  every tick with code `sha_unrecorded`.
- `ops.force_validation`'s docstring (`src/jarvis/ops.py:6211-6218`) states the premise
  this incident falsifies: that the `sha_unrecorded` population is rounds judged BEFORE
  jarvis-0.10.0 introduced `validation_rounds.head_sha`. A GitHub timeout on the evidence
  fetch produces it live, on a round judged today.

### Root cause, and what is deliberately left alone

Root cause of the stall: the self-heal arm is keyed on ONE hold code when the property it
heals — "a passed round exists, its commit binding is unusable, and the pull request is
otherwise ready" — has two. Not keyed on anything about the evidence fetch.

Nothing records WHY the head is empty (no PR at all, versus `pr_error`). A
cause-distinguishing column was considered and REJECTED: a readable pull request plus a
passed round with no commit bound is the same stall whatever produced it, and the existing
bounds already cap the legacy pre-0.10.0 population at one round per commit
(`ops.rejudged_heads`, `src/jarvis/ops.py:6347`). So the trigger is ANY
`HELD_SHA_UNRECORDED` hold.

The upstream fallback itself — `evidence` quietly judging a worktree when `gh` times out,
rather than deferring the round — is NOT fixed here. That is a separate, larger decision
about round scheduling; this spec makes its consequence self-healing instead of permanent.
Symptom-level by choice, and said plainly.

## The fix

Five changes. Each names where it lives and why there.

### 1. The re-judge arm admits the second code

`src/jarvis/daemon.py:7955` — the condition on the self-heal arm becomes
`decision.code in (automerge.HELD_SHA_MOVED, automerge.HELD_SHA_UNRECORDED)`. The guard
call and `self._rejudge_moved_head(...)` below it are unchanged.

`_carry_catch_up`'s arm above it (`src/jarvis/daemon.py:7888`) STAYS `HELD_SHA_MOVED`-only.
A carry rebinds an EXISTING judged commit onto a new head; here there is no judged commit
to carry, so admitting the code there would ask the rebind machinery to prove a merge added
nothing unjudged relative to nothing. `rebind=outcome.rebind` therefore arrives `False` on
this path by construction — `outcome` is only set by that arm — so the re-judge is a
COUNTED round. That is the intent, not an accident: see change 3.

### 2. The guard is reused unchanged

`automerge.only_the_head_moved` (`src/jarvis/automerge.py:467`) is NOT modified. Only its
docstring's scope sentence at `src/jarvis/automerge.py:478` widens from "ONLY MEANINGFUL
AFTER A `HELD_SHA_MOVED`" to name both codes.

Soundness, which the docstring must carry: the function answers "would this PR merge if a
round had judged the head it has" by re-asking `decide` with the live head substituted for
the judged one. The worry that sentence guards against is reading a round that never passed
as a near miss. `HELD_SHA_UNRECORDED` is reachable ONLY when `outcome == "passed"` — it is
the `round_row is not None and outcome == "passed"` arm at
`src/jarvis/automerge.py:416-420`, and every other way into `if not judged:` returns
`HELD_NOT_PASSED`. So substitution cannot manufacture a pass. Conditions 4 and 5 are
satisfied by construction exactly as on the moved-head path, and conditions 1-3, 6 and the
base facts are still judged for real.

`HELD_NOT_PASSED` stays OUT. A round that is not `passed` is a verdict the panel meant to
stand. The separate gap there is the red_ci one recorded in kn-bbc03206
(`jarvis learn show kn-bbc03206`); this work order must not touch it.

### 3. The reason text must not claim the head moved

`ops.rejudge_moved_head` (`src/jarvis/ops.py:6390`) currently always falls to
`REJUDGE_FORCED_REASON` (`src/jarvis/ops.py:6282`) on the non-rework, non-rebind arm
(`src/jarvis/ops.py:6485`), whose text is "round {n} passed on {judged}, and the head of
the pull request is now {head}". With `judged_sha` empty that renders a false claim about
a commit, in a string `round_line` and `timeline._describe` show verbatim.

Add a constant beside it — same block, `src/jarvis/ops.py:6282-6309`, with the same `#:`
commentary discipline — selected in the `else` arm when `judged` is empty. It must say:
round N passed WITHOUT recording which commit it judged (it read a worktree rather than the
pull request), and the pull request is being judged at `{head}`. It must not contain the
words "moved", "older" or `{judged}`.

Beside it, `Daemon._rejudge_moved_head`'s success log
(`src/jarvis/daemon.py:9268-9270`, "the head moved from %s to %s") renders as "from  to
60c2e976" with an empty `judged_sha`. Same false claim, log-level. Fix in the same change.

Counting and bounds are UNCHANGED:

- It is a COUNTED round — not a rebind (nothing was judged to rebind), not a user rework
  (the user asked for nothing). So the `cause` stays `""` at `src/jarvis/ops.py:6487` and
  `uncounted=False`, and the budget decline at `src/jarvis/ops.py:6466` governs it.
- The spend is bounded with no new code: `ops.rejudged_heads`
  (`src/jarvis/ops.py:6347`) dedupes per head commit via the `validation_forced` payload's
  `head_sha`, checked at `src/jarvis/ops.py:6434`. A pull request whose fetch keeps timing
  out therefore costs ONE round per distinct head commit, not one per tick — a repeatedly
  unreachable PR at a static head costs nothing extra. The decline dedupe at
  `src/jarvis/ops.py:6457`/`6467` is per head across both causes and needs no change.

### 4. The decline reaches the user

`invariants._parked_on_a_decline` (`src/jarvis/invariants.py:872`) gains a parameter for
the hold code it accepts; the check at `src/jarvis/invariants.py:890` compares against it
instead of the literal `HELD_SHA_MOVED`. Add `HELD_SHA_UNRECORDED` to the import at
`src/jarvis/invariants.py:48`.

- `rejudge_exhausted` (`src/jarvis/invariants.py:841`) and `rebind_exhausted`
  (`src/jarvis/invariants.py:861`) pass `HELD_SHA_MOVED` and keep their current meaning
  exactly.
- New predicate beside them — `sha_unrecorded_exhausted` — passes `HELD_SHA_UNRECORDED`
  with `rebind=False`. `rebind=True` is unreachable on this code: the only writer of a
  `REBIND_EXHAUSTED` decline is the `rebind` arm, which only the carry sets, and change 1
  keeps the carry on `HELD_SHA_MOVED`.
- The three stay MUTUALLY EXCLUSIVE and still by construction, not by luck: all three read
  the SAME newest `automerge_held` payload and demand different codes of it, and the two
  `SHA_MOVED` ones are already separated by the per-head decline cause (spec 2026-09-27
  §4.5).

New blocker constant in the same family as `SHA_MOVED_BLOCKER`
(`src/jarvis/invariants.py:321`) and `REBIND_EXHAUSTED_BLOCKER`
(`src/jarvis/invariants.py:333`), with the same `#:` note saying why it is a third sentence
rather than a reuse. `SHA_MOVED_BLOCKER` is WRONG here: "the panel's verdict names an older
commit" is false — it names NONE. The new sentence must say the round passed without
recording which commit it judged, that the OS has no round left to bind one, and offer the
same three remedies that are actually live: merge it by hand, `jarvis validation force`, or
raise `validation.max_rounds` and the OS re-judges it itself (the last is true here —
unlike on the rebind arm, this path reads `cfg.max_rounds` at `src/jarvis/ops.py:6466`).

Wiring: `true_blockers`, beside the two existing arms at
`src/jarvis/invariants.py:1274-1279`, same `wo["status"] == "waiting_pr_merge"` gate so no
other order pays for the reads, with a comment noting the three cannot co-occur.

Why the last clause still self-clears correctly. `_parked_on_a_decline` ends on
`store.validated_head(store.latest_validation_round(...)) != head`
(`src/jarvis/invariants.py:901-902`). Three cases:

- The user raises `max_rounds`, the next tick re-judges at head H and the round records H
  — `validated_head == H`, clause false, flag drops within a tick. Same self-clearing
  property as the moved-head case.
- The forced round runs but the PR fetch fails AGAIN, so it records `""` — `"" != H`,
  clause true, the blocker stays. Correct: the order IS still stalled, and the newest hold
  is `sha_unrecorded` again.
- The head moves to H2 — the newest hold is a fresh `sha_unrecorded` at H2, the decline
  events do not carry H2 (`src/jarvis/invariants.py:893-896`), the flag drops and the
  re-judge path owns the order again.

`store.validated_head` is the single source `decide` is forbidden to re-derive, so no new
reader of "which commit did the panel accept" is introduced anywhere in this change.

### 4b. The dedupe must not be silent either

One path reaches no user at all, and it is the one the heal creates. The OS spends its one
round on head H, that round records `''` AGAIN (the fetch failed twice), `decide` holds on
`HELD_SHA_UNRECORDED` at H once more — and `rejudge_moved_head` now returns at its first
guard (`src/jarvis/ops.py:6454`, `head in rejudged_heads`) every tick, writing NOTHING. No
decline, so `sha_unrecorded_exhausted` is false and the order is parked green and silent
with an untouched round budget.

`invariants.sha_unrecorded_rejudge_spent` is the derivation for it, beside
`sha_unrecorded_exhausted`: newest `automerge_held` is `HELD_SHA_UNRECORDED` on a non-empty
head, that head is in `ops.rejudged_heads`, and `store.validated_head(latest round) !=
head` — §4's tail clause verbatim, for §4's reason. NOT `_parked_on_a_decline` with a
fourth argument: that function requires a `REJUDGE_DECLINED_EVENT` and this case has none.

A FOURTH sentence, `SHA_UNRECORDED_SPENT_BLOCKER`, rather than a reuse of the third:
`SHA_UNRECORDED_BLOCKER` says "no rounds are left to bind one" and offers
`validation.max_rounds`, and both are FALSE here — the budget was never spent and the
per-head dedupe is permanent, so another round changes nothing on this commit. The honest
remedies are two: merge it by hand, or `jarvis validation force` once GitHub is reachable.

Mutual exclusivity with the three stays by construction: they all require a decline for the
held head, this one requires an OS `validation_forced` for it, and the dedupe returns before
either decline arm can write one for a head the OS has already judged.

### 5. The docstring stops calling it a migration population

`ops.force_validation` (`src/jarvis/ops.py:6207`), the "Why it exists" paragraph at
`src/jarvis/ops.py:6211-6218`. It currently grounds the whole `sha_unrecorded` population
in rounds judged before jarvis-0.10.0. Rewrite to say the population has two sources: the
pre-0.10.0 rounds, AND rounds judged live whose evidence packet could not fetch the pull
request and fell back to the worktree (`evidence.EvidencePacket.pr_error`,
`evidence.judged_head`) — and that since this spec the OS re-judges the second kind itself,
so the command is the user's remedy when the OS's round budget is spent, not the only
route. Refusal text at `src/jarvis/ops.py:6188-6191` is accurate and unchanged.

## Rejected alternatives

1. **Record WHY the head is empty** (a cause column, or `pr_error` on the round) and
   re-judge only the `pr_error` ones. Rejected by Neo on question 1938: a readable pull
   request plus a passed round with no commit bound is the same stall whatever produced it,
   the remedy is identical, and the legacy population is already bounded at one round per
   head by `ops.rejudged_heads`. Buys a schema change and a second classification to keep
   honest, for no behaviour difference.
2. **Fix it upstream — defer the round when the PR fetch fails** instead of judging a
   worktree. The genuinely correct fix and out of scope: it changes when rounds open, which
   is the round machine's scheduling contract, and it leaves every already-parked order
   (wo-5a7f71b5 among them) stalled. Named as the root cause above.
3. **Make `jarvis validation force` the answer and just document it.** This is the status
   quo. It requires a human to notice a silent stall that raises no attention item — the
   second half of the defect — and the whole point of the 2026-09-19 moved-head spec was
   that the OS heals this class itself.
4. **Reuse `SHA_MOVED_BLOCKER` for the decline.** Cheapest, and it writes a false sentence:
   there is no older commit. The `REBIND_EXHAUSTED_BLOCKER` precedent
   (`src/jarvis/invariants.py:326-335`) is exactly this judgement already made once — a
   third stall gets a third sentence when the remedy or the claim differs.
5. **Admit `HELD_NOT_PASSED` to the arm too**, since it is the other `not judged` exit.
   Rejected: it is a verdict the panel meant to stand, `only_the_head_moved` would read it
   as a near miss, and the real gap behind it is the red_ci one in kn-bbc03206.

## Out of scope — explicitly

- `rules.py`'s self-evolution detector table. A detector for a gap this change closes is
  noise.
- `remedies.ATTENTION_TEMPLATES`. No rule writes the new blocker's key; the sentence
  reaches the user through `true_blockers` alone.
- The `HELD_NOT_PASSED` red_ci gap (kn-bbc03206). Separate work order.
- The `evidence` worktree fallback itself (rejected alternative 2).
- Anything on the `_carry_catch_up` / rebind path.

## Tests

Home: `tests/test_rejudge_moved_head.py` — these are the same mechanism and belong beside
the moved-head cases, not in a new file.

1. **It heals.** Parked `waiting_pr_merge` order, latest round `passed` with `head_sha=''`,
   PR readable, OPEN, green, CLEAN, mergeable, no pending assumption, budget left. One poll
   tick yields exactly ONE `validation_forced` event with `by == ops.REJUDGE_BY_OS`,
   `head_sha` = the live head, `judged_sha == ''`, and the round is COUNTED
   (`uncounted_cause` empty).
2. **It does not repeat.** A second tick at the same head writes no further
   `validation_forced` and no `REJUDGE_DECLINED_EVENT` — `ops.rejudged_heads`' dedupe.
3. **The guard refuses when anything else also holds the merge.** Same fixture with red CI;
   and again with a pending assumption. No round is opened in either.
4. **The decline reaches the user.** Same fixture with the round budget spent: exactly one
   `REJUDGE_DECLINED_EVENT` with `cause == ops.REJUDGE_BUDGET_SPENT`, and
   `invariants.true_blockers` then yields the NEW sentence — not `SHA_MOVED_BLOCKER`, not
   `REBIND_EXHAUSTED_BLOCKER`. Plus: after a later round records the live head, the blocker
   is gone.
5. **The reason text never claims the head moved.** The stored `forced_reason` and the
   `validation_forced` payload contain neither "moved" nor the empty judged sha rendered as
   a commit.
6. **`HELD_NOT_PASSED` is still never re-judged.** A latest round that is `rejected` /
   `pending` / `failed` on an otherwise-green parked PR opens no round. Regression lock on
   change 2's exclusion.

Run the TARGETED suites before the pull request — `tests/test_rejudge_moved_head.py`,
`test_automerge.py`, `test_forced_validation.py`, `test_rules_tick.py`,
`test_validation_surfaces.py`, `test_remedies.py`, `test_invariants.py` — and leave the
full `tests/ evals/` run to CI, which does it on three Python versions. A full suite in a
worker turn costs ~21 minutes for the same answer (standing user ruling).
