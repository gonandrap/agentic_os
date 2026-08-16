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

import logging
from typing import Any

from . import claude_cli, structured
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


# The learnings block is the one part of Neo's prompt that grows without limit. Every
# `jarvis neo learn` appends an entry IN FULL, `learnings_limit` bounds the row count and
# nothing bounded the size, and unlike the project knowledge base — which ships workers an
# index and lets them fetch what they need — Neo cannot look anything up: its calls run
# headless with the question as their only input, so an index would be no index at all.
# Measured on the production ledger, 16 learnings were 23.4k of a 27.2k-character system
# prompt, and that rides on EVERY Neo call — five times over on a panel round.
#
# 20,000 IS A CEILING, NOT A CUT, AND THE DIFFERENCE IS THE POINT. Neo's whole job is to
# spend its own tokens so the user does not spend attention, and at measured production
# rates the entire headless side of the OS — Neo, digests, the panel — is 2.5% of the
# fleet's bill. Trading a third of the user's accumulated rulings for ~1% of spend would
# be a bad deal. What is NOT acceptable is the unbounded case: nothing stopped this block
# reaching 100k, and at that size it is both expensive and unreadable by the model. So the
# budget is set just under today's ledger — it evicts the six oldest entries now and holds
# the line from here.
#
# WATCH THE CLIFF WHEN TUNING THIS. Eviction is by age and the entries differ in size by
# more than 10x (479 chars against 5,266 on the same ledger), so one bloated learning can
# displace ten good ones and a small budget change can move the count a long way: 24,000
# keeps all 16, 20,000 keeps 10, 16,000 keeps 5. The real remedy is distilling the giants
# rather than raising this number — filed as a backlog item.
LEARNINGS_CHAR_BUDGET = 20000


def render_learnings(rows: list[dict[str, Any]],
                     budget: int = LEARNINGS_CHAR_BUDGET) -> list[str]:
    """The learnings block, capped in characters, newest kept.

    OLDEST-FIRST TRUNCATION, for the same reason `NeoStore.learnings` returns rows
    oldest-first: the block is append-only, so an ordinary new learning extends the
    cached prompt prefix instead of rewriting it. Dropping from the FRONT is what keeps
    that true — trimming the newest would move the boundary on every single addition.
    Going over budget shifts the prefix once, exactly as overflowing `learnings_limit`
    already did.

    THE OMISSION IS STATED, and it is stated LAST. Silence would be the worse failure:
    the persona above tells Neo that a budget which quietly truncates something the user
    mandated is worse than no budget, and a ruling that vanished without trace is that
    failure with no way to notice it. Last, because the note is the only line whose text
    changes when the drop count changes — everything above it stays byte-identical and
    stays cached.

    THE NEWEST ENTRY IS ALWAYS KEPT, even alone over budget. A learning the user recorded
    a minute ago is the likeliest one the next question turns on, and a block that came
    back empty because a single entry was too long would be a silent, total regression
    dressed up as a cap.
    """
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
    """Persona + learnings. Byte-stable across questions (per project and kind) so
    consecutive headless calls of the same kind share a cached prompt prefix.

    Each kind gets its own persona, and for the same reason in both cases: the general
    answerer persona is told to escalate anything that publishes or touches production,
    which is right for open questions and would send every release — and every plan
    whose children mention shipping — straight to the user, which is the attention cost
    both features exist to remove.
    """
    from .gates import REVIEWER_PERSONA
    from .plans import PLAN_REVIEWER_PERSONA

    persona = {"approval": REVIEWER_PERSONA, "plan": PLAN_REVIEWER_PERSONA}.get(
        kind, PERSONA)
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
        parts.append(f"Work order context:\n{q['context']}")
    parts += ["", f"Worker question:\n{q['question']}"]
    return "\n".join(parts)


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


