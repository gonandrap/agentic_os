"""Where a session's TIME went — the other half of `jarvis cost` (docs/superpowers/specs/2026-08-30-the-anatomy-of-a-turn.md)"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import navigation
from . import usage as usage_mod
from .catalog import (DEFAULT_INSPECT_REPORT_JOIN_FLOOR,
                      DEFAULT_INSPECT_REPORT_WRITE_FLOOR, InspectConfig)
from .holds import HOLD_CAUSES, Hold, by_cause

#: Cache TTLs in seconds
TTL_5M = 300.0
TTL_1H = 3600.0

#: Tools whose span is the lead agent WAITING rather than working: it has dispatched something and is...
JOIN_TOOLS = ("TaskOutput",)

#: Tools whose span is the lead DELEGATING and then waiting for the result (docs/superpowers/specs/2026-09-29-a-runaway-tool-call-is-not-a-slow-subagent.md §1)
DELEGATION_TOOLS = ("Agent", "Task")

#: Tools whose span NAMES a subagent, and the only evidence a subagent is attached on (spec §4b)
SPAWN_TOOLS = ("Agent", "TaskOutput")

#: How many directory levels of subagent this module actually reads
SUBAGENT_DEPTH_READ = 1

COLD_START, TTL_EXPIRY, PREFIX_MISS = "cold-start", "ttl-expiry", "prefix-miss"
#: The fourth, and the only one the OS chose
COMPACTION = "compaction"

#: The buckets a wall clock divides into, in the order they are rendered
PARTS = ("generating", "blocked", "tools", "idle", "unaccounted")

WRITE_CAUSE_NOTES = {
    COLD_START: "the first call of the session — unavoidable",
    TTL_EXPIRY: "the cache had expired: nothing was called for longer than the TTL",
    PREFIX_MISS: "the prefix was re-written while it was still warm — a defect",
    COMPACTION: "the OS compacted the conversation here — this is the summary being "
                "written, and it is the price of not re-sending the whole history",
}

#: How a turn's injected prompt is recognised, most specific first
TRIGGERS: tuple[tuple[str, str], ...] = (
    ("<task-notification>", "a subagent finished"),
    ("[Neo, answering for the user]", "a Neo answer"),
    ("You are the worker agent for", "dispatch"),
    ("You are the PLANNER for", "dispatch"),
    ("You are the MANAGER", "dispatch"),
)
MESSAGE_TRIGGER = "a message"


def _first_line(text: str, limit: int) -> str:
    line = " ".join(text.split())
    return line[:limit] + "…" if len(line) > limit else line


# -- tool parameters: what may be reported, and how much of it

#: THE SHAPES, COPIED FROM `autoreview` AND NOT IMPORTED
PARAM_SECRET_SHAPES: tuple[tuple[str, str], ...] = (
    # The whole block, not the BEGIN line: substituting the header alone would leave the base64 body...
    (r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----.*?"
     r"(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)", "a private key block"),
    (r"\bssh-(?:rsa|dss|ed25519)\s+AAAA[0-9A-Za-z+/=]+", "an ssh key line"),
    (r"\bAuthorization[\"\']?\s*:\s*[\"\']?(?:Bearer|Basic|Token)\s+\S+",
     "an Authorization header value"),
    # FROM HERE DOWN, NOT IN `autoreview`
    (r"\bsk-ant-[A-Za-z0-9_-]{8,}", "an Anthropic API key"),
    (r"\bsk-(?:[A-Za-z0-9]+-)?[A-Za-z0-9_-]{16,}", "an sk- API key"),
    (r"\bgithub_pat_[A-Za-z0-9_]{8,}|\bgh[poshur]_[A-Za-z0-9]{8,}", "a GitHub token"),
    (r"\bAKIA[0-9A-Z]{16}\b", "an AWS access key id"),
    (r"\bxox[abpr]-[A-Za-z0-9-]{8,}", "a Slack token"),
    # `keep` survives the substitution: the scheme
    (r"(?P<keep>\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s:/@]{3,}(?=@)",
     "a password in a URL"),
    (r"(?P<keep>\bcurl\b[^\n]*?\s-u[ =]+[^\s:@]+:)[^\s\"\']+",
     "a curl -u credential"),
    # The VALUE shape is spelled into the pattern rather than tested afterwards
    (r"(?P<keep>--(?:password|token|api[-_]?key)[ =]+)"
     r"(?=[A-Za-z0-9+/=._~-]*[A-Za-z])(?=[A-Za-z0-9+/=._~-]*[0-9])"
     r"[A-Za-z0-9+/=._~-]{6,}",
     "a credential passed as a flag"),
)

#: An assignment whose left side NAMES a credential
_PARAM_ASSIGNMENT_RE = re.compile(
    r"(?P<head>^[ \t]*(?:(?:export|set|const|let|var|readonly)[ \t]+)?"
    r"(?P<quote>[\"\']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P=quote)"
    r"[ \t]*(?:=>|:=|=|:)[ \t]*)"
    r"(?P<value>[^\r\n]*?)(?P<tail>[ \t]*[,;]?[ \t]*)$")

#: THE SAME ASSIGNMENT, ANYWHERE IN THE LINE
_PARAM_INLINE_ASSIGNMENT_RE = re.compile(
    r"(?P<head>(?:^|[\s;&|(\"'\[{])(?:(?:export|env|set)[ \t]+)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)=)"
    r"(?P<value>[^\s;&|]+)")

#: Characters that CLOSE the context around a value instead of belonging to it: the closing quote of...
_PARAM_VALUE_CLOSERS = "\"')]}"

#: Words that make a name a credential's name
_PARAM_SECRET_NAMES = frozenset({
    "key", "keys", "apikey", "accesskey", "privatekey", "secretkey",
    "token", "tokens", "authtoken", "secret", "secrets", "clientsecret",
    "password", "passwd", "passphrase", "pwd", "credential", "credentials",
})

#: A VALUE THAT IS NOT A SECRET, however secret its name — the negative control
_PARAM_PLACEHOLDER_RE = re.compile(
    r"none|null|nil|nan|true|false|x+|\.+|-+|_+|"
    r"todo|tbd|fixme|changeme|change[-_]me|placeholder|redacted|dummy|fake|sample|"
    r"example|examples|test|testing|secret|password|passwd|token|key|value|"
    r"your[-_].*|my[-_].*|some[-_].*|the[-_].*", re.IGNORECASE)
_PARAM_VALUE_CHARS = re.compile(r"[A-Za-z0-9+/=._~-]+")
_PARAM_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")
_PARAM_SHAPE_RE = [(re.compile(p, re.IGNORECASE | re.DOTALL), name)
                   for p, name in PARAM_SECRET_SHAPES]

CREDENTIAL_VALUE_MARKER = "<redacted: a credential value>"


def _names_a_credential(identifier: str) -> bool:
    parts = [p.lower() for p in _PARAM_WORD_SPLIT_RE.split(identifier) if p]
    return any(p in _PARAM_SECRET_NAMES for p in parts)


def _looks_like_a_credential(raw: str) -> bool:
    """`autoreview._secret_value`'s test, same reasoning: an expression is not a value"""
    value = raw.strip()
    quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
    if quoted:
        value = value[1:-1].strip()
    if len(value) < 6 or not _PARAM_VALUE_CHARS.fullmatch(value):
        return False
    if _PARAM_PLACEHOLDER_RE.fullmatch(value):
        return False
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    return (has_digit and has_alpha) or len(value) >= 20


def _assigned_credential(raw: str) -> tuple[bool, str]:
    """Is this assigned value a credential, and what CLOSERS trail it: `(yes, tail)`"""
    if _looks_like_a_credential(raw):
        return True, ""
    trimmed = raw.rstrip(_PARAM_VALUE_CLOSERS)
    return _looks_like_a_credential(trimmed), raw[len(trimmed):]


def _redact_assignment(m: "re.Match[str]") -> str:
    """One inline assignment, kept unless BOTH halves say credential"""
    is_credential, closers = _assigned_credential(m.group("value"))
    if _names_a_credential(m.group("name")) and is_credential:
        return m.group("head") + CREDENTIAL_VALUE_MARKER + closers
    return m.group(0)


