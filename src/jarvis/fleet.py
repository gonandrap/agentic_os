"""The account, which is not a project — how much of it is in use, and whether it is up.

Every other cap in this OS rations something a project owns: `max_concurrent` its own
work orders, `feature_orders.max_parallel` one feature's children. The thing that ran out
on 2026-09-02 was the ACCOUNT, and no arrangement of per-project numbers can ration that,
because the limit is not divided among projects. So there is one number here, fleet-wide,
and one fact: whether Claude is currently refusing turns for everyone.

The incident: wo-878aefdb, and fo-6269be9a's four children on 2026-09-02.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import worker_session
from .invariants import clock
from .project_store import RETRY_SWEEP_STATUSES, UNGOVERNED_ORIGINS, ProjectStore

log = logging.getLogger("jarvisd")

#: Receipt: the OS has already told the user about the outage that is on. Its VALUE is
#: the moment the window reopens and is for reading a database by hand; the predicate is
#: whether it is set at all, so one outage produces one inbox entry however many workers
#: were refused by it. Cleared the moment no work order is refused any more, which is
#: what makes the NEXT outage announceable without a second key to expire.
OUTAGE_ANNOUNCED_KEY = "usage_outage_announced"

#: The user's brake (issue #843): while set, no work order starts a turn unless it is on
#: the allow-list. JSON `{"since": ts, "reason": str, "allow": [wo/fo ids]}`, written only
#: by `jarvis pause` / `jarvis resume`. Absent or empty means the fleet runs normally.
PAUSE_KEY = "fleet_paused"

#: When the usage window was last seen reopening: the tick an outage lifted. Written by
#: `announce`, read by `ramp`. The reopen is the dangerous moment — every turn the window
#: refused comes due in the same second, and each resumes against a cache that expired
#: hours ago (issue #843: 11 turns, 2.25M cache-write tokens in two minutes, the window
#: spent again within ten).
REOPENED_KEY = "usage_window_reopened_at"

#: When the window was last spent AGAIN soon after reopening (`BREAKER_SECONDS`). While
#: set, the next ramp is slower (`BREAKER_RAMP_CAP`, twice as long). Cleared by the first
#: outage that begins outside the breaker window — a window that lasted is the evidence.
BREAKER_KEY = "usage_breaker_tripped_at"

#: How many worker turns may be in flight while the fleet ramps back up after a reopen,
#: whatever `max_in_flight` says, and for how long. The cap is the steady state; the ramp
#: is the recovery, because a reopen is the one moment the whole backlog is due at once.
RAMP_CAP = 2
RAMP_SECONDS = 30 * 60
#: A window spent again within this long of reopening trips the breaker.
BREAKER_SECONDS = 30 * 60
BREAKER_RAMP_CAP = 1


@dataclass(frozen=True)
class FleetPause:
    """The user has stopped the fleet. Only the allow-listed orders may start a turn."""

    since: float
    reason: str
    allow: frozenset[str]

    def allows(self, order_id: str | None) -> bool:
        return order_id is not None and order_id in self.allow


@dataclass(frozen=True)
class Ramp:
    """The fleet is recovering from a usage-window reopen: a smaller in-flight cap."""

    since: float
    until: float
    cap: int
    #: The breaker tripped: the last window was spent again soon after reopening.
    tripped: bool = False


def load_pause(central: Any) -> FleetPause | None:
    raw = central.get_state(PAUSE_KEY) if central is not None else ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        # A brake that cannot be read is still a brake: fail closed, allow nothing.
        return FleetPause(since=0.0, reason="(unreadable pause record)",
                          allow=frozenset())
    return FleetPause(since=float(data.get("since") or 0.0),
                      reason=str(data.get("reason") or ""),
                      allow=frozenset(str(i) for i in data.get("allow") or ()))


def _write_pause(central: Any, pause: FleetPause) -> None:
    central.set_state(PAUSE_KEY, json.dumps({
        "since": pause.since, "reason": pause.reason, "allow": sorted(pause.allow)}))


def pause(central: Any, reason: str = "", allow: Iterable[str] = (),
          now: float | None = None) -> FleetPause:
    """Stop the fleet. Re-pausing keeps the original `since` and REPLACES the allow-list."""
    current = load_pause(central)
    since = current.since if current is not None and current.since else (
        time.time() if now is None else now)
    new = FleetPause(since=since, reason=reason or (current.reason if current else ""),
                     allow=frozenset(allow))
    _write_pause(central, new)
    return new


def allow(central: Any, order_ids: Iterable[str]) -> FleetPause:
    """Let these orders through a pause. Refuses when the fleet is not paused."""
    current = load_pause(central)
    if current is None:
        raise ValueError("the fleet is not paused — there is nothing to let through")
    new = FleetPause(since=current.since, reason=current.reason,
                     allow=current.allow | frozenset(order_ids))
    _write_pause(central, new)
    return new


def unpause(central: Any) -> None:
    central.set_state(PAUSE_KEY, "")


def ramp(central: Any, now: float | None = None) -> Ramp | None:
    """The ramp in force at `now`, or None once it has run its course (or never began)."""
    if central is None:
        return None
    reopened = float(central.get_state(REOPENED_KEY) or 0.0)
    if not reopened:
        return None
    tripped = bool(central.get_state(BREAKER_KEY))
    length = RAMP_SECONDS * (2 if tripped else 1)
    now = time.time() if now is None else now
    if now >= reopened + length:
        return None
    return Ramp(since=reopened, until=reopened + length,
                cap=BREAKER_RAMP_CAP if tripped else RAMP_CAP, tripped=tripped)


def attach(state: "Fleet", central: Any) -> "Fleet":
    """Put the user's brake and the reopen ramp on a reading. One call for every reader."""
    state.pause = load_pause(central)
    state.ramp = ramp(central, state.at)
    return state


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
                 at: float | None = None, pause: FleetPause | None = None,
                 ramp: Ramp | None = None):
        self.cap = cap
        self.in_flight = in_flight
        self.outage = outage
        #: The user's brake (`jarvis pause`), and the post-reopen ramp. Both set by
        #: `attach`, because both live in the central store and `read` opens only the
        #: project stores. None on a reading nobody attached them to, which is the
        #: behaviour every caller had before issue #843.
        self.pause = pause
        self.ramp = ramp
        #: The clock this reading was taken against. ONE per read, and `blocked` uses it
        #: rather than asking the clock again: a tick claims work orders in a loop and
        #: calls `blocked` on each, so a second clock would let the hold lift halfway
        #: through a tick. Same rule, for the same reason, as `invariants.clock`.
        self.at = time.time() if at is None else at

    def blocked(self, order_id: str | None = None) -> str:
        """Why nothing may launch, in words for the user — or "" when something may.

        The outage outranks the cap because it is the more useful of the two true
        answers: a slot frees by itself in minutes and says nothing, whereas "the account
        is refusing turns until 18:40" is the whole explanation for a quiet fleet. Same
        ranking, and the same reasoning, as the dependency label above the slot label in
        `invariants.status_label`.

        `order_id` is the work order asking. The user's pause is per order — an
        allow-listed one gets through it — so a caller that names no order is told the
        fleet is paused whatever the allow-list says.
        """
        cause = self.hold_cause(order_id)
        if cause == "fleet_outage":
            return ("the Claude usage window is spent, reopening at "
                    f"{clock(self.outage.reopens_at)}")  # type: ignore[union-attr]
        if cause == "fleet_paused":
            return pause_sentence(self.pause)  # type: ignore[arg-type]
        if cause == "fleet_ramp":
            return ramp_sentence(self.ramp, self.in_flight)  # type: ignore[arg-type]
        if cause == "fleet_cap":
            return f"{self.in_flight} of {self.cap} worker turns already in flight"
        return ""

    def hold_cause(self, order_id: str | None = None) -> str | None:
        """Which hold binds, as the cause code `Daemon._record_retry_held` writes."""
        if self.shut():
            return "fleet_outage"
        if self.pause is not None and not self.pause.allows(order_id):
            return "fleet_paused"
        if self.capacity_cause() is not None:
            return self.capacity_cause()
        return None

    def capacity_cause(self) -> str | None:
        """The holds that are about room, not about which order is asking.

        The ramp is named only while it is the TIGHTER of the two: under a steady cap no
        wider than the ramp's, the cap is what binds and the cap is the true answer.
        """
        if self.ramp is not None and self.at < self.ramp.until \
                and self.ramp.cap < self.cap and self.in_flight >= self.ramp.cap:
            return "fleet_ramp"
        if self.in_flight >= self.cap:
            return "fleet_cap"
        return None

    def shut(self) -> bool:
        """Is the ACCOUNT refusing turns right now — the outage half of `blocked`, alone?

        The panel needs this one and NOT the cap. A validation seat is a `claude -p` call,
        not a worker turn: it does not count against `max_in_flight` and holding it back
        because workers are busy would stall reviews for no reason. It is refused by the
        same window, though, and that half does apply (GitHub issue #235,
        `Daemon.validation_tick`).

        Reads `self.at`, not the clock, for the reason the attribute exists: one tick, one
        answer, however many callers ask.
        """
        return self.outage is not None and self.at < self.outage.reopens_at

    def launched(self) -> None:
        self.in_flight += 1


