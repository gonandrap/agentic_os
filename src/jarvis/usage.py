"""Token accounting: what a WORKER's conversation actually cost.

This module reads the ledger Claude Code keeps for a worker's session: a JSONL
transcript per session under `~/.claude/projects/<slug>/<session-id>.jsonl`, with a
`usage` object on every assistant message. It attributes that spend back to the work
order that caused it, via `work_orders.session_id`.

Read-only, and deliberately outside the store layer: it derives a fact about a work
order from a file Jarvis does not own and cannot repair, so there is nothing to
persist and nothing to reconcile.

THIS IS NOT THE WHOLE BILL, and it never was. A work order also costs whatever Jarvis
itself spent on it — Neo answering its questions, the panel's seats deliberating, the
digest shortening the result for the dashboard. Those are one-shot `claude -p` calls
with no session Jarvis owns and nothing in their transcripts naming a work order, so
they cannot be recovered this way at all: they are recorded as they happen, in
`agent_usage`, and `ops.cost_report` adds the two together (and shows the split, because
"the OS spent a third of this work order's tokens on itself" is the fact worth seeing).

## The re-write tax, and why it gets its own number

Every worker turn is a separate `claude -p --resume` process (`claude_cli.turn_args`).
On the first API call of each turn after the first, the conversation prefix the
previous turn had cached is invalidated and the whole accumulated context is re-sent
as a cache WRITE, at 1.25x, instead of a cache READ at 0.1x — twice over, in fact, on
two consecutive calls.

A boundary goes cold for one of two reasons, and they have OPPOSITE remedies, which is
why `rewrite_ttl_write`/`rewrite_prefix_write` split the tax by cause rather than
reporting one number:

- THE PREFIX MOVED. Claude Code's system prompt carries a dynamic per-machine section
  including `git status`, so a worker — whose whole job is editing files in its
  worktree — presents a different prefix next turn. Time is not the variable: a
  10-second boundary after an edit is cold and a 54-minute one after a read-only turn
  is warm. `dispatch._write_worker_settings` sets `includeGitInstructions: false`,
  which removes the snapshot and is the ONLY switch that does
  (`--exclude-dynamic-system-prompt-sections` merely relocates it). No cache TTL can
  help here — the entry is alive and does not match.
- THE ENTRY EXPIRED. The gap exceeded the write's TTL, so nothing survives, not even
  the static system prompt. Only this half would be bought back by a longer TTL.

Telling them apart is what makes the TTL decision answerable, and confusing them is how
a 12-second boundary got read as a 14-minute one:
`docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md`.

What Jarvis controls is the number of boundaries, not their price — which is why
`Daemon.deliver_messages` coalesces everything queued for a work order into one turn,
and why this module reports `resume_boundaries` next to the tax.

So `rewrite_excess` is the headline number this module exists to produce: in a
perfectly cached session every token is written to the cache exactly once, and the
total written can therefore never exceed the largest context the session reached.
Everything above that line is a token Jarvis paid to send twice.

    rewrite_excess = max(0, sum(cache_write) - max(context))

That definition needs no threshold, which is the point — an earlier heuristic
("a big write next to a small read") got the same answer here but only because
this session's boundaries were stark.

## Prices are a proxy, not a bill

The dollar figures are Anthropic list prices, and the user is on a subscription: they
are a common unit that lets a cache-read token be compared with an output token, not
an invoice. Tokens are the primary figure everywhere; cost is derived and labelled.

## A cache write is priced by its TTL, and Jarvis bought the expensive one for months

A prompt-cache WRITE costs 1.25x base input at the 5-MINUTE TTL and 2x at the ONE-HOUR
TTL; a read is 0.1x under either (kn-f94abf34, measured over 1,075 transcripts). Every
write a Jarvis worker made before `FORCE_PROMPT_CACHING_5M` shipped was a 1h write, so
pricing them all at 1.25x understated the largest avoidable line in the bill by up to
60% of itself. Both rates live here and the SPLIT decides: `cache_creation` carries
`ephemeral_1h_input_tokens` and `ephemeral_5m_input_tokens`, per assistant message in a
transcript and per turn in a result envelope, and where it is present it is used. Where
it is absent — an old row, a caller that has counts and nothing else — the 5-minute rate
is the floor and the estimate stays where it always was, rather than guessing upward.

THE FLOOR IS ONLY HONEST IF THE CALLER ACTUALLY HAS NO SPLIT TO GIVE. A caller that omits
`priced()`'s `cache_1h`/`cache_5m` is claiming ignorance on the report's behalf, and two
surfaces reading the same rows then disagree about the same tokens — see
`docs/superpowers/specs/2026-08-22-the-five-minute-write-everywhere.md`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

TRANSCRIPT_ROOT_ENV = "JARVIS_TRANSCRIPT_ROOT"

# List price in $ per million tokens, keyed by the family name that appears in the
# model id (`claude-opus-5`, `claude-haiku-4-5-20251001`): (input, output).
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
#: The TTL Jarvis buys (`claude_cli.PROMPT_CACHE_5M_ENV`). A boundary closer together
#: than this cannot be an expiry, whatever else it looks like.
WRITE_TTL_SECONDS = 300.0
#: There is no module-level default for the cold-prefix floor on purpose. It is
#: `os.cold_prefix_floor`, every reader passes it in, and a caller with no catalog gets
#: the catalog's own error rather than a number this module invented — a report that
#: silently classified boundaries against a guessed threshold would print a finding the
#: configuration never produced.

#: The four things a bill can be charged for, in the order they are rendered. Named here
#: because every surface that breaks a line item down by class walks this list, and a
#: class that exists in one renderer and not another is a token the reader cannot find.
TOKEN_CLASSES = ("input", "cache_write", "cache_read", "output")


def write_rate(cache_write: int, cache_1h: int = 0, cache_5m: int = 0) -> float:
    """The multiple of base input price one cache-write token cost, given the TTL split.

    The split is often a SAMPLE rather than the whole: a turn's result envelope reports
    `cache_creation` for part of the turn while `modelUsage` reports the total written.
    Its RATIO is what is used, applied to the whole — an 87/13 sample of a turn that
    wrote 1.9M tokens prices those 1.9M at 1.89x. With no split at all the 5-minute rate
    stands, which is the floor and what this module has always charged.
    """
    known = cache_1h + cache_5m
    if not cache_write or known <= 0:
        return CACHE_WRITE_RATE
    share_1h = min(1.0, max(0.0, cache_1h / known))
    return CACHE_WRITE_RATE + share_1h * (CACHE_WRITE_1H_RATE - CACHE_WRITE_RATE)


def class_costs(model: str, *, input: int = 0, cache_write: int = 0, cache_read: int = 0,
                output: int = 0, cache_1h: int = 0, cache_5m: int = 0) -> dict[str, float]:
    """What each class of token cost, at list — the line items of a bill.

    One dict per call site rather than a single total, because "where did the money go"
    is answered by the classes and not by their sum: a session whose bill is 70% cache
    READ is behaving, and one whose bill is 70% cache WRITE is paying the re-write tax.
    Keys are `TOKEN_CLASSES`, so a renderer can walk them without knowing the rates.
    """
    in_rate, out_rate = price_for(model)
    return {
        "input": input * in_rate / 1e6,
        "cache_write": (cache_write * in_rate
                        * write_rate(cache_write, cache_1h, cache_5m) / 1e6),
        "cache_read": cache_read * in_rate * CACHE_READ_RATE / 1e6,
        "output": output * out_rate / 1e6,
    }


def transcript_root() -> Path:
    """Where Claude Code keeps its session transcripts.

    `JARVIS_TRANSCRIPT_ROOT` overrides it so a test can point at a fixture tree; the
    real location follows `CLAUDE_CONFIG_DIR`, which Claude Code itself honours.
    """
    override = os.environ.get(TRANSCRIPT_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config).expanduser() if config else Path("~/.claude").expanduser()
    return base / "projects"


def price_for(model: str) -> tuple[float, float]:
    """Input and output $/MTok for a model id.

    `<synthetic>` is Claude Code's placeholder for a message it generated itself (an
    API error rendered as an assistant turn); it was never billed, so it prices at
    zero rather than falling through to the Opus default.
    """
    if not model or model == "<synthetic>":
        return (0.0, 0.0)
    for family, price in PRICES.items():
        if family in model:
            return price
    return DEFAULT_PRICE


@dataclass
class Usage:
    """Token totals for one or more sessions.

    `context_peak` takes the max when two usages merge (the biggest context anything
    reached); every other field is additive. `rewrite_excess` is computed per session
    at read time and then summed, which is why it is a stored field and not a property
    derived from the merged totals — a merged `context_peak` would understate it.
    """

    messages: int = 0
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    context_peak: int = 0
    rewrite_excess: int = 0
    resume_boundaries: int = 0
    #: Cache-write tokens observed AT a cold boundary, split by which of the two causes
    #: the docstring describes produced it. These are raw observations and their sum is
    #: NOT `rewrite_excess` — that has its own threshold-free definition. They exist to
    #: carry the RATIO, which is the only part that merges honestly across sessions
    #: (kn-7a2180ba: make the finer accounting a partition of the coarser, never an
    #: addend). `rewrite_ttl_excess` applies the ratio and IS a partition.
    rewrite_ttl_write: int = 0
    rewrite_prefix_write: int = 0
    #: How many of `resume_boundaries` were the TTL expiring. The rest moved the prefix.
    boundaries_ttl: int = 0
    cost_by_model: dict[str, float] = field(default_factory=dict)
    #: The TTL split of `cache_write`, where the source reported one. Their sum can be
    #: LESS than `cache_write` (a partial sample) and is zero when nothing is known —
    #: which is why they are kept beside it rather than instead of it. See `write_rate`.
    cache_1h: int = 0
    cache_5m: int = 0
    #: What each class of token cost, at list, summed as usages merge. Carried rather
    #: than re-derived from the totals because the write rate depends on a TTL split
    #: that is exact per message and only approximate once messages are added together.
    cost_by_class: dict[str, float] = field(default_factory=dict)
    #: TOKENS by model — `{model: {input, cache_write, cache_read, output, ...}}`, the
    #: same classes as the fields above. `cost_by_model` was never enough to subtract
    #: one usage from another: the bill splits a turn into the agents that ran inside it
    #: by taking the turn's totals MINUS its subagents', and a subtraction that mixes
    #: models would move tokens between price bands. Per model, it cannot.
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
            boundaries_ttl=self.boundaries_ttl + other.boundaries_ttl,
            cost_by_model=merged,
            cache_1h=self.cache_1h + other.cache_1h,
            cache_5m=self.cache_5m + other.cache_5m,
            cost_by_class=classes,
            tokens_by_model=tokens,
        )

    @property
    def billed_input(self) -> int:
        """Every input token the API was asked to process, however it was priced."""
        return self.input + self.cache_write + self.cache_read

    @property
    def total_tokens(self) -> int:
        return self.billed_input + self.output

    @property
    def list_cost_usd(self) -> float:
        return sum(self.cost_by_model.values())

    @property
    def cached_input(self) -> int:
        """Input tokens the cache served or stored — everything but the fresh ones."""
        return self.cache_write + self.cache_read

    @property
    def rewrite_cost_usd(self) -> float:
        """What the re-written tokens cost ABOVE what reading them would have cost.

        Priced at the blended input rate of the models actually used, so a session
        that ran on Haiku is not charged Opus waste, and at the write rate the TTL
        split actually implies: at the 1-hour TTL a re-write is 20x a read, not 12.5x.
        """
        rate = self._blended_input_rate()
        excess = write_rate(self.cache_write, self.cache_1h, self.cache_5m) \
            - CACHE_READ_RATE
        return self.rewrite_excess * excess * rate / 1e6

    @property
    def rewrite_ttl_share(self) -> float | None:
        """Of the tax, the fraction a longer cache TTL could have bought back.

        None when no boundary was classified — an honest 'not measured', which the
        renderers must not print as 0% (that would read as a finding).
        """
        seen = self.rewrite_ttl_write + self.rewrite_prefix_write
        return self.rewrite_ttl_write / seen if seen else None

    @property
    def rewrite_ttl_excess(self) -> int:
        """`rewrite_excess` apportioned to TTL expiry; the remainder moved the prefix."""
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
            "list_cost_usd": round(self.list_cost_usd, 2),
            "rewrite_cost_usd": round(self.rewrite_cost_usd, 2),
            "cost_by_model": {m: round(c, 2) for m, c in self.cost_by_model.items()},
            "cost_by_class": {c: round(v, 4) for c, v in self.cost_by_class.items()},
        }


@dataclass
class Call:
    """ONE API call — the finest grain there is, and the one a turn is made of.

    A worker turn is a whole agent loop: the model answers, a tool runs, the model is
    called again with the result appended, until it stops. Every one of those calls
    re-sends the conversation so far, so a turn's `cache_read` is a SUM over its calls
    and not a size — 11 calls each re-reading a 55k conversation read 517k between them,
    which is the question this record exists to answer (wo-e23252e4, turn 1).

    One per assistant message in the transcript, which is exactly one per API call: on
    that turn the 11 messages' counts sum to the CLI's own `modelUsage` totals to the
    token, in all four classes. `ts` is when the call landed, and is what buckets a call
    into the turn it ran in — the same last-turn-started-by-then rule the rest of the
    bill uses.
    """

    ts: float = 0.0
    model: str = ""
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    cache_1h: int = 0
    cache_5m: int = 0

    @property
    def context(self) -> int:
        """What this one call was asked to read — its share of the window.

        THE figure a turn's `cache_read` is not: the context is what ONE call carried,
        and it is the number that says whether a conversation is getting too big.
        """
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
    """One subagent the session spawned, with enough about it to name a bill line.

    `agent_type` and `description` come from the `.meta.json` Claude Code writes beside
    each subagent transcript — "Explore", "Find worker session creation code". Without
    them a bill can only offer `agent-ac837f05`, which answers "how much" and not the
    question anyone actually has, which is "on what".

    `started_at` is a unix timestamp, used to charge the subagent to the TURN it ran in
    (the same last-turn-started-by-then rule everything else on the bill uses).
    """

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
    """One session's spend, with its subagents kept separate.

    Subagents get their own line because they are a design choice a reader can act on:
    on the planner that prompted this module they were a third of the bill.
    """

    session_id: str
    main: Usage = field(default_factory=Usage)
    subagents: Usage = field(default_factory=Usage)
    subagent_count: int = 0
    found: bool = False
    #: One entry per subagent, in the order the transcripts were read. `subagents` is
    #: their sum and stays the figure everything else uses; this is what lets the bill
    #: put each of them on its own row instead of reporting a lump.
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
    """Name and time one subagent transcript, from its meta file and its own rows.

    Both sources are best-effort: a subagent whose meta file is missing still gets a
    line, named for its file, because dropping it would take its tokens off the bill.
    """
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
    """Every JSON row of one transcript, in file order — the one line-reader.

    A transcript is JSONL of mixed row types, and every reader in the OS wants a
    different projection of it: token counts, prose, tool spans. They share this so a
    change in how Claude Code writes a file lands in one place; a missing or corrupt
    file yields nothing, which is what makes "no transcript" the same answer everywhere.

    `needle` is a substring pre-filter applied to the RAW LINE before parsing. Most rows
    in a transcript are of no interest to any one caller, and `json.loads` on a megabyte
    of them is the whole cost of a read: `_assistant_messages` skips ~75% of the rows in
    a real file this way. A caller that wants everything passes nothing.
    """
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
    """The content blocks of a transcript row's message, optionally of one type.

    A row's `message.content` is a list of blocks on an API message and a bare string on
    some user rows; both shapes are real and neither is an error, so a caller asking for
    blocks of a string message gets none rather than a crash.
    """
    content = (row.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)
            and (not kind or b.get("type") == kind)]


def _time_span(path: Path) -> tuple[float, float]:
    """When a transcript's rows start and end, as unix timestamps (0 if unreadable)."""
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
        # Claude Code writes RFC-3339 with a literal Z, which `fromisoformat` accepts
        # only from 3.11; the replace keeps this working the same on either.
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _assistant_messages(path: Path | str) -> list[dict[str, Any]]:
    """The assistant messages in one transcript, deduped by message id.

    THE TRAP: a single assistant message is written to the transcript several times —
    once as each content block arrives, and again as its text grows. Every copy repeats
    the same `usage` object except `output_tokens`, which climbs toward the final value.
    Summing the rows therefore counts input two or three times over: the first
    measurement of wo-cd73c537 read 2.7M cache-write tokens where the true figure is
    1.03M. Keep one entry per message id, and take the MAX of each field rather than
    the first — the first copy of a message reports `output_tokens: 1`.
    """
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    # The pre-filter: a row with no `usage` key cannot carry token counts, and most
    # rows in a transcript are tool results or UI state.
    for row in rows(path, '"usage"'):
        if row.get("type") != "assistant":
            continue
        message = row.get("message") or {}
        usage, mid = message.get("usage"), message.get("id")
        if not isinstance(usage, dict) or not mid:
            continue
        entry = by_id.get(mid)
        if entry is None:
            # `ts` rides along so a message can be placed in the TURN it ran in.
            # First occurrence, not max: a message is rewritten as its text grows,
            # and when the call LANDED is when its first row was written.
            entry = by_id[mid] = {"model": message.get("model") or "",
                                  "ts": parse_stamp(row.get("timestamp"))}
            order.append(mid)
        for key, value in usage.items():
            if isinstance(value, int):
                entry[key] = max(entry.get(key, 0), value)
        # The TTL of the cache write, one level down. Flattened in rather than
        # skipped with the rest of the nested values: it is not a detail but a
        # PRICE — the same token costs 1.25x or 2x depending on which of these two
        # it landed in (kn-f94abf34), and it is exact per message here.
        creation = usage.get("cache_creation")
        if isinstance(creation, dict):
            for key in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
                value = creation.get(key)
                if isinstance(value, int):
                    entry[key] = max(entry.get(key, 0), value)
    return [by_id[mid] for mid in order]


