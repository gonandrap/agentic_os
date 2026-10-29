"""What a turn is doing RIGHT NOW — `jarvis watch` and `ops.live_report` (#227)"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import inspection
from . import usage as usage_mod
from .catalog import DEFAULT_INSPECT_QUOTE_CHARS, DEFAULT_INSPECT_REPORT_WRITE_FLOOR
from .holds import Hold

#: Every state a frame can be in, in the order they are decided (Neo q678)
STATES = ("working", "generating", "idle", "settled", "no-transcript")
WORKING, GENERATING, IDLE, SETTLED, NO_TRANSCRIPT = STATES

# THE THREE NUMBERS BELOW ARE MODULE CONSTANTS AND NOT CATALOG SETTINGS

#: How many finished tool calls a frame carries
RECENT_SPANS = 5

#: The width every tool parameter is rendered at
PARAMS_CAP = 200
TRUNCATED = "…[truncated]"
REDACTED = "[redacted]"

#: Same width `jarvis inspect` quotes a prompt at by default, borrowed as a literal
QUOTE_CHARS = DEFAULT_INSPECT_QUOTE_CHARS

_HEAD_BYTES = 512
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")

#: kn-1791a5e6's one pattern: the userinfo of a URL
_URL_USERINFO_RE = re.compile(r"://[^/\s@]+@")
_URL_USERINFO_SAFE = "://<redacted>@"

#: Words that make a NAME a credential's name
SECRET_NAME_PARTS = frozenset({
    "key", "keys", "apikey", "accesskey", "privatekey", "secretkey", "seckey",
    "token", "tokens", "authtoken", "secret", "secrets", "clientsecret",
    "password", "passwd", "passphrase", "pwd", "credential", "credentials",
})

#: SHAPES, because a NAME test only catches the credential somebody already thought of and round 1 of...
SECRET_SHAPES: tuple[tuple[str, str], ...] = (
    # The whole BLOCK and not the BEGIN line
    (r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----.*?"
     r"(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)", "a private key block"),
    (r"\bssh-(?:rsa|dss|ed25519)\s+AAAA[0-9A-Za-z+/=]+", "an ssh key line"),
    (r"\bAuthorization[\"\']?\s*:\s*[\"\']?(?:Bearer|Basic|Token)\s+\S+",
     "an Authorization header value"),
    # THE TWO `inspection`'S SET DOES NOT HAVE
    (r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{6,}", "a GitHub token"),
    (r"\bsk-[A-Za-z0-9_-]{6,}", "an API key"),
)

#: `NAME=value` ANYWHERE IN THE STRING
_ASSIGNMENT_RE = re.compile(
    r"(?P<head>(?:\A|(?<=[\s;&|(]))(?:(?:export|env|set)\s+)?-{0,2}"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)=)(?P<value>[^\s]+)")

#: A VALUE THAT IS NOT A SECRET, however secret its name
_PLACEHOLDER_RE = re.compile(
    r"none|null|nil|nan|true|false|x+|\.+|-+|_+|"
    r"todo|tbd|fixme|changeme|change[-_]me|placeholder|redacted|dummy|fake|sample|"
    r"example|examples|test|testing|secret|password|passwd|token|key|value|"
    r"your[-_].*|my[-_].*|some[-_].*|the[-_].*", re.IGNORECASE)
_VALUE_CHARS = re.compile(r"[A-Za-z0-9+/=._~-]+")
_SHAPE_RE = [(re.compile(pattern, re.IGNORECASE | re.DOTALL), name)
             for pattern, name in SECRET_SHAPES]
CREDENTIAL_VALUE = "<redacted: a credential value>"


def clock(ts: float) -> str:
    """A wall-clock time a person can compare against their own — local, to the second"""
    return time.strftime("%H:%M:%S", time.localtime(ts))


# -- redaction


def redact_params(payload: Any, cap: int = PARAMS_CAP) -> dict[str, str]:
    """A tool call's input, safe and bounded — the one path every parameter takes"""
    if not isinstance(payload, dict):
        return {}
    def one(key: str, value: Any) -> str:
        return REDACTED if _names_secret(key) else _render(_scrub(value), cap)

    return {str(key): one(str(key), value) for key, value in payload.items()}


def _names_secret(identifier: str) -> bool:
    parts = [p.lower() for p in _WORD_SPLIT_RE.split(identifier) if p]
    return any(p in SECRET_NAME_PARTS for p in parts)


