# A feature's spec pull request lands first

Issue 1308. Ruling: Neo question 2416, on kn-663d59b5, which supersedes the issue-822
ruling recorded in `2026-09-27-a-planner-submits-behind-its-spec-pull-request.md` §2
("no panel over spec pull requests").

## The problem

fo-744c42a5's planner finished `completed` with its spec PR (#1289) open and green.
Nothing owned that PR: `pr_url_recorded {source: plan_submit}` was deliberately not a
declaration, so the merge poller, the conflict healer and auto-merge all skipped it.
Two children then merged to main ahead of it, leaving `spec_section` references that
resolve nowhere on main. The slow sweep's INV-SPEC-POINTER-REAL then reported the spec
as missing ("no tracked file carries that name ... cancel the feature"): its ladder
reads the planner's worktree and `origin/<default>` only, and the worktree was gone.

## The fix

### 1. The spec PR is a delivery

`ops.submit_plan` passes the planner's recorded `pr_url` into the planner settle
(`_settle_submitter` then `finish(..., pr_url=…)`). The `finished {pr_url}` event is a
declaration, so the planner parks like any delivered order: a validation round opens
over the spec PR when the project validates, `Daemon.auto_merge` merges it once that
round passes, and the merge poller completes the planner. `plan_submit` joins
`DECLARING_SOURCES` so a planner recorded under the old route routes too (§4).

### 2. Children wait for the spec

`ProjectStore.spec_hold(wo)` is the one derivation: a child (`kind='worker'` with a
`parent_id`) whose feature's planner carries a `pr_url` and sits in
`SPEC_PR_OPEN_STATUSES` (`validating`, `waiting_pr_merge`, `needs_review`). Two readers:

- the `spec_unmerged` claim rule in `CLAIM_RULES`: the child does not dispatch;
- `automerge.decide(spec_unmerged=…)`, code `HELD_SPEC_UNMERGED`: a child already
  dispatched, under a feature reparked by §4, does not auto-merge.

Once the planner completes (its spec merged) the hold lifts on the next tick. Stacking
(Phase 4, bl-e3a88979) would replace this serialisation; until it ships, the feature
waits for its spec.

### 3. INV-SPEC-POINTER-REAL names the real cause

When the design doc is absent from the default branch, `invariants._unmerged_spec`
looks for it on the planner's recorded branch (`landing.pushed_exists`:
`origin/<branch>`, then the local branch a removed worktree leaves). Found there:

- planner parked behind its PR: silent — the OS is driving that merge;
- planner `completed` with a PR, feature open: repaired by §4;
- otherwise: owed, naming the branch and the PR to merge (or the `jarvis wo pr` to
  record one). It never suggests cancelling the feature.

### 4. Self-heal for planners settled under the old route

`ops.repark_spec_pull_request(wo_id)` runs `finish`'s tail on a `completed` planner
that still carries an unmerged spec PR: a round when validation applies, then
`land_when_cleared`. It writes `spec_pr_reparked` and is a no-op on anything not
`completed`. §3's repair is its only caller, so it runs on the slow sweep.

## Out of scope

Merging a spec before Neo releases the plan is possible (the round and Neo's review run
side by side); a rejected plan revised after its spec merged follows the existing
"reset the branch onto `origin/main`" refusal. Branch stacking.
