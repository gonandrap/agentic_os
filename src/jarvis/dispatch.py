"""Dispatch: turn a claimed work order into a running Claude Code worker."""

from __future__ import annotations

import os
import shutil
import sys
import uuid
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
        "Read OPERATION.md at the project root and follow it exactly: work in your "
        "worktree, commit and open a PR per repo conventions, record every assumption "
        f"with `jarvis wo assume {wo['id']} \"...\"`, file deferred work with "
        f"`jarvis backlog add {project.name} \"...\"`, report learnings with "
        f"`jarvis learn add ... --project {project.name}`, and when done run "
        f"`jarvis wo finish {wo['id']} --summary \"...\"`.",
        "Work autonomously toward a complete end-to-end solution unless this work "
        "order says otherwise.",
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
    session_id = str(uuid.uuid4())
    worktree = f"wo-{wo['id']}"
    knowledge = central.relevant_knowledge(project.name, limit=knowledge_limit)
    prompt = build_worker_prompt(wo, project, knowledge)

    model = wo.get("model") or project.worker.model
    effort = wo.get("effort") or project.worker.effort
    permission_mode = wo.get("permission_mode") or project.worker.permission_mode
    extra_sp = wo.get("append_system_prompt") or project.worker.append_system_prompt

    try:
        claude_cli.spawn_background(
            prompt=prompt,
            cwd=project.path,
            name=worker_name(wo),
            session_id=session_id,
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            append_system_prompt=extra_sp,
            worktree=worktree,
            env={
                "JARVIS_WO_ID": wo["id"],
                "JARVIS_PROJECT": project.name,
                "JARVIS_PROJECT_PATH": str(project.path),
                # Workers call `jarvis …` from Bash (contract); make sure it resolves
                # even though the Claude supervisor daemon has its own PATH.
                "PATH": _worker_path(),
            },
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
        session_id=session_id,
        worktree=worktree,
        model=model,
        effort=effort,
        permission_mode=permission_mode,
    )
    store.set_status(wo["id"], "running")
    store.add_event(wo["id"], "dispatched", {
        "session_id": session_id,
        "worktree": worktree,
        "model": model,
        "permission_mode": permission_mode,
    })
    central.touch_project(project.name)
    return store.get_work_order(wo["id"])
