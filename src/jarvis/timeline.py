"""A work order's record, in two readings (docs/superpowers/specs/2026-08-23-the-work-order-record.md §1)"""

from __future__ import annotations

import json
import time
from typing import Any

#: `ops.ESCALATION_FAMILY`'s three written families, as the literals this module is allowed to hold
ESCALATION_HANDOVER = frozenset({
    "rounds_spent", "user_rework_ceiling", "rebinds_spent"})
ESCALATION_NOT_REVIEWED = frozenset({
    "bounce_exhausted", "nothing_to_judge", "repeat_submission"})
ESCALATION_UNJUDGED = frozenset({
    "no_verdict", "validator_unreachable", "prompt_refused", "panel_killed"})

# Plumbing: how a message got carried, which session was bound, when a turn ended
DEBUG_KINDS = frozenset({
    "message_queued",           # queued for delivery — the message body is the signal
    "delivering",               # delivery attempt
    "message_delivered",        # delivery receipt
    "turn_started",             # a `claude -p` turn was launched
    "turn_ended",               # that turn's process finished and its reply was captured
    "session_released",         # a legacy background agent handed its session over
    "hook_ignored",             # a hook from a session that is not this work order's
    "tool_managed_paths",       # which tool-owned files the worktree's index now hides
    "permission_mode_changed",  # worker permission plumbing
    "notification_ignored",     # idle prompt on an already-settled work order
    # Same moment as the message carrying the answer, so the message is the entry — §5
    "neo_answered",
    "escalation_answered",
    # THE SWEEP LOOKING, which is not the same event as the sweep FINDING something (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)
    "health_reviewed",
    # The retry sweep DEFERRING a relaunch, restated every five minutes for as long as the cap holds (docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md)
    "retry_held",
})

#: `background.EVENT`, spelled out here for the reason `SUPERVISOR_SOURCE` is: this module is a leaf... (docs/superpowers/specs/2026-09-22-a-dead-background-job-is-not-a-live-one.md)
BACKGROUND_ORPHANED = "background_orphaned"
BACKGROUND_NUDGED = "background_nudged"
BACKGROUND_UNRESOLVED = "background_unresolved"

#: What the conversation prints ABOVE a message whose turn left a background job behind
VOID_MESSAGE = ("⚠ VOID — the turn that wrote this ended while {jobs} was still "
                "running, and a turn is one process: the job died with it. Nothing this "
                "message describes as running has run since.")

#: How much of a command survives into a job's label
JOB_COMMAND_CHARS = 80

