"""What a turn is doing RIGHT NOW — `jarvis watch` and `ops.live_report`.

`inspection` answers "where did this turn's time go" once it is over. This answers the
question asked while it is still running: "this has been going two minutes, what is it
doing?" Same file on disk, opposite regime, and the difference is what makes this a
module of its own rather than a function in `inspection`: §4 of
`.jarvis/features/fo-ff8570fa` rewrites the walk inside `read_transcript`, and two
children editing that walk conflict for no gain.

## Why a byte cursor

`usage.rows` re-parses the whole transcript on every call and `usage.index_sessions`
walks every transcript on disk. Both are right at reconcile cadence — once a tick, over a
file that is finished — and wrong at a two-second refresh, where the re-parse is paid for
again on every frame and the file only ever grows at the end. So a `Reader` resolves the
path ONCE and reads forward from a byte cursor, and the cursor is `uilog.read_errors`'s
exactly: offset plus inode plus a hash of the first line, because a rotated or replaced
file (Claude Code rewrites one on compaction) must restart from zero rather than have the
old offset seek past real rows into the middle of a new one.

## The blind spot, and the words for it

A row lands when a MESSAGE COMPLETES. A model that has been generating for ten minutes
with nothing emitted yet has written nothing at all, so there is no observation of it to
report — and the same silence is what a hung turn looks like. The snapshot therefore says
`state: "generating"`, carries `stale_seconds`, and says in those words that nothing has
been written since a given time. IT NEVER INVENTS ACTIVITY AND NEVER REPORTS A STALE
READING AS A CURRENT ONE: `now` is non-null in exactly one state. Same discipline as
kn-2bba079c / issue #227 — a remainder is not a measurement, and every layer above one
will treat it as one.

## What it is not

No table, no hook, nothing persisted, no acting path, and no alarm: `last_write` reports
`inspection`'s own cause for a cache write and raises nothing, because prefix stability's
authority is `invariants.check_prefix_stable`. No database is opened here either — the
two facts only the OS's record knows (is a turn in flight, has the order settled) are
PASSED IN, which is `inspection.read_session`'s `spans` rule and what keeps this callable
from a test, a `--json` consumer and a web request alike. And no subagent transcript is
opened: §8 owns those.
"""

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

#: Every state a frame can be in, in the order they are decided (Neo q678). Declared as a
#: tuple and pinned by a test because both renderers branch on the string: a sixth state
#: nobody wrote a word for renders as a blank panel, a renamed one as the wrong panel.
STATES = ("working", "generating", "idle", "settled", "no-transcript")
WORKING, GENERATING, IDLE, SETTLED, NO_TRANSCRIPT = STATES

# THE THREE NUMBERS BELOW ARE MODULE CONSTANTS AND NOT CATALOG SETTINGS, which is
# kn-67cdb54b's rule (2) rather than an exception to it: every threshold a surface JUDGES
# BY belongs in the catalog, and none of these is judged by. Each one bounds a RENDERING —
# how many spans fit on a frame, how wide a parameter is painted, how much of a prompt is
# quoted — and nothing is ever compared against them to produce a finding, an alarm or a
# verdict. `inspection`'s floors are settings because a write above one is CALLED a defect.

#: How many finished tool calls a frame carries. A window, not a history: `jarvis inspect`
#: has the whole session, and a terminal frame that scrolls is one the eye cannot read.
RECENT_SPANS = 5

#: The width every tool parameter is rendered at. Tool inputs carry whole file contents
#: and credentials, and this payload reaches a terminal, a dashboard page and a `--json`
#: consumer — so it is capped here, once, rather than by each renderer.
PARAMS_CAP = 200
TRUNCATED = "…[truncated]"
REDACTED = "[redacted]"

#: Same width `jarvis inspect` quotes a prompt at by default, borrowed as a literal.
QUOTE_CHARS = DEFAULT_INSPECT_QUOTE_CHARS

_HEAD_BYTES = 512
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")

#: kn-1791a5e6's one pattern: the userinfo of a URL, which is where a tokenised remote
#: carries its credential — `https://x-access-token:ghp_…@github.com/acme/p.git`, the
#: exact string a worker's `git push` puts in a tool input. Matched by SHAPE and not by
#: any word, because the token is a random string and a denylist of names only catches the
#: leak somebody already thought of.
_URL_USERINFO_RE = re.compile(r"://[^/\s@]+@")
_URL_USERINFO_SAFE = "://<redacted>@"

