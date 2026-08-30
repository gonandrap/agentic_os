#!/usr/bin/env python3
"""Reduce a real Claude Code transcript to the skeleton `jarvis inspect` reads.

The regression fixture for the anatomy of a turn has to be a REAL session — the three
findings it pins down (a cold start, a prefix-miss twelve seconds after the previous
call, a TTL expiry after a 450-second block) are facts about timings and token counts
that a hand-written file would only reproduce by being told the answer.

The session itself cannot be committed: 1.5 MB, and its tool results contain the whole
of the repository it was reading plus the user's and Neo's own words. So this keeps
exactly the fields the analysis consumes — the clock, the usage objects, the tool ids
and names, and enough of an injected prompt to recognise which kind it was — and drops
every payload. What survives is unreadable as prose and identical as arithmetic.

    scripts/redact_transcript.py <transcript.jsonl> <out-dir>

Re-run it when the fixture needs rebuilding; the output is committed under tests/data.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

#: Row-level fields the analysis reads. Everything else — cwd, gitBranch, version, uuid,
#: the parent chain — is either machine-identifying or unused.
KEEP = ("type", "timestamp", "promptSource", "isMeta", "requestId", "sessionId")

#: How much of an injected prompt survives. Long enough for `inspection.TRIGGERS` to
#: match on it, short enough that no sentence anyone wrote comes with it.
PROMPT_HEAD = 48


def _blocks(content: object) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def redact_row(row: dict) -> dict | None:
    """One transcript row, stripped to its skeleton, or None if it carries nothing."""
    out = {k: row[k] for k in KEEP if k in row}
    if not out.get("type"):
        return None
    message = row.get("message")
    if not isinstance(message, dict):
        # A `attachment` / `worktree-state` / UI row: kept only for its type and clock,
        # which is what proves the reader ignores them.
        return out if "timestamp" in out else None

    kept: dict = {}
    for field in ("id", "model", "usage"):
        if field in message:
            kept[field] = message[field]

    content = message.get("content")
    if isinstance(content, str):
        kept["content"] = content[:PROMPT_HEAD]
    else:
        blocks = []
        for block in _blocks(content):
            kind = block.get("type")
            if kind == "tool_use":
                payload = block.get("input") or {}
                # `description` is the agent's own label for the call and is what the
                # report prints; `task_id` is the join to a subagent. Nothing else.
                keep_input = {k: payload[k] for k in ("description", "task_id")
                              if isinstance(payload.get(k), str)}
                blocks.append({"type": kind, "id": block.get("id"),
                               "name": block.get("name"), "input": keep_input})
            elif kind == "tool_result":
                blocks.append({"type": kind, "tool_use_id": block.get("tool_use_id")})
            elif kind == "text":
                blocks.append({"type": kind, "text": block.get("text", "")[:PROMPT_HEAD]})
        kept["content"] = blocks
    out["message"] = kept
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    source, out_dir = Path(argv[1]), Path(argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / source.name
    with source.open(errors="replace") as handle, target.open("w") as sink:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            small = redact_row(row) if isinstance(row, dict) else None
            if small:
                sink.write(json.dumps(small) + "\n")

    # The subagent META files, and only those: they are what turns "blocked 450s on
    # a6e6596b" into a sentence, and they hold a type and a description and nothing else.
    subagents = source.with_suffix("") / "subagents"
    if subagents.is_dir():
        into = out_dir / source.stem / "subagents"
        into.mkdir(parents=True, exist_ok=True)
        for meta in sorted(subagents.glob("agent-*.meta.json")):
            shutil.copy(meta, into / meta.name)
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
