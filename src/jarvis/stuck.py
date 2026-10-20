"""Is this order not progressing? One pure predicate over scalars.

§1 of docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets-investigated.md.
Shaped like `health.py`: nothing here calls a model, opens a store, reads a file or writes
a row, and `health.due`'s docstring gives the reason verbatim — "this function is the whole
of the spend decision and reading state inside it would make that decision untestable
without a store". The registry's facts pass can call it as a reader and the daemon tick
calls it directly, from the same signature.

NOT in `invariants.py`, and that is a ruling rather than a preference: Appendix A.9 of
docs/superpowers/specs/2026-09-27-investigation-orders.md:1152 — an invariant answers "is
THIS named gap class present?" and its output is a remedy; this answers "is something wrong
here, and what is wrong is not known?" and its output is a model session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .health import SEP

SECONDS_PER_MINUTE = 60  # a unit, not a setting

#: The statuses judged on SILENCE rather than on time in status. An order that entered
#: `running` two hours ago and is typing is working; every other status is judged on time
#: in status because nothing is expected to happen inside it at all.
ACTIVITY_STATUSES = ("running",)

#: The fourth self-clearing hold of §2's `excluded_cause` family: this order's
#: `depends_on` edges are all LIVE, so it is waiting as `--depends-on` designed and the
#: wait ends without anyone acting. A DEAD edge is not this cause — see §2.3.
LIVE_DEPENDENCY = "live_dependency"

#: The holds this module names itself, §4 of docs/superpowers/specs/2026-10-12-a-managers-
#: by-design-idle-is-not-a-standstill.md. Each rides on `Verdict.excluded` beside
#: `worker_session.PAUSE_USAGE_LIMIT`, so a held order is a visible row rather than an
#: invisible skip. `IDLE_MANAGER_CAUSE` is the ONE definition of the manager's cause in the
#: codebase and is shared with `ops.IDLE_MANAGER_CAUSE`, which is now an alias of it.
HOLD_EXEMPT_KIND = "exempt_kind"
IDLE_MANAGER_CAUSE = "its feature's next message"

#: §1 of docs/superpowers/specs/2026-10-14-a-worker-turn-in-flight-is-not-stuck.md.
TURN_IN_FLIGHT_CAUSE = "a worker turn is in flight"

#: §1 of docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md.
BASE_RED_CAUSE = "the default branch is red"

#: §2.1 of docs/superpowers/specs/2026-10-15-a-child-held-by-its-features-unratified-
#: plan-is-not-stuck.md.
PLAN_ASSUMPTION_CAUSE = "its feature's unratified plan"

#: One sentence per cause, because the shipped one is false for both of the above: an
#: exempt kind never clears, and a manager's hold clears when its feature speaks rather
#: than by itself. `{cause}` and `{kind}` are the only slots.
EXCLUSION_REASON: Mapping[str, str] = {
    HOLD_EXEMPT_KIND: ("an order of kind {kind} is never judged by this sweep — "
                       "`os.fleet_health.exempt_kinds` names it, so time in status is "
                       "not a claim about it"),
    IDLE_MANAGER_CAUSE: ("a manager is idle until its feature messages it, and this "
                         "feature has not finished with it — nothing is judged while "
                         "that is true"),
    TURN_IN_FLIGHT_CAUSE: ("a worker turn is in flight and writes nothing to the "
                           "timeline while its tools run — it clears by itself when the "
                           "turn ends, so nothing is judged while it does"),
    BASE_RED_CAUSE: ("the OS is holding this merge itself: the default branch's own build "
                     "is broken and nothing merges onto a red base — it clears when the "
                     "base goes green, with no decision from you, so nothing is judged "
                     "while that is true"),
    # §2.1: the shipped sentence is FALSE here — a person ruling on the planner's
    # assumptions is the only thing that clears this, so the sentence names that move.
    PLAN_ASSUMPTION_CAUSE: ("this child is withheld from dispatch until its feature's "
                            "plan is ratified, and only you can ratify it — "
                            "`jarvis wo review <planner-id>` on the feature's planner "
                            "is what clears it, so nothing is judged while it stands"),
}

#: The usage window's sentence, and the answer for any hold added later: all five of §2's
#: causes resume by themselves.
DEFAULT_EXCLUSION_REASON = ("held by {cause}, which clears by itself — nothing is judged "
                            "while it does")

#: What the investigator is asked, and it is a TEMPLATE rather than prose composed at the
#: call site for the reason `ops.create_investigation_order` refuses an empty `why`: the
#: investigator's first reader is a fresh session with no memory of the sweep. The last
#: three questions are §3's, and they are asked of every status — deciding whether a
#: user-owed wait is GENUINE is this session's job and can never be a filter before it.
WHY = """{subject} has been {status_label} for {status_age}, and the OS itself has not
touched it for {activity_age}. {reason}

Nothing chose this: the fleet-health sweep is arithmetic over rows the OS already writes,
and it holds no opinion about what is wrong. What the record says is blocking it:
{blocker}

Find out what is actually holding it, and answer these three about that blocker:

