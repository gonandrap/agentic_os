"""Token accounting: what a WORKER's conversation actually cost (docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md)"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from jarvis import transcript_cache

TRANSCRIPT_ROOT_ENV = "JARVIS_TRANSCRIPT_ROOT"

# List price in $ per million tokens
PRICES: dict[str, tuple[float, float]] = {
    "opus": (5.0, 25.0),
    "fable": (10.0, 50.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
DEFAULT_PRICE = PRICES["opus"]
CACHE_WRITE_RATE = 1.25  # x input price, at the 5-minute TTL
CACHE_WRITE_1H_RATE = 2.0  # x input price, at the 1-hour TTL
CACHE_READ_RATE = 0.10  # x input price, under either TTL
#: The TTL Jarvis buys (`claude_cli.PROMPT_CACHE_5M_ENV`)
WRITE_TTL_SECONDS = 300.0

#: The share of ALL cache writes that must be TTL expiry before buying the 1-hour write starts paying
TTL_BREAK_EVEN = (CACHE_WRITE_1H_RATE - CACHE_WRITE_RATE) / (
    (CACHE_WRITE_1H_RATE - CACHE_WRITE_RATE) + (CACHE_WRITE_RATE - CACHE_READ_RATE))
#: There is no module-level default for the cold-prefix floor on purpose

#: The four things a bill can be charged for, in the order they are rendered
TOKEN_CLASSES = ("input", "cache_write", "cache_read", "output")


def write_rate(cache_write: int, cache_1h: int = 0, cache_5m: int = 0) -> float:
    """The multiple of base input price one cache-write token cost, given the TTL split"""
    known = cache_1h + cache_5m
    if not cache_write or known <= 0:
        return CACHE_WRITE_RATE
    share_1h = min(1.0, max(0.0, cache_1h / known))
    return CACHE_WRITE_RATE + share_1h * (CACHE_WRITE_1H_RATE - CACHE_WRITE_RATE)


def class_costs(model: str, *, input: int = 0, cache_write: int = 0, cache_read: int = 0,
                output: int = 0, cache_1h: int = 0, cache_5m: int = 0) -> dict[str, float]:
    """What each class of token cost, at list — the line items of a bill"""
    in_rate, out_rate = price_for(model)
    return {
        "input": input * in_rate / 1e6,
        "cache_write": (cache_write * in_rate
                        * write_rate(cache_write, cache_1h, cache_5m) / 1e6),
        "cache_read": cache_read * in_rate * CACHE_READ_RATE / 1e6,
        "output": output * out_rate / 1e6,
    }


def transcript_root() -> Path:
    """Where Claude Code keeps its session transcripts"""
    override = os.environ.get(TRANSCRIPT_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config).expanduser() if config else Path("~/.claude").expanduser()
    return base / "projects"


def price_for(model: str) -> tuple[float, float]:
    """Input and output $/MTok for a model id"""
    if not model or model == "<synthetic>":
        return (0.0, 0.0)
    for family, price in PRICES.items():
        if family in model:
            return price
    return DEFAULT_PRICE


@dataclass
class Usage:
    """Token totals for one or more sessions"""

    messages: int = 0
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    context_peak: int = 0
    rewrite_excess: int = 0
    resume_boundaries: int = 0
    #: Cache-write tokens observed AT a cold boundary
    rewrite_ttl_write: int = 0
    rewrite_prefix_write: int = 0
    #: THE THIRD CAUSE, AND THE ONLY ONE THE OS CHOOSES TO PAY
    rewrite_compact_write: int = 0
    #: How many of `resume_boundaries` were the TTL expiring, and how many the OS compacted across
    boundaries_ttl: int = 0
    boundaries_compact: int = 0
    #: Boundaries whose TTL-vs-prefix split was left OPEN — the caller had no `os.cold_prefix_floor`
    boundaries_undecided: int = 0
    #: Messages a WINDOW could not place: `timestamp` missing or malformed
    undated_messages: int = 0
    #: Messages whose EVERY row was a mid-stream snapshot (`stop_reason: null`) (docs/superpowers/specs/2026-10-08-subagent-output-attribution.md §1)
    placeholder_calls: int = 0
    placeholder_output: int = 0
    cost_by_model: dict[str, float] = field(default_factory=dict)
    #: The TTL split of `cache_write`, where the source reported one
    cache_1h: int = 0
    cache_5m: int = 0
    #: What each class of token cost, at list, summed as usages merge
    cost_by_class: dict[str, float] = field(default_factory=dict)
    #: TOKENS by model — `{model: {input
    tokens_by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def __add__(self, other: Usage) -> Usage:
        merged = dict(self.cost_by_model)
        for model, cost in other.cost_by_model.items():
            merged[model] = merged.get(model, 0.0) + cost
        classes = dict(self.cost_by_class)
        for name, cost in other.cost_by_class.items():
            classes[name] = classes.get(name, 0.0) + cost
        tokens = {m: dict(counts) for m, counts in self.tokens_by_model.items()}
        for model, counts in other.tokens_by_model.items():
            into = tokens.setdefault(model, {})
            for name, value in counts.items():
                into[name] = into.get(name, 0) + value
        return Usage(
            messages=self.messages + other.messages,
            input=self.input + other.input,
            cache_write=self.cache_write + other.cache_write,
            cache_read=self.cache_read + other.cache_read,
            output=self.output + other.output,
            context_peak=max(self.context_peak, other.context_peak),
            rewrite_excess=self.rewrite_excess + other.rewrite_excess,
            resume_boundaries=self.resume_boundaries + other.resume_boundaries,
            rewrite_ttl_write=self.rewrite_ttl_write + other.rewrite_ttl_write,
            rewrite_prefix_write=self.rewrite_prefix_write + other.rewrite_prefix_write,
            rewrite_compact_write=(self.rewrite_compact_write
                                   + other.rewrite_compact_write),
            boundaries_ttl=self.boundaries_ttl + other.boundaries_ttl,
            boundaries_compact=self.boundaries_compact + other.boundaries_compact,
            boundaries_undecided=(self.boundaries_undecided
                                  + other.boundaries_undecided),
            undated_messages=self.undated_messages + other.undated_messages,
            placeholder_calls=self.placeholder_calls + other.placeholder_calls,
            placeholder_output=self.placeholder_output + other.placeholder_output,
            cost_by_model=merged,
            cache_1h=self.cache_1h + other.cache_1h,
            cache_5m=self.cache_5m + other.cache_5m,
            cost_by_class=classes,
            tokens_by_model=tokens,
        )

    @property
    def billed_input(self) -> int:
        """Every input token the API was asked to process, however it was priced"""
        return self.input + self.cache_write + self.cache_read

    @property
    def total_tokens(self) -> int:
        return self.billed_input + self.output

    @property
    def list_cost_usd(self) -> float:
        return sum(self.cost_by_model.values())

    @property
    def cached_input(self) -> int:
        """Input tokens the cache served or stored — everything but the fresh ones"""
        return self.cache_write + self.cache_read

    @property
    def rewrite_cost_usd(self) -> float:
        """What the re-written tokens cost ABOVE what reading them would have cost"""
        rate = self._blended_input_rate()
        excess = write_rate(self.cache_write, self.cache_1h, self.cache_5m) \
            - CACHE_READ_RATE
        return self.rewrite_excess * excess * rate / 1e6

    @property
    def rewrite_ttl_share(self) -> float | None:
        """Of the tax, the fraction a longer cache TTL could have bought back"""
        seen = (self.rewrite_ttl_write + self.rewrite_prefix_write
                + self.rewrite_compact_write)
        return self.rewrite_ttl_write / seen if seen else None

    @property
    def rewrite_ttl_excess(self) -> int:
        """`rewrite_excess` apportioned to TTL expiry; the remainder moved the prefix"""
        share = self.rewrite_ttl_share
        return round(self.rewrite_excess * share) if share is not None else 0

    def _blended_input_rate(self) -> float:
        models = [m for m in self.cost_by_model if price_for(m)[0]]
        if not models:
            return DEFAULT_PRICE[0]
        return sum(price_for(m)[0] for m in models) / len(models)

    def as_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "input": self.input,
            "cache_write": self.cache_write,
            "cache_read": self.cache_read,
            "output": self.output,
            "cache_1h": self.cache_1h,
            "cache_5m": self.cache_5m,
            "billed_input": self.billed_input,
            "cached_input": self.cached_input,
            "total_tokens": self.total_tokens,
            "context_peak": self.context_peak,
            "rewrite_excess": self.rewrite_excess,
            "resume_boundaries": self.resume_boundaries,
            "rewrite_ttl_share": self.rewrite_ttl_share,
            "rewrite_ttl_excess": self.rewrite_ttl_excess,
            "boundaries_ttl": self.boundaries_ttl,
            "boundaries_compact": self.boundaries_compact,
            "boundaries_undecided": self.boundaries_undecided,
            "undated_messages": self.undated_messages,
            "placeholder_calls": self.placeholder_calls,
            "placeholder_output": self.placeholder_output,
            "rewrite_compact_write": self.rewrite_compact_write,
            "list_cost_usd": round(self.list_cost_usd, 2),
            "rewrite_cost_usd": round(self.rewrite_cost_usd, 2),
            "cost_by_model": {m: round(c, 2) for m, c in self.cost_by_model.items()},
            "cost_by_class": {c: round(v, 4) for c, v in self.cost_by_class.items()},
        }


@dataclass
class Call:
    """ONE API call — the finest grain there is, and the one a turn is made of"""

    ts: float = 0.0
    model: str = ""
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    cache_1h: int = 0
    cache_5m: int = 0
    #: Whether any row of this message carried a `stop_reason` (docs/superpowers/specs/2026-10-08-subagent-output-attribution.md §1)
    sealed: bool = True

    @property
    def context(self) -> int:
        """What this one call was asked to read — its share of the window"""
        return self.input + self.cache_write + self.cache_read

    @property
    def total_tokens(self) -> int:
        return self.context + self.output

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts, "model": self.model, "input": self.input,
            "cache_write": self.cache_write, "cache_read": self.cache_read,
            "output": self.output, "cache_1h": self.cache_1h,
            "cache_5m": self.cache_5m, "context": self.context,
            "total": self.total_tokens,
        }


@dataclass
class Subagent:
    """One subagent the session spawned, with enough about it to name a bill line"""

    agent_id: str
    agent_type: str = ""
    description: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    usage: Usage = field(default_factory=Usage)

    @property
    def label(self) -> str:
        name = self.agent_type or "subagent"
        return f"{name} · {self.description}" if self.description else name

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id, "agent_type": self.agent_type,
            "description": self.description, "label": self.label,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "usage": self.usage.as_dict(),
        }


@dataclass
class SessionUsage:
    """One session's spend, with its subagents kept separate"""

    session_id: str
    main: Usage = field(default_factory=Usage)
    subagents: Usage = field(default_factory=Usage)
    subagent_count: int = 0
    found: bool = False
    #: One entry per subagent, in the order the transcripts were read
    each_subagent: list[Subagent] = field(default_factory=list)

    @property
    def total(self) -> Usage:
        return self.main + self.subagents

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "found": self.found,
            "subagent_count": self.subagent_count,
            "main": self.main.as_dict(),
            "subagents": self.subagents.as_dict(),
            "each_subagent": [s.as_dict() for s in self.each_subagent],
            "total": self.total.as_dict(),
        }


