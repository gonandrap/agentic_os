"""Gated actions — privileged operations a worker may attempt but not unilaterally do.

A worker developing the OS needs to be able to *ship*: merge its PR, cut a release.
The first cut of that guard rail was a hard `deny` rule in the catalog, which is a wall,
not a gate — it stops the action and there is no way through it short of the user editing
their catalog. Progress on the OS stalled behind exactly that wall.

This module replaces the wall with a gate. A worker attempts the real command; the
PreToolUse hook classifies it, blocks *this* attempt, and files an approval request that
Neo reviews independently. When Neo approves, the worker is resumed and the retry goes
through. The user's attention is spent only when Neo declines to decide.

## Why the hook cannot simply wait

Two hard constraints shape the design, and both are load-bearing:

1. **A PreToolUse hook cannot lift a `deny` rule.** From the Claude Code permission
   docs: *"Hook decisions don't bypass permission rules. Claude Code evaluates deny and
   ask rules regardless of what a PreToolUse hook returns: a matching deny rule blocks
   the call."* So a gated action must NOT also be denied in the project's settings — the
   deny wins and the gate can never open. `INV-GATE-DENY-CONFLICT` (invariants.py)
   exists solely to catch that misconfiguration, because it fails silently otherwise:
   the request reaches Neo, Neo approves, and the retry is still blocked.
2. **The hook is synchronous and short (30s); a Neo review takes minutes.** So the hook
   never waits for a verdict. It denies the attempt with an explanation, and approval
   arrives later through the ordinary message-delivery path — the same one
   `jarvis wo ask` already uses. The worker retries and the second attempt is allowed.

## Scope of a grant

An approval authorises one command, for one work order, for a short window
(`GRANT_TTL_SECONDS`), a bounded number of times (`GRANT_MAX_USES`). "Merge PR #31" must
not silently become "and also cut a release", or "and merge whatever you like tomorrow".
Matching is on the exact command string for that reason: a grant is a receipt for a
specific act, not a capability the worker keeps.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .project_store import ProjectStore

# How long an approval stays usable, and how many attempts it covers. The window is
# short because it is a receipt for an act the worker is about to perform, not standing
# authority. More than one use because a real `gh pr merge` can fail on a transient
# (out-of-date branch, failing check) and forcing a fresh Neo round trip for the retry
# is the sort of friction this whole feature exists to remove.
GRANT_TTL_SECONDS = 3600
GRANT_MAX_USES = 3

APPROVAL_STATUSES = ("pending", "approved", "denied", "expired")


@dataclass(frozen=True)
class GateKind:
    """One class of privileged action, and how to recognise an attempt at it."""

    name: str
    # What the action does, in the terms whoever reviews it needs. Rendered into the
    # request Neo sees, so it must read as a claim about consequences.
    summary: str
    patterns: tuple[str, ...]
    # Literals that, appearing in a project's `permissions.deny` rules, mean the deny
    # will shadow this gate — the call is blocked before the hook's `allow` is even
    # consulted, so approval can never take effect. Listed explicitly rather than
    # derived from `patterns`, because deriving them turns common words like "start"
    # into false alarms on rules like `Bash(npm start*)`.
    conflict_markers: tuple[str, ...] = ()


# Default recognisers. Deliberately broad: a false positive costs one Neo review, a
# false negative lets a worker ship unreviewed. Anchored on the verbs that actually
# publish something, so ordinary work (pushing a feature branch, force-pushing one's own
# PR branch, running tests) never trips a gate.
KINDS: tuple[GateKind, ...] = (
    GateKind(
        name="pr_merge",
        summary="merge a pull request into the default branch",
        patterns=(
            r"\bgh\s+pr\s+merge\b",
            r"\bgh\s+api\b[^\n]*\bpulls/\d+/merge\b",
        ),
        conflict_markers=("gh pr merge", "pulls/"),
    ),
    GateKind(
        name="release",
        summary="cut a release and deploy it (this reaches the live production fleet)",
        patterns=(
            r"shipit",                      # the OS's own release script
            r"\bgh\s+release\s+create\b",
            r"\bnpm\s+publish\b",
            r"\b(twine|uv)\s+publish\b",
            r"\bgit\s+push\b[^\n]*--(tags|follow-tags)\b",
        ),
        conflict_markers=("shipit", "gh release", "npm publish", "twine publish",
                          "uv publish", "--tags", "--follow-tags"),
    ),
    GateKind(
        name="service_restart",
        summary="restart or stop a system service (this interrupts the running fleet)",
        patterns=(
            r"\bsystemctl\b[^\n]*\b(restart|stop|start|disable|enable)\b",
        ),
        conflict_markers=("systemctl",),
    ),
    GateKind(
        name="push_protected",
        summary="push directly to a protected branch, bypassing review",
        patterns=(
            r"\bgit\s+push\b[^\n]*\b(origin\s+)?(main|master)\b",
            r"\bgit\s+push\b[^\n]*\bHEAD:(refs/heads/)?(main|master)\b",
        ),
        conflict_markers=("git push",),
    ),
)

KIND_NAMES = tuple(k.name for k in KINDS)


@dataclass(frozen=True)
class GateConfig:
    """Which gates are live for a project, plus any extra recognisers it needs.

    Empty `enabled` means gating is off, which is the default for every project. Gates
    change what a worker is allowed to do, so they are opt-in per project: switching
    them on fleet-wide would put a Neo review in front of every `gh pr merge` in every
    repo, which trades one bottleneck for a slower one.
    """

    enabled: frozenset[str] = frozenset()
    extra_patterns: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.enabled)

    def to_json(self) -> str:
        return json.dumps({
            "enabled": sorted(self.enabled),
            "patterns": {k: list(v) for k, v in sorted(self.extra_patterns.items())},
        }, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> GateConfig:
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return cls()
        return cls.parse(data)

    @classmethod
    def parse(cls, data: Any) -> GateConfig:
        """Build from catalog JSON. Unknown gate names are an error worth raising at
        catalog-load time, not a silently-ignored typo that leaves a gate open."""
        if data in (None, False):
            return cls()
        if data is True:
            return cls(enabled=frozenset(KIND_NAMES))
        if isinstance(data, list):
            data = {"enabled": data}
        if not isinstance(data, dict):
            raise ValueError('"gates" must be a bool, a list of gate names, or an object')

        enabled_raw = data.get("enabled", [])
        if enabled_raw is True:
            enabled = set(KIND_NAMES)
        elif isinstance(enabled_raw, list):
            enabled = {str(n) for n in enabled_raw}
        else:
            raise ValueError('"gates.enabled" must be a list of gate names, or true')
        unknown = sorted(enabled - set(KIND_NAMES))
        if unknown:
            raise ValueError(
                f"unknown gate(s) {unknown} — known gates: {list(KIND_NAMES)}"
            )

        patterns_raw = data.get("patterns", {}) or {}
        if not isinstance(patterns_raw, dict):
            raise ValueError('"gates.patterns" must be an object of gate -> [regex]')
        extra: dict[str, tuple[str, ...]] = {}
        for name, pats in patterns_raw.items():
            if name not in KIND_NAMES:
                raise ValueError(
                    f"unknown gate {name!r} in gates.patterns — known: {list(KIND_NAMES)}"
                )
            if not isinstance(pats, list):
                raise ValueError(f'"gates.patterns.{name}" must be a list of regexes')
            for p in pats:
                try:
                    re.compile(str(p))
                except re.error as e:
                    raise ValueError(f"gates.patterns.{name}: bad regex {p!r}: {e}") from e
            extra[name] = tuple(str(p) for p in pats)
        return cls(enabled=frozenset(enabled), extra_patterns=extra)


@dataclass(frozen=True)
class GatedAction:
    """A command recognised as an attempt at a privileged action."""

    kind: str
    summary: str
    command: str
    matched: str  # the pattern that fired — shown to the reviewer, and to the user


# Quoted spans are data, not code. `'…'` is literal; `"…"` may interpolate, but a gated
# verb inside it is still an argument, not a command — unless a shell re-parses it, which
# is what _SHELL_INVOKER catches below.
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", re.DOTALL)

# …with one exception: these hand their quoted payload back to a shell to execute, so
# there the quotes are code after all. Scan such commands whole.
_SHELL_INVOKER = re.compile(
    r"\b(?:ba|z|k|da|a)?sh\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*c\b|\beval\b|\bxargs\b",
    re.IGNORECASE,
)


def scannable(command: str) -> str:
    """The part of `command` that could actually *execute* something.

    Blanks quoted arguments so that merely naming a privileged action doesn't trip its
    gate — `git commit -m "document systemctl restart"` writes a commit message, and
    `jarvis learn add "…never run the release script…"` writes a note. Both used to be
    gated as the real thing, which cost a Neo review and stalled the worker for nothing.

    A quoted payload IS code when something re-parses it (`sh -c`, `eval`, `xargs`), so
    those are scanned whole. Erring that way is deliberate: a spurious gate costs one
    review, a missed one ships unreviewed code.
    """
    if _SHELL_INVOKER.search(command):
        return command
    # Replace rather than delete, so neighbouring tokens can't fuse into a false match.
    return _QUOTED.sub(" ", command)


def classify(command: str, config: GateConfig) -> GatedAction | None:
    """The gate this Bash command trips, or None.

    Matches the whole command rather than parsed segments: a gated action hidden in a
    pipeline, a subshell or behind `&&` is the same action, and a classifier that only
    understands well-formed simple commands is a classifier with a bypass. Quoted
    arguments are blanked first — see `scannable`.
    """
    if not command or not config.enabled:
        return None
    haystack = scannable(command)
    for kind in KINDS:
        if kind.name not in config.enabled:
            continue
        for pattern in (*kind.patterns, *config.extra_patterns.get(kind.name, ())):
            if re.search(pattern, haystack, re.IGNORECASE):
                return GatedAction(kind=kind.name, summary=kind.summary,
                                   command=command.strip(), matched=pattern)
    return None


# -- misconfiguration that silently shuts a gate --------------------------------------

_BASH_RULE = re.compile(r"^Bash\((.*)\)$", re.IGNORECASE)


def _deny_rule_core(rule: str) -> str | None:
    """The literal part of a `Bash(...)` deny rule, or None for non-Bash rules."""
    m = _BASH_RULE.match(rule.strip())
    if not m:
        return None
    return m.group(1).strip().strip("*").strip().lower()


def deny_conflicts(config: GateConfig, deny_rules: Iterable[str]
                   ) -> list[tuple[str, str]]:
    """Deny rules that would block an enabled gate. Returns (gate name, rule) pairs.

    This is the one misconfiguration that fails *silently and completely*: the worker
    files a request, Neo reviews it, Neo approves it, the worker retries — and the retry
    is still blocked, because Claude Code evaluates deny rules before it looks at what a
    hook returned. Everything logs success and nothing ships. Hence a dedicated check.
    """
    conflicts: list[tuple[str, str]] = []
    cores = [(rule, _deny_rule_core(rule)) for rule in deny_rules]
    for kind in KINDS:
        if kind.name not in config.enabled:
            continue
        for rule, core in cores:
            if not core:
                continue
            for marker in kind.conflict_markers:
                if marker in core or core in marker:
                    conflicts.append((kind.name, rule))
                    break
    return conflicts


# -- the request Neo reviews ---------------------------------------------------------

# Neo's ordinary persona is told to escalate anything "production-impacting" or that
# "publishes" — correct for open questions, and fatal here: every release would go
# straight to the user and the gate would be a slower version of the wall it replaced.
# Gate reviews therefore get their own mandate, with the standard the user actually
# applies: is this the work order's own reviewed work, shipped the normal way?
REVIEWER_PERSONA = """You are Neo, reviewing a PRIVILEGED ACTION REQUEST inside the \
Jarvis agentic OS.

