"""Where a session's TIME went — the other half of `jarvis cost`.

`usage` reads a transcript for TOKENS and answers "what did this cost". The same file
also carries a clock: every row is timestamped, so a turn's wall clock can be split into
the three things an agent does with it. That split is what this module produces, and it
is the question `jarvis cost` cannot answer — "how is a design-only agent taking
14-minute turns?" took a hand-written script over the raw JSONL, and found two defects
that cost money on every work order in the fleet.

Design and worked example: `docs/superpowers/specs/2026-08-30-the-anatomy-of-a-turn.md`.

## The partition

The method is `docs/findings/anatomy-of-an-expensive-turn.md` §1 step 4, with two
buckets added:

    executing tools   `tool_use` timestamp to the matching `tool_result` timestamp
    blocked           the subset of those where the tool is a BLOCKING JOIN
    idle              after the turn's last API call, before the next turn's prompt
    generating        the wall clock left over, ON A TURN THAT MADE AN API CALL
    unaccounted       the same remainder on a turn that made NONE

UNACCOUNTED IS THE SECOND ADDITION TO THE METHOD, and it is there because `generating`
is not measured — it is what nothing else explains, and on a turn with no API call and
no tool there is nothing to subtract. wo-f1ce0f24 turn 3 therefore rendered as 65
minutes of pure generation beside its own `0 calls` and `peak 0`, and four layers read
that number as measurement (issue 227). A span with no API call in it was never OBSERVED
generating; naming the gap is honest where charging it to the model is not. It is
`unaccounted` rather than `stalled` because the name states the evidence and not a
diagnosis: a transcript Claude Code has pruned produces the same silence as a turn that
hung. The DIAGNOSIS is `STALL_ALARM` below, which is a judgement about how long the
silence has run.

Blocked is carved out of tool time rather than added beside it because the two are
opposite facts about the same seconds: 45 seconds of `Bash` is work being done, and 450
seconds of `TaskOutput` is the lead agent asleep with no API call in flight — and long
enough to lose its prompt cache, which is why the same 450 seconds shows up again below
as a `ttl-expiry` write.

IDLE IS THE ONE ADDITION TO THE METHOD, and it is here because the method's subject
session could not see it. A turn runs from its prompt to the NEXT turn's prompt, which is
what makes the turns sum to the session with nothing dropped — but a worker turn is one
`claude -p` process, and between it exiting and Jarvis sending the next prompt there is a
stretch where nothing is generating because nothing is running. On the subject session
that stretch is 5s and 41s and charging it to the model is invisible. Across the fleet's
441 worker turns it reaches ELEVEN DAYS, on a turn with a successor — a work order parked
in `waiting_input` until a `wo send` arrived. Reported separately, `generating + idle` is
the method's original figure, and neither number is a lie on either session.

## Three cache writes that look identical and are not

A large `cache_creation_input_tokens` is the single most expensive event in a session,
and until now nothing said WHY it happened. There are three causes and they have
completely different fixes:

    cold-start    the first call of the session. Unavoidable and not a defect.
    ttl-expiry    the previous call is older than the cache TTL. The fix is to wait less
                  — shorten the blocking join that caused the gap.
    prefix-miss   the previous call is RECENT and the prefix was re-written anyway. A
                  DEFECT: something changed the prompt prefix (kn-335170a1). The fix is
                  upstream of the clock entirely.
    compaction    the OS compacted here (`worker_session.compact`), so this write is a
                  summary replacing a history. Not a defect and not free: it is the
                  price of the ttl-expiry write that did NOT happen.

THE THRESHOLD IS LOAD-BEARING, not cosmetic. Inside one turn every call after the first
writes the delta it just added to the conversation while reading the rest — a few
thousand tokens, seconds after the previous call, which by the gap test alone would read
as a `prefix-miss`. It is not one; it is the cache working. Only a write large enough to
be a re-send of the conversation is classified at all, which is why the floor is a
setting rather than a literal (`catalog.InspectConfig`, per project) and why every report
states the value it was taken at.

## No paid call, and nothing persisted

Everything here is arithmetic over files Claude Code already wrote. Like `usage`, this
derives a fact about a work order from a file Jarvis does not own and cannot repair, so
there is nothing to store and nothing to reconcile — and a transcript that has expired
is reported as absent rather than guessed at.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import usage as usage_mod
from .catalog import (DEFAULT_INSPECT_REPORT_JOIN_FLOOR,
                      DEFAULT_INSPECT_REPORT_WRITE_FLOOR, InspectConfig)
from .holds import HOLD_CAUSES, Hold, by_cause

#: Cache TTLs in seconds. NOT CONFIGURABLE, and deliberately not: these are the two
#: durations Anthropic's prompt cache actually offers (kn-f94abf34), not a policy Jarvis
#: gets to hold an opinion about. Making them settable would let a catalog declare that
#: the cache lives for an hour when it does not, and every `ttl-expiry` label downstream
#: would then be wrong. The 5-minute one is what every Jarvis call buys since
#: `claude_cli.PROMPT_CACHE_5M_ENV` shipped (kn-5dd784f5); the hour is what older
#: transcripts were charged for, and a write's own `cache_1h` split is what says which
#: of the two a LATER call had to beat.
TTL_5M = 300.0
TTL_1H = 3600.0

#: Tools whose span is the lead agent WAITING rather than working: it has dispatched
#: something and is blocked on the result with no API call in flight. Only `TaskOutput`
#: today — `Agent` itself returns immediately when the subagent is backgrounded, and the
#: wait it defers is exactly what `TaskOutput` later collects.
JOIN_TOOLS = ("TaskOutput",)

#: Tools whose span NAMES a subagent, and the only evidence a subagent is attached on
#: (spec §4b). `Agent` spawns it and `TaskOutput` collects it; a subagent no span names
#: is reported `unattached` rather than given the turn its timestamps fall in — inventing
#: a parent the record does not name is the issue-227 mistake.
SPAWN_TOOLS = ("Agent", "TaskOutput")

#: How many directory levels of subagent this module actually reads. `_subagent_labels`
#: and `_subagent_transcripts` glob ONE, so a subagent spawned by a subagent is counted
#: (`SubagentAnatomy.deeper`) and not read. Stated in every payload, because a depth the
#: reader cannot see is a completeness claim this code cannot make (spec §4b).
SUBAGENT_DEPTH_READ = 1

COLD_START, TTL_EXPIRY, PREFIX_MISS = "cold-start", "ttl-expiry", "prefix-miss"
#: The fourth, and the only one the OS chose. A compaction leaves a summary that the
#: next call writes seconds later with the static head still served — the exact shape of
#: a prefix miss, and the reason it is labelled before the gap is looked at. Without it
#: `jarvis inspect` would report the remedy as the defect it was bought to avoid.
COMPACTION = "compaction"

#: The buckets a wall clock divides into, in the order they are rendered. Walked rather
#: than spelled out at each site, so a bucket cannot exist in one renderer and not another
#: — `cli.PART_LABELS` is keyed by these and `tests/test_inspection.py` pins the two equal,
#: because a fifth bucket missing from one renderer is a table that no longer sums to 100.
PARTS = ("generating", "blocked", "tools", "idle", "unaccounted")

WRITE_CAUSE_NOTES = {
    COLD_START: "the first call of the session — unavoidable",
    TTL_EXPIRY: "the cache had expired: nothing was called for longer than the TTL",
    PREFIX_MISS: "the prefix was re-written while it was still warm — a defect",
    COMPACTION: "the OS compacted the conversation here — this is the summary being "
                "written, and it is the price of not re-sending the whole history",
}

#: How a turn's injected prompt is recognised, most specific first. The text is the
#: prompt Jarvis itself wrote (`promptSource == "sdk"`), so these match Jarvis's own
#: wording rather than anything Claude Code generates.
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


# -- tool parameters: what may be reported, and how much of it -------------------------

#: THE SHAPES, COPIED FROM `autoreview` AND NOT IMPORTED. `autoreview.SECRET_EVIDENCE`
#: and its placeholder set are the reference for how carefully this is taken here
#: (kn-32fa2a7d: bound every field you did not write), but `inspection` is a leaf —
#: `usage`, `catalog`, `holds` and nothing else — and importing the review pipeline to
#: read a transcript would put a model-calling module under `jarvis cost`. The price of
#: the copy is that a shape added there must be added here; the price of the import is a
#: cycle. Each entry is (pattern, what the marker CALLS it): the marker names the shape
#: and never quotes the match, because a report that echoes the credential has only
#: moved it one file along (kn-deef42ea).
PARAM_SECRET_SHAPES: tuple[tuple[str, str], ...] = (
    # The whole block, not the BEGIN line: substituting the header alone would leave the
    # base64 body sitting in the payload underneath the marker.
    (r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----.*?"
     r"(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)", "a private key block"),
    (r"\bssh-(?:rsa|dss|ed25519)\s+AAAA[0-9A-Za-z+/=]+", "an ssh key line"),
    (r"\bAuthorization[\"\']?\s*:\s*[\"\']?(?:Bearer|Basic|Token)\s+\S+",
     "an Authorization header value"),
    # FROM HERE DOWN, NOT IN `autoreview`. Its `SECRET_EVIDENCE` has exactly three
    # shapes and all three are above; these were invented here and have no upstream to
    # go looking for. The reason they are not shared: `autoreview` scans a DIFF, where a
    # credential arrives as an added assignment line, and this scans a COMMAND LINE,
    # where the common case is a bare token with nothing naming it — a `ghp_…` piped
    # into `gh auth login`, a password inside a connection URL.
    (r"\bsk-ant-[A-Za-z0-9_-]{8,}", "an Anthropic API key"),
    (r"\bsk-(?:[A-Za-z0-9]+-)?[A-Za-z0-9_-]{16,}", "an sk- API key"),
    (r"\bgithub_pat_[A-Za-z0-9_]{8,}|\bgh[poshur]_[A-Za-z0-9]{8,}", "a GitHub token"),
    (r"\bAKIA[0-9A-Z]{16}\b", "an AWS access key id"),
    (r"\bxox[abpr]-[A-Za-z0-9-]{8,}", "a Slack token"),
    # `keep` survives the substitution: the scheme, the user and the flag name are what
    # make the redacted line still readable as the command that was run.
    (r"(?P<keep>\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s:/@]{3,}(?=@)",
     "a password in a URL"),
    (r"(?P<keep>\bcurl\b[^\n]*?\s-u[ =]+[^\s:@]+:)[^\s\"\']+",
     "a curl -u credential"),
    # The VALUE shape is spelled into the pattern rather than tested afterwards, and it
    # is `_looks_like_a_credential`'s test in regex: at least six characters from the
    # credential charset, carrying both a digit and a letter. That is what keeps
    # `--password changeme`, `--token ""` and `--token $GH_TOKEN` out (spec §4a).
    (r"(?P<keep>--(?:password|token|api[-_]?key)[ =]+)"
     r"(?=[A-Za-z0-9+/=._~-]*[A-Za-z])(?=[A-Za-z0-9+/=._~-]*[0-9])"
     r"[A-Za-z0-9+/=._~-]{6,}",
     "a credential passed as a flag"),
)

#: An assignment whose left side NAMES a credential. Same two-part test as
#: `autoreview._line_marker`: the name must name one and the value must look like one.
_PARAM_ASSIGNMENT_RE = re.compile(
    r"(?P<head>^[ \t]*(?:(?:export|set|const|let|var|readonly)[ \t]+)?"
    r"(?P<quote>[\"\']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P=quote)"
    r"[ \t]*(?:=>|:=|=|:)[ \t]*)"
    r"(?P<value>[^\r\n]*?)(?P<tail>[ \t]*[,;]?[ \t]*)$")

#: THE SAME ASSIGNMENT, ANYWHERE IN THE LINE — `NAME=value cmd`, `export NAME=value &&
#: …`, `…; NAME=value …`, `env NAME=value …`. The whole-line form above cannot see any
#: of those, and they are how a credential most often reaches `params["command"]`. The
#: value runs to the next whitespace, `;`, `&` or `|`, which is where the shell ends it.
_PARAM_INLINE_ASSIGNMENT_RE = re.compile(
    r"(?P<head>(?:^|[\s;&|(])(?:(?:export|env|set)[ \t]+)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)=)"
    r"(?P<value>[^\s;&|]+)")

#: Words that make a name a credential's name, matched against the identifier's PARTS:
#: `monkey` is not a `key` and `AWS_SECRET_ACCESS_KEY` is.
_PARAM_SECRET_NAMES = frozenset({
    "key", "keys", "apikey", "accesskey", "privatekey", "secretkey",
    "token", "tokens", "authtoken", "secret", "secrets", "clientsecret",
    "password", "passwd", "passphrase", "pwd", "credential", "credentials",
})

#: A VALUE THAT IS NOT A SECRET, however secret its name — the negative control. Most
#: `key=` lines in any repo carry an empty default, a placeholder or an expression, and
#: redacting those would make the parameter report useless for the one thing it is for:
#: reading what the worker actually ran.
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
    """`autoreview._secret_value`'s test, same reasoning: an expression is not a value."""
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