def _subagent_detail(path: Path, sub_usage: Usage) -> Subagent:
    """Name and time one subagent transcript, from its meta file and its own rows"""
    meta: dict[str, Any] = {}
    meta_path = path.with_suffix(".meta.json")
    try:
        meta = json.loads(meta_path.read_text()) or {}
    except (OSError, ValueError):
        meta = {}
    started, ended = _time_span(path)
    return Subagent(
        agent_id=path.stem,
        agent_type=str(meta.get("agentType") or ""),
        description=str(meta.get("description") or ""),
        started_at=started, ended_at=ended, usage=sub_usage,
    )


def rows(path: Path | str, needle: str = "") -> Iterator[dict[str, Any]]:
    """Every JSON row of one transcript, in file order — the one line-reader"""
    try:
        handle = Path(path).open(errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            if needle and needle not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def blocks_of(row: dict[str, Any], kind: str = "") -> list[dict[str, Any]]:
    """The content blocks of a transcript row's message, optionally of one type"""
    content = (row.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)
            and (not kind or b.get("type") == kind)]


def _time_span(path: Path) -> tuple[float, float]:
    """When a transcript's rows start and end, as unix timestamps (0 if unreadable)"""
    first = last = 0.0
    for row in rows(path, '"timestamp"'):
        when = parse_stamp(row.get("timestamp"))
        if when:
            first = first or when
            last = when
    return (first, last)


