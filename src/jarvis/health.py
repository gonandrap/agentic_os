"""When the OS looks at an open instrument, and what it looks at.

§4 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md. TWO JOBS KEPT
STRICTLY APART: the probes decide what is wrong, and this decides when it is worth
spending a model call to ask. Nothing here calls a model, reads a transcript or writes a
row.

The fingerprint is deliberately CHEAP and DETERMINISTIC. It is computed for every open
unit of every supervised project on every sweep tick, so anything in it that opened a
session file would make watching cost more than judging.
"""

from __future__ import annotations

from typing import Any

SECONDS_PER_MINUTE = 60  # a unit, not a setting

#: The vocabulary `due` answers in, and `health_reviews.trigger` records. Recorded rather
#: than re-derived because "why did it look now" is a question about a decision already
#: taken, and re-deriving it later reads the state as it is now.
TRIGGERS = ("first-look", "changed", "stale")

#: What separates the parts of a fingerprint. Any character not in a status, a sequence
#: number or a count would do; the point is that the string is one opaque value to
#: everything downstream, which compares it and never parses it.
SEP = "|"


def observer_kinds() -> tuple[str, ...]:
    """The `wo_events` kinds the OS writes ABOUT a unit while watching it, which the
    fingerprint must not count.

    THIS IS A CORRECTNESS RULE, NOT TIDINESS. A sweep writes `health_finding` and
    `health_reviewed` onto the carrier and raises the attention flag, which writes an
    `attention` event of its own — so a fingerprint that counted any of them would MOVE
    AS A RESULT OF BEING LOOKED AT, and the dedupe, which is equality of exactly this
    string, could then never engage. The alarm would come straight back the instant the
    user put the flag down: §6.3 of the PR 159 spec's wallpaper failure, arriving through
    a door that spec never had.

    §4 lists `needs_attention` and a raw event count among the fingerprint's components.
    Both are perturbed by the observation and neither can be one; the four-assertion
    dedupe test that same section specifies is what catches it.
    """
    from .project_store import ALARM_EVENT_KINDS

    return (*ALARM_EVENT_KINDS, "attention")


def fingerprint(pstore: Any, subject: dict[str, Any]) -> str:
    """A cheap, deterministic summary of everything about this unit that can move.

    `subject` is `{"kind": "work_order" | "feature_order", "row": <the store row>}` —
    the same vocabulary `supervisor.build_evidence` takes, so a caller holds one object
    and not two.

    Two units with the same fingerprint are the same situation, which is what makes it
    both the trigger and the dedupe memory. It is NOT a hash: an unequal pair is the only
    question ever asked of it, and a readable value is what makes a `health_reviews` row
    diagnosable by eye.
    """
    row = subject["row"]
    if subject["kind"] == "feature_order":
        # A feature has no session, so what moves is its children and its rounds. The
        # child STATUSES rather than their ids: a child re-filed under a new id with the
        # same status is a change, and `feature_children` returns them in a stable order.
        children = pstore.feature_children(row["id"])
        latest = pstore.latest_validation_round(fo_id=row["id"])
        parts = [
            str(row.get("status") or ""),
            ",".join(str(c.get("status") or "") for c in children),
            str(latest["round"]) if latest else "-",
            str(len(pstore.superseded_children(row["id"]))),
        ]
    else:
        turn = pstore.latest_turn(row["id"])
        parts = [
            str(row.get("status") or ""),
            str(turn["seq"]) if turn else "-",
            str(turn["state"]) if turn else "-",
            str(pstore.count_events(row["id"], exclude=observer_kinds())),
            str(len(pstore.pending_assumptions(row["id"]))),
            str(len(pstore.queued_messages(row["id"]))),
        ]
    return SEP.join(parts)


def due(review: dict[str, Any] | None, current: str, cfg: Any, now: float,
        created: float) -> str | None:
    """Which trigger says to look at this unit now, or None to leave it alone.

    `review` is the unit's most recent `health_reviews` row, or None. `created` is the
    unit's own `created_at`, and it is an argument rather than something read off
    `subject` because this function is the whole of the spend decision and reading state
    inside it would make that decision untestable without a store.

    THE `stale` CLAUSE FIRES ONCE PER STALE WINDOW, not once for ever. §4 words it as
    "exactly one review until it moves again", and once-for-ever is unbuildable against
    the dedupe test that same section specifies: it grades four consecutive sweeps at an
    UNCHANGED fingerprint, which no strictly-once rule can produce. A window also keeps
    the alarm-side dedupe as the thing that stops the repeat, which is where §4 puts it.
    """
    interval = cfg.health_min_interval_minutes * SECONDS_PER_MINUTE
    stale = cfg.health_stale_minutes * SECONDS_PER_MINUTE
    if review is None:
        # A unit created moments ago has nothing to say yet, and the sweep is the
        # standing cost of watching: the floor applies to the first look too.
        return "first-look" if now - created >= interval else None
    since = now - float(review.get("ts") or 0.0)
    if str(review.get("fingerprint") or "") != current:
        return "changed" if since >= interval else None
    return "stale" if since >= stale else None
