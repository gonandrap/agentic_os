"""Neo — the OS-level answerer agent.

Workers ask questions (`jarvis wo ask`) instead of stalling on the user. Neo answers
them AS the user: a headless Claude call with a stable persona + the user's accumulated
learnings, question-specific context last.

Token economics drive the design:
  * The persona + learnings travel as the system prompt and are byte-stable across
    calls, so consecutive answers share a cached prefix.
  * The queue drains FIFO and back-to-back — every answer after the first lands
    inside the Anthropic prompt-cache TTL (~5 min), so the shared prefix is a cache
    read, not a re-process.
  * Learnings render append-only (oldest first): user feedback extends the prefix
    instead of rewriting it.

Neo escalates instead of guessing: anything irreversible, credential-touching,
production-impacting, or genuinely unknowable about the user's intent goes to the
user as an attention item.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import claude_cli
from .neo_store import NeoStore

log = logging.getLogger("neo")

# Keep in sync with the OPERATION.md / worker-contract wording: workers are told the
# answer arrives as their next user turn.
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
Either may carry one optional cleanup dispatch:
  "dispatch": {"title": "<short: which record is wrong>", "description": "<the full brief>"}"""


def build_system_prompt(store: NeoStore, project: str, learnings_limit: int = 50,
                        kind: str = "question") -> str:
    """Persona + learnings. Byte-stable across questions (per project and kind) so
    consecutive headless calls of the same kind share a cached prompt prefix.

    Approval requests get the gate-reviewer persona instead of the answerer one: the
    general persona is told to escalate anything that publishes or touches production,
    which is right for open questions and would send every release straight to the user.
    """
    from .gates import REVIEWER_PERSONA

    persona = REVIEWER_PERSONA if kind == "approval" else PERSONA
    parts = [persona, "", "# Learnings (from the user's reviews of your past answers)"]
    rows = store.learnings(project, limit=learnings_limit)
    if not rows:
        parts.append("(none yet — escalate when unsure)")
    for r in rows:
        scope = r["project"] or "global"
        parts.append(f"- [{scope}] {r['content']}")
    return "\n".join(parts)


def build_question_prompt(q: dict[str, Any]) -> str:
    parts = [
        f"Project: {q['project']}",
        f"Work order: {q['wo_id']}",
    ]
    if q.get("context"):
        parts.append(f"Work order context:\n{q['context']}")
    parts += ["", f"Worker question:\n{q['question']}"]
    return "\n".join(parts)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# What Neo may write in `verdict`, and the status it means. Both the bare verb and the
# past participle are accepted: the persona asks for "approve", the database stores
# "approved", and a model that writes the other one is not making a mistake worth
# escalating over.
_VERDICT_ALIASES = {
    "approve": "approved", "approved": "approved",
    "deny": "denied", "denied": "denied", "reject": "denied", "rejected": "denied",
    "dismiss": "dismissed", "dismissed": "dismissed",
}


def _gate_verdict(data: dict[str, Any]) -> str:
    """The gate verdict Neo reached: `approved`, `denied` or `dismissed`.

    Reads the `verdict` field, falling back to the older boolean `approve`. The fallback
    is not politeness — it is required during a release. Neo's persona lives in the
    deployed code but its LEARNINGS live in the production state directory, so the two
    can disagree across an upgrade in either direction: a Neo still emitting the old
    shape must keep working, and a `verdict` this build did not expect must not silently
    open a gate.

    Anything unrecognised falls back to the boolean, which defaults to False. Output that
    says nothing intelligible therefore denies rather than approves: the only way through
    a gate is an explicit yes.
    """
    raw = data.get("verdict")
    if isinstance(raw, str):
        verdict = _VERDICT_ALIASES.get(raw.strip().lower())
        if verdict:
            return verdict
    return "approved" if bool(data.get("approve", False)) else "denied"


def parse_dispatch(data: Any) -> dict[str, str] | None:
    """Normalise the optional `dispatch` block — Neo filing a pre-approved work order
    to correct a self-contradicting ledger (see PERSONA).

    A dispatch with no title is dropped rather than guessed at: the title is what the
    user sees in `jarvis wo list`, and "(untitled)" work orders appearing on their own
    is how a helpful feature becomes noise.
    """
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    return {"title": title[:200],
            "description": str(data.get("description") or "").strip()}


