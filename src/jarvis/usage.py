"""Token accounting: what a work order actually cost.

Jarvis records no token usage of its own — nothing in the three databases has ever
carried a token count — so the only ledger is the one Claude Code already keeps: a
JSONL transcript per session under `~/.claude/projects/<slug>/<session-id>.jsonl`,
with a `usage` object on every assistant message. This module reads those and
attributes the spend back to the work order that caused it, via
`work_orders.session_id`.

Read-only, and deliberately outside the store layer: it derives a fact about a work
order from a file Jarvis does not own and cannot repair, so there is nothing to
persist and nothing to reconcile.

## The re-write tax, and why it gets its own number

Every worker turn is a separate `claude -p --resume` process (`claude_cli.turn_args`).
On the first API call of each turn after the first, the conversation prefix the
previous turn had cached is invalidated and the whole accumulated context is re-sent
as a cache WRITE, at 1.25x, instead of a cache READ at 0.1x — twice over, in fact, on
two consecutive calls.

The cause is upstream and Jarvis cannot fix it: Claude Code's system prompt carries a
dynamic per-machine section that includes `git status`, so a worker — whose whole job
is editing files in its worktree — presents a different prompt prefix on its next turn.
Confirmed in a clean room at 27k of context: a git repo whose turn edited a file goes
cold, the same repo read-only stays warm, and `--exclude-dynamic-system-prompt-sections`
relocates the section without stabilising it. It is NOT TTL (a 10-second boundary after
an edit is cold; a 54-minute one after a read-only turn is warm) and NOT the MCP set.
Evidence and the four-call repro:
`docs/superpowers/specs/2026-08-08-token-spend-findings.md`.

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
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
CACHE_WRITE_RATE = 1.25  # x input price
CACHE_READ_RATE = 0.10  # x input price


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
    cost_by_model: dict[str, float] = field(default_factory=dict)

    def __add__(self, other: Usage) -> Usage:
        merged = dict(self.cost_by_model)
        for model, cost in other.cost_by_model.items():
            merged[model] = merged.get(model, 0.0) + cost
        return Usage(
            messages=self.messages + other.messages,
            input=self.input + other.input,
            cache_write=self.cache_write + other.cache_write,
            cache_read=self.cache_read + other.cache_read,
            output=self.output + other.output,
            context_peak=max(self.context_peak, other.context_peak),
            rewrite_excess=self.rewrite_excess + other.rewrite_excess,
            resume_boundaries=self.resume_boundaries + other.resume_boundaries,
            cost_by_model=merged,
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
    def rewrite_cost_usd(self) -> float:
        """What the re-written tokens cost ABOVE what reading them would have cost.

        Priced at the blended input rate of the models actually used, so a session
        that ran on Haiku is not charged Opus waste.
        """
        rate = self._blended_input_rate()
        return self.rewrite_excess * (CACHE_WRITE_RATE - CACHE_READ_RATE) * rate / 1e6

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
            "billed_input": self.billed_input,
            "total_tokens": self.total_tokens,
            "context_peak": self.context_peak,
            "rewrite_excess": self.rewrite_excess,
            "resume_boundaries": self.resume_boundaries,
            "list_cost_usd": round(self.list_cost_usd, 2),
            "rewrite_cost_usd": round(self.rewrite_cost_usd, 2),
            "cost_by_model": {m: round(c, 2) for m, c in self.cost_by_model.items()},
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
            "total": self.total.as_dict(),
        }


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
    try:
        handle = Path(path).open(errors="replace")
    except OSError:
        return []
    with handle:
        for line in handle:
            # Cheap pre-filter: a row with no `usage` key cannot carry token counts,
            # and most rows in a transcript are tool results or UI state.
            if '"usage"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "assistant":
                continue
            message = row.get("message") or {}
            usage, mid = message.get("usage"), message.get("id")
            if not isinstance(usage, dict) or not mid:
                continue
            entry = by_id.get(mid)
            if entry is None:
                entry = by_id[mid] = {"model": message.get("model") or ""}
                order.append(mid)
            for key, value in usage.items():
                if isinstance(value, int):
                    entry[key] = max(entry.get(key, 0), value)
    return [by_id[mid] for mid in order]


def _usage_of(path: Path) -> Usage:
    messages = _assistant_messages(path)
    if not messages:
        return Usage()
    usage = Usage(messages=len(messages))
    previous_read: int | None = None
    for message in messages:
        model = message.get("model") or ""
        plain = message.get("input_tokens", 0)
        write = message.get("cache_creation_input_tokens", 0)
        read = message.get("cache_read_input_tokens", 0)
        out = message.get("output_tokens", 0)
        usage.input += plain
        usage.cache_write += write
        usage.cache_read += read
        usage.output += out
        usage.context_peak = max(usage.context_peak, plain + write + read)
        # A turn boundary shows up as the cache going BACKWARDS: this call read less
        # of the prefix than the one before it did. Counting the drops rather than
        # thresholding the write size keeps this free of magic numbers, and it lands
        # on exactly (turns - 1) for every work order measured.
        if previous_read is not None and read < previous_read:
            usage.resume_boundaries += 1
        previous_read = read
        in_rate, out_rate = price_for(model)
        cost = (
            plain * in_rate
            + write * in_rate * CACHE_WRITE_RATE
            + read * in_rate * CACHE_READ_RATE
            + out * out_rate
        ) / 1e6
        if cost:
            usage.cost_by_model[model] = usage.cost_by_model.get(model, 0.0) + cost
    usage.rewrite_excess = max(0, usage.cache_write - usage.context_peak)
    return usage


def index_sessions(root: Path | None = None) -> dict[str, Path]:
    """Map every session id Claude Code has a transcript for to that transcript.

    Built in one pass rather than globbing per work order: session ids are UUIDs and
    so globally unique, but the directory they live under is the slugified cwd the
    session was CREATED in, which for a worker is its worktree — not something Jarvis
    can reconstruct reliably once the worktree is gone.
    """
    root = root or transcript_root()
    index: dict[str, Path] = {}
    if not root.is_dir():
        return index
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        for path in project_dir.glob("*.jsonl"):
            index[path.stem] = path
    return index


def read_session(session_id: str, root: Path | None = None,
                 index: dict[str, Path] | None = None) -> SessionUsage:
    """Spend for one session id, subagents included but reported separately."""
    result = SessionUsage(session_id=session_id)
    if index is None:
        index = index_sessions(root)
    path = index.get(session_id)
    if path is None:
        return result
    result.found = True
    result.main = _usage_of(path)
    # Claude Code writes each subagent's own transcript beside the parent's, under a
    # directory named for the parent session.
    subagent_dir = path.with_suffix("") / "subagents"
    if subagent_dir.is_dir():
        for sub in sorted(subagent_dir.glob("*.jsonl")):
            sub_usage = _usage_of(sub)
            if sub_usage.messages:
                result.subagents = result.subagents + sub_usage
                result.subagent_count += 1
    return result