1. Is the blocker true, current and correctly worded?
2. Is it the user's call, or is it the OS failing to decide?
3. Does the user have the reason and the link they need to decide?
"""


@dataclass(frozen=True)
class Verdict:
    """One order judged, and the whole of why."""

    stuck: bool
    #: Which of the two clocks was judged: `"status"` or `"activity"`.
    clock: str
    #: The judged clock minus the seconds the OS's own record says were held.
    active_seconds: float
    threshold_seconds: float
    #: The hold that stopped the judgement, `""` when none.
    excluded: str
    #: One sentence. The sweep's `why` and the CLI's line both read it.
    reason: str


def assess(status: str, seconds_in_status: float, seconds_since_activity: float,
           discounted_seconds: float, thresholds: Mapping[str, float],
           fallback_seconds: float,
           activity_statuses: Sequence[str] = ACTIVITY_STATUSES,
           excluded_cause: str = "",
           kind: str = "worker",
           exempt_kinds: Sequence[str] = (),
           by_design_idle: bool = False,
           turn_in_flight: bool = False) -> Verdict:
    """The whole decision, over scalars: no store, no config object, no row.

    `fallback_seconds` covers an open status `thresholds` does not name, so a status added
    to `project_store.WO_STATUSES` later is watched on the day it ships rather than
    silently unwatched.

    `excluded_cause` short-circuits to `stuck=False`. §2: the account's usage window, a
    live `jarvis pause`, the post-reopen ramp, a live blocking dependency
    (`LIVE_DEPENDENCY`) and a validation round held for GitHub's checks
    (`project_store.VALIDATION_CI_CAUSE`), because nothing is wrong in those cases and all
    five resume by themselves — so an investigation would buy a session to be told the OS
    is working as designed. `exempt_kinds` and `by_design_idle` exclude too (§1, §2.2 of
    the 2026-10-12 spec), as does a worker whose turn is in flight — it writes nothing to
    the timeline while its tools run (§1 of the 2026-10-14 spec). So does a
    `waiting_pr_merge` order whose auto-merge the OS is itself holding because the default
    branch is red (`BASE_RED_CAUSE`, §1 of
    docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md).

    That CI one is an EXTENSION of kn-6e836793 (the user's 2026-09-27 ruling that the
    usage hold was the only one) on the record rather than a silent widening — the
    "Reconciliation with the standing ruling" section of
    docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md.
    It passes the same two tests as a usage window: the OS is deliberately not acting,
    and the hold clears with NO user decision in the loop. Any hold failing either test
    stays judged — the auth hold does, because a human running `/login` is what clears it.

    `kind`, `exempt_kinds` and `by_design_idle` are the 2026-10-12 spec's: `status` cannot
    express "nothing is expected to happen here until somebody speaks to it", and a
    `kind='manager'` order is idle-until-messaged by design. All three are SCALARS or a
    policy list the caller resolved (`activity_statuses`' shape) — `by_design_idle` is
    computed by the caller from its store, so this module still opens nothing.

    Precedence, and it matters. An exempt kind is not a subject at all, so whether the
    account's window is shut is irrelevant to it; a fleet pause is a fact about the whole
    fleet and outranks a fact about one order's feature.
    """
    on_activity = status in activity_statuses
    clock = "activity" if on_activity else "status"
    judged = seconds_since_activity if on_activity else seconds_in_status
    threshold = float(thresholds.get(status, fallback_seconds))
    active = max(0.0, judged - discounted_seconds)
    cause = ""
    if kind in exempt_kinds:
        cause = HOLD_EXEMPT_KIND
    elif excluded_cause:
        cause = excluded_cause
    elif by_design_idle:
        cause = IDLE_MANAGER_CAUSE
    elif turn_in_flight:
        cause = TURN_IN_FLIGHT_CAUSE
    if cause:
        # One site composes the sentence, per cause: `active_seconds` and
        # `threshold_seconds` stay populated so the row still shows the arithmetic.
        return Verdict(stuck=False, clock=clock, active_seconds=active,
                       threshold_seconds=threshold, excluded=cause,
                       reason=EXCLUSION_REASON.get(
                           cause, DEFAULT_EXCLUSION_REASON).format(cause=cause,
                                                                   kind=kind))
    over = active >= threshold
    return Verdict(
        stuck=over, clock=clock, active_seconds=active, threshold_seconds=threshold,
        excluded="",
        reason=f"{_hours(active)} of active time {'past' if over else 'inside'} the "
               f"{_hours(threshold)} threshold for {status}, judged on time "
               f"{'since its last activity' if on_activity else 'in status'}")


def fingerprint(status: str, status_since: float, blocker: str,
                event_count: int) -> str:
    """The situation, as one readable value. §6b.

    `health.SEP`-joined and NOT a hash, on `health.fingerprint`'s own rule: the only
    question ever asked of it is inequality, and a readable value is diagnosable by eye.
    `event_count` is counted by the CALLER, excluding `health.observer_kinds()`, so a
    fingerprint cannot move as a result of being looked at.
    """
    return SEP.join((status, str(int(status_since)), blocker, str(event_count)))


def _hours(seconds: float) -> str:
    return f"{seconds / (SECONDS_PER_MINUTE * 60):.1f}h"
