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
        # Which privileged actions the PreToolUse gate mediates for this worker. Travels
        # as env rather than being looked up per hook call: the hook runs on every Bash
        # command and must not load and parse the catalog to decide it has nothing to do.
        "JARVIS_GATES": project.gates.to_json(),
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
        f"- **Neo is your first responder. Any doubt goes to it.** Not just the big "
        f"calls — any point where you are not sure. `jarvis wo ask {wo['id']} "
        f"\"<your question>\"`, then END YOUR TURN. The answer arrives as your next "
        f"user turn, usually within a minute, from Neo (the user's delegate) or the "
        f"user. This is the normal, expected way to work: it is not an escalation, it "
        f"does not interrupt the user, and it costs you about a minute. Put "
        f"everything needed to decide INSIDE the question text — whoever answers sees "
        f"only that text, never your session — with the concrete options and your "
        f"recommendation.",
        "  - The trigger is DOUBT, not importance. If you catch yourself weighing "
        "options, thinking \"either would work\", or picking one because you have to "
        "pick something, you are in doubt: ask. Ask BEFORE you build on it, not "
        "after.",
        "  - Do not talk yourself out of asking. \"It's reversible\", \"it's only an "
        "implementation detail\", \"I'll note it as an assumption\" — those are "
        "rationalisations for guessing. Almost everything is reversible; that is not "
        "the question. The question is whether you would be REBUILDING if you guessed "
        "wrong.",
        f"- `jarvis wo assume {wo['id']} \"...\"` is for the OTHER case, and it "
        f"should be RARE: a call you made with NO doubt — you followed an existing "
        f"convention, the work order implied it, the codebase left one sensible "
        f"option. Record EVERY such call, including the small and obvious ones "
        f"(naming, file layout, which convention you followed, how you split the "
        f"commits): recording is cheap and the work order record is the only audit "
        f"trail anyone gets. An assumption is a disclosure of something you were SURE "
        f"about. It is never a guess you are hoping nobody checks — if you are "
        f"guessing, ask instead.",
        f"- File deferred work instead of leaving notes: `jarvis backlog add "
        f"{project.name} \"...\"`",
        f"- The OS knowledge base is the ONLY memory that survives you: "
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
    if project.gates:
        parts += ["", *_gate_briefing(wo, project)]
    if knowledge:
        parts += ["", "# Knowledge base (learnings from this and other projects)"]
        for k in knowledge:
            scope = k["project"] or "global"
            topic = f" [{k['topic']}]" if k["topic"] else ""
            parts.append(f"- ({scope}{topic}) {k['content']}")
    return "\n".join(parts)


def _gate_briefing(wo: dict[str, Any], project: ProjectSpec) -> list[str]:
    """Tell the worker that shipping is reachable, and how.

    Worth stating explicitly: a worker that believes releases are simply forbidden will
    finish the work order with "someone should ship this" rather than asking, and the
    gate never gets used. The point of the gate is that the answer is "yes, with review".
    """
    from .gates import KINDS

    live = [k for k in KINDS if k.name in project.gates.enabled]
    lines = [
        "# Privileged actions (gated, NOT forbidden)",
        "These actions are reviewed before they run — an independent reviewer (Neo, the "
        "user's delegate) decides, and approval lets you proceed:",
    ]
    lines += [f"- `{k.name}` — {k.summary}" for k in live]
    lines += [
        "",
        "Attempting one directly is safe: the attempt is blocked, a request is filed "
        "automatically, and you are told to wait. But you make a much stronger case by "
        "asking first, because the reviewer sees ONLY the text you write:",
        f"    jarvis gate request {wo['id']} \"<the exact command>\" "
        f"--why \"<why this is ready>\" --evidence \"<PR number, test results, checks>\"",
        "",
        "Then END YOUR TURN. The verdict arrives as your next user turn. If approved, run "
        "that exact command — the approval is scoped to that one string and expires, so "
        "do not reword it. If denied, fix what the reason names; do not retry as-is.",
    ]
    return lines


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
