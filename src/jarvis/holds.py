"""When a work order was NOT ALLOWED TO WORK — the other half of `inspection`'s clock"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import db
from .project_store import (VALIDATION_AUTH_CAUSE, VALIDATION_HELD_CAUSE,
                            ProjectStore)
from .worker_session import PAUSE_AUTH, PAUSE_TRANSIENT, PAUSE_USAGE_LIMIT

#: The three transport pauses, named by `worker_session` and carried verbatim in `turn_paused`'s...
TRANSPORT = frozenset({PAUSE_USAGE_LIMIT, PAUSE_TRANSIENT, PAUSE_AUTH})

NEO_QUESTION = "neo_question"
GATE = "gate"
VALIDATION = "validation"
BUDGET = "budget"
MESSAGE = "message"

#: What completes the sentence "held 3.9h by …"
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

#: Which cause explains the other when two start at the same instant
_RANK = {PAUSE_USAGE_LIMIT: 0, PAUSE_TRANSIENT: 0, PAUSE_AUTH: 0,
         BUDGET: 1, GATE: 2, NEO_QUESTION: 3, VALIDATION: 4, MESSAGE: 5}

#: Shorter than this and a hold is the reconcile loop's own granularity rather than anything that...
_MIN_HOLD = 1.0

#: Timeline kind -> (cause, the payload field correlating it with its closer)
_OPEN: dict[str, tuple[str | None, str | None]] = {
    "turn_paused": (None, "seq"),
    "question_asked": (NEO_QUESTION, "neo_question_id"),
    "gate_requested": (GATE, "approval_id"),
    "validation_submitted": (VALIDATION, "round"),
    "validation_forced": (VALIDATION, "round"),
    "budget_exhausted": (BUDGET, None),
    "message_queued": (MESSAGE, "msg_id"),
}

#: Timeline kind -> (the causes it can close, the payload field, whether that field is a LIST of keys)
_CLOSE: dict[str, tuple[frozenset[str], str | None, bool]] = {
    "turn_resumed": (TRANSPORT, "retried_seq", False),
    "turn_cancelled": (TRANSPORT, "seq", False),
    # No `seq` in its payload, and rightly: giving up ends the pause whatever turn it was on
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
    """One interval this work order was not permitted to run, and what held it"""

    cause: str
    started: float
    ended: float | None = None
    #: What the timeline pairs this hold on — a Neo question id, an approval id, a round, a message id
    key: Any = field(default=None, compare=False)

    @property
    def open(self) -> bool:
        return self.ended is None

    def finish(self, now: float | None = None) -> float:
        return self.ended if self.ended is not None else (
            db.now() if now is None else now)

    @property
    def phrase(self) -> str:
        """"a fleet usage limit" — what completes "held 3.9h by …\""""
        return HOLD_CAUSES.get(self.cause, self.cause)

    def overlap(self, start: float, end: float, now: float | None = None) -> float:
        """Seconds of this hold that fall inside `[start, end]`"""
        return max(0.0, min(self.finish(now), end) - max(self.started, start))

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        return {"cause": self.cause, "phrase": self.phrase, "started": self.started,
                "ended": self.ended, "open": self.open,
                "seconds": round(self.finish(now) - self.started, 2)}


def held(store: ProjectStore, wo_id: str, *,
         now: float | None = None) -> list[Hold]:
    """Every interval the OS's record says this work order was held, oldest first"""
    now = db.now() if now is None else now
    spans = _episodes(store.list_events(wo_id, limit=_EVENT_LIMIT))
    spans = _outside(spans, _working(store.list_turns(wo_id), now), now)
    return _merge(spans, now)


def held_family(store: ProjectStore, wo_ids: Sequence[str], *,
                now: float | None = None) -> list[Hold]:
    """Every interval NO member of this family could run — a feature order's hold (docs/superpowers/specs/2026-09-30-time-in-state-counts-a-usage-limit-hold-as-running.md)"""
    now = db.now() if now is None else now
    episodes: list[Hold] = []
    turns: list[dict[str, Any]] = []
    for wo_id in wo_ids:
        episodes.extend(_episodes(store.list_events(wo_id, limit=_EVENT_LIMIT)))
        turns.extend(store.list_turns(wo_id))
    episodes.sort(key=lambda h: h.started)
    return _merge(_outside(episodes, _working(turns, now), now), now)


#: Enough timeline for any conversation the fleet has run
_EVENT_LIMIT = 10_000


def _key(payload: dict[str, Any], field: str | None) -> Any:
    return None if field is None else payload.get(field)



