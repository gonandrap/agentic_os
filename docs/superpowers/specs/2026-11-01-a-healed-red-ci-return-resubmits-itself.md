# A healed red_ci return resubmits itself

Issue 1065 (kn-bbc03206). Related: `2026-09-18-a-red-base-heals-itself.md`,
`2026-10-13-a-red-head-is-sent-back-before-the-panel.md`.

## 1. The problem

Evidence: wo-3f01dcc8, PR #1053. Rounds 1 and 2 were `red_ci` returns while main was red
(issue #1054). The heal rebuilt the pull request (`pr_base_updated`, cause `base_red`,
new head f6120c1) and CI reran. The order then sat in `validating` with no open round,
no attention flag and no actor. `jarvis validation force` refused it.

Root cause: a round settled `rejected` with `uncounted_cause = red_ci` records no
validated head. `Daemon.heal_inherited_failure` (src/jarvis/daemon.py) only carries a
PASSED verdict (`judged == head_before`), so a `red_ci` return falls through: no round
reopens and the worker is never told. `RED_CI_FEEDBACK` had told the worker to wait for
green. `ops.land_when_cleared` then rests the order in `validating`, because `rejected`
is in `OPEN_VALIDATION_OUTCOMES`. `ops.force_validation_refusal` refuses `validating`
(not in `FORCEABLE_STATUSES`). A hand-sent `wo send` was the only way out.

## 2. The fix: the OS resubmits after its own heal

Where: `Daemon.heal_inherited_failure`, in the branch where no verdict carried. The
heal is the only code that knows the red was inherited.

Mechanism: after a rebuild that moved the head, if the newest round is a `red_ci`
return, `ops.resubmit_healed_red_ci` opens a round on the new head itself. This is the
resubmission the envelope asked the worker for. It opens at once. The `ci_pending` hold
waits for CI. A green head reaches the seats. A still-red head is another `red_ci`
return, and its envelope tells the worker. The round counts if judged and is never
declined on `max_rounds` (Neo question 2889). `validation_forced` carries `by: os` and
no `cause`, because the timeline reads any cause as "no round spent".

It is skipped when:
- the worker is busy (the heal already defers);
- a message is queued for the worker (the worker will resubmit);
- the head equals the returned round's head, or one the OS already re-judged;
- `force_validation_refusal` refuses.

Rejected: re-judging from `Daemon.auto_merge` on `HELD_NOT_PASSED`. It only reaches
`waiting_pr_merge`, and kn-bbc03206 records that `automerge.only_the_head_moved`
deliberately excludes that code.

## 3. The floor: force accepts the resting state

`force_validation_refusal` also accepts `validating` when the newest round is a settled
`red_ci` return and no worker turn is running (`ops.red_ci_return_idle`). This covers
orders parked before §2 shipped. `FORCEABLE_STATUSES` is unchanged: `validating` on a
judged rejection is the worker's rework state, and the worker may be typing.