A worker agent tried to run a command that ships code — merging a pull request, cutting
a release, restarting a service. The command was blocked and handed to you. You decide,
on the user's behalf, whether it goes through.

You are the independent check, not a rubber stamp. The worker wrote the request; it is
an argument for its own case, so verify the claims against the evidence it supplies
rather than accepting the summary.

APPROVE when all of these hold:
- The action is squarely within what the work order was asked to do.
- The change went through the normal path: work landed on a branch, in a pull request,
  with the project's checks or tests reported passing.
- The command matches the stated intent — the PR number, tag or service named is the
  one the request is about, and nothing extra rides along.
- Consequences are recoverable by ordinary means (revert the merge, ship the previous
  release again).

DENY when the request is outside the work order's scope, skips review (a direct push to
a protected branch when a PR was the agreed route), targets something other than what it
claims, or bundles unrelated changes. Say plainly what is wrong; the worker sees your
reason and can fix it and ask again.

ESCALATE to the user, rather than deciding, when:
- Tests or checks are failing, absent, or not mentioned at all.
- The action is irreversible or destructive (deleting a release, rewriting published
  history, dropping data).
- It touches credentials, secrets, billing, or anything user-facing beyond this repo.
- The work order itself is ambiguous about whether shipping was in scope.
- Anything in the request does not add up, including a claim you cannot check.

