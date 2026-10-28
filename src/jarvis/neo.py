"""Neo — the OS-level answerer agent"""

from __future__ import annotations

import logging
from typing import Any

from . import claude_cli, db, structured
from .neo_store import ESCALATION_CAUSES_CHOSEN, NeoStore

log = logging.getLogger("neo")

# Keep in sync with the OPERATION.md / worker-contract wording: workers are told the answer arrives...
ANSWER_PREFIX = "[Neo, answering for the user]"

PERSONA = """You are Neo, the user's delegate inside the Jarvis agentic OS.

Worker agents across the user's projects ask questions when they need a human
decision. Your job is to answer EXACTLY as the user would — their priorities,
their taste, their risk tolerance — so the user's attention stays free.

Rules:
- Answer decisively and concretely. Workers need a decision, not a discussion.
- Ground every answer in the learnings below when they apply; they are distilled
  from the user's actual corrections and are authoritative about how the user thinks.
- ESCALATE (do not answer) when the question involves: production systems or live
  credentials; spending money; deleting or publishing anything; legal/people matters;
  or a genuine preference you have no learning about and cannot infer.

LENGTH IS CAPPED. The user reads these answers to stay across the fleet, so an
answer they will not finish is an answer that did not land.
- When you AGREE with what the worker recommended: name the option and give ONE LINE
  of explanation. Nothing more. Do not restate their reasoning back at them.
- When you OVERRIDE the worker — a different option, or conditions attached — you get
  AT MOST 50 WORDS of explanation in total, however many points you are making.
- `reason` is always one line, on answers and escalations alike.
- These are budgets for your EXPLANATION, not for the decision: state every call the
  worker asked you to make. Cut the justification, never the answer.
- The worker can ask again. A follow-up costs it about a minute; an answer the user
  skims past costs more. Say so if you cut something they may need.
- EXEMPT from the budgets: wording a learning below requires you to state VERBATIM.
  When a learning fixes the words, quote them in full and do not count it against the
  50 words — a budget that silently truncates a phrase the user mandated is worse than
  no budget.

WHEN THE LEDGER CONTRADICTS ITSELF, DISPATCH A CLEANUP. The learnings below and the
project knowledge base are the OS's memory, and they are append-only: a superseded
ruling stays visible next to the one that replaced it until someone writes the
correction. If, while answering, you find that two entries genuinely conflict, or that
one is stale, or that the worker's question only arose because the record is unclear,
add a `dispatch` object to your JSON. That files a work order to fix the record, ALREADY
APPROVED by you — the worker it goes to will not come back to ask whether it may.
- Only for a contradiction you can point at. Not for a gap, not for a record you merely
  wish were fuller, and never as a way to defer the question you were actually asked.
- Answer the worker's question in `answer` regardless. The cleanup is separate work.
- `description` is the whole brief: which entries conflict, quoted; which one the user
  actually settled on and when; what the corrected record should say. The cleanup
  worker sees only that text. The remedy is to APPEND a superseding entry — the stores
  have no retraction — so say plainly what it should say.

Output STRICT JSON, nothing else:
  {"escalate": false, "answer": "<the decision, addressed to the worker — 1 line of explanation if you agree with its recommendation, 50 words max if you override it>", "reason": "<one line why>"}
  or
  {"escalate": true, "answer": "", "reason": "<one line why the user must decide>"}
An escalation may carry one optional label, which is grouped and reported:
  "cause": "<on an escalation, name the cause from this list; omit it if none fits: \
high-stakes | no-learning-applies>"
Either may carry one optional cleanup dispatch:
  "dispatch": {"title": "<short: which record is wrong>", "description": "<the full brief>"}
`dispatch` is a FIELD of that one JSON object, never a second object after it (issue #1098)."""


# The learnings block is the one part of Neo's prompt that grows without limit
LEARNINGS_CHAR_BUDGET = 20000


def render_learnings(rows: list[dict[str, Any]],
                     budget: int = LEARNINGS_CHAR_BUDGET) -> list[str]:
    """The learnings block, capped in characters, newest kept"""
    if not rows:
        return ["(none yet — escalate when unsure)"]
    lines = [f"- [{r['project'] or 'global'}] {r['content']}" for r in rows]
    kept: list[str] = []
    spent = 0
    for line in reversed(lines):  # newest first, so the oldest fall off the end
        if kept and spent + len(line) > budget:
            break
        spent += len(line)
        kept.append(line)
    kept.reverse()
    dropped = len(lines) - len(kept)
    if dropped:
        kept.append(
            f"({dropped} older learning{'s' if dropped > 1 else ''} not shown — this "
            f"block is capped at {budget} characters. Ask the user rather than assume "
            f"nothing was ever ruled on a point you cannot see.)")
    return kept