def pause_sentence(pause: FleetPause) -> str:
    since = f" since {clock(pause.since)}" if pause.since else ""
    why = f" ({pause.reason})" if pause.reason else ""
    return (f"the fleet is paused by the user{since}{why} — "
            f"`jarvis resume <wo-id>` lets one order through")


def ramp_sentence(ramp: Ramp, in_flight: int) -> str:
    slow = ", slowly: the last window was spent again soon after reopening" \
        if ramp.tripped else ""
    return (f"ramping up after the usage window reopened at {clock(ramp.since)}{slow} — "
            f"{in_flight} of {ramp.cap} worker turns in flight until {clock(ramp.until)}")


def read(cap: int, stores: Mapping[str, ProjectStore],
         now: float | None = None) -> Fleet:
    """The fleet's state, derived from every project's turns.

    Costs one COUNT per project plus one indexed `latest_turn` per work order in
    `RETRY_SWEEP_STATUSES` — the same shape and roughly the same cost as
    `Daemon.retry_paused_turns`, which walks the same rows to ask the same question of
    one project at a time.

    THE SAME TUPLE THAT SWEEP WALKS, and issue #259 is why. The outage is a fact about
    the ACCOUNT, not about the work order the refusal happened to land on, so a window
    whose only evidence is a pause parked in `waiting_pr_merge` is just as shut — and
    read through `ACTIVE_STATUSES` this could not see one, leaving the OS dispatching
    fresh work into a closed window. Nothing new can be held for ever by the widening:
    `due()` below still releases the hold the moment the window reopens, and the pause
    that proves it is one the retry sweep now relaunches.
    """
    in_flight = 0
    outage: Outage | None = None
    for name, store in stores.items():
        in_flight += store.count_running_turns()
        for wo in store.list_work_orders(statuses=RETRY_SWEEP_STATUSES):
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


