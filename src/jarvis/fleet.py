"""The account, which is not a project — how much of it is in use, and whether it is up.

Every other cap in this OS rations something a project owns: `max_concurrent` its own
work orders, `feature_orders.max_parallel` one feature's children. The thing that ran out
on 2026-09-02 was the ACCOUNT, and no arrangement of per-project numbers can ration that,
because the limit is not divided among projects. So there is one number here, fleet-wide,
and one fact: whether Claude is currently refusing turns for everyone.

The incident: wo-878aefdb, and fo-6269be9a's four children on 2026-09-02.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping

from . import worker_session
from .invariants import clock
from .project_store import ACTIVE_STATUSES, UNGOVERNED_ORIGINS, ProjectStore

log = logging.getLogger("jarvisd")

#: Receipt: the OS has already told the user about the outage that is on. Its VALUE is
#: the moment the window reopens and is for reading a database by hand; the predicate is
#: whether it is set at all, so one outage produces one inbox entry however many workers
#: were refused by it. Cleared the moment no work order is refused any more, which is
#: what makes the NEXT outage announceable without a second key to expire.
OUTAGE_ANNOUNCED_KEY = "usage_outage_announced"


@dataclass(frozen=True)
class Outage:
    """Claude is refusing turns because the account's window is spent.

    Re-derived from the refused turns themselves every time it is asked for — no column,
    no status, no flag, the rule `project_store.py` states for exactly this choice and
    that `worker_session.turn_pause` already follows. A work order that gets a turn
    through is no longer refused, so the outage lifts itself; nothing has to remember to
    clear it.
    """

    #: The work order that was told, and its project. One of possibly several — the
    #: refusal is identical, so the first is as good as any, and naming one makes the
    #: inbox entry point somewhere.
    project: str
    wo_id: str
    #: When dispatch may resume: `TurnPause.retry_at`, NOT the raw parsed reset. They
    #: differ by `RATE_LIMIT_MIN_DELAY` at most, and using the pause's own moment is what
    #: guarantees the hold cannot outlast the retry that would lift it.
    reopens_at: float
    #: The refusal verbatim, for the inbox entry.
    message: str


class Fleet:
    """What the account is doing this tick, and whether anything else may start.

    Read once per tick and then MUTATED as turns are launched (`launched`), so the cap
    binds within a tick as well as across them: `dispatch_pending` runs per project, and
    a fleet count re-read per project would let each project's pass believe the last
    one's launches had not happened.
    """

    def __init__(self, cap: int, in_flight: int, outage: Outage | None = None,
                 at: float | None = None):
        self.cap = cap
        self.in_flight = in_flight
        self.outage = outage
        #: The clock this reading was taken against. ONE per read, and `blocked` uses it
        #: rather than asking the clock again: a tick claims work orders in a loop and
        #: calls `blocked` on each, so a second clock would let the hold lift halfway
        #: through a tick. Same rule, for the same reason, as `invariants.clock`.
        self.at = time.time() if at is None else at

    def blocked(self) -> str:
        """Why nothing may launch, in words for the user — or "" when something may.

        The outage outranks the cap because it is the more useful of the two true
        answers: a slot frees by itself in minutes and says nothing, whereas "the account
        is refusing turns until 18:40" is the whole explanation for a quiet fleet. Same
        ranking, and the same reasoning, as the dependency label above the slot label in
        `invariants.status_label`.
        """
        if self.outage is not None and self.at < self.outage.reopens_at:
            return ("the Claude usage window is spent, reopening at "
                    f"{clock(self.outage.reopens_at)}")
        if self.in_flight >= self.cap:
            return f"{self.in_flight} of {self.cap} worker turns already in flight"
        return ""

    def launched(self) -> None:
        self.in_flight += 1


def read(cap: int, stores: Mapping[str, ProjectStore],
         now: float | None = None) -> Fleet:
    """The fleet's state, derived from every project's turns.

    Costs one COUNT per project plus one indexed `latest_turn` per ACTIVE work order —
    the same shape and roughly the same cost as `Daemon.retry_paused_turns`, which walks
    the same rows to ask the same question of one project at a time.
    """
    in_flight = 0
    outage: Outage | None = None
    for name, store in stores.items():
        in_flight += store.count_running_turns()
        for wo in store.list_work_orders(statuses=ACTIVE_STATUSES):
            if wo["origin"] in UNGOVERNED_ORIGINS:
                continue  # the user's own session; its refusals are not Jarvis's news
            try:
                pause = worker_session.turn_pause(store, wo["id"])
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] could not diagnose %s", name, wo["id"])
                continue
            if pause is None or pause.reason != worker_session.PAUSE_USAGE_LIMIT:
                continue
            # `resumable` excludes the exhausted one, which is on its way to `failed` and
            # asking for the user: a refusal nobody will retry must not hold the fleet
            # for ever. `due()` excludes the one whose window has already reopened —
            # holding on it would hold past the moment that was supposed to release it.
            if not pause.resumable or pause.due(now):
                continue
            if outage is None or pause.retry_at > outage.reopens_at:
                outage = Outage(project=name, wo_id=wo["id"],
                                reopens_at=pause.retry_at, message=pause.message)
    return Fleet(cap, in_flight, outage, at=now)


def current(cat: Any, now: float | None = None) -> Fleet:
    """`read` for a caller with no stores open — a CLI process, a dashboard request.

    Opens and closes its own, so a read-only surface can answer "why is nothing
    starting?" without holding connections it would then have to remember to close.
    """
    stores: dict[str, ProjectStore] = {}
    try:
        for p in cat.projects:
            if p.path.is_dir():
                stores[p.name] = ProjectStore(p.path)
        return read(cat.os.max_in_flight, stores, now)
    finally:
        for store in stores.values():
            store.close()


def announce(central: Any, fleet: Fleet) -> bool:
    """Put the outage in the inbox — ONCE, however many workers it refused.

    On 2026-09-02 four workers were refused within 18 seconds by the same window. Four
    inbox entries saying the same sentence is not four times the information; it is the
    strip that stops being read (the fear `ops.os_status` states about attention
    rollups). The receipt is cleared when nothing is refused any more, so a genuinely
    new outage later still gets its own entry.
    """
    if fleet.outage is None:
        if central.get_state(OUTAGE_ANNOUNCED_KEY):
            central.set_state(OUTAGE_ANNOUNCED_KEY, "")  # read first: this runs every tick
        return False
    if central.get_state(OUTAGE_ANNOUNCED_KEY):
        return False
    central.set_state(OUTAGE_ANNOUNCED_KEY, f"{fleet.outage.reopens_at:.0f}")
    central.add_inbox(
        project=fleet.outage.project,
        title=("Claude usage limit reached — the fleet is holding until "
               f"{clock(fleet.outage.reopens_at)}"),
        body=(f"{fleet.outage.message}\n\nNo work order has failed and nothing needs "
              "you: dispatch and retries are held fleet-wide until the window reopens, "
              f"then resume {fleet.cap} at a time. First refused: {fleet.outage.wo_id}."),
        level="warning",
        wo_id=fleet.outage.wo_id,
    )
    return True
