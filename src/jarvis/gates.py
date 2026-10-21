"""Gated actions — privileged operations a worker may attempt but not unilaterally do (#31)"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from .gate_rules import (  # re-exported: this is still the module callers import
    AUTO_MERGE,
    KIND_NAMES,
    KINDS,
    PR_MERGE,
    SELF_HEAL,
    GateKind,
    RuleSet,
    reads_only,
    scannable,
)
from .provenance import (
    CONTEXT_LIMIT,
    DESCRIPTION_LIMIT,
    EVIDENCE_LIMIT,
    TITLE_LIMIT,
    WO_DESCRIPTION,
    WO_DESCRIPTION_WHOSE,
    WO_TITLE,
    WO_TITLE_WHOSE,
    Borrowed,
    borrowed_context,
    borrowed_sections,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from .central_store import CentralStore
    from .neo_store import NeoStore
    from .project_store import ProjectStore

log = logging.getLogger(__name__)

__all__ = [
    "APPROVAL_STATUSES", "AWAITING_CASE", "CASE_TTL_CEILING_SECONDS",
    "DEFAULT_CASE_TTL_SECONDS", "GRANT_MAX_USES",
    "GRANT_TTL_SECONDS", "PR_MERGE", "SELF_HEAL", "AUTO_MERGE",
    "GateConfig",
    "CONTEST_HEADER", "CONTEST_NOT_AN_AUTHORISATION",
    "GateKind", "GatedAction", "KINDS", "KIND_NAMES", "NO_CASE_JUSTIFICATION",
    "NO_CASE_PREFIX", "is_no_case", "no_case_justification",
    "REVIEWER_PERSONA", "RuleSet",
    "VERDICTS", "abandoned_message", "abandoned_reason", "amend_request",
    "apply_decision",
    "build_contest_question", "build_request_question", "classify", "contest_command",
    "deny_conflicts", "exits_advice", "file_request", "open_gate", "queue_for_review",
    "asks", "explain_command", "kind_of",
    "pull_request_in",
    "question_text", "reads_only", "render_user_messages", "request_command", "scannable",
    "summarise", "sweep_unargued",
]

# How long an approval stays usable, and how many attempts it covers
GRANT_TTL_SECONDS = 3600
GRANT_MAX_USES = 3

APPROVAL_STATUSES = ("awaiting_case", "pending", "approved", "denied", "dismissed",
                     "expired")

# Filed, recorded, and DELIBERATELY not in front of a reviewer yet: the worker ran the command... (docs/superpowers/specs/2026-09-11-holding-an-unargued-gate.md)
AWAITING_CASE = "awaiting_case"

# How long a held request waits for its case — or its contest — before the OS abandons it
DEFAULT_CASE_TTL_SECONDS = 240.0

#: The clamp ceiling
CASE_TTL_CEILING_SECONDS = 300.0

# The verdicts a reviewer can reach
VERDICTS = ("approved", "denied", "dismissed")

# What the justification says when the hook filed the request because the worker ran the command...
NO_CASE_JUSTIFICATION = ("(none — the worker ran the command directly rather than filing "
                         "a request, so no case was made for it)")

#: Every form of the placeholder opens with this, and no case a worker writes does
NO_CASE_PREFIX = "(none"


def is_no_case(justification: str) -> bool:
    """True when `justification` is a placeholder rather than a case someone made"""
    return not justification.strip() or justification.strip().startswith(NO_CASE_PREFIX)


def no_case_justification(command: str, prior_id: int | None = None) -> str:
    """The placeholder for a request nobody argued — saying only what is established (#233)"""
    from .gate_rules import files_a_claim, list_segments

    pointer = ("" if prior_id is None else
               f" Request {prior_id} on this work order is for the same kind of action; "
               f"the case, if one was made, is there.")
    if any(files_a_claim(command[s:e]) for s, e in list_segments(command)):
        return ("(none recorded. This command FILES a gate request, and the recogniser "
                "matched inside the case it carried, so the OS has NOT established that "
                "the worker ran the action rather than asking for it." + pointer + ")")
    if "jarvis gate " in command:
        return ("(none recorded. This command names `jarvis gate`, so the OS cannot tell "
                "whether the worker was filing a case or running the action, and has "
                "established neither." + pointer + ")")
    if pointer:
        return "(none recorded with this attempt." + pointer + ")"
    return NO_CASE_JUSTIFICATION

# The string every reader of a contested request keys on
CONTEST_HEADER = "CONTESTED GATE MATCH"

# Appended when a reviewer tries to APPROVE a contest
CONTEST_NOT_AN_AUTHORISATION = (
    "(Recorded as a REFUSAL, not an authorisation. This was a contest: the worker argued "
    "that the command performs no privileged action, and made no case for performing one. "
    "If the action is genuine, it needs a real request — `jarvis gate request` — with a "
    "case a reviewer can check.)"
)


@dataclass(frozen=True)
class GateConfig:
    """Which gates are live for a project, plus any extra recognisers it needs"""

    enabled: frozenset[str] = frozenset()
    extra_patterns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    case_ttl_seconds: float = DEFAULT_CASE_TTL_SECONDS

    def __bool__(self) -> bool:
        return bool(self.enabled)

    def to_json(self) -> str:
        return json.dumps({
            "enabled": sorted(self.enabled),
            "patterns": {k: list(v) for k, v in sorted(self.extra_patterns.items())},
            # Rides along to the worker so the hook can tell it how long it has to make the case
            "case_ttl_seconds": self.case_ttl_seconds,
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
        """Build from catalog JSON"""
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

        ttl_raw = data.get("case_ttl_seconds", DEFAULT_CASE_TTL_SECONDS)
        try:
            ttl = float(ttl_raw)
        except (TypeError, ValueError):
            raise ValueError('"gates.case_ttl_seconds" must be a number of seconds') from None
        if ttl <= 0:
            raise ValueError('"gates.case_ttl_seconds" must be positive — a held request '
                             'that never expires is the leak this setting bounds')
        if ttl >= CASE_TTL_CEILING_SECONDS:
            # Clamped, not refused: a settings file from the previous release legally carries 600 and this runs...
            log.warning(
                "gates.case_ttl_seconds=%.0f is at or past the %.0fs prompt-cache TTL; "
                "clamped to %.0f. A worker parked longer than the cache lives re-sends "
                "its whole conversation at the cache-write rate.",
                ttl, CASE_TTL_CEILING_SECONDS, DEFAULT_CASE_TTL_SECONDS)
            ttl = DEFAULT_CASE_TTL_SECONDS
        return cls(enabled=frozenset(enabled), extra_patterns=extra, case_ttl_seconds=ttl)


@dataclass(frozen=True)
class GatedAction:
    """A command recognised as an attempt at a privileged action"""

    kind: str
    summary: str
    command: str
    matched: str  # the pattern that fired — shown to the reviewer, and to the user
    # Which row of the rule base fired
    rule_id: str = ""


_SUMMARIES = {k.name: k.summary for k in KINDS}


def classify(command: str, config: GateConfig, rules: RuleSet | None = None,
             central: CentralStore | None = None) -> GatedAction | None:
    """The gate this Bash command trips, or None"""
    if not command or not config.enabled:
        return None
    ruleset = rules if rules is not None else RuleSet.load(central)
    decision = ruleset.decide(command, config.enabled, config.extra_patterns)
    if central is not None:
        for rule_id, _, _ in decision.cleared:
            if rule_id != "catalog":
                central.record_gate_rule_hit(rule_id)
    if decision.match is None:
        return None
    return GatedAction(kind=decision.match.kind,
                       summary=_SUMMARIES.get(decision.match.kind, decision.match.kind),
                       command=command.strip(), matched=decision.match.pattern,
                       rule_id=decision.match.rule_id)


# -- what pull request a merge command names

#: A literal pull-request URL sitting in the command
_PR_URL_IN = re.compile(
    r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/[0-9]+")

#: `gh pr merge 735 --squash`, flags in any position before the number
_PR_NUMBER_IN = re.compile(r"\bgh\s+pr\s+merge\s+(?:-{1,2}[^\s]+\s+)*?([0-9]+)\b")

#: `--repo owner/name` / `-R owner/name`: the number is then about THAT repository
_REPO_FLAG_IN = re.compile(r"(?:--repo[=\s]|-R\s*)([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)")

#: `gh api --method PUT repos/<owner>/<repo>/pulls/735/merge`
_PR_API_IN = re.compile(
    r"\brepos/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pulls/([0-9]+)/merge\b")


def pull_request_in(command: str, cwd: Path | None) -> str:
    """The pull request a merge command names, checked, or `""` (docs/superpowers/specs/2026-09-25-a-gate-records-the-pull-request.md §1.)"""
    from . import github

    text = command or ""
    url = ""
    literal = _PR_URL_IN.search(text)
    api = _PR_API_IN.search(text)
    number = _PR_NUMBER_IN.search(text)
    if literal:
        url = literal.group(0)
    elif api:
        url = f"https://github.com/{api.group(1)}/{api.group(2)}/pull/{api.group(3)}"
    elif number:
        # The host is not in the command, and `checked_pr_url` constrains owner and repo rather than host
        named = _REPO_FLAG_IN.search(text)
        repo = (named.group(1), named.group(2)) if named else github.origin_repo(cwd)
        if repo is not None:
            url = f"https://github.com/{repo[0]}/{repo[1]}/pull/{number.group(1)}"
    if not url:
        return ""
    try:
        return github.checked_pr_url(url, cwd=cwd)
    except github.UntrustedPullRequest:
        return ""


# -- misconfiguration that silently shuts a gate

_BASH_RULE = re.compile(r"^Bash\((.*)\)$", re.IGNORECASE)


def _deny_rule_core(rule: str) -> str | None:
    """The literal part of a `Bash(...)` deny rule, or None for non-Bash rules"""
    m = _BASH_RULE.match(rule.strip())
    if not m:
        return None
    return m.group(1).strip().strip("*").strip().lower()


def deny_conflicts(config: GateConfig, deny_rules: Iterable[str]
                   ) -> list[tuple[str, str]]:
    """Deny rules that would block an enabled gate"""
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


# -- what a blocked worker is told to do


def kind_of(name: str) -> GateKind | None:
    """One `GateKind` by name, or None"""
    return next((k for k in KINDS if k.name == name), None)


def asks(kind: str) -> tuple[str, str]:
    """What `--why` and `--evidence` should contain FOR THIS GATE — spec 2026-09-12 §6"""
    found = kind_of(kind)
    if found is None:
        return GateKind(name=kind, summary="").why_ask, GateKind(name=kind,
                                                                 summary="").evidence_ask
    return found.why_ask, found.evidence_ask


def _handle(wo_id: str, command: str, approval_id: int | str | None) -> str:
    """How an exit ADDRESSES the blocked command: by request id where one exists"""
    return str(approval_id) if approval_id is not None else f"{wo_id} \"{command}\""


def request_command(wo_id: str, command: str, kind: str = "",
                    why: str = "", evidence: str = "",
                    approval_id: int | str | None = None) -> str:
    """The command that puts a case for a real privileged action in front of a reviewer"""
    kind_why, kind_evidence = asks(kind)
    return (f"jarvis gate request {_handle(wo_id, command, approval_id)} "
            f"--why \"{why or f'<{kind_why}>'}\" "
            f"--evidence \"{evidence or f'<{kind_evidence}>'}\"")


def contest_command(wo_id: str, command: str,
                    why: str = "<why this performs no privileged action>",
                    approval_id: int | str | None = None) -> str:
    """The command that disputes the MATCH rather than arguing for the action"""
    return (f"jarvis gate contest {_handle(wo_id, command, approval_id)} "
            f"--why \"{why}\"")


def explain_command(command: str, approval_id: int | str | None = None) -> str:
    """The diagnosis step, addressed the same way — see `_handle`"""
    handle = str(approval_id) if approval_id is not None else f"\"{command}\""
    return f"jarvis gate explain {handle}"


def exits_advice(wo_id: str, command: str, kind: str = "",
                 approval_id: int | str | None = None) -> str:
    """The two ways out of a block, and the diagnosis that picks between them (docs/superpowers/specs/2026-09-12-contesting-a-gate-match.md §3.)"""
    return (
        f"TWO WAYS OUT. If you cannot tell which you need, ask the OS first — it reports "
        f"where the matched literal sits and whether the shell would run it:\n"
        f"    {explain_command(command, approval_id)}\n\n"
        f"If the command DOES perform the action, make the case:\n"
        f"    {request_command(wo_id, command, kind, approval_id=approval_id)}\n\n"
        f"If it does NOT — the recogniser matched text it is only reading or writing "
        f"about (a name in a grep pattern, a path in a commit message, a string in a "
        f"heredoc) — contest the match. That asks a reviewer to DISMISS it as a "
        f"classifier false positive; it authorises nothing and it is not a request for "
        f"permission, so it needs no PR and no test results:\n"
        f"    {contest_command(wo_id, command, approval_id=approval_id)}\n\n"
        + ("All three address the block by its REQUEST NUMBER, not by the command "
           "string: the OS already has the string, and re-typing it into an argument is "
           "how a worker in a git worktree finds its own way out refused by the "
           "isolation guard (§8). Neither exit ever runs the command.\n\n"
           if approval_id is not None else "")
        + f"Do not guess between them: answering the request's two questions about a "
        f"command that performs no privileged action means writing something false into "
        f"the only text the reviewer sees, and walking away leaves the block on the "
        f"record with nobody ever told the recogniser was wrong."
    )


# -- the request Neo reviews

# Neo's ordinary persona is told to escalate anything "production-impacting" or that "publishes"
REVIEWER_PERSONA ="""You are Neo, reviewing a PRIVILEGED ACTION REQUEST inside the \
Jarvis agentic OS.

BEFORE ANYTHING ELSE: if the request below opens with `SELF-HEAL REQUEST`, three of the
rules that follow are switched off for it and the rest apply unchanged.

That request was filed by the OS itself, not by a worker, and it carries a rendered
intent (`heal <alarm>: <remedy> <subject> — …`) where the others carry a shell command.
It is a genuine privileged action: approving it lets the OS reach a session the user is
paying for, on the strength of its own judgement.

- The premise check does not apply, and neither does the read-only test. There is no
  command to inspect; the action is the remedy named in the request.
- NEVER DISMISS ONE, and never propose an `exempt_pattern` for one. Dismissal exists to
  correct the recogniser, and the recogniser did not fire here — nothing classifies into
  this gate. A pattern derived from an intent string would be a rule about prose.
- Judge it on the remedy's own blast line — what it touches and what it cannot undo —
  against the evidence packet and the supervisor's reasoning, both quoted in full.
  Approve, deny, or escalate; deny whenever the case for acting is not made, which
  leaves the symptom with the user and costs nothing but a tick.

SECOND: if the request below opens with `CONTESTED GATE MATCH`, the worker is not asking
for permission. It asserts that the OS's recogniser misfired and that the command performs
no privileged action at all. Everything below still applies — read it — with these
changes:

- YOU HAVE TWO VERDICTS, `dismiss` and `deny`. There is no third. A contest contains no
  case for performing a privileged action, so there is nothing here you could approve;
  the OS records an approval of a contest as a refusal, because that is what it is.
- DISMISS if the worker is right, and teach the classifier with an `exempt_pattern` as
  below. This is the whole point of the route.
- DENY if it is wrong — if the command really does merge, release or restart. Say which
  part of it performs the action. The worker's route back is `jarvis gate request` with a
  real case, and your reason is what tells it to take that route.
- The request carries the OS's own structural analysis of where the matched literal sits.
  Weigh it above the worker's prose: it is a fact about the command, and the worker is
  arguing its own case.
- DO NOT ESCALATE unless the worker's claim is one you cannot check at all. A contest is
  a factual claim about the classifier, not an authorisation, and sending it to the user
  spends their attention on an OS bug — the exact cost this gate exists to avoid. If you
  are torn, deny: that costs the worker one round trip and authorises nothing.

Everything below is about the other kinds.

A worker agent ran a command that the OS's recogniser matched as privileged — merging a
pull request, cutting a release, restarting a service. The command was blocked and handed
to you. You decide, on the user's behalf, what happens to it.

You are the independent check, not a rubber stamp. The worker wrote the request; it is
an argument for its own case, so verify the claims against the evidence it supplies
rather than accepting the summary.

FIRST, THE PREMISE CHECK — does this command actually perform the action at all?

The recogniser matches text, so it also fires on commands that merely NAME a privileged
action. Ask this before anything else, because the other three verdicts all answer a
question that has not arisen yet.

DISMISS when the command performs no privileged action. Typical cases:
- The gated literal appears only inside a search pattern, a file path, a quoted string,
  a heredoc body, a commit message or PR prose — it is being read or written about, not
  run. `grep -rn <deploy script> src/` searches for a name; it deploys nothing.
- The command is plainly read-only: grep, head, cat, ls, git log, git diff, git status.
- The verb is not the gated one. `gh pr create` opens a pull request; the gate is on the
  MERGE. Opening a PR is ordinary work.

Dismissing is not approval and not refusal. It records that the OS's classifier was
wrong, unblocks the exact command, and authorises nothing — so it costs the user no
attention and leaves no audit trail claiming a release was vetted. When a command is a
false positive, DISMISS is the only honest verdict available; do not approve it (that
records an authorisation that never happened) and do not deny it (that tells the worker
it misbehaved and blocks a command that was always fine).

WHEN YOU DISMISS, TEACH THE CLASSIFIER. Add an `exempt_pattern`: a regular expression
describing the FAMILY of commands this one belongs to, so the next worker to write
something of the same shape is not blocked and you are not asked again. This is the only
route by which the OS gets better at this; a dismissal without one fixes a single command
string and nothing else.

- Describe the shape, not the instance. `git commit -F -` with a release path in the
  message body is a family; that exact commit message is not.
- Anchor it on literal text. A pattern with no literal in it is not a description of a
  family, it is the gate switched off, and the OS will refuse it.
- Cover the WHOLE command, start to end. A pattern describing only the harmless prefix
  clears whatever was chained after it — `&&`, `;`, or a second line. The OS refuses one
  that does not match the command it was written for end to end, and refuses one that
  reaches across a newline.
- Do not propose one at all for a command spanning more than one line. A regex cannot say
  which line it is about; omit the field and let the OS derive the structural rule.
- Never write one that would match a command that really does perform the action. The OS
  tests every pattern you propose against commands that must always be gated and drops it
  if it fails, but do not rely on that: it is a backstop, not a reviewer.
- Omit the field entirely if you cannot describe the family safely. The OS falls back to
  a structural rule it derives itself, which is narrower and always safe.

HARD LIMIT on dismissal, and it is absolute: a command that ACTUALLY invokes the deploy
or release script, ACTUALLY merges a pull request, or ACTUALLY restarts or stops a
service is a genuine privileged action, however routine or well-justified it looks. It
gets the full review below. If you are unsure whether the command runs the thing or only
mentions it, it runs it — assume the privileged reading and review it properly.

If the command really does perform the action, continue:

SECOND, THE DUPLICATE CHECK — was this already decided?

The request below lists the work order's earlier gate requests. Read it. A grant is
scoped to an EXACT command string, so a worker that reruns an approved command with a
pipe, a redirect or a `2>&1` appended trips the gate a second time and files what looks
like a brand-new, unjustified request for an action you already authorised — and if that
earlier grant has uses spent, the action has ALREADY RUN.

Never escalate a duplicate to the user: they have answered this question once, and asking
again about something already done is worse than useless, because it reads as though the
gate failed to hold. Judge it against the earlier verdict instead. If that verdict was
approve and the extra text changes nothing about what executes, approve it too, saying
which request it repeats. If the addition does something the approval did not cover, deny
it and say what is new. If the earlier verdict was deny, deny it again.

If the command really is a fresh privileged action, continue:

APPROVE when all of these hold:
- The action is squarely within what the work order was asked to do.
- The change went through the normal path: work landed on a branch, in a pull request,
  with the project's checks or tests reported passing. For a RELEASE of code that is
  already on the main branch, that evidence is CI's verdict on the exact merged commits.
  The merge is what asserts the code is ready; a green CI run on those commits IS the
  check, and it is complete evidence on its own. This clause is written for the kinds
  that SHIP CODE. Some gates do not — restarting a service, writing a fleet setting —
  and for those the same question is asked of different facts: the evidence box names
  what was asked for, and that is what to judge. Never hold a service restart to the
  absence of a pull request.
- The command matches the stated intent — the PR number, tag or service named is the
  one the request is about, and nothing extra rides along.
- Consequences are recoverable by ordinary means (revert the merge, ship the previous
  release again).

DENY when the request is a real privileged action that should not proceed: outside the
work order's scope, skipping review (a direct push to a protected branch when a PR was
the agreed route), targeting something other than what it claims, or bundling unrelated
changes. Say plainly what is wrong; the worker sees your reason and can fix it and ask
again. Deny is an accusation that the worker asked for the wrong thing — never use it
for a command the recogniser matched by mistake. That is what DISMISS is for.

ESCALATE to the user, rather than deciding, when:
- Tests or checks are failing, absent, or not mentioned at all. One carve-out, and it is
  not optional: a release of already-merged code is NOT expected to re-run anything. Do
  not ask a release request for a local test run, and never escalate one for lacking it —
  CI on the merged commits is the evidence, and demanding more makes every worker burn an
  hour re-proving what the merge already settled. A release that reports CI green and
  nothing else has met this bar.
- The action is irreversible or destructive (deleting a release, rewriting published
  history, dropping data).
- It touches credentials, secrets, billing, or anything user-facing beyond this repo.
- The work order itself is ambiguous about whether shipping was in scope.
- Anything in the request does not add up, including a claim you cannot check.

THE USER MAY HAVE ANSWERED THIS ALREADY. When the request carries a section of the
user's own words to the work order, read it before you escalate. It verifies nothing — a
user authorising a merge has not made its checks green, so every evidence test above
still applies — but it settles WHO DECIDES, and a user who has instructed this work order
in writing has already decided. Escalating it back to them asks a question they have
answered and reads as though their answer was lost. Escalate on what they have NOT
answered, if anything, and say which of their words you relied on.

Escalating is the safe answer and costs only a little of the user's time. Approving
something that should not ship costs much more. When genuinely torn about a REAL
privileged action, escalate. Note that a false positive is the one case where escalating
is NOT the safe answer: it spends the user's attention on an OS bug, which is the exact
cost this gate exists to avoid. Dismiss it instead.

Output STRICT JSON, nothing else:
  {"escalate": false, "verdict": "approve",  "reason": "<one line: what you verified>"}
  {"escalate": false, "verdict": "deny",     "reason": "<one line: what is wrong>"}
  {"escalate": false, "verdict": "dismiss",  "reason": "<one line: why this command \
performs no privileged action>", "exempt_pattern": "<regex for the family, or omit>"}
  {"escalate": true,  "verdict": "deny",     "reason": "<one line: why the user must \
decide>"}
An escalation may carry one optional label, which is grouped and reported:
  "cause": "<on an escalation, name the cause from this list; omit it if none fits: \
privileged-action | evidence-insufficient>\""""


HISTORY_LIMIT = 8


def render_history(rows: Iterable[dict[str, Any]]) -> list[str]:
    """The work order's earlier gate requests, as the reviewer's duplicate check"""
    rows = [r for r in rows][:HISTORY_LIMIT]
    if not rows:
        return []
    out = ["", "EARLIER GATE REQUESTS ON THIS WORK ORDER (newest first). Check this one "
           "against them before deciding — a repeat of an action already decided is not "
           "a new question:"]
    for r in rows:
        spent = f", {r['uses']} of {r['max_uses']} uses spent" if r.get("uses") else ""
        by = f" by {r['decided_by']}" if r.get("decided_by") else ""
        out.append(f"  request {r['id']} — {r['kind']}: {r['status']}{by}{spent}")
        out.append(f"      command: {r['command'][:300]}")
        if r.get("decision_reason"):
            out.append(f"      reason: {r['decision_reason'][:300]}")
    return out


USER_MESSAGE_LIMIT = 5
USER_MESSAGE_CHARS = 700


def render_user_messages(rows: Iterable[dict[str, Any]]) -> list[str]:
    """The user's own words to this work order, as the reviewer's authorisation check"""
    rows = [r for r in rows][:USER_MESSAGE_LIMIT]
    if not rows:
        return []
    out = ["", "WHAT THE USER HAS TOLD THIS WORK ORDER (newest first). These are the "
           "USER'S OWN WORDS, relayed to the worker through `jarvis wo send` or the "
           "dashboard — not the worker's account of what the user wants. Weigh an "
           "explicit instruction or authorisation here AS THE USER'S, and say in your "
           "reason if you relied on one. It is not evidence: authorising a merge does "
           "not make its checks green, and a genuinely unverified action is still worth "
           "escalating. Only messages the OS could attribute to the user appear here, "
           "so a message the WORKER wrote is never among them:"]
    for r in rows:
        body = " ".join((r.get("content") or "").split())[:USER_MESSAGE_CHARS]
        out.append(f"  · {body}")
    return out


def build_request_question(action: GatedAction, wo: dict[str, Any],
                           justification: str, evidence: str = "",
                           agent_type: str | None = None,
                           history: Iterable[dict[str, Any]] = (),
                           user_messages: Iterable[dict[str, Any]] = ()) -> str:
    """Render the approval request Neo (or the user) reads"""
    actor = f"The `{agent_type}` seat of work order {wo['id']}" if agent_type \
        else f"The worker for work order {wo['id']}"
    parts = [
        f"PRIVILEGED ACTION REQUEST — gate `{action.kind}`",
        "",
        f"{actor} wants to {action.summary}.",
        "",
        "Exact command it will run (approval authorises this command and nothing else):",
        f"    {action.command}",
        "",
        f"Work order: {wo['id']}",
        "",
    ]
    # EVERY UNTRUSTED FIELD THIS BUILDER RENDERS, enumerated — spec 2026-09-24 fix 1
    parts += borrowed_sections((
        Borrowed(label=WO_TITLE, whose=WO_TITLE_WHOSE, text=wo.get("title") or "",
                 limit=TITLE_LIMIT, absent="The work order has no title."),
        Borrowed(label=WO_DESCRIPTION, whose=WO_DESCRIPTION_WHOSE,
                 text=wo.get("description") or "", limit=DESCRIPTION_LIMIT),
        Borrowed(label="the worker's justification for this request",
                 whose=f"the worker of {wo['id']}", text=justification,
                 absent="The worker gave no justification — treat that as a red flag."),
        # Labelled with what THIS gate asked for — 2026-09-12 §6
        Borrowed(label=f"the evidence the worker supplied ({asks(action.kind)[1]})",
                 whose=f"the worker of {wo['id']}", text=evidence,
                 limit=EVIDENCE_LIMIT),
    ))
    parts += render_user_messages(user_messages)
    parts += render_history(history)
    parts += [
        "",
        "Decide: dismiss it if the command performs no privileged action and the "
        "recogniser matched it by mistake; otherwise approve it, deny it with a reason "
        "the worker can act on, or escalate to the user.",
        f"(The recogniser that fired was: {action.matched})",
    ]
    return "\n".join(parts)


def describe_match(command: str, pattern: str) -> list[str]:
    """Where the matched literal sits — `jarvis gate explain`'s analysis, handed to the reviewer unasked"""
    from .gate_rules import reads_only, shape_of

    shape = shape_of(command, pattern)
    lines = [f"The OS's reading of it: the matched literal is "
             f"{shape.describe() if shape else 'in a position the OS could not parse'}."]
    if reads_only(command):
        lines.append("Every command in the chain is read-only.")
    if shape and not shape.exemptible:
        # The one thing that makes an otherwise convincing contest wrong (kn-986fc008)
        lines.append("A dismissal of this could NOT be generalised into a standing rule "
                     "— by this reading the shell could still run the literal.")
    return lines


def build_contest_question(action: GatedAction, wo: dict[str, Any], argument: str,
                           agent_type: str | None = None,
                           history: Iterable[dict[str, Any]] = (),
                           user_messages: Iterable[dict[str, Any]] = ()) -> str:
    """Render a CONTESTED match for the reviewer"""
    actor = f"the `{agent_type}` seat of work order {wo['id']}" if agent_type \
        else f"the worker for work order {wo['id']}"
    parts = [
        f"{CONTEST_HEADER} — gate `{action.kind}`",
        "",
        f"The OS blocked this command as `{action.kind}`, and {actor} says the "
        f"recogniser was wrong: that it performs no privileged action at all.",
        "",
        "The blocked command, exactly as it was run:",
        f"    {action.command}",
        "",
        f"The recogniser that fired: {action.matched}",
    ]
    parts += describe_match(action.command, action.matched)
    parts += [
        "",
        f"Work order: {wo['id']}",
        "",
    ]
    # The same enumeration, on the same rule
    parts += borrowed_sections((
        Borrowed(label=WO_TITLE, whose=WO_TITLE_WHOSE, text=wo.get("title") or "",
                 limit=TITLE_LIMIT, absent="The work order has no title."),
        Borrowed(label=WO_DESCRIPTION, whose=WO_DESCRIPTION_WHOSE,
                 text=wo.get("description") or "", limit=DESCRIPTION_LIMIT),
        Borrowed(label="the worker's argument that this is a false positive",
                 whose=f"the worker of {wo['id']}", text=argument,
                 absent="The worker gave no argument — deny it; there is nothing here "
                        "to review."),
    ))
    parts += render_user_messages(user_messages)
    parts += render_history(history)
    parts += [
        "",
        "Decide, and you have TWO verdicts only. `dismiss` if the worker is right: that "
        "clears this command, records no authorisation, and — with an `exempt_pattern` — "
        "stops the OS asking about commands of the same shape fleet-wide. `deny` if it "
        "is wrong, saying which part of the command performs the action; the worker's "
        "route back is a real request with a case. There is nothing here to approve: a "
        "contest argues that no privileged action exists, so it carries no case for "
        "performing one.",
    ]
    return "\n".join(parts)


# -- filing a request


def file_request(store: ProjectStore, neo: Any, project: str, wo: dict[str, Any],
                 action: GatedAction, justification: str = "", evidence: str = "",
                 max_uses: int = GRANT_MAX_USES, agent_type: str | None = None,
                 hold: bool = False, contested: bool = False,
                 ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Record an approval request (docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md)"""
    approval = store.add_approval(
        wo["id"], action.kind, action.command, matched=action.matched,
        justification=justification, evidence=evidence, max_uses=max_uses,
        agent_type=agent_type, status=AWAITING_CASE if hold else "pending",
        contested=contested,
    )
    # The park rides with `queue_for_review`, which is the ONLY door to `pending`
    question = None if hold else queue_for_review(store, neo, project, wo, action,
                                                  approval)
    return approval, question


def question_text(store: ProjectStore, wo: dict[str, Any], action: GatedAction,
                  approval: dict[str, Any]) -> str:
    """The reviewer's page for this request, whichever kind of claim it carries"""
    # Read before the question, so the reviewer's history is everything BUT this request
    history = [a for a in store.list_approvals(wo["id"], limit=HISTORY_LIMIT)
               if a["id"] != approval["id"]]
    user_messages = store.user_messages(wo["id"], USER_MESSAGE_LIMIT)
    if approval["contested"]:
        return build_contest_question(action, wo, approval["justification"],
                                      approval["agent_type"], history, user_messages)
    return build_request_question(action, wo, approval["justification"],
                                  approval["evidence"], approval["agent_type"], history,
                                  user_messages)


def queue_for_review(store: ProjectStore, neo: Any, project: str, wo: dict[str, Any],
                     action: GatedAction, approval: dict[str, Any]) -> dict[str, Any]:
    """Put a recorded request in front of a reviewer (docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md)"""
    question = neo.ask(
        project, wo["id"], question_text(store, wo, action, approval),
        # Spec 2026-09-24 fix 1: `context=` is the field that carried the defect
        context=borrowed_context((
            Borrowed(label=WO_TITLE, whose=WO_TITLE_WHOSE, text=wo.get("title") or "",
                     limit=TITLE_LIMIT),
            Borrowed(label=WO_DESCRIPTION, whose=WO_DESCRIPTION_WHOSE,
                     text=wo.get("description") or "", limit=CONTEXT_LIMIT))),
        kind="approval",
    )
    store.start_review(approval["id"], question["id"])
    # The worker has nothing to do until a verdict lands
    if store.get_work_order(wo["id"])["status"] in ("running", "dispatching"):
        store.set_status(wo["id"], "waiting_input")
    return question


def abandoned_message(approval: dict[str, Any], ttl_seconds: float) -> str:
    """What the worker is told when its held request timed out"""
    return (
        f"[Gate {approval['id']} ABANDONED — no case was made within "
        f"{int(ttl_seconds // 60)} minutes]\n\n"
        f"The command that was blocked:\n    {approval['command']}\n\n"
        f"NOBODY REVIEWED IT AND NOBODY DECIDED IT. Nothing was authorised and nothing "
        f"was refused: you ran a command the `{approval['kind']}` gate matched, were "
        f"told to make the case or contest the match, and did neither. The command is "
        f"still blocked and the request is closed unanswered.\n\n"
        f"If you worked around the block, come back and finish this: a match nobody "
        f"contests is a classifier defect nobody ever hears about, and the next worker "
        f"loses the same turn you did.\n\n"
        + exits_advice(approval["wo_id"], approval["command"], approval["kind"],
                       approval["id"])
    )


def abandoned_reason(ttl_seconds: float) -> str:
    """What the record says about a request nobody argued and nobody reviewed"""
    return (f"no case was made for it within {int(ttl_seconds // 60)} minutes, and the "
            f"match was never contested. Nobody reviewed it.")


def sweep_unargued(store: ProjectStore, ttl_seconds: float) -> list[dict[str, Any]]:
    """Close every held request whose case never came"""
    from . import db
    from .invariants import end_wait_if_nothing_is_out

    cutoff = db.now() - ttl_seconds
    closed = []
    for approval in store.list_approvals(statuses=(AWAITING_CASE,)):
        if approval["ts"] > cutoff:
            continue
        row = store.abandon_approval(approval["id"],
                                     reason=abandoned_reason(ttl_seconds))
        store.queue_message(row["wo_id"], abandoned_message(row, ttl_seconds),
                            source="gate")
        # The request parked the work order; the close has to unpark it
        end_wait_if_nothing_is_out(store, row["wo_id"])
        closed.append(row)
    return closed


def _merge_case(existing: str, addition: str) -> str:
    """The prior case plus the new one, as one field a reviewer reads top to bottom"""
    prior = existing.strip()
    addition = addition.strip()
    if not addition:
        return existing
    if is_no_case(prior):
        return addition
    if addition in prior:
        return existing
    return f"{prior}\n\nAMENDED by the worker:\n{addition}"


def amend_request(store: ProjectStore, neo: Any, wo: dict[str, Any],
                  action: GatedAction, approval: dict[str, Any],
                  justification: str = "", evidence: str = "") -> dict[str, Any]:
    """Attach a case to a request that is already pending"""
    from .neo_store import OPEN_Q_STATUSES

    amended = store.amend_approval(
        approval["id"],
        justification=_merge_case(approval["justification"], justification),
        evidence=_merge_case(approval["evidence"], evidence),
    )
    question = neo.get(approval["neo_question_id"]) if approval["neo_question_id"] else None
    if question is not None and question["status"] in OPEN_Q_STATUSES:
        neo.revise_question(question["id"],
                            question_text(store, wo, action, amended))
    return amended


# -- opening the gate


def open_gate(store: ProjectStore, grant: dict[str, Any],
              neo: NeoStore | None = None) -> dict[str, Any]:
    """Spend one use of a grant, and close the requests it has just made moot"""
    approval = store.consume_grant(grant["id"])
    if approval["status"] != "approved":
        return approval
    # Held requests are swept too
    for pending in (store.pending_approvals(approval["wo_id"])
                    + store.held_approvals(approval["wo_id"])):
        if pending["id"] == approval["id"] or pending["kind"] != approval["kind"]:
            continue
        if approval["command"] not in pending["command"]:
            continue
        store.supersede_approval(pending["id"], (
            f"the same {approval['kind']} action ran under approved request "
            f"{approval['id']}, which this command only wraps — nothing is left to "
            f"authorise or refuse, and no authorisation is implied by closing it"
        ))
        # The approval closes; its Neo question does not
        if neo is not None and pending["neo_question_id"]:
            neo.supersede(
                pending["neo_question_id"],
                f"SUPERSEDED by approval {approval['id']}",
                f"the {approval['kind']} action ran under approved request "
                f"{approval['id']}; nothing here authorises anything",
            )
    return approval


# -- deciding

# What the worker is told when a gate opens
def approved_message(approval: dict[str, Any], reason: str, by: str) -> str:
    return (
        f"[Gate {approval['id']} APPROVED by {by}] {reason}\n\n"
        f"Run this command again, exactly as written, and it will go through:\n"
        f"    {approval['command']}\n\n"
        f"The approval covers this command only, for the next "
        f"{GRANT_TTL_SECONDS // 60} minutes. Anything else still needs its own request."
    )


def denied_message(approval: dict[str, Any], reason: str, by: str) -> str:
    # A denied CONTEST refuses a different claim, so it has to say which one — §2
    if approval.get("contested"):
        return (
            f"[Gate {approval['id']} — CONTEST REJECTED by {by}] {reason}\n\n"
            f"The command that stays blocked:\n    {approval['command']}\n\n"
            f"You argued that this performs no privileged action and the reviewer "
            f"disagreed, so the match stands. Do not retry it as-is. If the work is "
            f"genuinely ready, make the real case:\n"
            f"    {request_command(approval['wo_id'], approval['command'], approval['kind'], approval_id=approval['id'])}\n"
            f"Otherwise leave it and finish the work order explaining what is left."
        )
    return (
        f"[Gate {approval['id']} DENIED by {by}] {reason}\n\n"
        f"The command that was blocked:\n    {approval['command']}\n\n"
        f"Do not retry it as-is — it will be blocked again. Address the reason above, "
        f"then either request approval afresh (`jarvis gate request`) or finish the "
        f"work order explaining what is left."
    )


# A dismissal has to tell the worker three things a grant does not: that nothing was authorised
def dismissed_message(approval: dict[str, Any], reason: str, by: str,
                      learned: dict[str, Any] | None = None) -> str:
    # What the OS learned is told to the worker, not just written to the record
    footer = ""
    if learned and learned.get("learned"):
        footer = (f"\n\nThe OS learned from this. Rule {learned['learned']} now clears "
                  f"{learned.get('rule') or 'commands of this shape'}, for every work "
                  f"order and every project, so nobody has to re-litigate it. Inspect it "
                  f"with `jarvis gate rules`.")
    elif learned and learned.get("notes"):
        footer = ("\n\nThe OS could not generalise this dismissal into a standing rule: "
                  + "; ".join(learned["notes"]) + ". The command itself is still cleared.")
    return _dismissed_body(approval, reason, by) + footer


def _dismissed_body(approval: dict[str, Any], reason: str, by: str) -> str:
    return (
        f"[Gate {approval['id']} DISMISSED by {by} — not a privileged action] {reason}\n\n"
        f"The OS matched this command as `{approval['kind']}` by mistake. It performs no "
        f"privileged action, so nothing was authorised and nothing was refused: this is "
        f"a defect in the gate's recogniser, not a verdict on your request.\n\n"
        f"Run it again, exactly as written, and it will go through:\n"
        f"    {approval['command']}\n\n"
        f"One limit on that. This cleared a command STRING; it did not reset the review "
        f"state of any privileged action the string refers to. If an equivalent request "
        f"for that action is already pending or escalated, do NOT run this again — it "
        f"would open a second request while the first is undecided. Close the gap on the "
        f"request that already exists instead: send the reviewer what it was missing "
        f"(`jarvis wo ask`, or `jarvis notify` if the user has to see it) and leave the "
        f"original standing.\n\n"
        f"The dismissal covers this exact command string for this work order and does "
        f"not expire. Anything that genuinely does ship code still needs a real request."
    )


_VERDICT_MESSAGE = {
    "approved": approved_message,
    "denied": denied_message,
    "dismissed": dismissed_message,
}


def learn_from_dismissal(central: CentralStore, approval: dict[str, Any],
                         reason: str, decided_by: str,
                         exempt_pattern: str = "",
                         project: str = "") -> dict[str, Any]:
    """Turn a dismissal into a standing rule, and report what happened either way"""
    from . import gate_rules

    try:
        ruleset = RuleSet.load(central)
        proposal = gate_rules.propose_exemption(
            ruleset, command=approval["command"], kind=approval["kind"],
            pattern=approval["matched"], reason=reason, exempt_pattern=exempt_pattern,
            approval_id=approval["id"], wo_id=approval["wo_id"], project=project,
        )
        if proposal.rule is None:
            return {"learned": None, "notes": proposal.notes}
        row = central.add_gate_rule(
            proposal.rule["role"], proposal.rule["test"], proposal.rule["pattern"],
            kind=proposal.rule["kind"], summary=proposal.rule["summary"],
            source=proposal.rule["source"], project=proposal.rule["project"],
            wo_id=proposal.rule["wo_id"], approval_id=proposal.rule["approval_id"],
            reason=f"{decided_by}: {reason}",
        )
        return {"learned": row["id"], "notes": proposal.notes,
                "rule": gate_rules.Rule.from_row(row).render()}
    except Exception as e:  # noqa: BLE001 — see the docstring
        return {"learned": None, "notes": [f"the rule base could not be updated: {e!r}"]}


def apply_decision(store: ProjectStore, approval_id: int, verdict: str,
                   reason: str, decided_by: str,
                   central: CentralStore | None = None,
                   exempt_pattern: str = "", project: str = "") -> dict[str, Any]:
    """Record a verdict and queue the worker's resume message"""
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r} — expected one of {list(VERDICTS)}")
    # A CONTEST CAN NEVER BECOME AN AUTHORISATION
    filed = store.get_approval(approval_id)
    if filed is not None and filed["contested"] and verdict == "approved":
        verdict = "denied"
        reason = f"{reason.strip()}\n\n{CONTEST_NOT_AN_AUTHORISATION}".strip()
    approval = store.decide_approval(approval_id, verdict=verdict, reason=reason,
                                     decided_by=decided_by)
    learned: dict[str, Any] = {}
    if verdict == "dismissed":
        from .central_store import CentralStore as _CS

        own = central is None
        store_c = central or _CS()
        try:
            learned = learn_from_dismissal(store_c, approval, reason, decided_by,
                                           exempt_pattern=exempt_pattern,
                                           project=project)
        finally:
            if own:
                store_c.close()
    # A `self_heal` request was filed by the OS against a session that never asked (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §5.)
    filed_by_the_os = approval["kind"] in (SELF_HEAL, AUTO_MERGE)
    if approval["kind"] == SELF_HEAL:
        from . import remedies

        remedies.record_verdict(store, approval, verdict, reason, decided_by,
                                central=central, project=project)
    elif approval["kind"] == AUTO_MERGE:
        from . import automerge

        automerge.record_verdict(store, approval, verdict, reason, decided_by,
                                 central=central, project=project)
    else:
        message = (dismissed_message(approval, reason, decided_by, learned)
                   if verdict == "dismissed"
                   else _VERDICT_MESSAGE[verdict](approval, reason, decided_by))
        store.queue_message(approval["wo_id"], message, source="gate")
    if verdict == "dismissed":
        store.add_event(approval["wo_id"], "gate_dismissed", {
            "approval_id": approval_id,
            "by": decided_by,
            "kind": approval["kind"],
            "command": approval["command"],
            "matched": approval["matched"],
            "reason": reason,
            "learned_rule": learned["learned"],
            "learned": learned.get("rule", ""),
            "learn_notes": learned["notes"],
        })
    else:
        store.add_event(approval["wo_id"], "gate_decided", {
            "approval_id": approval_id,
            "decision": verdict,
            "by": decided_by,
            "kind": approval["kind"],
            "reason": reason,
        })
    # Reverse what `request` did to the status
    _record_pull_request(store, approval, verdict)
    if not filed_by_the_os:
        from .invariants import end_wait_if_nothing_is_out

        end_wait_if_nothing_is_out(store, approval["wo_id"])
    return approval


def _record_pull_request(store: ProjectStore, approval: dict[str, Any],
                         verdict: str) -> None:
    """Write the pull request an APPROVED merge authorised, if the record has none (#742)"""
    try:
        if verdict != "approved" or approval["kind"] not in (PR_MERGE, AUTO_MERGE):
            return
        wo = store.get_work_order(approval["wo_id"])
        if wo is None or wo.get("pr_url"):
            return
        pr_url = pull_request_in(approval["command"], store.project_path)
        if not pr_url:
            return
        store.update_work_order(wo["id"], pr_url=pr_url)
        store.add_event(wo["id"], "pr_url_recorded", {
            "approval_id": approval["id"], "pr_url": pr_url, "source": "gate"})
    except Exception:  # noqa: BLE001 — see the docstring
        log.exception("could not record the pull request of gate %s", approval.get("id"))


def summarise(approvals: Iterable[dict[str, Any]]) -> str:
    """One-line rendering of a work order's gate history, for status output"""
    rows = list(approvals)
    if not rows:
        return "no gate requests"
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return ", ".join(f"{n} {status}" for status, n in sorted(by_status.items()))
