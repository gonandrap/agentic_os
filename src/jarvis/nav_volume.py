"""How the fleet NAVIGATES code, measured by side — `jarvis navigation`.

§2 of
docs/superpowers/specs/2026-10-02-subagent-cache-anatomy-and-the-navigation-split.md,
and the successor to the hand-run script of
docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md §5.3, whose BEFORE
figures were 0 symbol calls and 41.3% of 14,558 MB of read volume.

A LEAF MODULE. It imports `usage`, `catalog` and the stdlib-only `navigation` and
nothing else: it never opens the OS database and never imports `ops`, `project_store` or
`inspection` — `inspection`'s constraint, for its reason, that a report over files on
disk must not fail because a catalog or a database moved.

The classifier lives in `navigation` (§3 of the same spec, Neo q1238): this module
counts, and decides nothing about what a command means.

**The side comes from the PATH.** No transcript row carries `isSidechain`:
`<slug>/<uuid>.jsonl` is a LEAD and `<slug>/<uuid>/subagents/agent-*.jsonl` is a
SUBAGENT, which is the layout `usage.read_session` already relies on. Nothing is
inferred from content and no vendor field is invented.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog import NavigationConfig
from . import navigation
from . import usage

SIDE_LEAD = "lead"
SIDE_SUBAGENT = "subagent"
SIDES = (SIDE_LEAD, SIDE_SUBAGENT)

#: The figure every reading of this report is a comparison against, carried in the
#: PAYLOAD and not only in a renderer's prose: a share with nothing to compare it to is
#: a number, and the question after `worker.bash_first: off` is the trend.
BEFORE_NOTE = ("before `worker.bash_first: off`: 0 symbol calls, 41.3% of 14,558 MB of "
               "read volume (spec 2026-10-01 §5.3)")

#: `mcp__<server>__` — stripped before a tool name is matched, because both
#: `mcp__serena__` and `mcp__plugin_serena_serena__` exist in this fleet
#: (`dispatch.SERENA_TOOL_PREFIXES`).
MCP_PREFIX = navigation.MCP_PREFIX

#: The `Read` tool, counted apart: it is navigation the OS does not classify by command,
#: and keeping it separate is what lets the Bash share be read as a Bash share.
READ_TOOL = "Read"


@dataclass
class SideVolume:
    """One side's navigation behaviour over one or more transcripts."""

    side: str
    transcripts: int = 0
    symbol_calls: int = 0          # Serena symbol tools, either prefix
    text_search_calls: int = 0     # the `Grep`/`Glob` TOOLS
    nav_bash_calls: int = 0        # Bash whose command is a read/search
    code_nav_bash_calls: int = 0   # ...of a path with a configured code suffix
    other_bash_calls: int = 0
    read_tool_calls: int = 0       # the `Read` tool
    result_bytes: int = 0          # every attributed `tool_result`
    nav_bash_bytes: int = 0
    code_nav_bash_bytes: int = 0
    symbol_bytes: int = 0
    #: `tool_result` bytes whose `tool_use_id` matched no `tool_use` in the same file.
    #: REPORTED, never silently dropped and never in a share's numerator.
    unattributed_bytes: int = 0

    def code_nav_share(self) -> float | None:
        """`code_nav_bash_bytes / result_bytes`, or None on an empty corpus.

        None and NEVER 0.0: a zero share is a finding and an unmeasured one is not,
        which is `usage.rewrite_ttl_share`'s rule.
        """
        if not self.result_bytes:
            return None
        return self.code_nav_bash_bytes / self.result_bytes

    def as_dict(self) -> dict[str, Any]:
        return {"side": self.side,
                "transcripts": self.transcripts,
                "symbol_calls": self.symbol_calls,
                "text_search_calls": self.text_search_calls,
                "nav_bash_calls": self.nav_bash_calls,
                "code_nav_bash_calls": self.code_nav_bash_calls,
                "other_bash_calls": self.other_bash_calls,
                "read_tool_calls": self.read_tool_calls,
                "result_bytes": self.result_bytes,
                "nav_bash_bytes": self.nav_bash_bytes,
                "code_nav_bash_bytes": self.code_nav_bash_bytes,
                "symbol_bytes": self.symbol_bytes,
                "unattributed_bytes": self.unattributed_bytes,
                "code_nav_share": self.code_nav_share()}


def _empty_sides() -> dict[str, SideVolume]:
    """Both keys always present, so a renderer never tests for one."""
    return {side: SideVolume(side=side) for side in SIDES}


@dataclass
class NavigationVolume:
    """One scope's navigation, split by side."""

    scope: str
    found: bool = False
    sides: dict[str, SideVolume] = field(default_factory=_empty_sides)
    window_days: int | None = None

    def fold(self, other: NavigationVolume) -> None:
        """Add another scope's reading into this one, side by side.

        What a feature order needs: one report over a planner and every child, with the
        sides still apart. `found` is an OR — one unmeasurable unit does not make the
        rollup unmeasured.
        """
        self.found = self.found or other.found
        for side in SIDES:
            _merge(self.sides[side], other.sides[side])

    def as_dict(self) -> dict[str, Any]:
        return {"scope": self.scope,
                "found": self.found,
                "window_days": self.window_days,
                "sides": {side: self.sides[side].as_dict() for side in SIDES},
                "before": BEFORE_NOTE}


def _merge(dst: SideVolume, src: SideVolume) -> None:
    """Add one transcript's reading into a side's running total."""
    for name, value in vars(src).items():
        if name == "side":
            continue
        setattr(dst, name, getattr(dst, name) + value)