def _redact_assignment(m: "re.Match[str]") -> str:
    """One inline assignment, kept unless BOTH halves say credential."""
    if _names_a_credential(m.group("name")) and \
            _looks_like_a_credential(m.group("value")):
        return m.group("head") + CREDENTIAL_VALUE_MARKER
    return m.group(0)


def redact_param(text: str) -> str:
    """One tool-input value with its secret-shaped content NAMED, never quoted. Pure.

    Applied before the value reaches any payload (spec §4a) — a tool input carries file
    contents and anything a worker typed into a `Bash` command, and `jarvis inspect`
    output is read, pasted and stored. Returns the text unchanged when nothing matches,
    which is the overwhelmingly common case.
    """
    if not text:
        return text
    # ASSIGNMENTS BEFORE SHAPES, and the order is load-bearing: `API_KEY=sk-live-…`
    # is one finding, not two, and the assignment marker is the more informative of the
    # pair. Running the shapes first would name the value and leave the key beside it.
    lines = []
    for line in text.split("\n"):
        line = _PARAM_INLINE_ASSIGNMENT_RE.sub(_redact_assignment, line)
        m = _PARAM_ASSIGNMENT_RE.match(line)
        if m and _names_a_credential(m.group("name")) and \
                _looks_like_a_credential(m.group("value")):
            line = m.group("head") + CREDENTIAL_VALUE_MARKER + m.group("tail")
        lines.append(line)
    text = "\n".join(lines)
    for pattern, name in _PARAM_SHAPE_RE:
        text = pattern.sub(
            lambda m, n=name: (m.groupdict().get("keep") or "") + f"<redacted: {n}>",
            text)
    return text


@dataclass(frozen=True)
class ParamCaps:
    """How much of a tool's input may be reported, per value, per span and per turn.

    NOT `catalog.InspectConfig` settings, unlike the write and join floors. Those are a
    per-project JUDGEMENT about what counts as expensive; these are a STRUCTURAL bound
    on how big one report may get — a single `Write` input can be a whole file, and no
    project wants a different answer to "may `jarvis inspect` print a megabyte". Stated
    in every payload (`Anatomy.as_dict`) because a report that truncates without saying
    so is not reproducible.
    """

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
    """One tool input, redacted and bounded: `(params, truncated, dropped, spent)`.

    EVERY key of the input is accounted for — kept, listed as shortened, or listed as
    dropped — so the reader can tell "the tool was not given that" from "the report
    would not print it". `spent` is the turn's budget already used; the returned figure
    is what it becomes.
    """
    if not isinstance(payload, dict):
        return {}, [], [], spent
    params: dict[str, str] = {}
    truncated: list[str] = []
    dropped: list[str] = []
    span_spent = 0
    for key, value in payload.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
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
    """One tool call, from the model asking for it to the result coming back.

    `ended` is 0.0 when no matching `tool_result` was ever written — a turn killed
    mid-call. Such a span has no duration to report, and reporting zero would quietly
    subtract the very seconds the reader is looking for, so it is EXCLUDED from the
    partition and counted separately as `unfinished`.
    """

    name: str
    tool_id: str
    started: float
    ended: float = 0.0
    detail: str = ""
    #: The tool's whole `input`, redacted and capped (`_params_of`). ADDITIVE: `detail`
    #: above is unchanged and stays the one-line answer every renderer already prints —
    #: spec §4a, `docs/specs/2026-09-24-order-observability.md`.
    params: dict[str, str] = field(default_factory=dict)
    #: Keys shortened to `ParamCaps.per_value`, and keys left out because the span or
    #: turn budget ran out. Separate lists: "cut short" and "not printed" are different
    #: facts about the same key and a reader acts on them differently.
    params_truncated: list[str] = field(default_factory=list)
    params_dropped: list[str] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.ended > self.started

    @property
    def seconds(self) -> float:
        return self.ended - self.started if self.finished else 0.0

    @property
    def is_join(self) -> bool:
        return self.name in JOIN_TOOLS

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "tool_id": self.tool_id, "started": self.started,
                "ended": self.ended, "seconds": round(self.seconds, 2),
                "detail": self.detail, "join": self.is_join,
                "finished": self.finished, "params": self.params,
                "params_truncated": self.params_truncated,
                "params_dropped": self.params_dropped}