#: Words that make a NAME a credential's name, matched against the identifier's parts:
#: `monkey` is not a `key` and `AWS_SECRET_ACCESS_KEY` is. COPIED from
#: `autoreview.SECRET_NAME_PARTS` rather than imported, which is kn-097c40ef's rule for a
#: redacting leaf: `autoreview` calls models, and importing it to read a transcript would
#: put a model-calling module on `jarvis watch`'s import path. Price of the copy is that a
#: word added there must be added here; price of the import is a live frame that pays for
#: the review pipeline.
SECRET_NAME_PARTS = frozenset({
    "key", "keys", "apikey", "accesskey", "privatekey", "secretkey", "seckey",
    "token", "tokens", "authtoken", "secret", "secrets", "clientsecret",
    "password", "passwd", "passphrase", "pwd", "credential", "credentials",
})

#: SHAPES, because a NAME test only catches the credential somebody already thought of
#: and round 1 of this order shipped with only names plus URL userinfo: `GH_TOKEN=ghp_…
#: git push` has an innocent key, no userinfo, and was published verbatim in BOTH
#: `now.params` and `now.detail`. Copied from `inspection.PARAM_SECRET_SHAPES` for
#: SECRET_NAME_PARTS' reason and one more: that symbol lives on an unmerged sibling
#: branch, so there is nothing to import yet. Each entry is (pattern, what the marker
#: CALLS it) — the marker NAMES the shape and never quotes the match, because a frame
#: that echoes the credential has only moved it one file along (kn-deef42ea).
SECRET_SHAPES: tuple[tuple[str, str], ...] = (
    # The whole BLOCK and not the BEGIN line, under DOTALL: substituting the header alone
    # leaves the base64 body sitting in the payload underneath the marker.
    (r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----.*?"
     r"(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)", "a private key block"),
    (r"\bssh-(?:rsa|dss|ed25519)\s+AAAA[0-9A-Za-z+/=]+", "an ssh key line"),
    (r"\bAuthorization[\"\']?\s*:\s*[\"\']?(?:Bearer|Basic|Token)\s+\S+",
     "an Authorization header value"),
    # THE TWO `inspection`'S SET DOES NOT HAVE, and the reason the round-1 review failed:
    # an issued credential names itself in its first characters, so it is recognisable
    # with no name and no assignment around it — `gh auth login --with-token ghp_…` is
    # neither. Kept as an explicit prefix list rather than an entropy test: a bare
    # high-entropy string is every sha in every `git` command a worker runs.
    (r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{6,}", "a GitHub token"),
    (r"\bsk-[A-Za-z0-9_-]{6,}", "an API key"),
)

#: `NAME=value` ANYWHERE IN THE STRING, and that is a DELIBERATE DIVERGENCE from
#: `inspection._PARAM_ASSIGNMENT_RE`, which is anchored `^…$` per line. `inspection` reads
#: file contents, where an assignment owns its line; this reads a Bash `command`, which is
#: ONE line with the assignment as an env prefix — `GH_TOKEN=ghp_abc123 git push origin
#: HEAD` slips past the anchored pattern entirely. So: a word boundary after the start of
#: the string, whitespace, or a shell separator, and the value runs to the next
#: whitespace. THE FLAG FORM IS THE SAME LEAK: on a command line a credential arrives as
#: an env prefix (`GH_TOKEN=…`) or as a flag (`deploy --password=…`, `-p=…`, `--api-key=…`)
#: and nothing distinguishes them, so an optional leading `-`/`--` is part of the head. The
#: name still has to follow a shell separator — that is what makes the left side a name
#: rather than the tail of some other token — and the two-part test still decides, so
#: `--key=none` comes through untouched.
_ASSIGNMENT_RE = re.compile(
    r"(?P<head>(?:\A|(?<=[\s;&|(]))(?:(?:export|env|set)\s+)?-{0,2}"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)=)(?P<value>[^\s]+)")