#: Claude Code's model id for a message it wrote itself rather than getting from the
#: API — an auth refusal, a usage-limit notice, a cancellation. `price_for` already
#: knows it costs nothing; `said_in_session` is the caller that reads what it SAYS.
SYNTHETIC_MODEL = "<synthetic>"


def said_in_session(session_id: str, *, since: float = 0.0, until: float | None = None,
                    index: dict[str, list[Path]] | None = None,
                    root: Path | None = None) -> list[tuple[str, str]]:
    """(model, text) for every assistant message in a session, in the order written.

    NOT accounting — the one reader in this module that wants the prose rather than the
    tokens, and it exists because the transcript is sometimes the ONLY record of why a
    turn died (`worker_session._transcript_error`). It shares this module's index
    because that index is the only reliable way to find a worker's transcript: the
    directory is the slugified cwd the session was created in, which for a worker is a
    worktree that may no longer exist.

    `since`/`until` bound it to ONE TURN, and they are not optional in practice. A
    session outlives the turn that failed in it: wo-c2793bf0's transcript holds the auth
    refusal that killed turn 1 and, eight minutes later, an unrelated "No response
    requested." from turn 2 — so the last synthetic message in the FILE is the wrong
    answer to "why did this turn die".
    """
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
    """Every API call in one transcript, in the order they were made.

    The same messages `_usage_of` totals, kept apart instead of added up. Deliberately a
    second walk over the file rather than a second return value: the totals are read on
    every cost surface there is and the per-call detail on one, and paying for the split
    only where it is asked for keeps `read_session` the cheap thing it has to stay.
    """
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
        )
        for message in _assistant_messages(path)
    ]


