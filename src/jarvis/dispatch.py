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
    """What the worker is told, composed from the work order and its project.

    Two kinds of work order get two contracts (`project_store.WO_KINDS`). Everything
    around the contract — the pre-approval marker, the gate briefing, the knowledge base,
    "what the outside world sees" — is identical for both, and deliberately so: a planner
    is an ordinary session that happens to produce a plan, and every surface the OS
    already gives a worker (asking Neo, assumptions, the gate, the timeline,
    cancellation) applies to it unchanged.
    """
    if wo.get("kind") == "planner":
        return _planner_prompt(wo, project, knowledge)
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
        f"- **The PR title MUST start with `[{wo['id']}] `** — e.g. "
        f"`[{wo['id']}] {wo['title'][:40]}`. It is what ties the pull request back to "
        f"this work order for everyone who never sees Jarvis. `gh pr create` with any "
        f"other title is blocked.",
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
        f"then write your full answer as the last thing you say. If you opened a pull "
        f"request, pass it too: `--pr <url>`. That parks the work order in 'waiting for "
        f"PR merge', where it stays on the user's open list with the link until they "
        f"merge it, instead of settling as completed work nobody is looking at.",
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
    return "\n".join(_common_briefing(parts, wo, project, knowledge))


def _common_briefing(parts: list[str], wo: dict[str, Any], project: ProjectSpec,
                     knowledge: list[dict[str, Any]]) -> list[str]:
    """The tail every briefing carries, whatever the work order's kind."""
    pre_approved = _pre_approval(wo)
    if pre_approved:
        parts += ["", *_pre_approved_briefing(pre_approved)]
    if project.gates:
        parts += ["", *_gate_briefing(wo, project)]
    if knowledge:
        parts += ["", "# Knowledge base (learnings from this and other projects)"]
        for k in knowledge:
            scope = k["project"] or "global"
            topic = f" [{k['topic']}]" if k["topic"] else ""
            parts.append(f"- ({scope}{topic}) {k['content']}")
    return parts