def parse_stamp(stamp: Any) -> float:
    if not isinstance(stamp, str):
        return 0.0
    try:
        # Claude Code writes RFC-3339 with a literal Z, which `fromisoformat` accepts only from 3.11
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _assistant_messages(path: Path | str) -> list[dict[str, Any]]:
    """The assistant messages in one transcript, deduped by message id"""
    return transcript_cache.product(path, "messages",
                                    lambda: _read_assistant_messages(path))


def _read_assistant_messages(path: Path | str) -> list[dict[str, Any]]:
    """The uncached walk; every caller goes through `_assistant_messages`"""
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    # The pre-filter: a row with no `usage` key cannot carry token counts
    for row in rows(path, '"usage"'):
        if row.get("type") != "assistant":
            continue
        message = row.get("message") or {}
        usage, mid = message.get("usage"), message.get("id")
        if not isinstance(usage, dict) or not mid:
            continue
        entry = by_id.get(mid)
        if entry is None:
            # `ts` rides along so a message can be placed in the TURN it ran in
            entry = by_id[mid] = {"model": message.get("model") or "",
                                  "ts": parse_stamp(row.get("timestamp")),
                                  "sealed": False}
            order.append(mid)
        # `stop_reason` lives on `message` and not on `message.usage`
        entry["sealed"] = entry["sealed"] or message.get("stop_reason") is not None
        for key, value in usage.items():
            if isinstance(value, int):
                entry[key] = max(entry.get(key, 0), value)
        # The TTL of the cache write, one level down
        creation = usage.get("cache_creation")
        if isinstance(creation, dict):
            for key in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
                value = creation.get(key)
                if isinstance(value, int):
                    entry[key] = max(entry.get(key, 0), value)
    # Filtered AFTER the merge, never per row: `ts` is the FIRST copy of a message and that copy can be...
    return [entry for entry in (by_id[mid] for mid in order) if _is_api_call(entry)]


