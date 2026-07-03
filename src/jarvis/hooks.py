"""Handler for `jarvis _hook` — invoked by the Claude Code hooks that the OS injects
into every managed project's settings (SessionStart / Stop / SessionEnd / Notification).

Claude Code pipes a JSON payload on stdin (hook_event_name, session_id, cwd, ...).
We map the session to a work order (JARVIS_WO_ID env var set at dispatch, falling back
to a session_id lookup) and update the project DB. Sessions that aren't Jarvis workers
are a silent no-op, so interactive sessions in managed projects are unaffected.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from .project_store import ProjectStore

# A Bash command every worker must be able to run without a permission prompt:
# a chain of `cd <dir>` / `jarvis …` segments joined by &&, nothing else.
_SHELL_DANGEROUS = re.compile(r"[|;`$<>]")


def is_jarvis_command_chain(command: str) -> bool:
    if _SHELL_DANGEROUS.search(command):
        return False
    for segment in command.split("&&"):
        try:
            words = shlex.split(segment.strip())
        except ValueError:
            return False
        if not words:
            return False
        if words[0] == "jarvis":
            continue
        if words[0] == "cd" and len(words) == 2:
            continue
        return False
    return "jarvis" in command


def preflight_decision(payload: dict[str, Any]) -> dict[str, Any] | None:
    """PreToolUse: auto-approve the worker contract commands (`jarvis …`), which
    otherwise stall background sessions on a permission prompt when the model
    prefixes them with `cd <dir> &&`."""
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command", "")
    if is_jarvis_command_chain(command):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "jarvis contract command",
            }
        }
    return None


def find_project_root(cwd: Path) -> Path | None:
    """Map a hook cwd (possibly a worktree under .claude/worktrees/) to the project
    root that holds .jarvis/."""
    cwd = cwd.resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".jarvis").is_dir():
            return candidate
        # worktrees live at <root>/.claude/worktrees/<name>
        if candidate.parent.name == "worktrees" and candidate.parent.parent.name == ".claude":
            root = candidate.parent.parent.parent
            if (root / ".jarvis").is_dir():
                return root
    return None


def handle_hook(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "")
    cwd = Path(payload.get("cwd") or env.get("PWD") or ".")

    if event == "PreToolUse":
        return preflight_decision(payload)

    root_env = env.get("JARVIS_PROJECT_PATH")
    root = Path(root_env) if root_env else find_project_root(cwd)
    if root is None or not (root / ".jarvis").is_dir():
        return None  # not a managed project — no-op

    store = ProjectStore(root)
    try:
        wo_id = env.get("JARVIS_WO_ID")
        wo = None
        if wo_id:
            try:
                wo = store.get_work_order(wo_id)
            except KeyError:
                wo = None
        if wo is None and session_id:
            wo = store.find_by_session(session_id)
        if wo is None:
            return None  # not a worker session — no-op

        wo_id = wo["id"]
        store.add_event(wo_id, f"hook:{event}", {
            "session_id": session_id,
            "cwd": str(cwd),
            "message": payload.get("message"),
        })

        if event == "SessionStart":
            if wo["status"] in ("dispatching",):
                store.set_status(wo_id, "running")
            # Bind (or correct) the session id: --bg dispatch assigns its own,
            # so the hook is the authoritative source.
            if session_id and wo.get("session_id") != session_id:
                store.update_work_order(wo_id, session_id=session_id)

        elif event == "Notification":
            # Fired when the session needs attention (permission request, idle prompt).
            message = payload.get("message") or "Worker needs attention"
            if wo["status"] in ("running", "dispatching"):
                store.set_status(wo_id, "waiting_input")
            store.flag_attention(wo_id, message)
            store.add_notification(
                title=f"{wo_id} needs input",
                body=message,
                level="warning",
                wo_id=wo_id,
                source="hook:Notification",
            )

        elif event == "Stop":
            # End of a turn: the worker went idle. Completion is signaled separately
            # via `jarvis wo finish`; the daemon reconciler settles final states.
            store.add_event(wo_id, "turn_ended", {})

        elif event == "SessionEnd":
            fresh = store.get_work_order(wo_id)
            if fresh["status"] in ("running", "waiting_input", "dispatching"):
                if fresh.get("result_summary"):
                    _finalize(store, wo_id)
                else:
                    store.set_status(wo_id, "needs_review")
                    store.flag_attention(wo_id, "session ended without `jarvis wo finish`")
        return {"wo_id": wo_id, "event": event}
    finally:
        store.close()


def _finalize(store: ProjectStore, wo_id: str) -> None:
    if store.pending_assumptions(wo_id):
        store.set_status(wo_id, "needs_review")
        store.flag_attention(wo_id, "assumptions pending review")
    else:
        store.set_status(wo_id, "completed")
        store.clear_attention(wo_id)


def main_hook() -> int:
    """Entry point for `jarvis _hook`. Never fails the session: always exit 0."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        result = handle_hook(payload, dict(os.environ))
        if result and "hookSpecificOutput" in result:
            print(json.dumps(result))
    except Exception as e:  # noqa: BLE001 — a broken hook must not break sessions
        try:
            from .paths import logs_dir
            logs_dir().mkdir(parents=True, exist_ok=True)
            with (logs_dir() / "hook-errors.log").open("a") as f:
                f.write(f"{e!r}\n")
        except Exception:  # noqa: BLE001
            pass
    return 0
