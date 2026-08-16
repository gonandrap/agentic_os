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

APPROVAL_STATUSES = ("pending", "approved", "denied", "dismissed", "expired")

# The verdicts a reviewer can reach. `dismissed` is not a softer denial and not a quieter
# approval — it answers a different question.
#
# The first three statuses shipped without it, and the omission produced a contradiction
# the user was made to arbitrate twice, both times coherently and in opposite directions.
# When a recogniser fires on a command that performs no privileged action — a deploy
# script's name quoted inside a grep pattern, a PR body that cites a path — every
# available verdict recorded something false. `approved` asserts that a privileged action
# was reviewed and authorised, leaving an audit trail implying a real deploy was vetted.
# `denied` asserts the worker made a bad request and tells it not to retry, blocking a
# command that was never privileged. Escalating spends the user's attention on an OS bug,
# which is the exact cost the gate exists to avoid. The identical command was denied once
# and approved once as a direct result.
#
# `dismissed` says the only true thing: the classifier was wrong. It clears the command,
# records no authorisation, and is counted separately so the false-positive rate is a
# number someone can watch rather than an anecdote.
VERDICTS = ("approved", "denied", "dismissed")


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


# Tools that read and cannot execute. A privileged action named in an *argument* to one
# of these is a mention, not an attempt: `cat scripts/shipit.sh` prints the release
# script, and printing it ships nothing.
#
# `scannable()` already covers the quoted case, and cannot cover this one — the thing
# being named here is a *path*, and nobody quotes paths. That gap gated three commands
# on wo-52a6164d alone (`cat …/skills/shipit/SKILL.md`, `grep … scripts/shipit.sh`),
# each costing a Neo review and, until the status fix below, a spurious attention item.
#
# Membership is decided by what a tool CAN do, never by what it is usually used for.
# `find` has `-exec`, `awk` has `system()`, `xargs` and `eval` re-parse their input,
# `git` pushes, and `sed -i` writes — none of them are here or can be.
_READERS = frozenset({
    "cat", "tac", "head", "tail", "nl", "wc", "ls", "stat", "file", "diff", "cmp",
    "grep", "egrep", "fgrep", "rg", "ag", "cut", "sort", "uniq", "tr", "column",
    "jq", "yq", "basename", "dirname", "realpath", "readlink", "echo", "printf",
    "pwd", "tree", "strings", "od", "xxd", "md5sum", "sha256sum", "cksum", "du", "df",
    "which", "sed",
})

# Wrappers that run whatever follows them without changing what it can do, so the
# reader test applies to the word after them instead.
_TRANSPARENT = frozenset({"command", "builtin", "time", "nice", "ionice", "sudo", "env"})

# A substitution runs a command to build an argument, so the reader in front of it is
# no longer the only thing executing. `cat $(which shipit)` reads; `cat <(./shipit.sh)`
# ships. Neither is worth telling apart — both leave reader territory.
_SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")

# Where one command ends and the next begins. `&` splits only when it starts a
# background job: in `2>&1` it is part of a redirection, and splitting there would leave
# `2>` looking like a command name and fail every reader that redirects its stderr.
_SEPARATORS = re.compile(r"\|\||&&|[|;\n]|(?<![<>&])&")

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def reads_only(command: str) -> bool:
    """True when every command in `command` can read but not execute.

    The exemption is all-or-nothing across the pipeline on purpose: a reader piping into
    a shell (`cat scripts/shipit.sh | bash`) is the obvious bypass, and one non-reading
    segment anywhere is enough to lose it. So the only commands this clears are ones
    that cannot run the thing they name — which is why it is safe to apply to every
    gate rather than just `release`.

    Unrecognised syntax fails the test rather than passing it: a name carrying a slash
    is not the `cat` on PATH but something in the tree that merely shares its name, and
    an empty segment means the split found something this parser does not model.
    """
    if _SUBSTITUTION.search(command) or _SHELL_INVOKER.search(command):
        return False
    # Split the *masked* command: a separator inside a quoted argument starts no new
    # command, and `grep "a\|b" f | head` used to be torn in half at the alternation.
    # Only command names and flags are read below, and neither is ever quoted.
    segments = [s.strip() for s in _SEPARATORS.split(_QUOTED.sub(" ", command))]
    if not any(segments):
        return False
    for segment in segments:
        if not segment:
            continue
        words = segment.lstrip("({ ").split()
        while words and (_ASSIGNMENT.match(words[0])
                         or words[0].lstrip("\\") in _TRANSPARENT):
            words = words[1:]
        if not words:
            return False
        name = words[0].lstrip("\\")
        if name not in _READERS:
            return False
        # `sed -n '1,20p' f` reads; `sed -i s/a/b/ f` rewrites the file.
        if name == "sed" and any(w.startswith("-i") or w == "--in-place"
                                 for w in words[1:]):
            return False
    return True