#: The token classes an API call is billed in
_CALL_TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens")


def _is_api_call(message: dict[str, Any]) -> bool:
    """Whether a merged assistant message stands for a call to the API"""
    if (message.get("model") or "") == SYNTHETIC_MODEL:
        return False
    return any(message.get(key, 0) for key in _CALL_TOKEN_KEYS)


#: Claude Code's model id for a message it wrote itself rather than getting from the API
SYNTHETIC_MODEL = "<synthetic>"


def said_in_session(session_id: str, *, since: float = 0.0, until: float | None = None,
                    index: dict[str, list[Path]] | None = None,
                    root: Path | None = None) -> list[tuple[str, str]]:
    """(model, text) for every assistant message in a session, in the order written"""
    if index is None:
        index = index_sessions(root)
    said: list[tuple[str, str]] = []
    for path in sorted(index.get(session_id) or []):
        for row in rows(path, '"assistant"'):
            if row.get("type") != "assistant":
                continue
            ts = parse_stamp(row.get("timestamp"))
            if ts < since or (until is not None and ts > until):
                continue
            text = " ".join(b.get("text", "")
                            for b in blocks_of(row, "text")).strip()
            if text:
                said.append(((row.get("message") or {}).get("model") or "", text))
    return said


def calls_of(path: Path | str) -> list[Call]:
    """Every API call in one transcript, in the order they were made"""
    return [
        Call(
            ts=message.get("ts") or 0.0,
            model=message.get("model") or "",
            input=message.get("input_tokens", 0),
            cache_write=message.get("cache_creation_input_tokens", 0),
            cache_read=message.get("cache_read_input_tokens", 0),
            output=message.get("output_tokens", 0),
            cache_1h=message.get("ephemeral_1h_input_tokens", 0),
            cache_5m=message.get("ephemeral_5m_input_tokens", 0),
            sealed=message.get("sealed", True),
        )
        for message in _assistant_messages(path)
    ]


#: Why one cache boundary happened
BOUNDARY_COMPACTED = "compacted"
BOUNDARY_TTL = "ttl"
BOUNDARY_PREFIX = "prefix"
BOUNDARY_UNDECIDED = "undecided"


@dataclass
class Boundary:
    """One place a conversation was re-written, and what re-wrote it"""

    ts: float          # the call that paid for it
    cause: str
    cache_write: int   # that call's own write
    cache_read: int
    gap: float         # seconds since the previous call, 0.0 for the first


def classify_boundaries(calls: Sequence[Call], *, compactions: Sequence[float] = (),
                        cold_prefix_floor: int | None = None) -> list[Boundary]:
    """Every cold boundary in a run of calls, with the cause each is attributed to (#867)"""
    found: list[Boundary] = []
    previous_read: int | None = None
    previous_ts: float | None = None
    for call in calls:
        read = call.cache_read
        ts = call.ts or None
        # A turn boundary shows up as the cache going BACKWARDS: this call read less of the prefix than the...
        if previous_read is not None and read < previous_read:
            # Expired, or the prefix moved: findings/2026-08-30-where-the-800-dollars-went.md
            gap = (ts - previous_ts) if (ts and previous_ts) else None
            expired = (gap is not None and gap >= WRITE_TTL_SECONDS
                       and cold_prefix_floor is not None
                       and read <= cold_prefix_floor)
            # Tested FIRST: a compacted boundary reads the static head seconds after the summary landed
            if ts and previous_ts and any(previous_ts < c <= ts for c in compactions):
                cause = BOUNDARY_COMPACTED
            elif expired:
                cause = BOUNDARY_TTL
            elif cold_prefix_floor is None:
                cause = BOUNDARY_UNDECIDED
            else:
                cause = BOUNDARY_PREFIX
            found.append(Boundary(ts=call.ts, cause=cause,
                                  cache_write=call.cache_write, cache_read=read,
                                  gap=gap or 0.0))
        previous_read = read
        previous_ts = ts
    return found


def session_calls(session_id: str, root: Path | None = None,
                  index: dict[str, list[Path]] | None = None) -> list[Call]:
    """The LEAD agent's API calls for one session, across every segment of it"""
    if index is None:
        index = index_sessions(root)
    calls: list[Call] = []
    for path in sorted(index.get(session_id) or []):
        calls.extend(calls_of(path))
    calls.sort(key=lambda c: c.ts)
    return calls


