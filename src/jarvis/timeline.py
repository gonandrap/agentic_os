"""Work order timeline: the story, not the plumbing.

`wo_events` records everything that happens to a work order, which mixes two very
different audiences. The user wants the story — what was asked, what the worker
decided, what came back. The rest (message delivery bookkeeping, Claude Code session
hooks, turn boundaries, session binding) exists to debug the circuitry.

`build_timeline` merges events with the actual conversation and renders each entry as
prose. Debug entries are held back unless explicitly requested.

The second audience problem, and the one this module keeps getting wrong in the other
direction: an entry that repeats text the reader is already looking at costs attention
rather than losing it, so nothing fails and nobody reports it. Three did — the opening
entry restated the work order's own description, an assumption restated the assumption
listed a screen above, and a question was printed in full directly above its answer. The
rule that replaced them: an entry says WHAT HAPPENED, and where the thing that happened
has a record of its own, it POINTS at it (`_ref`) rather than reproducing it.
"""

from __future__ import annotations

import json
import time
from typing import Any

# Plumbing: how a message got carried, which session was bound, when a turn ended.
# None of it tells the user anything about the work itself.
DEBUG_KINDS = frozenset({
    "message_queued",           # queued for delivery — the message body is the signal
    "delivering",               # delivery attempt
    "message_delivered",        # delivery receipt
    "turn_started",             # a `claude -p` turn was launched
    "turn_ended",               # that turn's process finished and its reply was captured
    "session_released",         # a legacy background agent handed its session over
    "hook_ignored",             # a hook from a session that is not this work order's
    "permission_mode_changed",  # worker permission plumbing
    "notification_ignored",     # idle prompt on an already-settled work order
    # Both answer kinds are the SAME MOMENT as the message that carries the answer, and
    # both writers queue that message in the same breath. Rendered as signal they cost
    # the reader two lines for one event — "Neo answered the worker" with nothing under
    # it, then the answer itself — and the second line is the one worth reading. So the
    # bookkeeping is debug and the answer is the timeline entry; `_message_label` is
    # what makes the surviving line say who actually spoke.
    "neo_answered",             # Neo's answer went out — the message beneath it is the answer
    "escalation_answered",      # the user's answer to an escalation, likewise
})

