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


# Serena's READ-ONLY tool surface. Naming a tool in an agent's `tools:` key makes it
# available; it does not make it runnable — permission is a separate gate, and a headless
# turn cannot answer a prompt. Probed live 2026-08-03: a seat holding
# `mcp__…__activate_project` in `tools:` but not in `permissions.allow` had the call
# BLOCKED and reported it could not proceed; with these rules added, the same seat ran
# activate_project -> find_symbol -> find_referencing_symbols and answered correctly with
# no text search at all. So this list is what turns "Serena first" from prose into
# something a worker can actually do.
#
# Enumerated rather than granted wholesale, for the same reason the planning seats
# enumerate: Serena also ships `execute_shell_command`, `create_text_file` and
# `replace_symbol_body`. Allowing the server as a unit would hand every worker — and every
# seat that is deliberately denied a shell — a shell, through a side door.
SERENA_READ_TOOLS = (
    "activate_project", "get_symbols_overview", "find_symbol",
    "find_referencing_symbols", "find_declaration", "find_implementations",
    "search_for_pattern", "find_file", "list_dir", "list_memories", "read_memory",
)

# A plugin install produces the long prefix, `claude mcp add serena` the short one. Jarvis
# configures no MCP server itself, so it cannot know which; both are listed, and a rule
# naming a tool that does not exist on this install is simply inert.
SERENA_TOOL_PREFIXES = ("mcp__serena__", "mcp__plugin_serena_serena__")


def serena_allow_rules() -> list[str]:
    """`permissions.allow` entries for every read-only Serena tool, under both prefixes."""
    return [f"{prefix}{tool}"
            for prefix in SERENA_TOOL_PREFIXES for tool in SERENA_READ_TOOLS]