def session_calls(session_id: str, root: Path | None = None,
                  index: dict[str, list[Path]] | None = None) -> list[Call]:
    """The LEAD agent's API calls for one session, across every segment of it.

    The lead agent only: a subagent writes its own transcript, and its calls belong to
    its own row on the bill rather than to the session's. That is what keeps these
    reconcilable — they sum to the turn's line MINUS its subagents, which is exactly the
    lead-agent line the bill already computes.
    """
    if index is None:
        index = index_sessions(root)
    calls: list[Call] = []
    for path in sorted(index.get(session_id) or []):
        calls.extend(calls_of(path))
    calls.sort(key=lambda c: c.ts)
    return calls


def _usage_of(path: Path, cold_prefix_floor: int) -> Usage:
    messages = _assistant_messages(path)
    if not messages:
        return Usage()
    usage = Usage(messages=len(messages))
    previous_read: int | None = None
    previous_ts: float | None = None
    for message in messages:
        model = message.get("model") or ""
        ts = message.get("ts") or None
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
        # A turn boundary shows up as the cache going BACKWARDS: this call read less
        # of the prefix than the one before it did. Counting the drops rather than
        # thresholding the write size keeps this free of magic numbers, and it lands
        # on exactly (turns - 1) for every work order measured.
        if previous_read is not None and read < previous_read:
            usage.resume_boundaries += 1
            # Expired, or the prefix moved: findings/2026-08-30-where-the-800-dollars-went.md
            gap = (ts - previous_ts) if (ts and previous_ts) else None
            expired = (gap is not None and gap >= WRITE_TTL_SECONDS
                       and read <= cold_prefix_floor)
            if expired:
                usage.rewrite_ttl_write += write
                usage.boundaries_ttl += 1
            else:
                usage.rewrite_prefix_write += write
        previous_read = read
        previous_ts = ts
        # Per MESSAGE, where the TTL split is exact rather than a sample — which is the
        # most accurate this estimate can be made without the CLI's own figure.
        classes = class_costs(model, input=plain, cache_write=write, cache_read=read,
                              output=out, cache_1h=hour, cache_5m=five)
        for name, value in classes.items():
            if value:
                usage.cost_by_class[name] = usage.cost_by_class.get(name, 0.0) + value
        cost = sum(classes.values())
        if cost:
            usage.cost_by_model[model] = usage.cost_by_model.get(model, 0.0) + cost
    usage.rewrite_excess = max(0, usage.cache_write - usage.context_peak)
    return usage


