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
#:
#: `account-window` is the odd one: it records no look at all, it is what the daemon
#: writes against the ACCOUNT when the usage window is shut (spec
#: docs/superpowers/specs/2026-09-28-a-usage-limit-is-not-a-failed-sweep.md §3).
#: `re-assert` is the other odd one: it records a look NOBODY TOOK. The fingerprint is
#: unchanged and a re-derivable reason explains the stillness, so the prior judgement is
#: copied forward for free rather than bought again (spec
#: docs/superpowers/specs/2026-10-02-the-health-sweep-must-not-pay-to-restate-a-fingerprint.md).
TRIGGERS = ("first-look", "changed", "stale", "re-assert", "account-window")

#: Named rather than spelled at each call site: the daemon routes on it and the supervisor
#: records it, and a typo in either would silently buy a call or silently skip one.
REASSERT = "re-assert"

#: THE BLOCKER ID FOR EACH TRANSPORT PAUSE CAUSE — the canonical link between the two
#: vocabularies, and the only place they meet. ONE ID PER CAUSE so a catalog can count a
#: spent window as an explanation without also excusing an expired sign-in; `usage-limit`
#: keeps its spelling so catalogs naming it still load (Neo q1241).
#:
#: The values are `holds` constant NAMES, resolved by `transport_blockers()`, because the
#: cause literals must not be re-spelled here and they cannot be imported at module
#: level: `catalog` imports this module, and `holds` reaches `catalog` through
#: `worker_session`, so `from .holds import …` here is a circular import.
_TRANSPORT_CAUSE_NAMES = {
    "usage-limit": "PAUSE_USAGE_LIMIT",
    "api-outage": "PAUSE_TRANSIENT",
    "expired-login": "PAUSE_AUTH",
}


def transport_blockers() -> dict[str, str]:
    """`_TRANSPORT_CAUSE_NAMES` with each name resolved to the `holds` pause cause."""
    from . import holds

    return {blocker_id: getattr(holds, name)
            for blocker_id, name in _TRANSPORT_CAUSE_NAMES.items()}


#: THE RE-DERIVABLE REASONS A UNIT IS STANDING STILL, in FIRST-MATCH order, and this
#: tuple is the vocabulary rather than the policy: which of them COUNT is
#: `SupervisorConfig.health_reassert_blockers`, a catalog setting (kn-1cec46b5).
#:
#: Each id is answered by the existing canonical derivation and nothing new is written —
#: `user` by `invariants.true_blockers`, `dependency` by `ops.blocked_by`, `pull-request`
#: by the row's own two columns, the three transport ids by `holds.held` against ONE
#: cause each (`_TRANSPORT_CAUSE_NAMES`), `assumptions` by
#: `ProjectStore.pending_assumptions`. A reason re-derived here is how two surfaces come
#: to disagree about one order.
BLOCKERS = ("user", "dependency", "pull-request", *_TRANSPORT_CAUSE_NAMES, "assumptions")

#: A FEATURE's answer, and it is not in `BLOCKERS` because it is not a reason of its own:
#: it means every non-settled child has one of those above. Not configurable for that
#: reason — narrowing the five narrows this with them.
CHILDREN = "children"

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

    # `investigation_verdict` is written ONTO the subject by `ops.submit_verdict` (§7 of
    # docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets-investigated.md),
    # so it is the same class of event and `stuck.fingerprint` must not count it.
    return (*ALARM_EVENT_KINDS, "attention", "investigation_verdict")


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


def blocker(pstore: Any, subject: dict[str, Any], cfg: Any) -> str | None:
    """The id of the first re-derivable reason this unit is standing still, or None.

    `subject` is `fingerprint`'s dict, and this is as cheap and as deterministic as
    `fingerprint` is for the same reason: it runs per open unit per sweep tick, so
    nothing here opens a session file, reaches the network or calls a model.

    IT LIVES HERE AND NOT IN `supervisor` because `due` is the one consumer that must
    stay pure, and a helper in the module that owns the spend decision cannot be bypassed
    by a caller that forgets it.
    """
    if subject["kind"] == "feature_order":
        return _children_blocker(pstore, subject["row"], cfg)
    return _work_order_blocker(pstore, subject["row"], cfg)


