"""Dispatch: turn a claimed work order into a running Claude Code worker."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import claude_cli
from .catalog import OsConfig, ProjectSpec
from .central_store import CentralStore, KnowledgeBrief
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
    # read rights over the whole project. Workers default to `auto` mode (which runs
    # routine tools unattended), so these are a safety net for projects that opt into
    # a stricter mode — under `acceptEdits`/`default` a --bg session would otherwise
    # prompt and stall (verified live). Sensitive-path deny guards from the project's
    # settings_overrides still win in every mode.
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


def render_knowledge_block(brief: KnowledgeBrief, project_name: str) -> list[str]:
    """The knowledge base as a map plus a retrieval verb, not as a payload.

    Pasting entries in full made the prompt grow with the base — every work order in the
    fleet paying for every learning ever recorded — while the selector (most-recent-N)
    meant the entry that actually mattered usually fell outside the window anyway. So the
    prompt ships headlines and ids at bounded cost, and the worker pulls full text for
    what its task touches.
    """
    lines = [
        "",
        f"# Knowledge base — {brief.total} entries visible to `{project_name}` "
        f"(this project + global)",
        "**This section is an INDEX, not the knowledge.** Headlines are truncated; the "
        "full text of an entry arrives only when you ask for it. Before you touch an "
        "area — a build step, a deploy path, a convention, a service — look it up:",
        "```bash",
        f'jarvis learn search "<term>" --project {project_name}  # full text of matches',
        "jarvis learn show <id> [<id> ...]  # full text of specific entries",
        f"jarvis learn list --project {project_name} --topic <t>  # everything in a topic",
        f"jarvis learn topics --project {project_name}  # what topics exist",
        "```",
        "Entries marked `(global)` came from another project; the rest are this one's.",
    ]
    if brief.pinned:
        lines += ["", "## Pinned — read these now (full text)"]
        for k in brief.pinned:
            topic = f" [{k['topic']}]" if k["topic"] else ""
            lines.append(f"- ({k['project'] or 'global'}{topic}) {k['content']}")
    if brief.digest:
        lines += ["", "## Index — headline only, `jarvis learn show <id>` for the rest"]
        current = object()
        for k in brief.digest:
            if k["topic"] != current:
                current = k["topic"]
                lines.append(f"### {k['topic'] or '(no topic)'}")
            scope = "" if k["project"] == project_name else " (global)"
            lines.append(f"- `{k['id']}`{scope} {k['headline']}")
    if brief.overflow:
        listed = ", ".join(f"{t or '(no topic)'} ({n})" for t, n in brief.overflow)
        lines += [
            "",
            f"## Not indexed above — {brief.overflow_count} further entries, by topic",
            f"{listed}",
            f"Reach them with `jarvis learn list --project {project_name} --topic <topic>` "
            f"or `jarvis learn search`.",
        ]
    return lines


def build_worker_prompt(wo: dict[str, Any], project: ProjectSpec,
                        knowledge: KnowledgeBrief | None = None) -> str:
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
        f"your next user turn (from Neo, the user's delegate, or the user). Put "
        f"everything needed to decide INSIDE the question text: whoever answers sees "
        f"only that text, never your session. Prefer recording an assumption and "
        f"continuing when the decision is reversible.",
        f"- File deferred work instead of leaving notes: `jarvis backlog add "
        f"{project.name} \"...\"`",
        f"- READ the OS knowledge base on demand — it is indexed at the end of this "
        f"prompt, not pasted into it: `jarvis learn search \"<term>\" --project "
        f"{project.name}` and `jarvis learn show <id>`. Look up any area you are about "
        f"to touch before you touch it; a past worker probably already paid for the "
        f"lesson. Do not assume the index headline is the whole entry.",
        f"- WRITE to it: the OS knowledge base is the ONLY memory that survives you: "
        f"`jarvis learn add \"...\" --project {project.name} --topic \"<topic>\"`. "
        f"Anything durable you learn — project state, gotchas, conventions, decisions "
        f"— goes there. Your own memory files, notes and scratch docs are invisible to "
        f"the user, to Neo and to the next worker (Jarvis mirrors any memory file you "
        f"do write, but say it here and it lands intact).",
        f"- Alert the human when needed: `jarvis notify --project {project.name} "
        f"--level warning|critical \"title\" \"body\"`",
        "- Hit a bug in Jarvis OS itself (a `jarvis` command fails, hangs, or does the "
        "wrong thing)? Use your `report-jarvis-bug` skill, then carry on with this work "
        "order. Bugs in THIS project are not Jarvis OS bugs — those go to the backlog.",
        f"- When done, ALWAYS run: `jarvis wo finish {wo['id']} --summary \"...\"` and "
        f"then write your full answer as the last thing you say.",
        "",
        "# What the outside world sees",
        "The work order record IS this conversation, as far as anyone else is concerned. "
        "The last message of every turn you take is captured verbatim into it, and the "
        "user and Neo make their decisions from that record — neither will ever open "
        "this session. So end every turn with the complete answer: findings, caveats, "
        "uncertainties, what you did NOT do, and absolute paths. `--summary` is a "
        "one-line headline for that answer, never a substitute for it — anything that "
        "lives only in the summary is the only thing anyone reads, so a detail you drop "
        "there is a detail that ceases to exist.",
        "",
        "Work autonomously toward a complete end-to-end solution unless this work "
        "order says otherwise. User feedback may arrive as new user turns; treat it "
        "as authoritative for this work order.",
    ]
    if knowledge:
        parts += render_knowledge_block(knowledge, project.name)
    return "\n".join(parts)


def dispatch_work_order(
    store: ProjectStore,
    central: CentralStore,
    project: ProjectSpec,
    wo: dict[str, Any],
    os_config: OsConfig | None = None,
) -> dict[str, Any]:
    """Spawn the worker for a work order already in `dispatching` state."""
    cfg = os_config or OsConfig()
    worktree = wo["id"]  # ids already carry the wo- prefix
    knowledge = central.knowledge_brief(
        project.name,
        pinned_limit=cfg.knowledge_inject_limit,
        digest_limit=cfg.knowledge_digest_limit,
        digest_chars=cfg.knowledge_digest_chars,
    )
    prompt = build_worker_prompt(wo, project, knowledge)

    model = wo.get("model") or project.worker.model
    effort = wo.get("effort") or project.worker.effort
    permission_mode = wo.get("permission_mode") or project.worker.permission_mode
    extra_sp = wo.get("append_system_prompt") or project.worker.append_system_prompt

    settings_file = _write_worker_settings(project, wo)
    # OS skills (e.g. reporting a Jarvis bug) reach the worker only via --add-dir: its
    # worktree holds tracked files only, so an untracked .claude/skills/ never arrives.
    from .bootstrap import install_agent_skills
    skills_dir = install_agent_skills(project.path)
    try:
        job_id = claude_cli.spawn_background(
            prompt=prompt,
            cwd=project.path,
            name=worker_name(wo),
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            append_system_prompt=extra_sp,
            worktree=worktree,
            settings_file=settings_file,
            add_dirs=[skills_dir],
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

    # job_id lets the reconciler recover this turn's final assistant message once the
    # session goes idle; reply_job_id is cleared so that capture is still outstanding.
    store.update_work_order(
        wo["id"],
        worktree=worktree,
        model=model,
        effort=effort,
        permission_mode=permission_mode,
        job_id=job_id,
        reply_job_id=None,
    )
    store.set_status(wo["id"], "running")
    store.add_event(wo["id"], "dispatched", {
        "worktree": worktree,
        "model": model,
        "permission_mode": permission_mode,
        "job": job_id,
        "note": "session id binds via SessionStart hook / name reconciliation",
    })
    central.touch_project(project.name)
    return store.get_work_order(wo["id"])