def build_system_prompt(store: NeoStore, project: str, learnings_limit: int = 50,
                        kind: str = "question",
                        learnings_chars: int = LEARNINGS_CHAR_BUDGET) -> str:
    """Persona + learnings"""
    from .autoreview import ASSUMPTION_REVIEWER_PERSONA
    from .findings import FINDINGS_REVIEWER_PERSONA
    from .gates import REVIEWER_PERSONA
    from .plans import PLAN_REVIEWER_PERSONA
    from .supervisor import ALARM_REVIEWER_PERSONA

    persona = {"approval": REVIEWER_PERSONA, "plan": PLAN_REVIEWER_PERSONA,
               "alarm": ALARM_REVIEWER_PERSONA,
               "assumption": ASSUMPTION_REVIEWER_PERSONA,
               "findings": FINDINGS_REVIEWER_PERSONA}.get(kind, PERSONA)
    parts = [persona, "", "# Learnings (from the user's reviews of your past answers)"]
    parts += render_learnings(store.learnings(project, limit=learnings_limit),
                              budget=learnings_chars)
    return "\n".join(parts)


def build_question_prompt(q: dict[str, Any]) -> str:
    parts = [
        f"Project: {q['project']}",
        f"Work order: {q['wo_id']}",
    ]
    if q.get("context"):
        # A plan question's context IS the subject — the spec being judged (§5)
        label = {"plan": "Spec under review",
                 "findings": "Improvement order"}.get(q.get("kind") or "",
                                                      "Work order context")
        parts.append(f"{label}:\n{q['context']}")
    parts += ["", f"Worker question:\n{q['question']}"]
    return "\n".join(parts)


# What Neo may write in `verdict`, and the status it means
_VERDICT_ALIASES = {
    "approve": "approved", "approved": "approved",
    # A findings verdict says accept/reject; it means what approve/deny means everywhere else
    "accept": "approved", "accepted": "approved",
    "deny": "denied", "denied": "denied", "reject": "denied", "rejected": "denied",
    "dismiss": "dismissed", "dismissed": "dismissed",
}


def _gate_verdict(data: dict[str, Any]) -> str:
    """The gate verdict Neo reached: `approved`, `denied` or `dismissed`"""
    raw = data.get("verdict")
    if isinstance(raw, str):
        verdict = _VERDICT_ALIASES.get(raw.strip().lower())
        if verdict:
            return verdict
    return "approved" if bool(data.get("approve", False)) else "denied"


def _verdict_stated(data: dict[str, Any]) -> bool:
    """Did Neo actually RULE, or is `denied` only what `_gate_verdict` fell back to?"""
    raw = data.get("verdict")
    if isinstance(raw, str) and _VERDICT_ALIASES.get(raw.strip().lower()):
        return True
    return isinstance(data.get("approve"), bool)


def parse_dispatch(data: Any) -> dict[str, str] | None:
    """Normalise the optional `dispatch` block"""
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    return {"title": title[:200],
            "description": str(data.get("description") or "").strip()}


def _escalation_cause(raw: Any) -> str:
    """The model's `cause` label, or `""` — §2 of docs/superpowers/specs/2026-10-01-neo-observability.md"""
    cause = str(raw or "").strip().lower()
    return cause if cause in ESCALATION_CAUSES_CHOSEN else ""