#: What ends a hold `_episodes` synthesised for a held round
_SYNTHESIS_CLOSERS = frozenset({
    "validation_submitted", "validation_forced", "validation_passed",
    "validation_rejected", "validation_escalated", "validation_void",
    "validation_failed",
})


def _close_synthesised(open_holds: dict[tuple[str, Any], Hold], done: list[Hold],
                       cause: str, ts: float) -> None:
    """End the unkeyed hold synthesised for a held round, if one is open"""
    hold = open_holds.pop((cause, None), None)
    if hold is not None:
        done.append(Hold(hold.cause, hold.started, max(ts, hold.started), hold.key))


def _episodes(events: Sequence[dict[str, Any]]) -> list[Hold]:
    """Pair the timeline's opening and closing events into raw hold spans"""
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
                                     max(ts, hold.started), hold.key))
        # Every terminal of a held round ends the synthesised hold, not only the resubmission (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
        if kind in _SYNTHESIS_CLOSERS:
            for synthesised in (PAUSE_USAGE_LIMIT, PAUSE_AUTH):
                _close_synthesised(open_holds, done, synthesised, ts)
        # A round the panel could not run because the usage window was spent closes the round and holds the...
        if kind == "validation_failed" and payload.get("cause") == VALIDATION_HELD_CAUSE:
            # The unkeyed slot never loses an episode silently — spec §2d1 point 2
            _close_synthesised(open_holds, done, PAUSE_USAGE_LIMIT, ts)
            open_holds[(PAUSE_USAGE_LIMIT, None)] = Hold(PAUSE_USAGE_LIMIT, ts)
            continue
        # The same synthesis for a round whose seats could not authenticate (docs/superpowers/specs/2026-09-26-the-panel-must-not-mistake-an-auth-failure-for-a-verdict.md §7.)
        if kind == "validation_failed" and payload.get("cause") == VALIDATION_AUTH_CAUSE:
            _close_synthesised(open_holds, done, PAUSE_AUTH, ts)
            open_holds[(PAUSE_AUTH, None)] = Hold(PAUSE_AUTH, ts)
            continue
        opener = _OPEN.get(kind)
        if opener is None:
            continue
        cause, field = opener
        cause = cause or str(payload.get("reason") or "")
        if cause not in HOLD_CAUSES:
            continue
        key = _key(payload, field)
        open_holds[(cause, key)] = Hold(cause, ts, key=key)
    done.extend(open_holds.values())
    done.sort(key=lambda h: h.started)
    return done


def _working(turns: Sequence[dict[str, Any]], now: float) -> list[tuple[float, float]]:
    """When a `claude -p` process of this work order was alive, merged and in order"""
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
    """`spans` with every second a worker turn was running cut out of them"""
    out: list[Hold] = []
    for hold in spans:
        cursor, end = hold.started, hold.finish(now)
        for start, stop in working:
            if stop <= cursor or start >= end:
                continue
            if start > cursor:
                out.append(Hold(hold.cause, cursor, start, hold.key))
            cursor = max(cursor, stop)
        if end > cursor:
            out.append(Hold(hold.cause, cursor,
                            None if hold.open and end >= now else end, hold.key))
    out.sort(key=lambda h: h.started)
    return out


def _merge(spans: Sequence[Hold], now: float) -> list[Hold]:
    """Overlaps counted once, attributed to whichever hold started first"""
    out: list[Hold] = []
    cursor = float("-inf")
    for hold in sorted(spans, key=lambda h: (h.started, _RANK.get(h.cause, 9))):
        start, end = max(hold.started, cursor), hold.finish(now)
        if end - start < _MIN_HOLD:
            continue
        out.append(Hold(hold.cause, start,
                        None if hold.open and end >= now else end, hold.key))
        cursor = end
    return out


def union_seconds(intervals: Sequence[tuple[float, float]]) -> float:
    """The measure of the UNION of these intervals — overlapping seconds counted once (docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md)"""
    total, cursor = 0.0, float("-inf")
    for start, end in sorted(intervals):
        if end <= start or end <= cursor:  # empty, or already covered
            continue
        total += end - max(start, cursor)
        cursor = end
    return total


def by_cause(spans: Sequence[Hold], start: float, end: float,
             now: float | None = None) -> dict[str, float]:
    """Seconds held inside `[start, end]`, per cause, biggest first"""
    totals: dict[str, float] = {}
    for hold in spans:
        seconds = hold.overlap(start, end, now)
        if seconds > 0:
            totals[hold.cause] = totals.get(hold.cause, 0.0) + seconds
    return dict(sorted(totals.items(), key=lambda kv: -kv[1]))