@dataclass
class Prompt:
    """One prompt that landed in the session — the reason a turn happened.

    `source` is `"sdk"` for a prompt JARVIS injected (dispatch, a `wo send`, a Neo
    answer, a `<task-notification>`) and `"user"` for one a human typed into the same
    session. Both start a turn and the distinction is not cosmetic: an injected session
    (`jarvis wo inject`) or a worker a person later picked up by hand has turns Jarvis
    never sent, and reading only the injected ones fuses them into one turn that appears
    to have run for days.
    """

    ts: float
    kind: str
    quote: str
    source: str = "sdk"

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "quote": self.quote,
                "source": self.source}


@dataclass
class Write:
    """One cache write big enough to classify, with the cause it is attributed to."""

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
    """One turn of the conversation, and the three ways its wall clock was spent.

    `triggers` is a LIST because Jarvis coalesces everything queued for a work order
    into one turn (`Daemon.deliver_messages`), so a turn can be started by a subagent
    finishing and a Neo answer arriving twenty milliseconds apart. Both are the reason
    it happened and showing one would misattribute it.
    """

    seq: int
    started: float
    ended: float
    #: The last thing the token accounting can see inside this turn — its last API call
    #: or finished tool span. `ended` runs on to the NEXT turn's prompt; what lies
    #: between the two is `idle`, and on a parked work order it is most of the turn.
    active_ended: float = 0.0
    triggers: list[Prompt] = field(default_factory=list)
    spans: list[ToolSpan] = field(default_factory=list)
    calls: list[usage_mod.Call] = field(default_factory=list)
    #: The intervals of THIS turn the OS's own record says it was held (`holds.held`),
    #: already clipped to it. Empty when nothing held it, and empty for a reading taken
    #: without a store — a report that cannot see the record must say a turn was held
    #: for zero seconds, never guess.
    holds: list[Hold] = field(default_factory=list)
    #: The subagents this turn's own spans NAME (`_attach_subagents`). A PARTITION of the
    #: turn, drawn out of it and never added to it (kn-7a2180ba, spec §4b): nothing below
    #: reads this field, so attaching one moves no figure of the parent's.
    subagents: list[SubagentAnatomy] = field(default_factory=list)

    @property
    def wall(self) -> float:
        return max(0.0, self.ended - self.started)

    @property
    def held(self) -> float:
        """Wall clock this turn was not permitted to run — see `holds`.

        Clamped to the wall clock rather than trusted: the spans come from one table and
        the clock from another file, and an `active` that went negative would be a
        partition no reader could make sense of.
        """
        return min(self.wall, sum(h.overlap(self.started, self.ended)
                                  for h in self.holds))

    @property
    def active(self) -> float:
        """THE CLOCK A THRESHOLD BELONGS ON: wall minus every recorded hold.

        Equal to `wall` on a turn nothing held, which is most of them — so this is not a
        second accounting to keep in step, it is the same one with the OS's own waiting
        taken out of it.

        NOT `active_ended`, which is a MOMENT and not a duration: that one is where the
        token accounting stops being able to see inside the turn, and it is what `idle`
        is measured from.
        """
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
        """After the worker's last API call, before the next turn's prompt.

        Nothing is running here — the `claude -p` process has exited and Jarvis has not
        sent the next prompt yet. See the module docstring for why it is not `generating`.
        """
        if not self.active_ended:
            return 0.0
        return max(0.0, self.ended - self.active_ended)

    @property
    def _remainder(self) -> float:
        """The wall clock nothing else accounts for — `generating` or `unaccounted`.

        Clamped at zero rather than allowed to go negative: tool spans are read from a
        file Jarvis does not write, and a clock skew or an overlapping pair of spans
        must not produce a partition that reads as nonsense.
        """
        return max(0.0, self.wall - self.blocked - self.tools - self.idle)

    @property
    def observed(self) -> bool:
        """Did anything in this turn actually reach the API? See `unaccounted`."""
        return bool(self.calls)

    @property
    def generating(self) -> float:
        """The remainder, but only where an API call vouches for it.

        A call's timestamp is when its response FINISHED, so every second up to it was
        the model producing that response and the remainder before the last call is
        honestly generating. A turn with no call at all has nothing for the clock to
        lead up to, and charging it anyway is issue 227.
        """
        return self._remainder if self.observed else 0.0

    @property
    def unaccounted(self) -> float:
        """The remainder of a turn that never reached the API — see the module docstring."""
        return 0.0 if self.observed else self._remainder

    @property
    def context_peak(self) -> int:
        """The largest context any one call of this turn carried — §1 step 1.

        Per TURN, which is the grain the method asks for and the grain `Usage` cannot
        give: `usage.priced` deliberately leaves `context_peak` at zero because it is a
        property of a conversation rather than of counts.
        """
        return max((c.context for c in self.calls), default=0)

    @property
    def unfinished(self) -> int:
        return sum(1 for s in self.spans if not s.finished)

    @property
    def usage(self) -> usage_mod.Usage:
        total = usage_mod.Usage()
        for call in self.calls:
            total = total + usage_mod.priced(
                call.model, messages=1, input=call.input,
                cache_write=call.cache_write, cache_read=call.cache_read,
                output=call.output, cache_1h=call.cache_1h, cache_5m=call.cache_5m)
        return total

    def share(self) -> dict[str, float]:
        """The partition as fractions of the wall clock, or all zero for an empty turn."""
        if self.wall <= 0:
            return {k: 0.0 for k in PARTS}
        return {k: getattr(self, k) / self.wall for k in PARTS}

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "started": self.started, "ended": self.ended,
            "wall": round(self.wall, 2),
            # BOTH CLOCKS, ALWAYS, and the wall one first: it is the honest answer to
            # "how long did this take in the real world" and deleting it was never on
            # the table. `held` is the difference, `held_by` says who took it.
            "active": round(self.active, 2), "held": round(self.held, 2),
            "held_by": {k: round(v, 2) for k, v in self.held_by().items()},
            "generating": round(self.generating, 2),
            "blocked": round(self.blocked, 2), "tools": round(self.tools, 2),
            "idle": round(self.idle, 2),
            "unaccounted": round(self.unaccounted, 2),
            # THE HEADLINE FACT ABOUT A TURN, carried beside the clock rather than left
            # to be inferred from `api_calls == 0`: every renderer has to say it first
            # and loudest, and an inference is what four layers got wrong (issue 227).
            "observed": self.observed,
            "share": {k: round(v, 4) for k, v in self.share().items()},
            "context_peak": self.context_peak,
            "api_calls": len(self.calls), "tool_calls": len(self.spans),
            "unfinished_tool_calls": self.unfinished,
            "triggers": [p.as_dict() for p in self.triggers],
            # ADDITIVE, and carried because the renderer DERIVES NOTHING (spec §4c):
            # `jarvis inspect --params` prints each span's parameters, and a CLI that
            # re-read the transcript to get them is a second answer to the same
            # question. The profile above stays the summary; this is the detail.
            "spans": [s.as_dict() for s in self.spans],
            "usage": self.usage.as_dict(),
            # ADDITIVE and empty on most turns: every key above is unchanged, and none
            # of them reads this one (spec §4b's partition rule).
            "subagents": [s.as_dict() for s in self.subagents],
        }


