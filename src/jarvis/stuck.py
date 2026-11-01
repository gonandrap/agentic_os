"""Is this order not progressing? (docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets-investigated.md)"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .health import SEP

SECONDS_PER_MINUTE = 60  # a unit, not a setting

#: The statuses judged on SILENCE rather than on time in status
ACTIVITY_STATUSES = ("running",)

#: The fourth self-clearing hold of §2's `excluded_cause` family: this order's `depends_on` edges are...
LIVE_DEPENDENCY = "live_dependency"

#: The holds this module names itself
HOLD_EXEMPT_KIND = "exempt_kind"
IDLE_MANAGER_CAUSE = "its feature's next message"

#: §1 of docs/superpowers/specs/2026-10-14-a-worker-turn-in-flight-is-not-stuck.md
TURN_IN_FLIGHT_CAUSE = "a worker turn is in flight"

#: §1 of docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md
BASE_RED_CAUSE = "the default branch is red"

#: §1 of docs/superpowers/specs/2026-10-20-a-merge-held-for-running-checks-is-not-stuck.md
AUTOMERGE_CHECKS_CAUSE = "GitHub's checks on the merge commit"

#: §2.1 of docs/superpowers/specs/2026-10-15-a-child-held-by-its-features-unratified-...
PLAN_ASSUMPTION_CAUSE = "its feature's unratified plan"

#: §2.1 of docs/superpowers/specs/2026-10-19-an-order-held-by-an-open-precondition-is-not-stuck.md
PRECONDITION_CAUSE = "an open precondition"

#: §1 of docs/superpowers/specs/2026-10-20-a-work-order-at-its-budget-cap-is-not-stuck.md
BUDGET_CAP_CAUSE = "its budget cap"

#: One sentence per cause, because the shipped one is false for both of the above: an exempt kind...
EXCLUSION_REASON: Mapping[str, str] = {
    HOLD_EXEMPT_KIND: ("an order of kind {kind} is never judged by this sweep — "
                       "`os.fleet_health.exempt_kinds` names it, so time in status is "
                       "not a claim about it"),
    IDLE_MANAGER_CAUSE: ("a manager is idle until its feature messages it, and this "
                         "feature has not finished with it — nothing is judged while "
                         "that is true"),
    TURN_IN_FLIGHT_CAUSE: ("a worker turn is in flight and writes nothing to the "
                           "timeline while its tools run — its transcript is still "
                           "moving, so nothing is judged until it falls silent past the "
                           "threshold"),
    BASE_RED_CAUSE: ("the OS is holding this merge itself: the default branch's own build "
                     "is broken and nothing merges onto a red base — it clears when the "
                     "base goes green, with no decision from you, so nothing is judged "
                     "while that is true"),
    # §1: the default sentence is TRUE here and still says nothing a reader can act on
    AUTOMERGE_CHECKS_CAUSE: ("the OS is holding this merge itself: GitHub has not finished "
                             "running the checks on the commit the panel judged, and "
                             "nothing merges a commit whose checks have not reported — it "
                             "clears when they do, with no decision from you, so nothing "
                             "is judged while that is true"),
    # §2.1: the shipped sentence is FALSE here
    PLAN_ASSUMPTION_CAUSE: ("this child is withheld from dispatch until its feature's "
                            "plan is ratified, and only you can ratify it — "
                            "`jarvis wo review <planner-id>` on the feature's planner "
                            "is what clears it, so nothing is judged while it stands"),
    # §2.1: the shipped sentence is FALSE here too; both parties clear it
    PRECONDITION_CAUSE: ("this order was created held until its precondition clears and "
                         "the row is still open — the OS clears it when it resolves the "
                         "ref, and `jarvis wo unblock <id>` is how you clear it yourself, "
                         "so nothing is judged while it stands"),
    # §1 of the 2026-10-20 spec: the shipped sentence is FALSE here too
    BUDGET_CAP_CAUSE: ("the OS launches no turn it cannot pay for, and only you can "
                       "fund another — `jarvis wo budget <id> <amount>` is what clears "
                       "it, and closing the order is the other answer, so nothing is "
                       "judged while it stands"),
}

#: The usage window's sentence, and the answer for any hold added later: all five of §2's causes...
DEFAULT_EXCLUSION_REASON = ("held by {cause}, which clears by itself — nothing is judged "
                            "while it does")

#: What the investigator is asked
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
    """One order judged, and the whole of why"""

    stuck: bool
    #: Which of the two clocks was judged: `"status"` or `"activity"`
    clock: str
    #: The judged clock minus the seconds the OS's own record says were held
    active_seconds: float
    threshold_seconds: float
    #: The hold that stopped the judgement, `""` when none
    excluded: str
    #: One sentence
    reason: str


def assess(status: str, seconds_in_status: float, seconds_since_activity: float,
           discounted_seconds: float, thresholds: Mapping[str, float],
           fallback_seconds: float,
           activity_statuses: Sequence[str] = ACTIVITY_STATUSES,
           excluded_cause: str = "",
           kind: str = "worker",
           exempt_kinds: Sequence[str] = (),
           by_design_idle: bool = False,
           turn_in_flight: bool = False,
           stalled_turn_seconds: float = 0.0) -> Verdict:
    """The whole decision, over scalars: no store, no config object, no row (docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md)"""
    on_activity = status in activity_statuses
    clock = "activity" if on_activity else "status"
    judged = seconds_since_activity if on_activity else seconds_in_status
    threshold = float(thresholds.get(status, fallback_seconds))
    active = max(0.0, judged - discounted_seconds)
    # Issue 1449: an in-flight turn silent past the threshold is wedged, not working
    silent_turn = turn_in_flight and on_activity and active >= threshold
    cause = ""
    if kind in exempt_kinds:
        cause = HOLD_EXEMPT_KIND
    elif excluded_cause:
        cause = excluded_cause
    elif by_design_idle:
        cause = IDLE_MANAGER_CAUSE
    elif turn_in_flight and not silent_turn:
        cause = TURN_IN_FLIGHT_CAUSE
    if cause:
        # One site composes the sentence
        return Verdict(stuck=False, clock=clock, active_seconds=active,
                       threshold_seconds=threshold, excluded=cause,
                       reason=EXCLUSION_REASON.get(
                           cause, DEFAULT_EXCLUSION_REASON).format(cause=cause,
                                                                   kind=kind))
    # Issue 1451: the fact that sets needs_attention, so both surfaces agree
    if stalled_turn_seconds:
        return Verdict(stuck=True, clock=clock, active_seconds=active,
                       threshold_seconds=threshold, excluded="",
                       reason=f"its turn has been running for "
                              f"{_hours(stalled_turn_seconds)}, past the point the OS "
                              f"trusts a turn in flight to be progressing")
    over = active >= threshold
    if silent_turn:
        return Verdict(
            stuck=True, clock=clock, active_seconds=active, threshold_seconds=threshold,
            excluded="",
            reason=f"a worker turn is in flight but silent: {_hours(active)} with no "
                   f"API call, tool event or record write, past the {_hours(threshold)} "
                   f"threshold for {status}")
    return Verdict(
        stuck=over, clock=clock, active_seconds=active, threshold_seconds=threshold,
        excluded="",
        reason=f"{_hours(active)} of active time {'past' if over else 'inside'} the "
               f"{_hours(threshold)} threshold for {status}, judged on time "
               f"{'since its last activity' if on_activity else 'in status'}")


def fingerprint(status: str, status_since: float, blocker: str,
                event_count: int) -> str:
    """The situation, as one readable value. §6b"""
    return SEP.join((status, str(int(status_since)), blocker, str(event_count)))


def _hours(seconds: float) -> str:
    return f"{seconds / (SECONDS_PER_MINUTE * 60):.1f}h"
