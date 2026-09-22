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
    # THE SWEEP LOOKING, which is not the same event as the sweep FINDING something.
    # `health_finding` is a signal and stays one; this fires on every sweep including
    # the clear ones, which on a long-running order is dozens a day. §4 of
    # docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md keeps the
    # ledger of looking out of the surfaces for exactly this reason; the same argument
    # applies to the default timeline.
    "health_reviewed",
})

#: `background.EVENT`, spelled out here for the reason `SUPERVISOR_SOURCE` is: this
#: module is a leaf and opens nothing. A test pins the two equal. Spec
#: docs/superpowers/specs/2026-09-22-a-dead-background-job-is-not-a-live-one.md.
BACKGROUND_ORPHANED = "background_orphaned"
BACKGROUND_NUDGED = "background_nudged"
BACKGROUND_UNRESOLVED = "background_unresolved"

#: What the conversation prints ABOVE a message whose turn left a background job behind
#: — `build_conversation`'s `void` field, spec §3. It retracts the promise without
#: touching the words: the message is what the worker said, and this is what the OS
#: knows about it. Free of any elapsed time, so it reads the same the hour it is written
#: and two days later.
VOID_MESSAGE = ("⚠ VOID — the turn that wrote this ended while {jobs} was still "
                "running, and a turn is one process: the job died with it. Nothing this "
                "message describes as running has run since.")

#: How much of a command survives into a job's label — `background.COMMAND_CHARS`, and
#: pinned equal to it by the same test.
JOB_COMMAND_CHARS = 80