def cost_between(session_id: str, since: float, until: float,
                 root: Path | None = None,
                 index: dict[str, list[Path]] | None = None) -> float:
    """List-price dollars the lead agent's API calls cost inside one turn's window (#471)"""
    return sum(
        priced(call.model, input=call.input, cache_write=call.cache_write,
               cache_read=call.cache_read, output=call.output,
               cache_1h=call.cache_1h, cache_5m=call.cache_5m).list_cost_usd
        for call in session_calls(session_id, root=root, index=index)
        if since <= call.ts <= until
    )


def window_envelope(session_id: str, since: float, until: float, *, schema: int,
                    root: Path | None = None,
                    index: dict[str, list[Path]] | None = None) -> dict[str, Any] | None:
    """A turn's usage envelope rebuilt from the transcript (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)"""
    if index is None:
        index = index_sessions(root)
    lead: list[Call] = []
    subs: list[Call] = []
    for path in sorted(index.get(session_id) or []):
        lead += [c for c in calls_of(path) if since <= c.ts <= until]
        subagent_dir = path.with_suffix("") / "subagents"
        if subagent_dir.is_dir():
            for sub in sorted(subagent_dir.glob("*.jsonl")):
                subs += [c for c in calls_of(sub) if since <= c.ts <= until]
    calls = lead + subs
    if not calls:
        return None
    by_model: dict[str, dict[str, Any]] = {}
    for call in calls:
        row = by_model.setdefault(call.model, {"model": call.model, "cost_usd": 0.0,
                                               "context_window": None,
                                               **{c: 0 for c in TOKEN_CLASSES}})
        for cls in TOKEN_CLASSES:
            row[cls] += getattr(call, cls)
        row["cost_usd"] += priced(call.model, input=call.input,
                                  cache_write=call.cache_write,
                                  cache_read=call.cache_read, output=call.output,
                                  cache_1h=call.cache_1h,
                                  cache_5m=call.cache_5m).list_cost_usd
    models = list(by_model.values())
    for row in models:
        row["cost_usd"] = round(row["cost_usd"], 6)
    return {
        "usage_v": schema,
        "total_cost_usd": round(sum(m["cost_usd"] for m in models), 6),
        **{cls: sum(m[cls] for m in models) for cls in TOKEN_CLASSES},
        # No envelope was written: what it would have said is these calls
        "reported": {"session_id": session_id, "source": "transcript"},
        "continues": False,
        "cache_1h": sum(c.cache_1h for c in calls),
        "cache_5m": sum(c.cache_5m for c in calls),
        # Counted here, unlike the envelope's: one per API call in the window
        "api_calls": len(calls),
        "iterations_sampled": len(lead),
        "num_turns": None,
        "context_peak": max((c.context for c in lead), default=0),
        "context_window": None,
        "duration_api_ms": None,
        "cost_by_model": {m["model"]: m["cost_usd"] for m in models},
        "by_model": models,
    }


def compactions_in(path: Path | str) -> list[dict[str, Any]]:
    """Every compaction in one transcript, oldest first, with what it did"""
    return transcript_cache.product(path, "compactions",
                                    lambda: _read_compactions(path))


def _read_compactions(path: Path | str) -> list[dict[str, Any]]:
    """The uncached walk; every caller goes through `compactions_in`"""
    found = []
    for row in rows(path, '"compact_boundary"'):
        if row.get("type") != "system" or row.get("subtype") != "compact_boundary":
            continue
        when = parse_stamp(row.get("timestamp"))
        meta = row.get("compactMetadata")
        if not when or not isinstance(meta, dict):
            continue
        found.append({"ts": when, "trigger": str(meta.get("trigger") or ""),
                      "pre": int(meta.get("preTokens") or 0),
                      "post": int(meta.get("postTokens") or 0)})
    return sorted(found, key=lambda c: c["ts"])


def compaction_stamps(path: Path | str) -> list[float]:
    """When each compaction happened in one transcript, oldest first"""
    return [c["ts"] for c in compactions_in(path)]


def last_compaction(session_id: str, *, since: float = 0.0,
                    root: Path | None = None,
                    index: dict[str, list[Path]] | None = None) -> dict[str, Any] | None:
    """The most recent compaction in a session, or None if it has never been compacted"""
    if index is None:
        index = index_sessions(root)
    found: list[dict[str, Any]] = []
    for path in sorted(index.get(session_id) or []):
        found.extend(c for c in compactions_in(path) if c["ts"] >= since)
    return max(found, key=lambda c: c["ts"]) if found else None


def _call_of(message: dict[str, Any]) -> Call:
    return Call(ts=message.get("ts") or 0.0, model=message.get("model") or "",
                input=message.get("input_tokens", 0),
                cache_write=message.get("cache_creation_input_tokens", 0),
                cache_read=message.get("cache_read_input_tokens", 0),
                output=message.get("output_tokens", 0),
                cache_1h=message.get("ephemeral_1h_input_tokens", 0),
                cache_5m=message.get("ephemeral_5m_input_tokens", 0),
                sealed=message.get("sealed", True))


