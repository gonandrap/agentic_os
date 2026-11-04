"""The stop-condition phrase family, and nothing else (docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md)"""

from __future__ import annotations

import re

#: The five, and only the five — §4: a sixth is a one-line diff behind the posture above
STOP_PHRASES = ("do not start until", "do not start before",
                "do not dispatch until", "held until", "blocked until")

#: What may sit in front of a phrase without changing what it says: a bullet
_MARKERS = "-*•>#\t "

#: How long a matched sentence may be where it is quoted back
MAX_SENTENCE = 300

#: A hold on code reaching PRODUCTION, not on it merging (docs/superpowers/specs/2026-10-22-a-deployed-precondition-clears-itself.md)
DEPLOYED_PREFIX = "deployed:"


class PreconditionError(ValueError):
    """A ref that names nothing"""


def _candidate_starts(line: str) -> list[int]:
    """Offsets a stop phrase may begin at: the start of the line, or after `.`/`;`/`:`"""
    starts = [0]
    for i, ch in enumerate(line):
        if ch in ".;:":
            starts.append(i + 1)
    return starts


def states_hold(title: str, description: str) -> str | None:
    """The matched stop-condition sentence, or None"""
    for line in f"{title}\n{description}".splitlines():
        lowered = line.lower()
        for start in _candidate_starts(line):
            at = start
            while at < len(line) and line[at] in _MARKERS:
                at += 1
            for phrase in STOP_PHRASES:
                if not lowered.startswith(phrase, at):
                    continue
                remainder = line[at + len(phrase):].strip(_MARKERS + ":")
                if remainder:
                    return line[at:].strip(_MARKERS + "*")[:MAX_SENTENCE]
    return None


_REF = (r"(?:(?:PR|pull request)\s*#?\d+|#\d+|\S*/pull/\d+"
        r"|wo-[0-9a-f]{8}|io-[0-9a-f]{8}|bl-[0-9a-f]{8})")
_QUOTED_RE = re.compile(r"(?<!\w)'[^'\n]*'(?!\w)|\"[^\"\n]*\"|`[^`\n]*`")
_CLAUSE_RE = re.compile(r"(?<=[.;])\s+|\n")
# Wider than STOP_PHRASES: an OS filing refuses no human's command (issue 1567)
_SEQUENCE_RE = re.compile(
    r"^[-*•>#\s]*(?:(?:start|begin|dispatch)(?:\s+(?:this|it|work))?(?:\s+only)?"
    r"\s+(?:after|once|until)\b|sequenced?\s+(?:this\s+|it\s+)?(?:after|behind)\b)"
    rf"|\b(?:depends on|requires|blocked (?:on|by)|waits? (?:for|on|until)|prerequisite:?)\s+{_REF}"
    rf"|\b(?:after|once|when|until)\s+{_REF}[^.;]*?\b(?:merges?|lands?|ships?|completes?"
    r"|finishes|closes|settles|(?:is|are|has|have)(?:\s+been)?\s+"
    r"(?:merged|landed|shipped|completed?|finished|closed|settled|done))\b",
    re.IGNORECASE)
_ID_RE = re.compile(r"\b(wo|io|bl)-([0-9a-f]{8})\b")
_PR_RE = re.compile(r"(?:\bPR\s*#?|\bpull request\s*#?|/pull/|(?<![\w/])#)(\d+)", re.IGNORECASE)


def states_sequence(*texts: str) -> list[str]:
    """Every clause stating work must wait, untruncated, quoted text aside"""
    clauses: list[str] = []
    for text in texts:
        for clause in _CLAUSE_RE.split(_QUOTED_RE.sub(" ", text or "")):
            if _SEQUENCE_RE.search(clause) or states_hold(clause, ""):
                clauses.append(clause.strip())
    return clauses


def sequence_refs(clause: str) -> list[tuple[str, str]]:
    """What a sequence clause names: (`wo`|`io`|`bl`|`pr`, value)"""
    named = [(kind, f"{kind}-{tail}") for kind, tail in _ID_RE.findall(clause)]
    return named + [("pr", number) for number in _PR_RE.findall(clause)]


def deployed_target(ref: str) -> str:
    """What a `deployed:` ref waits for — a `wo-…` id or a commit sha; `""` for any other ref"""
    if not ref.startswith(DEPLOYED_PREFIX):
        return ""
    return ref[len(DEPLOYED_PREFIX):].strip()


def checked_ref(raw: str) -> str:
    """The ref, stripped"""
    ref = (raw or "").strip()
    if not ref:
        raise PreconditionError(
            "a precondition needs something to point at — `bl-…`, `kn-…`, `io-…`, "
            "`deployed:<wo-id|sha>`, `#issue`, a URL, or the words for the wait")
    if ref.startswith(DEPLOYED_PREFIX):
        target = deployed_target(ref)
        if not target:
            raise PreconditionError(
                "`deployed:` needs the work order or commit whose code must reach "
                "production — `deployed:wo-19e58248` or `deployed:c507208`")
        return f"{DEPLOYED_PREFIX}{target}"
    return ref
