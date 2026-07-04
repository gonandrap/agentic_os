"""Dispatch: turn a claimed work order into a running Claude Code worker."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import claude_cli
from .catalog import ProjectSpec
from .central_store import CentralStore
from .project_store import ProjectStore


def _worker_path() -> str:
    """Daemon PATH, with the directory holding `jarvis` prepended."""
    path = os.environ.get("PATH", "")
    exe = shutil.which("jarvis") or sys.executable
    bindir = str(Path(exe).parent)
    if bindir not in path.split(os.pathsep):
        path = f"{bindir}{os.pathsep}{path}"
    return path


def _write_worker_settings(project: ProjectSpec, wo: dict[str, Any]) -> Path:
    """Merge the project's injected settings with per-work-order env and persist
    them for --settings.

    The worker session lives in a fresh worktree where the (untracked)
    .claude/settings.json doesn't exist, so hooks/permissions/env must travel with
    the spawn. The file outlives the spawn call — Claude reloads settings from it —
    so it is kept under the project's .jarvis dir for the work order's lifetime.
    """
    import json as _json

    from .bootstrap import build_settings
    from .paths import jarvis_home

    settings = build_settings(project.settings_overrides)
    settings.pop("_jarvis", None)

    # Declarative worker permissions: full edit rights inside its own worktree,
    # read rights over the whole project. (Verified live: acceptEdits alone still
    # prompts inside --bg sessions, which would stall unattended work orders.)
    proj_abs = str(project.path).lstrip("/")
    wt_abs = f"{proj_abs}/.claude/worktrees/{wo['id']}"
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    for rule in (
        f"Edit(//{wt_abs}/**)",
        f"Write(//{wt_abs}/**)",
        f"NotebookEdit(//{wt_abs}/**)",
        f"Read(//{proj_abs}/**)",
    ):
        if rule not in allow:
            allow.append(rule)

    env = dict(settings.get("env") or {})
    env.update({
        "JARVIS_WO_ID": wo["id"],
        "JARVIS_PROJECT": project.name,
        "JARVIS_PROJECT_PATH": str(project.path),
        # The worker's jarvis calls must hit the same central state as the daemon.
        "JARVIS_HOME": str(jarvis_home()),
        # Workers call `jarvis …` from Bash (contract); make sure it resolves even
        # though the Claude supervisor daemon has its own PATH.
        "PATH": _worker_path(),
    })
    settings["env"] = env
    out = project.path / ".jarvis" / "worker-settings" / f"{wo['id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_json.dumps(settings, indent=2))
    return out


def worker_name(wo: dict[str, Any]) -> str:
    """Background session display name. The `[WO <id>]` prefix is the visible marker
    (in the agents view and UI) that this session is framework-managed."""
    return f"[WO {wo['id']}] {wo['title'][:60]}"


def build_worker_prompt(wo: dict[str, Any], project: ProjectSpec,
                        knowledge: list[dict[str, Any]]) -> str:
    parts = [
        f"You are the worker agent for Jarvis work order `{wo['id']}` in project "
        f"`{project.name}`.",
        "",
        f"# Work order: {wo['title']}",
        "",
        wo.get("description") or "(no further description — the title is the task)",
        "",
        "# Operating contract",
        "You MUST follow this contract (it mirrors the project's OPERATION.md — do "
        "not go looking for that file, everything you need is here):",
        "- Work only inside your assigned worktree (you start in it). Commit your "
        "work and open a PR per this repo's conventions. Never push to main.",
        f"- Record EVERY assumption you make: `jarvis wo assume {wo['id']} \"...\"`",
        f"- Blocked on a decision you cannot make? Ask the OS and END YOUR TURN: "
        f"`jarvis wo ask {wo['id']} \"<your question>\"` — the answer arrives as "
        f"your next user turn (from Neo, the user's delegate, or the user). Prefer "
        f"recording an assumption and continuing when the decision is reversible.",
        f"- File deferred work instead of leaving notes: `jarvis backlog add "
        f"{project.name} \"...\"`",
        f"- Report reusable learnings: `jarvis learn add \"...\" --project {project.name}`",
        f"- Alert the human when needed: `jarvis notify --project {project.name} "
        f"--level warning|critical \"title\" \"body\"`",
        f"- When done, ALWAYS run: `jarvis wo finish {wo['id']} --summary \"...\"`",
        "Work autonomously toward a complete end-to-end solution unless this work "
        "order says otherwise. User feedback may arrive as new user turns; treat it "
        "as authoritative for this work order.",
    ]
    if knowledge:
        parts += ["", "# Knowledge base (learnings from this and other projects)"]
        for k in knowledge:
            scope = k["project"] or "global"
            topic = f" [{k['topic']}]" if k["topic"] else ""
            parts.append(f"- ({scope}{topic}) {k['content']}")
    return "\n".join(parts)


def dispatch_work_order(
    store: ProjectStore,
    central: CentralStore,
    project: ProjectSpec,
    wo: dict[str, Any],
    knowledge_limit: int = 8,
) -> dict[str, Any]:
    """Spawn the worker for a work order already in `dispatching` state."""
    worktree = wo["id"]  # ids already carry the wo- prefix
    knowledge = central.relevant_knowledge(project.name, limit=knowledge_limit)
    prompt = build_worker_prompt(wo, project, knowledge)

    model = wo.get("model") or project.worker.model
    effort = wo.get("effort") or project.worker.effort
    permission_mode = wo.get("permission_mode") or project.worker.permission_mode
    extra_sp = wo.get("append_system_prompt") or project.worker.append_system_prompt

    settings_file = _write_worker_settings(project, wo)
    try:
        claude_cli.spawn_background(
            prompt=prompt,
            cwd=project.path,
            name=worker_name(wo),
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            append_system_prompt=extra_sp,
            worktree=worktree,
            settings_file=settings_file,
        )
    except claude_cli.ClaudeCliError as e:
        store.set_status(wo["id"], "failed")
        store.flag_attention(wo["id"], f"dispatch failed: {e}")
        store.add_notification(
            title=f"Dispatch failed for {wo['id']}",
            body=str(e),
            level="warning",
            wo_id=wo["id"],
            source="jarvisd",
        )
        raise

    store.update_work_order(
        wo["id"],
        worktree=worktree,
        model=model,
        effort=effort,
        permission_mode=permission_mode,
    )
    store.set_status(wo["id"], "running")
    store.add_event(wo["id"], "dispatched", {
        "worktree": worktree,
        "model": model,
        "permission_mode": permission_mode,
        "note": "session id binds via SessionStart hook / name reconciliation",
    })
    central.touch_project(project.name)
    return store.get_work_order(wo["id"])