def _scrub(value: Any) -> Any:
    """Both tests applied at EVERY depth — a leak nests by construction"""
    if isinstance(value, dict):
        return {k: REDACTED if _names_secret(str(k)) else _scrub(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def _scrub_text(text: str) -> str:
    """One string, with every secret-shaped part of it NAMED rather than quoted"""
    if not text:
        return text
    text = _URL_USERINFO_RE.sub(_URL_USERINFO_SAFE, text)
    for pattern, name in _SHAPE_RE:
        text = pattern.sub(f"<redacted: {name}>", text)
    return _ASSIGNMENT_RE.sub(_assignment_marker, text)


def _assignment_marker(m: re.Match[str]) -> str:
    """The two-part test, `autoreview._line_marker`'s: the name must NAME a credential AND the value must..."""
    if _names_secret(m.group("name")) and _looks_like_a_credential(m.group("value")):
        return m.group("head") + CREDENTIAL_VALUE
    return m.group(0)


def _looks_like_a_credential(raw: str) -> bool:
    """`autoreview._secret_value`'s test, same reasoning: an expression is not a value"""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if len(value) < 6 or not _VALUE_CHARS.fullmatch(value):
        return False
    if _PLACEHOLDER_RE.fullmatch(value):
        return False
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    return (has_digit and has_alpha) or len(value) >= 20


def _render(value: Any, cap: int) -> str:
    """One string per parameter, capped — which is also what bounds a nested structure"""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    # Again, on the RENDERED text and still before the cut (kn-1791a5e6: scrub before you slice):...
    text = _scrub_text(text)
    if len(text) <= cap:
        return text
    return text[:max(0, cap - len(TRUNCATED))] + TRUNCATED


# -- the frame


@dataclass
class _Span:
    """One tool call in flight or just finished"""

    tool: str
    tool_id: str
    started: float
    detail: str = ""
    params: dict[str, str] = field(default_factory=dict)
    ended: float = 0.0

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        end = self.ended if self.ended else (now or 0.0)
        return {"tool": self.tool, "detail": self.detail, "params": self.params,
                "started": self.started, "ended": self.ended or None,
                "elapsed": round(max(0.0, end - self.started), 2)}


@dataclass
class _Turn:
    seq: int
    started: float
    triggers: list[inspection.Prompt] = field(default_factory=list)


@dataclass
class Live:
    """One frame: the answer to "what is it doing right now", and what it will not claim"""

    wo_id: str
    project: str
    session_id: str
    found: bool
    state: str
    note: str
    now: dict[str, Any] | None = None
    turn: dict[str, Any] | None = None
    recent: list[dict[str, Any]] = field(default_factory=list)
    tokens: dict[str, int] | None = None
    last_write: dict[str, Any] | None = None
    stale_seconds: float | None = None
    subagents: dict[str, str] = field(default_factory=dict)
    holds: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"wo_id": self.wo_id, "project": self.project,
                "session_id": self.session_id, "found": self.found,
                "state": self.state, "turn": self.turn, "now": self.now,
                "recent": self.recent, "tokens": self.tokens,
                "last_write": self.last_write, "stale_seconds": self.stale_seconds,
                "subagents": self.subagents, "holds": self.holds, "note": self.note,
                "params_cap": PARAMS_CAP}


# -- the reader


class Reader:
    """One transcript, read forward across frames"""

    def __init__(self) -> None:
        self.session_id = ""
        self.path: Path | None = None
        self.offset = 0
        #: Bytes the last poll took off disk
        self.bytes_last_read = 0
        self._ident = ""
        self._reset()

    def _reset(self) -> None:
        """Everything accumulated from rows, dropped"""
        self.offset = 0
        self._turn: _Turn | None = None
        self._turns_seen = 0
        self._saw_assistant = False
        self._open: dict[str, _Span] = {}
        self._recent: list[_Span] = []
        self._calls: dict[str, usage_mod.Call] = {}
        self._turn_calls: list[str] = []
        self._last_ts = 0.0
        self._subagents: dict[str, str] = {}

    def bind(self, session_id: str, *, root: Path | None = None,
             index: dict[str, list[Path]] | None = None) -> None:
        """Resolve this session's transcript once, and keep it"""
        if session_id and session_id == self.session_id and self.path is not None:
            return
        self.session_id = session_id
        self._ident = ""
        self._reset()
        self.path = None
        if not session_id:
            return
        if index is None:
            index = usage_mod.index_sessions(root)
        paths = index.get(session_id) or []
        if paths:
            self.path = max(paths, key=lambda p: _mtime(p))
            self._subagents = subagent_labels(self.path)

    # -- polling

    def _poll(self) -> None:
        self.bytes_last_read = 0
        if self.path is None:
            return
        try:
            with self.path.open("rb") as handle:
                st = os.fstat(handle.fileno())
                head = handle.read(_HEAD_BYTES)
                ident = _fingerprint(st, head)
                if ident != self._ident or self.offset > st.st_size:
                    # Rotation, truncation or a compaction rewrite
                    self._reset()
                    self._ident = ident
                handle.seek(self.offset)
                raw = handle.read()
        except OSError:
            return
        self.bytes_last_read = len(raw)
        text = raw.decode("utf-8", errors="replace")
        # A JSONL BEING APPENDED TO IS READ MID-LINE
        cut = text.rfind("\n")
        if cut < 0:
            return
        whole = text[:cut + 1]
        self.offset += len(whole.encode("utf-8"))
        self._consume(whole)
        # Re-read only on a poll that saw new rows: a subagent's meta file appears while the turn runs
        self._subagents = subagent_labels(self.path)

    def _consume(self, text: str) -> None:
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("type") not in usage_mod.REPLAY_ROW_TYPES:
                self._row(row)

    def _row(self, row: dict[str, Any]) -> None:
        ts = usage_mod.parse_stamp(row.get("timestamp"))
        if ts:
            self._last_ts = max(self._last_ts, ts)
        prompt = _prompt_of(row, ts)
        if prompt is not None:
            # The turn boundary is `inspection.read_transcript`'s
            if self._turn is None or self._saw_assistant:
                self._turns_seen += 1
                self._turn = _Turn(seq=self._turns_seen, started=ts)
                self._saw_assistant = False
                self._turn_calls = []
            self._turn.triggers.append(prompt)
            return
        if row.get("type") == "assistant":
            self._saw_assistant = True
            self._call(row)
        for block in usage_mod.blocks_of(row, "tool_use"):
            tool_id = str(block.get("id") or "")
            if not tool_id:
                continue
            self._open[tool_id] = _Span(
                tool=str(block.get("name") or ""), tool_id=tool_id, started=ts,
                detail=detail_of(block.get("input")),
                params=redact_params(block.get("input")))
        for block in usage_mod.blocks_of(row, "tool_result"):
            span = self._open.pop(str(block.get("tool_use_id") or ""), None)
            if span is not None:
                span.ended = ts
                self._recent.insert(0, span)
                del self._recent[RECENT_SPANS:]

    def _call(self, row: dict[str, Any]) -> None:
        """One API call, deduped by message id and kept at its MAXIMUM counts"""
        message = row.get("message") or {}
        counts, mid = message.get("usage"), str(message.get("id") or "")
        if not isinstance(counts, dict) or not mid:
            return
        call = usage_mod.Call(
            ts=usage_mod.parse_stamp(row.get("timestamp")),
            model=str(message.get("model") or ""),
            input=int(counts.get("input_tokens", 0) or 0),
            cache_write=int(counts.get("cache_creation_input_tokens", 0) or 0),
            cache_read=int(counts.get("cache_read_input_tokens", 0) or 0),
            output=int(counts.get("output_tokens", 0) or 0))
        seen = self._calls.get(mid)
        if seen is not None:
            call = usage_mod.Call(
                ts=max(seen.ts, call.ts), model=call.model or seen.model,
                input=max(seen.input, call.input),
                cache_write=max(seen.cache_write, call.cache_write),
                cache_read=max(seen.cache_read, call.cache_read),
                output=max(seen.output, call.output))
        self._calls[mid] = call
        if mid not in self._turn_calls:
            self._turn_calls.append(mid)

    # -- the frame

    def snapshot(self, *, wo_id: str, project: str, turn_in_flight: bool,
                 settled: bool, now: float,
                 holds: Sequence[Hold] = (),
                 write_floor: int = DEFAULT_INSPECT_REPORT_WRITE_FLOOR) -> Live:
        """Read whatever has arrived, then say what it means"""
        self._poll()
        found = self.path is not None
        state = self._state(found, turn_in_flight, settled)
        # SINCE WHEN, and null when there is no "when" (#227)
        stale = (round(max(0.0, now - self._last_ts), 2)
                 if found and self._last_ts else None)
        spans = [s.as_dict(now) for s in self._recent]
        return Live(
            wo_id=wo_id, project=project, session_id=self.session_id, found=found,
            state=state, note=self._note(state, now, stale),
            # ONLY IN `working`
            now=self._open_span().as_dict(now) if state == WORKING else None,
            turn=self._turn_dict(now) if found else None,
            recent=spans,
            tokens=self._tokens() if found else None,
            last_write=self._last_write(write_floor) if found else None,
            stale_seconds=stale,
            subagents=dict(self._subagents),
            holds=[h.as_dict(now) for h in holds])

    def _state(self, found: bool, turn_in_flight: bool, settled: bool) -> str:
        """The five, decided in this order — Neo q678"""
        if not found:
            return NO_TRANSCRIPT
        if settled:
            return SETTLED
        if turn_in_flight and self._open_this_turn():
            return WORKING
        if turn_in_flight:
            return GENERATING
        return IDLE

    def _open_this_turn(self) -> list[_Span]:
        """Open spans started by the CURRENT turn (#227)"""
        if self._turn is None:
            return []
        return [s for s in self._open.values() if s.started >= self._turn.started]

    def _open_span(self) -> _Span:
        """The newest span open IN THIS TURN: an agent asks for one tool at a time"""
        return max(self._open_this_turn(), key=lambda s: s.started)

    def _turn_dict(self, now: float) -> dict[str, Any] | None:
        if self._turn is None:
            return None
        return {"seq": self._turn.seq, "started": self._turn.started,
                "elapsed": round(max(0.0, now - self._turn.started), 2),
                "triggers": [p.as_dict() for p in self._turn.triggers]}

    def _tokens(self) -> dict[str, int]:
        """THE CURRENT TURN's counts, never the session's: `jarvis cost` owns the session and the live view's..."""
        calls = [self._calls[mid] for mid in self._turn_calls if mid in self._calls]
        return {"input": sum(c.input for c in calls),
                "output": sum(c.output for c in calls),
                "cache_write": sum(c.cache_write for c in calls),
                "cache_read": sum(c.cache_read for c in calls),
                "context": calls[-1].context if calls else 0}

    def _last_write(self, write_floor: int) -> dict[str, Any] | None:
        """The newest cache write big enough to classify, in `inspection`'s words"""
        writes = inspection.classify_writes(list(self._calls.values()), write_floor)
        if not writes:
            return None
        last = writes[-1]
        return {"ts": last.ts, "written": last.written, "read": last.read,
                "cause": last.cause, "note": last.note}

    def _note(self, state: str, now: float, stale: float | None) -> str:
        """The frame's sentence, composed once — see `Live.note`"""
        if state == NO_TRANSCRIPT:
            # Spec 2026-09-27 §7: this reader falls back to NO seal
            return (f"no transcript on disk for session {self.session_id or '(none)'} "
                    "— nothing can be read live; `jarvis inspect` can still answer from "
                    "a sealed autopsy if this order has one")
        silence = (f"nothing has been written since {clock(self._last_ts)}"
                   f" ({_secs(stale)} ago)" if self._last_ts
                   else "nothing has been written to this transcript at all")
        if state == WORKING:
            span = self._open_span()
            return (f"{span.tool} has been running for "
                    f"{_secs(now - span.started)}")
        if state == GENERATING:
            # THE BLIND SPOT, in the spec's own words: a row lands when a message completes
            return (f"awaiting the model (request in flight, no block completed); "
                    f"{silence}")
        if state == SETTLED:
            return ("this work order has settled — the last frame, not a live reading; "
                    + silence)
        return f"no turn is in flight — {silence}"


def _secs(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    return f"{seconds / 60:.1f}m" if seconds >= 90 else f"{seconds:.0f}s"


def detail_of(payload: Any, limit: int = QUOTE_CHARS) -> str:
    """A one-line answer to "doing what" for a tool call"""
    if not isinstance(payload, dict):
        return ""
    for key in ("description", "command", "task_id", "file_path", "pattern", "skill"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            # `_scrub` is `redact_params`' own scrub
            flat = " ".join(str(_scrub(value)).split())
            return flat[:limit] + "…" if len(flat) > limit else flat
    return ""


def subagent_labels(path: Path) -> dict[str, str]:
    """Task id -> label, from the `agent-*.meta.json` files beside a transcript"""
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


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _fingerprint(st: os.stat_result, head: bytes) -> str:
    """`uilog._fingerprint`, verbatim in reasoning: the FIRST LINE rather than the first N bytes"""
    first_line = head.split(b"\n", 1)[0]
    return f"{st.st_ino}-{hashlib.sha1(first_line).hexdigest()[:12]}"


def _prompt_of(row: dict[str, Any], ts: float) -> inspection.Prompt | None:
    """The prompt this row is, or None — the same three rules `inspection` reads by"""
    if row.get("type") != "user":
        return None
    source = "sdk" if row.get("promptSource") == "sdk" else "user"
    if source == "user" and (row.get("isMeta")
                             or usage_mod.blocks_of(row, "tool_result")):
        return None
    content = (row.get("message") or {}).get("content")
    text = (content if isinstance(content, str)
            else " ".join(b.get("text", "")
                          for b in usage_mod.blocks_of(row, "text")))
    if not text.strip():
        return None
    flat = " ".join(text.split())
    quote = flat[:QUOTE_CHARS] + "…" if len(flat) > QUOTE_CHARS else flat
    kind = inspection.MESSAGE_TRIGGER
    for needle, named in inspection.TRIGGERS:
        if needle in text:
            kind = named
            break
    return inspection.Prompt(ts=ts, kind=kind, quote=quote, source=source)