def current(cat: Any, now: float | None = None, central: Any = None) -> Fleet:
    """`read` for a caller with no stores open — a CLI process, a dashboard request.

    Opens and closes its own, so a read-only surface can answer "why is nothing
    starting?" without holding connections it would then have to remember to close.
    With `central`, the user's pause and the reopen ramp are attached too.
    """
    stores: dict[str, ProjectStore] = {}
    try:
        for p in cat.projects:
            if p.path.is_dir():
                stores[p.name] = ProjectStore(p.path)
        state = read(cat.os.max_in_flight, stores, now)
        return attach(state, central) if central is not None else state
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
            # THE OUTAGE JUST LIFTED: the one tick that knows the window reopened. The
            # ramp (`ramp`) runs from here, so the backlog the window refused comes back
            # a couple of turns at a time instead of all at once (issue #843).
            central.set_state(REOPENED_KEY, f"{fleet.at:.0f}")
        return False
    if central.get_state(OUTAGE_ANNOUNCED_KEY):
        return False
    _check_breaker(central, fleet)
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


def _check_breaker(central: Any, fleet: Fleet) -> None:
    """A NEW outage: was the window spent again right after it reopened?

    That is the signature of issue #843 — the backlog resuming at once and spending the
    fresh window in minutes — and it repeats at every reopen unless something changes.
    What changes: the next ramp is slower (`BREAKER_RAMP_CAP`, twice as long), and the
    user hears about it ONCE, loudly, with the figures. A window that lasted clears it.
    """
    reopened = float(central.get_state(REOPENED_KEY) or 0.0)
    if reopened and fleet.at - reopened < BREAKER_SECONDS:
        minutes = max(1, int((fleet.at - reopened) // 60))
        central.set_state(BREAKER_KEY, f"{fleet.at:.0f}")
        central.add_inbox(
            project=fleet.outage.project,  # type: ignore[union-attr]
            title=(f"Claude usage window spent again {minutes} min after it reopened — "
                   f"the fleet will ramp back one turn at a time"),
            body=(f"The window reopened at {clock(reopened)} and was spent again by "
                  f"{clock(fleet.at)}. The resumed backlog burned it. Next reopen: at "
                  f"most {BREAKER_RAMP_CAP} worker turn in flight for "
                  f"{2 * RAMP_SECONDS // 60} minutes. To stop everything: `jarvis "
                  f"pause`; to let one order through: `jarvis resume <wo-id>`."),
            level="critical",
            wo_id=fleet.outage.wo_id,  # type: ignore[union-attr]
        )
    elif central.get_state(BREAKER_KEY):
        central.set_state(BREAKER_KEY, "")
