"""What Jarvis put in the worker's context window, per turn, and what changed.

§5 of docs/specs/2026-09-24-order-observability.md. Nothing on disk answers "how much of
this context is system prompt, how much is skills, how much is the knowledge block", so
this module measures the ingredients JARVIS ITSELF supplies at the moment a turn is
launched, and `record` stores one payload per turn on `wo_turns.context_json`.

THREE PROPERTIES THIS FILE IS BUILT AROUND, all three of them corrections to defects the
knowledge base already names:

* EVERY TOKEN FIGURE IS AN ESTIMATE (bytes / `TOKEN_BYTES`) and every row says so. There
  is no byte-exact figure for the rendered system prompt — it does not exist, and
  claiming one is the defect kn-0cb81cec warns about. So bytes are measured, tokens are
  derived, and `estimated` is on the row rather than in a docstring.
* ABSENT IS NEVER ZERO (issue #227). An ingredient that is not in this turn's window, or
  that could not be read, carries `bytes`/`tokens` of None and a sentence saying which.
  A 0 would read as "it was there and it was empty".
* CLAUDE CODE'S HALF IS NOT OURS TO MEASURE. The base system prompt and the tool schemas
  render at position 0 inside the CLI; `residual` INFERS them from the observed cold-start
  cache write and is labelled `measured: False`. A remainder is not a measurement.

Leaf-ish by construction: `dispatch` and `worker_session` are imported lazily inside
functions only (they import this module's callers — a module-level import is a cycle).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.context")

#: Bytes per token, the estimator's whole model. Four is the usual English-prose figure;
#: it is deliberately crude and deliberately published on every payload (`token_bytes`),
#: because a reader who knows the divisor can redo the arithmetic and a reader handed an
#: unexplained token count cannot (kn-0cb81cec).
TOKEN_BYTES = 4

#: The payload's shape version, so a later reader can tell a v1 row from its successor
#: without guessing from the keys present.
SCHEMA = 1

#: Walk ceilings. A measurement must cost less than the thing it measures: `add_dirs`
#: points at the skills tree, which is hundreds of files, and `memory_files` at a walk
#: that can climb out of a shallow checkout. Both caps are reported in the payload
#: (`caps`) with their VALUES, so a truncated count is legible as truncated rather than
#: as a smaller prompt.
MAX_FILES = 400
MAX_BYTES = 4_000_000

#: The inferred row's name, fixed so a reader can find it across turns.
RESIDUAL = "claude_code_base"


def caps() -> dict[str, int]:
    """The ceilings this measurement ran under — on the payload, not only in the code."""
    return {"max_files": MAX_FILES, "max_bytes": MAX_BYTES}


def _tokens(nbytes: int | None) -> int | None:
    return None if nbytes is None else nbytes // TOKEN_BYTES


def _row(name: str, nbytes: int | None, *, detail: dict[str, Any] | None = None,
         note: str = "", measured: bool = True) -> dict[str, Any]:
    """One ingredient row. `bytes is None` is the absent/unreadable case, never 0."""
    return {"name": name, "bytes": nbytes, "tokens": _tokens(nbytes),
            "estimated": True, "measured": measured, "absent": nbytes is None,
            "detail": detail or {}, "note": note}


def _absent(name: str, note: str, detail: dict[str, Any] | None = None
            ) -> dict[str, Any]:
    """An ingredient that is NOT in this turn's window. A row, so the reader sees the
    sentence; never a 0, which would claim it was there and empty (issue #227)."""
    return _row(name, None, detail=detail, note=note)


def _text_bytes(text: str | None) -> int:
    return len((text or "").encode("utf-8", "replace"))


def _file_bytes(path: Path) -> tuple[int | None, str]:
    try:
        return len(path.read_bytes()), ""
    except OSError as e:
        return None, f"could not be read ({e.__class__.__name__}: {e})"


def _worktree_cwd(project: Any, wo: dict[str, Any]) -> Path:
    """The directory the turn runs in — where Claude Code resolves CLAUDE.md from.

    Derived here rather than from `worker_session.worktree_path` to keep this module off
    that import (cycle); the fallback to the project root is the same one `send` makes.
    """
    name = wo.get("worktree")
    if name:
        path = Path(project.path) / ".claude" / "worktrees" / str(name)
        if path.is_dir():
            return path
    return Path(project.path)


def _knowledge_block(knowledge: Any, project_name: str) -> str:
    """The rendered block, re-rendered from the brief dispatch already has.

    Re-rendered rather than sliced out of the prompt by guesswork: `render_knowledge_block`
    is the only thing that knows what the block is, so measuring its own output is exact.
    """
    from .dispatch import render_knowledge_block  # lazy: dispatch imports this module

    return "\n".join(render_knowledge_block(knowledge, project_name))


def _walk(dirs: list[Path]) -> tuple[int, dict[str, Any]]:
    """Bytes under `dirs`, under both caps. Returns the total and what it covered."""
    total = files = 0
    truncated = False
    for d in dirs:
        for path in sorted(Path(d).rglob("*")):
            if not path.is_file():
                continue
            if files >= MAX_FILES or total >= MAX_BYTES:
                truncated = True
                break
            size, _ = _file_bytes(path)
            if size is None:
                continue
            total += size
            files += 1
    return total, {"dirs": [str(d) for d in dirs], "files": files,
                   "truncated": truncated}


def _mcp_row(project: Any) -> dict[str, Any]:
    """The MCP server SET — names and a count, and NO byte figure.

    Tool schemas render at position 0 of the context window and Claude Code composes
    them; Jarvis supplies the server list and nothing else, so a size here would be
    invented. kn-2c41d4cc's blind spot: name what you cannot measure instead of
    reporting a zero for it.

    Read from the wiring inventory's CACHE only (`wiring.peek`). Triggering a discovery
    would run `claude mcp list` inside the dispatch path, which is a subprocess spawned
    by a measurement.
    """
    from . import wiring

    note = ("the SET only — tool schemas render at position 0 and are Claude Code's to "
            "compose, not Jarvis's to measure")
    inv = wiring.peek()
    if inv is None:
        return _row("mcp_servers", None,
                    detail={"read": False},
                    note=f"{note}; the wiring inventory has not been read yet, so not "
                         f"even the names are known here")
    names = sorted(item.name for item in wiring.applied(project.wiring, inv).items
                   if item.kind == "mcp" and item.wired)
    return _row("mcp_servers", None, detail={"read": True, "names": names,
                                             "count": len(names)}, note=note)


def measure(project: Any, wo: dict[str, Any], turn: dict[str, Any],
            briefing: dict[str, Any], knowledge: Any = None) -> list[dict[str, Any]]:
    """Every ingredient of this turn's window that Jarvis itself supplied.

    `briefing` is `worker_session.briefing_for`'s dict, passed IN rather than rebuilt:
    rebuilding it rewrites the worker settings file, which is a side effect a measurement
    must not have.

    `knowledge` is the `KnowledgeBrief` only on the dispatch turn — later turns carry no
    knowledge block at all, and that ingredient is then absent-with-a-note rather than 0.
    """
    rows: list[dict[str, Any]] = []
    append = str(briefing.get("append_system_prompt") or "")
    rows.append(_row("append_system_prompt", _text_bytes(append),
                     detail={"lines": len(append.splitlines())},
                     note="Jarvis's git briefing plus the standing instructions, on "
                          "--append-system-prompt"))

    prompt = str(turn.get("prompt") or "")
    # `if knowledge` and not `is not None`: a `KnowledgeBrief` is falsy when the base has
    # nothing visible to the project, and `build_worker_prompt` appends no block for a
    # falsy one — measuring the render anyway would charge the prompt for text that was
    # never in it.
    if knowledge:
        block = _knowledge_block(knowledge, project.name)
        if prompt.endswith(block):
            rows.append(_row("worker_prompt", _text_bytes(prompt) - _text_bytes(block),
                             note="the turn's prompt, with the knowledge block counted "
                                  "separately below"))
            rows.append(_row("knowledge_index", _text_bytes(block),
                             detail={"entries": getattr(knowledge, "total", None)},
                             note="the knowledge INDEX block "
                                  "`render_knowledge_block` appends to the dispatch "
                                  "prompt"))
        else:
            rows.append(_row("worker_prompt", _text_bytes(prompt),
                             note="the turn's prompt WHOLE: the knowledge block could "
                                  "not be separated from it, so it is counted here and "
                                  "not twice"))
            rows.append(_absent("knowledge_index",
                                "this turn's prompt does not end in the rendered block, "
                                "so its size is unknown here and is counted inside "
                                "`worker_prompt` — absent, not empty",
                                detail={"entries": getattr(knowledge, "total", None)}))
    else:
        rows.append(_row("worker_prompt", _text_bytes(prompt),
                         note="the turn's prompt"))
        rows.append(_absent("knowledge_index",
                            "no knowledge block on this turn — it is appended to the "
                            "dispatch prompt only, and only when the base has entries "
                            "visible to this project, so it is absent here rather than "
                            "empty"))

    settings = Path(project.path) / ".jarvis" / "worker-settings" / f"{wo['id']}.json"
    size, why = _file_bytes(settings)
    rows.append(_row("worker_settings", size, detail={"path": str(settings)},
                     note=why or "the --settings file: hooks, permissions, env"))

    rows.append(_memory_row(project, wo))
    rows.append(_agent_row(briefing))
    rows.append(_add_dirs_row(briefing))
    rows.append(_mcp_row(project))
    return rows


def _memory_row(project: Any, wo: dict[str, Any]) -> dict[str, Any]:
    from . import hooks

    cwd = _worktree_cwd(project, wo)
    try:
        paths = hooks.memory_files(Path(project.path), cwd)
    except OSError as e:
        return _absent("memory_files", f"the memory walk could not be read ({e})")
    total = 0
    counted: list[str] = []
    for path in paths[:MAX_FILES]:
        size, _ = _file_bytes(path)
        if size is None:
            continue
        total += min(size, max(0, MAX_BYTES - total))
        counted.append(str(path))
    return _row("memory_files", total,
                detail={"paths": counted, "count": len(counted), "cwd": str(cwd)},
                note="the CLAUDE.md-shaped files Claude Code loads, nearest first")


def _agent_row(briefing: dict[str, Any]) -> dict[str, Any]:
    name = briefing.get("agent")
    if not name:
        return _absent("agent_persona",
                       "this work order runs as no agent type, so there is no persona "
                       "file in its window")
    for d in briefing.get("add_dirs") or []:
        path = Path(d) / ".claude" / "agents" / f"{name}.md"
        if path.is_file():
            size, why = _file_bytes(path)
            return _row("agent_persona", size,
                        detail={"agent": name, "path": str(path)},
                        note=why or "the feature's own agent definition (--agent)")
    return _absent("agent_persona",
                   f"agent {name!r} is on the argv and its definition file was not "
                   f"found under any --add-dir, so its size is unknown",
                   detail={"agent": name})


def _add_dirs_row(briefing: dict[str, Any]) -> dict[str, Any]:
    dirs = [Path(d) for d in (briefing.get("add_dirs") or []) if Path(d).is_dir()]
    if not dirs:
        return _absent("add_dirs", "no --add-dir on this turn")
    total, detail = _walk(dirs)
    return _row("add_dirs", total, detail=detail,
                note="the skills, seats and feature-agent trees reachable through "
                     "--add-dir" + (" (truncated at the cap)" if detail["truncated"]
                                    else ""))


def payload(project: Any, wo: dict[str, Any], turn: dict[str, Any],
            briefing: dict[str, Any], knowledge: Any = None) -> dict[str, Any]:
    """What `record` stores: the measurement plus everything needed to read it back."""
    return {"schema": SCHEMA, "caps": caps(), "token_bytes": TOKEN_BYTES,
            "ingredients": measure(project, wo, turn, briefing, knowledge)}


def record(store: Any, project: Any, wo: dict[str, Any], turn: dict[str, Any],
           briefing: dict[str, Any], knowledge: Any = None) -> None:
    """The ONE writer of `wo_turns.context_json`. Never raises into the dispatch path.

    Neo's ruling on question 681 partitions the two call sites so this runs EXACTLY ONCE
    per turn: `worker_session._launch` records every turn except the seq-1 dispatch turn,
    and `dispatch.dispatch_work_order` records that one (it is the only place holding the
    `KnowledgeBrief`). A retried dispatch turn takes a fresh seq, so it falls to
    `_launch` and is still recorded once.

    A failed measurement writes NOTHING and the column stays NULL — "not recorded", which
    the read path already renders as a sentence. A measurement that could stop a turn
    from running would be worth strictly less than the turn.
    """
    try:
        import json as _json

        store.conn.execute("UPDATE wo_turns SET context_json=? WHERE id=?",
                           (_json.dumps(payload(project, wo, turn, briefing, knowledge)),
                            turn["id"]))
        store.conn.commit()
    except Exception:  # noqa: BLE001 — see the docstring: never into the dispatch path
        log.warning("could not record the context ledger for %s turn %s",
                    wo.get("id"), turn.get("seq"), exc_info=True)


def measured_tokens(ingredients: list[dict[str, Any]]) -> int:
    """The sum of the estimated token counts actually measured. Absent rows contribute
    nothing — not 0 as a value, but no term at all."""
    return sum(int(row["tokens"]) for row in ingredients
               if row.get("measured") and row.get("tokens") is not None)


def _written(write: Any) -> int | None:
    """`inspection.Write.written`, from the object or from its `as_dict`."""
    if write is None:
        return None
    if isinstance(write, dict):
        value = write.get("written")
    else:
        value = getattr(write, "written", None)
    return None if value is None else int(value)


def residual(measured: int, cold_start_write: Any) -> dict[str, Any]:
    """Claude Code's own base prompt and tool schemas, INFERRED — `measured: False`.

    Observed cold-start prefix minus the sum of what Jarvis measured. The label is the
    point: a remainder is not a measurement, and this row is the only thing in the
    payload that is not one (§5, "The residual is inferred and must be labelled as
    such").
    """
    written = _written(cold_start_write)
    if written is None:
        return {"name": RESIDUAL, "bytes": None, "tokens": None, "estimated": True,
                "measured": False, "absent": True, "detail": {},
                "note": "no cold-start cache write was found for this turn, so Claude "
                        "Code's own share of the window cannot be inferred — absent, "
                        "not zero"}
    remainder = written - measured
    if remainder < 0:
        return {"name": RESIDUAL, "bytes": None, "tokens": None, "estimated": True,
                "measured": False, "absent": True,
                "detail": {"observed_prefix_tokens": written,
                           "measured_tokens": measured},
                "note": f"the estimate ({measured:,} tokens) EXCEEDS the observed "
                        f"prefix ({written:,} tokens), so the residual is unknown "
                        f"rather than negative or zero"}
    return {"name": RESIDUAL, "bytes": None, "tokens": remainder, "estimated": True,
            "measured": False, "absent": False,
            "detail": {"observed_prefix_tokens": written, "measured_tokens": measured},
            "note": "INFERRED, not measured: the observed cold-start prefix minus "
                    "everything Jarvis could account for — Claude Code's base system "
                    "prompt and its tool schemas"}


def delta(current: list[dict[str, Any]], previous: list[dict[str, Any]],
          against_seq: int | None = None) -> dict[str, Any]:
    """Per-ingredient change against the PREVIOUS RECORDED turn.

    An ingredient present on one side only is `appeared`/`disappeared` and never a change
    from 0 — the knowledge block leaving the window between turn 1 and turn 2 is the
    normal case, and reporting it as "-4.2k" would say the prompt shrank by a block that
    was never in it.
    """
    before = {row["name"]: row for row in previous}
    after = {row["name"]: row for row in current}
    rows: list[dict[str, Any]] = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        old_bytes = None if old is None else old.get("bytes")
        new_bytes = None if new is None else new.get("bytes")
        if old is None:
            rows.append({"name": name, "kind": "appeared", "bytes_delta": None,
                         "tokens_delta": None, "bytes_before": None,
                         "bytes_after": new_bytes,
                         "note": "not in the previous turn's payload at all"})
        elif new is None:
            rows.append({"name": name, "kind": "disappeared", "bytes_delta": None,
                         "tokens_delta": None, "bytes_before": old_bytes,
                         "bytes_after": None,
                         "note": "recorded on the previous turn and not on this one"})
        elif old_bytes is None and new_bytes is None:
            # Absent on BOTH turns — no persona, no MCP inventory read. Neither an
            # appearance nor a change; saying "appeared" here would invent an event.
            rows.append({"name": name, "kind": "absent", "bytes_delta": None,
                         "tokens_delta": None, "bytes_before": None,
                         "bytes_after": None,
                         "note": "absent on both turns, so nothing changed"})
        elif old_bytes is None or new_bytes is None:
            appeared = old_bytes is None
            rows.append({"name": name,
                         "kind": "appeared" if appeared else "disappeared",
                         "bytes_delta": None, "tokens_delta": None,
                         "bytes_before": old_bytes, "bytes_after": new_bytes,
                         "note": ("absent on the previous turn and measured on this one"
                                  if appeared else
                                  "measured on the previous turn and absent on this one")
                         + ", so this is an appearance rather than a change"})
        else:
            rows.append({"name": name, "kind": "change",
                         "bytes_delta": new_bytes - old_bytes,
                         "tokens_delta": _tokens(new_bytes) - _tokens(old_bytes),
                         "bytes_before": old_bytes, "bytes_after": new_bytes,
                         "note": ""})
    grew = [r for r in rows if r["kind"] == "change" and (r["bytes_delta"] or 0) > 0]
    biggest = max(grew, key=lambda r: r["bytes_delta"])["name"] if grew else None
    return {"against_seq": against_seq, "rows": rows, "biggest_growth": biggest,
            "grew": [r["name"] for r in sorted(grew, key=lambda r: -r["bytes_delta"])],
            "appeared": [r["name"] for r in rows if r["kind"] == "appeared"],
            "disappeared": [r["name"] for r in rows if r["kind"] == "disappeared"]}
