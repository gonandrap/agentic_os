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


def _describe(kind: str, p: dict[str, Any], wo: dict[str, Any]) -> tuple[str, str]:
    """(label, detail) in plain language for one event."""
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
    if kind == "turn_cancelled":
        return "Worker turn cancelled", ""
    if kind == "attention":
        return "Needs you", p.get("reason") or ""
    if kind == "assumption":
        return "Assumption recorded", p.get("content") or ""
    if kind == "question_asked":
        return "Worker asked a question", p.get("question") or ""
    if kind == "neo_answered":
        return "Neo answered the worker", p.get("answer") or ""
    if kind == "neo_dispatched":
        return ("Neo filed a pre-approved cleanup",
                p.get("cleanup_wo_id") or "")
    if kind == "escalation_answered":
        return "You answered the worker", p.get("answer") or ""
    if kind == "reviewed":
        verb = "accepted" if p.get("accepted") else "rejected"
        count = p.get("count")
        return f"Assumptions {verb}", f"{count} assumption(s)" if count else ""
    if kind == "learning_captured":
        return "Learning captured", p.get("topic") or ""
    if kind == "gate_requested":
        return (f"Asked permission to {p.get('kind') or 'act'}",
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
                   *, include_debug: bool = False) -> list[dict[str, Any]]:
    """Merge events and conversation into time-ordered, human-readable entries.

    Each entry: {ts, level, kind, label, detail}. Debug entries are omitted unless
    `include_debug`.
    """
    entries: list[dict[str, Any]] = []
    for e in events:
        kind = e.get("kind", "")
        level = event_level(kind)
        if level == "debug" and not include_debug:
            continue
        label, detail = _describe(kind, _payload(e), wo)
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
