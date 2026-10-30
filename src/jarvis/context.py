"""What Jarvis put in the worker's context window, per turn, and what changed (docs/superpowers/specs/2026-09-24-order-observability.md)"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

# The ONE module-level jarvis import
from . import observability
from .agent_usage import OBSERVE_CONTEXT_WRITE

log = logging.getLogger("jarvis.context")

#: Bytes per token, the estimator's whole model
TOKEN_BYTES = 4

#: The payload's shape version, so a later reader can tell a v1 row from its successor without...
SCHEMA = 1

#: Walk ceilings
MAX_FILES = 400
MAX_BYTES = 4_000_000

#: The inferred row's name, fixed so a reader can find it across turns
RESIDUAL = "claude_code_base"

#: What Jarvis may put in a worker's window before it does any work... (docs/os/architecture/worker-context.md)
START_BUDGET_TOKENS = 10_000
START_INGREDIENTS = ("append_system_prompt", "worker_prompt", "knowledge_index",
                     "memory_files", "session_start_context")


def caps() -> dict[str, int]:
    """The ceilings this measurement ran under — on the payload, not only in the code"""
    return {"max_files": MAX_FILES, "max_bytes": MAX_BYTES}


def _tokens(nbytes: int | None) -> int | None:
    return None if nbytes is None else nbytes // TOKEN_BYTES


def _row(name: str, nbytes: int | None, *, detail: dict[str, Any] | None = None,
         note: str = "", measured: bool = True) -> dict[str, Any]:
    """One ingredient row"""
    return {"name": name, "bytes": nbytes, "tokens": _tokens(nbytes),
            "estimated": True, "measured": measured, "absent": nbytes is None,
            "detail": detail or {}, "note": note}


def _absent(name: str, note: str, detail: dict[str, Any] | None = None
            ) -> dict[str, Any]:
    """An ingredient that is NOT in this turn's window (#227)"""
    return _row(name, None, detail=detail, note=note)


def _text_bytes(text: str | None) -> int:
    return len((text or "").encode("utf-8", "replace"))


def _file_bytes(path: Path) -> tuple[int | None, str]:
    try:
        return len(path.read_bytes()), ""
    except OSError as e:
        return None, f"could not be read ({e.__class__.__name__}: {e})"


def _worktree_cwd(project: Any, wo: dict[str, Any], turn: dict[str, Any]) -> Path:
    """The directory the turn runs in — where Claude Code resolves CLAUDE.md from (docs/superpowers/specs/2026-09-29-one-copy-of-the-projects-claude-md.md §4.)"""
    name = wo.get("worktree")
    if name:
        path = Path(project.path) / ".claude" / "worktrees" / str(name)
        if path.is_dir() or (turn.get("kind") == "dispatch" and turn.get("seq") == 1):
            return path
    return Path(project.path)


def _knowledge_block(knowledge: Any, project_name: str) -> str:
    """The rendered block, re-rendered from the brief dispatch already has"""
    from .dispatch import render_knowledge_block  # lazy: dispatch imports this module

    return "\n".join(render_knowledge_block(knowledge, project_name))