@dataclass
class SubagentAnatomy:
    """One subagent's OWN anatomy: its turns, its cache writes, its context peak.

    The same arithmetic as the parent, on its own transcript — `classify_writes` over
    `usage.calls_of`, `Turn.context_peak` off its turns — because a subagent transcript
    is a transcript and that is what answers "when is the user paying a write-cache" for
    the half of the spend the lead agent's file does not hold.

    ITS WRITES ARE ITS OWN AND ARE NEVER FOLDED INTO THE PARENT'S (spec §4b): a parent
    turn that merely waited on a join did not pay that write, and attributing it upward
    names the wrong turn as the prefix break.
    """

    task_id: str
    label: str = ""
    turns: list[Turn] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)
    #: Subagents of THIS subagent, counted and not read — see `SUBAGENT_DEPTH_READ`.
    deeper: int = 0

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
        """Tokens written, totalled per cause. Summed HERE and not in a renderer: the
        CLI and the dashboard both read this dict verbatim (spec §4c)."""
        totals: dict[str, int] = {}
        for write in self.writes:
            totals[write.cause] = totals.get(write.cause, 0) + write.written
        return totals

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "label": self.label,
            "turns": [t.as_dict() for t in self.turns],
            "writes": [w.as_dict() for w in self.writes],
            "writes_by_cause": self.writes_by_cause(),
            "context_peak": self.context_peak,
            "wall": round(self.wall, 2), "api_calls": self.api_calls,
            "deeper": self.deeper, "depth_read": SUBAGENT_DEPTH_READ,
        }


@dataclass
class Anatomy:
    """One session, taken apart. `found` is false when no transcript exists for it.

    Absent rather than empty: `usage.read_session` reports a missing transcript the same
    way and every surface that renders one has to say "no transcript" rather than
    "0 seconds", which is a different and untrue claim.
    """

    session_id: str
    found: bool = False
    turns: list[Turn] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)
    #: The thresholds this reading was taken at, carried so every rendering of it can
    #: state them. A report that shows "3 large writes" without saying what large meant
    #: is not reproducible, and these are per-project settings (`catalog.InspectConfig`).
    write_floor: int = DEFAULT_INSPECT_REPORT_WRITE_FLOOR
    join_floor: int = DEFAULT_INSPECT_REPORT_JOIN_FLOOR
    #: Task id -> what it was, from the subagent `.meta.json` Claude Code writes beside
    #: the transcript. This is what turns "blocked 450s on a7b62083" into a sentence.
    #: NOT `Turn.subagents`, which is the list of `SubagentAnatomy` — hence the name.
    subagent_labels: dict[str, str] = field(default_factory=dict)
    #: Subagent transcripts no `Agent`/`TaskOutput` span of this session names. Reported
    #: HERE rather than attached to the turn their timestamps fall in: a timestamp
    #: fallback invents a parent the record does not name (issue 227), so an unattached
    #: subagent says it is unattached (spec §4b, decided on wo-5f4d8611 q687).
    unattached_subagents: list[SubagentAnatomy] = field(default_factory=list)
    #: Every hold on the WHOLE work order (`holds.held`), including any falling outside
    #: the turns below. Kept whole rather than only as the per-turn slices, because a
    #: hold still running past the last turn is the live one, and it is the answer to
    #: "why has nothing happened since".
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
        """Seconds held per cause across the session, biggest first."""
        totals: dict[str, float] = {}
        for turn in self.turns:
            for cause, seconds in turn.held_by().items():
                totals[cause] = totals.get(cause, 0.0) + seconds
        return dict(sorted(totals.items(), key=lambda kv: -kv[1]))

    @property
    def unexplained(self) -> float:
        """Idle the record does NOT account for — the residual, and the honest one.

        `held` is clipped to the gaps between turns (`holds`' module note), so it is a
        subset of `idle` and this subtraction is exact rather than an estimate. It is the
        number the question behind this module actually asked: once the holds come out,
        is there a turn that sat open with no API call in flight and nothing holding it?
        A large one here is a DIFFERENT defect from anything this file can fix.
        """
        return max(0.0, sum(t.idle for t in self.turns) - self.held)

    def joins(self, over: float | None = None) -> list[ToolSpan]:
        """Blocking joins at or over `over` seconds — the report's floor by default."""
        floor = self.join_floor if over is None else over
        return sorted((s for s in self.spans if s.is_join and s.seconds >= floor),
                      key=lambda s: s.seconds, reverse=True)

    def tool_profile(self) -> list[dict[str, Any]]:
        """Count, total and mean seconds per tool name, dearest total first.

        Unfinished spans are counted but contribute no seconds, and the count says so —
        a mean over calls that never returned would be an average of a lie.
        """
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

    def partition(self) -> dict[str, float]:
        """The whole session's clock, summed over its turns.

        `active`, `held` and `unexplained` are deliberately NOT in `PARTS`: those five
        divide the wall clock between them and have to keep summing to it, while these
        cut the same clock a second way. Folding them in would make every percentage the
        renderers print stop meaning anything.
        """
        return {"wall": self.wall, "active": self.active, "held": self.held,
                "unexplained": self.unexplained,
                **{k: sum(getattr(t, k) for t in self.turns) for k in PARTS}}

    def cache_ttl(self) -> dict[str, int]:
        """Which TTL this session's cache writes were bought at — §1 step 5, §3.2.

        The finding it exists to make visible is a ZERO: across 1.8M cache-write tokens
        in the subject session the one-hour TTL was requested for none of them, and no
        surface said so. `unknown` is writes whose record carries no split at all, kept
        apart from a measured zero rather than folded into it.
        """
        split = {"cache_1h": 0, "cache_5m": 0, "unknown": 0}
        for turn in self.turns:
            for call in turn.calls:
                split["cache_1h"] += call.cache_1h
                split["cache_5m"] += call.cache_5m
                split["unknown"] += max(0, call.cache_write - call.cache_1h
                                        - call.cache_5m)
        return split

    def rewrite_excess(self) -> int:
        """Tokens this session paid to send twice — `usage`'s definition, not a second one.

        In a perfectly cached session every token is written to the cache exactly once,
        so the total written can never exceed the largest context reached. `usage` owns
        this arithmetic and the comment that justifies it; this reads the same two
        numbers off the calls already in hand rather than re-opening the file.
        """
        written = sum(c.cache_write for t in self.turns for c in t.calls)
        peak = max((t.context_peak for t in self.turns), default=0)
        return max(0, written - peak)

    def as_dict(self) -> dict[str, Any]:
        part = self.partition()
        wall = part["wall"] or 1.0
        return {
            "session_id": self.session_id,
            "found": self.found,
            "write_floor": self.write_floor,
            "join_floor": self.join_floor,
            # Stated beside the floors for the same reason they are: a truncation the
            # reader cannot size is not reproducible (spec §4a).
            "param_caps": PARAM_CAPS.as_dict(),
            "partition": {k: round(v, 2) for k, v in part.items()},
            "share": {k: round(part[k] / wall, 4) for k in PARTS},
            "held_by": {k: round(v, 2) for k, v in self.held_by().items()},
            "holds": [h.as_dict() for h in self.holds],
            # The legend, carried WITH the reading rather than looked up beside it: a
            # `--json` consumer and the dashboard both render a cause and neither should
            # have to hold its own copy of the wording (`PART_LABELS`' rule).
            "hold_causes": HOLD_CAUSES,
            "context_peak": max((t.context_peak for t in self.turns), default=0),
            "rewrite_excess": self.rewrite_excess(),
            "cache_ttl": self.cache_ttl(),
            "turns": [t.as_dict() for t in self.turns],
            "writes": [w.as_dict() for w in self.writes],
            "joins": [s.as_dict() for s in self.joins()],
            "tools": self.tool_profile(),
            # ADDITIVE, and `subagent_depth_read` is stated for the reason the floors and
            # the caps are: silence here would read as "there were none deeper".
            "unattached_subagents": [s.as_dict()
                                     for s in self.unattached_subagents],
            "subagent_depth_read": SUBAGENT_DEPTH_READ,
        }


# -- reading a transcript --------------------------------------------------------------


