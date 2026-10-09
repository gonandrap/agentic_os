"""How the fleet NAVIGATES code, measured by side — `jarvis navigation`.

§2 of
docs/superpowers/specs/2026-10-02-subagent-cache-anatomy-and-the-navigation-split.md,
and the successor to the hand-run script of
docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md §5.3, whose BEFORE
figures do not reproduce (Neo q1246) — `BEFORE_NOTE` carries the restatement under the
strict classifier.

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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .catalog import NavigationConfig
from . import navigation
from . import usage

SIDE_LEAD = "lead"
SIDE_SUBAGENT = "subagent"
SIDES = (SIDE_LEAD, SIDE_SUBAGENT)

#: The figure every reading of this report is a comparison against, carried in the
#: PAYLOAD and not only in a renderer's prose: a share with nothing to compare it to is
#: a number, and the question after `worker.bash_first: off` is the trend.
BEFORE_NOTE = (
    "baseline restated under the strict classifier over a DIFFERENT corpus from spec "
    "2026-10-01 §5.3, whose 41.3% of 14,558 MB over 276 transcripts does not "
    "reproduce (Neo q1246) — `jarvis navigation --project jarvis_os --days 36500`, "
    "all history, measured 2026-10-05: lead 1 symbol call, 38.2% of 72.2 MB of "
    "tool-result bytes over 351 transcripts; subagent 1,148 symbol calls, 34.7% of "
    "58.6 MB over 439 transcripts"
)

#: The SECOND baseline, separately attributable: `BEFORE_NOTE` was measured under the
#: strict source classifier over a different corpus and is left byte-identical (§2.2), so
#: the doc figures travel beside it rather than inside it. A RECORDED BASELINE and not an
#: assertion — whether doc reads fell is unprovable until a later order flips
#: `worker.doc_nav_hook` and a window passes.
#: docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md §1 and §3.2.
DOC_BEFORE_NOTE = (
    "doc baseline recorded 2026-10-06 over 877 transcripts under "
    "`~/.claude/projects/*agentic*/`, tokens as `chars // 4`: `.md` Read with no "
    "`limit` 1,032,302 tok over 199 calls; `.md` Bash dumps (sed+cat+head) 1,252,687 "
    "tok over 2,189 calls; `.py` Read with no `limit` 487,048 tok over 133 calls. A "
    "RECORDED BASELINE, not an assertion: reproduce a number today with `jarvis "
    "navigation --project jarvis_os --days 36500`, whose per-side `doc_read_bytes`, "
    "`doc_dump_bash_bytes` and no-`limit` Read counts are the same reading; whether doc "
    "reads FELL is unprovable until a later order flips `worker.doc_nav_hook` and a "
    "window passes"
)

#: `mcp__<server>__` — stripped before a tool name is matched, because both
#: `mcp__serena__` and `mcp__plugin_serena_serena__` exist in this fleet
#: (`dispatch.SERENA_TOOL_PREFIXES`).
MCP_PREFIX = navigation.MCP_PREFIX

#: The `Read` tool, counted apart: it is navigation the OS does not classify by command,
#: and keeping it separate is what lets the Bash share be read as a Bash share.
READ_TOOL = "Read"

#: What a SUBAGENT transcript is called, named ONCE and used by both readers: the
#: per-order path and the wide walk disagreeing about this made the two reports
#: incomparable for the same session (PR 927 review). The layout this module's docstring
#: documents, and `usage.read_session`'s.
SUBAGENT_GLOB = "agent-*.jsonl"


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
    # docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md §3.2.
    read_tool_bytes: int = 0       # the `Read` tool's own result bytes
    doc_read_bytes: int = 0        # ...of a path with a configured doc suffix
    whole_file_read_calls: int = 0  # `Read` with no `limit` in its input
    doc_dump_bash_calls: int = 0   # Bash that `dumps_doc`, with NO limit_lines
    doc_dump_bash_bytes: int = 0
    #: Per-tool result bytes, nothing in the tree reports this. A `Counter` AND NOT A
    #: `dict[str, int]`: `_merge` folds every field with `+` over `vars()`, where a plain
    #: dict raises `TypeError` and `Counter + Counter` folds. Do not simplify it.
    bytes_by_tool: Counter[str] = field(default_factory=Counter)

    def code_nav_share(self) -> float | None:
        """`code_nav_bash_bytes / result_bytes`, or None on an empty corpus.

        None and NEVER 0.0: a zero share is a finding and an unmeasured one is not,
        which is `usage.rewrite_ttl_share`'s rule.
        """
        if not self.result_bytes:
            return None
        return self.code_nav_bash_bytes / self.result_bytes

    def as_dict(self, *, calls_reported: bool = True) -> dict[str, Any]:
        # Neo q1242: per-order call counts are `inspection.nav_profile`'s sealed-span
        # ones, so the per-order projection omits these six.
        calls = {"symbol_calls": self.symbol_calls,
                 "text_search_calls": self.text_search_calls,
                 "nav_bash_calls": self.nav_bash_calls,
                 "code_nav_bash_calls": self.code_nav_bash_calls,
                 "other_bash_calls": self.other_bash_calls,
                 "read_tool_calls": self.read_tool_calls,
                 # §3.2: new CALL counters inside the gate, new BYTE counters outside it.
                 "whole_file_read_calls": self.whole_file_read_calls,
                 "doc_dump_bash_calls": self.doc_dump_bash_calls}  \
            if calls_reported else {}
        return {"side": self.side,
                "transcripts": self.transcripts,
                **calls,
                "result_bytes": self.result_bytes,
                "nav_bash_bytes": self.nav_bash_bytes,
                "code_nav_bash_bytes": self.code_nav_bash_bytes,
                "symbol_bytes": self.symbol_bytes,
                "unattributed_bytes": self.unattributed_bytes,
                "read_tool_bytes": self.read_tool_bytes,
                "doc_read_bytes": self.doc_read_bytes,
                "doc_dump_bash_bytes": self.doc_dump_bash_bytes,
                # Sorted and plain, so the JSON payload is stable.
                "bytes_by_tool": dict(sorted(self.bytes_by_tool.items())),
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
    #: Neo q1242: false on the PER-ORDER reader, whose call counts are the sealed-span
    #: ones in `inspection.nav_profile`.
    calls_reported: bool = True

    def fold(self, other: NavigationVolume) -> None:
        """Add another scope's reading into this one, side by side.

        What a feature order needs: one report over a planner and every child, with the
        sides still apart. `found` is an OR — one unmeasurable unit does not make the
        rollup unmeasured.
        """
        self.found = self.found or other.found
        # Neo q1242: a rollup of per-order volumes is still per-order.
        self.calls_reported = self.calls_reported and other.calls_reported
        for side in SIDES:
            _merge(self.sides[side], other.sides[side])

    def as_dict(self) -> dict[str, Any]:
        return {"scope": self.scope,
                "found": self.found,
                "window_days": self.window_days,
                "calls_reported": self.calls_reported,
                "sides": {side: self.sides[side].as_dict(
                    calls_reported=self.calls_reported) for side in SIDES},
                "before": BEFORE_NOTE,
                # §3.2: a second, separately attributable baseline — not an edit to the
                # first.
                "doc_before": DOC_BEFORE_NOTE}


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
    produced: dict[str, tuple[str, bool, bool, bool]] = {}
    for row in usage.rows(path):
        kind = row.get("type")
        if kind == "assistant":
            for block in usage.blocks_of(row, "tool_use"):
                tool_id = str(block.get("id") or "")
                name = str(block.get("name") or "")
                tool_input = block.get("input") or {}
                is_nav = False
                is_doc_read = False
                is_doc_dump = False
                if navigation.is_symbol_call(name, tuple(cfg.symbol_tools)):
                    vol.symbol_calls += 1
                elif name in tuple(cfg.text_search_tools):
                    vol.text_search_calls += 1
                elif name == READ_TOOL:
                    vol.read_tool_calls += 1
                    # §1(a): `limit is None` IS the predicate, from `tool_input` alone.
                    if "limit" not in tool_input:
                        vol.whole_file_read_calls += 1
                    is_doc_read = str(tool_input.get("file_path") or "").endswith(
                        tuple(cfg.doc_suffixes))
                elif name == "Bash":
                    command = str(tool_input.get("command") or "")
                    is_nav = navigation.navigates_source(
                        command, tuple(cfg.code_suffixes), tuple(cfg.bash_commands))
                    if is_nav:
                        # §3: under the strict classifier the two counters are the same
                        # reading — a source-navigation call IS a code-targeting one.
                        vol.nav_bash_calls += 1
                        vol.code_nav_bash_calls += 1
                    else:
                        vol.other_bash_calls += 1
                    # NO `limit_lines` on purpose: the baseline must keep the small reads
                    # the hook will go on allowing, or the AFTER figure shows a drop the
                    # refusal never caused (§3.2). An INDEPENDENT classification — a
                    # command can be both a source nav and a doc dump, or neither.
                    is_doc_dump = navigation.dumps_doc(
                        command, tuple(cfg.doc_suffixes),
                        tuple(cfg.doc_dump_commands))
                    if is_doc_dump:
                        vol.doc_dump_bash_calls += 1
                if tool_id:
                    produced[tool_id] = (name, is_nav, is_doc_read, is_doc_dump)
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
            name, is_nav, is_doc_read, is_doc_dump = origin
            vol.result_bytes += size
            # §3.2: every ATTRIBUTED result, so unattributed bytes stay out as they stay
            # out of every share's numerator.
            vol.bytes_by_tool[name] += size
            if is_nav:
                vol.nav_bash_bytes += size
                vol.code_nav_bash_bytes += size
            if name == READ_TOOL:
                vol.read_tool_bytes += size
                if is_doc_read:
                    vol.doc_read_bytes += size
            if is_doc_dump:
                vol.doc_dump_bash_bytes += size
            if navigation.is_symbol_call(name, tuple(cfg.symbol_tools)):
                vol.symbol_bytes += size
    return vol


def _subagents_of(path: Path) -> list[Path]:
    """The subagent transcripts Claude Code wrote beside one lead file."""
    directory = path.with_suffix("") / "subagents"
    if not directory.is_dir():
        return []
    return sorted(directory.glob(SUBAGENT_GLOB))


def read_session(session_id: str, cfg: NavigationConfig, *,
                 index: dict[str, list[Path]] | None = None) -> NavigationVolume:
    """One session's navigation — the per-order path, and `jarvis inspect`'s section.

    `index` is `usage.index_sessions()`, passed in by a caller that already holds it: a
    session id is a UUID but the directory it lives under is the slugified cwd it was
    created in, which Jarvis cannot reconstruct once a worktree is gone.
    """
    # Neo q1242: the per-order surface carries byte volumes and the side split only.
    vol = NavigationVolume(scope=session_id, calls_reported=False)
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

    THE PREFIX IS NOT A SUFFICIENT TEST, and `in_slug_scope` is what read_tree uses
    instead. This mapping is not injective: `_` and `/` both become `-`, so
    `/ws/jarvis_os` and `/ws/jarvis/os` have the SAME slug, and a bare `startswith`
    folded a sibling project's leads and subagents into its neighbour's report (PR 927
    review). Nothing derived from the slug alone can separate that pair.
    """
    return "".join(c if c.isalnum() else "-" for c in str(path))


