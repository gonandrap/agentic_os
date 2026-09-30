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
           excluded_cause: str = "") -> Verdict:
    """The whole decision, over scalars: no store, no config object, no row.

    `fallback_seconds` covers an open status `thresholds` does not name, so a status added
    to `project_store.WO_STATUSES` later is watched on the day it ships rather than
    silently unwatched.

    `excluded_cause` short-circuits to `stuck=False`. §2: the account's usage window, a
    live `jarvis pause` and the post-reopen ramp are the only ones, because nothing is
    wrong in those cases and all three resume by themselves — so an investigation would
    buy a session to be told the OS is working as designed.
    """
    on_activity = status in activity_statuses
    clock = "activity" if on_activity else "status"
    judged = seconds_since_activity if on_activity else seconds_in_status
    threshold = float(thresholds.get(status, fallback_seconds))
    active = max(0.0, judged - discounted_seconds)
    if excluded_cause:
        return Verdict(stuck=False, clock=clock, active_seconds=active,
                       threshold_seconds=threshold, excluded=excluded_cause,
                       reason=f"held by {excluded_cause}, which clears by itself — "
                              f"nothing is judged while it does")
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