def _work_order_blocker(pstore: Any, row: dict[str, Any], cfg: Any) -> str | None:
    """FIRST MATCH WINS, in `BLOCKERS`' order — the catalog chooses which count, never
    the order they are asked in.

    `user` first because it is the one answer that outranks the rest: an order owing the
    user a decision is explained whatever else is also true, and `true_blockers` already
    ranks within itself. It also subsumes the closed-pull-request and dead-dependency
    cases, so neither is asked again below.
    """
    from . import db, holds, invariants, ops

    counts = tuple(getattr(cfg, "health_reassert_blockers", BLOCKERS) or ())
    causes = transport_blockers()
    for blocker_id in BLOCKERS:
        if blocker_id not in counts:
            continue
        # ON `db.now()`, THE OS'S CLOCK, which is what the sweep itself measures every
        # window against (`due`'s `now`). `true_blockers` defaults to `time.time()`, and
        # the two differ for anything whose age is read from a row rather than from the
        # wall — a message queued at the OS's clock would otherwise read as months
        # undelivered.
        if blocker_id == "user" and invariants.true_blockers(pstore, row, now=db.now()):
            return blocker_id
        # NOT `invariants.dead_dependencies`, which answers the narrower "can never
        # clear", and not `true_blockers`, which is silent on ordinary blocking by
        # design — and ordinary blocking is the whole of alarm al-4bb82f7e.
        if blocker_id == "dependency" and ops.blocked_by(pstore, row):
            return blocker_id
        # A PURE ROW READ, NO NETWORK: `github.pr_view` is the daemon's poll, never a
        # sweep's. `pr_state == 'CLOSED'` cannot reach here — the status gate, plus
        # `user` above.
        if (blocker_id == "pull-request" and row.get("status") == "waiting_pr_merge"
                and row.get("pr_url")):
            return blocker_id
        # ONE CAUSE PER ID, never the whole of `holds.TRANSPORT`: a catalog narrowed to
        # `usage-limit` has explained a spent window and nothing else, so an order held
        # by an expired sign-in pays (Neo q1241). `held` reads the OPEN/CLOSE pairs, so
        # "held right now" is a fact and not a guess.
        if blocker_id in causes and any(
                h.open and h.cause == causes[blocker_id]
                for h in holds.held(pstore, row["id"], now=db.now())):
            return blocker_id
        # LAST, and reachable only where `user` declined it: `true_blockers` suppresses
        # the assumptions line while `neo_reviews_later` holds, and an assumption sitting
        # with Neo is still a re-derivable reason the unit is still.
        if blocker_id == "assumptions" and pstore.pending_assumptions(row["id"]):
            return blocker_id
    return None


def _children_blocker(pstore: Any, row: dict[str, Any], cfg: Any) -> str | None:
    """A FEATURE's stillness is its children's, and nothing else explains it (Neo q1220).

    `CHILDREN` only when every non-settled child has a blocker by the rules above and
    none is running. IT FAILS CLOSED: one child moving, or one nobody can explain, and
    the feature pays — as does a feature with no children at all.
    """
    from .project_store import TERMINAL_STATUSES

    open_children = [c for c in pstore.feature_children(row["id"])
                     if c.get("status") not in TERMINAL_STATUSES]
    if not open_children or any(c.get("status") == "running" for c in open_children):
        return None
    if all(_work_order_blocker(pstore, c, cfg) for c in open_children):
        return CHILDREN
    return None


def due(review: dict[str, Any] | None, current: str, cfg: Any, now: float,
        created: float, last_attempt: float | None = None,
        blocker: str | None = None) -> str | None:
    """Which trigger says to look at this unit now, or None to leave it alone.

    `review` is the unit's most recent `health_reviews` row, or None. `created` is the
    unit's own `created_at`, and it is an argument rather than something read off
    `subject` because this function is the whole of the spend decision and reading state
    inside it would make that decision untestable without a store.

    `last_attempt` is the ts of the most recent sweep OF ANY OUTCOME, failures included,
    or None if never swept. `review` deliberately cannot answer that — see the floor.

    `blocker` is `blocker()`'s answer for this unit, AND IT ARRIVES AS AN ARGUMENT: this
    function reads no state today and must not start, or every test of the spend decision
    would need a store.

    THE `stale` CLAUSE FIRES ONCE PER STALE WINDOW, not once for ever. §4 words it as
    "exactly one review until it moves again", and once-for-ever is unbuildable against
    the dedupe test that same section specifies: it grades four consecutive sweeps at an
    UNCHANGED fingerprint, which no strictly-once rule can produce. A window also keeps
    the alarm-side dedupe as the thing that stops the repeat, which is where §4 puts it.
    """
    interval = cfg.health_min_interval_minutes * SECONDS_PER_MINUTE
    stale = cfg.health_stale_minutes * SECONDS_PER_MINUTE
    # THE FLOOR IS ON ATTEMPTS, NOT ON JUDGEMENTS. Every branch below floors on
    # `review`, which excludes a failure — so a sweep that always fails has no floor at
    # all and falls back to the tick rate. §4.1 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.
    if last_attempt is not None and now - last_attempt < interval:
        return None
    if review is None:
        # A unit created moments ago has nothing to say yet, and the sweep is the
        # standing cost of watching: the floor applies to the first look too.
        return "first-look" if now - created >= interval else None
    since = now - float(review.get("ts") or 0.0)
    if str(review.get("fingerprint") or "") != current:
        return "changed" if since >= interval else None
    if since < stale:
        return None
    # THE FREE ROW, AND THE GATE IS NARROW ON PURPOSE. A prior `clear` found nothing, so
    # there is no judgement to copy forward and a free row would be the OS inventing one;
    # a unit nothing explains is one whose stillness IS the news. Both stay paid.
    if str(review.get("outcome")) == "findings" and blocker is not None:
        return REASSERT
    return "stale"