STATUS_LABEL = {
    "pending": "Queued",
    "dispatching": "Dispatching worker",
    "running": "Running",
    "waiting_input": "Waiting on you",
    "validating": "Under review by the validation panel",
    "needs_review": "Needs your review",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


def event_level(kind: str) -> str:
    """"debug" for plumbing, "signal" for anything the user should see by default.

    Unknown kinds are signal — better to show an unclassified event than to swallow it.
    """
    if kind.startswith("hook:") or kind in DEBUG_KINDS:
        return "debug"
    return "signal"


def _clock(reset_at: Any) -> str:
    """A usage-limit reset moment as local wall-clock, or "" if there was none."""
    if not isinstance(reset_at, (int, float)):
        return ""
    return time.strftime("%H:%M", time.localtime(float(reset_at)))


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("payload")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _neo_question_id(p: dict[str, Any]) -> int | None:
    qid = p.get("neo_question_id")
    try:
        return int(qid) if qid is not None else None
    except (TypeError, ValueError):
        return None


def _ref(kind: str, p: dict[str, Any]) -> dict[str, Any] | None:
    """The record this entry POINTS AT, rather than reproduces, or None.

    An entry carries a ref when the thing that happened has a home of its own that says
    it better and says more: a question has an answer, and a timeline is the wrong place
    to read a paragraph twice. Deliberately surface-neutral — `{kind, id, label}`, not a
    URL — because the same entry is rendered by the dashboard, which has a page for it,
    and by `jarvis wo show`, which has `jarvis neo show`.
    """
    if kind == "question_asked":
        qid = _neo_question_id(p)
        if qid is not None:
            return {"kind": "neo_question", "id": qid, "label": f"question #{qid}"}
    return None


def _describe(kind: str, p: dict[str, Any]) -> tuple[str, str]:
    """(label, detail) in plain language for one event.

    Reads the event's own payload and nothing else. It used to take the work order too,
    for the `created` entry that restated its title and description; that entry now says
    only that it happened, and the parameter went with it.
    """
    if kind == "created":
        # NO detail. The title and the description are the top of every surface that
        # renders this timeline — the work order page, `jarvis wo show` — so repeating
        # them as the first entry spends the reader's first scroll on text they have
        # just read. What the entry is worth saying is that it happened, and when.
        return "Work order created", ""
    if kind == "status":
        status = p.get("status", "")
        return STATUS_LABEL.get(status, status or "Status changed"), ""
    if kind == "dispatched":
        return "Worker dispatched", p.get("worktree") or ""
    if kind == "turn_failed":
        return "Worker turn failed", (p.get("error") or "")[:200]
    # The self-healing trio. Deliberately NOT filed under "Worker turn failed": nothing
    # about the WORK went wrong — the transport did, either by refusing the turn (the
    # usage window) or by dropping it (the API) — and the OS puts itself right. What the
    # reader needs is the pause, the resume, and only if it comes to that, the point at
    # which the OS gave up and it became their problem.
    #
    # `reason` distinguishes the two. It is absent on rows written before transient
    # retries existed, and those were all usage-limit ones, so its absence reads as that
    # — which is why the legacy kinds below need no payload migration.
    if kind in ("turn_paused", "rate_limited"):
        if p.get("reason") == "transient":
            status = f" {p['status']}" if p.get("status") else ""
            return (f"Paused — Claude API error{status}",
                    f"{p.get('error') or ''} · retrying shortly")
        when = _clock(p.get("reset_at"))
        return ("Paused — Claude usage limit",
                f"{p.get('error') or ''}"
                + (f" · resuming after {when}" if when else ""))
    if kind in ("turn_resumed", "rate_limit_retry"):
        what = ("the Claude API error" if p.get("reason") == "transient"
                else "the usage limit")
        attempt = p.get("attempt")
        of = f" of {p['of']}" if p.get("of") else ""
        return (f"Resumed after {what}",
                f"attempt {attempt}{of}" if attempt else "")
    if kind in ("turn_retries_exhausted", "rate_limit_exhausted"):
        what = ("Claude API" if p.get("reason") == "transient" else "usage-limit")
        return ("Still failing after retrying",
                f"{p.get('attempts')} {what} retries: {p.get('error') or ''}")
    if kind == "turn_cancelled":
        return "Worker turn cancelled", ""
    if kind == "attention":
        return "Needs you", p.get("reason") or ""
    if kind == "assumption":
        # The number, not the text. Every surface that shows this timeline also lists
        # the assumptions themselves, numbered the same way, so the text here was the
        # same paragraph twice on one page. `n` is written by
        # `ProjectStore.add_assumption`; `build_timeline` fills it in for rows written
        # before it was.
        n = p.get("n")
        return (f"Assumption #{n} recorded" if n else "Assumption recorded"), ""
    if kind == "question_asked":
        # The question is NOT reproduced here. It has a record of its own that holds the
        # answer beside it, and a reader following a timeline wants to know a question
        # was asked and be able to go and read it — not to read a paragraph inline, then
        # its answer again two lines down as a message. `_ref` is the way there; the text
        # is the fallback for an event that somehow carries no question id.
        if _neo_question_id(p) is not None:
            return "Worker asked a question", ""
        return "Worker asked a question", str(p.get("question") or "")
    # Both "answered" kinds are debug (see DEBUG_KINDS): the answer is the message
    # queued in the same breath, and that message is the timeline entry worth reading.
    # They still render, with no detail, when debug entries are asked for.
    if kind == "neo_answered":
        return "Neo answered the worker", ""
    if kind == "neo_dispatched":
        return ("Neo filed a pre-approved cleanup",
                p.get("cleanup_wo_id") or "")
    if kind == "escalation_answered":
        return "You answered the worker", ""  # same as neo_answered, above
    if kind == "reviewed":
        verb = "accepted" if p.get("accepted") else "rejected"
        count = p.get("count")
        return f"Assumptions {verb}", f"{count} assumption(s)" if count else ""
    if kind == "learning_captured":
        return "Learning captured", p.get("topic") or ""
    if kind == "gate_requested":
        # The seat, when a subagent tripped the gate. `add_approval` only writes the key
        # when there is one, so the unqualified line is still what a plain worker gets.
        who = f" (seat `{p['agent_type']}`)" if p.get("agent_type") else ""
        return (f"Asked permission to {p.get('kind') or 'act'}{who}",
                p.get("command") or "")
    if kind == "gate_decided":
        verb = "Approved" if p.get("decision") == "approved" else "Denied"
        return (f"{verb} by {p.get('by') or '?'}: {p.get('kind') or 'gate'}",
                p.get("reason") or "")
    if kind == "gate_dismissed":
        # Deliberately not phrased as a verdict on the worker. Nothing was authorised and
        # nothing was refused: the OS's own recogniser misfired, and the record has to say
        # so plainly or it reads later as a release someone waved through.
        return (f"Not a privileged action — the `{p.get('kind') or 'gate'}` gate matched "
                f"this by mistake ({p.get('by') or '?'})",
                p.get("reason") or "")
    if kind == "gate_superseded":
        # Neither a verdict nor a dismissal: the question stopped being answerable. Said
        # plainly so the record cannot be read as "someone approved this quietly".
        return (f"Gate request closed unanswered — the `{p.get('kind') or 'gate'}` "
                f"question no longer applies", p.get("reason") or "")
    if kind == "gate_escalated":
        return "Gate approval escalated to you", p.get("reason") or ""
    if kind == "gate_opened":
        # The moment the privileged command actually ran — the most audit-relevant
        # entry a work order can have, so it is never debug. Unless it was never
        # privileged, in which case calling it "the approved command" would write the
        # exact falsehood the dismissed verdict exists to keep out of the record.
        if p.get("clearance") == "dismissed":
            return (f"Ran the command the `{p.get('kind') or 'gate'}` gate had matched "
                    f"by mistake", "no privileged action was authorised")
        return (f"Ran the approved {p.get('kind') or 'command'}",
                f"use {p.get('use')} of {p.get('of')}")
    # The validation loop. Five kinds rather than one with an outcome in the payload,
    # because the four are the whole story a reader wants at a glance — and each gets a
    # LABEL of its own here. `event_level` returns "signal" for anything it does not
    # know, so these arriving unclassified would look fine on the timeline while
    # rendering as a bare kind and a JSON blob.
    if kind == "validation_submitted":
        rnd = p.get("round")
        return ("Submitted for validation",
                f"round {rnd}" if rnd else "")
    if kind == "validation_passed":
        return "Validation passed", p.get("reason") or ""
    if kind == "validation_rejected":
        # The reason IS the ask the worker has to answer, so unlike the "answered"
        # kinds above it is shown here: nothing else in the timeline carries it.
        return "Validation rejected — sent back", p.get("reason") or ""
    if kind == "validation_escalated":
        return ("Validation gave up — over to you", p.get("reason") or "")
    if kind == "validation_failed":
        # A FIFTH kind, and the one most easily misread: nothing judged the work here.
        # A reader who takes this for a rejection goes looking for something to fix that
        # nobody ever asked for, so the two causes get two different sentences.
        if p.get("cause") == "no_validator":
            return ("Validation skipped — no validator was configured",
                    p.get("reason") or "")
        attempt = p.get("attempt")
        return ("Validation could not be run — the reviewer was unreachable",
                f"attempt {attempt}: {p.get('error') or ''}" if attempt
                else (p.get("error") or ""))
    if kind == "deferral_submitted":
        # The worker deciding something is not its job is a scope decision, and the
        # timeline is the only place the user ever sees it: the item itself lands on the
        # backlog, where nothing points back at this work order's story.
        return ("Deferred something out of scope", p.get("title") or "")
    if kind == "finished":
        return "Finished", p.get("summary") or ""
    if kind == "marked_done":
        # The user closed it, not the worker — worth telling apart on the record.
        return "Marked done by you", (
            "the worker's turn was stopped" if p.get("session_stopped") else "")
    if kind == "hidden":
        return ("Hidden" if p.get("hidden") else "Unhidden"), ""
    if kind == "invariant":
        detail = p.get("detail") or ""
        if p.get("repaired"):
            return "OS self-check repaired this", f"{detail} → {p.get('repair') or ''}"
        return "OS self-check failed", detail
    # Unclassified or debug: show the kind and its raw payload.
    return kind, json.dumps(p, sort_keys=True) if p else ""


def _message_label(m: dict[str, Any]) -> str:
    """Who is speaking, from the message's own `source`.

    Every inbound message used to read "You → worker", which was false for the commonest
    one of all: Neo's answer to a worker's question is queued by the daemon with
    `source="neo"`. The timeline said the user had answered, and the separate
    "Neo answered the worker" event beside it — now debug — was the only thing that said
    otherwise. One line, correctly attributed, replaces both.
    """
    if m.get("direction") != "user_to_agent":
        return "Worker → you"
    return "Neo → worker" if m.get("source") == "neo" else "You → worker"


def build_timeline(wo: dict[str, Any], events: list[dict[str, Any]],
                   messages: list[dict[str, Any]],
                   *, include_debug: bool = False) -> list[dict[str, Any]]:
    """Merge events and conversation into time-ordered, human-readable entries.

    Each entry: {ts, level, kind, label, detail, ref}. `ref` is None for most entries and
    otherwise names a record this entry points at instead of reproducing (see `_ref`).
    Debug entries are omitted unless `include_debug`.

    This function stays pure — it never opens a store.
    """
    entries: list[dict[str, Any]] = []
    seen_assumptions = 0
    for e in events:
        kind = e.get("kind", "")
        payload = _payload(e)
        if kind == "assumption":
            # An assumption's number is its position among this work order's
            # assumptions. `add_assumption` writes it into the payload; rows written
            # before it did are numbered here, by the same rule and therefore to the
            # same numbers — both count in `ts` order, which is the order the
            # assumptions table is read back in.
            seen_assumptions += 1
            payload = {**payload, "n": payload.get("n") or seen_assumptions}
        level = event_level(kind)
        if level == "debug" and not include_debug:
            continue
        label, detail = _describe(kind, payload)
        entries.append({"ts": e.get("ts") or 0.0, "level": level, "kind": kind,
                        "label": label, "detail": detail, "ref": _ref(kind, payload)})
    for m in messages:
        entries.append({
            "ts": m.get("ts") or 0.0, "level": "signal", "kind": "message",
            "label": _message_label(m), "detail": m.get("content") or "", "ref": None,
        })
    entries.sort(key=lambda e: e["ts"])
    return entries


def count_debug(events: list[dict[str, Any]]) -> int:
    return sum(1 for e in events if event_level(e.get("kind", "")) == "debug")