def _inside(ts: float, since: float | None, until: float | None) -> bool:
    """Half-open, like every other window in the OS"""
    return not ((since is not None and ts < since) or (until is not None and ts >= until))


def _in_window(messages: list[dict[str, Any]], since: float | None,
               until: float | None) -> tuple[list[dict[str, Any]], int]:
    """The messages inside the window, and how many could not be PLACED"""
    if since is None and until is None:
        return messages, 0
    inside = [m for m in messages if m.get("ts") and _inside(m["ts"], since, until)]
    return inside, sum(1 for m in messages if not m.get("ts"))


def _usage_of(path: Path, cold_prefix_floor: int, *, since: float | None = None,
              until: float | None = None) -> Usage:
    messages = _assistant_messages(path)
    if not messages:
        return Usage()
    compactions = compaction_stamps(path)
    windowed, undated = _in_window(messages, since, until)
    usage = Usage(messages=len(windowed), undated_messages=undated)
    calls: list[Call] = []
    for message in windowed:
        model = message.get("model") or ""
        plain = message.get("input_tokens", 0)
        write = message.get("cache_creation_input_tokens", 0)
        read = message.get("cache_read_input_tokens", 0)
        out = message.get("output_tokens", 0)
        hour = message.get("ephemeral_1h_input_tokens", 0)
        five = message.get("ephemeral_5m_input_tokens", 0)
        usage.input += plain
        usage.cache_write += write
        usage.cache_read += read
        usage.output += out
        usage.cache_1h += hour
        usage.cache_5m += five
        counts = usage.tokens_by_model.setdefault(model, {})
        for name, value in (("input", plain), ("cache_write", write),
                            ("cache_read", read), ("output", out),
                            ("cache_1h", hour), ("cache_5m", five)):
            counts[name] = counts.get(name, 0) + value
        usage.context_peak = max(usage.context_peak, plain + write + read)
        # Counted, never estimated (spec 2026-10-08-subagent-output-attribution §1)
        if not message.get("sealed", True):
            usage.placeholder_calls += 1
            usage.placeholder_output += out
        calls.append(_call_of(message))
        # Per MESSAGE, where the TTL split is exact rather than a sample
        classes = class_costs(model, input=plain, cache_write=write, cache_read=read,
                              output=out, cache_1h=hour, cache_5m=five)
        for name, value in classes.items():
            if value:
                usage.cost_by_class[name] = usage.cost_by_class.get(name, 0.0) + value
        cost = sum(classes.values())
        if cost:
            usage.cost_by_model[model] = usage.cost_by_model.get(model, 0.0) + cost
    # ONE FILE at a time, which is what keeps this an exact refactor: a session-wide run would see a... (docs/superpowers/specs/2026-10-07-cost-window-selector.md)
    boundaries = classify_boundaries(
        calls if windowed is messages else [_call_of(m) for m in messages],
        compactions=compactions, cold_prefix_floor=cold_prefix_floor)
    if windowed is not messages:
        boundaries = [b for b in boundaries if _inside(b.ts, since, until)]
    fold_boundaries(usage, boundaries)
    usage.rewrite_excess = max(0, usage.cache_write - usage.context_peak)
    return usage


def fold_boundaries(usage: Usage, boundaries: Sequence[Boundary]) -> None:
    """Add a classified run of boundaries into a `Usage`'s six boundary fields"""
    for boundary in boundaries:
        usage.resume_boundaries += 1
        if boundary.cause == BOUNDARY_COMPACTED:
            usage.rewrite_compact_write += boundary.cache_write
            usage.boundaries_compact += 1
        elif boundary.cause == BOUNDARY_TTL:
            usage.rewrite_ttl_write += boundary.cache_write
            usage.boundaries_ttl += 1
        elif boundary.cause == BOUNDARY_UNDECIDED:
            usage.boundaries_undecided += 1
        else:
            usage.rewrite_prefix_write += boundary.cache_write


def priced(model: str, *, input: int = 0, cache_write: int = 0, cache_read: int = 0,
           output: int = 0, messages: int = 0, cache_1h: int = 0,
           cache_5m: int = 0) -> Usage:
    """Token counts a caller already holds, priced the same way a transcript's are"""
    usage = Usage(messages=messages, input=input, cache_write=cache_write,
                  cache_read=cache_read, output=output, cache_1h=cache_1h,
                  cache_5m=cache_5m)
    classes = class_costs(model, input=input, cache_write=cache_write,
                          cache_read=cache_read, output=output,
                          cache_1h=cache_1h, cache_5m=cache_5m)
    usage.cost_by_class = {name: value for name, value in classes.items() if value}
    cost = sum(classes.values())
    if cost:
        usage.cost_by_model[model] = cost
    return usage


