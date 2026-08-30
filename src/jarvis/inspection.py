"""Where a session's TIME went — the other half of `jarvis cost`.

`usage` reads a transcript for TOKENS and answers "what did this cost". The same file
also carries a clock: every row is timestamped, so a turn's wall clock can be split into
the three things an agent does with it. That split is what this module produces, and it
is the question `jarvis cost` cannot answer — "how is a design-only agent taking
14-minute turns?" took a hand-written script over the raw JSONL, and found two defects
that cost money on every work order in the fleet.

Design and worked example: `docs/superpowers/specs/2026-08-30-the-anatomy-of-a-turn.md`.

## The partition, and why it is only three parts

    executing tools   `tool_use` timestamp to the matching `tool_result` timestamp
    blocked           the subset of those where the tool is a BLOCKING JOIN
    generating        the wall clock left over

Blocked is carved out of tool time rather than added beside it because the two are
opposite facts about the same seconds: 45 seconds of `Bash` is work being done, and 450
seconds of `TaskOutput` is the lead agent asleep with no API call in flight — and long
enough to lose its prompt cache, which is why the same 450 seconds shows up again below
as a `ttl-expiry` write. Everything not inside a tool span is charged to the model,
including the gaps between spans; there is nothing else it can be.

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
be a re-send of the conversation is classified at all, which is why `write_floor` has a
conservative default and why every report states it.

## No paid call, and nothing persisted

Everything here is arithmetic over files Claude Code already wrote. Like `usage`, this
derives a fact about a work order from a file Jarvis does not own and cannot repair, so
there is nothing to store and nothing to reconcile — and a transcript that has expired
is reported as absent rather than guessed at.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import usage as usage_mod

#: Cache TTLs in seconds. The 5-minute one is what every Jarvis call buys since
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

#: What a re-write has to weigh before it is classified. Below this a write is the
#: conversation's own growth, not a re-send of it; see the module docstring.
DEFAULT_WRITE_FLOOR = 20_000

#: What a blocking join has to last before it is worth a line of its own.
DEFAULT_JOIN_FLOOR = 30.0

COLD_START, TTL_EXPIRY, PREFIX_MISS = "cold-start", "ttl-expiry", "prefix-miss"

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


def _first_line(text: str, limit: int = 140) -> str:
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
    def generating(self) -> float:
        """The wall clock nothing else accounts for.

        Clamped at zero rather than allowed to go negative: tool spans are read from a
        file Jarvis does not write, and a clock skew or an overlapping pair of spans
        must not produce a partition that reads as nonsense.
        """
        return max(0.0, self.wall - self.blocked - self.tools)

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
            return {"generating": 0.0, "blocked": 0.0, "tools": 0.0}
        return {"generating": self.generating / self.wall,
                "blocked": self.blocked / self.wall,
                "tools": self.tools / self.wall}

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "started": self.started, "ended": self.ended,
            "wall": round(self.wall, 2), "generating": round(self.generating, 2),
            "blocked": round(self.blocked, 2), "tools": round(self.tools, 2),
            "share": {k: round(v, 4) for k, v in self.share().items()},
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
    write_floor: int = DEFAULT_WRITE_FLOOR
    #: Task id -> what it was, from the subagent `.meta.json` Claude Code writes beside
    #: the transcript. This is what turns "blocked 450s on a7b62083" into a sentence.
    subagents: dict[str, str] = field(default_factory=dict)

    @property
    def spans(self) -> list[ToolSpan]:
        return [s for turn in self.turns for s in turn.spans]

    @property
    def wall(self) -> float:
        return sum(t.wall for t in self.turns)

    def joins(self, over: float = DEFAULT_JOIN_FLOOR) -> list[ToolSpan]:
        return sorted((s for s in self.spans if s.is_join and s.seconds >= over),
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
                "generating": sum(t.generating for t in self.turns),
                "blocked": sum(t.blocked for t in self.turns),
                "tools": sum(t.tools for t in self.turns)}

    def as_dict(self, join_floor: float = DEFAULT_JOIN_FLOOR) -> dict[str, Any]:
        part = self.partition()
        wall = part["wall"] or 1.0
        return {
            "session_id": self.session_id,
            "found": self.found,
            "write_floor": self.write_floor,
            "join_floor": join_floor,
            "partition": {k: round(v, 2) for k, v in part.items()},
            "share": {k: round(part[k] / wall, 4)
                      for k in ("generating", "blocked", "tools")},
            "turns": [t.as_dict() for t in self.turns],
            "writes": [w.as_dict() for w in self.writes],
            "joins": [s.as_dict() for s in self.joins(join_floor)],
            "tools": self.tool_profile(),
        }


# -- reading a transcript --------------------------------------------------------------


def _detail_of(payload: Any) -> str:
    """A one-line answer to "doing what" for a tool call.

    `description` first wherever it exists, because it is the agent's own words for what
    it was doing and every long-running tool in the fleet carries one.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("description", "command", "task_id", "file_path", "pattern", "skill"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _first_line(value)
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


def _prompt_of(row: dict[str, Any], ts: float) -> Prompt | None:
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
    return Prompt(ts=ts, kind=_trigger_kind(text), quote=_first_line(text),
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


def read_transcript(path: Path | str) -> tuple[list[Turn], dict[str, str]]:
    """One transcript file, cut into turns with their tool spans attached.

    A single ordered walk, because the three things it collects are interleaved and
    every one of them is defined by its position relative to the others: a turn starts
    at an injected prompt, but only at one with an assistant message since the last turn
    started — otherwise the two prompts Jarvis coalesces into one turn would read as two.
    """
    turns: list[Turn] = []
    pending: dict[str, ToolSpan] = {}
    open_turn: Turn | None = None
    saw_assistant = False

    for row in usage_mod.rows(path):
        ts = usage_mod.parse_stamp(row.get("timestamp"))
        # A TURN ENDS WHEN THE MODEL STOPS, NOT WHEN THE NEXT TURN STARTS. A worker turn
        # is one `claude -p` process and the next one can be days later, so closing a
        # turn at its successor's start charges it the whole idle gap: measured over the
        # fleet's 370 worker turns that read the longest one as ELEVEN DAYS of wall
        # clock, and every percentile above the median was the gap rather than the work.
        # Only conversation rows count — the UI-state rows Claude Code appends
        # (`custom-title`, `worktree-state`) are written outside the turn.
        if ts and open_turn is not None and row.get("type") in ("assistant", "user"):
            open_turn.ended = max(open_turn.ended, ts)
        prompt = _prompt_of(row, ts)
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
                            started=ts, detail=_detail_of(block.get("input")))
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


def classify_writes(calls: Sequence[usage_mod.Call],
                    floor: int = DEFAULT_WRITE_FLOOR) -> list[Write]:
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


def read_session(session_id: str, *, write_floor: int = DEFAULT_WRITE_FLOOR,
                 root: Path | None = None,
                 index: dict[str, list[Path]] | None = None) -> Anatomy:
    """Take one session apart, across every segment file it left behind.

    Segments are read in path order and their turns concatenated by start time, then
    renumbered — a session resumed under a second cwd has a file under each slug
    (`usage.index_sessions`), and numbering per file would produce two turn 1s.
    """
    anatomy = Anatomy(session_id=session_id, write_floor=write_floor)
    if index is None:
        index = usage_mod.index_sessions(root)
    paths = index.get(session_id)
    if not paths:
        return anatomy
    anatomy.found = True
    turns: list[Turn] = []
    for path in sorted(paths):
        found, labels = read_transcript(path)
        turns.extend(found)
        anatomy.subagents.update(labels)
    turns.sort(key=lambda t: t.started)
    for seq, turn in enumerate(turns, start=1):
        turn.seq = seq
    anatomy.turns = turns

    calls = usage_mod.session_calls(session_id, index=index)
    anatomy.writes = classify_writes(calls, write_floor)
    _attach_calls(turns, calls)
    _close_turns(turns)
    _name_joins(turns, anatomy.subagents)
    return anatomy


def _close_turns(turns: Sequence[Turn]) -> None:
    """End each turn at the last thing the token accounting can see inside it.

    THE ROW CLOCK IS NOT ENOUGH ON ITS OWN. A transcript file can be appended to long
    after its last API call — an old-transport session resumed by hand under the same id
    writes conversation rows with no prompt row before them, and one such file charged a
    21-minute turn with TWELVE DAYS of wall clock. Rows `usage` cannot count must not
    move a clock `usage` is the denominator of either: bounding the turn by its own
    calls and tool spans is what keeps `jarvis inspect` and `jarvis cost` cutting the
    session at identical points, which is the whole value of laying one beside the other.

    A turn with neither — a prompt whose turn never produced anything — keeps the row
    clock, because there is nothing better and zero would be a claim rather than a gap.
    """
    for turn in turns:
        seen = [call.ts for call in turn.calls]
        seen += [span.ended for span in turn.spans if span.finished]
        if seen:
            turn.ended = max(turn.started, max(seen))


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


def alarms(anatomy: Anatomy, cfg: Any, wo_id: str = "",
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
    if not anatomy.found or not anatomy.turns or not getattr(cfg, "enabled", True):
        return []
    turn = anatomy.turns[-1]
    wall = max(turn.wall, (now - turn.started) if now else 0.0)
    raised: list[Alarm] = []
    hint = f" — `jarvis inspect {wo_id}`" if wo_id else ""

    if wall >= cfg.turn_minutes * 60:
        raised.append(Alarm(TURN_ALARM, (
            f"this turn has been running {int(wall // 60)} minutes and is still being "
            f"billed{hint}")))
    for span in turn.spans:
        if span.is_join and not span.finished and now and \
                now - span.started >= cfg.join_seconds:
            waited = int((now - span.started) // 60)
            raised.append(Alarm(JOIN_ALARM, (
                f"blocked {waited}m waiting on {span.detail or span.tool_id} with no "
                f"API call in flight — long enough to lose the prompt cache, so the "
                f"wait will be paid for twice{hint}")))
            break
    for write in anatomy.writes:
        if write.ts >= turn.started and write.written >= cfg.write_tokens \
                and write.cause != COLD_START:
            raised.append(Alarm(WRITE_ALARM, (
                f"re-sent {write.written:,} cached tokens in one call ({write.cause}) "
                f"— the conversation is being paid for again{hint}")))
            break
    return raised


def live_alarms(session_id: str, cfg: Any, *, wo_id: str = "",
                now: float | None = None, write_floor: int | None = None,
                root: Path | None = None,
                index: dict[str, list[Path]] | None = None) -> list[Alarm]:
    """`alarms` for a session id — one transcript read, no paid call, nothing written.

    The write floor follows the ALARM's threshold rather than the report's: the only
    writes this needs to see are the ones big enough to raise one, and classifying the
    small ones would be work whose answer is thrown away.
    """
    floor = write_floor if write_floor is not None else int(
        getattr(cfg, "write_tokens", DEFAULT_WRITE_FLOOR))
    anatomy = read_session(session_id, write_floor=floor, root=root, index=index)
    return alarms(anatomy, cfg, wo_id=wo_id, now=now)
