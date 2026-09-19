"""When a work order was NOT ALLOWED TO WORK — the other half of `inspection`'s clock.

`inspection` partitions a session's WALL CLOCK: generating, blocked on a subagent,
running tools, idle. That partition answers "what was this turn doing" and cannot answer
"could it have been doing anything at all", and the difference is not academic. On
2026-09-18 two work orders read as 59% and 67% idle over six and five hours; the account's
usage window had been spent for four of those hours, so the OS was holding them exactly as
designed. Reporting that as waste — and alarming on it — is the OS asking the user to look
at something the OS itself created and understands.

So there are two clocks. WALL is how long it took in the real world and is never deleted:
it is the honest answer to "how long did this take". ACTIVE is wall minus every interval
the OS's own record says the order was held, and it is the one a threshold belongs on.

## The record is the timeline, not a reconstruction

Every hold the OS imposes is already written down as a PAIR of timeline events — one that
opens it and one that closes it, correlated by an id in the payload. `turn_paused` /
`turn_resumed` carry the refused turn's `seq`; `question_asked` / `neo_answered` carry the
Neo question id; `gate_requested` / `gate_decided` carry the approval id. So a hold window
is read off the record rather than inferred from the shape of a gap, which is the one way
to get "held 3.9h by a fleet usage limit" right instead of plausible. `_OPEN` and `_CLOSE`
below are that pairing, and adding a cause is a row in each rather than a new walk.

A hold with no closing event is STILL HELD and runs to `now`. That is not a gap in the
data — it is the live case, and it is the one the alarm is judged against.

## Two rules that make the arithmetic safe

CLIPPED TO THE GAPS BETWEEN TURNS. A hold is only subtracted where no `claude -p` process
of this work order was alive. Some holds legitimately overlap a running turn — a message
queued while the worker is mid-task waits for it to finish, a validation round runs in
PARALLEL with a `needs_review` park (issue 212) — and subtracting those seconds would
delete time the order spent working, which is the opposite of the mistake this module
exists to fix. The clip also makes `held <= idle` true by construction, which is what lets
`inspection` report the residual as the honest "nothing was holding it" number.

It is also why subagent time survives untouched, with no special case: `blocked` is inside
a running turn, so nothing can ever be subtracted from it. The work order that commissioned
this said so in as many words — 130 minutes of blocking on its own subagent is the order's
own choice and is exactly what an alarm should still see.

MERGED, EARLIEST START WINS. Two causes can overlap (a gate escalated while the usage
window is spent). The overlap is counted ONCE, and attributed to whichever started first —
so the per-cause figures always sum to the total and a reader can never be told the same
four hours twice under two names.

A DEPENDENCY EDGE HOLDS NOTHING MEASURABLE, and that is an enumeration result rather than
an omission: an order blocked on a dependency has not been dispatched, so it has no turn,
no transcript and no wall clock for a hold to intersect. `jarvis wo list` already says
`pending — blocked by …` and nothing here can improve on it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Sequence

from . import db
from .project_store import VALIDATION_HELD_CAUSE, ProjectStore
from .worker_session import PAUSE_AUTH, PAUSE_TRANSIENT, PAUSE_USAGE_LIMIT

#: The three transport pauses, named by `worker_session` and carried verbatim in
#: `turn_paused`'s `reason`. Imported rather than re-spelled: the payload is written by
#: that module and a second copy of these strings is a drift waiting to happen.
TRANSPORT = frozenset({PAUSE_USAGE_LIMIT, PAUSE_TRANSIENT, PAUSE_AUTH})

NEO_QUESTION = "neo_question"
GATE = "gate"
VALIDATION = "validation"
BUDGET = "budget"
MESSAGE = "message"

#: What completes the sentence "held 3.9h by …". Here rather than at each surface so the
#: report, the alarm and the dashboard cannot call one hold three different things — the
#: rule `worker_session.PAUSE_NOUN` already follows for its own third of this table.
HOLD_CAUSES = {
    PAUSE_USAGE_LIMIT: "a fleet usage limit",
    PAUSE_TRANSIENT: "a Claude API outage",
    PAUSE_AUTH: "an expired Claude Code sign-in",
    NEO_QUESTION: "a question with Neo",
    GATE: "a privileged-action gate awaiting a verdict",
    VALIDATION: "the validation panel",
    BUDGET: "an exhausted budget",
    MESSAGE: "a message waiting to be delivered",
}

#: Which cause explains the other when two start at the same instant. NOT cosmetic, and
#: not a guess about importance: these holds CAUSE one another, and the merge below would
#: otherwise name the symptom. `Daemon.deliver_messages` holds a queued message for
#: exactly as long as the transport is paused (kn-fa875823), so a message and a spent
#: usage window open on the same reap and the window is the answer — wo-16a488ee's
#: 197-minute hold on 2026-09-18 reported as "a message waiting to be delivered" until
#: this existed, which is a true sentence that hides the only useful one. Lower sorts
#: first; the transport pauses lead because nothing in the OS outranks the account.
_RANK = {PAUSE_USAGE_LIMIT: 0, PAUSE_TRANSIENT: 0, PAUSE_AUTH: 0,
         BUDGET: 1, GATE: 2, NEO_QUESTION: 3, VALIDATION: 4, MESSAGE: 5}

#: Shorter than this and a hold is the reconcile loop's own granularity rather than
#: anything that happened — a round opened and settled inside one tick, a question Neo
#: answered in the same second. Dropped so a report does not carry a line per non-event.
_MIN_HOLD = 1.0

#: Timeline kind -> (cause, the payload field correlating it with its closer). A cause of
#: `None` means the event NAMES its own — `turn_paused` carries `reason`, which is how one
#: row here covers all three transport pauses without pretending they are one thing.
#: A key of `None` means the hold is unkeyed: at most one can be open at a time, so the
#: next closer of that cause ends it.
_OPEN: dict[str, tuple[str | None, str | None]] = {
    "turn_paused": (None, "seq"),
    "question_asked": (NEO_QUESTION, "neo_question_id"),
    "gate_requested": (GATE, "approval_id"),
    "validation_submitted": (VALIDATION, "round"),
    "validation_forced": (VALIDATION, "round"),
    "budget_exhausted": (BUDGET, None),
    "message_queued": (MESSAGE, "msg_id"),
}

#: Timeline kind -> (the causes it can close, the payload field, whether that field is a
#: LIST of keys). `turn_resumed` names the turn it is retrying rather than its own, and
#: `message_delivered` closes everything it flushed in one go — both are why the field is
#: named per kind instead of assumed.
_CLOSE: dict[str, tuple[frozenset[str], str | None, bool]] = {
    "turn_resumed": (TRANSPORT, "retried_seq", False),
    "turn_cancelled": (TRANSPORT, "seq", False),
    # No `seq` in its payload, and rightly: giving up ends the pause whatever turn it
    # was on. An unkeyed closer ends every open hold of its causes.
    "turn_retries_exhausted": (TRANSPORT, None, False),
    "neo_answered": (frozenset({NEO_QUESTION}), "neo_question_id", False),
    "escalation_answered": (frozenset({NEO_QUESTION}), "neo_question_id", False),
    "gate_decided": (frozenset({GATE}), "approval_id", False),
    "gate_dismissed": (frozenset({GATE}), "approval_id", False),
    "gate_abandoned": (frozenset({GATE}), "approval_id", False),
    "gate_opened": (frozenset({GATE}), "approval_id", False),
    "gate_superseded": (frozenset({GATE}), "approval_id", False),
    "validation_passed": (frozenset({VALIDATION}), "round", False),
    "validation_rejected": (frozenset({VALIDATION}), "round", False),
    "validation_escalated": (frozenset({VALIDATION}), "round", False),
    "validation_void": (frozenset({VALIDATION}), "round", False),
    "validation_failed": (frozenset({VALIDATION}), "round", False),
    "budget_resumed": (frozenset({BUDGET}), None, False),
    "message_delivered": (frozenset({MESSAGE}), "msg_ids", True),
}


@dataclass(frozen=True)
class Hold:
    """One interval this work order was not permitted to run, and what held it.

    `ended` is None while the hold is still on. Not `now` frozen at read time: a reader
    that stores one of these and re-renders it later would otherwise report a hold as
    over because it once asked the clock.

    THREE FIELDS AND NO FREE TEXT, WHICH IS A SECURITY BOUNDARY RATHER THAN MINIMALISM.
    An earlier draft carried a `detail` lifted from the opening event's payload — the
    refusal message, the question, the gate's command — and `as_dict` published it
    through `jarvis inspect --json`. Every one of those sources is text this OS does not
    control: a gate's command is the privileged shell line a worker proposed, which in
    this repository routinely embeds a tokenised remote, and a transport pause's `error`
    is a VCS stderr tail, which is kn-1791a5e6's leak verbatim. Truncating is not a
    defence there (the credential is at the FRONT of the URL), and scrubbing is a
    denylist that only removes the leak somebody already thought of. A timing report has
    no use for any of it: what a reader needs is the CAUSE, and `HOLD_CAUSES` is a fixed
    table of our own sentences. The reset moment is not lost with it — the timeline keeps
    `reset_at` on the `turn_paused` payload, and `invariants.pause_note` is what renders
    it to a person. Adding a field here means publishing it; think first.
    """

    cause: str
    started: float
    ended: float | None = None

    @property
    def open(self) -> bool:
        return self.ended is None

    def finish(self, now: float | None = None) -> float:
        return self.ended if self.ended is not None else (
            time.time() if now is None else now)

    @property
    def phrase(self) -> str:
        """"a fleet usage limit" — what completes "held 3.9h by …"."""
        return HOLD_CAUSES.get(self.cause, self.cause)

    def overlap(self, start: float, end: float, now: float | None = None) -> float:
        """Seconds of this hold that fall inside `[start, end]`."""
        return max(0.0, min(self.finish(now), end) - max(self.started, start))

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        return {"cause": self.cause, "phrase": self.phrase, "started": self.started,
                "ended": self.ended, "open": self.open,
                "seconds": round(self.finish(now) - self.started, 2)}


def held(store: ProjectStore, wo_id: str, *,
         now: float | None = None) -> list[Hold]:
    """Every interval the OS's record says this work order was held, oldest first.

    One indexed read of the timeline and one of the turns — no model, no transcript, and
    nothing written. Cheap enough to run per running work order per reconcile tick, which
    is what `Daemon.check_burning_turns` does with it.
    """
    now = time.time() if now is None else now
    spans = _episodes(store.list_events(wo_id, limit=_EVENT_LIMIT))
    spans = _outside(spans, _working(store.list_turns(wo_id), now), now)
    return _merge(spans, now)


#: Enough timeline for any conversation the fleet has run. `list_events` takes the OLDEST
#: `limit` rows, so a cap that bit would drop the RECENT holds — the ones a live alarm is
#: judged against — and report a busy work order as never held at all.
_EVENT_LIMIT = 10_000


def _key(payload: dict[str, Any], field: str | None) -> Any:
    return None if field is None else payload.get(field)



def _episodes(events: Sequence[dict[str, Any]]) -> list[Hold]:
    """Pair the timeline's opening and closing events into raw hold spans.

    Closers are applied BEFORE openers on the same event, so a kind that is both — a
    `validation_submitted` that both ends a usage-limit hold on the panel and starts the
    round's own — closes the old one rather than being swallowed by it.
    """
    open_holds: dict[tuple[str, Any], Hold] = {}
    done: list[Hold] = []
    for event in events:
        kind = event["kind"]
        ts = float(event["ts"])
        payload = db.from_json(event.get("payload"), {}) or {}
        closer = _CLOSE.get(kind)
        if closer is not None:
            causes, field, many = closer
            if field is None:
                keys: list[Any] = [None]
            elif many:
                keys = list(payload.get(field) or [])
            else:
                keys = [payload.get(field)]
            for cause, key in list(open_holds):
                if cause in causes and (field is None or key in keys):
                    hold = open_holds.pop((cause, key))
                    done.append(Hold(hold.cause, hold.started,
                                     max(ts, hold.started)))
        # A round the panel could not run because the usage window was spent closes the
        # round and holds the WORK ORDER until the window reopens — a usage-limit hold
        # with no `turn_paused` behind it, because no worker turn was ever launched into
        # it. Ended by the next submission, which is the OS reopening the round itself.
        if kind == "validation_failed" and payload.get("cause") == VALIDATION_HELD_CAUSE:
            open_holds[(PAUSE_USAGE_LIMIT, None)] = Hold(PAUSE_USAGE_LIMIT, ts)
            continue
        if kind in ("validation_submitted", "validation_forced"):
            reopened = open_holds.pop((PAUSE_USAGE_LIMIT, None), None)
            if reopened is not None:
                done.append(Hold(reopened.cause, reopened.started,
                                 max(ts, reopened.started)))
        opener = _OPEN.get(kind)
        if opener is None:
            continue
        cause, field = opener
        cause = cause or str(payload.get("reason") or "")
        if cause not in HOLD_CAUSES:
            continue
        key = _key(payload, field)
        open_holds[(cause, key)] = Hold(cause, ts)
    done.extend(open_holds.values())
    done.sort(key=lambda h: h.started)
    return done


def _working(turns: Sequence[dict[str, Any]], now: float) -> list[tuple[float, float]]:
    """When a `claude -p` process of this work order was alive, merged and in order."""
    spans = sorted((float(t["started_at"]),
                    float(t["ended_at"]) if t["ended_at"] else now) for t in turns)
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _outside(spans: Sequence[Hold], working: Sequence[tuple[float, float]],
             now: float) -> list[Hold]:
    """`spans` with every second a worker turn was running cut out of them.

    See the module note: a hold that overlaps a live turn is the OS queueing something
    behind work that IS happening, and subtracting it would delete the work.
    """
    out: list[Hold] = []
    for hold in spans:
        cursor, end = hold.started, hold.finish(now)
        for start, stop in working:
            if stop <= cursor or start >= end:
                continue
            if start > cursor:
                out.append(Hold(hold.cause, cursor, start))
            cursor = max(cursor, stop)
        if end > cursor:
            out.append(Hold(hold.cause, cursor,
                            None if hold.open and end >= now else end))
    out.sort(key=lambda h: h.started)
    return out


def _merge(spans: Sequence[Hold], now: float) -> list[Hold]:
    """Overlaps counted once, attributed to whichever hold started first.

    The alternative — letting two causes both claim the same four hours — makes the
    per-cause figures sum past the total, and a reader who adds them up is then told the
    OS lost track of an afternoon.

    `_RANK` breaks the tie, and a tie is the common case rather than the exotic one: two
    holds that open on the same reap of the same turn start at the same float.
    """
    out: list[Hold] = []
    cursor = float("-inf")
    for hold in sorted(spans, key=lambda h: (h.started, _RANK.get(h.cause, 9))):
        start, end = max(hold.started, cursor), hold.finish(now)
        if end - start < _MIN_HOLD:
            continue
        out.append(Hold(hold.cause, start,
                        None if hold.open and end >= now else end))
        cursor = end
    return out


def by_cause(spans: Sequence[Hold], start: float, end: float,
             now: float | None = None) -> dict[str, float]:
    """Seconds held inside `[start, end]`, per cause, biggest first."""
    totals: dict[str, float] = {}
    for hold in spans:
        seconds = hold.overlap(start, end, now)
        if seconds > 0:
            totals[hold.cause] = totals.get(hold.cause, 0.0) + seconds
    return dict(sorted(totals.items(), key=lambda kv: -kv[1]))