def _detail_of(payload: Any, limit: int) -> str:
    """A one-line answer to "doing what" for a tool call.

    `description` first wherever it exists, because it is the agent's own words for what
    it was doing and every long-running tool in the fleet carries one.

    REDACTED BEFORE IT IS CUT, and in that order (spec §4a, wo-5f4d8611 q687): `command`
    is second in the list, so an undescribed `Bash` call put the raw command line here
    while `params` beside it was redacted. Truncating first would leave the head of a
    credential printed; redacting first can only cost the tail of a marker.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("description", "command", "task_id", "file_path", "pattern", "skill"):
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
    """The prompt this row is, or None if it is not one.

    THREE KINDS OF `user` ROW share the type and only one of them starts a turn. A tool
    result is the agent's own loop, not an interruption. An `isMeta` row is Claude Code
    talking to itself — a skill's base directory, a hook's output — and counting one as a
    prompt would cut a turn in half at the moment a skill loaded.
    """
    if row.get("type") != "user":
        return None
    source = "sdk" if row.get("promptSource") == "sdk" else "user"
    if source == "user" and (row.get("isMeta") or usage_mod.blocks_of(row,
                                                                     "tool_result")):
        return None
    text = _prompt_text(row)
    if not text.strip():
        return None
    return Prompt(ts=ts, kind=_trigger_kind(text), quote=_first_line(text, limit),
                  source=source)


def _subagent_labels(path: Path) -> dict[str, str]:
    """Task id -> label, from the meta files beside a transcript.

    The task id a `TaskOutput` blocks on IS the subagent's transcript stem minus its
    `agent-` prefix, which is the only join between "what the lead agent waited on" and
    "what that thing was".
    """
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
    """One transcript file, cut into turns with their tool spans attached.

    A single ordered walk, because the three things it collects are interleaved and
    every one of them is defined by its position relative to the others: a turn starts
    at an injected prompt, but only at one with an assistant message since the last turn
    started — otherwise the two prompts Jarvis coalesces into one turn would read as two.
    """
    cfg = cfg or InspectConfig()
    turns: list[Turn] = []
    pending: dict[str, ToolSpan] = {}
    open_turn: Turn | None = None
    saw_assistant = False
    # The per-turn parameter budget is spent HERE, in the one ordered walk, because this
    # is the only place that knows which turn a span landed in. Reset with the turn: the
    # cap bounds one turn's report, not the session's (spec §4a).
    param_spent = 0

    for row in usage_mod.rows(path):
        ts = usage_mod.parse_stamp(row.get("timestamp"))
        # A TURN ENDS WHEN THE MODEL STOPS, NOT WHEN THE NEXT TURN STARTS. A worker turn
        # is one `claude -p` process and the next one can be days later, so closing a
        # turn at its successor's start charges it the whole idle gap: measured over the
        # fleet's 441 worker turns that read the longest one as ELEVEN DAYS of wall
        # clock, and every percentile above the median was the gap rather than the work.
        # Only conversation rows count — the UI-state rows Claude Code appends
        # (`custom-title`, `worktree-state`) are written outside the turn.
        if ts and open_turn is not None and row.get("type") in ("assistant", "user"):
            open_turn.ended = max(open_turn.ended, ts)
        prompt = _prompt_of(row, ts, cfg.quote_chars)
        if prompt is not None:
            # A new turn only if the model has spoken since the last one started:
            # `Daemon.deliver_messages` coalesces everything queued for a work order
            # into ONE turn, so a `<task-notification>` and the Neo answer twenty
            # milliseconds behind it are two triggers of one turn, not two turns.
            if open_turn is None or saw_assistant:
                open_turn = Turn(seq=len(turns) + 1, started=ts, ended=ts)
                turns.append(open_turn)
                saw_assistant = False
                param_spent = 0
            open_turn.triggers.append(prompt)
            continue
        if row.get("type") == "assistant":
            saw_assistant = True
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
                            params_dropped=dropped)
            pending[tool_id] = span
            # Charged to the turn that ASKED for it. A span whose result lands after the
            # next turn starts still belongs to the turn that spent the seconds.
            if open_turn is not None:
                open_turn.spans.append(span)
        for block in usage_mod.blocks_of(row, "tool_result"):
            span = pending.pop(str(block.get("tool_use_id") or ""), None)
            if span is not None:
                span.ended = ts

    return turns, _subagent_labels(Path(path))


def classify_writes(calls: Sequence[usage_mod.Call], floor: int,
                    compactions: Sequence[float] = ()) -> list[Write]:
    """Every cache write at or over `floor`, labelled with what caused it.

    The gap is measured to the PREVIOUS API call in the session whatever its size, and
    compared against the TTL that call bought — a 1-hour write (every Jarvis call before
    kn-5dd784f5) survives a gap that would expire a 5-minute one, so testing both
    against 300 seconds would call an honest expiry a defect.

    `compactions` are the moments the conversation was replaced
    (`usage.compactions_in`); a write across one is labelled `COMPACTION` before
    anything else, because by gap and read alone it is indistinguishable from a miss.
    """
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


def read_session(session_id: str, cfg: InspectConfig | None = None, *,
                 root: Path | None = None,
                 index: dict[str, list[Path]] | None = None,
                 spans: Sequence[Hold] = ()) -> Anatomy:
    """Take one session apart, across every segment file it left behind.

    Segments are read in path order and their turns concatenated by start time, then
    renumbered — a session resumed under a second cwd has a file under each slug
    (`usage.index_sessions`), and numbering per file would produce two turn 1s.

    `spans` is `holds.held` for the work order this session belongs to, and it is PASSED
    IN rather than read here on purpose: this module walks files Claude Code wrote and
    has never opened the OS's own database, which is what keeps it free to be called
    from a test, a `--json` consumer and the daemon alike. Left empty the report is the
    one it was before — every turn `active == wall`, which is the honest reading for a
    caller that cannot see the record rather than a claim that nothing held it.
    """
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
    for seq, turn in enumerate(turns, start=1):
        turn.seq = seq
    anatomy.turns = turns

    calls = usage_mod.session_calls(session_id, index=index)
    compactions = [c for path in sorted(paths)
                   for c in usage_mod.compaction_stamps(path)]
    anatomy.writes = classify_writes(calls, cfg.report_write_floor,
                                     sorted(compactions))
    _attach_calls(turns, calls)
    _close_turns(turns)
    _name_joins(turns, anatomy.subagent_labels)
    # AFTER `_name_joins`, which rewrites a join's `detail` from the bare task id to
    # "label (id)": matching on the id as a SUBSTRING works either side of it, so the
    # order of these two is not a trap for the next reader (spec §4b).
    anatomy.unattached_subagents = _attach_subagents(
        turns,
        [_read_subagent(sub, cfg, anatomy.subagent_labels)
         for path in sorted(paths) for sub in _subagent_transcripts(path)])
    # AFTER `_close_turns`, which is the only thing that knows where a turn ends: a hold
    # attached before it would be measured against a turn whose `ended` was still its
    # own last row rather than its successor's prompt.
    anatomy.holds = list(spans)
    _attach_holds(turns, anatomy.holds)
    return anatomy


def _attach_holds(turns: Sequence[Turn], spans: Sequence[Hold]) -> None:
    """Give every turn the holds that overlap it. One hold can reach two turns.

    The whole span is attached to each rather than pre-cut, because `Turn.held` clips it
    to its own clock anyway — and a pre-cut copy is a second version of the same fact to
    keep in step.
    """
    for turn in turns:
        turn.holds = [h for h in spans if h.overlap(turn.started, turn.ended) > 0]


def _close_turns(turns: Sequence[Turn]) -> None:
    """Give every turn its two ends: when it stopped working, and when it stopped.

    `active_ended` is the last thing the token accounting can SEE inside the turn — its
    own API calls and finished tool spans. THE ROW CLOCK IS NOT ENOUGH FOR THIS. A
    transcript can be appended to long after its last call (an old-transport session
    resumed by hand under the same id writes conversation rows with no prompt row before
    them), and one such file charged a 21-minute turn with TWELVE DAYS. Rows `usage`
    cannot count must not move a clock `usage` is the denominator of.

    `ended` runs on to the NEXT turn's prompt, which is the method's own rule
    (`docs/findings/anatomy-of-an-expensive-turn.md` §2) and is what makes the turns
    sum to the session with nothing between them dropped. The gap between the two is `idle`.

    A turn with no calls and no finished spans keeps `active_ended` at zero, which reads
    as "no idle known" rather than as an idle turn — there is nothing to measure from,
    and zero would be a claim rather than a gap.
    """
    for index, turn in enumerate(turns):
        seen = [call.ts for call in turn.calls]
        seen += [span.ended for span in turn.spans if span.finished]
        if seen:
            turn.active_ended = max(turn.started, max(seen))
        following = turns[index + 1] if index + 1 < len(turns) else None
        # The LAST turn has no successor to run on to, so it ends where it stopped
        # working — and that is exactly where the twelve-day transcript above lives.
        turn.ended = following.started if following else (turn.active_ended
                                                          or turn.ended)


def _attach_calls(turns: Sequence[Turn], calls: Iterable[usage_mod.Call]) -> None:
    """Put each API call in the turn that was running when it landed.

    The same last-turn-started-by-then rule the bill uses (`bill._turn_locator`), so the
    two accountings cut the session at identical points and a reader can lay one beside
    the other.
    """
    ordered = sorted(turns, key=lambda t: t.started)
    for call in calls:
        home: Turn | None = None
        for turn in ordered:
            if turn.started <= call.ts:
                home = turn
            else:
                break
        if home is not None:
            home.calls.append(call)


def _read_subagent(path: Path, cfg: InspectConfig,
                   labels: dict[str, str]) -> SubagentAnatomy:
    """One subagent transcript, taken apart the way `read_session` takes the parent apart.

    The task id is the stem minus its `agent-` prefix — the same join `_subagent_labels`
    uses, and the only thing tying "what the lead waited on" to "what that thing was".
    An unlabelled subagent keeps an empty label: no meta file was written, and there is
    nothing else in the record to name it.
    """
    stem = path.stem
    task_id = stem[len("agent-"):] if stem.startswith("agent-") else stem
    turns, _ = read_transcript(path, cfg)
    calls = usage_mod.calls_of(path)
    _attach_calls(turns, calls)
    _close_turns(turns)
    return SubagentAnatomy(
        task_id=task_id, label=labels.get(task_id, ""), turns=turns,
        writes=classify_writes(calls, cfg.report_write_floor,
                               sorted(usage_mod.compaction_stamps(path))),
        deeper=len(_subagent_transcripts(path)))


def _names(span: ToolSpan, task_id: str) -> bool:
    """Whether this span names that subagent — `detail` or any reported parameter.

    Substring, not equality, and that is what `_name_joins` needs: it rewrites a join's
    `detail` from the bare id to "label (id)", so equality would hold before it ran and
    not after. The params are read too, because the `TaskOutput` span is the join in
    practice — an `Agent` span's parameters are a `description` and carry no task id at
    all (tests/data/transcripts/-wo-5a6b2d6d), so `task_id` on `TaskOutput` is the only
    place the id appears as a parameter.
    """
    return span.name in SPAWN_TOOLS and (
        task_id in span.detail or any(task_id in v for v in span.params.values()))


def _attach_subagents(turns: Sequence[Turn],
                      subs: Sequence[SubagentAnatomy]) -> list[SubagentAnatomy]:
    """Hang each subagent off the turn whose span NAMES it; return the rest.

    No timestamp fallback, decided on this work order (q687, option 1): containment
    would invent a parent the record does not name, which is issue 227's mistake. A
    subagent nothing names is returned to `Anatomy.unattached_subagents` and reported as
    unattached.
    """
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
    """Replace a join's raw task id with what was actually being waited on."""
    for turn in turns:
        for span in turn.spans:
            if span.is_join and span.detail in labels:
                span.detail = f"{labels[span.detail]} ({span.detail})"