def redact_param(text: str) -> str:
    """One tool-input value with its secret-shaped content NAMED, never quoted"""
    if not text:
        return text
    # ASSIGNMENTS BEFORE SHAPES, and the order is load-bearing: `API_KEY=sk-live-…` is one finding
    lines = []
    for line in text.split("\n"):
        line = _PARAM_INLINE_ASSIGNMENT_RE.sub(_redact_assignment, line)
        m = _PARAM_ASSIGNMENT_RE.match(line)
        if m and _names_a_credential(m.group("name")):
            is_credential, closers = _assigned_credential(m.group("value"))
            if is_credential:
                line = (m.group("head") + CREDENTIAL_VALUE_MARKER + closers
                        + m.group("tail"))
        lines.append(line)
    text = "\n".join(lines)
    for pattern, name in _PARAM_SHAPE_RE:
        text = pattern.sub(
            lambda m, n=name: (m.groupdict().get("keep") or "") + f"<redacted: {n}>",
            text)
    return text


#: How deep the leaf walk goes. 12 is far past any real tool input (`MultiEdit` is two)
_PARAM_WALK_MAX_DEPTH = 12

#: What stands in for that subtree
PARAM_DEPTH_MARKER = f"<not walked: nested past depth {_PARAM_WALK_MAX_DEPTH}>"


def _credential_pair(key: Any, value: Any) -> bool:
    """A credential-NAMED key holding a credential-SHAPED value"""
    return (isinstance(key, str) and isinstance(value, str)
            and _names_a_credential(key) and _assigned_credential(value)[0])


def _redact_leaves(value: Any, depth: int = 0) -> Any:
    """Every STRING LEAF of a nested tool input redacted, structure untouched"""
    if isinstance(value, str):
        return redact_param(value)
    if depth >= _PARAM_WALK_MAX_DEPTH:
        return PARAM_DEPTH_MARKER
    if isinstance(value, dict):
        walked: dict[Any, Any] = {}
        for k, v in value.items():
            redacted_key = _redact_leaves(k, depth + 1)
            # Two distinct keys can redact ALIKE, and a comprehension would drop the first silently
            if isinstance(redacted_key, str) and redacted_key in walked:
                nth = 2
                while f"{redacted_key} #{nth}" in walked:
                    nth += 1
                redacted_key = f"{redacted_key} #{nth}"
            walked[redacted_key] = (CREDENTIAL_VALUE_MARKER
                                    if _credential_pair(k, v)
                                    else _redact_leaves(v, depth + 1))
        return walked
    if isinstance(value, (list, tuple)):
        return [_redact_leaves(v, depth + 1) for v in value]
    return value


@dataclass(frozen=True)
class ParamCaps:
    """How much of a tool's input may be reported, per value, per span and per turn"""

    per_value: int = 500
    per_span: int = 2_000
    per_turn: int = 20_000

    def as_dict(self) -> dict[str, int]:
        return {"per_value": self.per_value, "per_span": self.per_span,
                "per_turn": self.per_turn}


PARAM_CAPS = ParamCaps()


def _params_of(payload: Any, spent: int,
               caps: ParamCaps = PARAM_CAPS,
               ) -> tuple[dict[str, str], list[str], list[str], int]:
    """One tool input, redacted and bounded: `(params, truncated, dropped, spent)`"""
    if not isinstance(payload, dict):
        return {}, [], [], spent
    params: dict[str, str] = {}
    truncated: list[str] = []
    dropped: list[str] = []
    span_spent = 0
    for key, value in payload.items():
        if isinstance(value, str):
            # Same pair test the leaf walk runs, at the top level it never reaches
            text = CREDENTIAL_VALUE_MARKER if _credential_pair(key, value) else value
        else:
            text = json.dumps(_redact_leaves(value), default=str)
        # Belt and braces: the line pass also runs over the serialised form
        text = redact_param(text)
        if len(text) > caps.per_value:
            text = text[:caps.per_value] + "…"
            truncated.append(str(key))
        if (span_spent + len(text) > caps.per_span
                or spent + len(text) > caps.per_turn):
            dropped.append(str(key))
            if str(key) in truncated:
                truncated.remove(str(key))
            continue
        params[str(key)] = text
        span_spent += len(text)
        spent += len(text)
    return params, truncated, dropped, spent


@dataclass
class ToolSpan:
    """One tool call, from the model asking for it to the result coming back"""

    name: str
    tool_id: str
    started: float
    ended: float = 0.0
    detail: str = ""
    #: The tool's whole `input`, redacted and capped (`_params_of`) (docs/superpowers/specs/2026-09-24-order-observability.md)
    params: dict[str, str] = field(default_factory=dict)
    #: Keys shortened to `ParamCaps.per_value`, and keys left out because the span or turn budget ran out
    params_truncated: list[str] = field(default_factory=list)
    params_dropped: list[str] = field(default_factory=list)
    #: `run_in_background: true` on the call that opened this span
    backgrounded: bool = False
    #: Does this Bash call read or search SOURCE?
    navigates_source: bool | None = None

    @property
    def finished(self) -> bool:
        return self.ended > self.started

    @property
    def seconds(self) -> float:
        return self.ended - self.started if self.finished else 0.0

    @property
    def is_join(self) -> bool:
        if self.name in JOIN_TOOLS:
            return True
        return self.name in DELEGATION_TOOLS and not self.backgrounded

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "tool_id": self.tool_id, "started": self.started,
                "ended": self.ended, "seconds": round(self.seconds, 2),
                "detail": self.detail, "join": self.is_join,
                "finished": self.finished, "params": self.params,
                "params_truncated": self.params_truncated,
                "params_dropped": self.params_dropped}


@dataclass
class Prompt:
    """One prompt that landed in the session — the reason a turn happened"""

    ts: float
    kind: str
    quote: str
    source: str = "sdk"

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "quote": self.quote,
                "source": self.source}


@dataclass
class Write:
    """One cache write big enough to classify, with the cause it is attributed to"""

    ts: float
    written: int
    read: int
    gap: float
    cause: str
    model: str = ""
    ttl: float = TTL_5M

    @property
    def note(self) -> str:
        return WRITE_CAUSE_NOTES[self.cause]

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "written": self.written, "read": self.read,
                "gap": round(self.gap, 2), "cause": self.cause, "note": self.note,
                "model": self.model, "ttl": self.ttl}