def _planner_prompt(wo: dict[str, Any], project: ProjectSpec,
                    knowledge: list[dict[str, Any]]) -> str:
    """The briefing for a feature order's planner.

    Three things make it different from a worker's, and each is load-bearing:

    * **Its output is a graph, not a change.** So its terminal action is structured —
      `jarvis fo plan --from-file` against a JSON document — rather than prose. The
      `--from-file` shape is not cosmetic either: `gates.scannable()`'s quote-blanking
      fails on nested and mixed quoting, and a plan is a long argument full of repo
      paths, which is exactly the input that trips the gate classifier into a false
      positive. It goes in a file.
    * **Its readers are strangers.** Each child work order is dispatched into a fresh
      session that sees its own description and nothing else — not this plan, not this
      conversation, not its siblings. That is the failure this briefing spends the most
      words on, because it is the one the validator can only partly catch.
    * **It plans; it does not build.** A planner that returns the finished solution has
      failed at the job even if the solution is good, because the point of the feature
      order is a decomposition the fleet can execute in parallel.

    That last one is carried HERE, in prose, and not by a permission rule — which is a
    weaker guarantee and worth stating plainly rather than leaving for someone to
    discover. The planner is a work order, not a subagent, so it has no `tools:`
    frontmatter (the CLI-enforced layer); its only available restriction is the
    `permissions.deny` path `_write_worker_settings` writes, and a deny broad enough to
    stop product code also stops the two things a planner is REQUIRED to do — write the
    `plan.json` it submits, and produce a design document, whose pull request the design
    makes the base of the children's stack. So the planner runs on ordinary worker
    permissions in this phase, deliberately. The hard posture arrives with the seats,
    which can express it declaratively; see the Phase 3 backlog item.
    """
    from .plans import CHILD_CAP, MIN_DESCRIPTION_CHARS

    fo_id = wo.get("parent_id") or "?"
    parts = [
        f"You are the PLANNER for Jarvis feature order `{fo_id}` in project "
        f"`{project.name}`, running as work order `{wo['id']}`.",
        "",
        f"# Feature order: {wo['title']}",
        "",
        wo.get("description") or "(no further description — the title is the ask)",
        "",
        "# Your job: produce a plan, not a solution",
        "Decompose the feature above into a dependency-ordered set of ordinary work "
        "orders, each of which one worker can carry out in one session and finish with "
        "its own pull request. Read the codebase as much as you need to — that is what "
        "your worktree is for. What you hand back is the decomposition.",
        "",
        "**Do not build the feature.** A planner that returns the working solution has "
        "failed, however good the solution is: the feature order exists to produce work "
        "the fleet can execute in parallel, and a finished branch is not that. Writing "
        "code to UNDERSTAND the problem is fine and expected; shipping it is not the job.",
        "",
        "# The plan",
        f"Write it to a JSON file in your worktree and submit it with:",
        f"    jarvis fo plan {fo_id} --from-file plan.json",
        "",
        "```json",
        "{",
        '  "summary": "one line: what this feature is, once it is all done",',
        '  "justification": "only if you exceed the child cap — why it cannot be fewer",',
        '  "children": [',
        "    {",
        '      "key": "schema",',
        '      "title": "short imperative title, as a work order would have",',
        '      "description": "the WHOLE brief for this piece — see below",',
        '      "needs": ["other-key", "..."],',
        '      "acceptance": "how the worker knows it is done (optional)"',
        "    }",
        "  ]",
        "}",
        "```",
        "",
        f"`key` is a short lowercase slug, local to this plan — it is how you wire "
        f"`needs` between children before any work-order id exists. `needs` names other "
        f"keys IN THIS PLAN and nothing else; the OS turns them into real dependency "
        f"edges, and a child does not start until everything it needs has completed and "
        f"merged.",
        "",
        "## Every child's description must stand alone",
        "This is the rule the whole plan lives or dies on. Each child is dispatched into "
        "a NEW session with a worker that sees its own description and nothing else — "
        "not this plan, not this conversation, not what its siblings are doing, not the "
        "feature order. Anything you know because you read the whole feature has to be "
        "written INTO each child that needs it, even where that means repeating "
        "yourself across children. Repetition is cheap; a worker guessing is not.",
        "",
        "So: no \"as discussed in the plan\", no \"same as the previous work order\", no "
        "\"as described above\". Those are rejected mechanically, before anything is "
        "created. Name the files, the functions and the interfaces; say what the piece "
        "must not change; say what its sibling is doing if that is why an interface is "
        "shaped the way it is.",
        "",
        "## What the validator refuses",
        "Checked in Python at submission, before a single work order exists, so a plan "
        "that fails costs you a revision and nothing else:",
        "- a dependency cycle among the children, or a child depending on itself",
        "- a `needs` naming a key that is not in the plan",
        f"- more than {CHILD_CAP} children with no `justification` saying why it cannot "
        f"be done in fewer ({CHILD_CAP} is the cap; a plan at or over it is escalated to "
        f"the user rather than waved through, so stay under it unless you genuinely "
        f"cannot)",
        f"- a description under {MIN_DESCRIPTION_CHARS} characters, or one that only "
        f"repeats the title, or one that points at something the child worker cannot see",
        "",
        "If it refuses, it names every problem at once. Fix them all and resubmit.",
        "",
        "## After you submit",
        "Neo (the user's delegate) reviews the plan and either releases it — at which "
        "point the OS creates every child work order with its edges and starts "
        "dispatching them — or sends it back. A rejection arrives as your next user turn "
        "with the reason: revise and submit again from this same session.",
        "",
        "# Operating contract",
        f"- `jarvis fo plan {fo_id} --from-file <file>` IS your finish. Do not run "
        f"`jarvis wo finish` — submitting the plan settles this work order for you.",
        f"- **Neo is your first responder. Any doubt goes to it.** `jarvis wo ask "
        f"{wo['id']} \"<your question>\"`, then END YOUR TURN; the answer arrives as your "
        f"next user turn, usually within a minute. The trigger is DOUBT, not importance. "
        f"For a planner the highest-value questions are about SCOPE — whether a piece "
        f"belongs in this feature at all — because that is the one thing you cannot "
        f"recover from by revising the decomposition. Put everything needed to decide "
        f"inside the question text; whoever answers sees only that text.",
        f"- `jarvis wo assume {wo['id']} \"...\"` for a call you made with NO doubt. "
        f"Record every one, including the small ones.",
        f"- Work only inside your worktree (you start in it).",
        f"- File deferred work instead of leaving notes: `jarvis backlog add "
        f"{project.name} \"...\"` — including anything you decided was OUT of this "
        f"feature's scope.",
        f"- The OS knowledge base is the ONLY memory that survives you: "
        f"`jarvis learn add \"...\" --project {project.name} --topic \"<topic>\"`.",
        f"- Alert the human when needed: `jarvis notify --project {project.name} "
        f"--level warning|critical \"title\" \"body\"`",
        "- Hit a bug in Jarvis OS itself? Use your `report-jarvis-bug` skill, then carry "
        "on.",
        "",
        "# What the outside world sees",
        "The work order record IS this conversation, as far as anyone else is concerned. "
        "The last message of every turn you take is captured verbatim into it, and the "
        "user and Neo decide from that record — neither will ever open this session. End "
        "every turn with the complete answer: what you decomposed and why, what you "
        "deliberately left out, what you are unsure about, and absolute paths.",
        "",
        "Work autonomously toward a submitted plan. User feedback may arrive as new user "
        "turns; treat it as authoritative.",
    ]
    return "\n".join(_common_briefing(parts, wo, project, knowledge))