#: A VALUE THAT IS NOT A SECRET, however secret its name — the negative control, and the
#: second half of `autoreview`'s two-part test (kn-097c40ef: bring it or every placeholder
#: is redacted). Most `key=` in any command carries an empty default or a placeholder, and
#: redacting those makes the frame useless for the one thing it is for: reading what the
#: worker actually ran.
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
    """A wall-clock time a person can compare against their own — local, to the second.

    Local rather than UTC for `notifications.log`'s reason: this string is read by a human
    watching a terminal, beside a clock on the same machine.
    """
    return time.strftime("%H:%M:%S", time.localtime(ts))


# -- redaction ------------------------------------------------------------------------


def redact_params(payload: Any, cap: int = PARAMS_CAP) -> dict[str, str]:
    """A tool call's input, safe and bounded — the one path every parameter takes.

    Three jobs, and none is cosmetic.

    A value whose KEY names a secret is REPLACED rather than shortened. The key survives,
    so a reader still sees what the tool was called with.

    A value's SHAPE is redacted wherever it sits, which is the half the key test and the
    userinfo test both miss: `GH_TOKEN=ghp_… git push` and `curl -H 'Authorization:
    Bearer sk-…'` have innocent keys and no URL to strip.

    A URL's USERINFO is stripped from every string at every depth, and this is the half a
    key test cannot do: `{"command": "git push https://x-access-token:ghp_…@github.com/
    acme/p.git"}` has an innocent key, and the credential sits at the FRONT of the value
    where no cap can reach it. kn-1791a5e6, whose rule is scrub BEFORE you slice — a
    truncated leak is a leak.

    Everything left is rendered to one string and cut at `cap` with a visible marker that
    says only that something was cut; what was cut is the thing we are declining to
    publish.
    """
    if not isinstance(payload, dict):
        return {}
    def one(key: str, value: Any) -> str:
        return REDACTED if _names_secret(key) else _render(_scrub(value), cap)

    return {str(key): one(str(key), value) for key, value in payload.items()}


def _names_secret(identifier: str) -> bool:
    parts = [p.lower() for p in _WORD_SPLIT_RE.split(identifier) if p]
    return any(p in SECRET_NAME_PARTS for p in parts)