@dataclass
class Turn:
    """One turn of the conversation, and the three ways its wall clock was spent"""

    seq: int
    started: float
    ended: float
    #: Which transcript turn of that ONE OS turn this is, 1-based (docs/superpowers/specs/2026-09-27-a-request-in-flight-is-not-a-stalled-turn.md §3.)
    part: int = 1
    #: The last thing the token accounting can see inside this turn
    active_ended: float = 0.0
    #: When this turn's LAST INPUT ROW landed with no assistant row after it (docs/superpowers/specs/2026-09-27-a-request-in-flight-is-not-a-stalled-turn.md §1)
    awaiting_since: float = 0.0
    triggers: list[Prompt] = field(default_factory=list)
    spans: list[ToolSpan] = field(default_factory=list)
    calls: list[usage_mod.Call] = field(default_factory=list)
    #: The cold boundaries that fell in THIS turn
    boundaries: list[usage_mod.Boundary] = field(default_factory=list)
    #: The intervals of THIS turn the OS's own record says it was held (`holds.held`)
    holds: list[Hold] = field(default_factory=list)
    #: The subagents this turn's own spans NAME (`_attach_subagents`)
    subagents: list[SubagentAnatomy] = field(default_factory=list)

    @property
    def wall(self) -> float:
        return max(0.0, self.ended - self.started)

    @property
    def held(self) -> float:
        """Wall clock this turn was not permitted to run — see `holds`"""
        return min(self.wall, sum(h.overlap(self.started, self.ended)
                                  for h in self.holds))

    @property
    def active(self) -> float:
        """THE CLOCK A THRESHOLD BELONGS ON: wall minus every recorded hold"""
        return max(0.0, self.wall - self.held)

    def held_by(self) -> dict[str, float]:
        return by_cause(self.holds, self.started, self.ended)

    @property
    def blocked(self) -> float:
        return sum(s.seconds for s in self.spans if s.is_join)

    @property
    def tools(self) -> float:
        return sum(s.seconds for s in self.spans if not s.is_join)

    @property
    def idle(self) -> float:
        """After the worker's last API call, before the next turn's prompt"""
        if not self.active_ended:
            return 0.0
        return max(0.0, self.ended - self.active_ended)

    @property
    def _remainder(self) -> float:
        """The wall clock nothing else accounts for — `generating` or `unaccounted`"""
        return max(0.0, self.wall - self.blocked - self.tools - self.idle)

    @property
    def observed(self) -> bool:
        """Did anything in this turn actually reach the API?"""
        return bool(self.calls)

    @property
    def awaiting(self) -> bool:
        """A request is in flight and no content block has completed — spec §1"""
        return bool(self.awaiting_since)

    @property
    def generating(self) -> float:
        """The remainder, but only where an API call vouches for it"""
        return self._remainder if self.observed else 0.0

    @property
    def unaccounted(self) -> float:
        """The remainder of a turn that never reached the API — see the module docstring"""
        return 0.0 if self.observed else self._remainder

    @property
    def context_peak(self) -> int:
        """The largest context any one call of this turn carried — §1 step 1"""
        return max((c.context for c in self.calls), default=0)

    @property
    def unfinished(self) -> int:
        return sum(1 for s in self.spans if not s.finished)

    @property
    def usage(self) -> usage_mod.Usage:
        """This turn's tokens, priced — AND classified, which `priced` cannot do (#867)"""
        total = usage_mod.Usage()
        for call in self.calls:
            total = total + usage_mod.priced(
                call.model, messages=1, input=call.input,
                cache_write=call.cache_write, cache_read=call.cache_read,
                output=call.output, cache_1h=call.cache_1h, cache_5m=call.cache_5m)
        total.context_peak = self.context_peak
        # `usage`'s definition at this grain
        total.rewrite_excess = max(
            0, sum(c.cache_write for c in self.calls) - self.context_peak)
        usage_mod.fold_boundaries(total, self.boundaries)
        return total

    def share(self) -> dict[str, float]:
        """The partition as fractions of the wall clock, or all zero for an empty turn"""
        if self.wall <= 0:
            return {k: 0.0 for k in PARTS}
        return {k: getattr(self, k) / self.wall for k in PARTS}

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "part": self.part,
            "started": self.started, "ended": self.ended,
            "wall": round(self.wall, 2),
            # BOTH CLOCKS, ALWAYS, and the wall one first: it is the honest answer to "how long did this take in...
            "active": round(self.active, 2), "held": round(self.held, 2),
            "held_by": {k: round(v, 2) for k, v in self.held_by().items()},
            "generating": round(self.generating, 2),
            "blocked": round(self.blocked, 2), "tools": round(self.tools, 2),
            "idle": round(self.idle, 2),
            "unaccounted": round(self.unaccounted, 2),
            # THE HEADLINE FACT ABOUT A TURN
            "observed": self.observed,
            # ADDITIVE, and no key above reads them — spec §1, the rule `subagents` was added under
            "awaiting": self.awaiting, "awaiting_since": self.awaiting_since,
            "share": {k: round(v, 4) for k, v in self.share().items()},
            "context_peak": self.context_peak,
            "api_calls": len(self.calls), "tool_calls": len(self.spans),
            "unfinished_tool_calls": self.unfinished,
            "triggers": [p.as_dict() for p in self.triggers],
            # ADDITIVE, and carried because the renderer DERIVES NOTHING (spec §4c): `jarvis inspect --params`...
            "spans": [s.as_dict() for s in self.spans],
            "usage": self.usage.as_dict(),
            # ADDITIVE and empty on most turns: every key above is unchanged
            "subagents": [s.as_dict() for s in self.subagents],
        }


def _rewrite_block(boundaries: Sequence[usage_mod.Boundary], *,
                   written: int, excess: int) -> dict[str, int | None]:
    """A boundary census, keyed to `jarvis cost`'s own `rewrite` block"""
    def count(cause: str) -> int:
        return sum(1 for b in boundaries if b.cause == cause)

    def written_at(cause: str) -> int:
        return sum(b.cache_write for b in boundaries if b.cause == cause)

    return {
        "boundaries": len(boundaries),
        "ttl_boundaries": count(usage_mod.BOUNDARY_TTL),
        "compact_boundaries": count(usage_mod.BOUNDARY_COMPACTED),
        "undecided_boundaries": count(usage_mod.BOUNDARY_UNDECIDED),
        "ttl_write": written_at(usage_mod.BOUNDARY_TTL),
        "prefix_write": written_at(usage_mod.BOUNDARY_PREFIX),
        "compact_write": written_at(usage_mod.BOUNDARY_COMPACTED),
        "cache_write": written,
        "tokens": excess,
    }


@dataclass
class SubagentAnatomy:
    """One subagent's OWN anatomy: its turns, its cache writes, its context peak"""

    task_id: str
    label: str = ""
    turns: list[Turn] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)
    #: Subagents of THIS subagent, counted and not read — see `SUBAGENT_DEPTH_READ`
    deeper: int = 0
    #: THRESHOLD-FREE, and the reason this class needed more than `writes`: a subagent that wrote 334,427...
    total_written: int | None = None
    #: The largest single write, floor-free
    max_write: int | None = None
    #: The floor `writes` was built at
    write_floor: int | None = None
    #: `usage.classify_boundaries` over THIS subagent's calls — the threshold-free census q1215 requires
    boundaries: list[usage_mod.Boundary] = field(default_factory=list)
    #: Calls, carried as a count so `total_written` can be read as a rate
    api_call_count: int | None = None

    @property
    def wall(self) -> float:
        return sum(t.wall for t in self.turns)

    @property
    def api_calls(self) -> int:
        return sum(len(t.calls) for t in self.turns)

    @property
    def context_peak(self) -> int:
        return max((t.context_peak for t in self.turns), default=0)

    def writes_by_cause(self) -> dict[str, int]:
        """Tokens written, totalled per cause"""
        totals: dict[str, int] = {}
        for write in self.writes:
            totals[write.cause] = totals.get(write.cause, 0) + write.written
        return totals

    def rewrite(self) -> dict[str, int | None] | None:
        """This subagent's own boundary census, same keys as `Anatomy.rewrite()`"""
        if self.total_written is None:
            return None
        return _rewrite_block(self.boundaries, written=self.total_written,
                              excess=max(0, self.total_written - self.context_peak))

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "label": self.label,
            "turns": [t.as_dict() for t in self.turns],
            "writes": [w.as_dict() for w in self.writes],
            "writes_by_cause": self.writes_by_cause(),
            "context_peak": self.context_peak,
            "wall": round(self.wall, 2), "api_calls": self.api_calls,
            "deeper": self.deeper, "depth_read": SUBAGENT_DEPTH_READ,
            # THRESHOLD-FREE, beside the floored list and never instead of it (spec 2026-10-02 §1.1):...
            "total_written": self.total_written, "max_write": self.max_write,
            "write_floor": self.write_floor,
            "api_call_count": self.api_call_count,
            # The CENSUS, not the rows: the per-turn `boundaries` key already exists at the parent grain and a...
            "rewrite": self.rewrite(),
        }


#: The navigation reading's WORDING
NAV_LABELS = {"symbol_calls": "symbol", "source_nav_calls": "source nav",
              "unclassified": "unclassified"}


def nav_line(nav: dict[str, int]) -> str:
    """The three counts as three SEPARATE numbers — §3 (#227)"""
    line = (f"navigation — {nav.get('symbol_calls', 0)} {NAV_LABELS['symbol_calls']}, "
            f"{nav.get('source_nav_calls', 0)} {NAV_LABELS['source_nav_calls']}")
    if nav.get("unclassified"):
        line += f", {nav['unclassified']} {NAV_LABELS['unclassified']}"
    return line