def _pre_approval(wo: dict[str, Any]) -> dict[str, Any] | None:
    """The pre-approval marker, if this work order carries one.

    Metadata arrives as a JSON string straight off the row, and a work order with none
    is the overwhelming common case, so this stays quiet about anything malformed —
    a briefing is not the place to raise a schema error.
    """
    from . import db
    from .project_store import PRE_APPROVED_KEY

    meta = wo.get("metadata")
    if isinstance(meta, str):
        meta = db.from_json(meta, {})
    if not isinstance(meta, dict):
        return None
    marker = meta.get(PRE_APPROVED_KEY)
    return marker if isinstance(marker, dict) and marker else None


def _pre_approved_briefing(marker: dict[str, Any]) -> list[str]:
    """Tell the worker the decision it would otherwise ask about is already made.

    The contract above tells workers to ask on any doubt, and that is right for work
    the user commissioned. This work order was filed BY the reviewer, so "may I?" would
    route the question back to the one who already said yes — a minute spent to be told
    what the briefing says. The scope line is what keeps that narrow: it names the thing
    approved, and everything outside it goes back to the ordinary rule.
    """
    by = str(marker.get("by") or "the reviewer")
    scope = str(marker.get("scope") or "the change this work order describes")
    lines = [
        "# This work order is PRE-APPROVED",
        f"It was filed by {by}, who already decided it should happen. You do NOT need "
        f"to ask whether to proceed: {scope} is approved. Go and do it.",
        "The approval covers THAT and nothing else. Everything the contract says still "
        "applies to everything else — ask on any other doubt, record your assumptions, "
        "and privileged actions are still gated. If you find the work order is wrong "
        "about the facts, say so and ask rather than carrying out something incorrect.",
    ]
    origin = marker.get("from_wo")
    if origin:
        lines.append(f"Filed while answering a question on {origin}.")
    return lines


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
        "",
        # Without this the worker reads a gate on a `grep` as a judgement on its work and
        # starts rewording harmless commands to sneak past the recogniser. Naming the
        # third outcome is what makes "just file it and wait" the obvious move instead.
        "A third verdict exists: DISMISSED. The recogniser matches text, so it sometimes "
        "fires on a command that merely NAMES one of these actions — a release script "
        "inside a grep pattern, a path quoted in a PR body. That is an OS bug, not a "
        "refusal: the reviewer dismisses it, nothing is authorised, and you may run the "
        "command as written. So if a gate fires on something you know ships nothing, do "
        "not reword the command to get around it — file it, say plainly why it performs "
        "no privileged action, and end your turn.",
    ]
    return lines


def dispatch_work_order(
    store: ProjectStore,
    central: CentralStore,
    project: ProjectSpec,
    wo: dict[str, Any],
    knowledge_limit: int = 8,
) -> dict[str, Any]:
    """Open the worker's conversation for a work order already in `dispatching` state.

    Dispatch composes what the worker is told — the prompt, the settings file, the
    resolved model/effort/permission mode — and hands the running of it to
    `worker_session`, which owns the transport.
    """
    from . import worker_session

    knowledge = central.relevant_knowledge(project.name, limit=knowledge_limit)
    prompt = build_worker_prompt(wo, project, knowledge)

    # Resolved onto the row before the turn is launched, so every later turn rebuilds the
    # same briefing from the record rather than re-reading a catalog that may have moved.
    resolved = {
        "model": wo.get("model") or project.worker.model,
        "effort": wo.get("effort") or project.worker.effort,
        "permission_mode": wo.get("permission_mode") or project.worker.permission_mode,
    }
    store.update_work_order(wo["id"], **resolved)
    wo = store.get_work_order(wo["id"])

    try:
        turn = worker_session.start(store, project, wo, prompt)
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

    store.set_status(wo["id"], "running")
    store.add_event(wo["id"], "dispatched", {
        "worktree": wo["id"],
        "session_id": store.get_work_order(wo["id"])["session_id"],
        "turn": turn["seq"],
        "pid": turn["pid"],
        **resolved,
    })
    central.touch_project(project.name)
    return store.get_work_order(wo["id"])