def parse_verdict(raw: str) -> dict[str, Any]:
    """Parse Neo's strict-JSON reply, tolerating fenced or chatty output.
    Unparseable output is treated as an escalation — never deliver garbage.

    `verdict` carries the ruling on an approval request; `approve` is kept alongside it
    as the boolean the old contract used, so callers that only ask "did this open a
    gate" still read correctly. Note that a dismissal leaves `approve` False: it clears
    the command without authorising anything, and code that conflates the two would put
    an approval back into the audit trail.

    `dispatch` is absent on almost every answer; None means "nothing to clean up".
    """
    m = _JSON_RE.search(raw or "")
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and "escalate" in data:
                verdict = _gate_verdict(data)
                return {
                    "escalate": bool(data.get("escalate")),
                    "answer": str(data.get("answer") or ""),
                    "reason": str(data.get("reason") or ""),
                    "verdict": verdict,
                    "approve": verdict == "approved",
                    "dispatch": parse_dispatch(data.get("dispatch")),
                }
        except json.JSONDecodeError:
            pass
    return {"escalate": True, "answer": "", "approve": False, "verdict": "denied",
            "dispatch": None,
            "reason": f"unparseable Neo output: {(raw or '')[:120]}"}


def answer_question(store: NeoStore, q: dict[str, Any], model: str,
                    learnings_limit: int = 50, timeout: int = 300) -> dict[str, Any]:
    """One Neo call for one claimed question. Returns the parsed verdict."""
    from .paths import ensure_home

    system = build_system_prompt(store, q["project"], learnings_limit,
                                 kind=q.get("kind") or "question")
    prompt = build_question_prompt(q)
    raw = claude_cli.run_headless(
        prompt, system_prompt=system, model=model, timeout=timeout,
        # Neutral cwd: running from a project dir would pull that repo's
        # CLAUDE.md/context into Neo's prompt (and break prefix stability).
        cwd=ensure_home(),
    )
    return parse_verdict(raw)


def drain_queue(store: NeoStore, model: str, learnings_limit: int = 50,
                deliver: Any = None, max_questions: int = 50) -> list[dict[str, Any]]:
    """Answer every queued question in FIFO order, back-to-back.

    `deliver(question, verdict)` is called per question with the outcome — the
    daemon uses it to route answers to workers and escalations to the user. The
    drain is sequential BY DESIGN: ordering + tight spacing keep Neo's shared
    prompt prefix warm in the Anthropic cache.
    """
    results = []
    for _ in range(max_questions):
        q = store.claim_next()
        if q is None:
            break
        try:
            verdict = answer_question(store, q, model, learnings_limit)
        except claude_cli.ClaudeCliError as e:
            log.error("neo failed answering question %s: %s", q["id"], e)
            store.mark(q["id"], "failed", reason=str(e))
            verdict = {"escalate": True, "answer": "", "reason": f"neo call failed: {e}",
                       "failed": True}
            if deliver:
                deliver(q, verdict)
            results.append({"question": q, "verdict": verdict})
            continue
        if verdict["escalate"]:
            store.mark(q["id"], "escalated", reason=verdict["reason"])
            log.info("neo escalated question %s: %s", q["id"], verdict["reason"])
        else:
            # An approval's decision lives in `approve`, not in prose. Record it as the
            # answer too so `jarvis neo show` and the review surfaces read plainly.
            answer = verdict["answer"]
            if q.get("kind") == "approval":
                answer = (verdict.get("verdict") or "denied").upper()
            store.record_answer(q["id"], answer, answered_by="neo",
                                reason=verdict["reason"])
            log.info("neo answered question %s (%s)", q["id"], q.get("kind") or "question")
        if deliver:
            deliver(q, verdict)
        results.append({"question": q, "verdict": verdict})
    return results


def learning_from_review(q: dict[str, Any], feedback: str) -> str:
    """Distill a corrected answer into a learning Neo will see next time."""
    return (f"When a worker asked: \"{q['question'][:200]}\" — I answered: "
            f"\"{(q.get('answer') or '')[:200]}\". The user corrected me: {feedback}")


def learning_from_assumption_review(wo: dict[str, Any], assumptions: list[dict[str, Any]],
                                    accepted: bool, feedback: str) -> str:
    """Distill the user's verdict on a work order's assumptions into a learning.

    Neo's other learning source (reviews of its own answers) only fires once Neo is
    already answering. This one fires on the decisions the user is making TODAY, so
    Neo arrives at its first question with the user's taste already in the prompt.
    """
    what = "; ".join(f"\"{a['content'][:200]}\"" for a in assumptions[:5]) or "(none recorded)"
    verdict = "accepted" if accepted else "rejected"
    return (f"On work order \"{wo.get('title', '')[:120]}\" a worker decided: {what}. "
            f"The user {verdict} that: {feedback}")