@dataclass
class Anatomy:
    """One session, taken apart"""

    session_id: str
    found: bool = False
    turns: list[Turn] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)
    #: Every cold boundary in the session
    boundaries: list[usage_mod.Boundary] = field(default_factory=list)
    #: The thresholds this reading was taken at, carried so every rendering of it can state them
    write_floor: int = DEFAULT_INSPECT_REPORT_WRITE_FLOOR
    join_floor: int = DEFAULT_INSPECT_REPORT_JOIN_FLOOR
    #: Task id -> what it was, from the subagent `.meta.json` Claude Code writes beside the transcript
    subagent_labels: dict[str, str] = field(default_factory=dict)
    #: Subagent transcripts no `Agent`/`TaskOutput` span of this session names
    unattached_subagents: list[SubagentAnatomy] = field(default_factory=list)
    #: `wo_turns.seq` values the caller passed that no transcript turn bound to
    unmatched_os_turns: list[int] = field(default_factory=list)
    #: Every hold on the WHOLE work order (`holds.held`), including any falling outside the turns below
    holds: list[Hold] = field(default_factory=list)

    @property
    def spans(self) -> list[ToolSpan]:
        return [s for turn in self.turns for s in turn.spans]

    @property
    def wall(self) -> float:
        return sum(t.wall for t in self.turns)

    @property
    def held(self) -> float:
        return sum(t.held for t in self.turns)

    @property
    def active(self) -> float:
        return sum(t.active for t in self.turns)

    def held_by(self) -> dict[str, float]:
        """Seconds held per cause across the session, biggest first"""
        totals: dict[str, float] = {}
        for turn in self.turns:
            for cause, seconds in turn.held_by().items():
                totals[cause] = totals.get(cause, 0.0) + seconds
        return dict(sorted(totals.items(), key=lambda kv: -kv[1]))

    @property
    def unexplained(self) -> float:
        """Idle the record does NOT account for — the residual, and the honest one"""
        return max(0.0, sum(t.idle for t in self.turns) - self.held)

    def joins(self, over: float | None = None) -> list[ToolSpan]:
        """Blocking joins at or over `over` seconds — the report's floor by default"""
        floor = self.join_floor if over is None else over
        return sorted((s for s in self.spans if s.is_join and s.seconds >= floor),
                      key=lambda s: s.seconds, reverse=True)

    def tool_profile(self) -> list[dict[str, Any]]:
        """Count, total and mean seconds per tool name, dearest total first"""
        by_name: dict[str, dict[str, Any]] = {}
        for span in self.spans:
            row = by_name.setdefault(span.name, {"name": span.name, "calls": 0,
                                                 "seconds": 0.0, "unfinished": 0})
            row["calls"] += 1
            row["seconds"] += span.seconds
            if not span.finished:
                row["unfinished"] += 1
        for row in by_name.values():
            timed = row["calls"] - row["unfinished"]
            row["mean"] = row["seconds"] / timed if timed else 0.0
        return sorted(by_name.values(), key=lambda r: r["seconds"], reverse=True)

    def nav_profile(self) -> dict[str, int]:
        """How this session navigated code: symbol calls, source-navigation Bash calls (#227)"""
        profile = {"symbol_calls": 0, "source_nav_calls": 0, "unclassified": 0}
        for span in self.spans:
            if navigation.is_symbol_call(span.name):
                profile["symbol_calls"] += 1
            elif span.navigates_source is True:
                profile["source_nav_calls"] += 1
            elif span.name == "Bash" and span.navigates_source is None:
                profile["unclassified"] += 1
        return profile

    def partition(self) -> dict[str, float]:
        """The whole session's clock, summed over its turns"""
        return {"wall": self.wall, "active": self.active, "held": self.held,
                "unexplained": self.unexplained,
                **{k: sum(getattr(t, k) for t in self.turns) for k in PARTS}}

    def cache_ttl(self) -> dict[str, int]:
        """Which TTL this session's cache writes were bought at — §1 step 5, §3.2"""
        split = {"cache_1h": 0, "cache_5m": 0, "unknown": 0}
        for turn in self.turns:
            for call in turn.calls:
                split["cache_1h"] += call.cache_1h
                split["cache_5m"] += call.cache_5m
                split["unknown"] += max(0, call.cache_write - call.cache_1h
                                        - call.cache_5m)
        return split

    def rewrite_excess(self) -> int:
        """Tokens this session paid to send twice — `usage`'s definition, not a second one"""
        written = sum(c.cache_write for t in self.turns for c in t.calls)
        peak = max((t.context_peak for t in self.turns), default=0)
        return max(0, written - peak)

    def rewrite(self) -> dict[str, int | None]:
        """The session's re-write total, keyed to `jarvis cost`'s own `rewrite` block (#867)"""
        # ONE spelling of the dict, shared with `SubagentAnatomy.rewrite` (§1.1)
        return _rewrite_block(
            self.boundaries,
            written=sum(c.cache_write for t in self.turns for c in t.calls),
            excess=self.rewrite_excess())

    def as_dict(self) -> dict[str, Any]:
        part = self.partition()
        nav = self.nav_profile()
        wall = part["wall"] or 1.0
        return {
            "session_id": self.session_id,
            "found": self.found,
            "write_floor": self.write_floor,
            "join_floor": self.join_floor,
            # Stated beside the floors for the same reason they are: a truncation the reader cannot size is not...
            "param_caps": PARAM_CAPS.as_dict(),
            "partition": {k: round(v, 2) for k, v in part.items()},
            "share": {k: round(part[k] / wall, 4) for k in PARTS},
            "held_by": {k: round(v, 2) for k, v in self.held_by().items()},
            "holds": [h.as_dict() for h in self.holds],
            # The legend, carried WITH the reading rather than looked up beside it: a `--json` consumer and the...
            "hold_causes": HOLD_CAUSES,
            "context_peak": max((t.context_peak for t in self.turns), default=0),
            "rewrite_excess": self.rewrite_excess(),
            # ADDITIVE, and the session-level figure #846 needs: `rewrite_excess` above stays the same number
            "rewrite": self.rewrite(),
            "cache_ttl": self.cache_ttl(),
            "turns": [t.as_dict() for t in self.turns],
            "writes": [w.as_dict() for w in self.writes],
            "joins": [s.as_dict() for s in self.joins()],
            "tools": self.tool_profile(),
            # ADDITIVE and a SIBLING of `tools` above: §3's three-key contract
            "nav": nav,
            "nav_line": nav_line(nav),
            # ADDITIVE, and `subagent_depth_read` is stated for the reason the floors and the caps are: silence...
            "unattached_subagents": [s.as_dict()
                                     for s in self.unattached_subagents],
            # ADDITIVE, empty unless the caller bound the numbering (spec 2026-09-27 §3)
            "unmatched_os_turns": list(self.unmatched_os_turns),
            "subagent_depth_read": SUBAGENT_DEPTH_READ,
        }


# -- reading a transcript


def _navigates(block: dict[str, Any]) -> bool | None:
    """Does this `tool_use` block read or search source? (#227)"""
    if str(block.get("name") or "") != "Bash":
        return None
    command = str((block.get("input") or {}).get("command") or "")
    if not command:
        return None
    return navigation.navigates_source(command, navigation.SOURCE_SUFFIXES)


def _detail_of(payload: Any, limit: int) -> str:
    """A one-line answer to "doing what" for a tool call"""
    if not isinstance(payload, dict):
        return ""
    # `substring_pattern` is Serena `search_for_pattern`'s own parameter name (#845)
    for key in ("description", "command", "task_id", "file_path", "pattern",
                "substring_pattern", "skill"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _first_line(redact_param(value), limit)
    return ""


def _trigger_kind(text: str) -> str:
    for needle, kind in TRIGGERS:
        if needle in text:
            return kind
    return MESSAGE_TRIGGER


def _prompt_text(row: dict[str, Any]) -> str:
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in usage_mod.blocks_of(row, "text"))


def _prompt_of(row: dict[str, Any], ts: float, limit: int) -> Prompt | None:
    """The prompt this row is, or None if it is not one"""
    if row.get("type") != "user":
        return None
    source = "sdk" if row.get("promptSource") == "sdk" else "user"
    if source == "user" and (row.get("isMeta") or usage_mod.blocks_of(row,
                                                                     "tool_result")):
        return None
    text = _prompt_text(row)
    if not text.strip():
        return None
    # REDACTED BEFORE IT IS CUT, as `_detail_of` is: the quote is sealed
    return Prompt(ts=ts, kind=_trigger_kind(text),
                  quote=_first_line(redact_param(text), limit), source=source)