# -- the live half: raising it while it is still burning -------------------------------


TURN_ALARM, JOIN_ALARM, WRITE_ALARM = "long-turn", "long-join", "big-rewrite"
#: The one alarm here whose finding is that NOTHING was spent — see `ALARM_KINDS`.
STALL_ALARM = "stalled-turn"

#: THE AGGREGATE PAIR, and the only kinds in this module that are not about one turn:
#: `alarms()` never raises them and cannot. They are a PROJECT's re-write tax over a
#: cohort window of settled orders, computed by `bill.rewrite_tax` and raised by
#: `Daemon.check_rewrite_tax` — the standing condition `WRITE_ALARM` cannot report,
#: because that one judges a single call while the turn that made it is still running.
#:
#: They are declared HERE, beside the live four, because `ALARM_KINDS` is the one place a
#: surface looks up what an alarm kind MEANS (`ui.app` passes it to /alarms) and a kind
#: missing from it renders as a bare id. The raising lives where the arithmetic is.
#:
#: TWO KINDS RATHER THAN ONE WITH THE CAUSE IN ITS PROSE: the prefix moving is bought
#: back by keeping the prefix still and the entry expiring by a longer TTL, so they are
#: opposite cures (kn-1449447a), and one kind would make "which cure" a detail of a
#: sentence instead of the identity a dedupe, a filter and a learning can key on.
REWRITE_PREFIX_ALARM = "rewrite-tax-prefix"
REWRITE_TTL_ALARM = "rewrite-tax-ttl"

#: THE OTHER AGGREGATE PAIR, and the only alarms in the OS about spend it did not make.
#: A one-hour cache write costs 2.0x base input where a five-minute one costs 1.25x, and
#: `claude_cli.cache_env` forces the cheap one on every process Jarvis starts — so a 1h
#: write left in the fleet is one of two unrelated faults. `Daemon.check_cache_ttl` raises
#: them off `one_hour_writes`; issue 164 item 3.
#:
#: TWO KINDS AND NEVER ONE, for the reason the pair above is two: `…-dispatched` is a
#: BREACH of this OS's own transport guarantee and a defect to fix in this repository,
#: while `…-foreign` is a person's own `claude` and can only be REPORTED — the setting is
#: their personal config and Jarvis must not write it. Merging them would blame the OS for
#: a human sitting next to it, which is the misreading finding 3 was written to prevent.
CACHE_1H_DISPATCHED_ALARM = "cache-1h-dispatched"
CACHE_1H_FOREIGN_ALARM = "cache-1h-foreign"

#: What each kind IS, for a surface listing alarms rather than raising one. An `Alarm`'s
#: own `reason` is about one turn and carries its numbers; this is the standing meaning,
#: and it lives beside the constants so a dashboard and the CLI cannot drift on it.
ALARM_KINDS = {
    TURN_ALARM: "a turn still running, still being billed",
    # THE ODD ONE OUT, deliberately: the other three say money is going out and this one
    # says none is. It is the opposite finding to the `going-in-circles` health probe,
    # which says effort is being RE-spent — here the work never started (issue 227).
    STALL_ALARM: "a turn open with no API call ever made — nothing is being billed",
    JOIN_ALARM: "a join open past the cache TTL — the wait is paid for twice",
    WRITE_ALARM: "the conversation sent again, at the cache-write rate",
    # The two aggregate kinds. Worded as a share of a PROJECT rather than of a turn, so a
    # reader of the legend cannot take them for another reading of `big-rewrite`.
    REWRITE_PREFIX_ALARM: "a project's conversations re-sent because the prompt PREFIX "
                          "moved — the half no cache TTL can buy back",
    REWRITE_TTL_ALARM: "a project's conversations re-sent because the cache entry "
                       "EXPIRED — the half a longer TTL could buy back",
    # The 1h pair. Worded as WHO BOUGHT IT rather than what it cost, because that is the
    # whole distinction and the legend is where a reader meets these kinds first.
    CACHE_1H_DISPATCHED_ALARM: "Jarvis's own turns bought the one-hour cache write — a "
                               "breach of the transport's 5-minute guarantee",
    CACHE_1H_FOREIGN_ALARM: "sessions Jarvis never dispatched bought the one-hour cache "
                            "write — reportable, not fixable from inside the OS",
}


@dataclass
class Alarm:
    """A turn that is costing money NOW, in a sentence the attention list can carry.

    `kind` is what tripped and `reason` is what the user reads. Both, because the reason
    is prose that will be reworded and the kind is what a test and a timeline event
    match on.
    """

    kind: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason}