def index_sessions(root: Path | None = None) -> dict[str, list[Path]]:
    """Map every session id Claude Code has a transcript for to its transcript files"""
    root = root or transcript_root()
    index: dict[str, list[Path]] = {}
    if not root.is_dir():
        return index
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        for path in project_dir.glob("*.jsonl"):
            index.setdefault(path.stem, []).append(path)
    return index


def read_session(session_id: str, cold_prefix_floor: int,
                 root: Path | None = None,
                 index: dict[str, list[Path]] | None = None,
                 *, since: float | None = None,
                 until: float | None = None) -> SessionUsage:
    """Spend for one session id, subagents included but reported separately"""
    result = SessionUsage(session_id=session_id)
    if index is None:
        index = index_sessions(root)
    paths = index.get(session_id)
    if not paths:
        return result
    result.found = True
    for path in sorted(paths):
        result.main = result.main + _usage_of(path, cold_prefix_floor, since=since,
                                              until=until)
        # Claude Code writes each subagent's own transcript beside the parent's
        subagent_dir = path.with_suffix("") / "subagents"
        if subagent_dir.is_dir():
            for sub in sorted(subagent_dir.glob("*.jsonl")):
                sub_usage = _usage_of(sub, cold_prefix_floor, since=since, until=until)
                if sub_usage.messages:
                    result.subagents = result.subagents + sub_usage
                    result.subagent_count += 1
                    result.each_subagent.append(_subagent_detail(sub, sub_usage))
    # A further segment file exists only because the session was resumed under a different cwd
    result.main.resume_boundaries += len(paths) - 1
    return result


# --- Tool results

#: How a result's size was arrived at
BASIS_CONTEXT_DELTA = "context-delta"
BASIS_CHARS = "chars"

#: The caller of a tool call is THE FILE ITS ROW CAME FROM
CALLER_MAIN = "main"
CALLER_SUBAGENT = "subagent"

#: `background.ERROR_PREFIX`, duplicated rather than imported: this module is a leaf and `background`...
_RESULT_ERROR_PREFIX = "Error: "


@dataclass
class ToolResult:
    """ONE tool call and what came back, as the transcript records it"""

    tool_use_id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    caller: str = CALLER_MAIN
    #: When the model ASKED (the `tool_use` row) and when the result LANDED
    call_ts: float = 0.0
    ts: float = 0.0
    chars: int = 0
    tokens: int = 0
    token_basis: str = ""
    is_error: bool = False
    matched: bool = False


def tool_results(path: Path | str, *, chars_per_token: float = 4.0,
                 ) -> list[ToolResult]:
    """Every tool call in one transcript, paired with its result and sized"""
    if chars_per_token <= 0:
        raise ValueError("chars_per_token must be > 0")
    saved = transcript_cache.product(
        path, f"tools@{chars_per_token:g}",
        lambda: [asdict(r) for r in _read_tool_results(
            path, chars_per_token=chars_per_token)])
    # Rebuilt per call: `_size_group` mutates these, and the cache must not see it
    return [ToolResult(**row) for row in saved]


def _read_tool_results(path: Path | str, *, chars_per_token: float,
                       ) -> list[ToolResult]:
    """The uncached walk; every caller goes through `tool_results`"""
    path = Path(path)
    caller = CALLER_SUBAGENT if path.parent.name == "subagents" else CALLER_MAIN
    found: list[ToolResult] = []
    pending: dict[str, ToolResult] = {}
    # The results that landed since the last API call: the ones the NEXT call's context growth paid for
    group: list[ToolResult] = []
    calls: dict[str, Call] = {}
    previous: Call | None = None
    # A prompt row or a compaction between two calls means the context grew (or shrank) for a reason that...
    interrupted = False

    for row in rows(path):
        kind = row.get("type")
        if kind == "system" and row.get("subtype") == "compact_boundary":
            interrupted = True
            continue
        if _is_prompt_row(row):
            interrupted = True
            continue
        if kind == "assistant":
            call, mid = _call_of_row(row)
            if call is not None:
                seen = calls.get(mid)
                if seen is not None:
                    # THE STREAMING DUPLICATE: one assistant message is written several times as its text grows
                    _merge_call(seen, call)
                else:
                    _size_group(group, previous, call, interrupted, chars_per_token)
                    group = []
                    interrupted = False
                    calls[mid] = call
                    previous = call
            for block in blocks_of(row, "tool_use"):
                tool_id = str(block.get("id") or "")
                if not tool_id or tool_id in pending:
                    continue
                arguments = block.get("input")
                item = ToolResult(
                    tool_use_id=tool_id, name=str(block.get("name") or ""),
                    input=arguments if isinstance(arguments, dict) else {},
                    caller=caller, call_ts=parse_stamp(row.get("timestamp")))
                pending[tool_id] = item
                found.append(item)
            continue
        for block in blocks_of(row, "tool_result"):
            item = pending.pop(str(block.get("tool_use_id") or ""), None)
            if item is None:
                # A result for a call this file never recorded
                continue
            item.matched = True
            item.ts = parse_stamp(row.get("timestamp"))
            item.chars = len(_result_text(block.get("content")))
            item.is_error = _result_failed(block, row.get("toolUseResult"))
            group.append(item)
    # A result after the LAST call of the session has no delta to be measured against
    _size_group(group, previous, None, True, chars_per_token)
    return found