def _subagent_labels(path: Path) -> dict[str, str]:
    """Task id -> label, from the meta files beside a transcript"""
    labels: dict[str, str] = {}
    directory = path.with_suffix("") / "subagents"
    if not directory.is_dir():
        return labels
    for meta_path in sorted(directory.glob("agent-*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text()) or {}
        except (OSError, ValueError):
            continue
        task_id = meta_path.name[len("agent-"):-len(".meta.json")]
        parts = [str(meta.get("agentType") or ""), str(meta.get("description") or "")]
        labels[task_id] = " · ".join(p for p in parts if p)
    return labels


def read_transcript(path: Path | str,
                    cfg: InspectConfig | None = None,
                    ) -> tuple[list[Turn], dict[str, str]]:
    """One transcript file, cut into turns with their tool spans attached"""
    cfg = cfg or InspectConfig()
    turns: list[Turn] = []
    pending: dict[str, ToolSpan] = {}
    open_turn: Turn | None = None
    saw_assistant = False
    # The per-turn parameter budget is spent HERE
    param_spent = 0

    for row in usage_mod.rows(path):
        ts = usage_mod.parse_stamp(row.get("timestamp"))
        # A TURN ENDS WHEN THE MODEL STOPS, NOT WHEN THE NEXT TURN STARTS
        if ts and open_turn is not None and row.get("type") in ("assistant", "user"):
            open_turn.ended = max(open_turn.ended, ts)
        prompt = _prompt_of(row, ts, cfg.quote_chars)
        if prompt is not None:
            # A new turn only if the model has spoken since the last one started: `Daemon.deliver_messages`...
            if open_turn is None or saw_assistant:
                open_turn = Turn(seq=len(turns) + 1, started=ts, ended=ts)
                turns.append(open_turn)
                saw_assistant = False
                param_spent = 0
            open_turn.triggers.append(prompt)
            # Spec §1: the input is in, the answer is not
            open_turn.awaiting_since = ts
            continue
        if row.get("type") == "assistant":
            saw_assistant = True
            if open_turn is not None:
                open_turn.awaiting_since = 0.0
        for block in usage_mod.blocks_of(row, "tool_use"):
            tool_id = str(block.get("id") or "")
            if not tool_id:
                continue
            params, truncated, dropped, param_spent = _params_of(
                block.get("input"), param_spent)
            span = ToolSpan(name=str(block.get("name") or ""), tool_id=tool_id,
                            started=ts,
                            detail=_detail_of(block.get("input"),
                                              cfg.quote_chars),
                            params=params, params_truncated=truncated,
                            params_dropped=dropped,
                            # The RAW block, the same read `background._scan_calls` does and for the same reason (spec §1)
                            backgrounded=bool((block.get("input") or {}).get(
                                "run_in_background")),
                            # §3: the RAW command, never `detail` (prose) or `params` (capped)
                            navigates_source=_navigates(block))
            pending[tool_id] = span
            # Charged to the turn that ASKED for it
            if open_turn is not None:
                open_turn.spans.append(span)
        for block in usage_mod.blocks_of(row, "tool_result"):
            span = pending.pop(str(block.get("tool_use_id") or ""), None)
            if span is not None:
                span.ended = ts
            # Spec §1: a result is an input too — the model is answering again
            if open_turn is not None:
                open_turn.awaiting_since = ts

    return turns, _subagent_labels(Path(path))


def classify_writes(calls: Sequence[usage_mod.Call], floor: int,
                    compactions: Sequence[float] = ()) -> list[Write]:
    """Every cache write at or over `floor`, labelled with what caused it"""
    writes: list[Write] = []
    previous: usage_mod.Call | None = None
    for call in calls:
        if call.cache_write >= floor:
            ttl = TTL_1H if previous is not None and previous.cache_1h else TTL_5M
            gap = call.ts - previous.ts if previous is not None else 0.0
            if previous is None:
                cause = COLD_START
            elif any(previous.ts < c <= call.ts for c in compactions):
                cause = COMPACTION
            elif gap > ttl:
                cause = TTL_EXPIRY
            else:
                cause = PREFIX_MISS
            writes.append(Write(ts=call.ts, written=call.cache_write,
                                read=call.cache_read, gap=gap, cause=cause,
                                model=call.model, ttl=ttl))
        previous = call
    return writes


#: How far AFTER a transcript turn's first row an OS turn may have started and still be its turn
TURN_BIND_TOLERANCE_SECONDS = 5.0


def turn_name(seq: int) -> str:
    """"turn 7", or "an unrecorded turn" for the 0 of a turn the OS never minted"""
    return f"turn {seq}" if seq else "an unrecorded turn"


def _bind_turns(turns: Sequence[Turn],
                turn_starts: Sequence[tuple[int, float]]) -> list[int]:
    """Number the transcript's turns with the OS's own `wo_turns.seq` — spec §3"""
    if not turn_starts:
        for seq, turn in enumerate(turns, start=1):
            turn.seq, turn.part = seq, 1
        return []
    ordered = sorted(turn_starts, key=lambda pair: pair[1])
    matched: set[int] = set()
    parts: dict[int, int] = {}
    for turn in turns:
        cutoff = turn.started + TURN_BIND_TOLERANCE_SECONDS
        bound = [seq for seq, started in ordered if started <= cutoff]
        turn.seq = bound[-1] if bound else 0
        parts[turn.seq] = parts.get(turn.seq, 0) + 1
        turn.part = parts[turn.seq]
        matched.add(turn.seq)
    return [seq for seq, _ in ordered if seq not in matched]


def read_session(session_id: str, cfg: InspectConfig | None = None, *,
                 root: Path | None = None,
                 index: dict[str, list[Path]] | None = None,
                 spans: Sequence[Hold] = (),
                 turn_starts: Sequence[tuple[int, float]] = (),
                 cold_prefix_floor: int | None = None) -> Anatomy:
    """Take one session apart, across every segment file it left behind"""
    cfg = cfg or InspectConfig()
    anatomy = Anatomy(session_id=session_id, write_floor=cfg.report_write_floor,
                      join_floor=cfg.report_join_floor)
    if index is None:
        index = usage_mod.index_sessions(root)
    paths = index.get(session_id)
    if not paths:
        return anatomy
    anatomy.found = True
    turns: list[Turn] = []
    for path in sorted(paths):
        found, labels = read_transcript(path, cfg)
        turns.extend(found)
        anatomy.subagent_labels.update(labels)
    turns.sort(key=lambda t: t.started)
    anatomy.unmatched_os_turns = _bind_turns(turns, turn_starts)
    anatomy.turns = turns

    calls = usage_mod.session_calls(session_id, index=index)
    compactions = [c for path in sorted(paths)
                   for c in usage_mod.compaction_stamps(path)]
    anatomy.writes = classify_writes(calls, cfg.report_write_floor,
                                     sorted(compactions))
    # ONCE over the whole session, then attributed — Neo q1048
    anatomy.boundaries = usage_mod.classify_boundaries(
        calls, compactions=sorted(compactions), cold_prefix_floor=cold_prefix_floor)
    _attach_boundaries(turns, anatomy.boundaries)
    _attach_calls(turns, calls)
    _close_turns(turns)
    _name_joins(turns, anatomy.subagent_labels)
    # AFTER `_name_joins`, which rewrites a join's `detail` from the bare task id to "label (id)":...
    anatomy.unattached_subagents = _attach_subagents(
        turns,
        [_read_subagent(sub, cfg, anatomy.subagent_labels, cold_prefix_floor)
         for path in sorted(paths) for sub in _subagent_transcripts(path)])
    # AFTER `_close_turns`, which is the only thing that knows where a turn ends: a hold attached before...
    anatomy.holds = list(spans)
    _attach_holds(turns, anatomy.holds)
    return anatomy


def _attach_holds(turns: Sequence[Turn], spans: Sequence[Hold]) -> None:
    """Give every turn the holds that overlap it"""
    for turn in turns:
        turn.holds = [h for h in spans if h.overlap(turn.started, turn.ended) > 0]


def _close_turns(turns: Sequence[Turn]) -> None:
    """Give every turn its two ends: when it stopped working, and when it stopped (docs/findings/anatomy-of-an-expensive-turn.md)"""
    for index, turn in enumerate(turns):
        seen = [call.ts for call in turn.calls]
        seen += [span.ended for span in turn.spans if span.finished]
        if seen:
            turn.active_ended = max(turn.started, max(seen))
        following = turns[index + 1] if index + 1 < len(turns) else None
        # The LAST turn has no successor to run on to, so it ends where it stopped working
        turn.ended = following.started if following else (turn.active_ended
                                                          or turn.ended)


def _turn_at(ordered: Sequence[Turn], ts: float) -> Turn | None:
    """Which turn was running when something landed, or None if nothing had started"""
    home: Turn | None = None
    for turn in ordered:
        if turn.started <= ts:
            home = turn
        else:
            break
    return home


def _attach_calls(turns: Sequence[Turn], calls: Iterable[usage_mod.Call]) -> None:
    """Put each API call in the turn that was running when it landed"""
    ordered = sorted(turns, key=lambda t: t.started)
    for call in calls:
        home = _turn_at(ordered, call.ts)
        if home is not None:
            home.calls.append(call)


def _attach_boundaries(turns: Sequence[Turn],
                       boundaries: Iterable[usage_mod.Boundary]) -> None:
    """Put each classified boundary in the turn that was running when it was paid for"""
    ordered = sorted(turns, key=lambda t: t.started)
    for boundary in boundaries:
        home = _turn_at(ordered, boundary.ts)
        if home is not None:
            home.boundaries.append(boundary)


def _read_subagent(path: Path, cfg: InspectConfig,
                   labels: dict[str, str],
                   cold_prefix_floor: int | None = None) -> SubagentAnatomy:
    """One subagent transcript, taken apart the way `read_session` takes the parent apart"""
    stem = path.stem
    task_id = stem[len("agent-"):] if stem.startswith("agent-") else stem
    turns, _ = read_transcript(path, cfg)
    calls = usage_mod.calls_of(path)
    _attach_calls(turns, calls)
    _close_turns(turns)
    compactions = sorted(usage_mod.compaction_stamps(path))
    return SubagentAnatomy(
        task_id=task_id, label=labels.get(task_id, ""), turns=turns,
        writes=classify_writes(calls, cfg.report_write_floor, compactions),
        deeper=len(_subagent_transcripts(path)),
        # THRESHOLD-FREE, from the calls already in hand (spec 2026-10-02 §1.2)
        total_written=sum(c.cache_write for c in calls),
        max_write=max((c.cache_write for c in calls), default=0),
        write_floor=cfg.report_write_floor,
        api_call_count=len(calls),
        # REUSED, never a second classifier: the OS has one judgement about a cache read that went backwards
        boundaries=usage_mod.classify_boundaries(
            calls, compactions=compactions, cold_prefix_floor=cold_prefix_floor))


def hung_subagent_call(subs: Sequence[SubagentAnatomy], now: float, older_than: float,
                       *, since: float = 0.0) -> tuple[SubagentAnatomy, ToolSpan] | None:
    """The first (subagent, span) whose LAST span is an unfinished tool call older than `older_than`..."""
    for sub in subs:
        span = next((t.spans[-1] for t in reversed(sub.turns) if t.spans), None)
        # A zero start is a missing stamp, not 1970 (issue 1309)
        if span is None or span.finished or not span.started or span.started < since:
            continue
        if now - span.started >= older_than:
            return sub, span
    return None


def _names(span: ToolSpan, task_id: str) -> bool:
    """Whether this span names that subagent — `detail` or any reported parameter"""
    return span.name in SPAWN_TOOLS and (
        task_id in span.detail or any(task_id in v for v in span.params.values()))


def _attach_subagents(turns: Sequence[Turn],
                      subs: Sequence[SubagentAnatomy]) -> list[SubagentAnatomy]:
    """Hang each subagent off the turn whose span NAMES it; return the rest"""
    unattached: list[SubagentAnatomy] = []
    for sub in subs:
        home = next((t for t in turns
                     if any(_names(s, sub.task_id) for s in t.spans)), None)
        if home is None:
            unattached.append(sub)
        else:
            home.subagents.append(sub)
    return unattached


def _name_joins(turns: Sequence[Turn], labels: dict[str, str]) -> None:
    """Replace a join's raw task id with what was actually being waited on"""
    for turn in turns:
        for span in turn.spans:
            if span.is_join and span.detail in labels:
                span.detail = f"{labels[span.detail]} ({span.detail})"


# -- the live half: raising it while it is still burning


TURN_ALARM, JOIN_ALARM, WRITE_ALARM = "long-turn", "long-join", "big-rewrite"
#: The one alarm here whose finding is that NOTHING was spent — see `ALARM_KINDS`
STALL_ALARM = "stalled-turn"

#: A request in flight past `alarm_awaiting_minutes` with no completed content block
SLOW_RESPONSE_ALARM = "slow-model-response"

#: Kinds that are a NOTE, not an interruption: recorded, rendered, never escalated
INFORMATIONAL_KINDS = frozenset({SLOW_RESPONSE_ALARM})

#: THE AGGREGATE PAIR, and the only kinds in this module that are not about one turn: `alarms()` never...
REWRITE_PREFIX_ALARM = "rewrite-tax-prefix"
REWRITE_TTL_ALARM = "rewrite-tax-ttl"

#: THE OTHER AGGREGATE PAIR, and the only alarms in the OS about spend it did not make
CACHE_1H_DISPATCHED_ALARM = "cache-1h-dispatched"
CACHE_1H_FOREIGN_ALARM = "cache-1h-foreign"

#: THE STAMP AGAINST THE RATE: an arm that never reached the CLI (docs/superpowers/specs/2026-10-22-the-arm-that-did-not-apply.md)
CACHE_ARM_MISSED_ALARM = "cache-arm-missed"

#: What each kind IS, for a surface listing alarms rather than raising one
ALARM_KINDS = {
    TURN_ALARM: "a turn still running, still being billed",
    # THE ODD ONE OUT, deliberately: the other three say money is going out and this one says none is
    STALL_ALARM: "a turn that was open with no API call ever made — nothing was being "
                 "billed (historical: nothing raises this now, spec of 2026-09-27)",
    SLOW_RESPONSE_ALARM: "a request has been in flight this long with no completed "
                         "content block — the model is answering slowly",
    JOIN_ALARM: "a wait that is costing more than it buys — a `TaskOutput` open past "
                "the cache TTL, or a delegation whose subagent has a tool call hung",
    WRITE_ALARM: "the conversation sent again, at the cache-write rate",
    # The two aggregate kinds
    REWRITE_PREFIX_ALARM: "a project's conversations re-sent because the prompt PREFIX "
                          "moved — the half no cache TTL can buy back",
    REWRITE_TTL_ALARM: "a project's conversations re-sent because the cache entry "
                       "EXPIRED — the half a longer TTL could buy back",
    # The 1h pair
    CACHE_1H_DISPATCHED_ALARM: "Jarvis's own turns bought the one-hour cache write — a "
                               "breach of the transport's 5-minute guarantee",
    CACHE_1H_FOREIGN_ALARM: "sessions Jarvis never dispatched bought the one-hour cache "
                            "write — reportable, not fixable from inside the OS",
    CACHE_ARM_MISSED_ALARM: "a turn stamped with a cache-TTL arm paid the OTHER TTL — "
                            "the arm never reached the CLI, so the stamp records only "
                            "the intent",
    # The compaction post-conditions, raised by `invariants.check_compactions_hold`
    "compaction-chained": "a compaction run on a conversation the previous turn had "
                          "already compacted — it summarised the summary",
    "compaction-unproven": "a compaction recorded as done with no boundary and no "
                           "achieved size — nothing shows it shrank the conversation",
}


@dataclass
class Alarm:
    """A turn that is costing money NOW, in a sentence the attention list can carry"""

    kind: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason}


def _spend_so_far(turn: Turn, now: float | None) -> str:
    """What the turn has actually bought, so "still being billed" can be checked"""
    if not turn.calls:
        return "NO API CALL — nothing has been billed"
    made = f"{len(turn.calls)} API call" + ("s" if len(turn.calls) > 1 else "")
    ago = (now - max(call.ts for call in turn.calls)) if now else 0.0
    when = "seconds ago" if ago < 60 else f"{int(ago // 60)}m ago"
    spend = turn.usage
    return (f"{made}, the last one {when}, {spend.total_tokens:,} tokens for "
            f"${spend.list_cost_usd:.2f} so far")


def awaiting_note(turn: Turn) -> str:
    """"awaiting the model since 21:32 (request in flight, no block completed)\""""
    since = datetime.fromtimestamp(turn.awaiting_since).strftime("%H:%M")
    return f"awaiting the model since {since} (request in flight, no block completed)"


def held_note(wall: float, holding: dict[str, float]) -> str:
    """" (6.8h on the wall clock, 4.0h of it held by a fleet usage limit)", or "\""""
    if not holding:
        return ""
    named = ", ".join(f"{_hours(seconds)} of it held by {HOLD_CAUSES.get(cause, cause)}"
                      for cause, seconds in holding.items())
    return f" ({_hours(wall)} on the wall clock, {named})"


def _hours(seconds: float) -> str:
    """Durations as a reader thinks of them"""
    return f"{seconds / 3600:.1f}h" if seconds >= 3600 else f"{int(seconds // 60)}m"


def alarms(anatomy: Anatomy, cfg: InspectConfig, wo_id: str = "",
           now: float | None = None, *, dispatched: float) -> list[Alarm]:
    """What is wrong with the turn that is running, most actionable first"""
    if not anatomy.found or not anatomy.turns or not cfg.enabled:
        return []
    turn = anatomy.turns[-1]
    if turn.started < dispatched:
        return []
    wall = max(turn.wall, (now - turn.started) if now else 0.0)
    # `anatomy.holds` and NOT `turn.holds`: the per-turn slices were attached against a turn that ends...
    window = (turn.started, turn.started + wall)
    held = min(wall, sum(h.overlap(*window, now) for h in anatomy.holds))
    active = max(0.0, wall - held)
    holding = by_cause(anatomy.holds, *window, now)
    raised: list[Alarm] = []
    hint = f" — `jarvis inspect {wo_id}`" if wo_id else ""

    # `stalled-turn` IS NEVER RAISED LIVE (spec §2 branch 2): its evidence was the absence of an...
    if turn.observed and active >= cfg.alarm_turn_minutes * 60:
        raised.append(Alarm(TURN_ALARM, (
            f"this turn has been running {int(active // 60)} minutes"
            f"{held_note(wall, holding)} and is still being billed "
            f"({_spend_so_far(turn, now)}){hint}")))
    # INDEPENDENT OF THE ONE ABOVE, not exclusive with it: `awaiting_since` is set on every `tool_result`...
    if turn.awaiting:
        wait = (now - turn.awaiting_since) if now else 0.0
        awaited = max(0.0, wait - min(wait, sum(
            h.overlap(turn.awaiting_since, turn.awaiting_since + wait, now)
            for h in anatomy.holds)))
        if awaited >= cfg.alarm_awaiting_minutes * 60:
            raised.append(Alarm(SLOW_RESPONSE_ALARM, (
                f"{awaiting_note(turn)} — {int(awaited // 60)} minutes"
                f"{held_note(wall, holding)} with nothing completed yet; the model is "
                f"answering slowly, and this is not a stall{hint}")))
    for span in turn.spans:
        if not span.is_join or span.finished or not now or not span.started:
            continue
        # A DELEGATION is judged on HANG EVIDENCE and never on elapsed wait (spec §2b): at...
        if span.name in DELEGATION_TOOLS:
            found = hung_subagent_call(
                [*turn.subagents, *anatomy.unattached_subagents], now,
                cfg.alarm_subagent_tool_minutes * 60, since=span.started)
            if found is None:
                continue
            sub, hung = found
            called = f"{hung.name} ({hung.detail})" if hung.detail else hung.name
            raised.append(Alarm(JOIN_ALARM, (
                f"subagent {sub.label or sub.task_id} blocked "
                f"{_hours(now - hung.started)} in {called} — long enough to lose the "
                f"prompt cache, so the wait will be paid for twice{hint}")))
            break
        if now - span.started >= cfg.alarm_join_seconds:
            waited = int((now - span.started) // 60)
            raised.append(Alarm(JOIN_ALARM, (
                f"blocked {waited}m waiting on {span.detail or span.tool_id} with no "
                f"API call in flight — long enough to lose the prompt cache, so the "
                f"wait will be paid for twice{hint}")))
            break
    for write in anatomy.writes:
        # A compaction is exempt for `COLD_START`'s reason and one more: the alarm's advice is "the...
        if write.ts >= turn.started and write.written >= cfg.alarm_write_tokens \
                and write.cause not in (COLD_START, COMPACTION):
            raised.append(Alarm(WRITE_ALARM, (
                f"re-sent {write.written:,} cached tokens in one call ({write.cause}) "
                f"— the conversation is being paid for again{hint}")))
            break
    return raised


def live_alarms(session_id: str, cfg: InspectConfig, *, wo_id: str = "",
                now: float | None = None, dispatched: float,
                root: Path | None = None,
                index: dict[str, list[Path]] | None = None,
                spans: Sequence[Hold] = (),
                turn_starts: Sequence[tuple[int, float]] = ()) -> list[Alarm]:
    """`alarms` for a session id — one transcript read, no paid call, nothing written"""
    reading = replace(cfg, report_write_floor=cfg.alarm_write_tokens)
    anatomy = read_session(session_id, reading, root=root, index=index, spans=spans,
                           turn_starts=turn_starts)
    return alarms(anatomy, cfg, wo_id=wo_id, now=now, dispatched=dispatched)


# -- the fleet's cache-TTL hygiene: who is still buying the one-hour write

#: The `cache_creation` key that says a write bought the hour
ONE_HOUR_KEY = '"ephemeral_1h_input_tokens"'


@dataclass
class HourWrites:
    """One session's ONE-HOUR cache writes, split by who sent the prompt"""

    session_id: str
    #: The transcript directory: the slugified cwd the session was created in
    directory: str = ""
    dispatched: int = 0
    foreign: int = 0
    #: Jarvis's own transport buying the hour ON PURPOSE: a turn stamped with the `1h` or `cli` arm... (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)
    deliberate: int = 0
    #: First and last 1h WRITE, not the session's lifetime
    first_ts: float = 0.0
    last_ts: float = 0.0

    @property
    def total(self) -> int:
        return self.dispatched + self.foreign + self.deliberate

    def as_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "directory": self.directory,
                "dispatched": self.dispatched, "foreign": self.foreign,
                "deliberate": self.deliberate,
                "total": self.total, "first_ts": self.first_ts,
                "last_ts": self.last_ts}


def _mentions(path: Path, needle: str) -> bool:
    """Whether a file contains `needle` at all, without parsing a line of it"""
    try:
        with path.open(errors="replace") as handle:
            return any(needle in line for line in handle)
    except OSError:
        return False


def _subagent_transcripts(path: Path) -> list[Path]:
    """The subagent transcripts written beside one session file — `read_session`'s rule"""
    directory = path.with_suffix("") / "subagents"
    return sorted(directory.glob("*.jsonl")) if directory.is_dir() else []


#: The arms that BOUGHT the hour on purpose — `catalog.VALID_WORKER_CACHE_TTL` minus the control
DELIBERATE_ARMS = ("1h", "cli")


def _arm_of(arms: Mapping[str, Sequence[tuple[float, float, str]]] | None,
            session_id: str, ts: float) -> str:
    """Which cache-TTL arm was running in `session_id` at `ts`, or "" — spec §5"""
    for started_at, ended_at, arm in (arms or {}).get(session_id, ()):
        if started_at <= ts <= ended_at:
            return arm
    return ""


def one_hour_writes(since: float, *, root: Path | None = None,
                    cfg: InspectConfig | None = None,
                    arms: Mapping[str, Sequence[tuple[float, float, str]]] | None = None,
                    ) -> list[HourWrites]:
    """Every session that bought the one-hour cache write since `since`, biggest first (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)"""
    root = root or usage_mod.transcript_root()
    if not root.is_dir():
        return []
    cfg = cfg or InspectConfig()
    found: dict[str, HourWrites] = {}
    for path in sorted(root.glob("*/*.jsonl")):
        subagents = _subagent_transcripts(path)
        if not any(_mentions(p, ONE_HOUR_KEY) for p in (path, *subagents)):
            continue
        turns, _ = read_transcript(path, cfg)
        calls = usage_mod.calls_of(path)
        for sub in subagents:
            calls.extend(usage_mod.calls_of(sub))
        calls.sort(key=lambda c: c.ts)
        _attach_calls(turns, calls)
        for turn in turns:
            dispatched = bool(turn.triggers) and all(
                p.source == "sdk" for p in turn.triggers)
            for call in turn.calls:
                if not call.cache_1h or call.ts < since:
                    continue
                entry = found.get(path.stem)
                if entry is None:
                    entry = found[path.stem] = HourWrites(session_id=path.stem,
                                                          directory=path.parent.name)
                if not dispatched:
                    entry.foreign += call.cache_1h
                elif _arm_of(arms, path.stem, call.ts) in DELIBERATE_ARMS:
                    entry.deliberate += call.cache_1h
                else:
                    entry.dispatched += call.cache_1h
                entry.first_ts = min(entry.first_ts, call.ts) or call.ts
                entry.last_ts = max(entry.last_ts, call.ts)
    return sorted(found.values(), key=lambda e: (-e.total, e.session_id))


#: The one line that fixes the foreign half
REMEDY_LINE = '"env": {"FORCE_PROMPT_CACHING_5M": "1"}  in ~/.claude/settings.json'

#: Said on the foreign alarm and on nothing else
NOT_OURS_TO_FIX = (
    "JARVIS CANNOT FIX THIS AND MUST NOT TRY: the file is the user's own Claude "
    "configuration, outside every project the OS manages. The deliverable is a work "
    "order that tells a PERSON to add the line above, and a check that it took."
)

#: How many offending sessions the reason names before it stops
NAMED_SESSIONS = 3


def _hour_premium_usd(tokens: int) -> float:
    """What buying the hour COST above the five-minute write, at Opus list prices"""
    premium = usage_mod.CACHE_WRITE_1H_RATE - usage_mod.CACHE_WRITE_RATE
    return premium * tokens * usage_mod.DEFAULT_PRICE[0] / 1e6


def _name_sessions(found: Sequence[HourWrites], attr: str) -> str:
    """The biggest offenders, named"""
    named = [e for e in found if getattr(e, attr)][:NAMED_SESSIONS]
    return "; ".join(
        f"{e.session_id} in {e.directory} ({getattr(e, attr):,} tokens, last written "
        f"{datetime.fromtimestamp(e.last_ts, tz=timezone.utc):%Y-%m-%d})"
        for e in named)


def hour_alarms(found: Sequence[HourWrites], cfg: InspectConfig, *,
                days: int) -> list[Alarm]:
    """What is standing-wrong with the fleet's cache TTL, one alarm per CAUSE"""
    dispatched = sum(e.dispatched for e in found)
    foreign = sum(e.foreign for e in found)
    deliberate = sum(e.deliberate for e in found)
    window = f"in the last {days} days"
    raised: list[Alarm] = []
    if dispatched >= cfg.alarm_cache_1h_dispatched_tokens:
        # Scoped to the 5m arm, and the deliberate total named beside it: an arm that bought the hour on...
        raised.append(Alarm(CACHE_1H_DISPATCHED_ALARM, (
            f"{dispatched:,} tokens were written at the ONE-HOUR cache TTL {window} by "
            f"turns JARVIS ITSELF dispatched whose `wo_turns.cache_ttl` arm was 5m — "
            f"about ${_hour_premium_usd(dispatched):,.2f} at Opus list above what the "
            f"five-minute write would have cost. Those turns were launched to buy the "
            f"FIVE-MINUTE write, so this is a BREACH of what they asked for and a defect "
            f"in the OS — not in anyone's personal configuration. Start at "
            f"`claude_cli.spawn_turn`'s `cache` argument, `dispatch.cache_ttl_env` and "
            f"`dispatch._write_worker_settings`. A further {deliberate:,} tokens bought "
            f"the hour ON PURPOSE on a 1h or cli arm {window}, which is not part of this "
            f"figure and is not a fault. Sessions: "
            f"{_name_sessions(found, 'dispatched')}.")))
    if foreign >= cfg.alarm_cache_1h_tokens:
        raised.append(Alarm(CACHE_1H_FOREIGN_ALARM, (
            f"{foreign:,} tokens were written at the ONE-HOUR cache TTL {window} by "
            f"sessions Jarvis never dispatched — about "
            f"${_hour_premium_usd(foreign):,.2f} at Opus list above what the five-minute "
            f"write would have cost. These are `claude` processes a person started, so "
            f"the OS's own transport is not implicated. The fix is one line: "
            f"{REMEDY_LINE}. {NOT_OURS_TO_FIX} Sessions: "
            f"{_name_sessions(found, 'foreign')}.")))
    return raised


# -- the stamp against the RATE (docs/superpowers/specs/2026-10-22-the-arm-that-did-not-apply.md)

#: The arms that CLAIM a rate — `cli` claims none, so it cannot be missed
CHECKED_ARMS = ("1h", "5m")

#: How many offending turns the reason names before it stops
NAMED_TURNS = 3


@dataclass
class ArmMiss:
    """One STAMPED turn whose calls bought the TTL it was not armed for"""

    project: str
    wo_id: str
    seq: int
    arm: str
    #: Tokens written at the OTHER TTL — what the arm claims was not paid for
    tokens: int
    ts: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"project": self.project, "wo_id": self.wo_id, "seq": self.seq,
                "arm": self.arm, "tokens": self.tokens, "ts": self.ts}