def _write_worker_settings(project: ProjectSpec, wo: dict[str, Any]) -> Path:
    """Merge the project's injected settings with per-work-order env and persist
    them for --settings.

    The worker session lives in a fresh worktree where the (untracked)
    .claude/settings.json doesn't exist, so hooks/permissions/env must travel with
    the spawn. The file outlives the spawn call — Claude reloads settings from it —
    so it is kept under the project's .jarvis dir for the work order's lifetime.
    """
    import json as _json

    from . import agent_usage
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
        # Read-only code navigation. Without these the symbol tools are visible and
        # unrunnable, which is worse than absent: the worker is told to prefer Serena,
        # tries, gets blocked, and either stalls asking for permission it cannot be
        # granted headlessly or falls back to grep having wasted a call.
        *serena_allow_rules(),
    ):
        if rule not in allow:
            allow.append(rule)

    # THE prefix-stability lever. Claude Code builds a git-status snapshot (branch,
    # `status --short`, last five commits) into the dynamic half of the system prompt
    # and rebuilds it per process — and a worker turn IS a process (`-p --resume`). So
    # the worker dirties its tree, the snapshot changes, the system prompt changes, and
    # the cached prefix for the entire conversation dies at every turn boundary. This
    # setting is the only switch that removes the snapshot; measured on 2.1.233, turn 2
    # of a resumed worker goes from writing 10,983 / reading 15,995 tokens to writing
    # 552 / reading 26,113. It also drops the CLI's own git and commit/PR instruction
    # blocks, which `worker_brief.git_briefing` restates as static text on
    # --append-system-prompt (tests/test_stable_prefix.py holds the two together).
    settings["includeGitInstructions"] = False

    env = dict(settings.get("env") or {})
    env.update({
        "JARVIS_WO_ID": wo["id"],
        "JARVIS_PROJECT": project.name,
        "JARVIS_PROJECT_PATH": str(project.path),
        # The worker's jarvis calls must hit the same central state as the daemon.
        "JARVIS_HOME": str(jarvis_home()),
        # Where TOKEN ACCOUNTING goes, pinned separately from JARVIS_HOME and read by
        # `agent_usage` alone. The two are the same value here and diverge in exactly
        # one place: a test run inside this worker, whose isolation gate redirects
        # JARVIS_HOME away from live state. Real tokens billed by an opt-in LLM eval
        # are still this work order's cost, and without this they were spent into a
        # tmp directory and deleted (issue #103). See `agent_usage.SPEND_HOME_ENV`.
        agent_usage.SPEND_HOME_ENV: str(jarvis_home()),
        # Workers call `jarvis …` from Bash (contract); make sure it resolves even
        # though the Claude supervisor daemon has its own PATH.
        "PATH": _worker_path(),
        # Which privileged actions the PreToolUse gate mediates for this worker. Travels
        # as env rather than being looked up per hook call: the hook runs on every Bash
        # command and must not load and parse the catalog to decide it has nothing to do.
        "JARVIS_GATES": project.gates.to_json(),
        # Buy the 5-minute prompt cache (write 1.25x) instead of the 1-hour one (2x),
        # which Claude Code would otherwise pick for a headless session. Measurements
        # and the reversal criteria: docs/superpowers/specs/
        # 2026-08-10-resume-cost-and-the-cache.md, and kn-f94abf34.
        "FORCE_PROMPT_CACHING_5M": "1",
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
        "full text of an entry arrives only when you ask for it. If a headline below "
        "touches what you are about to do, FETCH IT — before you act on it, and before "
        "you ask Neo or record an assumption about it:",
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


def materialize_design_doc(store: ProjectStore, project: ProjectSpec,
                           wo: dict[str, Any]) -> dict[str, str] | None:
    """Put the parent feature's design document where this child worker can read it.

    A child's worktree branches from the default branch, but the design document lives
    on the PLANNER's unmerged branch — so the snapshot taken at `fo plan` time is
    written under the project's shared `.jarvis/` tree, which workers already read
    (agent skills live there). Returns `{repo_path, path}` for the prompt, or None when
    the work order has no parent or its plan names no document. Idempotent: later
    dispatches of siblings rewrite the same bytes.
    """
    from . import db

    if not wo.get("parent_id") or wo.get("kind") == "planner":
        return None
    fo = store.get_feature_order(wo["parent_id"])
    plan = db.from_json((fo or {}).get("plan"), {}) or {}
    if not (fo and plan.get("design_doc") and plan.get("design_doc_content")):
        return None
    target = (project.path / ".jarvis" / "features" / fo["id"]
              / Path(plan["design_doc"]).name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(plan["design_doc_content"])
    return {"repo_path": plan["design_doc"], "path": str(target)}


def feature_context(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """The feature a MANAGER owns, with its live children. None for anything else.

    Read at dispatch rather than snapshotted at creation, and passed in rather than
    looked up inside `build_worker_prompt`, for the same two reasons
    `materialize_design_doc` is shaped this way: the prompt builder stays a pure function
    of its arguments, and the manager's children change under it — it files more of them
    as the feature runs, so anything frozen at release would be wrong by its second turn.
    """
    if wo.get("kind") != "manager" or not wo.get("parent_id"):
        return None
    try:
        fo = store.get_feature_order(wo["parent_id"])
    except KeyError:
        return None  # a briefing is not the place to raise on a deleted parent
    return {"fo": fo, "children": store.feature_children(fo["id"])}


def build_worker_prompt(wo: dict[str, Any], project: ProjectSpec,
                        knowledge: KnowledgeBrief | None = None,
                        design_doc: dict[str, str] | None = None,
                        feature: dict[str, Any] | None = None) -> str:
    """What the worker is told, composed from the work order and its project.

    Three kinds of work order get three shapes. A WORKER opens with the minimum — its
    identity, the work order, a compressed contract of only the load-bearing
    invariants — plus an index of the full briefings it can fetch on demand with
    `jarvis brief <section>` (single-sourced in `worker_brief`, so the CLI and this
    prompt cannot drift). A PLANNER keeps its full prompt: one session per feature,
    already reviewed as a unit. A MANAGER gets neither: it writes no product code, so
    every line of the worker contract about worktrees, pull requests and finishing would
    be an instruction to do something it must not do. The surfaces around the contract —
    the pre-approval marker and the knowledge index — are identical for all three.
    """
    if wo.get("kind") == "planner":
        return _planner_prompt(wo, project, knowledge)
    if wo.get("kind") == "manager":
        return _manager_prompt(wo, project, knowledge, feature)
    from . import worker_brief
    from .gates import KINDS

    live_gates = tuple(k.name for k in KINDS
                       if k.name in project.gates.enabled) if project.gates else ()
    parts = [
        f"You are the worker agent for Jarvis work order `{wo['id']}` in project "
        f"`{project.name}`.",
        "",
        f"# Work order: {wo['title']}",
        "",
        wo.get("description") or "(no further description — the title is the task)",
        *([
            "",
            "# Design document",
            f"Your brief references sections of this feature's design document "
            f"(`{design_doc['repo_path']}`). A snapshot is materialised at "
            f"{design_doc['path']} — read the sections your brief names rather than "
            f"the whole document, and treat it as read-only: the authoritative copy "
            f"is on the planner's branch.",
        ] if design_doc else []),
        "",
        *worker_brief.core_contract(wo["id"], wo["title"], project.name,
                                    bool(knowledge), live_gates),
        "",
        *worker_brief.section_index(wo["id"], gated=bool(project.gates)),
    ]
    pre_approved = _pre_approval(wo)
    if pre_approved:
        parts += ["", *_pre_approved_briefing(pre_approved)]
    parts += [
        "",
        "Work autonomously toward a complete end-to-end solution unless this work "
        "order says otherwise. User feedback may arrive as new user turns; treat it "
        "as authoritative for this work order.",
    ]
    if knowledge:
        parts += render_knowledge_block(knowledge, project.name)
    return "\n".join(parts)


def _navigation_briefing() -> list[str]:
    """Serena before grep — the full text lives in `worker_brief` (single source
    with `jarvis brief navigation`); this shape survives for the planner's
    `_common_briefing` tail."""
    from . import worker_brief

    return worker_brief.navigation_section().splitlines()


def _common_briefing(parts: list[str], wo: dict[str, Any], project: ProjectSpec,
                     knowledge: KnowledgeBrief | None = None) -> list[str]:
    """The full-briefing tail — now only the PLANNER's prompt carries it inline.

    A worker's prompt reaches the same text through `worker_brief.section_index`
    and `jarvis brief <section>` instead; the pre-approval marker and the knowledge
    index are the parts a worker still gets inline, composed in
    `build_worker_prompt` directly.
    """
    parts += ["", *_navigation_briefing()]
    pre_approved = _pre_approval(wo)
    if pre_approved:
        parts += ["", *_pre_approved_briefing(pre_approved)]
    if project.gates:
        parts += ["", *_gate_briefing(wo, project)]
    if knowledge:
        parts += render_knowledge_block(knowledge, project.name)
    return parts


def _planner_prompt(wo: dict[str, Any], project: ProjectSpec,
                    knowledge: KnowledgeBrief | None = None) -> str:
    """The briefing for a feature order's planner.

    Four things make it different from a worker's, and each is load-bearing:

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
    * **It leads a team.** Two seats — `jarvis-architect` and `jarvis-test-lead` — reach
      it as subagent types over the extra `--add-dir` that `briefing_for` gives a
      planner and no one else. A briefing that did not name them would leave two
      definitions sitting on disk that nothing ever invokes, so this section is what
      makes the seats real.

    The third one is carried HERE, in prose, and not by a permission rule on the planner
    itself — which is a weaker guarantee and worth stating plainly rather than leaving
    for someone to discover. The planner is a work order, not a subagent, so it has no
    `tools:` frontmatter (the CLI-enforced layer); its only available restriction is the
    `permissions.deny` path `_write_worker_settings` writes, and a deny broad enough to
    stop product code also stops the two things a planner is REQUIRED to do — write the
    `plan.json` it submits, and produce a design document, whose pull request the design
    makes the base of the children's stack. Phase 3 revisited this and left it alone. The
    alternative its backlog item floated was denying edits under the project's source
    directories while leaving the worktree root writable — but "source directory" is not a
    concept the catalog has, so it would mean guessing `src/`-shaped paths per project,
    and breaking any planner whose design document lives under one is exactly the case
    decision 2 depends on.

    The SEATS are where the posture is enforced instead, and there it is real: each is
    declared `tools: Read, Grep, Glob`, which the CLI enforces rather than advises. No
    `Bash` either — withholding `Write` while granting a shell is not a prohibition,
    because a heredoc writes a file just as well (ruled 2026-08-03).
    """
    from .plans import CHILD_CAP, MAX_DESCRIPTION_CHARS, MIN_DESCRIPTION_CHARS

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
        "# Your team",
        "You are the lead of a planning team, not a lone session. Two seats are "
        "available to you as subagent types through the Task tool, and they exist "
        "because a decomposition and its acceptance criteria are different jobs that go "
        "wrong in different ways:",
        "",
        "- **`jarvis-architect`** — which pieces are separable, what the interface "
        "between them is, what must land first, and what should NOT be split. Consult it "
        "BEFORE you write the plan, and again whenever a child looks too big for one "
        "session.",
        "- **`jarvis-test-lead`** — what \"done\" means for each child and how its worker "
        "proves it, written to stand alone in a brief read cold. Consult it AFTER the "
        "decomposition is settled and before you submit.",
        "",
        "Both seats can read the codebase and neither can write to it: they have `Read`, "
        "`Grep` and `Glob` and nothing else, enforced by the CLI rather than by "
        "instruction. So they cannot do the work by accident, and they cannot run a "
        "command for you — anything that needs a shell is yours to run.",
        "",
        "Consulting them is expected, not optional politeness, and they are the reason "
        "this is a feature order rather than a work order. But you hold the plan: they "
        "advise in prose, you decide what the children are, and you own the submission. "
        "Where the architect and the test lead disagree with each other or with you, say "
        "so in your final answer rather than quietly picking one.",
        "",
        "# The plan",
        f"Write it to a JSON file in your worktree and submit it with:",
        f"    jarvis fo plan {fo_id} --from-file plan.json",
        "",
        "```json",
        "{",
        '  "summary": "one line: what this feature is, once it is all done",',
        '  "design_doc": "docs/specs/<feature>.md — the design document your briefs '
        'reference, relative to the repo root",',
        '  "design_doc_by": "<child key> — INSTEAD of design_doc, when there is no spec '
        'yet: the child that writes one",',
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
        "## The design document carries the shared context; each brief stands alone on "
        "top of it",
        "Write the feature's design document FIRST — a markdown file in your worktree "
        "(convention: `docs/`), with numbered sections — and name it in the plan's "
        "`design_doc` field. Everything you know because you read the whole feature — "
        "the architecture, the data model, the interfaces, the traps — goes THERE, "
        "once. Do not repeat it into every child: that duplication is what took "
        "plan-review questions to 84KB.",
        "",
        "**Every plan stands on a design document, and the validator checks it.** If "
        "writing one yourself is genuinely not the right move — the feature is a set of "
        "small independent fixes, or the spec is itself what has to be worked out "
        "against the code — then make writing it THE FIRST CHILD: name that child's key "
        "in `design_doc_by` instead of naming a `design_doc`, and give every other child "
        "a `needs` path back to it. Name one or the other; naming both is refused.",
        "",
        "Each child is dispatched into a NEW session with a worker that sees its own "
        "description plus the design document, and nothing else — not this plan, not "
        "this conversation, not what its siblings are doing. The OS snapshots the "
        "document when you submit and materialises it where every child worker can "
        "read it. So a description is a BRIEF, not an encyclopedia: the goal, the "
        "scope boundary (what this piece must not touch), what done means, and "
        "explicit references to the design document's sections by number for "
        "everything deeper (`the state machine is design doc section 3`). It must "
        "still stand alone as INSTRUCTIONS — a stranger must know what to do from the "
        "description; the document is where they read how it fits.",
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
        f"- a description over {MAX_DESCRIPTION_CHARS} characters. This is the hard "
        f"edge of \"a brief, not an encyclopedia\", and it is not negotiable by writing "
        f"more carefully: if the piece needs more than that to explain, the explanation "
        f"belongs in the design document and the brief cites its section number",
        f"- a plan naming neither `design_doc` nor `design_doc_by`, a `design_doc_by` "
        f"that is not a child of the plan, or a `design_doc_by` child that some sibling "
        f"does not depend on",
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
        f"recover from by revising the decomposition. A question is one paragraph: the "
        f"decision, the options, your recommendation — arguing from your design "
        f"document by section in-text (e.g. `from section 3 of design doc "
        f"\"docs/specs/feature.md\": …`), never by pasting it; the referenced section "
        f"is delivered to whoever answers automatically.",
        f"- `jarvis wo assume {wo['id']} \"...\"` for a call you made with NO doubt. "
        f"Record every one, including the small ones.",
        f"- Work only inside your worktree (you start in it).",
        f"- File deferred work instead of leaving notes: `jarvis backlog add "
        f"{project.name} \"...\"` — including anything you decided was OUT of this "
        f"feature's scope.",
        # conditional for the same reason as the worker's: no index, no instruction to
        # go and read one
        *([f"- READ the OS knowledge base before you decompose — it is indexed at the "
           f"end of this prompt, not pasted into it: `jarvis learn search \"<term>\" "
           f"--project {project.name}` and `jarvis learn show <id>`. A plan built "
           f"without it will hand children the lessons the fleet already paid for, "
           f"again."] if knowledge else []),
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


def _manager_prompt(wo: dict[str, Any], project: ProjectSpec,
                    knowledge: KnowledgeBrief | None = None,
                    feature: dict[str, Any] | None = None) -> str:
    """The briefing for a feature order's project manager.

    Three things make it different from a worker's, and each is why the worker contract
    cannot simply be reused with a line struck out:

    * **It produces no code.** Every load-bearing sentence of `worker_brief.core_contract`
      is about producing a change — the worktree, the `[wo-…]` pull request title,
      `jarvis wo finish`. A manager that read them would open a pull request against a
      feature it is supposed to be coordinating, which is the exact failure this branch
      exists to prevent.
    * **It is idle by design, and long-lived.** So the prompt says so in as many words.
      A session told to work autonomously toward a complete solution will invent work
      when its inbox is empty; this one is told that an empty inbox IS the finished state
      between messages, and that it never settles itself (`Daemon.settle_features` closes
      it when its feature closes).
    * **It sees the feature, not a slice of it.** It is the only session that reasons
      about the whole ask and every child at once, so both ride in the prompt — the ask
      from the feature order, the children read live at dispatch (`feature_context`).

    The last line of the contract is the one that keeps principle 1 of the design intact:
    a manager does not learn who sends it messages. Everything it receives arrives as an
    ordinary user turn through the message queue, posted to a ROLE by something that never
    named this work order — and a manager that went looking for the sender would couple
    the two ends the bus exists to keep apart.
    """
    fo = (feature or {}).get("fo") or {}
    children = (feature or {}).get("children") or []
    fo_id = wo.get("parent_id") or "?"
    parts = [
        f"You are the PROJECT MANAGER for Jarvis feature order `{fo_id}` in project "
        f"`{project.name}`, running as work order `{wo['id']}`.",
        "",
        f"# Feature order: {fo.get('title') or wo['title']}",
        "",
        "## The original ask",
        fo.get("description") or "(no further description — the title is the ask)",
        "",
        "## Its work orders",
        *([f"- `{c['id']}` [{c['status']}] {c['title']}" for c in children]
          or ["(none yet)"]),
        "",
        "# Your job: the feature's follow-through",
        "You own this feature order's follow-through — not its code. **You will not "
        "write product code and you will not open a pull request.** The children above "
        "do that, each in its own session and its own worktree.",
        "",
        "**You will receive messages. Act on each one and end your turn.** Between "
        "messages you are idle, and that is correct — it is what this session is for. "
        "Do not go looking for work, do not review the children unasked, and do not try "
        "to finish this work order: it ends when the feature ends, and the OS does that "
        "for you.",
        "",
        "## Review feedback on the feature",
        "An independent review of this feature as a whole can come back with concrete "
        "asks. When it does, it arrives as a message. Decide what actually has to "
        "change, then file a work order UNDER THIS FEATURE for each thing that does:",
        "",
        f"    jarvis wo create {project.name} \"<title>\" -d \"<the whole brief>\" "
        f"--parent {fo_id}",
        "",
        "`--parent` is what makes it part of the feature: the feature waits for it and "
        "shows it in its tree. Without the flag you would be filing unrelated work that "
        "the feature settles without. The worker who picks it up sees only that "
        "description and has never read this conversation — brief it as a stranger. Then "
        "resubmit the feature's evidence once they have landed. Judge the feedback rather "
        "than obeying it: if an ask is wrong, say so in your answer and say what you did "
        "instead.",
        "",
        "## A deferral request",
        "A work order may report something worth doing that is not its job. File it on "
        "the backlog, recording where it came from — which work order suggested it, and "
        f"that it came out of {fo_id}:",
        "",
        f"    jarvis backlog add {project.name} \"<title>\" -d \"<the brief>\" \\",
        f"        --origin-wo <the work order that suggested it> --origin-fo {fo_id} \\",
        "        --origin-note \"<why it was deferred>\"",
        "",
        "The message you receive spells that command out with its values already filled "
        "in; run it as it stands. Use the flags rather than writing the relationship "
        "into the description: they are columns, so a reader months from now can ask the "
        "backlog where an item came from instead of hoping somebody wrote it down. "
        "Filing it is the whole action — you are not being asked to schedule it.",
        "",
        "**You do not know who sends you these messages. Do not try to find out.** "
        "Whoever it was addressed a role, not you, and never learns who read it.",
        "",
        "# Operating contract",
        f"- **Neo is your first responder. Any doubt goes to it.** `jarvis wo ask "
        f"{wo['id']} \"<your question>\"`, then END YOUR TURN; the answer arrives as your "
        f"next user turn. The trigger is DOUBT, not importance. A question is one "
        f"paragraph: the decision, the options, your recommendation.",
        f"- `jarvis wo assume {wo['id']} \"...\"` for a call you made with NO doubt. "
        f"Record every one: this work order's record is the only account anyone gets of "
        f"why the feature changed shape.",
        "- Do not run `jarvis wo finish` and do not open a pull request. Neither applies "
        "to you.",
        f"- Read what the fleet already knows before you decide anything: `jarvis learn "
        f"search \"<term>\" --project {project.name}`."
        if knowledge else
        f"- Record what you learn: `jarvis learn add \"...\" --project {project.name}`.",
        f"- Alert the human when needed: `jarvis notify --project {project.name} "
        f"--level warning|critical \"title\" \"body\"`",
        "- Hit a bug in Jarvis OS itself? Use your `report-jarvis-bug` skill, then carry "
        "on.",
        "",
        "# What the outside world sees",
        "The work order record IS this conversation, as far as anyone else is concerned. "
        "The last message of every turn you take is captured verbatim into it, and the "
        "user and Neo decide from that record — neither will ever open this session. End "
        "every turn with the complete answer: what you were asked, what you decided, "
        "what you filed, and what you deliberately did not do.",
    ]
    if knowledge:
        parts += render_knowledge_block(knowledge, project.name)
    return "\n".join(parts)


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

    The full text lives in `worker_brief.gates_section` (single source with
    `jarvis brief gates`); this shape survives for the planner's `_common_briefing`
    tail and for tests that compare the worker-facing gate surfaces.
    """
    from . import worker_brief

    return worker_brief.gates_section(
        wo["id"], enabled=tuple(project.gates.enabled)).splitlines()


def dispatch_work_order(
    store: ProjectStore,
    central: CentralStore,
    project: ProjectSpec,
    wo: dict[str, Any],
    os_config: OsConfig | None = None,
) -> dict[str, Any]:
    """Open the worker's conversation for a work order already in `dispatching` state.

    Dispatch composes what the worker is told — the prompt, the settings file, the
    resolved model/effort/permission mode — and hands the running of it to
    `worker_session`, which owns the transport.
    """
    from . import worker_session

    cfg = os_config or OsConfig()
    knowledge = central.knowledge_brief(
        project.name,
        pinned_limit=cfg.knowledge_inject_limit,
        digest_limit=cfg.knowledge_digest_limit,
        digest_chars=cfg.knowledge_digest_chars,
    )
    prompt = build_worker_prompt(wo, project, knowledge,
                                 design_doc=materialize_design_doc(store, project, wo),
                                 feature=feature_context(store, wo))

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