def priced(model: str, *, input: int = 0, cache_write: int = 0, cache_read: int = 0,
           output: int = 0, messages: int = 0, cache_1h: int = 0,
           cache_5m: int = 0) -> Usage:
    """Token counts a caller already holds, priced the same way a transcript's are.

    The OS's own calls (`agent_usage`) arrive as counts from the CLI's result JSON rather
    than as transcript rows, and their exact dollar figure comes from the CLI too. This
    exists so they can ALSO be expressed in this module's currency — Anthropic list
    prices — because that is the only way OS spend and worker spend can be added into one
    number without mixing an exact figure with an estimate. The two accountings are kept
    side by side everywhere else for the same reason; see `ops._turn_summary`.

    No `context_peak` and no `rewrite_excess`: both are properties of a CONVERSATION, and
    a one-shot OS call has none. Leaving them zero is what keeps a hundred Neo calls from
    reading as a re-write problem they cannot have.
    """
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
    """Map every session id Claude Code has a transcript for to its transcript files.

    Built in one pass rather than globbing per work order: session ids are UUIDs and
    so globally unique, but the directory they live under is the slugified cwd the
    session was CREATED in, which for a worker is its worktree — not something Jarvis
    can reconstruct reliably once the worktree is gone.

    A LIST of files per id, because that cwd can change between segments of one
    session, leaving a file under each slug. wo-2fa7c0e9's did — repo root, then its
    worktree — and an index that kept one path per id read that work order as $0.51 /
    1 turn where the true figure was three times that.
    """
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
                 index: dict[str, list[Path]] | None = None) -> SessionUsage:
    """Spend for one session id, subagents included but reported separately.

    `cold_prefix_floor` is `os.cold_prefix_floor` and is REQUIRED: a caller that cannot
    reach a catalog must fail rather than classify against a guessed threshold.
    """
    result = SessionUsage(session_id=session_id)
    if index is None:
        index = index_sessions(root)
    paths = index.get(session_id)
    if not paths:
        return result
    result.found = True
    for path in sorted(paths):
        result.main = result.main + _usage_of(path, cold_prefix_floor)
        # Claude Code writes each subagent's own transcript beside the parent's, under
        # a directory named for the parent session — beside whichever segment the
        # subagent was spawned from.
        subagent_dir = path.with_suffix("") / "subagents"
        if subagent_dir.is_dir():
            for sub in sorted(subagent_dir.glob("*.jsonl")):
                sub_usage = _usage_of(sub, cold_prefix_floor)
                if sub_usage.messages:
                    result.subagents = result.subagents + sub_usage
                    result.subagent_count += 1
                    result.each_subagent.append(_subagent_detail(sub, sub_usage))
    # A further segment file exists only because the session was resumed under a
    # different cwd, so each one is a turn boundary the cache-read comparison cannot
    # see (it never compares across files).
    result.main.resume_boundaries += len(paths) - 1
    return result