def _validate_verdict(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise a parsed Neo reply into the verdict dict, or raise.

    `escalate` is the field that says this is a Neo verdict at all rather than some other
    JSON object the model happened to emit, so its absence is a bad shape, not a default.
    """
    if "escalate" not in data:
        raise structured.InvalidOutput("no `escalate` field in Neo's reply")
    verdict = _gate_verdict(data)
    return {
        "escalate": bool(data.get("escalate")),
        "answer": str(data.get("answer") or ""),
        "reason": str(data.get("reason") or ""),
        "verdict": verdict,
        "approve": verdict == "approved",
        "dispatch": parse_dispatch(data.get("dispatch")),
        # Carried through unvalidated on purpose: whether a proposed exemption may exist
        # is decided by `gate_rules.propose_exemption`, against canary commands this layer
        # knows nothing about. Truncating it is the only judgement made here, and that is
        # about the size of a database column, not the content of a rule.
        "exempt_pattern": str(data.get("exempt_pattern") or "")[:400],
    }


def _unparseable_verdict(raw: str) -> dict[str, Any]:
    """The fail-safe: output Neo cannot be held to becomes an escalation to the user.

    Deliberately NOT a retry. An answer nobody can read must never reach a worker as an
    answer, and asking again would spend a call to find that out — so this path fails
    toward the user's attention, which is the failure the OS can recover from.
    """
    return {"escalate": True, "answer": "", "approve": False, "verdict": "denied",
            "dispatch": None,
            "reason": f"unparseable Neo output: {(raw or '')[:120]}"}


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
    return structured.coerce(raw, _validate_verdict, on_invalid=_unparseable_verdict)


def answer_question(store: NeoStore, q: dict[str, Any], model: str,
                    learnings_limit: int = 50, timeout: int = 300,
                    record: Any = None) -> dict[str, Any]:
    """One Neo call for one claimed question. Returns the parsed verdict.

    `record(kind, usage=…, …)` is the accounting seam (`agent_usage.record` by default):
    answering a worker's question costs tokens, and they belong to the work order that
    asked. Recorded before the reply is parsed, because an unparseable answer was paid
    for just the same.
    """
    from . import agent_usage
    from .paths import ensure_home

    record = record or agent_usage.record
    system = build_system_prompt(store, q["project"], learnings_limit,
                                 kind=q.get("kind") or "question")
    prompt = build_question_prompt(q)
    result = claude_cli.run_headless_result(
        prompt, system_prompt=system, model=model, timeout=timeout,
        # Neutral cwd: running from a project dir would pull that repo's
        # CLAUDE.md/context into Neo's prompt (and break prefix stability).
        cwd=ensure_home(),
        # This call records itself, three lines down, with the question it answered.
        # Leaving the transport's own attribution on would double-count it whenever the
        # daemon's environment happens to carry a work order — see `claude_cli`.
        attribute=False,
    )
    record("neo_answer", usage=result, project=q.get("project") or "",
           wo_id=q.get("wo_id") or "", label=q.get("kind") or "question",
           model=result.model or model, question_id=q.get("id"))
    return parse_verdict(result.text)


def drain_queue(store: NeoStore, model: str, learnings_limit: int = 50,
                deliver: Any = None, max_questions: int = 50,
                answer: Any = None) -> list[dict[str, Any]]:
    """Answer every queued question in FIFO order, back-to-back.

    `deliver(question, verdict)` is called per question with the outcome — the
    daemon uses it to route answers to workers and escalations to the user. The
    drain is sequential BY DESIGN: ordering + tight spacing keep Neo's shared
    prompt prefix warm in the Anthropic cache.

    `answer(store, question, model, learnings_limit) -> verdict` is HOW a question gets
    answered, defaulting to `answer_question` — one headless call, one agent. It is the
    seam the multi-agent panel is injected through (`daemon._neo_drain` passes
    `panel.decide` bound to the catalog when the panel is enabled).

    THE SEAM IS A KEYWORD RATHER THAN AN IMPORT, AND THAT IS THE WHOLE POINT. The design
    says "`answer_question` becomes a caller of the panel" and "if the panel's first seat
    fails, fall back to today's single-agent path" — which taken literally is `neo`
    importing `panel` while `panel` imports `neo`, an import cycle in a codebase that has
    none. This module must never know that panels exist; falling back to the single agent
    is then simply not passing `answer=`.
    """
    answer = answer or answer_question
    results = []
    for _ in range(max_questions):
        q = store.claim_next()
        if q is None:
            break
        try:
            verdict = answer(store, q, model, learnings_limit)
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
            # `text`, not `answer`: `answer` is the injected answerer above, and rebinding
            # it here made the second question of every drain call a string.
            text = verdict["answer"]
            if q.get("kind") in ("approval", "plan"):
                # A ruling lives in `verdict`, not in prose. Record it as the answer too
                # so `jarvis neo show` and the review surfaces read plainly.
                text = (verdict.get("verdict") or "denied").upper()
            store.record_answer(q["id"], text, answered_by="neo",
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
