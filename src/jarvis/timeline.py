"""Work order timeline: the story, not the plumbing.

`wo_events` records everything that happens to a work order, which mixes two very
different audiences. The user wants the story — what was asked, what the worker
decided, what came back. The rest (message delivery bookkeeping, Claude Code session
hooks, turn boundaries, session binding) exists to debug the circuitry.

`build_timeline` merges events with the actual conversation and renders each entry as
prose. Debug entries are held back unless explicitly requested.
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
})

STATUS_LABEL = {
    "pending": "Queued",
    "dispatching": "Dispatching worker",
    "running": "Running",
    "waiting_input": "Waiting on you",
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


def _question(p: dict[str, Any], questions: dict[int, str]) -> str:
    """What the worker asked, from the payload or — for older events — from Neo's DB."""
    stored = p.get("question")
    if stored:
        return str(stored)
    qid = p.get("neo_question_id")
    try:
        return questions.get(int(qid), "") if qid is not None else ""
    except (TypeError, ValueError):
        return ""


def _describe(kind: str, p: dict[str, Any], wo: dict[str, Any],
              questions: dict[int, str]) -> tuple[str, str]:
    """(label, detail) in plain language for one event.

    `questions` back-fills question text for events that predate it being stored in
    the payload; see `build_timeline`.
    """
    if kind == "created":
        about = [wo.get("title") or "", wo.get("description") or ""]
        return "Work order created", "\n".join(x for x in about if x)
    if kind == "status":
        status = p.get("status", "")
        return STATUS_LABEL.get(status, status or "Status changed"), ""
    if kind == "dispatched":
        return "Worker dispatched", p.get("worktree") or ""
    if kind == "turn_failed":
        return "Worker turn failed", (p.get("error") or "")[:200]
    # The usage-limit trio. Deliberately NOT filed under "Worker turn failed": nothing
    # about the work went wrong, the turn was refused before it ran, and the OS puts
    # itself right. What the reader needs is the pause, the resume, and — only if it
    # comes to that — the point at which the OS gave up and it became their problem.
    if kind == "rate_limited":
        when = _clock(p.get("reset_at"))
        return ("Paused — Claude usage limit",
                f"{p.get('error') or ''}"
                + (f" · resuming after {when}" if when else ""))
    if kind == "rate_limit_retry":
        attempt = p.get("attempt")
        return ("Resumed after the usage limit",
                f"attempt {attempt}" if attempt else "")
    if kind == "rate_limit_exhausted":
        return ("Still refused after retrying",
                f"{p.get('attempts')} usage-limit retries: {p.get('error') or ''}")
    if kind == "turn_cancelled":
        return "Worker turn cancelled", ""
    if kind == "attention":
        return "Needs you", p.get("reason") or ""
    if kind == "assumption":
        return "Assumption recorded", p.get("content") or ""
    if kind == "question_asked":
        return "Worker asked a question", _question(p, questions)
    # The two "answered" kinds carry NO detail on purpose. Both writers queue the
    # answer as a message in the same breath, and messages render verbatim, so the
    # text is already the very next line of the timeline. Attaching it here too would
    # print every Neo answer twice.
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


def build_timeline(wo: dict[str, Any], events: list[dict[str, Any]],
                   messages: list[dict[str, Any]],
                   *, include_debug: bool = False,
                   questions: dict[int, str] | None = None) -> list[dict[str, Any]]:
    """Merge events and conversation into time-ordered, human-readable entries.

    Each entry: {ts, level, kind, label, detail}. Debug entries are omitted unless
    `include_debug`.

    `questions` is {neo_question_id: question}, used only to fill in `question_asked`
    events written before the text was stored in the payload. Callers get it from
    `ops.neo_question_texts`; omitting it costs nothing but those older details. This
    function stays pure — it never opens a store itself.
    """
    questions = questions or {}
    entries: list[dict[str, Any]] = []
    for e in events:
        kind = e.get("kind", "")
        level = event_level(kind)
        if level == "debug" and not include_debug:
            continue
        label, detail = _describe(kind, _payload(e), wo, questions)
        entries.append({"ts": e.get("ts") or 0.0, "level": level, "kind": kind,
                        "label": label, "detail": detail})
    for m in messages:
        to_worker = m.get("direction") == "user_to_agent"
        entries.append({
            "ts": m.get("ts") or 0.0, "level": "signal", "kind": "message",
            "label": "You → worker" if to_worker else "Worker → you",
            "detail": m.get("content") or "",
        })
    entries.sort(key=lambda e: e["ts"])
    return entries


def count_debug(events: list[dict[str, Any]]) -> int:
    return sum(1 for e in events if event_level(e.get("kind", "")) == "debug")
