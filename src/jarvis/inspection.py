"""Where a session's TIME went — the other half of `jarvis cost`.

`usage` reads a transcript for TOKENS and answers "what did this cost". The same file
also carries a clock: every row is timestamped, so a turn's wall clock can be split into
the three things an agent does with it. That split is what this module produces, and it
is the question `jarvis cost` cannot answer — "how is a design-only agent taking
14-minute turns?" took a hand-written script over the raw JSONL, and found two defects
that cost money on every work order in the fleet.

Design and worked example: `docs/superpowers/specs/2026-08-30-the-anatomy-of-a-turn.md`.

## The partition

The method is `docs/findings/anatomy-of-an-expensive-turn.md` §1 step 4, with one
bucket added:

    executing tools   `tool_use` timestamp to the matching `tool_result` timestamp
    blocked           the subset of those where the tool is a BLOCKING JOIN
    idle              after the turn's last API call, before the next turn's prompt
    generating        the wall clock left over

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
#: than spelled out at each site, so a bucket cannot exist in one renderer and not another.
PARTS = ("generating", "blocked", "tools", "idle")

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
    def generating(self) -> float:
        """The wall clock nothing else accounts for.

        Clamped at zero rather than allowed to go negative: tool spans are read from a
        file Jarvis does not write, and a clock skew or an overlapping pair of spans
        must not produce a partition that reads as nonsense.
        """
        return max(0.0, self.wall - self.blocked - self.tools - self.idle)

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
        return {"generating": self.generating / self.wall,
                "blocked": self.blocked / self.wall,
                "tools": self.tools / self.wall,
                "idle": self.idle / self.wall}

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "started": self.started, "ended": self.ended,
            "wall": round(self.wall, 2), "generating": round(self.generating, 2),
            "blocked": round(self.blocked, 2), "tools": round(self.tools, 2),
            "idle": round(self.idle, 2),
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

#: What each kind IS, for a surface listing alarms rather than raising one. An `Alarm`'s
#: own `reason` is about one turn and carries its numbers; this is the standing meaning,
#: and it lives beside the constants so a dashboard and the CLI cannot drift on it.
ALARM_KINDS = {
    TURN_ALARM: "a turn still generating, still being billed",
    JOIN_ALARM: "a join open past the cache TTL — the wait is paid for twice",
    WRITE_ALARM: "the conversation sent again, at the cache-write rate",
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


def alarms(anatomy: Anatomy, cfg: InspectConfig, wo_id: str = "",
           now: float | None = None) -> list[Alarm]:
    """What is wrong with the turn that is running, most actionable first.

    Judges THE LAST TURN ONLY. Everything before it is already paid for and belongs on
    `jarvis inspect`, not in front of the user: an alarm the user can do nothing about
    is the noise that gets a cost alarm ignored, and then it is worse than nothing.

    `now` is what makes this a LIVE reading rather than a historical one. A turn that is
    generating has written no row for minutes, so its clock has to be measured against
    the wall and not against its own last line — measuring it against the transcript
    would report an hour-long turn as however long ago it last spoke.
    """
    if not anatomy.found or not anatomy.turns or not cfg.enabled:
        return []
    turn = anatomy.turns[-1]
    wall = max(turn.wall, (now - turn.started) if now else 0.0)
    raised: list[Alarm] = []
    hint = f" — `jarvis inspect {wo_id}`" if wo_id else ""

    if wall >= cfg.alarm_turn_minutes * 60:
        raised.append(Alarm(TURN_ALARM, (
            f"this turn has been running {int(wall // 60)} minutes and is still being "
            f"billed{hint}")))
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
                now: float | None = None, root: Path | None = None,
                index: dict[str, list[Path]] | None = None) -> list[Alarm]:
    """`alarms` for a session id — one transcript read, no paid call, nothing written.

    The session is read at the ALARM's write threshold rather than the report's: the only
    writes this needs to see are the ones big enough to raise one, and classifying the
    small ones would be work whose answer is thrown away.
    """
    reading = replace(cfg, report_write_floor=cfg.alarm_write_tokens)
    anatomy = read_session(session_id, reading, root=root, index=index)
    return alarms(anatomy, cfg, wo_id=wo_id, now=now)