def classify(command: str, config: GateConfig) -> GatedAction | None:
    """The gate this Bash command trips, or None.

    Matches the whole command rather than parsed segments: a gated action hidden in a
    pipeline, a subshell or behind `&&` is the same action, and a classifier that only
    understands well-formed simple commands is a classifier with a bypass. Quoted
    arguments are blanked first — see `scannable` — and commands that can only read are
    exempt outright, see `reads_only`.
    """
    if not command or not config.enabled:
        return None
    if reads_only(command):
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
#
# The PREMISE CHECK comes first, and its position is the fix, not a stylistic choice. The
# earlier persona OPENED by asserting "a worker tried to run a command that ships code",
# then required of every approval that "work landed on a branch, in a pull request, with
# checks passing" — which a misclassified `grep` can never satisfy, so the closest-fitting
# clause left was DENY. The persona structurally forced the wrong answer on false
# positives; Neo was following it correctly. "Was this even a gated action" has to be
# answered BEFORE "should this gated action proceed", because it is a question about a
# different subject: the classifier, not the worker.
REVIEWER_PERSONA = """You are Neo, reviewing a PRIVILEGED ACTION REQUEST inside the \
Jarvis agentic OS.

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
  check, and it is complete evidence on its own.
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

Escalating is the safe answer and costs only a little of the user's time. Approving
something that should not ship costs much more. When genuinely torn about a REAL
privileged action, escalate. Note that a false positive is the one case where escalating
is NOT the safe answer: it spends the user's attention on an OS bug, which is the exact
cost this gate exists to avoid. Dismiss it instead.

Output STRICT JSON, nothing else:
  {"escalate": false, "verdict": "approve",  "reason": "<one line: what you verified>"}
  {"escalate": false, "verdict": "deny",     "reason": "<one line: what is wrong>"}
  {"escalate": false, "verdict": "dismiss",  "reason": "<one line: why this command \
performs no privileged action>"}
  {"escalate": true,  "verdict": "deny",     "reason": "<one line: why the user must \
decide>"}"""


HISTORY_LIMIT = 8


def render_history(rows: Iterable[dict[str, Any]]) -> list[str]:
    """The work order's earlier gate requests, as the reviewer's duplicate check.

    Without this a reviewer judges every request as if it were the first. That is how
    wo-52a6164d escalated a release to the user thirty-seven seconds after Neo had
    approved the same release: the worker re-ran the approved command with `| tail -40`
    appended, the exact-string grant did not match, the hook filed an unjustified request
    — and "unjustified real release" is a textbook escalation when you cannot see that
    the identical action was authorised a moment ago and has already run.
    """
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


def build_request_question(action: GatedAction, wo: dict[str, Any],
                           justification: str, evidence: str = "",
                           agent_type: str | None = None,
                           history: Iterable[dict[str, Any]] = ()) -> str:
    """Render the approval request Neo (or the user) reads.

    Whoever decides sees only this text — never the worker's session — so it has to
    carry the case by itself: what is being attempted, under which work order, why the
    worker believes it is ready, whatever it offered as proof, and what this same work
    order has already been told about the same gate.

    `agent_type` names the SEAT when a subagent tripped the gate. The work order still
    owns the request — its lead is answerable for what its team did — but the reviewer
    is being asked to judge an attempt, and an attempt by a seat that was never meant to
    run commands is a different fact from the same attempt by the lead.
    """
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
    parts += render_history(history)
    parts += [
        "",
        "Decide: dismiss it if the command performs no privileged action and the "
        "recogniser matched it by mistake; otherwise approve it, deny it with a reason "
        "the worker can act on, or escalate to the user.",
        f"(The recogniser that fired was: {action.matched})",
    ]
    return "\n".join(parts)


# -- filing a request ----------------------------------------------------------------