def _content_bytes(block: dict[str, Any]) -> int:
    """How much a `tool_result` block put back into the conversation.

    A result's `content` is a string on most tools and a list of blocks on some; both
    are real and neither is an error, so the list is serialised rather than skipped.
    """
    content = block.get("content")
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, separators=(",", ":"), default=str))


def read_transcript(path: Path, side: str, cfg: NavigationConfig) -> SideVolume:
    """One transcript's navigation, in ONE pass.

    One pass and no `needle`: both row kinds are needed — the `tool_use` blocks name the
    call and the `tool_result` blocks carry the volume — and two filtered passes read the
    file twice.
    """
    vol = SideVolume(side=side, transcripts=1)
    produced: dict[str, tuple[str, bool]] = {}
    for row in usage.rows(path):
        kind = row.get("type")
        if kind == "assistant":
            for block in usage.blocks_of(row, "tool_use"):
                tool_id = str(block.get("id") or "")
                name = str(block.get("name") or "")
                is_nav = False
                if navigation.is_symbol_call(name, tuple(cfg.symbol_tools)):
                    vol.symbol_calls += 1
                elif name in tuple(cfg.text_search_tools):
                    vol.text_search_calls += 1
                elif name == READ_TOOL:
                    vol.read_tool_calls += 1
                elif name == "Bash":
                    command = str((block.get("input") or {}).get("command") or "")
                    is_nav = navigation.navigates_source(
                        command, tuple(cfg.code_suffixes), tuple(cfg.bash_commands))
                    if is_nav:
                        # §3: under the strict classifier the two counters are the same
                        # reading — a source-navigation call IS a code-targeting one.
                        vol.nav_bash_calls += 1
                        vol.code_nav_bash_calls += 1
                    else:
                        vol.other_bash_calls += 1
                if tool_id:
                    produced[tool_id] = (name, is_nav)
            continue
        for block in usage.blocks_of(row, "tool_result"):
            size = _content_bytes(block)
            if not size:
                continue
            origin = produced.get(str(block.get("tool_use_id") or ""))
            if origin is None:
                # Never dropped and never in a share's numerator.
                vol.unattributed_bytes += size
                continue
            name, is_nav = origin
            vol.result_bytes += size
            if is_nav:
                vol.nav_bash_bytes += size
                vol.code_nav_bash_bytes += size
            if navigation.is_symbol_call(name, tuple(cfg.symbol_tools)):
                vol.symbol_bytes += size
    return vol


def _subagents_of(path: Path) -> list[Path]:
    """The subagent transcripts Claude Code wrote beside one lead file."""
    directory = path.with_suffix("") / "subagents"
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.jsonl"))


def read_session(session_id: str, cfg: NavigationConfig, *,
                 index: dict[str, list[Path]] | None = None) -> NavigationVolume:
    """One session's navigation — the per-order path, and `jarvis inspect`'s section.

    `index` is `usage.index_sessions()`, passed in by a caller that already holds it: a
    session id is a UUID but the directory it lives under is the slugified cwd it was
    created in, which Jarvis cannot reconstruct once a worktree is gone.
    """
    vol = NavigationVolume(scope=session_id)
    if index is None:
        index = usage.index_sessions()
    paths = index.get(session_id) or []
    if not paths:
        return vol
    vol.found = True
    for path in sorted(paths):
        _merge(vol.sides[SIDE_LEAD], read_transcript(path, SIDE_LEAD, cfg))
        for sub in _subagents_of(path):
            _merge(vol.sides[SIDE_SUBAGENT],
                   read_transcript(sub, SIDE_SUBAGENT, cfg))
    return vol


def slug_of(path: Path | str) -> str:
    """The directory name Claude Code derives from a cwd.

    Every non-alphanumeric character becomes a dash, which is what makes
    `/home/x/.claude/jobs` read as `-home-x--claude-jobs` on disk. Used to scope a wide
    walk to ONE project: a worker's worktree lives under the project path, so the
    project's slug is a prefix of its workers' slugs.
    """
    return "".join(c if c.isalnum() else "-" for c in str(path))


def read_tree(root: Path | None = None, cfg: NavigationConfig | None = None, *,
              days: int | None = None,
              slug_prefix: str = "") -> NavigationVolume:
    """Every transcript under one root, split by side — the wide path.

    `days` filters by file mtime BEFORE anything is opened: `~/.claude/projects` is 2.7G
    with 11,889 lead transcripts, so the window is what makes a wide scope affordable at
    all. None reads everything, which is what a caller asking for all history means.

    `slug_prefix` is how a PROJECT scope is read without enumerating its orders: the
    record cannot name a session whose worktree is gone (`usage.index_sessions`' reason),
    but the slug a transcript lives under still carries the cwd it was created in.
    """
    cfg = cfg or NavigationConfig()
    root = Path(root) if root is not None else usage.transcript_root()
    vol = NavigationVolume(scope="fleet", window_days=days)
    if not root.is_dir():
        return vol
    cutoff = (time.time() - days * 86400) if days else None

    def fresh(path: Path) -> bool:
        if cutoff is None:
            return True
        try:
            return path.stat().st_mtime >= cutoff
        except OSError:
            return False

    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        if slug_prefix and not project_dir.name.startswith(slug_prefix):
            continue
        for path in sorted(project_dir.glob("*.jsonl")):
            if fresh(path):
                vol.found = True
                _merge(vol.sides[SIDE_LEAD], read_transcript(path, SIDE_LEAD, cfg))
        for sub in sorted(project_dir.glob("*/subagents/agent-*.jsonl")):
            if fresh(sub):
                vol.found = True
                _merge(vol.sides[SIDE_SUBAGENT],
                       read_transcript(sub, SIDE_SUBAGENT, cfg))
    return vol