Escalating is the safe answer and costs only a little of the user's time. Approving
something that should not ship costs much more. When genuinely torn, escalate.

Output STRICT JSON, nothing else:
  {"escalate": false, "approve": true,  "reason": "<one line: what you verified>"}
  {"escalate": false, "approve": false, "reason": "<one line: what is wrong>"}
  {"escalate": true,  "approve": false, "reason": "<one line: why the user must decide>"}"""


def build_request_question(action: GatedAction, wo: dict[str, Any],
                           justification: str, evidence: str = "") -> str:
    """Render the approval request Neo (or the user) reads.

    Whoever decides sees only this text — never the worker's session — so it has to
    carry the case by itself: what is being attempted, under which work order, why the
    worker believes it is ready, and whatever it offered as proof.
    """
    parts = [
        f"PRIVILEGED ACTION REQUEST — gate `{action.kind}`",
        "",
        f"The worker for work order {wo['id']} wants to {action.summary}.",
        "",
        "Exact command it will run (approval authorises this command and nothing else):",
        f"    {action.command}",
        "",
        f"Work order: {wo.get('title') or '(untitled)'}",
    ]
    description = (wo.get("description") or "").strip()
    if description:
        parts += ["Work order description:", description[:1200]]
    parts += ["", "The worker's justification:",
              justification.strip() or "(the worker gave none — treat that as a red flag)"]
    if evidence.strip():
        parts += ["", "Evidence the worker supplied (branch, PR, test results):",
                  evidence.strip()[:2000]]
    parts += [
        "",
        "Decide: approve it, deny it with a reason the worker can act on, or escalate "
        "to the user.",
    ]
    return "\n".join(parts)


# -- filing a request ----------------------------------------------------------------


def file_request(store: ProjectStore, neo: Any, project: str, wo: dict[str, Any],
                 action: GatedAction, justification: str = "", evidence: str = "",
                 max_uses: int = GRANT_MAX_USES) -> tuple[dict[str, Any], dict[str, Any]]:
    """Record an approval request and queue it for review. Returns (approval, question).

    One path for both entry points — the hook (a worker that just ran the command) and
    `jarvis gate request` (a worker making its case first) — so a gate behaves the same
    however it was reached.

    The request rides the existing Neo queue rather than a parallel review pipeline,
    which is what gives it escalation-to-user, `jarvis neo list` and answer delivery
    without reimplementing any of them.
    """
    approval = store.add_approval(
        wo["id"], action.kind, action.command, matched=action.matched,
        justification=justification, evidence=evidence, max_uses=max_uses,
    )
    question = neo.ask(
        project, wo["id"],
        build_request_question(action, wo, justification, evidence),
        context=f"{wo.get('title') or ''}\n{(wo.get('description') or '')[:800]}",
        kind="approval",
    )
    store.link_neo_question(approval["id"], question["id"])
    # The worker has nothing to do until a verdict lands. Saying so keeps the reconciler
    # from reading the idle session as "finished without `jarvis wo finish`" and filing
    # it for review — a gate request is a wait, not an abandonment.
    if wo.get("status") in ("running", "dispatching"):
        store.set_status(wo["id"], "waiting_input")
    return approval, question


# -- deciding ------------------------------------------------------------------------

# What the worker is told when a gate opens. It has to name the command, because a
# grant is scoped to that exact string: a worker that retries a "tidied up" variant
# trips the gate again and cannot understand why.
def approved_message(approval: dict[str, Any], reason: str, by: str) -> str:
    return (
        f"[Gate {approval['id']} APPROVED by {by}] {reason}\n\n"
        f"Run this command again, exactly as written, and it will go through:\n"
        f"    {approval['command']}\n\n"
        f"The approval covers this command only, for the next "
        f"{GRANT_TTL_SECONDS // 60} minutes. Anything else still needs its own request."
    )


def denied_message(approval: dict[str, Any], reason: str, by: str) -> str:
    return (
        f"[Gate {approval['id']} DENIED by {by}] {reason}\n\n"
        f"The command that was blocked:\n    {approval['command']}\n\n"
        f"Do not retry it as-is — it will be blocked again. Address the reason above, "
        f"then either request approval afresh (`jarvis gate request`) or finish the "
        f"work order explaining what is left."
    )


def apply_decision(store: ProjectStore, approval_id: int, approved: bool,
                   reason: str, decided_by: str) -> dict[str, Any]:
    """Record a verdict and queue the worker's resume message.

    Shared by Neo's drain and the user's `jarvis gate approve/deny`, so a gate opened by
    either route leaves identical state behind. Returns the updated approval row.
    """
    approval = store.decide_approval(approval_id, approved=approved, reason=reason,
                                     decided_by=decided_by)
    message = (approved_message if approved else denied_message)(approval, reason, decided_by)
    store.queue_message(approval["wo_id"], message, source="gate")
    store.add_event(approval["wo_id"], "gate_decided", {
        "approval_id": approval_id,
        "decision": "approved" if approved else "denied",
        "by": decided_by,
        "kind": approval["kind"],
        "reason": reason,
    })
    return approval


def summarise(approvals: Iterable[dict[str, Any]]) -> str:
    """One-line rendering of a work order's gate history, for status output."""
    rows = list(approvals)
    if not rows:
        return "no gate requests"
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return ", ".join(f"{n} {status}" for status, n in sorted(by_status.items()))