def file_request(store: ProjectStore, neo: Any, project: str, wo: dict[str, Any],
                 action: GatedAction, justification: str = "", evidence: str = "",
                 max_uses: int = GRANT_MAX_USES,
                 agent_type: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Record an approval request and queue it for review. Returns (approval, question).

    One path for both entry points — the hook (a worker that just ran the command) and
    `jarvis gate request` (a worker making its case first) — so a gate behaves the same
    however it was reached.

    The request rides the existing Neo queue rather than a parallel review pipeline,
    which is what gives it escalation-to-user, `jarvis neo list` and answer delivery
    without reimplementing any of them.

    `agent_type` reaches here only from the hook: `jarvis gate request` is a shell
    command, so whoever ran it had a shell, and the seats have none.
    """
    # Read before the insert, so the reviewer's history is everything BUT this request.
    history = store.list_approvals(wo["id"], limit=HISTORY_LIMIT)
    approval = store.add_approval(
        wo["id"], action.kind, action.command, matched=action.matched,
        justification=justification, evidence=evidence, max_uses=max_uses,
        agent_type=agent_type,
    )
    question = neo.ask(
        project, wo["id"],
        build_request_question(action, wo, justification, evidence, agent_type, history),
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


# -- opening the gate ------------------------------------------------------------------


def open_gate(store: ProjectStore, grant: dict[str, Any]) -> dict[str, Any]:
    """Spend one use of a grant, and close the requests it has just made moot.

    The second half is what stops a work order finishing while a gate for the very action
    it completed is still sitting in the user's attention list. A grant is scoped to an
    exact command string, so the worker that retries an approved command with a pipe
    appended files a second request for the same action — and then, string mismatch
    resolved, runs the approved one anyway. The moment that grant opens, the second
    request is a question about something that has already happened under authorisation.

    Only requests of the same kind whose command CONTAINS the approved command are
    closed: the approved action plus decoration, never the reverse (a shorter command is
    a different action — dropping `--stage` from a deploy is what restarts the fleet).
    Closing authorises nothing, so the narrow risk of closing one too many is bounded:
    the command stays blocked and a retry files a fresh request for a real review.

    Only an `approved` grant supersedes. A dismissal says the recogniser was wrong about
    one string; whether it was also wrong about a different string is Neo's call, not a
    substring match's.
    """
    approval = store.consume_grant(grant["id"])
    if approval["status"] != "approved":
        return approval
    for pending in store.pending_approvals(approval["wo_id"]):
        if pending["id"] == approval["id"] or pending["kind"] != approval["kind"]:
            continue
        if approval["command"] not in pending["command"]:
            continue
        store.supersede_approval(pending["id"], (
            f"the same {approval['kind']} action ran under approved request "
            f"{approval['id']}, which this command only wraps — nothing is left to "
            f"authorise or refuse, and no authorisation is implied by closing it"
        ))
    return approval


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


# A dismissal has to tell the worker three things a grant does not: that nothing was
# authorised, that nothing about its request was wrong, and that clearing a command
# string is not the same as clearing the action the string talks ABOUT. A worker told
# only "you may proceed" learns to treat the gate as a formality; a worker told "you were
# denied" learns to avoid a command that was always fine. Neither is true here.
#
# The third one is the trap that produced this wording. A dismissed command is very often
# a `jarvis gate request` for some genuinely privileged action — the recogniser fires on
# the action named inside the quoted argument. "Run it again" then reads as "re-file",
# and if the real action is ALREADY sitting with a reviewer the worker opens a second,
# better-argued request for it while the first is undecided. That is reviewer-shopping in
# effect, whatever the worker intended, so the message has to rule it out where the worker
# reads it rather than leaving it to a learning nobody consults mid-turn.
def dismissed_message(approval: dict[str, Any], reason: str, by: str) -> str:
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


def apply_decision(store: ProjectStore, approval_id: int, verdict: str,
                   reason: str, decided_by: str) -> dict[str, Any]:
    """Record a verdict and queue the worker's resume message.

    Shared by Neo's drain and the user's `jarvis gate approve/deny/dismiss`, so a gate
    resolved by either route leaves identical state behind. Returns the updated row.

    A dismissal emits `gate_dismissed` rather than `gate_decided`, deliberately. The two
    are not the same event: `gate_decided` is the record of a privileged action being
    ruled on, and folding false positives into it would inflate exactly the audit trail
    the separate verdict exists to keep honest.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r} — expected one of {list(VERDICTS)}")
    approval = store.decide_approval(approval_id, verdict=verdict, reason=reason,
                                     decided_by=decided_by)
    store.queue_message(approval["wo_id"],
                        _VERDICT_MESSAGE[verdict](approval, reason, decided_by),
                        source="gate")
    if verdict == "dismissed":
        store.add_event(approval["wo_id"], "gate_dismissed", {
            "approval_id": approval_id,
            "by": decided_by,
            "kind": approval["kind"],
            "command": approval["command"],
            "matched": approval["matched"],
            "reason": reason,
        })
    else:
        store.add_event(approval["wo_id"], "gate_decided", {
            "approval_id": approval_id,
            "decision": verdict,
            "by": decided_by,
            "kind": approval["kind"],
            "reason": reason,
        })
    # Reverse what `request` did to the status. It parked the work order in
    # `waiting_input` because a gate request is a wait; the verdict ends the wait, and a
    # status that outlives it is read as a USER blocker by everything downstream —
    # `jarvis status`, the dashboard, and `invariants.true_blockers`, which renders it as
    # "worker is waiting on your input".
    #
    # That reading is wrong in both directions. A worker is very often still mid-turn
    # here: the hook reports the verdict inline, so a dismissal never interrupted it at
    # all. And when it did end its turn, what it waits on is the queued verdict message —
    # the OS's move, not the user's. On wo-52a6164d (the 0.5.4 self-ship) two dismissals
    # inside one turn left the work order reading `waiting_input` for forty minutes while
    # it worked, and the user was asked to unstick a worker that had never stalled.
    #
    # Narrow on both sides. Only from `waiting_input`, so a work order that has since
    # been cancelled or settled keeps where it got to; and only once nothing else is out,
    # since a second request still with Neo — or one escalated to the user, which
    # `pending_approvals` also returns — is still a genuine wait.
    wo_id = approval["wo_id"]
    if (store.get_work_order(wo_id)["status"] == "waiting_input"
            and not store.pending_approvals(wo_id)):
        store.set_status(wo_id, "running")
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
