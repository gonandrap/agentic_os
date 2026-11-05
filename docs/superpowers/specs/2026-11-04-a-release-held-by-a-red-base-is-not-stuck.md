# A release held by a red base is not stuck

Issue 1603, wo-352c402c. The release half of
`docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md`, whose §1-§3
stand here unchanged. Gap class `designed-wait-undiscounted`.

## The problem

`ops.defer_red_release` and `Daemon.hold_red_release` keep a release order in `pending` while
the base is red. wo-98a2b552 was swept at 0.6h with main red (run 41935766931), and
inv-275f7586 paid a session to report the OS working as designed.

**Root cause:** `ops.stuck_scan` knew the red base only for `waiting_pr_merge`, in BOTH
halves: `excluded_cause` tested that status, and `base_red_spans` was gated on it too. So the
hold was judged on raw time in status against the 0.5h `pending` threshold.

kn-a144e11f calls the spans unconditional and suspects the `checked_at +
BASE_HEALTH_FRESH_SECONDS` horizon. Both wrong: the status gate zeroed the span before the
horizon mattered, and the horizon is the deliberate §5(5) guard against a fact nobody can
confirm, so it stays.

## The fix

One predicate in `stuck_scan`, `held_by_red_base`: `waiting_pr_merge`, or `pending` and
`release.is_release_order`. It gates both the exclusion and the discount (kn-0a222b38), and
rides on `Verdict.excluded`, never a `continue` (kn-143a46df).

No event test. `hold_red_release` holds EVERY due pending release while the base is red, by
either path, so "pending release, fresh red fact" already is the hold. The `needs_review` park
is not `pending`, so it stays judged (kn-6e836793).

`stuck.EXCLUSION_REASON[BASE_RED_CAUSE]` now covers both holds.

### Detector

`invariants.check_red_release_hold_is_discounted` (`INV-RED-RELEASE-UNDISCOUNTED`, in
`jarvis doctor`): a pending release on a fresh red fact must read `excluded ==
BASE_RED_CAUSE` and `discounted_seconds > 0` on `ops.stuck_report`'s row. Remedy: none. It
catches a fresh red fact with no episode behind it as well as a lost branch.

### Not covered

* The §3 gate carries over: `hold_red_release` ignores `validation.auto_merge`, `stuck_scan`
  does not receive the fact without it. A project with auto-merge off holding a release stays
  judged. The hold is OS-project-only and that project runs auto-merge.
* The merge side of the same class (kn-f1aab0ee, kn-97e9adc8) is untouched.
