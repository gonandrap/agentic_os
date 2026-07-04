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
- Output STRICT JSON, nothing else:
  {"escalate": false, "answer": "<the decision, addressed to the worker>", "reason": "<one line why>"}
  or
  {"escalate": true, "answer": "", "reason": "<one line why the user must decide>"}"""


def build_system_prompt(store: NeoStore, project: str, learnings_limit: int = 50) -> str:
    """Persona + learnings. Byte-stable across questions (per project) so consecutive
    headless calls share a cached prompt prefix."""
    parts = [PERSONA, "", "# Learnings (from the user's reviews of your past answers)"]
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


def parse_verdict(raw: str) -> dict[str, Any]:
    """Parse Neo's strict-JSON reply, tolerating fenced or chatty output.
    Unparseable output is treated as an escalation — never deliver garbage."""
    m = _JSON_RE.search(raw or "")
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and "escalate" in data:
                return {
                    "escalate": bool(data.get("escalate")),
                    "answer": str(data.get("answer") or ""),
                    "reason": str(data.get("reason") or ""),
                }
        except json.JSONDecodeError:
            pass
    return {"escalate": True, "answer": "",
            "reason": f"unparseable Neo output: {(raw or '')[:120]}"}


def answer_question(store: NeoStore, q: dict[str, Any], model: str,
                    learnings_limit: int = 50, timeout: int = 300) -> dict[str, Any]:
    """One Neo call for one claimed question. Returns the parsed verdict."""
    from .paths import ensure_home

    system = build_system_prompt(store, q["project"], learnings_limit)
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
            store.record_answer(q["id"], verdict["answer"], answered_by="neo",
                                reason=verdict["reason"])
            log.info("neo answered question %s", q["id"])
        if deliver:
            deliver(q, verdict)
        results.append({"question": q, "verdict": verdict})
    return results


def learning_from_review(q: dict[str, Any], feedback: str) -> str:
    """Distill a corrected answer into a learning Neo will see next time."""
    return (f"When a worker asked: \"{q['question'][:200]}\" — I answered: "
            f"\"{(q.get('answer') or '')[:200]}\". The user corrected me: {feedback}")
