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

## Where the recognisers went

They are no longer here. What counts as an *attempt* at a privileged action is data in
`os.db`, seeded from the constants that used to live in this file and grown from the
dismissal verdicts this module records — see `gate_rules.py` for why, and for the safety
net (canaries) that makes a self-modifying classifier something other than a hole. This
file keeps the lifecycle: what a gate MEANS, who reviews it, and what a verdict does.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from .gate_rules import (  # re-exported: this is still the module callers import
    KIND_NAMES,
    KINDS,
    SELF_HEAL,
    GateKind,
    RuleSet,
    reads_only,
    scannable,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .central_store import CentralStore
    from .neo_store import NeoStore
    from .project_store import ProjectStore

log = logging.getLogger(__name__)

__all__ = [
    "APPROVAL_STATUSES", "AWAITING_CASE", "CASE_TTL_CEILING_SECONDS",
    "DEFAULT_CASE_TTL_SECONDS", "GRANT_MAX_USES",
    "GRANT_TTL_SECONDS", "SELF_HEAL",
    "GateConfig",
    "CONTEST_HEADER", "CONTEST_NOT_AN_AUTHORISATION",
    "GateKind", "GatedAction", "KINDS", "KIND_NAMES", "NO_CASE_JUSTIFICATION",
    "REVIEWER_PERSONA", "RuleSet",
    "VERDICTS", "abandoned_message", "amend_request", "apply_decision",
    "build_contest_question", "build_request_question", "classify", "contest_command",
    "deny_conflicts", "exits_advice", "file_request", "open_gate", "queue_for_review",
    "asks", "kind_of",
    "question_text", "reads_only", "render_user_messages", "request_command", "scannable",
    "summarise", "sweep_unargued",
]

# How long an approval stays usable, and how many attempts it covers. The window is
# short because it is a receipt for an act the worker is about to perform, not standing
# authority. More than one use because a real `gh pr merge` can fail on a transient
# (out-of-date branch, failing check) and forcing a fresh Neo round trip for the retry
# is the sort of friction this whole feature exists to remove.
GRANT_TTL_SECONDS = 3600
GRANT_MAX_USES = 3

APPROVAL_STATUSES = ("awaiting_case", "pending", "approved", "denied", "dismissed",
                     "expired")

# Filed, recorded, and DELIBERATELY not in front of a reviewer yet: the worker ran the
# command instead of asking, so the only justification on the row is the placeholder
# below. Nothing in the OS acts on a request in this status — no Neo question exists for
# it — until the worker comes back and makes its case, which is the one transition out of
# it (`queue_for_review`). See docs/superpowers/specs/2026-09-11-holding-an-unargued-gate.md.
#
# The alternative, filing straight to Neo and letting a late case amend the row, loses the
# race it depends on: the daemon polls every 5s and the worker's next tool call costs a
# model turn, so the reviewer reads the placeholder first nearly every time.
AWAITING_CASE = "awaiting_case"

# How long a held request waits for its case — or its contest — before the OS abandons it.
# It must close: nothing else closes a request no reviewer can see, and an unargued
# privileged action left open for ever is a worse record than a closed one. Per-project via
# `gates.case_ttl_seconds` in the catalog — kn-67cdb54b — because how long a worker
# plausibly takes to come back with test results is a fact about the project, not the OS.
#
# UNDER THE PROMPT CACHE'S TTL, AND THAT IS THE WHOLE CHOICE OF NUMBER. A blocked worker
# is told to end its turn, and when it obeys, NOTHING else will ever wake it: a held
# request has no reviewer and therefore no verdict, so this sweep's own message is the
# next thing the worker ever receives. The clock is therefore the worker's idle gap, and
# an idle gap past the cache TTL re-sends the entire conversation at the cache-WRITE
# rate. Measured on wo-5efc2de6: six cache writes, every one labelled `ttl-expiry`, gaps
# of 8.4-10.2 minutes against the old 600s value, ~914k tokens re-written of which the
# two gate holds account for ~201k. `jarvis inspect wo-5efc2de6` still prints it.
#
# 240s leaves a minute of headroom under `usage.WRITE_TTL_SECONDS`. It is not a guess at
# how long a worker needs — the only thing that clears a hold is ONE tool call, which a
# worker that is still working makes in seconds and a worker that has parked will never
# make however long it waits.
DEFAULT_CASE_TTL_SECONDS = 240.0

#: The ceiling a project's own `gates.case_ttl_seconds` is clamped to, for the reason
#: above. Duplicates `usage.WRITE_TTL_SECONDS` rather than importing it — `gates` is
#: loaded by the PreToolUse hook on every Bash call and `usage` is not on that path — so
#: a test pins the two literals against each other (kn-1449447a §3 is the same pattern).
CASE_TTL_CEILING_SECONDS = 300.0

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

# What the justification says when the hook filed the request because the worker ran the
# command instead of asking. It is a placeholder for a case nobody made — so `amend_request`
# REPLACES it rather than appending under it, and the hook's own advice ("run `jarvis gate
# request` to make the case properly") is what produces the amendment. Named here because
# both writer and reader must agree on the string: see GitHub issue 185.
NO_CASE_JUSTIFICATION = ("(none — the worker ran the command directly rather than filing "
                         "a request, so no case was made for it)")

# The header a CONTESTED request opens with, and the string every reader keys on: the
# reviewer persona's carve-out, and the test fake's branch. A contest is not a request to
# perform a privileged action — it is the assertion that there is no privileged action
# here to perform. See docs/superpowers/specs/2026-09-12-contesting-a-gate-match.md.
CONTEST_HEADER = "CONTESTED GATE MATCH"

# Appended when a reviewer tries to APPROVE a contest. A contest carries an argument that
# the command performs no privileged action; there is no case in it for performing one, so
# an approval would authorise an action nobody ever argued for — issue 185's failure with
# the politeness reversed. Recorded as a denial instead, which is what it factually is.
CONTEST_NOT_AN_AUTHORISATION = (
    "(Recorded as a REFUSAL, not an authorisation. This was a contest: the worker argued "
    "that the command performs no privileged action, and made no case for performing one. "
    "If the action is genuine, it needs a real request — `jarvis gate request` — with a "
    "case a reviewer can check.)"
)


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
    case_ttl_seconds: float = DEFAULT_CASE_TTL_SECONDS

    def __bool__(self) -> bool:
        return bool(self.enabled)

    def to_json(self) -> str:
        return json.dumps({
            "enabled": sorted(self.enabled),
            "patterns": {k: list(v) for k, v in sorted(self.extra_patterns.items())},
            # Rides along to the worker so the hook can tell it how long it has to make
            # the case. A deadline the blocked worker is not told is not a deadline.
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

        ttl_raw = data.get("case_ttl_seconds", DEFAULT_CASE_TTL_SECONDS)
        try:
            ttl = float(ttl_raw)
        except (TypeError, ValueError):
            raise ValueError('"gates.case_ttl_seconds" must be a number of seconds') from None
        if ttl <= 0:
            raise ValueError('"gates.case_ttl_seconds" must be positive — a held request '
                             'that never expires is the leak this setting bounds')
        if ttl >= CASE_TTL_CEILING_SECONDS:
            # CLAMPED, not refused, and the asymmetry with the check above is deliberate.
            # A too-short hold is broken; a too-long one is merely expensive — it parks
            # the worker past the prompt cache's TTL and re-sends the whole conversation
            # at the write rate (see DEFAULT_CASE_TTL_SECONDS). Raising would also break
            # on upgrade: a worker-settings file written by the previous release legally
            # carries 600, and this same `parse` runs inside the PreToolUse hook.
            log.warning(
                "gates.case_ttl_seconds=%.0f is at or past the %.0fs prompt-cache TTL; "
                "clamped to %.0f. A worker parked longer than the cache lives re-sends "
                "its whole conversation at the cache-write rate.",
                ttl, CASE_TTL_CEILING_SECONDS, DEFAULT_CASE_TTL_SECONDS)
            ttl = DEFAULT_CASE_TTL_SECONDS
        return cls(enabled=frozenset(enabled), extra_patterns=extra, case_ttl_seconds=ttl)


@dataclass(frozen=True)
class GatedAction:
    """A command recognised as an attempt at a privileged action."""

    kind: str
    summary: str
    command: str
    matched: str  # the pattern that fired — shown to the reviewer, and to the user
    # Which row of the rule base fired. `builtin` rules are seeded, `neo`/`user` ones were
    # learned; a reviewer looking at a request deserves to know which, because "the OS has
    # always thought this" and "the OS decided this last Tuesday" are different claims.
    rule_id: str = ""


_SUMMARIES = {k.name: k.summary for k in KINDS}


def classify(command: str, config: GateConfig, rules: RuleSet | None = None,
             central: CentralStore | None = None) -> GatedAction | None:
    """The gate this Bash command trips, or None.

    The recognisers come from the rule base rather than from this file — `rules` is what
    the OS currently believes, learned exemptions included. Omitting it loads them, which
    is the right default for a caller that just wants an answer and the wrong one for the
    hook, which already has a store open and should not pay for a second.

    Passing `central` in addition lets a cleared command be *counted*: an exemption that
    never fires is a rule that generalised nothing, and that is worth being able to see.
    """
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


# -- what a blocked worker is told to do ----------------------------------------------


def kind_of(name: str) -> GateKind | None:
    """One `GateKind` by name, or None. The lookup every render site needs and no
    caller should re-derive by scanning `KINDS`."""
    return next((k for k in KINDS if k.name == name), None)


def asks(kind: str) -> tuple[str, str]:
    """What `--why` and `--evidence` should contain FOR THIS GATE. See `GateKind`.

    An unknown kind falls back to the field defaults rather than raising: this renders
    the text a blocked worker reads, and a `KeyError` there would replace usable advice
    with no advice at all.
    """
    found = kind_of(kind)
    if found is None:
        return GateKind(name=kind, summary="").why_ask, GateKind(name=kind,
                                                                 summary="").evidence_ask
    return found.why_ask, found.evidence_ask


def request_command(wo_id: str, command: str, kind: str = "",
                    why: str = "", evidence: str = "") -> str:
    """The command that puts a case for a real privileged action in front of a reviewer.

    The placeholders come from the KIND, because "why this is ready to ship" is a
    question only a release can answer — a worker asked it about a service restart
    writes something that is not what the reviewer needs, and the reviewer sees nothing
    else. Pass `why`/`evidence` to override (a listing shows ellipses; a block shows the
    real question).
    """
    kind_why, kind_evidence = asks(kind)
    return (f"jarvis gate request {wo_id} \"{command}\" "
            f"--why \"{why or f'<{kind_why}>'}\" "
            f"--evidence \"{evidence or f'<{kind_evidence}>'}\"")


def contest_command(wo_id: str, command: str,
                    why: str = "<why this performs no privileged action>") -> str:
    """The command that disputes the MATCH rather than arguing for the action.

    No per-kind ask here, and that is the point: a contest asserts the command performs
    no privileged action of ANY kind, so the question is the same whichever recogniser
    fired.
    """
    return f"jarvis gate contest {wo_id} \"{command}\" --why \"{why}\""


def exits_advice(wo_id: str, command: str, kind: str = "") -> str:
    """The two ways out of a block, and the diagnosis that picks between them.

    ONE RENDERER, because the failure this fixes is a worker handed advice it cannot
    follow. Every surface that blocks a worker prints this — the hook's fresh block, the
    hook's retry message, the abandonment message — and each of them is the only thing
    its reader gets, so a route that exists in one of them and not the others is a route
    that does not exist (kn-467d1ecd).

    `explain` comes first and is the point of the block. A worker cannot always tell
    whether its own command performs the action — that is precisely why the gate matched
    it — and asking it to pick an exit blind is what produced three abandoned requests in
    a row. See docs/superpowers/specs/2026-09-12-contesting-a-gate-match.md §3.

    `kind` shapes the request line's placeholders and nothing else — §6.
    """
    return (
        f"TWO WAYS OUT. If you cannot tell which you need, ask the OS first — it reports "
        f"where the matched literal sits and whether the shell would run it:\n"
        f"    jarvis gate explain \"{command}\"\n\n"
        f"If the command DOES perform the action, make the case:\n"
        f"    {request_command(wo_id, command, kind)}\n\n"
        f"If it does NOT — the recogniser matched text it is only reading or writing "
        f"about (a name in a grep pattern, a path in a commit message, a string in a "
        f"heredoc) — contest the match. That asks a reviewer to DISMISS it as a "
        f"classifier false positive; it authorises nothing and it is not a request for "
        f"permission, so it needs no PR and no test results:\n"
        f"    {contest_command(wo_id, command)}\n\n"
        f"Do not guess between them: answering the request's two questions about a "
        f"command that performs no privileged action means writing something false into "
        f"the only text the reviewer sees, and walking away leaves the block on the "
        f"record with nobody ever told the recogniser was wrong."
    )


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
#
# And the `self_heal` carve-out sits ABOVE it for the same reason it exists at all: a
# self_heal row's `command` is a rendered intent, so the premise check — first, and
# highest priority — describes it exactly, and the persona would have forced DISMISS on
# every proposal and then asked for an `exempt_pattern` derived from prose. See spec
# 2026-09-02, §5.
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


USER_MESSAGE_LIMIT = 5
USER_MESSAGE_CHARS = 700


def render_user_messages(rows: Iterable[dict[str, Any]]) -> list[str]:
    """The user's own words to this work order, as the reviewer's authorisation check.

    Without this the reviewer cannot see an instruction the user gave the worker, ever:
    the request carries the work order's description and the worker's prose and nothing
    the user said after dispatch. That is how gate 85 went to the user for a decision
    they had made in writing twelve minutes earlier (`jarvis wo send`, msg-797, "resolve
    the conflicts and merge the PR, I authorize it").

    ONLY rows `ProjectStore.user_messages` could attribute to the human. Rendering the
    last N messages regardless of author would be worse than not rendering any: a worker
    has a shell and could file its own approval into its own conversation, and the panel
    would read it as the user's. Absent rather than present-and-empty when there are
    none — a blank section reads as "the user said nothing", which is a claim this
    function is not entitled to make about a work order predating the stamp.
    """
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
    """Render the approval request Neo (or the user) reads.

    Whoever decides sees only this text — never the worker's session — so it has to
    carry the case by itself: what is being attempted, under which work order, why the
    worker believes it is ready, whatever it offered as proof, what the USER has said to
    this work order since it was dispatched, and what this same work order has already
    been told about the same gate.

    That fifth item is `user_messages`, and it is passed IN rather than fetched here for
    the same reason the other four are: a reviewer that goes and looks things up makes
    `jarvis gate show` a partial account of what it saw, and makes two runs of the same
    review differ. See `render_user_messages`.

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
        # Labelled with what THIS gate asked for. The label used to read "branch, PR,
        # test results" over a service restart's evidence, which tells the reviewer to
        # judge it against something nobody asked the worker to supply — §6.
        parts += ["", f"Evidence the worker supplied ({asks(action.kind)[1]}):",
                  evidence.strip()[:2000]]
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
    """Where the matched literal sits, as the OS's own structural reading of it.

    The same analysis `jarvis gate explain` prints, handed to the reviewer unasked. The
    premise check is a question about the command's SHAPE — is the literal in executable
    position, and can anything in the chain execute the span it sits in — and a reviewer
    left to eyeball four hundred characters of shell gets it wrong in both directions.
    It is also the one input to the review the worker did not write.
    """
    from .gate_rules import reads_only, shape_of

    shape = shape_of(command, pattern)
    lines = [f"The OS's reading of it: the matched literal is "
             f"{shape.describe() if shape else 'in a position the OS could not parse'}."]
    if reads_only(command):
        lines.append("Every command in the chain is read-only.")
    if shape and not shape.exemptible:
        # Said plainly, because it is the one thing that makes an otherwise convincing
        # contest wrong: a heredoc handed to an interpreter IS code (kn-986fc008).
        lines.append("A dismissal of this could NOT be generalised into a standing rule "
                     "— by this reading the shell could still run the literal.")
    return lines


def build_contest_question(action: GatedAction, wo: dict[str, Any], argument: str,
                           agent_type: str | None = None,
                           history: Iterable[dict[str, Any]] = (),
                           user_messages: Iterable[dict[str, Any]] = ()) -> str:
    """Render a CONTESTED match for the reviewer. See spec 2026-09-12 §1.

    A different question from `build_request_question`, not a variant of it, because the
    subject is different: this asks about the OS's classifier, not about the worker's
    work. Rendering it as an approval request is exactly the failure being fixed — a
    reviewer handed "should this ship?" about a `cat > /tmp/probe.py` has no true answer
    available.
    """
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
        f"Work order: {wo.get('title') or '(untitled)'}",
    ]
    description = (wo.get("description") or "").strip()
    if description:
        parts += ["Work order description:", description[:1200]]
    parts += ["", "The worker's argument that this is a false positive:",
              argument.strip() or "(the worker gave none — deny it; there is nothing "
                                  "here to review)"]
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


# -- filing a request ----------------------------------------------------------------


def file_request(store: ProjectStore, neo: Any, project: str, wo: dict[str, Any],
                 action: GatedAction, justification: str = "", evidence: str = "",
                 max_uses: int = GRANT_MAX_USES, agent_type: str | None = None,
                 hold: bool = False, contested: bool = False,
                 ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Record an approval request. Returns (approval, question), question None if held.

    One path for both entry points — the hook (a worker that just ran the command) and
    `jarvis gate request` (a worker making its case first) — so a gate behaves the same
    however it was reached.

    `hold` is what makes the two differ in the one way they must. The hook has no case to
    pass on, so it files `AWAITING_CASE`: recorded, the worker parked, and NO Neo question
    yet, because the case is still coming. `queue_for_review` is the only way out.

    The request rides the existing Neo queue rather than a parallel review pipeline,
    which is what gives it escalation-to-user, `jarvis neo list` and answer delivery
    without reimplementing any of them.

    `agent_type` reaches here only from the hook: `jarvis gate request` is a shell
    command, so whoever ran it had a shell, and the seats have none.
    """
    approval = store.add_approval(
        wo["id"], action.kind, action.command, matched=action.matched,
        justification=justification, evidence=evidence, max_uses=max_uses,
        agent_type=agent_type, status=AWAITING_CASE if hold else "pending",
        contested=contested,
    )
    question = None if hold else queue_for_review(store, neo, project, wo, action,
                                                  approval)
    # The worker has nothing to do until a verdict lands. Saying so keeps the reconciler
    # from reading the idle session as "finished without `jarvis wo finish`" and filing
    # it for review — a gate request is a wait, not an abandonment. True of a held request
    # too: what it waits for is the worker's own next command.
    if wo.get("status") in ("running", "dispatching"):
        store.set_status(wo["id"], "waiting_input")
    return approval, question


def question_text(store: ProjectStore, wo: dict[str, Any], action: GatedAction,
                  approval: dict[str, Any]) -> str:
    """The reviewer's page for this request, whichever kind of claim it carries.

    The single place the two renderers are chosen between, so a contest cannot reach a
    reviewer dressed as an authorisation request through some second path — which is the
    property `queue_for_review` and `amend_request` would otherwise each have to keep.
    """
    # Read before the question, so the reviewer's history is everything BUT this request.
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
    """Put a recorded request in front of a reviewer. Returns the Neo question.

    The single door from `AWAITING_CASE` to `pending`, and the single place a reviewer's
    question text is first written — so "no reviewer ever reads a request nobody argued
    for" is a property of one function rather than a convention six callers must keep.
    """
    question = neo.ask(
        project, wo["id"], question_text(store, wo, action, approval),
        context=f"{wo.get('title') or ''}\n{(wo.get('description') or '')[:800]}",
        kind="approval",
    )
    store.start_review(approval["id"], question["id"])
    return question


def abandoned_message(approval: dict[str, Any], ttl_seconds: float) -> str:
    """What the worker is told when its held request timed out. NOT a verdict.

    The status is what changed here (`expired`, not `denied`) and the message is why that
    is affordable: `expired` used to be ruled out precisely because nothing told the
    worker about it, and the worker is who has to act. Now something does.
    """
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
        + exits_advice(approval["wo_id"], approval["command"], approval["kind"])
    )


def sweep_unargued(store: ProjectStore, ttl_seconds: float) -> list[dict[str, Any]]:
    """Close every held request whose case never came. Returns the rows it closed.

    The cost of holding a request back from review is that nothing else will ever close
    it: there is no question for Neo to answer and no escalation for the user to see, so
    without this a worker that wandered off leaves an unargued privileged action open for
    ever. The clock starts at filing, and it is the project's (`gates.case_ttl_seconds`).

    ABANDONED, NOT DENIED — the reversal of the original design, and spec 2026-09-12 §4
    has the argument. Nobody reviewed it, so nobody can refuse it; `denied` asserts that a
    reviewer found the request wanting, and on the evidence these are mostly commands that
    were never privileged at all (gate 95 is permanently on the record as a `release`
    action denied by the OS — it was a `python3 -c` that imported a module). The objection
    that answered this before was that `expired` is a status nobody tells the worker
    about; `abandoned_message` is the answer to it.

    The command string stays blocked either way, so nothing is authorised and a retry gets
    a real review.
    """
    from . import db
    from .invariants import end_wait_if_nothing_is_out

    cutoff = db.now() - ttl_seconds
    closed = []
    for approval in store.list_approvals(statuses=(AWAITING_CASE,)):
        if approval["ts"] > cutoff:
            continue
        row = store.abandon_approval(
            approval["id"],
            reason=(f"no case was made for it within {int(ttl_seconds // 60)} minutes, "
                    f"and the match was never contested. Nobody reviewed it."),
        )
        store.queue_message(row["wo_id"], abandoned_message(row, ttl_seconds),
                            source="gate")
        # The request parked the work order (`file_request`); the close has to unpark it,
        # or a work order nobody is reviewing anything for reads as "waiting on your
        # input" for ever. Same reason `apply_decision` does it, same narrow guard.
        end_wait_if_nothing_is_out(store, row["wo_id"])
        closed.append(row)
    return closed


def _merge_case(existing: str, addition: str) -> str:
    """The prior case plus the new one, as one field a reviewer reads top to bottom.

    The placeholder is a statement that no case exists, so a real case REPLACES it —
    leaving it above an amendment would keep telling the reviewer the exact falsehood
    issue 185 is about. Anything else the worker wrote stands and the new text is
    appended under it: the first case may be what a reviewer has already partly read,
    and silently overwriting it would lose an argument nobody retracted.
    """
    prior = existing.strip()
    addition = addition.strip()
    if not addition:
        return existing
    if not prior or prior == NO_CASE_JUSTIFICATION:
        return addition
    if addition in prior:
        return existing
    return f"{prior}\n\nAMENDED by the worker:\n{addition}"


def amend_request(store: ProjectStore, neo: Any, wo: dict[str, Any],
                  action: GatedAction, approval: dict[str, Any],
                  justification: str = "", evidence: str = "") -> dict[str, Any]:
    """Attach a case to a request that is already pending. Returns the amended approval.

    The alternative — filing a second request — is reviewer-shopping in effect even when
    it is not in intent (kn-76b155a0), and the alternative to that, refusing the
    amendment, throws the worker's text away, which is issue 185 itself. So the case goes
    onto the standing request and the reviewer's question text is rewritten in place.

    IN PLACE, never a fresh question re-linked to this approval: `daemon._deliver_gate_verdict`
    resolves the approval through `approvals.neo_question_id`, so moving that pointer
    while a verdict is in flight strands the verdict with nothing to apply it to.

    A question Neo is mid-review on, or one already escalated to the user, is amended
    all the same — the reviewer may have read the earlier text, and the caller is told so
    rather than the worker losing the case for a race it cannot see.
    """
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


# -- opening the gate ------------------------------------------------------------------


def open_gate(store: ProjectStore, grant: dict[str, Any],
              neo: NeoStore | None = None) -> dict[str, Any]:
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
    # Held requests are swept too. One filed by the hook is the likeliest duplicate there
    # is — the decorated retry that tripped the gate is exactly how the second row gets
    # written — and leaving it would keep an unargued request open for an action that has
    # already run under authorisation.
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
        # The approval closes; its Neo question does not, and if Neo had escalated that
        # question it went on asking the user to rule on an action that had already run
        # (production question 118, still in the attention list a week later). Only
        # `decide_gate` ever closed one of these, and this path is not it.
        if neo is not None and pending["neo_question_id"]:
            neo.supersede(
                pending["neo_question_id"],
                f"SUPERSEDED by approval {approval['id']}",
                f"the {approval['kind']} action ran under approved request "
                f"{approval['id']}; nothing here authorises anything",
            )
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
    # A denied CONTEST refuses a different claim, so it has to say so: the worker argued
    # the gate matched nothing, and the reviewer found that the command really does
    # perform the action. Telling it "address the reason and request approval afresh" is
    # right — but only once it knows which of its two claims was rejected.
    if approval.get("contested"):
        return (
            f"[Gate {approval['id']} — CONTEST REJECTED by {by}] {reason}\n\n"
            f"The command that stays blocked:\n    {approval['command']}\n\n"
            f"You argued that this performs no privileged action and the reviewer "
            f"disagreed, so the match stands. Do not retry it as-is. If the work is "
            f"genuinely ready, make the real case:\n"
            f"    {request_command(approval['wo_id'], approval['command'], approval['kind'])}\n"
            f"Otherwise leave it and finish the work order explaining what is left."
        )
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
def dismissed_message(approval: dict[str, Any], reason: str, by: str,
                      learned: dict[str, Any] | None = None) -> str:
    # What the OS learned is told to the worker, not just written to the record. A worker
    # that has just lost a turn to a false positive is the one participant who can tell
    # whether the generalisation is right, and it is about to write a PR body and a
    # summary describing this work — the same prose that tripped the gate in the first
    # place.
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
    """Turn a dismissal into a standing rule, and report what happened either way.

    This is the feedback loop, and it is three lines of consequence: a reviewer said "the
    classifier was wrong about this shape", and the classifier now knows. Before this
    existed the same reviewer said the same thing on four requests in one work order, and
    the OS forgot each time.

    Never raises. A rule base that cannot be written to is a degradation — the gate still
    works, it just stops learning — and turning it into an exception would convert a
    missed improvement into a failed verdict, blocking a worker over the OS's own
    bookkeeping.
    """
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
    """Record a verdict and queue the worker's resume message.

    Shared by Neo's drain and the user's `jarvis gate approve/deny/dismiss`, so a gate
    resolved by either route leaves identical state behind. Returns the updated row.

    A dismissal emits `gate_dismissed` rather than `gate_decided`, deliberately. The two
    are not the same event: `gate_decided` is the record of a privileged action being
    ruled on, and folding false positives into it would inflate exactly the audit trail
    the separate verdict exists to keep honest.

    A dismissal ALSO teaches the classifier — see `learn_from_dismissal`. What was learned
    (or why nothing could be) rides on the same event rather than a new one, so that the
    timeline reads as one fact about one request: the recogniser was wrong, and here is
    what the OS did about it.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r} — expected one of {list(VERDICTS)}")
    # A CONTEST CAN NEVER BECOME AN AUTHORISATION, and this is where that is true rather
    # than in the persona — a prompt is advice, and the record is what the audit trail is
    # made of. Coerced rather than raised because the caller is usually the daemon
    # delivering Neo's verdict: refusing it there would strand a worker waiting for an
    # answer over the OS's own bookkeeping. `ops.decide_gate` refuses it earlier, where
    # there is a human to tell. Spec 2026-09-12 §2.
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
    # A `self_heal` request was filed by the OS against a session that never asked, so
    # the two moves this function makes for a worker are both wrong for it. The message
    # is addressed to nobody: on a denial it is noise delivered into a running turn —
    # precisely the act this gate exists to fence — and on an approval it is redundant,
    # because the remedy itself is the intervention. And there is no wait to end: nothing
    # set one, since `remedies.propose` deliberately does not park the work order.
    #
    # ONE GUARD IN THE SHARED FUNCTION, NOT A SECOND DECISION PATH. Two paths over one
    # table is how the two come to leave different state behind; everything below —
    # `decide_approval`, the `gate_decided`/`gate_dismissed` event, dismissal learning —
    # is inherited unchanged. See
    # docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §5.
    self_heal = approval["kind"] == SELF_HEAL
    if self_heal:
        from . import remedies

        remedies.record_verdict(store, approval, verdict, reason, decided_by,
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
    # `pending_approvals` also returns — is still a genuine wait. `end_wait_if_nothing_is_out`
    # counts an unanswered Neo QUESTION as out too: this work order can be waiting on both.
    #
    # A `self_heal` verdict skips it entirely, for the reason at the top of the guard:
    # there was no wait to end, so the only thing this could do here is move a status
    # nothing in this flow set.
    if not self_heal:
        from .invariants import end_wait_if_nothing_is_out

        end_wait_if_nothing_is_out(store, approval["wo_id"])
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