STATUS_LABEL = {
    "pending": "Queued",
    "dispatching": "Dispatching worker",
    "running": "Running",
    # NOT "Waiting on you" (#264)
    "idle": "Idle — waiting for its feature",
    "waiting_input": "Waiting on you",
    "validating": "Under review by the validation panel",
    "needs_review": "Needs your review",
    # The two `ops.state_durations` was built for
    "waiting_pr_merge": "Waiting for its pull request to merge",
    "budget_exhausted": "Budget spent",
    "completed": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


#: What the OS read off disk when a turn died without writing a result (docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md §5)
TURN_HARVESTED = "turn_harvested"


def event_level(kind: str) -> str:
    """"debug" for plumbing, "signal" for anything the user should see by default"""
    if kind.startswith("hook:") or kind in DEBUG_KINDS:
        return "debug"
    return "signal"


def _clock(reset_at: Any) -> str:
    """A usage-limit reset moment as local wall-clock, or "" if there was none"""
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


#: The kinds carrying one alarm's life (docs/superpowers/specs/2026-08-31-the-supervisor.md)
ALARM_KINDS = frozenset({"cost_alarm", "alarm_reviewed", "alarm_escalated",
                         "alarm_advice", "health_finding", "health_reviewed",
                         "remedy_proposed", "remedy_applied", "remedy_refused",
                         "rule_fired", "rule_dry_run", "rule_cleared"})

#: `project_store.NO_TURN`, duplicated for `ALARM_KINDS`' reason
NO_TURN = -1


def _ref(kind: str, p: dict[str, Any]) -> dict[str, Any] | None:
    """The record this entry points at, or None"""
    if kind == "question_asked":
        qid = _neo_question_id(p)
        if qid is not None:
            return {"kind": "neo_question", "id": qid, "label": f"question #{qid}"}
    if kind in ALARM_KINDS:
        # The alarm, even on the two kinds that also carry a `neo_question_id`: a ref is singular
        alarm_id = p.get("alarm_id")
        if alarm_id:
            return {"kind": "alarm", "id": str(alarm_id), "label": f"alarm {alarm_id}"}
    return None


#: Each ingredient of the prompt prefix in the words a reader can act on
PREFIX_INGREDIENT_LABEL = {
    "cli_version": "the Claude Code version",
    "git_briefing": "the git briefing Jarvis adds to every turn",
    "worker_settings": "the worker's settings file",
    "memory": "a CLAUDE.md the worker loads",
}


def _prefix_changes(p: dict[str, Any]) -> str:
    """"the Claude Code version (2.1.271 -> 2.1.272) changed", from the drift payload"""
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
    """(label, detail) in plain language for one event, from its payload alone"""
    if kind == "created":
        return "Work order created", ""  # the title and description are already above
    if kind == "status":
        status = p.get("status", "")
        return STATUS_LABEL.get(status, status or "Status changed"), ""
    if kind == "dispatched":
        return "Worker dispatched", p.get("worktree") or ""
    # §2.6 of docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md
    if kind == "precondition_added":
        return (f"Held until {p.get('ref') or 'something'} clears",
                p.get("note") or "")
    if kind == "precondition_cleared":
        by = "the OS" if p.get("by") == "os" else "you"
        return (f"Hold on {p.get('ref') or 'something'} cleared by {by}",
                p.get("note") or "")
    if kind == "dispatch_deferred_for_compaction":
        # The prompt is QUEUED, not sent: the audit trail is this
        return ("Dispatch held behind a compaction",
                "the prompt goes out once the conversation is summarised")
    # §7: the verdict on THIS order, written here because the investigator's own work order is not what a...
    if kind == "investigation_verdict":
        return (f"Investigated: {p.get('classification') or 'no classification'}",
                " · ".join(x for x in (p.get("investigation"), p.get("filed")) if x))
    if kind == "turn_failed":
        return "Worker turn failed", (p.get("error") or "")[:200]
    if kind == TURN_HARVESTED:
        # §5 of docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md
        if p.get("empty"):
            return ("Nothing to harvest",
                    "the worktree was clean and the turn said nothing")
        authored = p.get("authored") or {}
        commits = int(authored.get("commits") or 0)
        dirty = list(authored.get("dirty") or ())
        parts = []
        if commits:
            parts.append(f"{commits} commit{'s' if commits != 1 else ''}")
        if dirty:
            parts.append(f"{len(dirty)} uncommitted file"
                         f"{'s' if len(dirty) != 1 else ''}"
                         + (f" checkpointed as {p['checkpoint']}"
                            if p.get("checkpoint") else " left uncommitted"))
        if p.get("said"):
            parts.append("its last message")
        return "Harvested what the turn left behind", ", ".join(parts)
    # The self-healing trio
    if kind in ("turn_paused", "rate_limited"):
        if p.get("reason") == "auth":
            # The only one of the three that names what the READER has to do: the other two resume on a clock
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
    # A SIGNAL AND NOT PLUMBING, unlike `turn_started`/`turn_ended` beside it
    if kind == "compacting":
        return "Compacting the conversation", p.get("reason") or ""
    if kind == "compacted":
        # `ok` is absent on the hook's own event (`hooks.note_compaction`)
        if p.get("ok") is False:
            # The conversation is INTACT and the next prompt still pays the re-write
            return "Compaction failed", str(p.get("error") or "")
        before, after = p.get("before"), p.get("after")
        if isinstance(before, int) and isinstance(after, int) and before:
            return ("Conversation compacted",
                    f"{before:,} tokens summarised to {after:,}")
        return "Conversation compacted", ""
    if kind == "attention":
        return "Needs you", p.get("reason") or ""
    if kind == "acknowledged":
        # Reached the user as an escaped JSON blob until issue 573
        who = p.get("by")
        return (f"Acknowledged by {who}" if who else "Acknowledged",
                "\n".join(str(b) for b in (p.get("blockers") or [])))
    if kind == "cost_alarm":
        # Its own line rather than folded into "Needs you": only the FIRST alarm of a turn raises the flag
        if p.get("seq") == NO_TURN:
            return "A standing cost finding on this project", p.get("reason") or ""
        return "Costing money while it runs", p.get("reason") or ""
    # The supervisor's three
    if kind == "alarm_reviewed":
        # The verdict's REASON, which is for the record
        verb = ("cleared this alarm" if p.get("verdict") == "ack"
                else "could not settle this alarm")
        return f"The supervisor {verb}", p.get("reason") or ""
    if kind == "alarm_escalated":
        qid = _neo_question_id(p)
        # The question number as text, not as the ref: this entry spends its one pointer on the alarm
        return "Alarm handed to Neo", f"question #{qid}" if qid is not None else ""
    if kind == "alarm_advice":
        return "Neo advised the supervisor", ""  # the answer is in the conversation
    # The health sweep's two and the remedy's three (§6 of... (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)
    if kind == "health_finding":
        return "The supervisor found something wrong", p.get("reason") or ""
    if kind == "health_reviewed":
        found = p.get("findings") or 0
        return ("The supervisor checked this over",
                f"{p.get('trigger') or 'swept'} · "
                + (f"{found} finding(s)" if found else "nothing found"))
    if kind == "remedy_proposed":
        # PERMISSION ASKED, NOTHING DONE (docs/superpowers/specs/2026-09-24-order-observability.md)
        detail = f"{p.get('remedy') or 'a remedy'}: {p.get('argument') or ''}"
        if not p.get("alarm_id"):
            return ("A fix you asked for needs permission", detail)
        return ("The supervisor asked permission to act", detail)
    if kind == "remedy_applied":
        detail = f"{p.get('remedy') or 'a remedy'}: {p.get('result') or ''}"
        if not p.get("alarm_id"):
            return ("The OS applied the fix you asked for", detail)
        return ("The supervisor acted", detail)
    if kind == "remedy_refused":
        # Two payload shapes arrive here
        by = f" by {p['by']}" if p.get("by") else ""
        return (f"The {p.get('remedy') or 'proposed'} remedy was refused{by}",
                p.get("reason") or "")
    # The self-evolution registry's three (§6 of docs/superpowers/specs/2026-09-27-self-evolution.md)
    if kind == "rule_fired":
        return ("A rule recognised a known gap",
                f"{p.get('detector_id') or ''}: {p.get('reason') or ''}")
    if kind == "rule_dry_run":
        return ("A rule would have fired (dry run)", p.get("detector_id") or "")
    if kind == "rule_cleared":
        return ("The gap a rule recognised has cleared", p.get("detector_id") or "")
    # The analyst's terminal action (`ops.submit_findings`)
    if kind == "findings_submitted":
        n = p.get("findings")
        return ("Findings submitted",
                f"{n} finding(s) on {p.get('improvement_order') or ''}".strip())
    if kind == "findings_reviewed":
        a, r = len(p.get("accepted") or []), len(p.get("rejected") or [])
        filed = len(p.get("created") or [])
        return (f"Findings reviewed by {p.get('by') or 'the user'}",
                f"{a} accepted, {r} rejected, {filed} order(s) filed")
    if kind == "assumption":
        n = p.get("n")  # the number, not the text — §4
        return (f"Assumption #{n} recorded" if n else "Assumption recorded"), ""
    if kind == "question_asked":
        # Never the text, with or without an id: the conversation renders the ask from this same payload
        return "Worker asked a question", ""
    # Debug (see DEBUG_KINDS); they still render, with no detail, when asked for
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
    # Auto-review's (`ops.AUTOREVIEW_EVENTS`
    if kind == "autoreview_asked":
        return (f"Asked Neo to rule on assumption #{p.get('n')}",
                f"question {p['neo_question_id']}" if p.get("neo_question_id") else "")
    if kind == "autoreview_accepted":
        # The model is in the label, not the detail: "the OS accepted it" and "THIS model accepted it" are...
        model = p.get("model") or "model not recorded"
        return (f"Assumption #{p.get('n')} accepted by the OS "
                f"(Neo, {model}) — not by you",
                p.get("reason") or "no reason recorded")
    if kind == "autoreview_escalated":
        # `dropped` means the OS had a ruling and threw it away because the state moved under it (spec §5.1)
        why = " — the OS dropped its ruling when the work order changed" if p.get(
            "dropped") else ""
        return (f"Assumption #{p.get('n')} left with you{why}",
                p.get("reason") or "no reason recorded")
    if kind == "autoreview_held":
        return (f"The OS would not decide assumption #{p.get('n')}",
                p.get("reason") or "no reason recorded")
    # ...and the early pass's five (docs/superpowers/specs/2026-09-23-an-assumption-...
    if kind == "autoreview_provisional":
        verdict = p.get("verdict") or "judged"
        model = p.get("model") or "model not recorded"
        return (f"The OS read assumption #{p.get('n')} while the worker was still "
                f"typing: {verdict}, provisionally (Neo, {model}) — nothing settled",
                p.get("reason") or "no reason recorded")
    if kind == "autoreview_objected":
        # The transport is in the detail
        how = p.get("transport") or "queue"
        return (f"The OS objected to assumption #{p.get('n')} and sent it to the worker",
                f"over the {how}: {p.get('reason') or 'no reason recorded'}")
    if kind == "autoreview_objection_withdrawn":
        return (f"Objection to assumption #{p.get('n')} withdrawn — the worker was "
                f"never told",
                p.get("reason") or "no reason recorded")
    if kind == "autoreview_confirmed":
        # `autoreview_accepted`'s words and its reason: the model belongs in the label
        model = p.get("model") or "model not recorded"
        return (f"Assumption #{p.get('n')} confirmed against the result and accepted "
                f"by the OS (Neo, {model}) — not by you",
                p.get("reason") or "no reason recorded")
    if kind == "autoreview_unconfirmed":
        return (f"Assumption #{p.get('n')} left with you — the OS did not confirm its "
                f"early reading once it saw the result",
                p.get("reason") or "no reason recorded")
    if kind == "learning_captured":
        return "Learning captured", p.get("topic") or ""
    if kind == "prefix_drift":
        # Labelled an early warning on the surface too
        return ("Prompt prefix moved — early warning, not a measurement",
                f"{_prefix_changes(p)}. This turn's cached prefix was probably re-written, "
                f"so the conversation so far was re-sent; `jarvis doctor` reports what it "
                f"actually cost.")
    if kind == "gate_requested":
        # The seat, when a subagent tripped the gate
        who = f" (seat `{p['agent_type']}`)" if p.get("agent_type") else ""
        # A held request was RECORDED, not asked: the worker ran the command and no reviewer has been shown...
        verb = ("Contested the `%s` gate match — says this performs no privileged action"
                % (p.get("kind") or "gate") if p.get("contested")
                else "Ran a gated command — request recorded, awaiting its case"
                if p.get("held") else f"Asked permission to {p.get('kind') or 'act'}")
        return (f"{verb}{who}", p.get("command") or "")
    if kind == "gate_amended":
        # Not a second request — the same one, better argued
        return (f"Made the case for the pending `{p.get('kind') or 'gate'}` request",
                p.get("command") or "")
    if kind == "gate_decided":
        verb = "Approved" if p.get("decision") == "approved" else "Denied"
        return (f"{verb} by {p.get('by') or '?'}: {p.get('kind') or 'gate'}",
                p.get("reason") or "")
    if kind == "gate_dismissed":
        # Deliberately not phrased as a verdict on the worker
        return (f"Not a privileged action — the `{p.get('kind') or 'gate'}` gate matched "
                f"this by mistake ({p.get('by') or '?'})",
                p.get("reason") or "")
    if kind == "gate_superseded":
        # Neither a verdict nor a dismissal: the question stopped being answerable
        return (f"Gate request closed unanswered — the `{p.get('kind') or 'gate'}` "
                f"question no longer applies", p.get("reason") or "")
    if kind == "gate_abandoned":
        # Not a denial
        return (f"Gate request abandoned — no case was ever made for the "
                f"`{p.get('kind') or 'gate'}` block, and nothing was decided",
                p.get("reason") or "")
    if kind == "gate_escalated":
        return "Gate approval escalated to you", p.get("reason") or ""
    if kind == "gate_opened":
        # The moment the privileged command actually ran
        if p.get("clearance") == "dismissed":
            return (f"Ran the command the `{p.get('kind') or 'gate'}` gate had matched "
                    f"by mistake", "no privileged action was authorised")
        return (f"Ran the approved {p.get('kind') or 'command'}",
                f"use {p.get('use')} of {p.get('of')}")
    # The validation loop
    if kind == "validation_submitted":
        rnd = p.get("round")
        return ("Submitted for validation",
                f"round {rnd}" if rnd else "")
    if kind == "validation_forced":
        # A SIXTH kind, and the only one no worker produced (docs/superpowers/specs/2026-09-19-a-moved-head-re-judges-itself.md §5)
        rnd, was = p.get("round"), p.get("was")
        who = "by the OS" if str(p.get("by") or "") == "os" else "by hand"
        # A REBIND says so beside the number: round 4 under `max_rounds` 3 reads as a spent budget otherwise...
        cause = str(p.get("cause") or ("rebind" if p.get("rebind") else ""))
        spent = (", no round spent — the rework you asked for"
                 if cause == "user_rework"                  # ops.USER_REWORK_CAUSE
                 else ", no round spent" if cause else "")
        return (f"Validation forced {who} — round {rnd}{spent}" if rnd
                else f"Validation forced {who}{spent}",
                f"{p.get('reason') or ''}"
                + (f" (was {was})" if was else ""))
    if kind == "validation_head_carried":
        # THE ROUND THAT WAS NOT SPENT, and Neo's second condition for allowing it (spec... (docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §4)
        judged = str(p.get("judged_sha") or "")
        now = str(p.get("carried_head_sha") or "")
        n = len(p.get("chain") or ()) or 1
        base = p.get("base") or "the base"
        # Not every walked merge is a merge of the base since Neo question 806 widened proof (a): one the...
        bases = p.get("merged_base_shas")
        n_base = n if bases is None else len(bases)
        how = (f"{n} merge(s) of {base} and nothing else" if n_base == n else
               f"{n} merge(s) — {n_base} of {base}, the rest bringing in only commits it "
               f"already contained")
        return ("Verdict carried to the new head",
                f"round {p.get('round')} passed on {judged[:10]}; it now covers "
                f"{now[:10]}, which is {judged[:10]} plus {how}")
    if kind == "validation_carry_refused":
        # The other half, and it must NAME THE PROOF: "someone resolved a conflict" and "the daemon could not...
        head, proof = str(p.get("head_sha") or ""), str(p.get("proof") or "")
        detail = {
            "patch_id": (f"the diff on {head[:10]} is not the diff round "
                         f"{p.get('round')} read, so the merge resolved content nobody "
                         f"judged"),
            "chain": (f"{head[:10]} is not the judged commit plus merges of the base "
                      f"or of commits it already contained"),
            "fetch": "no local answer about this branch's own diff",
            "read": "GitHub would not say what this commit merged",
        }.get(proof, str(p.get("detail") or "the proof could not be made"))
        return "Verdict not carried — re-judging", detail
    if kind == "validation_rejudge_declined":
        # The one moved head the OS will NOT re-judge: the round it would open is the last one
        head = str(p.get("head_sha") or "")[:10]
        if str(p.get("cause") or "") == "rebind_exhausted":     # ops.REBIND_EXHAUSTED
            return ("Left for you to re-judge",
                    f"the head is now {head} and the OS has already re-judged this "
                    f"merge {p.get('rebinds')} time(s) — the limit")
        return ("Left for you to re-judge",
                f"the head is now {head} and round "
                f"{p.get('next_round')} of {p.get('max_rounds')} would be the last")
    if kind == "validation_follow_ups_filed":
        # A SEVENTH kind, and the only one that is not a verdict: the round's non-blocking remarks
        items = [i for i in (p.get("items") or ()) if isinstance(i, dict)]
        kept = [i for i in (p.get("withheld") or ()) if isinstance(i, dict)]
        dropped, failed = int(p.get("dropped") or 0), int(p.get("failed") or 0)
        parts = [f"#{i.get('number')}" if i.get("number") else str(i.get("url") or "")
                 for i in items]
        # KEPT IS NOT FILED AND NOT LOST
        if kept:
            parts.append(f"{len(kept)} kept on the internal record"
                         + (f" — {p['reason']}" if p.get("reason") and not failed
                            else ""))
        if dropped:
            parts.append(f"{dropped} more over the cap")
        # A FINDING THAT COULD NOT BE FILED IS THE INTERESTING ONE
        if failed:
            parts.append(f"{failed} could not be filed"
                         + (f" — {p['reason']}" if p.get("reason") else ""))
        if not items:
            if failed:
                return "Review raised follow-ups it could not file", "; ".join(parts)
            if kept:
                return (f"Review kept {len(kept)} follow-up"
                        f"{'' if len(kept) == 1 else 's'} off the tracker",
                        "; ".join(parts))
            return "Review filed no follow-ups", "; ".join(parts)
        return (f"Review filed {len(items)} follow-up issue"
                f"{'' if len(items) == 1 else 's'}", "; ".join(parts))
    if kind == "validation_passed":
        return "Validation passed", p.get("reason") or ""
    if kind == "validation_rejected":
        # The reason IS the ask the worker has to answer
        if str(p.get("uncounted_cause") or "") == "user_rework":
            return ("Validation rejected — sent back, no round spent (the rework you "
                    "asked for)", p.get("reason") or "")
        # Spec §7: docs/superpowers/specs/2026-10-13-a-red-head-is-sent-back-before-the-panel.md
        if str(p.get("uncounted_cause") or "") == "red_ci":
            return ("Validation rejected — sent back unjudged, no round spent (CI was "
                    "red)", p.get("reason") or "")
        return "Validation rejected — sent back", p.get("reason") or ""
    if kind == "validation_bounced":
        # NO ROUND WAS SPENT and the line has to say so (docs/superpowers/specs/2026-09-22-a-round-must-answer-the-list.md §7.)
        cited = ", ".join(str(c) for c in (p.get("cited") or ()))
        return (f"Sent back without a review round — nothing round "
                f"{p.get('after_round')} asked about changed",
                f"unchanged since that round: {cited}" if cited else "")
    if kind == "validation_escalated":
        # WHICH FAMILY, not merely that a round escalated: "gave up" read as "nobody judged this" for the... (docs/superpowers/specs/2026-10-14-an-escalated-round-says-why-and-what-to-run-next.md §1)
        cause = str(p.get("cause") or "")
        if cause in ESCALATION_HANDOVER:
            return ("Validation was rejected and handed to you", p.get("reason") or "")
        if cause in ESCALATION_NOT_REVIEWED:
            return ("Validation was not reviewed — over to you", p.get("reason") or "")
        if cause in ESCALATION_UNJUDGED:
            return ("Validation could not be judged — over to you",
                    p.get("reason") or "")
        return ("Validation was escalated — over to you", p.get("reason") or "")
    if kind == "validation_void":
        # NOT a give-up and NOT a verdict
        return ("Validation voided — nothing for a reviewer to judge",
                p.get("reason") or "")
    if kind == "validation_failed":
        # A FIFTH kind, and the one most easily misread: nothing judged the work here
        if p.get("cause") == "no_validator":
            return ("Validation skipped — no validator was configured",
                    p.get("reason") or "")
        if p.get("cause") == "ci_pending":  # project_store.VALIDATION_CI_CAUSE
            # A FOURTH cause, and the most ordinary thing on this list: the pull request was submitted and GitHub...
            waiting = ", ".join(str(c) for c in (p.get("pending") or ()))
            return ("Validation waiting for CI",
                    f"still running: {waiting}" if waiting else "")
        if p.get("cause") == "usage_limit":  # project_store.VALIDATION_HELD_CAUSE
            # A THIRD cause, and the one a reader must not take for either of the others: nothing is wrong (#235)
            when = _clock(p.get("reopens_at"))
            return ("Validation held — the Claude usage window is spent",
                    (f"resuming by itself at {when}" if when else "")
                    + (f" · {p.get('error')}" if p.get("error") else ""))
        if p.get("cause") == "auth":  # project_store.VALIDATION_AUTH_CAUSE
            # A FIFTH cause, and the one whose old reading cost four work orders a round and a user each (GitHub... (#778)
            attempt = p.get("attempt")
            return ("Validation held — Claude Code could not authenticate",
                    f"attempt {attempt}: {p.get('error') or ''}")
        attempt = p.get("attempt")
        return ("Validation could not be run — the reviewer was unreachable",
                f"attempt {attempt}: {p.get('error') or ''}" if attempt
                else (p.get("error") or ""))
    # The conflict-healing loop, on the timeline so that a give-up arrives with the record of what was... (docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md)
    if kind == "pr_conflict_nudged":
        of = p.get("of")
        return ("Merge conflict — asked the worker to resolve it",
                f"attempt {p.get('attempt')} of {of}" if of else "")
    if kind == "pr_conflict_cleared":
        return "Merge conflict resolved", ""
    # wo-d3a70c29: evidence, not an alarm
    if kind == "pr_recompute_stalled":
        # TWO CONDITIONS, TWO SENTENCES
        ref, head = str(p.get("ref_oid") or ""), str(p.get("head_oid") or "")
        title = "GitHub stopped recomputing the pull request"
        if ref and head:
            return title, (f"branch at {ref[:10]}, pull request head at {head[:10]}")
        since = _clock(p.get("unknown_since"))
        return title, (f"mergeability unanswered since {since}" if since
                       else "mergeability unanswered")
    # ...and the remedy, which the OS now performs instead of the user (wo-7ec57748)
    if kind == "pr_resync_started":
        ref, head = str(p.get("ref_oid") or ""), str(p.get("head_oid") or "")
        return ("Closing and reopening the pull request to make GitHub recompute it",
                (f"branch at {ref[:10]}, pull request head at {head[:10]}"
                 if ref and head else str(p.get("pr_url") or "")))
    if kind == "pr_resync_done":
        # TWO WRITERS, ONE EVENT KIND, told apart by `observed`: the OS's own reopen returning
        if p.get("observed"):
            head = str(p.get("head_oid") or "")
            return ("Pull request is open again — the resync window is over",
                    f"head at {head[:10]}" if head else "")
        return "Pull request closed and reopened — waiting for GitHub to answer", ""
    if kind == "pr_resync_failed":
        # THE STAGE IS THE WHOLE POINT: a close that failed changed nothing
        return (f"Could not {p.get('stage') or 'resync'} the pull request to resync it",
                str(p.get("error") or p.get("reason") or ""))
    if kind == "pr_resync_cleared":
        head = str(p.get("head_oid") or "")
        return ("GitHub is recomputing the pull request again",
                f"head at {head[:10]}" if head else "")
    # Cancelled CI, re-run once per head (#1123)
    if kind == "ci_rerun_requested":
        runs = ", ".join(str(i) for i in p.get("run_ids") or [])
        return ("CI was cancelled — the OS re-ran it once",
                f"{p.get('checks') or ''}; run {runs}" if runs else str(p.get("checks") or ""))
    if kind == "ci_rerun_unrecoverable":
        return ("Cancelled CI the OS will not re-run again — over to you",
                str(p.get("blocker") or ""))
    if kind == "ci_rerun_recovered":
        return "Cancelled CI no longer holds the pull request", ""
    if kind == "pr_conflict_unresolved":
        return ("Merge conflict the worker could not resolve — over to you",
                f"{p.get('attempts')} attempts")
    # Issue #469: the two lines that say an attempt was NOT spent, and why
    if kind == "pr_conflict_deferred":
        return ("Merge conflict — waiting for gate "
                f"{p.get('approval_id')} before asking the worker",
                f"`{p.get('kind')}` is under review")
    if kind == "pr_conflict_rearmed":
        return ("Merge conflict — attempts given back, the gate had refused them",
                f"{p.get('attempts')} attempts restored")
    # The same three for the other repair (issue #224)
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
    # "It fixed itself" has to be PROVABLE (#705)
    if kind == "pr_repair_resettled":
        return ("Back in the merge queue — the reason it was held is gone",
                f"was {p.get('was')}" if p.get("was") else "")
    # ...and the inverse move, for the same reason (docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md)
    if kind == "give_up_reparked":
        return ("Back on your list — the panel's give-up was never decided",
                ", ".join(x for x in (f"was {p.get('was')}" if p.get("was") else "",
                                      f"round {p.get('round')}" if p.get("round")
                                      else "")
                          if x))
    # THE ONLY `automerge_*` KINDS WITH LABELS HERE (#253)
    if kind == "automerge_cleanup_failed":
        return ("The merge landed; the cleanup after it did not",
                (p.get("reason") or "")[:200])
    if kind == "automerge_command_unfinished":
        return ("The merge command never finished — GitHub says the merge landed",
                (p.get("reason") or "")[:200])
    # Spec 2026-09-28 §3.1: the OS refused before GitHub heard anything
    if kind == "automerge_base_stale":
        head = str(p.get("head_sha") or "")
        base = str(p.get("base") or "the base")
        oid = str(p.get("base_oid") or "")
        return ("Merge refused — the base moved under it",
                f"round {p.get('round') or '?'} passed on "
                f"{head[:10] or 'an unknown commit'}, and {base} is now "
                f"at {oid[:10] or 'a commit the OS could not read'}, which that commit "
                f"does not contain — catching the branch up instead")
    # THE TRACKER SIDE OF THE RECORD (issue #240)
    if kind == "issue_in_progress":
        return "Its GitHub issue says work is under way", p.get("issue_url") or ""
    if kind == "issue_released":
        return ("Its GitHub issue was handed back — nothing is under way",
                p.get("issue_url") or "")
    if kind == "issue_closed":
        return "Its GitHub issue was closed", p.get("issue_url") or ""
    if kind == "release_batched":
        # On the RELEASE order, naming the fix it is carrying
        return ("Carrying a landed fix into the next release",
                " — ".join(x for x in (p.get("issue_url"), p.get("wo_id")) if x))
    if kind == "release_overtaken":
        # On the RELEASE order: another release already carries its batch, and production has not caught up (#784)
        return ("Another release already carries these fixes",
                p.get("detail") or (p.get("tag") or ""))
    if kind == "release_held_red_base":
        # On the RELEASE order: the base is red, so the ship is deferred rather than attempted
        return ("Holding the release — the base branch is red",
                p.get("detail") or (p.get("base") or ""))
    if kind == "release_deferred_red_base":
        # On the RELEASE order, mid-run: it delivered no release because the base is not buildable
        return ("Waiting to ship — the base branch is not buildable",
                p.get("detail") or (p.get("base") or ""))
    if kind == "release_park_red_base":
        # ...and the end of that wait: past `Daemon.RED_PARK_AFTER_SECONDS` the release asks the user
        return ("Stopped waiting for the base branch and asked you",
                p.get("detail") or (p.get("base") or ""))
    if kind == "release_completed":
        # The ending itself, whichever path reached it: `why` is which one (release.py `settle`)
        return "The release this order was filed for is done", p.get("why") or ""
    if kind == "pr_merge_commit_recorded":
        # On the FIXING order, and NOT the branch tip: the commit the squash merge put on `main`
        return ("Recorded the commit its merge put on the base branch",
                p.get("merge_commit") or "")
    if kind == "deferral_submitted":
        # The worker deciding something is not its job is a scope decision
        return ("Deferred something out of scope", p.get("title") or "")
    if kind == BACKGROUND_ORPHANED:
        # The detector, on the timeline beside the void it puts on the message itself — spec §2
        return ("A background job died with the turn that started it",
                _job_labels(p) + " — nothing it was told to do has run")
    if kind == BACKGROUND_NUDGED:
        # Says the OS did it, for `pr_checks_nudged`'s reason one surface along: a message nobody typed must...
        of = p.get("of")
        return ("Sent back to run it in the foreground",
                f"attempt {p.get('attempt')} of {of}" if of else "")
    if kind == BACKGROUND_UNRESOLVED:
        return ("Backgrounded it again after being sent back — over to you",
                f"{p.get('attempts')} attempts")
    if kind == "finished":
        return "Finished", p.get("summary") or ""
    if kind == "marked_done":
        # The user closed it, not the worker — worth telling apart on the record
        return "Marked done by you", (
            "the worker's turn was stopped" if p.get("session_stopped") else "")
    if kind == "retry_requested":
        # §8 of docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md
        return ("You retried this order",
                "with your message" if p.get("authored")
                else "the OS's own relaunch note — you sent no message")
    if kind == "hidden":
        return ("Hidden" if p.get("hidden") else "Unhidden"), ""
    if kind == "invariant":
        detail = p.get("detail") or ""
        if p.get("repaired"):
            return "OS self-check repaired this", f"{detail} → {p.get('repair') or ''}"
        return "OS self-check failed", detail
    # Unclassified or debug: show the kind and its raw payload
    return kind, json.dumps(p, sort_keys=True) if p else ""


#: Message sources nobody decided: no user typed them (docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md)
UNAUTHORED_SOURCES = frozenset({"pr-conflict", "pr-checks", "background-orphan",
                                "relaunch"})

#: `remedies.MESSAGE_SOURCE`, spelled out here for the reason `ALARM_KINDS` is: this module is a leaf... (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)
SUPERVISOR_SOURCE = "supervisor"

#: `project_store.MESSAGE_AUTHOR_USER`, spelled out for the same leaf reason (docs/superpowers/specs/2026-08-23-the-work-order-record.md §5)
AUTHOR_USER = "user"

#: Named machine speakers, as (conversation tag, timeline sentence)
_SOURCE_SPEAKERS: dict[str, tuple[str, str]] = {
    # `source="neo"` is written in exactly one place (`daemon._neo_drain`)
    "neo": ("neo → worker", "Neo answered the worker"),
    SUPERVISOR_SOURCE: ("supervisor → worker", "The supervisor messaged the worker"),
}
_JARVIS_SPEAKER = ("jarvis → worker", "Jarvis messaged the worker")
_USER_SPEAKER = ("you → worker", "You messaged the worker")


def _speaker(m: dict[str, Any]) -> tuple[str, str]:
    """The user only when `authored_by` proves it, else a named machine or Jarvis — §5"""
    named = _SOURCE_SPEAKERS.get(m.get("source") or "")
    if named:
        return named
    if m.get("source") in UNAUTHORED_SOURCES:
        return _JARVIS_SPEAKER
    return _USER_SPEAKER if m.get("authored_by") == AUTHOR_USER else _JARVIS_SPEAKER


def _message_label(m: dict[str, Any]) -> str:
    """Who is speaking, as a tag over the words — §5"""
    if m.get("direction") != "user_to_agent":
        return "worker → you"
    return _speaker(m)[0]


def _message_event_label(m: dict[str, Any]) -> str:
    """What happened, for the timeline — the same speaker, worded as an event"""
    if m.get("direction") != "user_to_agent":
        return "Worker replied"
    return _speaker(m)[1]


def _message_ref(m: dict[str, Any]) -> dict[str, Any] | None:
    """The conversation turn this entry points at, or None if it has no id"""
    mid = m.get("id")
    if mid is None:
        return None
    return {"kind": "message", "id": mid, "label": "in the conversation"}


def _job_labels(p: dict[str, Any]) -> str:
    """The jobs in a `background_orphaned` payload, named for a person"""
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
    """Which messages the OS has since contradicted, keyed by message id — spec §3"""
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
    """Everything that was SAID about this work order, in the order it was said"""
    turns: list[dict[str, Any]] = []
    for e in events:
        kind = e.get("kind")
        if kind not in ("question_asked", "alarm_reviewed", "alarm_advice"):
            continue
        p = _payload(e)
        # `note` is empty by contract when the supervisor escalates
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
            # BEFORE `content`, so the CLI's generic printer renders the retraction above the words it retracts...
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
    """Merge events and messages into time-ordered entries saying WHAT HAPPENED"""
    entries: list[dict[str, Any]] = []
    seen_assumptions = 0
    for e in events:
        kind = e.get("kind", "")
        payload = _payload(e)
        if kind == "assumption":
            # Rows written before `add_assumption` stored `n` are numbered here
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