def _validate_verdict(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise a parsed Neo reply into the verdict dict, or raise"""
    if "escalate" not in data:
        raise structured.InvalidOutput("no `escalate` field in Neo's reply")
    verdict = _gate_verdict(data)
    return {
        "escalate": bool(data.get("escalate")),
        "answer": str(data.get("answer") or ""),
        "reason": str(data.get("reason") or ""),
        "verdict": verdict,
        "approve": verdict == "approved",
        "verdict_stated": _verdict_stated(data),
        "dispatch": parse_dispatch(data.get("dispatch")),
        # Carried through unvalidated on purpose: whether a proposed exemption may exist is decided by...
        "exempt_pattern": str(data.get("exempt_pattern") or "")[:400],
        # `exempt_pattern`'s treatment, for the same reason one kind along: what counts as a high-stakes...
        "stakes": str(data.get("stakes") or "")[:20].strip().lower(),
        # Read exactly as `stakes` is — normalised (docs/superpowers/specs/2026-10-01-neo-observability.md)
        "cause": _escalation_cause(data.get("cause")),
        # A findings verdict's rating on the steward's scale
        "severity": str(data.get("severity") or "")[:20].strip().lower(),
        # What a `validation` verdict asks the OS to do; that kind decides what it may be
        "action": str(data.get("action") or "")[:20].strip().lower(),
    }


#: Prefix of the `reason` a synthetic escalation carries
UNPARSEABLE_PREFIX = "unparseable Neo output: "


def _unparseable_verdict(raw: str) -> dict[str, Any]:
    """The fail-safe: output Neo cannot be held to becomes an escalation to the user"""
    return {"escalate": True, "answer": "", "approve": False, "verdict": "denied",
            "verdict_stated": False, "dispatch": None,
            # The one FAILED-class member written in this module
            "cause": "unparseable-reply",
            "reason": f"{UNPARSEABLE_PREFIX}{(raw or '')[:120]}"}


def _fold_split_reply(raw: str) -> dict[str, Any]:
    """Last chance before the fail-safe: one verdict split across two JSON objects (#1098)"""
    objects = structured.parse_json_objects(raw)
    if len(objects) < 2:
        return _unparseable_verdict(raw)
    ruling = [o for o in objects if "escalate" in o]
    if len(ruling) != 1:
        return _unparseable_verdict(raw)
    verdict = dict(ruling[0])
    if verdict.get("dispatch") is None:
        for other in objects:
            if other is not ruling[0] and other.get("dispatch") is not None:
                verdict["dispatch"] = other["dispatch"]
                break
    try:
        return _validate_verdict(verdict)
    except Exception:
        return _unparseable_verdict(raw)


def parse_verdict(raw: str) -> dict[str, Any]:
    """Parse Neo's strict-JSON reply, tolerating fenced or chatty output"""
    return structured.coerce(raw, _validate_verdict, on_invalid=_fold_split_reply)


def answer_question(store: NeoStore, q: dict[str, Any], model: str,
                    learnings_limit: int = 50, timeout: int = 300,
                    record: Any = None) -> dict[str, Any]:
    """One Neo call for one claimed question"""
    from . import agent_usage
    from .paths import ensure_home

    record = record or agent_usage.record
    system = build_system_prompt(store, q["project"], learnings_limit,
                                 kind=q.get("kind") or "question")
    prompt = build_question_prompt(q)
    result = claude_cli.run_headless_result(
        prompt, system_prompt=system, model=model, timeout=timeout,
        # Neutral cwd: running from a project dir would pull that repo's CLAUDE.md/context into Neo's prompt...
        cwd=ensure_home(),
        # This call records itself, three lines down, with the question it answered
        records_itself="neo_answer",
    )
    record("neo_answer", usage=result, project=q.get("project") or "",
           wo_id=q.get("wo_id") or "", label=q.get("kind") or "question",
           model=result.model or model, question_id=q.get("id"))
    verdict = parse_verdict(result.text)
    if verdict["reason"].startswith(UNPARSEABLE_PREFIX):
        # THE FULL REPLY, ONCE, HERE
        log.warning("neo question %s: reply could not be parsed (%d chars) — full raw "
                    "reply follows:\n%s", q.get("id"), len(result.text or ""),
                    result.text or "")
    # THE MODEL THAT ACTUALLY ANSWERED, as the transport reported it
    return {**verdict, "model": result.model or model}


def drain_queue(store: NeoStore, model: str, learnings_limit: int = 50,
                deliver: Any = None, max_questions: int = 50,
                answer: Any = None, unreachable: Any = None,
                refused: Any = None) -> list[dict[str, Any]]:
    """Answer every queued question in FIFO order, back-to-back (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §2.)"""
    answer = answer or answer_question
    results = []
    reached = False
    for _ in range(max_questions):
        q = store.claim_next()
        if q is None:
            break
        try:
            verdict = answer(store, q, model, learnings_limit)
        except claude_cli.UsageLimitError as e:
            # BEFORE the generic outage below (#235)
            from .worker_session import RATE_LIMIT_FALLBACK_DELAY

            reopens = e.limit.reset_at or (db.now() + RATE_LIMIT_FALLBACK_DELAY)
            store.hold_claim(q["id"], e.limit.message, reopens)
            log.info("neo question %s held until the usage window reopens", q["id"])
            results.append({"question": q, "verdict": None, "outcome": "held"})
            continue
        except claude_cli.PromptTooLargeError as e:
            # BEFORE the generic outage below (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
            outcome = store.release_claim(q["id"], f"prompt too large: {e}",
                                          max_attempts=0)
            log.error("neo question %s was refused before it was sent: %s", q["id"], e)
            if refused:
                refused(q, e)
            results.append({"question": q, "verdict": None, "outcome": outcome})
            continue
        except claude_cli.InputTooLargeError as e:
            # BEFORE the generic outage below (docs/superpowers/specs/2026-09-26-a-prompt-too-big-for-argv.md)
            outcome = store.release_claim(q["id"], f"input too large: {e}",
                                          max_attempts=0)
            log.error("neo question %s could not be sent: %s", q["id"], e)
            if unreachable:
                unreachable(q, f"input too large: {e}")
            results.append({"question": q, "verdict": None, "outcome": outcome})
            continue
        except claude_cli.ClaudeCliError as e:
            # A CALL THAT NEVER HAPPENED IS NOT A VERDICT
            outcome = store.release_claim(q["id"], str(e))
            log.error("neo could not be reached for question %s (%s): %s",
                      q["id"], outcome, e)
            if outcome == "unreachable" and unreachable:
                unreachable(q, str(e))
            results.append({"question": q, "verdict": None, "outcome": outcome})
            continue
        if not reached:
            # A model replied, so the transport works: re-ask what an outage stranded, once per drain
            reached = True
            revived = store.revive_unreachable()
            if revived:
                log.info("neo answered again: re-queued unreachable question(s) %s", revived)
        if verdict["escalate"]:
            # Spec §2 of docs/superpowers/specs/2026-10-01-neo-observability.md
            store.mark(q["id"], "escalated", reason=verdict["reason"],
                       cause=verdict.get("cause") or "")
            log.info("neo escalated question %s: %s", q["id"], verdict["reason"])
        else:
            # `text`, not `answer`: `answer` is the injected answerer above
            text = verdict["answer"]
            if q.get("kind") in ("approval", "plan", "findings"):
                # A ruling lives in `verdict`, not in prose
                text = (verdict.get("verdict") or "denied").upper()
            store.record_answer(q["id"], text, answered_by="neo",
                                reason=verdict["reason"])
            log.info("neo answered question %s (%s)", q["id"], q.get("kind") or "question")
        if deliver:
            deliver(q, verdict)
        results.append({"question": q, "verdict": verdict,
                        "outcome": "escalated" if verdict["escalate"] else "answered"})
    return results


def learning_from_review(q: dict[str, Any], feedback: str) -> str:
    """Distill a corrected answer into a learning Neo will see next time"""
    return (f"When a worker asked: \"{q['question'][:200]}\" — I answered: "
            f"\"{(q.get('answer') or '')[:200]}\". The user corrected me: {feedback}")


#: What a no-reason ruling says instead of a reason, and what makes it dedupable
NO_REASON_RULING = "no reason given — the user decided it as the worker recorded it"


def learnings_from_assumption_review(wo: dict[str, Any], assumptions: list[dict[str, Any]],
                                     accepted: bool, feedback: str, *,
                                     false_alarms: frozenset[int] = frozenset(),
                                     tightened: bool = False) -> list[str]:
    """The user's ruling as RULES Neo applies to the next assumption of the class (io-51469add)"""
    from .autoreview import assumption_class

    by_class: dict[str, list[dict[str, Any]]] = {}
    for a in assumptions:
        by_class.setdefault(assumption_class(str(a.get("content") or ""),
                                             tightened=tightened), []).append(a)
    feedback = (feedback or "").strip()
    rules = []
    for cls, rows in by_class.items():
        if not feedback:
            # Deterministic text, so the caller can dedupe it: one rule per class, not one per order
            verdict = "accepted it unchanged" if accepted else "rejected it"
            rules.append(f"RULE (from the user's assumption reviews): for {cls}, the user "
                         f"{verdict} — {NO_REASON_RULING}.")
            continue
        verdict = "accepted" if accepted else "rejected"
        alarm = (" The user called the high-stakes net's match on it a FALSE ALARM."
                 if any(int(a.get("id") or 0) in false_alarms for a in rows) else "")
        example = "; ".join(f"\"{str(a.get('content') or '')[:200]}\"" for a in rows[:3])
        rules.append(f"RULE (from the user's review of {wo.get('id') or 'a work order'}): "
                     f"for {cls}, the user {verdict} it. Reason: {feedback}.{alarm} "
                     f"Example, on \"{str(wo.get('title') or '')[:120]}\": {example}")
    return rules
