"""A work order's record, in two readings.

`build_conversation` is **what was said** — the exchange, in order, whoever spoke.
`build_timeline` is **what happened** — the sequence of events, each one pointing at
the record that holds the detail rather than reproducing it
(docs/superpowers/specs/2026-08-23-the-work-order-record.md §1, §3).

The two are not the same list rendered twice. Neither is complete without the other,
and each has exactly one thing the other must not carry:

- The worker's question to Neo is a `wo_events` row, never a message. Read the
  messages alone and Neo's answer arrives with nothing above it to answer, which is
  what the conversation showed until `build_conversation` existed.
- A message body belongs to the conversation. Merged into the timeline as well — as
  every message was — the timeline becomes a second, worse copy of it.

See docs/superpowers/specs/2026-08-24-the-conversation-owns-what-was-said.md.

`wo_events` also mixes two audiences. The user wants the story; the rest (message
delivery bookkeeping, Claude Code session hooks, turn boundaries, session binding)
exists to debug the circuitry, and is held back unless explicitly requested.
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
    # Same moment as the message carrying the answer, so the message is the entry — §5.
    "neo_answered",
    "escalation_answered",
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


#: The four kinds carrying one cost alarm's life, frozen in §1 of
#: docs/superpowers/specs/2026-08-31-the-supervisor.md. Spelled out here rather than
#: imported from `project_store`: this module is a leaf and opens no store.
ALARM_KINDS = frozenset({"cost_alarm", "alarm_reviewed", "alarm_escalated",
                         "alarm_advice"})


def _ref(kind: str, p: dict[str, Any]) -> dict[str, Any] | None:
    """The record this entry points at, or None. Surface-neutral by design — §3.

    Never a URL and never an anchor: the dashboard resolves a `neo_question` to
    `/neo/question/<id>`, an `alarm` to `/alarms/<project>/<al-id>` and a `message` to
    the conversation turn `build_conversation` gave the same id, while `jarvis wo show`
    prints the label and the CLI's own commands reach the same three records.
    """
    if kind == "question_asked":
        qid = _neo_question_id(p)
        if qid is not None:
            return {"kind": "neo_question", "id": qid, "label": f"question #{qid}"}
    if kind in ALARM_KINDS:
        # The alarm, even on the two kinds that also carry a `neo_question_id`: a ref is
        # singular, and the alarm's own page is where the verdict, Neo's question and the
        # review control all are (§4, §5).
        #
        # A `cost_alarm` row written before §1 has no `alarm_id` and so gets no ref — a
        # pointer that cannot resolve is not a saving, corollary 1 of §1.
        alarm_id = p.get("alarm_id")
        if alarm_id:
            return {"kind": "alarm", "id": str(alarm_id), "label": f"alarm {alarm_id}"}
    return None


def _describe(kind: str, p: dict[str, Any]) -> tuple[str, str]:
    """(label, detail) in plain language for one event, from its payload alone."""
    if kind == "created":
        return "Work order created", ""  # the title and description are already above
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
        if p.get("reason") == "auth":
            # The only one of the three that names what the READER has to do: the other
            # two resume on a clock, this one resumes on them.
            return ("Paused — Claude Code sign-in expired",
                    f"{p.get('error') or ''} · resuming when you sign in again")
        if p.get("reason") == "transient":
            status = f" {p['status']}" if p.get("status") else ""
            return (f"Paused — Claude API error{status}",
                    f"{p.get('error') or ''} · retrying shortly")
        when = _clock(p.get("reset_at"))
        return ("Paused — Claude usage limit",
                f"{p.get('error') or ''}"
                + (f" · resuming after {when}" if when else ""))
    if kind in ("turn_resumed", "rate_limit_retry"):
        if p.get("reason") == "auth":
            return "Resumed — you signed back in", ""
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
    if kind == "cost_alarm":
        # Its own line rather than folded into "Needs you": only the FIRST alarm of a
        # turn raises the flag, so the rest exist only here, and this is the row that
        # says a turn was already known to be expensive while it was still running.
        return "Costing money while it runs", p.get("reason") or ""
    # The supervisor's three. Same trap as the validation kinds below, already paid for
    # once here: `event_level` calls an unknown kind "signal", so a kind with no branch
    # renders as its own name beside a JSON blob and looks fine on the page.
    if kind == "alarm_reviewed":
        # The verdict's REASON, which is for the record. `note` is speech addressed to
        # the user and belongs to the conversation, which renders it from this same
        # payload — a fallback here would print it twice on one page (§4).
        verb = ("cleared this alarm" if p.get("verdict") == "ack"
                else "could not settle this alarm")
        return f"The supervisor {verb}", p.get("reason") or ""
    if kind == "alarm_escalated":
        qid = _neo_question_id(p)
        # The question number as text, not as the ref: this entry spends its one pointer
        # on the alarm, whose page quotes the question beside the verdict anyway.
        return "Alarm handed to Neo", f"question #{qid}" if qid is not None else ""
    if kind == "alarm_advice":
        return "Neo advised the supervisor", ""  # the answer is in the conversation
    if kind == "assumption":
        n = p.get("n")  # the number, not the text — §4
        return (f"Assumption #{n} recorded" if n else "Assumption recorded"), ""
    if kind == "question_asked":
        # Never the text, with or without an id: the conversation renders the ask from
        # this same payload, so a fallback here would print it twice on one page. `_ref`
        # is the way to the question's own record, where the ANSWER is beside it.
        return "Worker asked a question", ""
    # Debug (see DEBUG_KINDS); they still render, with no detail, when asked for.
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
    # The conflict-healing loop, on the timeline so that a give-up arrives with the
    # record of what was already tried (spec §6 of
    # docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md).
    if kind == "pr_conflict_nudged":
        of = p.get("of")
        return ("Merge conflict — asked the worker to resolve it",
                f"attempt {p.get('attempt')} of {of}" if of else "")
    if kind == "pr_conflict_cleared":
        return "Merge conflict resolved", ""
    if kind == "pr_conflict_unresolved":
        return ("Merge conflict the worker could not resolve — over to you",
                f"{p.get('attempts')} attempts")
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


#: Message sources nobody decided: no user typed them, and no delegate chose to send
#: them on the user's behalf either. So far exactly one — the automatic merge-conflict
#: nudge a poll writes on seeing GitHub report CONFLICTING (§6 of
#: docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md).
UNAUTHORED_SOURCES = frozenset({"pr-conflict"})


def _message_label(m: dict[str, Any]) -> str:
    """Who is speaking, from the message's own `source` — §5.

    The conversation's label: two parties and an arrow, because the reader is looking
    at the words. The timeline wants a sentence instead; see `_message_event_label`.
    """
    if m.get("direction") != "user_to_agent":
        return "worker → you"
    if m.get("source") == "neo":
        return "neo → worker"
    if m.get("source") in UNAUTHORED_SOURCES:
        return "jarvis → worker"
    return "you → worker"


def _message_event_label(m: dict[str, Any]) -> str:
    """What happened, for the timeline — the same `source` rule, worded as an event.

    A timeline entry is a sentence about a moment, not a speaker tag: the body it used
    to carry is a click away in the conversation, and "Neo → worker" over an empty
    detail says less than "Neo answered the worker" does.
    """
    if m.get("direction") != "user_to_agent":
        return "Worker replied"
    if m.get("source") == "neo":
        # `source="neo"` is written in exactly one place (`daemon._neo_drain`), and only
        # for the message carrying an answer — so this cannot mislabel anything else.
        return "Neo answered the worker"
    if m.get("source") in UNAUTHORED_SOURCES:
        return "Jarvis messaged the worker"
    return "You messaged the worker"


def _message_ref(m: dict[str, Any]) -> dict[str, Any] | None:
    """The conversation turn this entry points at, or None if it has no id.

    Corollary 1 of §1 in reverse: an id that cannot be resolved is not a pointer, so a
    message the store never gave an id keeps its text on the timeline instead.
    """
    mid = m.get("id")
    if mid is None:
        return None
    return {"kind": "message", "id": mid, "label": "in the conversation"}


def build_conversation(events: list[dict[str, Any]],
                       messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Everything that was SAID about this work order, in the order it was said.

    Messages are only half of it. The worker's question to Neo is a `question_asked`
    event — `ops.neo_ask` writes the text into its payload precisely so that a record
    built from the project store alone can show what was asked — and without it the
    conversation opens on Neo's answer to a question that appears nowhere.

    The supervisor's note and Neo's advice on a cost alarm arrive the same way and for
    the same reason (§4). A VERDICT is not speech and stays on the timeline; a note
    addressed to the user, and the advice that produced it, are.

    Each turn: {ts, kind, who, content, anchor, ref, msg_id, source, status, inbound}.
    `anchor` is the id every surface gives the turn, so that a timeline entry's
    `{"kind": "message", "id": …}` ref resolves to the words. Stays pure — it never
    opens a store, and in particular never opens Neo's.
    """
    turns: list[dict[str, Any]] = []
    for e in events:
        kind = e.get("kind")
        if kind not in ("question_asked", "alarm_reviewed", "alarm_advice"):
            continue
        p = _payload(e)
        # `note` is empty by contract when the supervisor escalates, so the same guard
        # that keeps a text-less question out also keeps an escalation's verdict out.
        # An empty bubble is worse than no bubble; the timeline still has the event.
        text = str(p.get({"question_asked": "question", "alarm_reviewed": "note",
                          "alarm_advice": "answer"}[kind]) or "")
        if not text:
            continue
        if kind == "question_asked":
            qid = _neo_question_id(p)
            turns.append({
                "ts": e.get("ts") or 0.0, "kind": "question", "who": "worker → Neo",
                "content": text, "anchor": f"q-{qid}" if qid is not None else "",
                "ref": _ref(kind, p), "msg_id": None,
                "source": "neo", "status": "", "inbound": False,
            })
            continue
        turns.append({
            "ts": e.get("ts") or 0.0,
            "kind": "note" if kind == "alarm_reviewed" else "advice",
            "who": ("supervisor → you" if kind == "alarm_reviewed"
                    else "neo → supervisor"),
            "content": text, "anchor": "", "ref": _ref(kind, p), "msg_id": None,
            "source": "", "status": "", "inbound": False,
        })
    for m in messages:
        mid = m.get("id")
        turns.append({
            "ts": m.get("ts") or 0.0, "kind": "message", "who": _message_label(m),
            "content": m.get("content") or "",
            "anchor": f"msg-{mid}" if mid is not None else "",
            "ref": None, "msg_id": mid, "source": m.get("source") or "",
            "status": m.get("status") or "",
            "inbound": m.get("direction") == "user_to_agent",
        })
    turns.sort(key=lambda t: t["ts"])
    return turns