STATUS_LABEL = {
    "pending": "Queued",
    "dispatching": "Dispatching worker",
    "running": "Running",
    # NOT "Waiting on you". A manager between its feature's messages asks nothing of
    # anybody, and eleven hours of it reading otherwise is GitHub issue #264.
    "idle": "Idle — waiting for its feature",
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


#: The kinds carrying one alarm's life, frozen in §1 of
#: docs/superpowers/specs/2026-08-31-the-supervisor.md and grown by §1 of
#: docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md. Spelled out here
#: rather than imported from `project_store`: this module is a leaf and opens no store.
#: A test asserts equality with `project_store.ALARM_EVENT_KINDS` — one growing without
#: the other stops `_ref` resolving the new kinds, with no error anywhere.
ALARM_KINDS = frozenset({"cost_alarm", "alarm_reviewed", "alarm_escalated",
                         "alarm_advice", "health_finding", "health_reviewed",
                         "remedy_proposed", "remedy_applied", "remedy_refused"})

#: `project_store.NO_TURN`, duplicated for `ALARM_KINDS`' reason — a leaf may not import
#: a store — and pinned equal to it by the same test. A `cost_alarm` carrying it judged
#: no turn: see `_describe`.
NO_TURN = -1


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


#: Each ingredient of the prompt prefix in the words a reader can act on, since the
#: payload's keys are the names `hooks.PREFIX_INGREDIENTS` uses and those name the code.
PREFIX_INGREDIENT_LABEL = {
    "cli_version": "the Claude Code version",
    "git_briefing": "the git briefing Jarvis adds to every turn",
    "worker_settings": "the worker's settings file",
    "memory": "a CLAUDE.md the worker loads",
}


def _prefix_changes(p: dict[str, Any]) -> str:
    """"the Claude Code version (2.1.271 -> 2.1.272) changed", from the drift payload.

    A version moved is quoted and a digest moved is not: the before/after of a hash is
    the question restated, while the before/after of a version is the answer.
    """
    changed = p.get("changed") or []
    before, after = p.get("before") or {}, p.get("after") or {}
    parts = []
    for name in changed:
        label = PREFIX_INGREDIENT_LABEL.get(name) or str(name)
        was, now = before.get(name), after.get(name)
        if name == "cli_version" and was and now:
            label += f" ({was} → {now})"
        parts.append(label)
    if not parts:
        return "Something in the prompt prefix changed"
    joined = (parts[0] if len(parts) == 1
              else ", ".join(parts[:-1]) + " and " + parts[-1])
    return joined[0].upper() + joined[1:] + " changed since the last turn"


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
    # A SIGNAL AND NOT PLUMBING, unlike `turn_started`/`turn_ended` beside it. The OS
    # spent a model call on the conversation rather than on the work, and it dropped
    # detail the worker had: that is a thing that happened TO this work order, and the
    # pinned self-healing learning asks for it to be provable rather than asserted.
    if kind == "compacting":
        return "Compacting the conversation", p.get("reason") or ""
    if kind == "compacted":
        # `ok` is absent on the hook's own event (`hooks.note_compaction`), which only
        # ever fires for a compaction that already happened — so missing means true.
        if p.get("ok") is False:
            # The conversation is INTACT and the next prompt still pays the re-write.
            # Saying "compacted" here would put a saving on the record that the bill
            # will not show.
            return "Compaction failed", str(p.get("error") or "")
        before, after = p.get("before"), p.get("after")
        if isinstance(before, int) and isinstance(after, int) and before:
            return ("Conversation compacted",
                    f"{before:,} tokens summarised to {after:,}")
        return "Conversation compacted", ""
    if kind == "attention":
        return "Needs you", p.get("reason") or ""
    if kind == "acknowledged":
        # Reached the user as an escaped JSON blob until issue 573. WHO acked is part of
        # the claim, not decoration: rows written before `ProjectStore.ack_attention`
        # recorded it carry no `by`, and those are attributed to nobody rather than to
        # the user — some of them were the OS acking on their behalf, which is the bug.
        who = p.get("by")
        return (f"Acknowledged by {who}" if who else "Acknowledged",
                "\n".join(str(b) for b in (p.get("blockers") or [])))
    if kind == "cost_alarm":
        # Its own line rather than folded into "Needs you": only the FIRST alarm of a
        # turn raises the flag, so the rest exist only here, and this is the row that
        # says a turn was already known to be expensive while it was still running.
        #
        # …EXCEPT for the aggregate kinds, which judge no turn (`seq == NO_TURN`) and
        # land on an order that has already settled. "while it runs" would be a claim
        # about a running turn on a row where none is running, which is the class of
        # false statement `inspection._spend_so_far` exists to prevent on the other
        # surface.
        if p.get("seq") == NO_TURN:
            return "A standing cost finding on this project", p.get("reason") or ""
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
    # The health sweep's two and the remedy's three (§6 of
    # docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md). A PROBE ID
    # and a REMEDY ID are database values, so they ride in the detail rather than in the
    # label: this module is a leaf and cannot reach the catalog that holds a probe's
    # title, which `/alarms` renders instead.
    if kind == "health_finding":
        return "The supervisor found something wrong", p.get("reason") or ""
    if kind == "health_reviewed":
        found = p.get("findings") or 0
        return ("The supervisor checked this over",
                f"{p.get('trigger') or 'swept'} · "
                + (f"{found} finding(s)" if found else "nothing found"))
    if kind == "remedy_proposed":
        # PERMISSION ASKED, NOTHING DONE, and the label has to carry that alone: this
        # entry and `remedy_applied` sit next to each other on a settled order, and a
        # reader who takes the first for the act goes looking for an effect only the
        # second one had.
        return ("The supervisor asked permission to act",
                f"{p.get('remedy') or 'a remedy'}: {p.get('argument') or ''}")
    if kind == "remedy_applied":
        return ("The supervisor acted",
                f"{p.get('remedy') or 'a remedy'}: {p.get('result') or ''}")
    if kind == "remedy_refused":
        # Two payload shapes arrive here — the catalog refusing to file the proposal at
        # all, and a reviewer denying it — and `by` is what tells them apart (§5).
        by = f" by {p['by']}" if p.get("by") else ""
        return (f"The {p.get('remedy') or 'proposed'} remedy was refused{by}",
                p.get("reason") or "")
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
    # Auto-review's four, and they belong directly under `reviewed` because they are the
    # same act by a different hand. THE VERB SAYS WHO: "accepted" above is the user, so
    # every line here names the OS, on the surface that narrates what happened to a work
    # order. `ops.autoreview_state` renders only the NEWEST of these as its one-line
    # summary, so an order where the OS decided two assumptions and left a third is
    # readable nowhere else — and a kind with no branch here falls through to the generic
    # renderer, which `event_level` labels "signal": it looks fine on the page and tells
    # the reader nothing (kn-3f133363).
    if kind == "autoreview_asked":
        return (f"Asked Neo to rule on assumption #{p.get('n')}",
                f"question {p['neo_question_id']}" if p.get("neo_question_id") else "")
    if kind == "autoreview_accepted":
        # The model is in the label, not the detail: "the OS accepted it" and "THIS model
        # accepted it" are different claims, and the record must never make the weaker one
        # look like the user's. Same words as `ops.assumption_decider` for one reason.
        model = p.get("model") or "model not recorded"
        return (f"Assumption #{p.get('n')} accepted by the OS "
                f"(Neo, {model}) — not by you",
                p.get("reason") or "no reason recorded")
    if kind == "autoreview_escalated":
        # `dropped` means the OS had a ruling and threw it away because the state moved
        # under it (spec §5.1). Worth saying: "left with you" alone would read as Neo
        # declining, and a reader deciding whether to trust the feature needs the two
        # apart.
        why = " — the OS dropped its ruling when the work order changed" if p.get(
            "dropped") else ""
        return (f"Assumption #{p.get('n')} left with you{why}",
                p.get("reason") or "no reason recorded")
    if kind == "autoreview_held":
        return (f"The OS would not decide assumption #{p.get('n')}",
                p.get("reason") or "no reason recorded")
    if kind == "learning_captured":
        return "Learning captured", p.get("topic") or ""
    if kind == "prefix_drift":
        # Labelled an early warning on the surface too, and not only in the code that
        # computes it: `jarvis doctor`'s INV-PREFIX-DRIFT reads what the API actually
        # billed, this reads what went into the prompt, and a reader who meets this line
        # first must not take it for the measurement (hooks.note_prefix).
        return ("Prompt prefix moved — early warning, not a measurement",
                f"{_prefix_changes(p)}. This turn's cached prefix was probably re-written, "
                f"so the conversation so far was re-sent; `jarvis doctor` reports what it "
                f"actually cost.")
    if kind == "gate_requested":
        # The seat, when a subagent tripped the gate. `add_approval` only writes the key
        # when there is one, so the unqualified line is still what a plain worker gets.
        who = f" (seat `{p['agent_type']}`)" if p.get("agent_type") else ""
        # A held request was RECORDED, not asked: the worker ran the command and no
        # reviewer has been shown anything. "Asked permission" would credit it with a
        # review that has not started — see gates.AWAITING_CASE.
        # A CONTEST is not a request for permission, and a record that says it is would
        # be read later as an attempt at the action — see spec 2026-09-12 §1.
        verb = ("Contested the `%s` gate match — says this performs no privileged action"
                % (p.get("kind") or "gate") if p.get("contested")
                else "Ran a gated command — request recorded, awaiting its case"
                if p.get("held") else f"Asked permission to {p.get('kind') or 'act'}")
        return (f"{verb}{who}", p.get("command") or "")
    if kind == "gate_amended":
        # Not a second request — the same one, better argued. Said that way because a
        # reader counting attempts at a privileged action must not count this as one.
        return (f"Made the case for the pending `{p.get('kind') or 'gate'}` request",
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
    if kind == "gate_abandoned":
        # Not a denial. Nobody reviewed it, so nobody refused it — and on the evidence
        # this is usually the worker routing around a recogniser false positive, which
        # is why `abandoned_count` exists. Spec 2026-09-12 §4.
        return (f"Gate request abandoned — no case was ever made for the "
                f"`{p.get('kind') or 'gate'}` block, and nothing was decided",
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
    # The validation loop. Seven kinds rather than one with an outcome in the payload,
    # because each is the whole story a reader wants at a glance — and each gets a
    # LABEL of its own here. `event_level` returns "signal" for anything it does not
    # know, so these arriving unclassified would look fine on the timeline while
    # rendering as a bare kind and a JSON blob.
    if kind == "validation_submitted":
        rnd = p.get("round")
        return ("Submitted for validation",
                f"round {rnd}" if rnd else "")
    if kind == "validation_forced":
        # A SIXTH kind, and the only one no worker produced — `jarvis validation force`,
        # a person re-opening the round. It says so in the LABEL rather than only in the
        # detail: the whole point of the command is that a forced re-judgement must not
        # read afterwards like a worker re-delivering, and the timeline is where that
        # reading happens. The reason is the ask the operator answered, so like
        # `validation_rejected` it is shown rather than folded away.
        #
        # AND THE OS FORCES ONE TOO now that a moved head re-judges itself (spec
        # docs/superpowers/specs/2026-09-19-a-moved-head-re-judges-itself.md §5). The
        # label separates them for the reason it exists in the first place: a round no
        # person asked for must not read afterwards as one they did.
        rnd, was = p.get("round"), p.get("was")
        who = "by the OS" if str(p.get("by") or "") == "os" else "by hand"
        return (f"Validation forced {who} — round {rnd}" if rnd
                else f"Validation forced {who}",
                f"{p.get('reason') or ''}"
                + (f" (was {was})" if was else ""))
    if kind == "validation_rejudge_declined":
        # The one moved head the OS will NOT re-judge: the round it would open is the
        # last one, and that one is the user's (same spec, §4).
        return ("Left for you to re-judge",
                f"the head is now {str(p.get('head_sha') or '')[:10]} and round "
                f"{p.get('next_round')} of {p.get('max_rounds')} would be the last")
    if kind == "validation_follow_ups_filed":
        # A SEVENTH kind, and the only one that is not a verdict: the round's
        # non-blocking remarks, filed as issues on the project's own tracker instead of
        # sent back (spec §4.6, and the user's ruling of 2026-09-16 on where they go).
        # NO SEAT IS NAMED — the payload carries them, and the timeline is read by the
        # submitter; which reviewer said it belongs in the issue body and on
        # `jarvis validation show` (spec §4.7).
        items = [i for i in (p.get("items") or ()) if isinstance(i, dict)]
        dropped, failed = int(p.get("dropped") or 0), int(p.get("failed") or 0)
        parts = [f"#{i.get('number')}" if i.get("number") else str(i.get("url") or "")
                 for i in items]
        if dropped:
            parts.append(f"{dropped} more over the per-round cap")
        # A FINDING THAT COULD NOT BE FILED IS THE INTERESTING ONE. Filing crosses a
        # network, so it can fail where the backlog row it replaced could not, and a
        # reader who is not told simply never learns the remark existed.
        if failed:
            parts.append(f"{failed} could not be filed"
                         + (f" — {p['reason']}" if p.get("reason") else ""))
        if not items:
            return ("Review raised follow-ups it could not file"
                    if failed else "Review filed no follow-ups", "; ".join(parts))
        return (f"Review filed {len(items)} follow-up issue"
                f"{'' if len(items) == 1 else 's'}", "; ".join(parts))
    if kind == "validation_passed":
        return "Validation passed", p.get("reason") or ""
    if kind == "validation_rejected":
        # The reason IS the ask the worker has to answer, so unlike the "answered"
        # kinds above it is shown here: nothing else in the timeline carries it.
        return "Validation rejected — sent back", p.get("reason") or ""
    if kind == "validation_escalated":
        return ("Validation gave up — over to you", p.get("reason") or "")
    if kind == "validation_void":
        # NOT a give-up and NOT a verdict, and the label has to say both: nobody judged
        # this, and nobody needs to. A reader who takes it for an escalation goes looking
        # for a decision the OS is not waiting on.
        return ("Validation voided — nothing for a reviewer to judge",
                p.get("reason") or "")
    if kind == "validation_failed":
        # A FIFTH kind, and the one most easily misread: nothing judged the work here.
        # A reader who takes this for a rejection goes looking for something to fix that
        # nobody ever asked for, so each cause gets a sentence of its own.
        if p.get("cause") == "no_validator":
            return ("Validation skipped — no validator was configured",
                    p.get("reason") or "")
        if p.get("cause") == "ci_pending":  # project_store.VALIDATION_CI_CAUSE
            # A FOURTH cause, and the most ordinary thing on this list: the pull request
            # was submitted and GitHub has not finished running the checks the panel
            # judges the declared evidence against. Nothing is wrong and nobody is
            # needed. The line names the checks rather than the moment — unlike the
            # usage window below, CI does not say when it will be done, and "waiting for
            # unit (3.11)" is what a reader can go and look at.
            waiting = ", ".join(str(c) for c in (p.get("pending") or ()))
            return ("Validation waiting for CI",
                    f"still running: {waiting}" if waiting else "")
        if p.get("cause") == "usage_limit":  # project_store.VALIDATION_HELD_CAUSE
            # A THIRD cause, and the one a reader must not take for either of the others:
            # nothing is wrong, nobody is needed, and the round goes again by itself. The
            # moment is the whole content of the line (GitHub issue #235).
            when = _clock(p.get("reopens_at"))
            return ("Validation held — the Claude usage window is spent",
                    (f"resuming by itself at {when}" if when else "")
                    + (f" · {p.get('error')}" if p.get("error") else ""))
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
    # Issue #469: the two lines that say an attempt was NOT spent, and why. Neither
    # asks anything of the user — the gate is the item that does.
    if kind == "pr_conflict_deferred":
        return ("Merge conflict — waiting for gate "
                f"{p.get('approval_id')} before asking the worker",
                f"`{p.get('kind')}` is under review")
    if kind == "pr_conflict_rearmed":
        return ("Merge conflict — attempts given back, the gate had refused them",
                f"{p.get('attempts')} attempts restored")
    # The same three for the other repair (issue #224). Separate lines rather than one
    # parameterised pair: the words a user reads about a red build are not the words
    # they read about a conflict, and this function is a vocabulary, not a mechanism.
    if kind == "pr_checks_nudged":
        of = p.get("of")
        return ("Failing checks — asked the worker to fix them",
                ", ".join(x for x in (p.get("failing"),
                                      f"attempt {p.get('attempt')} of {of}" if of else "")
                          if x))
    if kind == "pr_checks_cleared":
        return "Checks are green again", ""
    if kind == "pr_checks_unresolved":
        return ("Failing checks the worker could not fix — over to you",
                f"{p.get('attempts')} attempts")
    if kind == "pr_checks_deferred":
        return ("Failing checks — waiting for gate "
                f"{p.get('approval_id')} before asking the worker",
                f"`{p.get('kind')}` is under review")
    if kind == "pr_checks_rearmed":
        return ("Failing checks — attempts given back, the gate had refused them",
                f"{p.get('attempts')} attempts restored")
    # THE ONLY `automerge_*` KINDS WITH LABELS HERE, and deliberately: every other one is
    # rendered by `ops.automerge_state` as the mechanism's one-line state on the work
    # order. These two are excluded from `ops.AUTOMERGE_EVENTS` — the state after either
    # is "merged" — so the timeline is the only surface they have, and without a label
    # they fall through to the kind plus a raw JSON payload (issue #253, spec §5.5).
    #
    # Two kinds and not one: only the first is a cleanup. Saying "the cleanup" about a
    # merge command that TIMED OUT sends the reader hunting a branch deletion that was
    # never attempted (`automerge.AFTER_MERGE_EVENT`).
    if kind == "automerge_cleanup_failed":
        return ("The merge landed; the cleanup after it did not",
                (p.get("reason") or "")[:200])
    if kind == "automerge_command_unfinished":
        return ("The merge command never finished — GitHub says the merge landed",
                (p.get("reason") or "")[:200])
    # THE TRACKER SIDE OF THE RECORD (issue #240). `issues.record_applied` writes one of
    # these three after — and only after — GitHub accepted the change, so each is the
    # evidence that a claim on the public tracker is now true. The timeline is their only
    # surface: the issue itself shows the result, not when the OS decided it, and
    # `issue_state` is a column nothing renders. kn-3f133363.
    if kind == "issue_in_progress":
        return "Its GitHub issue says work is under way", p.get("issue_url") or ""
    if kind == "issue_released":
        return ("Its GitHub issue was handed back — nothing is under way",
                p.get("issue_url") or "")
    if kind == "issue_closed":
        return "Its GitHub issue was closed", p.get("issue_url") or ""
    if kind == "release_batched":
        # On the RELEASE order, naming the fix it is carrying. Written once per fix, so a
        # batched release reads as the list of bugs the next version closes.
        return ("Carrying a landed fix into the next release",
                " — ".join(x for x in (p.get("issue_url"), p.get("wo_id")) if x))
    if kind == "deferral_submitted":
        # The worker deciding something is not its job is a scope decision, and the
        # timeline is the only place the user ever sees it: the item itself lands on the
        # backlog, where nothing points back at this work order's story.
        return ("Deferred something out of scope", p.get("title") or "")
    if kind == BACKGROUND_ORPHANED:
        # The detector, on the timeline beside the void it puts on the message itself —
        # spec §2. Names the jobs, because the id is what a reader checks against `ps`
        # and what the OS's own alarm quoted.
        return ("A background job died with the turn that started it",
                _job_labels(p) + " — nothing it was told to do has run")
    if kind == BACKGROUND_NUDGED:
        # Says the OS did it, for `pr_checks_nudged`'s reason one surface along: a
        # message nobody typed must not read afterwards as one the user sent.
        of = p.get("of")
        return ("Sent back to run it in the foreground",
                f"attempt {p.get('attempt')} of {of}" if of else "")
    if kind == BACKGROUND_UNRESOLVED:
        return ("Backgrounded it again after being sent back — over to you",
                f"{p.get('attempts')} attempts")
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
#: them on the user's behalf either. Both are pull-request repairs a poll writes on
#: seeing GitHub report CONFLICTING or a failed check (§6 of
#: docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md, and
#: docs/superpowers/specs/2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md).
#: The third is `background.SOURCE`: the note a resume carries when the last turn ended
#: on a background job the OS caught dying with it (§4 of
#: docs/superpowers/specs/2026-09-22-a-dead-background-job-is-not-a-live-one.md).
UNAUTHORED_SOURCES = frozenset({"pr-conflict", "pr-checks", "background-orphan"})

#: `remedies.MESSAGE_SOURCE`, spelled out here for the reason `ALARM_KINDS` is: this
#: module is a leaf and opens nothing. A test pins the two equal. Deliberately NOT a
#: member of `UNAUTHORED_SOURCES` above — an unauthored message is one nobody decided,
#: and the supervisor decided this one, under a grant it had to be given first (§6 of
#: docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md).
SUPERVISOR_SOURCE = "supervisor"


def _message_label(m: dict[str, Any]) -> str:
    """Who is speaking, from the message's own `source` — §5.

    The conversation's label: two parties and an arrow, because the reader is looking
    at the words. The timeline wants a sentence instead; see `_message_event_label`.
    """
    if m.get("direction") != "user_to_agent":
        return "worker → you"
    if m.get("source") == "neo":
        return "neo → worker"
    if m.get("source") == SUPERVISOR_SOURCE:
        return "supervisor → worker"
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
    if m.get("source") == SUPERVISOR_SOURCE:
        # The same arm as `_message_label`'s, and it needs its own: without it a nudge
        # the user never sent reads on the timeline as "You messaged the worker".
        return "The supervisor messaged the worker"
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


def _job_labels(p: dict[str, Any]) -> str:
    """The jobs in a `background_orphaned` payload, named for a person.

    `background.Job.label`'s wording, written out again for the reason the constants
    above are: this module imports nothing. A test pins the two renderings equal.
    """
    named = []
    for job in (p.get("jobs") or ()):
        if not isinstance(job, dict):
            continue
        command = " ".join(str(job.get("command") or "").split())
        if len(command) > JOB_COMMAND_CHARS:
            command = command[:JOB_COMMAND_CHARS] + "…"
        named.append(f"`{job.get('id')}` (`{command}`)" if command
                     else f"`{job.get('id')}`")
    return ", ".join(named) or "a background job"


def _voided(events: list[dict[str, Any]]) -> dict[Any, str]:
    """Which messages the OS has since contradicted, keyed by message id — spec §3.

    DERIVED at render time from the event the reaper wrote, never stored beside the
    message: the fact has one home, every surface that builds the conversation reads it
    from here, and no column had to be added to carry a second copy of it.
    """
    void: dict[Any, str] = {}
    for e in events:
        if e.get("kind") != BACKGROUND_ORPHANED:
            continue
        p = _payload(e)
        if p.get("msg_id") is not None:
            void[p["msg_id"]] = VOID_MESSAGE.format(jobs=_job_labels(p))
    return void


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
                "void": "", "content": text,
                "anchor": f"q-{qid}" if qid is not None else "",
                "ref": _ref(kind, p), "msg_id": None,
                "source": "neo", "status": "", "inbound": False,
            })
            continue
        turns.append({
            "ts": e.get("ts") or 0.0,
            "kind": "note" if kind == "alarm_reviewed" else "advice",
            "who": ("supervisor → you" if kind == "alarm_reviewed"
                    else "neo → supervisor"),
            "void": "", "content": text, "anchor": "", "ref": _ref(kind, p),
            "msg_id": None, "source": "", "status": "", "inbound": False,
        })
    void = _voided(events)
    for m in messages:
        mid = m.get("id")
        turns.append({
            "ts": m.get("ts") or 0.0, "kind": "message", "who": _message_label(m),
            # BEFORE `content`, so the CLI's generic printer renders the retraction
            # above the words it retracts rather than below them (spec §3).
            "void": void.get(mid, ""),
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