def _walk(dirs: list[Path]) -> tuple[int, dict[str, Any]]:
    """Bytes under `dirs`, under both caps"""
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
    """The MCP server SET — names and a count, and NO byte figure"""
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


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def measure(project: Any, wo: dict[str, Any], turn: dict[str, Any],
            briefing: dict[str, Any], knowledge: Any = None,
            texts: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Every ingredient of this turn's window that Jarvis supplied; fills `texts` with each one's text"""
    rows: list[dict[str, Any]] = []
    texts = {} if texts is None else texts
    append = str(briefing.get("append_system_prompt") or "")
    texts["append_system_prompt"] = append
    rows.append(_row("append_system_prompt", _text_bytes(append),
                     detail={"lines": len(append.splitlines())},
                     note="Jarvis's git briefing plus the standing instructions, on "
                          "--append-system-prompt"))

    prompt = str(turn.get("prompt") or "")
    # `if knowledge` and not `is not None`: a `KnowledgeBrief` is falsy when the base has nothing visible...
    if knowledge:
        block = _knowledge_block(knowledge, project.name)
        if prompt.endswith(block):
            texts["worker_prompt"] = prompt[:len(prompt) - len(block)]
            texts["knowledge_index"] = block
            rows.append(_row("worker_prompt", _text_bytes(prompt) - _text_bytes(block),
                             note="the turn's prompt, with the knowledge block counted "
                                  "separately below"))
            rows.append(_row("knowledge_index", _text_bytes(block),
                             detail={"entries": getattr(knowledge, "total", None)},
                             note="the knowledge INDEX block "
                                  "`render_knowledge_block` appends to the dispatch "
                                  "prompt"))
        else:
            texts["worker_prompt"] = prompt
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
        texts["worker_prompt"] = prompt
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
    if size is not None:
        texts["worker_settings"] = _read_text(settings) or ""

    rows.append(_memory_row(project, wo, turn, texts))
    rows.append(_session_start_row(project, wo, turn, texts))
    rows.append(_agent_row(briefing, texts))
    rows.append(_add_dirs_row(briefing))
    rows.append(_mcp_row(project))
    return rows


def _memory_row(project: Any, wo: dict[str, Any], turn: dict[str, Any],
                texts: dict[str, str] | None = None) -> dict[str, Any]:
    from . import hooks

    cwd = _worktree_cwd(project, wo, turn)
    try:
        excluded = [str(p) for p in hooks.claude_md_excludes(Path(project.path), cwd)]
        paths = hooks.memory_files(Path(project.path), cwd)
    except OSError as e:
        return _absent("memory_files", f"the memory walk could not be read ({e})")
    total = 0
    counted: list[str] = []
    parts: list[str] = []
    for path in paths[:MAX_FILES]:
        size, _ = _file_bytes(path)
        if size is None:
            continue
        total += min(size, max(0, MAX_BYTES - total))
        counted.append(str(path))
        # The `==>` header is ours, to tell files apart; Claude Code wraps each in its own
        parts.append(f"==> {path} <==\n{_read_text(path) or ''}")
    if texts is not None:
        texts["memory_files"] = "\n".join(parts)
    # `excluded` names the dropped ancestor copies: absent and zero are different (module docstring) (docs/superpowers/specs/2026-09-29-one-copy-of-the-projects-claude-md.md §5.)
    return _row("memory_files", total,
                detail={"paths": counted, "count": len(counted), "cwd": str(cwd),
                        "excluded": excluded},
                note="the CLAUDE.md-shaped files Claude Code loads, nearest first — in "
                     "EVERY request's window for the life of the conversation (served "
                     "from cache when warm), unlike `knowledge_index` and "
                     "`worker_prompt`, which are listed on the turn that introduced "
                     "them")


def session_start_text(serena: bool, cwd: Path) -> str:
    """The `additionalContext` Jarvis's SessionStart hook injects, composed as the hook does"""
    from . import concision, hooks

    text = concision.house_style()
    return text + hooks.serena_activation_context(cwd) if serena else text


def _session_start_row(project: Any, wo: dict[str, Any], turn: dict[str, Any],
                       texts: dict[str, str]) -> dict[str, Any]:
    from . import wiring

    text = session_start_text(wiring.serena_wired(project.wiring),
                              _worktree_cwd(project, wo, turn))
    texts["session_start_context"] = text
    return _row("session_start_context", _text_bytes(text),
                note="the house style and Serena activation the SessionStart hook "
                     "injects on every turn")


def start_budget(ingredients: list[dict[str, Any]]) -> dict[str, Any]:
    """Jarvis's share of a worker's starting window against START_BUDGET_TOKENS"""
    tokens = sum(int(r["tokens"]) for r in ingredients
                 if r.get("name") in START_INGREDIENTS and r.get("tokens") is not None)
    return {"tokens": tokens, "budget": START_BUDGET_TOKENS,
            "over": tokens > START_BUDGET_TOKENS}


def _agent_row(briefing: dict[str, Any], texts: dict[str, str]) -> dict[str, Any]:
    name = briefing.get("agent")
    if not name:
        return _absent("agent_persona",
                       "this work order runs as no agent type, so there is no persona "
                       "file in its window")
    for d in briefing.get("add_dirs") or []:
        path = Path(d) / ".claude" / "agents" / f"{name}.md"
        if path.is_file():
            size, why = _file_bytes(path)
            if size is not None:
                texts["agent_persona"] = _read_text(path) or ""
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


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def payload(project: Any, wo: dict[str, Any], turn: dict[str, Any],
            briefing: dict[str, Any], knowledge: Any = None,
            texts: dict[str, str] | None = None) -> dict[str, Any]:
    """What `record` stores: the measurement plus everything needed to read it back"""
    texts = {} if texts is None else texts
    ingredients = measure(project, wo, turn, briefing, knowledge, texts)
    for row in ingredients:
        if row["name"] in texts:
            row["sha"] = _sha(texts[row["name"]])
    return {"schema": SCHEMA, "caps": caps(), "token_bytes": TOKEN_BYTES,
            "ingredients": ingredients, "start_budget": start_budget(ingredients)}


def blob(store: Any, sha: str) -> str | None:
    """An ingredient's exact text by the `sha` its row carries; None when never stored"""
    row = store.conn.execute("SELECT text FROM ctx_blobs WHERE sha=?", (sha,)).fetchone()
    return None if row is None else row[0]


# The meter wraps the WHOLE function (docs/superpowers/specs/2026-09-24-order-observability.md)
@observability.metered(OBSERVE_CONTEXT_WRITE, target="wo", project="project")
def record(store: Any, project: Any, wo: dict[str, Any], turn: dict[str, Any],
           briefing: dict[str, Any], knowledge: Any = None) -> None:
    """The ONE writer of `wo_turns.context_json`"""
    # §10's gate has exactly ONE consumer and this is it
    if not observability.records_context(wo, getattr(project, "observability", None)):
        return
    try:
        import json as _json

        texts: dict[str, str] = {}
        body = payload(project, wo, turn, briefing, knowledge, texts)
        store.conn.executemany("INSERT OR IGNORE INTO ctx_blobs (sha, text) VALUES (?, ?)",
                               [(_sha(t), t) for t in texts.values()])
        store.conn.execute("UPDATE wo_turns SET context_json=? WHERE id=?",
                           (_json.dumps(body), turn["id"]))
        store.conn.commit()
    except Exception:  # noqa: BLE001 — see the docstring: never into the dispatch path
        log.warning("could not record the context ledger for %s turn %s",
                    wo.get("id"), turn.get("seq"), exc_info=True)


def measured_tokens(ingredients: list[dict[str, Any]]) -> int:
    """The sum of the estimated token counts actually measured"""
    return sum(int(row["tokens"]) for row in ingredients
               if row.get("measured") and row.get("tokens") is not None)


def _written(write: Any) -> int | None:
    """`inspection.Write.written`, from the object or from its `as_dict`"""
    if write is None:
        return None
    if isinstance(write, dict):
        value = write.get("written")
    else:
        value = getattr(write, "written", None)
    return None if value is None else int(value)


def residual(measured: int, cold_start_write: Any) -> dict[str, Any]:
    """Claude Code's own base prompt and tool schemas, INFERRED — `measured: False`"""
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
    """Per-ingredient change against the PREVIOUS RECORDED turn"""
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
            # Absent on BOTH turns — no persona, no MCP inventory read
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