def arm_misses(rows: Iterable[tuple[str, str, int, str, float, int, int]],
               ) -> list[ArmMiss]:
    """Every stamped turn that paid the OTHER TTL, biggest first"""
    misses: list[ArmMiss] = []
    for project, wo_id, seq, arm, ts, cache_1h, cache_5m in rows:
        if arm not in CHECKED_ARMS:
            continue
        paid, other = (cache_1h, cache_5m) if arm == "1h" else (cache_5m, cache_1h)
        # Only the SPLIT is evidence (kn-dbf6f779): neither side reported says nothing
        if paid or not other:
            continue
        misses.append(ArmMiss(project=project, wo_id=wo_id, seq=seq, arm=arm,
                              tokens=other, ts=ts))
    return sorted(misses, key=lambda m: (-m.tokens, m.wo_id, m.seq))


def _name_turns(misses: Sequence[ArmMiss]) -> str:
    """The biggest offenders, named as the record spells them"""
    return "; ".join(
        f"{m.wo_id} turn {m.seq} in {m.project} (armed {m.arm}, {m.tokens:,} tokens "
        f"at the other TTL)" for m in misses[:NAMED_TURNS])


def arm_miss_alarms(misses: Sequence[ArmMiss], cfg: InspectConfig, *,
                    days: int) -> list[Alarm]:
    """The arm the OS believed it bought against the one the transcript paid"""
    tokens = sum(m.tokens for m in misses)
    if not misses or tokens < cfg.alarm_cache_arm_missed_tokens:
        return []
    hour = [m for m in misses if m.arm == "1h"]
    five = [m for m in misses if m.arm == "5m"]
    return [Alarm(CACHE_ARM_MISSED_ALARM, (
        f"{tokens:,} cache-write tokens in the last {days} days were bought at a TTL "
        f"the turn was NOT armed for, over {len(misses)} turns. "
        f"{len(hour)} were stamped `wo_turns.cache_ttl` = 1h and wrote ephemeral_1h = 0: "
        f"the arm NEVER REACHED THE CLI, so the stamp records the intent and nothing "
        f"about the rate. {len(five)} were stamped 5m and wrote ephemeral_5m = 0, which "
        f"bought the hour nobody asked for — about "
        f"${_hour_premium_usd(sum(m.tokens for m in five)):,.2f} at Opus list above the "
        f"five-minute write. EITHER WAY THIS IS A DEFECT IN THE OS, not in anyone's "
        f"personal configuration: an env variable the OS sets has to OUTRANK every "
        f"user-owned settings key for the same behaviour, so start at "
        f"`claude_cli.CACHE_TTL_KEYS`, `dispatch.cache_ttl_env` and what the CLI ranks "
        f"above them. Turns: {_name_turns(misses)}."))]