def build_timeline(wo: dict[str, Any], events: list[dict[str, Any]],
                   messages: list[dict[str, Any]],
                   *, include_debug: bool = False) -> list[dict[str, Any]]:
    """Merge events and messages into time-ordered entries saying WHAT HAPPENED.

    Each entry: {ts, level, kind, label, detail, ref}. Debug entries are omitted unless
    `include_debug`. Stays pure — it never opens a store.

    Messages are here as moments, not as text: they are what makes the timeline a
    sequence rather than a list of lifecycle changes, and their words are the
    conversation's, one `ref` away.
    """
    entries: list[dict[str, Any]] = []
    seen_assumptions = 0
    for e in events:
        kind = e.get("kind", "")
        payload = _payload(e)
        if kind == "assumption":
            # Rows written before `add_assumption` stored `n` are numbered here, by the
            # same rule and therefore to the same numbers — §4.
            seen_assumptions += 1
            payload = {**payload, "n": payload.get("n") or seen_assumptions}
        level = event_level(kind)
        if level == "debug" and not include_debug:
            continue
        label, detail = _describe(kind, payload)
        entries.append({"ts": e.get("ts") or 0.0, "level": level, "kind": kind,
                        "label": label, "detail": detail, "ref": _ref(kind, payload)})
    for m in messages:
        ref = _message_ref(m)
        entries.append({
            "ts": m.get("ts") or 0.0, "level": "signal", "kind": "message",
            "label": _message_event_label(m),
            "detail": "" if ref else (m.get("content") or ""), "ref": ref,
        })
    entries.sort(key=lambda e: e["ts"])
    return entries


def count_debug(events: list[dict[str, Any]]) -> int:
    return sum(1 for e in events if event_level(e.get("kind", "")) == "debug")
