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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import usage as usage_mod
from .catalog import (DEFAULT_INSPECT_REPORT_JOIN_FLOOR,
                      DEFAULT_INSPECT_REPORT_WRITE_FLOOR, InspectConfig)

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

COLD_START, TTL_EXPIRY, PREFIX_MISS = "cold-start", "ttl-expiry", "prefix-miss"

#: The buckets a wall clock divides into, in the order they are rendered. Walked rather
#: than spelled out at each site, so a bucket cannot exist in one renderer and not another
#: — `cli.PART_LABELS` is keyed by these and `tests/test_inspection.py` pins the two equal,
#: because a fifth bucket missing from one renderer is a table that no longer sums to 100.
PARTS = ("generating", "blocked", "tools", "idle", "unaccounted")

WRITE_CAUSE_NOTES = {
    COLD_START: "the first call of the session — unavoidable",
    TTL_EXPIRY: "the cache had expired: nothing was called for longer than the TTL",
    PREFIX_MISS: "the prefix was re-written while it was still warm — a defect",
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
                "finished": self.finished}


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

    @property
    def wall(self) -> float:
        return max(0.0, self.ended - self.started)

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
            "wall": round(self.wall, 2), "generating": round(self.generating, 2),
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
            "usage": self.usage.as_dict(),
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
    subagents: dict[str, str] = field(default_factory=dict)

    @property
    def spans(self) -> list[ToolSpan]:
        return [s for turn in self.turns for s in turn.spans]

    @property
    def wall(self) -> float:
        return sum(t.wall for t in self.turns)

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
        """The whole session's clock, summed over its turns."""
        return {"wall": self.wall,
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
            "partition": {k: round(v, 2) for k, v in part.items()},
            "share": {k: round(part[k] / wall, 4) for k in PARTS},
            "context_peak": max((t.context_peak for t in self.turns), default=0),
            "rewrite_excess": self.rewrite_excess(),
            "cache_ttl": self.cache_ttl(),
            "turns": [t.as_dict() for t in self.turns],
            "writes": [w.as_dict() for w in self.writes],
            "joins": [s.as_dict() for s in self.joins()],
            "tools": self.tool_profile(),
        }


# -- reading a transcript --------------------------------------------------------------


def _detail_of(payload: Any, limit: int) -> str:
    """A one-line answer to "doing what" for a tool call.

    `description` first wherever it exists, because it is the agent's own words for what
    it was doing and every long-running tool in the fleet carries one.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("description", "command", "task_id", "file_path", "pattern", "skill"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _first_line(value, limit)
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
            open_turn.triggers.append(prompt)
            continue
        if row.get("type") == "assistant":
            saw_assistant = True
        for block in usage_mod.blocks_of(row, "tool_use"):
            tool_id = str(block.get("id") or "")
            if not tool_id:
                continue
            span = ToolSpan(name=str(block.get("name") or ""), tool_id=tool_id,
                            started=ts,
                            detail=_detail_of(block.get("input"),
                                              cfg.quote_chars))
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


def classify_writes(calls: Sequence[usage_mod.Call], floor: int) -> list[Write]:
    """Every cache write at or over `floor`, labelled with what caused it.

    The gap is measured to the PREVIOUS API call in the session whatever its size, and
    compared against the TTL that call bought — a 1-hour write (every Jarvis call before
    kn-5dd784f5) survives a gap that would expire a 5-minute one, so testing both
    against 300 seconds would call an honest expiry a defect.
    """
    writes: list[Write] = []
    previous: usage_mod.Call | None = None
    for call in calls:
        if call.cache_write >= floor:
            ttl = TTL_1H if previous is not None and previous.cache_1h else TTL_5M
            gap = call.ts - previous.ts if previous is not None else 0.0
            if previous is None:
                cause = COLD_START
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
                 index: dict[str, list[Path]] | None = None) -> Anatomy:
    """Take one session apart, across every segment file it left behind.

    Segments are read in path order and their turns concatenated by start time, then
    renumbered — a session resumed under a second cwd has a file under each slug
    (`usage.index_sessions`), and numbering per file would produce two turn 1s.
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
        anatomy.subagents.update(labels)
    turns.sort(key=lambda t: t.started)
    for seq, turn in enumerate(turns, start=1):
        turn.seq = seq
    anatomy.turns = turns

    calls = usage_mod.session_calls(session_id, index=index)
    anatomy.writes = classify_writes(calls, cfg.report_write_floor)
    _attach_calls(turns, calls)
    _close_turns(turns)
    _name_joins(turns, anatomy.subagents)
    return anatomy


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
    """
    if not anatomy.found or not anatomy.turns or not cfg.enabled:
        return []
    turn = anatomy.turns[-1]
    if turn.started < dispatched:
        return []
    wall = max(turn.wall, (now - turn.started) if now else 0.0)
    raised: list[Alarm] = []
    hint = f" — `jarvis inspect {wo_id}`" if wo_id else ""

    # THE TWO DURATION ALARMS ARE EXCLUSIVE, and which one applies is decided by the
    # cost record rather than by the clock. A turn that has made no API call has bought
    # nothing, so `long-turn`'s claim — that it is still being billed — would be false;
    # the finding is the silence itself, and it is raised sooner.
    if not turn.observed:
        if wall >= cfg.alarm_stalled_minutes * 60:
            raised.append(Alarm(STALL_ALARM, (
                f"this turn has been open {int(wall // 60)} minutes and has made no API "
                f"call at all — the work never started, and nothing has been spent on "
                f"it{hint}")))
    elif wall >= cfg.alarm_turn_minutes * 60:
        raised.append(Alarm(TURN_ALARM, (
            f"this turn has been running {int(wall // 60)} minutes and is still being "
            f"billed ({_spend_so_far(turn, now)}){hint}")))
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
        if write.ts >= turn.started and write.written >= cfg.alarm_write_tokens \
                and write.cause != COLD_START:
            raised.append(Alarm(WRITE_ALARM, (
                f"re-sent {write.written:,} cached tokens in one call ({write.cause}) "
                f"— the conversation is being paid for again{hint}")))
            break
    return raised


def live_alarms(session_id: str, cfg: InspectConfig, *, wo_id: str = "",
                now: float | None = None, dispatched: float,
                root: Path | None = None,
                index: dict[str, list[Path]] | None = None) -> list[Alarm]:
    """`alarms` for a session id — one transcript read, no paid call, nothing written.

    The session is read at the ALARM's write threshold rather than the report's: the only
    writes this needs to see are the ones big enough to raise one, and classifying the
    small ones would be work whose answer is thrown away.
    """
    reading = replace(cfg, report_write_floor=cfg.alarm_write_tokens)
    anatomy = read_session(session_id, reading, root=root, index=index)
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