def _spend_so_far(turn: Turn, now: float | None) -> str:
    """What the turn has actually bought, so "still being billed" can be checked.

    THE COST IS READ, NEVER INFERRED FROM THE CLOCK (issue 227, and the standing rule
    recorded against al-abf99387). `turn.usage` is the same per-turn accounting `jarvis
    cost` prints, so an alarm that says a turn is expensive is quoting the record rather
    than the duration — and a turn wedged on a permission prompt, which makes no API call
    while its wall clock runs, cannot look like one that is generating.

    Called only for a turn that HAS calls: the caller branches on `Turn.observed` first,
    because a turn with none gets `STALL_ALARM` and no claim about money at all. THE
    GUARD BELOW IS THAT PRECONDITION IN CODE, and it returns rather than raising: this
    runs inside `Daemon.check_burning_turns`, where a `ValueError` out of `max()` would
    take a whole project's reconcile tick with it. It is worded so that a caller which
    lost the branch produces a sentence that is visibly self-contradicting — "still
    being billed (NO API CALL …)" — rather than a plausible one, which is the whole
    failure mode of issue 227.
    """
    if not turn.calls:
        return "NO API CALL — nothing has been billed"
    made = f"{len(turn.calls)} API call" + ("s" if len(turn.calls) > 1 else "")
    ago = (now - max(call.ts for call in turn.calls)) if now else 0.0
    when = "seconds ago" if ago < 60 else f"{int(ago // 60)}m ago"
    spend = turn.usage
    return (f"{made}, the last one {when}, {spend.total_tokens:,} tokens for "
            f"${spend.list_cost_usd:.2f} so far")


def held_note(wall: float, holding: dict[str, float]) -> str:
    """" (6.8h on the wall clock, 4.0h of it held by a fleet usage limit)", or "".

    THE ONE PLACE THE DIFFERENCE BETWEEN THE TWO CLOCKS IS PUT INTO WORDS, shared by the
    live alarm and the report so a user who meets it in an attention line and then runs
    `jarvis inspect` reads the same sentence twice rather than two accounts of one fact.
    Empty when nothing held the turn, which is most turns — the wall clock is then the
    active one and a reader is owed no explanation of a difference that does not exist.
    """
    if not holding:
        return ""
    named = ", ".join(f"{_hours(seconds)} of it held by {HOLD_CAUSES.get(cause, cause)}"
                      for cause, seconds in holding.items())
    return f" ({_hours(wall)} on the wall clock, {named})"


def _hours(seconds: float) -> str:
    """Durations as a reader thinks of them — `cli._mins` for a sentence rather than a
    column, so an alarm about four hours does not say `239.4m`."""
    return f"{seconds / 3600:.1f}h" if seconds >= 3600 else f"{int(seconds // 60)}m"


def alarms(anatomy: Anatomy, cfg: InspectConfig, wo_id: str = "",
           now: float | None = None, *, dispatched: float) -> list[Alarm]:
    """What is wrong with the turn that is running, most actionable first.

    Judges THE LAST TURN ONLY. Everything before it is already paid for and belongs on
    `jarvis inspect`, not in front of the user: an alarm the user can do nothing about
    is the noise that gets a cost alarm ignored, and then it is worse than nothing.

    `now` is what makes this a LIVE reading rather than a historical one. A turn that is
    generating has written no row for minutes, so its clock has to be measured against
    the wall and not against its own last line — measuring it against the transcript
    would report an hour-long turn as however long ago it last spoke.

    `dispatched` is the EVIDENCE that the last transcript turn is the one the caller
    means, and it has no default because there is no honest fallback: measuring `now -
    turn.started` against a turn the transcript has moved past charges the whole
    inter-turn gap to a turn that has not started. Every `long-turn` alarm the fleet
    raised before this was that race — three of them the 197 minutes from a session
    limit to its reset, one the 16 hours a work order sat parked overnight. Pass the
    turn RECORD's dispatch time; a transcript turn older than it is the previous turn,
    and nothing is known about the new one yet.

    THE TWO DURATION ALARMS ARE JUDGED ON ACTIVE TIME, NOT WALL CLOCK (the user's ruling
    of 2026-09-18). Both say something about the WORK — that it has been running an hour,
    that it has started and made no call — and neither claim is true of an order the OS
    is holding: a turn that spans a spent usage window is not slow, it is obeying. The
    `dispatched` guard above already kept the commonest shape of that out, but it is a
    proxy for the right question and this is the right question. `long-join` deliberately
    stays on the WALL clock: what it reports is a prompt cache going cold, and the cache
    expires in real seconds whether or not anyone was allowed to work.
    """
    if not anatomy.found or not anatomy.turns or not cfg.enabled:
        return []
    turn = anatomy.turns[-1]
    if turn.started < dispatched:
        return []
    wall = max(turn.wall, (now - turn.started) if now else 0.0)
    # `anatomy.holds` and NOT `turn.holds`: the per-turn slices were attached against a
    # turn that ends where the transcript stopped, and the hold this function exists to
    # respect is the one still running past that — the live case is the only case here.
    window = (turn.started, turn.started + wall)
    held = min(wall, sum(h.overlap(*window, now) for h in anatomy.holds))
    active = max(0.0, wall - held)
    holding = by_cause(anatomy.holds, *window, now)
    raised: list[Alarm] = []
    hint = f" — `jarvis inspect {wo_id}`" if wo_id else ""

    # THE TWO DURATION ALARMS ARE EXCLUSIVE, and which one applies is decided by the
    # cost record rather than by the clock. A turn that has made no API call has bought
    # nothing, so `long-turn`'s claim — that it is still being billed — would be false;
    # the finding is the silence itself, and it is raised sooner.
    if not turn.observed:
        if active >= cfg.alarm_stalled_minutes * 60:
            raised.append(Alarm(STALL_ALARM, (
                f"this turn has been open {int(active // 60)} minutes"
                f"{held_note(wall, holding)} and has made no API call at all — the work "
                f"never started, and nothing has been spent on it{hint}")))
    elif active >= cfg.alarm_turn_minutes * 60:
        raised.append(Alarm(TURN_ALARM, (
            f"this turn has been running {int(active // 60)} minutes"
            f"{held_note(wall, holding)} and is still being billed "
            f"({_spend_so_far(turn, now)}){hint}")))
    for span in turn.spans:
        if span.is_join and not span.finished and now and \
                now - span.started >= cfg.alarm_join_seconds:
            waited = int((now - span.started) // 60)
            raised.append(Alarm(JOIN_ALARM, (
                f"blocked {waited}m waiting on {span.detail or span.tool_id} with no "
                f"API call in flight — long enough to lose the prompt cache, so the "
                f"wait will be paid for twice{hint}")))
            break
    for write in anatomy.writes:
        # A compaction is exempt for `COLD_START`'s reason and one more: the alarm's
        # advice is "the conversation is being paid for again", and here it is being
        # paid for ONCE, on purpose, instead of in full.
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
                spans: Sequence[Hold] = ()) -> list[Alarm]:
    """`alarms` for a session id — one transcript read, no paid call, nothing written.

    The session is read at the ALARM's write threshold rather than the report's: the only
    writes this needs to see are the ones big enough to raise one, and classifying the
    small ones would be work whose answer is thrown away.

    `spans` is the caller's `holds.held` for this work order. Passing none is the pre-hold
    behaviour and alarms on the wall clock, which is why `Daemon.check_burning_turns`
    always passes them: the default is what a caller without a store gets, not a policy.
    """
    reading = replace(cfg, report_write_floor=cfg.alarm_write_tokens)
    anatomy = read_session(session_id, reading, root=root, index=index, spans=spans)
    return alarms(anatomy, cfg, wo_id=wo_id, now=now, dispatched=dispatched)


# -- the fleet's cache-TTL hygiene: who is still buying the one-hour write --------------
#
# Finding 3 of docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md. A 1h
# cache write costs 2.0x base input where a 5m write costs 1.25x, and `claude_cli.
# cache_env` forces the cheap one on every process Jarvis starts — so any 1h write left
# in the fleet is either a BREACH of that guarantee or a `claude` a person typed, and
# those are unrelated faults with unrelated fixes.
#
# THIS IS THE ONE COST READING THAT CANNOT COME OFF A BILL. `bill.rewrite_tax` reads
# sealed bills precisely to avoid a transcript walk; the sessions this exists to find
# were never dispatched, so they have no work order, no bill and no row anywhere in the
# OS. The transcript is the only place they exist.