def _is_prompt_row(row: dict[str, Any]) -> bool:
    """Whether this row is the USER talking, rather than the agent's own loop"""
    if row.get("type") != "user":
        return False
    if row.get("promptSource") != "sdk" and (
            row.get("isMeta") or blocks_of(row, "tool_result")):
        return False
    content = (row.get("message") or {}).get("content")
    text = content if isinstance(content, str) else " ".join(
        b.get("text", "") for b in blocks_of(row, "text"))
    return bool(text.strip())


#: A session's title is one line of a listing, not the prompt (usage-meter spec §6)
PROMPT_TITLE_CHARS = 120


def first_prompt(path: Path | str, *, limit: int = PROMPT_TITLE_CHARS) -> str:
    """The first thing the USER typed in one transcript, collapsed and truncated"""
    for row in rows(path):
        if not _is_prompt_row(row):
            continue
        content = (row.get("message") or {}).get("content")
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in blocks_of(row, "text"))
        collapsed = " ".join(text.split())
        if collapsed:
            return collapsed[:limit]
    return ""


def _call_of_row(row: dict[str, Any]) -> tuple[Call | None, str]:
    """The API call one assistant row stands for, with its message id, or (None, "")"""
    message = row.get("message") or {}
    counts, mid = message.get("usage"), message.get("id")
    if not isinstance(counts, dict) or not mid:
        return (None, "")
    entry: dict[str, Any] = {"model": message.get("model") or ""}
    for key, value in counts.items():
        if isinstance(value, int):
            entry[key] = value
    creation = counts.get("cache_creation")
    if isinstance(creation, dict):
        for key in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
            value = creation.get(key)
            if isinstance(value, int):
                entry[key] = value
    if not _is_api_call(entry):
        return (None, "")
    return (Call(ts=parse_stamp(row.get("timestamp")), model=entry["model"],
                 input=entry.get("input_tokens", 0),
                 cache_write=entry.get("cache_creation_input_tokens", 0),
                 cache_read=entry.get("cache_read_input_tokens", 0),
                 output=entry.get("output_tokens", 0),
                 cache_1h=entry.get("ephemeral_1h_input_tokens", 0),
                 cache_5m=entry.get("ephemeral_5m_input_tokens", 0)), str(mid))


def _merge_call(into: Call, other: Call) -> None:
    """Fold a repeated copy of one assistant message into the call already placed"""
    into.input = max(into.input, other.input)
    into.cache_write = max(into.cache_write, other.cache_write)
    into.cache_read = max(into.cache_read, other.cache_read)
    into.output = max(into.output, other.output)
    into.cache_1h = max(into.cache_1h, other.cache_1h)
    into.cache_5m = max(into.cache_5m, other.cache_5m)


def _size_group(group: list[ToolResult], previous: Call | None, nxt: Call | None,
                interrupted: bool, chars_per_token: float) -> None:
    """Charge one batch of results with the tokens the next call carried them as"""
    if not group:
        return
    delta = 0
    if previous is not None and nxt is not None and not interrupted:
        delta = nxt.context - previous.context - previous.output
    if delta > 0:
        for item, share in zip(group, _split_by_chars(delta,
                                                      [i.chars for i in group])):
            item.tokens = share
            item.token_basis = BASIS_CONTEXT_DELTA
        return
    for item in group:
        item.tokens = math.ceil(item.chars / chars_per_token)
        item.token_basis = BASIS_CHARS


def _split_by_chars(total: int, weights: list[int]) -> list[int]:
    """Split an exact total in proportion to character counts, losing nothing"""
    count = len(weights)
    if total <= 0 or count == 0:
        return [0] * count
    denominator = sum(weights) or count
    scale = weights if sum(weights) else [1] * count
    parts = [total * weight // denominator for weight in scale]
    remainders = sorted(range(count),
                        key=lambda i: (-(total * scale[i] % denominator), i))
    for index in remainders[:total - sum(parts)]:
        parts[index] += 1
    return parts


def _result_text(content: Any) -> str:
    """A `tool_result` block's text: a bare string, or a list of text blocks"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text") or "") for b in content
                        if isinstance(b, dict))
    return ""


def _result_failed(block: dict[str, Any], result: Any) -> bool:
    """Whether this result reports an ERROR — `background._failed`'s precedence"""
    if block.get("is_error"):
        return True
    if isinstance(result, dict):
        return bool(result.get("is_error") or result.get("isError"))
    text = result if isinstance(result, str) else _result_text(block.get("content"))
    return text.startswith(_RESULT_ERROR_PREFIX)