def _scrub(value: Any) -> Any:
    """Both tests applied at EVERY depth — a leak nests by construction.

    `{"env": {"GH_TOKEN": …}}` is the shape a `Bash` call carries a named secret in, and a
    tokenised remote turns up inside a list of edits as readily as at the top level. Run
    before anything is rendered or cut, so the cap can never preserve a credential by
    keeping the front of a string (kn-1791a5e6).
    """
    if isinstance(value, dict):
        return {k: REDACTED if _names_secret(str(k)) else _scrub(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def _scrub_text(text: str) -> str:
    """One string, with every secret-shaped part of it NAMED rather than quoted. Pure.

    THE ONE FUNCTION `redact_params` AND `detail_of` SHARE (kn-637a7236: one tool input
    reaches the payload several times, and scrubbing only `params` leaves the leak in
    `detail`). Order matters once: the URL's userinfo goes first, so a tokenised remote
    reads as `https://<redacted>@github.com/…` — a recognisable URL — instead of having
    its token named by the GitHub shape inside otherwise intact userinfo.
    """
    if not text:
        return text
    text = _URL_USERINFO_RE.sub(_URL_USERINFO_SAFE, text)
    for pattern, name in _SHAPE_RE:
        text = pattern.sub(f"<redacted: {name}>", text)
    return _ASSIGNMENT_RE.sub(_assignment_marker, text)


def _assignment_marker(m: re.Match[str]) -> str:
    """The two-part test, `autoreview._line_marker`'s: the name must NAME a credential AND
    the value must LOOK like one. Either half alone is useless — the name alone redacts
    `key=none`, the value alone redacts every sha."""
    if _names_secret(m.group("name")) and _looks_like_a_credential(m.group("value")):
        return m.group("head") + CREDENTIAL_VALUE
    return m.group(0)


def _looks_like_a_credential(raw: str) -> bool:
    """`autoreview._secret_value`'s test, same reasoning: an expression is not a value."""
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
    """One string per parameter, capped — which is also what bounds a nested structure.

    A dict of a thousand keys costs as much to paint as a string of a million characters,
    and a tool input can be either, so the container is serialised and then cut by the
    same rule rather than walked to some second depth limit.
    """
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    # Again, on the RENDERED text and still before the cut (kn-1791a5e6: scrub before you
    # slice): `default=str` can print an object whose repr holds a credential, and no leaf
    # test saw that string.
    text = _scrub_text(text)
    if len(text) <= cap:
        return text
    return text[:max(0, cap - len(TRUNCATED))] + TRUNCATED


# -- the frame ------------------------------------------------------------------------


@dataclass
class _Span:
    """One tool call in flight or just finished. `inspection.ToolSpan` plus its params.

    A separate type rather than that one extended: this carries the REDACTED input, which
    `inspection` has no business publishing through `jarvis inspect --json`, and `ended`
    of 0.0 here means "still open right now" rather than "was killed" — the opposite
    reading, and the one the record's `turn_in_flight` decides between.
    """

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
    """One frame: the answer to "what is it doing right now", and what it will not claim.

    `as_dict` is the contract §3 wrote down, and both renderers consume it without
    reshaping anything. Two keys are additions to that list and each is here for a
    reason a renderer cannot supply:

    `note` — the sentence a frame needs where the in-flight tool would be. Composed here
    because a renderer that derives a number or a phrase is one the OTHER renderer will
    disagree with (PR 65), and "nothing has been written since 14:03:11" written twice is
    two wordings of one fact.

    `params_cap` — the operating rule for anything Jarvis did not write: the cap is
    STATED in the payload. Without it a reader cannot tell a short command from a
    truncated one, and neither can a test.
    """

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


# -- the reader -----------------------------------------------------------------------


class Reader:
    """One transcript, read forward across frames. Created UNBOUND.

    Unbound on purpose (Neo q678 (1)): the CLI holds one of these across a repaint loop
    and must not have to know the session id to build it — and holding it is the whole
    point, because everything expensive here (resolving the path, parsing the rows
    already read) happens once and is then carried.
    """

    def __init__(self) -> None:
        self.session_id = ""
        self.path: Path | None = None
        self.offset = 0
        #: Bytes the last poll took off disk. Zero on a frame where nothing arrived,
        #: which is the observable that says the read was incremental at all.
        self.bytes_last_read = 0
        self._ident = ""
        self._reset()

    def _reset(self) -> None:
        """Everything accumulated from rows, dropped. Called on a rebind and whenever the
        file's identity changes — nothing carried over describes the new file."""
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
        """Resolve this session's transcript once, and keep it.

        A no-op when already bound to the same session, which is what makes the repaint
        loop cheap: `usage.index_sessions` walks every transcript on disk and is the one
        cost a two-second refresh cannot pay per frame.

        NEWEST MTIME WINS when a session has several files. A session resumed under a
        second cwd leaves one file per slug (`usage.index_sessions`), and `inspection`
        reads them all because it wants the whole history — but a LIVE turn is being
        appended to exactly one of them, and that one is the newest.
        """
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

    # -- polling ---------------------------------------------------------------

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
                    # Rotation, truncation or a compaction rewrite. Restart from zero
                    # rather than seek to an offset that now points into the middle of
                    # somebody else's row (`uilog._fingerprint`).
                    self._reset()
                    self._ident = ident
                handle.seek(self.offset)
                raw = handle.read()
        except OSError:
            return
        self.bytes_last_read = len(raw)
        text = raw.decode("utf-8", errors="replace")
        # A JSONL BEING APPENDED TO IS READ MID-LINE. The last line can be half written,
        # and it is the newest row — the one the user opened `jarvis watch` to see. So
        # only whole lines are parsed and the cursor stops at the last newline; the
        # partial bytes are simply read again next poll. Keeping them in memory BESIDE an
        # unadvanced cursor would count them twice, which is the same row rendered twice.
        cut = text.rfind("\n")
        if cut < 0:
            return
        whole = text[:cut + 1]
        self.offset += len(whole.encode("utf-8"))
        self._consume(whole)
        # Re-read only on a poll that saw new rows: a subagent's meta file appears while
        # the turn runs, and this is a glob of a small directory, never a transcript.
        self._subagents = subagent_labels(self.path)

    def _consume(self, text: str) -> None:
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                self._row(row)

    def _row(self, row: dict[str, Any]) -> None:
        ts = usage_mod.parse_stamp(row.get("timestamp"))
        if ts:
            self._last_ts = max(self._last_ts, ts)
        prompt = _prompt_of(row, ts)
        if prompt is not None:
            # The turn boundary is `inspection.read_transcript`'s, unchanged: a prompt
            # starts a new turn only if the model has spoken since the last one started,
            # because `Daemon.deliver_messages` coalesces everything queued for a work
            # order into ONE turn with several triggers.
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
        """One API call, deduped by message id and kept at its MAXIMUM counts.

        `usage._assistant_messages`' trap, and it bites harder here: a message is written
        to the transcript once per content block as it grows, every copy repeating the
        same input counts with a climbing `output_tokens`. Summing the rows counts the
        input two or three times over — and a live reader sees every one of those copies
        arrive, one frame apart.
        """
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

    # -- the frame -------------------------------------------------------------

    def snapshot(self, *, wo_id: str, project: str, turn_in_flight: bool,
                 settled: bool, now: float,
                 holds: Sequence[Hold] = (),
                 write_floor: int = DEFAULT_INSPECT_REPORT_WRITE_FLOOR) -> Live:
        """Read whatever has arrived, then say what it means.

        `turn_in_flight` and `settled` come from the caller because this module never
        opens the OS's database (`inspection.read_session`'s `spans` rule). They are not
        decoration: the transcript alone cannot tell a tool that is running from one
        whose turn was killed mid-call, and that is the difference between `working` and
        `idle`.
        """
        self._poll()
        found = self.path is not None
        state = self._state(found, turn_in_flight, settled)
        # SINCE WHEN, and null when there is no "when". A transcript with no parsable row
        # yet — a file holding one half-written line — has `_last_ts` of 0.0, and
        # `now - 0` is 1.7 billion seconds of silence, a number every layer above would
        # render as an eternally stalled turn. The note already says the right thing
        # without it (issue #227: absent is not a reading).
        stale = (round(max(0.0, now - self._last_ts), 2)
                 if found and self._last_ts else None)
        spans = [s.as_dict(now) for s in self._recent]
        return Live(
            wo_id=wo_id, project=project, session_id=self.session_id, found=found,
            state=state, note=self._note(state, now, stale),
            # ONLY IN `working`. In every other state the open span is a stale reading,
            # and publishing it as `now` is exactly what §3 forbids.
            now=self._open_span().as_dict(now) if state == WORKING else None,
            turn=self._turn_dict(now) if found else None,
            recent=spans,
            tokens=self._tokens() if found else None,
            last_write=self._last_write(write_floor) if found else None,
            stale_seconds=stale,
            subagents=dict(self._subagents),
            holds=[h.as_dict(now) for h in holds])

    def _state(self, found: bool, turn_in_flight: bool, settled: bool) -> str:
        """The five, decided in this order — Neo q678.

        `no-transcript` first because nothing else can be read without one, and `settled`
        before `working` because a settled order's open span is history however the record
        reads. `working` needs BOTH halves: a `tool_use` with no `tool_result` AND a turn
        the record says is running. An unfinished span alone is not evidence — a crashed
        turn leaves one open for ever.
        """
        if not found:
            return NO_TRANSCRIPT
        if settled:
            return SETTLED
        if turn_in_flight and self._open:
            return WORKING
        if turn_in_flight:
            return GENERATING
        return IDLE

    def _open_span(self) -> _Span:
        """The newest open span: an agent asks for one tool at a time, and if the
        transcript shows two, the later one is what it is waiting on."""
        return max(self._open.values(), key=lambda s: s.started)

    def _turn_dict(self, now: float) -> dict[str, Any] | None:
        if self._turn is None:
            return None
        return {"seq": self._turn.seq, "started": self._turn.started,
                "elapsed": round(max(0.0, now - self._turn.started), 2),
                "triggers": [p.as_dict() for p in self._turn.triggers]}

    def _tokens(self) -> dict[str, int]:
        """THE CURRENT TURN's counts, never the session's: `jarvis cost` owns the session
        and the live view's subject is the turn. `context` is the LATEST call's context —
        what one call was asked to read, which is the number that says whether the
        conversation has grown too big (`usage.Call.context`), and not a sum of it.
        """
        calls = [self._calls[mid] for mid in self._turn_calls if mid in self._calls]
        return {"input": sum(c.input for c in calls),
                "output": sum(c.output for c in calls),
                "cache_write": sum(c.cache_write for c in calls),
                "cache_read": sum(c.cache_read for c in calls),
                "context": calls[-1].context if calls else 0}

    def _last_write(self, write_floor: int) -> dict[str, Any] | None:
        """The newest cache write big enough to classify, in `inspection`'s words.

        REPORTS, RAISES NOTHING. The causes and their sentences are
        `inspection.WRITE_CAUSE_NOTES` because a second vocabulary for one fact is the
        drift this repository has shipped before, and the authority on whether a
        `prefix-miss` is a defect worth telling anyone about is
        `invariants.check_prefix_stable`, which this defers to.
        """
        writes = inspection.classify_writes(list(self._calls.values()), write_floor)
        if not writes:
            return None
        last = writes[-1]
        return {"ts": last.ts, "written": last.written, "read": last.read,
                "cause": last.cause, "note": last.note}

    def _note(self, state: str, now: float, stale: float | None) -> str:
        """The frame's sentence, composed once — see `Live.note`."""
        if state == NO_TRANSCRIPT:
            return (f"no transcript on disk for session {self.session_id or '(none)'} "
                    "— nothing can be read")
        silence = (f"nothing has been written since {clock(self._last_ts)}"
                   f" ({_secs(stale)} ago)" if self._last_ts
                   else "nothing has been written to this transcript at all")
        if state == WORKING:
            span = self._open_span()
            return (f"{span.tool} has been running for "
                    f"{_secs(now - span.started)}")
        if state == GENERATING:
            # THE BLIND SPOT, in the spec's own words: a row lands when a message
            # completes, so a model mid-answer is invisible and the only honest report is
            # the silence and its length.
            return f"a turn is in flight and {silence}"
        if state == SETTLED:
            return ("this work order has settled — the last frame, not a live reading; "
                    + silence)
        return f"no turn is in flight — {silence}"


def _secs(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    return f"{seconds / 60:.1f}m" if seconds >= 90 else f"{seconds:.0f}s"


def detail_of(payload: Any, limit: int = QUOTE_CHARS) -> str:
    """A one-line answer to "doing what" for a tool call.

    MIRRORS `inspection._detail_of` — same key order, copied and not imported. §4 of the
    feature is rewriting the walk inside `inspection.read_transcript`, and `_detail_of` is
    PRIVATE there: importing it would bind a live frame to a name that can move or change
    shape mid-feature. Reconcile the two when §4 lands.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("description", "command", "task_id", "file_path", "pattern", "skill"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            # `_scrub` is `redact_params`' own scrub, not a second copy of it: `detail`
            # and `params` are the same sanitised path, because a URL's userinfo sits at
            # the front of the string and a second, unscrubbed path is a leak waiting for
            # its first caller (kn-1791a5e6 — scrub before you slice).
            flat = " ".join(str(_scrub(value)).split())
            return flat[:limit] + "…" if len(flat) > limit else flat
    return ""


def subagent_labels(path: Path) -> dict[str, str]:
    """Task id -> label, from the `agent-*.meta.json` files beside a transcript.

    The task id IS the subagent transcript's stem minus its `agent-` prefix, which is the
    only join between "what the lead agent is waiting on" and "what that thing is". The
    MIRRORS `inspection._subagent_labels`, copied here for `detail_of`'s reason: it is
    private on a file §4 is rewriting, so reconcile the two when §4 lands — and no subagent
    TRANSCRIPT is opened: §8 owns those, and one read per frame is the cost this module
    exists to avoid.
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


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _fingerprint(st: os.stat_result, head: bytes) -> str:
    """`uilog._fingerprint`, verbatim in reasoning: the FIRST LINE rather than the first
    N bytes, because appending changes how much of the file a head read returns and every
    append would otherwise look like a new file."""
    first_line = head.split(b"\n", 1)[0]
    return f"{st.st_ino}-{hashlib.sha1(first_line).hexdigest()[:12]}"


def _prompt_of(row: dict[str, Any], ts: float) -> inspection.Prompt | None:
    """The prompt this row is, or None — the same three rules `inspection` reads by.

    THE VOCABULARY IS SHARED AND THE WALK IS NOT: `inspection.TRIGGERS` names the kinds
    so a trigger cannot be called one thing on a live frame and another in a report, but
    the walk MIRRORS `inspection._prompt_of` rather than calling it, because §4 rewrites
    that one and it is private. Reconcile the two when §4 lands.

    Two `user` rows are not prompts and both would cut a turn in half at the worst
    moment: a tool result is the agent's own loop, and an `isMeta` row is Claude Code
    talking to itself while a skill loads.
    """
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