def in_slug_scope(name: str, slug: str, exclude: Sequence[str] = ()) -> bool:
    """Does the transcript directory `name` belong to the project whose cwd slug is `slug`?

    TWO LAYERS, because `slug_of` is not injective (see its docstring):

    1. The slug EXACTLY, or the slug followed by the `-` a path separator becomes — so
       `-ws-jarvisx` is no longer read as a subdirectory of `-ws-jarvis`.
    2. Not a closer match for one of `exclude`, the slugs of the OTHER projects the
       catalog names. `/ws/jarvis_os` slugifies to `/ws/jarvis`'s slug plus `-os`, which
       layer 1 cannot tell from a subdirectory; the sibling's own longer slug can.

    An empty `slug` is the fleet scope and matches everything.
    """
    if not slug:
        return True
    if not (name == slug or name.startswith(slug + "-")):
        return False
    return not any(len(other) > len(slug)
                   and (name == other or name.startswith(other + "-"))
                   for other in exclude)


def read_tree(root: Path | None = None, cfg: NavigationConfig | None = None, *,
              days: int | None = None,
              slug_prefix: str = "",
              slug_exclude: Sequence[str] = ()) -> NavigationVolume:
    """Every transcript under one root, split by side — the wide path.

    `days` filters by file mtime BEFORE anything is opened: `~/.claude/projects` is 2.7G
    with 11,889 lead transcripts, so the window is what makes a wide scope affordable at
    all. None reads everything, which is what a caller asking for all history means.

    `slug_prefix` is how a PROJECT scope is read without enumerating its orders: the
    record cannot name a session whose worktree is gone (`usage.index_sessions`' reason),
    but the slug a transcript lives under still carries the cwd it was created in.
    Matched through `in_slug_scope` and never with a bare `startswith`; `slug_exclude`
    is the other projects' slugs, for the pair `in_slug_scope`'s layer 1 cannot split.
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
        if not in_slug_scope(project_dir.name, slug_prefix, slug_exclude):
            continue
        for path in sorted(project_dir.glob("*.jsonl")):
            if fresh(path):
                vol.found = True
                _merge(vol.sides[SIDE_LEAD], read_transcript(path, SIDE_LEAD, cfg))
            # `_subagents_of` and nothing of its own: one pattern, so this report and
            # `read_session`'s cannot disagree about one session (PR 927 review).
            for sub in _subagents_of(path):
                if fresh(sub):
                    vol.found = True
                    _merge(vol.sides[SIDE_SUBAGENT],
                           read_transcript(sub, SIDE_SUBAGENT, cfg))
    return vol