#: The `cache_creation` key that says a write bought the hour. Also this module's cheap
#: pre-filter: a file that never mentions it has nothing to say here, which is 98% of
#: them, and skipping those is what makes the walk affordable at all.
ONE_HOUR_KEY = '"ephemeral_1h_input_tokens"'


@dataclass
class HourWrites:
    """One session's ONE-HOUR cache writes, split by who sent the prompt.

    NEVER ADDED UP, and that is the whole design. `dispatched` means Jarvis's own
    transport bought the expensive write, which is a defect in this OS; `foreign` means a
    person's own session did, which the OS can only report. Finding 3 established that
    an alarm merging the two blames the OS for a human sitting next to it.
    """

    session_id: str
    #: The transcript directory: the slugified cwd the session was created in. Kept as
    #: the raw slug rather than a path, because reconstructing one guesses at every `-`
    #: that was once a `/` — it is an identifier here, not a location.
    directory: str = ""
    dispatched: int = 0
    foreign: int = 0
    #: First and last 1h WRITE, not the session's lifetime. A session mostly paying the
    #: correct rate must not report its whole span as the leak.
    first_ts: float = 0.0
    last_ts: float = 0.0

    @property
    def total(self) -> int:
        return self.dispatched + self.foreign

    def as_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "directory": self.directory,
                "dispatched": self.dispatched, "foreign": self.foreign,
                "total": self.total, "first_ts": self.first_ts,
                "last_ts": self.last_ts}


def _mentions(path: Path, needle: str) -> bool:
    """Whether a file contains `needle` at all, without parsing a line of it."""
    try:
        with path.open(errors="replace") as handle:
            return any(needle in line for line in handle)
    except OSError:
        return False


def _subagent_transcripts(path: Path) -> list[Path]:
    """The subagent transcripts written beside one session file — `read_session`'s rule.

    Their spend is charged to the session that SPAWNED them, here as there: a subagent
    has no id anyone can resume, so it is not an answer to "which session do I go and
    look at".
    """
    directory = path.with_suffix("") / "subagents"
    return sorted(directory.glob("*.jsonl")) if directory.is_dir() else []


def one_hour_writes(since: float, *, root: Path | None = None,
                    cfg: InspectConfig | None = None) -> list[HourWrites]:
    """Every session that bought the one-hour cache write since `since`, biggest first.

    ATTRIBUTION IS `_prompt_of`'s, NOT A SECOND POLICY. A turn is Jarvis's when the
    prompt that started it carries `promptSource: "sdk"` — the same discriminator
    `Prompt.source` has always used and the one finding 3 established by hand. Keying on
    `work_orders.session_id` instead gets it WRONG on the largest offender the fleet has:
    wo-2df8828c's transcript is ONE file holding both a dispatched turn and the same
    worktree reopened by hand afterwards, and all 2,512,088 of its 1h tokens are the
    hand-opened half's.

    A TURN IS JARVIS'S ONLY IF EVERY TRIGGER IS. `Daemon.deliver_messages` coalesces
    what it queues, so a turn has several, and one prompt a person typed among them means
    a person was at the keyboard — the claim `dispatched` makes is about the process, and
    a wrong one there accuses the OS of a defect it does not have.

    SUBAGENTS INHERIT THEIR PARENT'S CLASSIFICATION, because their transcripts carry no
    `promptSource` at all (measured: 1,002 user rows, none with the field). Classified on
    their own evidence every one of them would read as a person's, which would hide
    exactly the breach this exists to catch. `_attach_calls` does the inheriting — their
    calls land in the parent turn that was running, by the rule the bill already uses.

    AN UNKNOWN TTL IS NOT A 1h TTL. Only `ephemeral_1h_input_tokens` itself counts, so a
    row written before Claude Code reported the split contributes nothing — the floor
    `usage`'s module note sets out. An alarm that read absence as the expensive write
    would fire on every old transcript in the fleet.
    """
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
                if dispatched:
                    entry.dispatched += call.cache_1h
                else:
                    entry.foreign += call.cache_1h
                entry.first_ts = min(entry.first_ts, call.ts) or call.ts
                entry.last_ts = max(entry.last_ts, call.ts)
    return sorted(found.values(), key=lambda e: (-e.total, e.session_id))


#: The one line that fixes the foreign half, quoted verbatim because the alarm's whole
#: job is to make the work order behind it writable without a second investigation.
#: `claude_cli.PROMPT_CACHE_5M_ENV` is the same flag, applied where Jarvis CAN apply it.
REMEDY_LINE = '"env": {"FORCE_PROMPT_CACHING_5M": "1"}  in ~/.claude/settings.json'

#: Said on the foreign alarm and on nothing else. THE HONEST CON, recorded in finding 3
#: and not to be papered over: this reports a condition Jarvis cannot itself fix, because
#: the file is the user's personal config. Naming the limit is what stops a supervisor
#: proposing a remedy that would have Jarvis write there.
NOT_OURS_TO_FIX = (
    "JARVIS CANNOT FIX THIS AND MUST NOT TRY: the file is the user's own Claude "
    "configuration, outside every project the OS manages. The deliverable is a work "
    "order that tells a PERSON to add the line above, and a check that it took."
)

#: How many offending sessions the reason names before it stops. Enough that a work order
#: can be written off the alarm alone; bounded because the reason is read in an inbox row
#: and a Telegram push, and the tail of a long list is tokens nobody acts on.
NAMED_SESSIONS = 3


def _hour_premium_usd(tokens: int) -> float:
    """What buying the hour COST above the five-minute write, at Opus list prices.

    The premium and not the price: these tokens would have been written either way, so the
    avoidable money is the rate difference alone (kn-f94abf34 (0)). Opus because
    `HourWrites` counts tokens and not models — the alarm says "at Opus list" in as many
    words rather than implying a precision it does not have.
    """
    premium = usage_mod.CACHE_WRITE_1H_RATE - usage_mod.CACHE_WRITE_RATE
    return premium * tokens * usage_mod.DEFAULT_PRICE[0] / 1e6


def _name_sessions(found: Sequence[HourWrites], attr: str) -> str:
    """The biggest offenders, named. WHICH SESSION is the fact finding 3 turns on."""
    named = [e for e in found if getattr(e, attr)][:NAMED_SESSIONS]
    return "; ".join(
        f"{e.session_id} in {e.directory} ({getattr(e, attr):,} tokens, last written "
        f"{datetime.fromtimestamp(e.last_ts, tz=timezone.utc):%Y-%m-%d})"
        for e in named)


def hour_alarms(found: Sequence[HourWrites], cfg: InspectConfig, *,
                days: int) -> list[Alarm]:
    """What is standing-wrong with the fleet's cache TTL, one alarm per CAUSE.

    The dispatched one first, because it is the more serious finding by far and `alarms`'
    ordering contract is most-actionable-first: one is a defect in this repository and the
    other is a note to a person about their own machine.
    """
    dispatched = sum(e.dispatched for e in found)
    foreign = sum(e.foreign for e in found)
    window = f"in the last {days} days"
    raised: list[Alarm] = []
    if dispatched >= cfg.alarm_cache_1h_dispatched_tokens:
        raised.append(Alarm(CACHE_1H_DISPATCHED_ALARM, (
            f"{dispatched:,} tokens were written at the ONE-HOUR cache TTL {window} by "
            f"turns JARVIS ITSELF dispatched — about "
            f"${_hour_premium_usd(dispatched):,.2f} at Opus list above what the "
            f"five-minute write would have cost. `claude_cli.cache_env` forces "
            f"FORCE_PROMPT_CACHING_5M on every process this OS starts, so this is a "
            f"BREACH of that guarantee and a defect in the OS — not in anyone's personal "
            f"configuration. Start at `claude_cli._run` and `spawn_turn`, the only two "
            f"functions here that start a process, and at "
            f"`dispatch._write_worker_settings`. Sessions: "
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
