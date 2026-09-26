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
from .project_store import MAX_DISPATCH_ATTEMPTS, ProjectStore


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

    from . import agent_usage, concision, wiring
    from .bootstrap import build_settings, deep_merge
    from .paths import jarvis_home

    settings = build_settings(project.settings_overrides)
    settings.pop("_jarvis", None)

    # WHAT THE PROJECT DESELECTED on /config, and the only place a deselection takes
    # effect: the user's own Claude configuration is read to populate that page and
    # never written. Merged before the rules below so the permission block can ask
    # whether Serena survived. See `wiring.settings_patch`.
    settings = deep_merge(settings, wiring.settings_patch(project.wiring))
    serena = wiring.serena_wired(project.wiring)

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
        # granted headlessly or falls back to grep having wasted a call. Omitted
        # entirely when the project has deselected Serena — a rule naming an absent
        # tool is inert, but a settings file that both removes a server and grants its
        # tools is the incoherence this feature exists to avoid.
        *(serena_allow_rules() if serena else ()),
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
        # Whether the navigation briefing may say "Serena first" — `jarvis brief
        # navigation` is a separate process and would otherwise recommend a server this
        # same file has just removed. Travels as env for `JARVIS_GATES`' reason: the
        # answer is fixed at spawn, and a per-call catalog read would be a second
        # source of truth that can disagree with the settings beside it.
        "JARVIS_SERENA": "1" if serena else "0",
        # The `jarvis wo finish --summary` word cap the PreToolUse hook enforces
        # (spec 2026-09-19 SS5.3). Env for `JARVIS_GATES`' reason, and more sharply:
        # `hooks.finish_summary_decision` runs on EVERY Bash command, and a catalog
        # parse there would be a 39% tax on a ~155ms hook process.
        "JARVIS_SUMMARY_MAX_WORDS": str(project.concision.summary_max_words),
        # The standing worker instructions, for the `SubagentStart` hook to re-inject:
        # a subagent inherits none of its parent's `--append-system-prompt`. Resolved
        # exactly as `worker_session.briefing` resolves it, so the subagent is told what
        # the worker was told. Env for `JARVIS_GATES`' reason — `concision` must not
        # import `catalog` (see its module docstring).
        concision.STANDING_PROMPT_ENV: (wo.get("append_system_prompt")
                                        or project.worker.append_system_prompt or ""),
        # Buy the 5-minute prompt cache (write 1.25x) instead of the 1-hour one (2x),
        # which Claude Code would otherwise pick for a headless session. Taken from
        # `claude_cli` rather than spelled again: the settings file and the spawn
        # environment must not be able to disagree about what TTL Jarvis buys.
        # Measurements and the reversal criteria: kn-f94abf34, and docs/superpowers/
        # specs/2026-08-22-the-five-minute-write-everywhere.md.
        **claude_cli.PROMPT_CACHE_5M_ENV,
        # WHAT A TURN IS, for every hook that would otherwise assume it. Taken from
        # `claude_cli` for the reason the cache flag above is: the launcher and the
        # settings file must not be able to disagree. §3 of docs/superpowers/specs/
        # 2026-09-23-the-crew-a-worker-must-use.md.
        claude_cli.TURN_TRANSPORT_ENV: claude_cli.TRANSPORT_HEADLESS,
        # Whether the lead must delegate its file edits to the crew (§7 of that spec).
        # Env for `JARVIS_GATES`' reason: `hooks.crew_edit_decision` runs on every file
        # write and must not parse the catalog to decide it has nothing to do.
        "JARVIS_REQUIRE_CREW": "1" if project.worker.require_crew else "0",
        # The crew is the ordinary worker's; a planner has its own team prose. Carried as
        # env rather than read at hook time for the same reason as the key above.
        "JARVIS_WO_KIND": str(wo.get("kind") or "worker"),
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
    """Put this child's SECTION of the feature spec, and the spec, where it can read them.

    A child's worktree branches from the default branch, but the spec lives on the
    PLANNER's unmerged branch — so the snapshot taken at `fo plan` time is written under
    the project's shared `.jarvis/` tree, which workers already read (agent skills live
    there). Returns what the prompt renders (`repo_path`, `path`, and `section` /
    `section_path` when the child's section resolved), or None when the work order has no
    parent or its plan names no document. Idempotent: later dispatches of siblings rewrite
    the same bytes.

    §4 of docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md is why the
    section is written as its own file rather than only named: pointing a worker at a
    whole spec is what made every child read all of it.
    """
    from . import specs

    spec = specs.spec_of(store, wo)
    if spec is None:
        return None
    return specs.materialize(project.path, wo["parent_id"], wo["id"], spec)


def feature_context(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """The parent order a MANAGER or an ANALYST needs. None for anything else.

    Read at dispatch rather than snapshotted at creation, and passed in rather than
    looked up inside `build_worker_prompt`, for the same two reasons
    `materialize_design_doc` is shaped this way: the prompt builder stays a pure function
    of its arguments, and the manager's children change under it — it files more of them
    as the feature runs, so anything frozen at release would be wrong by its second turn.

    An ANALYST gets the row and an EMPTY `children` list rather than its parent's
    children: an improvement order has none, and the orders it proposes are filed
    independently (§1.3 of the improvement-orders spec). Same key so the manager path is
    untouched.
    """
    if wo.get("kind") not in ("manager", "analyst") or not wo.get("parent_id"):
        return None
    try:
        fo = store.get_feature_order(wo["parent_id"])
    except KeyError:
        return None  # a briefing is not the place to raise on a deleted parent
    if wo.get("kind") == "analyst":
        return {"fo": fo, "children": []}
    return {"fo": fo, "children": store.feature_children(fo["id"])}


def build_worker_prompt(wo: dict[str, Any], project: ProjectSpec,
                        knowledge: KnowledgeBrief | None = None,
                        design_doc: dict[str, str] | None = None,
                        feature: dict[str, Any] | None = None) -> str:
    """What the worker is told, composed from the work order and its project.

    Four kinds of work order get four shapes. A WORKER opens with the minimum — its
    identity, the work order, a compressed contract of only the load-bearing
    invariants — plus an index of the full briefings it can fetch on demand with
    `jarvis brief <section>` (single-sourced in `worker_brief`, so the CLI and this
    prompt cannot drift). A PLANNER keeps its full prompt: one session per feature,
    already reviewed as a unit. A MANAGER gets neither: it writes no product code, so
    every line of the worker contract about worktrees, pull requests and finishing would
    be an instruction to do something it must not do. An ANALYST is the same case for the
    same reason (§3.1 of the improvement-orders spec): falling through to the worker
    contract would tell it to open a pull request, which is the one thing it must never
    do. The surfaces around the contract — the pre-approval marker and the knowledge
    index — are identical for all four.
    """
    if wo.get("kind") == "planner":
        return _planner_prompt(wo, project, knowledge)
    if wo.get("kind") == "manager":
        return _manager_prompt(wo, project, knowledge, feature)
    if wo.get("kind") == "analyst":
        return _analyst_prompt(wo, project, knowledge, feature)
    from . import wiring, worker_brief
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
            "# Your section of the feature spec",
            *([f"This work order implements section \"{design_doc['section']}\" of "
               f"`{design_doc['repo_path']}`, and that section — not the brief above — "
               f"is the source of truth for WHAT to build. It is materialised at "
               f"{design_doc['section_path']}: read it first.",
               ] if design_doc.get("section_path") else []),
            *([f"The whole spec is at {design_doc['path']} if the section is not enough. "
               f"Both are read-only snapshots; the authoritative copy is on the "
               f"planner's branch.",
               ] if design_doc.get("path") else []),
        ] if design_doc else []),
        "",
        *worker_brief.core_contract(wo["id"], wo["title"], project.name,
                                    bool(knowledge), live_gates,
                                    kind=str(wo.get("kind") or "worker")),
        "",
        *worker_brief.section_index(wo["id"], gated=bool(project.gates),
                                    serena=wiring.serena_wired(project.wiring)),
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


def _navigation_briefing(serena: bool = True) -> list[str]:
    """Serena before grep — the full text lives in `worker_brief` (single source
    with `jarvis brief navigation`); this shape survives for the planner's
    `_common_briefing` tail."""
    from . import worker_brief

    return worker_brief.navigation_section(serena).splitlines()


def _common_briefing(parts: list[str], wo: dict[str, Any], project: ProjectSpec,
                     knowledge: KnowledgeBrief | None = None) -> list[str]:
    """The full-briefing tail — now only the PLANNER's prompt carries it inline.

    A worker's prompt reaches the same text through `worker_brief.section_index`
    and `jarvis brief <section>` instead; the pre-approval marker and the knowledge
    index are the parts a worker still gets inline, composed in
    `build_worker_prompt` directly.
    """
    from . import wiring

    parts += ["", *_navigation_briefing(wiring.serena_wired(project.wiring))]
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
        '  "design_doc": "docs/specs/<feature>.md — the spec you wrote, relative to the '
        'repo root. REQUIRED, and it must already be COMMITTED on your branch — the '
        'reviewer is sent the committed text, never your working tree",',
        '  "justification": "only if you exceed the child cap — why it cannot be fewer",',
        '  "children": [',
        "    {",
        '      "key": "schema",',
        '      "title": "short imperative title, as a work order would have",',
        '      "spec_section": "3 — the section of the spec this child implements, by '
        'number or by heading text",',
        '      "description": "what the section does NOT say — see below",',
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
        "## THE SPEC IS THE DELIVERABLE. The plan is an index into it.",
        "Write the feature's spec FIRST — a markdown file in your worktree (convention: "
        "`docs/`), with numbered sections — and name it in `design_doc`. Commit it "
        "before you submit; a plan that names no spec, or names one that is not "
        "committed on your branch, is refused — writing the file is not enough, the "
        "reviewer only ever sees the committed text. Everything you know because you "
        "read the whole feature — the architecture, the data model, the interfaces, the "
        "traps — goes THERE, once, and is never repeated into a brief.",
        "",
        "**Cut the spec's sections along the feature's FUNCTIONAL boundaries, because "
        "the sections are the split.** One section, one work order: every child names "
        "its section in `spec_section`, no two children may name the same one, and a "
        "child is handed that section and only that section. If a section cannot be one "
        "worker's job, it is not a section — and if two children want the same one, the "
        "seam is in the wrong place. Decide the boundaries while WRITING the spec; do "
        "not write prose first and carve it afterwards.",
        "",
        "**The spec ends with an `Agent profile` appendix, and it is not decoration.** "
        "The OS builds a Claude Code agent type from that section and every child work "
        "order of this feature runs AS it — so write it as a system prompt in the second "
        "person: the role, what it must know about this codebase, the conventions it "
        "must follow, the traps it must avoid, what it must never do. It is deleted when "
        "the feature order settles and can be rebuilt from the spec, so the spec is the "
        "only place it may live.",
        "",
        "Each child is dispatched into a NEW session with a worker that sees its own "
        "description plus its section, and nothing else — not this plan, not this "
        "conversation, not what its siblings are doing. So a description is not a brief "
        "any more, it is the MARGIN around one: what the section does not say. The scope "
        "boundary (what this piece must not touch), what done means, and which sibling "
        "owns the thing it must not touch. Everything about WHAT to build is in the "
        "section, and repeating it here is the duplication this whole shape exists to "
        "remove.",
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
        f"edge of \"the margin, not the brief\", and it is not negotiable by writing "
        f"more carefully: if the piece needs more than that to explain, the explanation "
        f"belongs in its section of the spec",
        "- a plan naming no `design_doc`, or one naming a file not committed on your "
        "branch",
        "- a child with no `spec_section`, a `spec_section` matching no heading in the "
        "spec, two children claiming the same section, or a child claiming the `Agent "
        "profile` appendix",
        "- a spec with no `Agent profile` section, or one too short to brief an agent",
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


def _evidence_checklist(refs: list[str]) -> list[str]:
    """One line per stored evidence ref, each naming the command that reads that shape.

    §3.3 of docs/superpowers/specs/2026-09-23-improvement-orders.md: resolution is the
    analyst's job, naming the verb is the OS's, so the analyst does not spend its first
    turn guessing which command reads which kind of reference. An UNRECOGNISED ref is
    still listed, with a note to work out how to read it — dropping it would hide
    evidence the user chose to attach. A ref that does not resolve is a FINDING about the
    OS's records, not an error here.
    """
    lines = []
    for ref in refs:
        ref = str(ref).strip()
        if not ref:
            continue
        if ref.startswith("wo-"):
            how = f"`jarvis wo show {ref}`"
        elif ref.startswith("fo-"):
            how = f"`jarvis fo show {ref}`"
        elif ref.startswith("io-"):
            how = f"`jarvis io show {ref}`"
        elif ref.startswith("al-"):
            how = "`jarvis alarms` — find this alarm in the list"
        elif ref.startswith("#") and ref[1:].isdigit():
            how = f"`gh issue view {ref[1:]}`"
        elif "://" in ref:
            how = (f"`gh pr view {ref}`" if "/pull/" in ref
                   else "fetch it")
        else:
            how = "work out how to read it, and say so in the report if you cannot"
        lines.append(f"- [ ] {ref} — {how}")
    return lines


def _analyst_prompt(wo: dict[str, Any], project: ProjectSpec,
                    knowledge: KnowledgeBrief | None = None,
                    feature: dict[str, Any] | None = None) -> str:
    """The briefing for an improvement order's analyst — §3.2 of
    docs/superpowers/specs/2026-09-23-improvement-orders.md.

    STATIC, unlike a child of a feature order: there is no spec to build an agent type
    from, so `specs.install_agent` is never called for an improvement order. It carries
    `_common_briefing` — the knowledge index and the navigation ranking — because it
    reads records and code for a living.

    "You analyse, you do not build" is PROSE, not enforcement, and that is worth stating
    plainly rather than leaving for someone to discover. Same reason as the planner's: an
    analyst is a work order, not a subagent, so its only lever is the `permissions.deny`
    path `_write_worker_settings` writes — and a deny broad enough to stop product code
    also stops it writing the `report.json` it is REQUIRED to submit.

    It gets no planning seats (`bootstrap.install_agent_assets` gives those to
    `kind == "planner"` only). A diagnosis is one reading of one record set; the seats
    exist to argue about a DECOMPOSITION, and they have no `Bash`, so they could not read
    the evidence anyway.
    """
    from . import db
    from .ops import EVIDENCE_REFS_KEY  # lazy: ops imports dispatch

    io_id = wo.get("parent_id") or "?"
    fo = (feature or {}).get("fo") or {}
    metadata = db.from_json(fo.get("metadata"), {}) or {}
    refs = list(metadata.get(EVIDENCE_REFS_KEY) or [])
    parts = [
        f"You are the ANALYST for Jarvis improvement order `{io_id}` in project "
        f"`{project.name}`, running as work order `{wo['id']}`.",
        "",
        f"# The observation: {wo['title']}",
        "",
        wo.get("description") or "(no further description — the title is the ask)",
        "",
        "# Your job: a diagnosis, not a fix",
        "You are an ANALYST. You do not fix anything. You produce a diagnosis. A session "
        "that returns a working fix has FAILED even if the fix is good — the improvement "
        "order exists because the cheapest fix that turns a symptom green keeps winning, "
        "and one more of those is not what is wanted here.",
        "",
        "**Argue against the obvious fix.** For every finding, state the cheapest fix "
        "that would turn the symptom green and say why it is insufficient. If a "
        "finding's recommendation IS that cheapest fix, say so, and say why that is "
        "nevertheless the right call.",
        "",
        "# Read the evidence through the CLI, never the databases",
        "Every record you need has a verb. Do not open a SQLite file, and do not read "
        "state under `.jarvis/` by hand:",
        "- `jarvis wo show <id>` / `jarvis wo list [project]` — a work order and its "
        "timeline, messages and assumptions",
        "- `jarvis inspect <wo-id|fo-id>` — where the time went, turn by turn",
        "- `jarvis validation show <wo-id|fo-id>` — every panel seat's verdict and reply",
        "- `jarvis cost <project|wo-id|fo-id>` — what it cost, split worker vs Jarvis",
        "- `jarvis alarms [project]` — turns the OS raised while they burned",
        "- `jarvis issues [project]` — tracker issues the fleet keeps hitting",
        "- `jarvis learn search \"<term>\"` / `jarvis learn show <id>` — what the fleet "
        "already knows",
        "- `gh pr view <url>` / `gh pr diff <url>` — a pull request and what it changed",
        *(["",
           "## The evidence you were given",
           "Read every one of these before you write anything. A ref that does not "
           "resolve is itself a FINDING about the OS's records — report it, do not stop:",
           *_evidence_checklist(refs)] if refs else []),
        "",
        "# Quote your evidence, verbatim",
        "A root cause asserted without a verbatim line — from a timeline, an `inspect` "
        "reading, a validation transcript, a diff — is an opinion, not a finding. "
        "Paraphrase is not a quote. Copy the line as it is printed, and name the command "
        "that printed it.",
        "",
        "# The report",
        "Write it to a JSON file in your worktree and submit it with the command below, "
        "which IS your finish. Do not run `jarvis wo finish` — submitting the report "
        "settles this work order for you. `--from-file` and not an inline argument, "
        "because the gate classifier's quote-blanking fails on nested and mixed quoting "
        "and a report is full of repo paths and quoted log lines.",
        "",
        f"    jarvis io report {io_id} --from-file report.json",
        "",
        "```json",
        "{",
        '  "summary": "one line: what is going wrong, across all findings",',
        '  "justification": "top-level and optional: why this needed more than 6 '
        'findings",',
        '  "findings": [',
        "    {",
        '      "key": "background-jobs",',
        '      "symptom": "what was observed, and where",',
        '      "root_cause": "why it happens, in mechanism terms",',
        '      "evidence": [',
        '        {"source": "jarvis wo show wo-dd8668fa", "quote": "the verbatim line"}',
        "      ],",
        '      "why_insufficient": "the cheapest green-making fix, and why it is wrong",',
        '      "recommendation": "what to do instead",',
        '      "proposed_orders": [',
        '        {"type": "work", "project": "<project>", "title": "...",',
        '         "description": "the full brief, standing alone"}',
        "      ]",
        "    }",
        "  ]",
        "}",
        "```",
        "",
        "`key` is a short lowercase slug, unique within the report — it is how the user "
        "and the dashboard address one finding. `type` is `work` or `feature`. "
        "`proposed_orders` MAY be empty: \"do nothing, and here is why\" is a legitimate "
        "recommendation, and inventing work to fill the list is worse than an empty one.",
        "",
        "The validator names every problem at once, so one revision fixes all of them.",
        "",
        "# Scope",
        # §4.3 of the improvement-orders spec owns this cap; `findings.MAX_FINDINGS` is
        # the constant that enforces it. Written literally rather than imported: the
        # validator module is a sibling's and importing it would couple dispatch to it.
        "- At most 6 findings, ranked worst first, unless your report carries a "
        "top-level `justification` saying why it cannot be fewer. This is the ATTENTION "
        "cap: a report the user will not read changes nothing.",
        "- You PROPOSE orders. You do not file them — no `jarvis wo create`, no "
        "`jarvis fo create`. The user decides each finding, and the OS files what they "
        "accept.",
        "- You write no product code, and you open NO pull request. Reading code and "
        "writing a throwaway script to understand it is fine and expected; shipping "
        "anything is not the job. This is stated as prose and nothing enforces it — you "
        "must be able to write `report.json`, so no permission rule can separate the two.",
        "",
        "# Operating contract",
        f"- **Neo is your first responder. Any doubt goes to it.** `jarvis wo ask "
        f"{wo['id']} \"<your question>\"`, then END YOUR TURN; the answer arrives as your "
        f"next user turn, usually within a minute. The trigger is DOUBT, not importance.",
        f"- `jarvis wo assume {wo['id']} \"...\"` for a call you made with NO doubt. "
        f"Record every one, including the small ones.",
        "- Work only inside your worktree (you start in it).",
        *([f"- READ the OS knowledge base before you diagnose — it is indexed at the end "
           f"of this prompt, not pasted into it: `jarvis learn search \"<term>\" "
           f"--project {project.name}` and `jarvis learn show <id>`."] if knowledge else []),
        f"- The OS knowledge base is the ONLY memory that survives you: "
        f"`jarvis learn add \"...\" --project {project.name} --topic \"<topic>\"`.",
        "- Hit a bug in Jarvis OS itself? Use your `report-jarvis-bug` skill, then carry "
        "on.",
        "",
        "# What the outside world sees",
        "The work order record IS this conversation, as far as anyone else is concerned. "
        "The last message of every turn you take is captured verbatim into it, and the "
        "user decides from that record — they will never open this session. End every "
        "turn with the complete answer: what you found, what you could not read, what "
        "you are unsure about, and absolute paths.",
        "",
        "Work autonomously toward a submitted report. User feedback may arrive as new "
        "user turns; treat it as authoritative.",
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
        "description and has never read this conversation — brief it as a stranger. "
        "Judge the feedback rather than obeying it: if an ask is wrong, say so in your "
        "answer and say what you did instead.",
        "",
        "**Once they have landed, submit the feature for review again.** Nothing else "
        "will: a feature order runs no session, so this is the only way it goes back to "
        "the reviewer, and a feature nobody resubmits waits for ever.",
        "",
        f"    jarvis fo submit {fo_id} --summary \"<what changed since last time>\" \\",
        "        --evidence \"<how the feature as a whole was verified>\"",
        "",
        "Wait until every work order under the feature is COMPLETED — a review reads the "
        "code that has actually merged, so submitting while a child is still in flight "
        "spends one of a small number of rounds on a diff that is missing the fix. The "
        "reviewer compares your `--evidence` against that diff, so claim only what it "
        "supports.",
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
    from . import budget, worker_session

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
        # Which configuration this ran under. NULL until the ledger holds a version —
        # "before the console existed", never version 1 (config-console design §5).
        "config_version": (central.head_config_version() or {}).get("id"),
    }
    store.update_work_order(wo["id"], **resolved)
    # A CHILD TAKES ITS SLICE OF THE FEATURE'S BUDGET HERE, and here is the only place it
    # ever does: reserve-on-dispatch is what stops two children claimed in the same tick
    # being handed the same remainder (src/jarvis/budget.py). No-op for a standalone
    # order and for a child whose feature has no budget, which is nearly all of them.
    budget.reserve(store, central, wo)
    wo = store.get_work_order(wo["id"])

    try:
        turn = worker_session.start(store, project, wo, prompt)
    except budget.BudgetExhausted as e:
        # Spent before it ever ran a turn — its feature had nothing left to lend it, or
        # the panel and Neo spent the order's own budget on an earlier round. Not a
        # dispatch FAILURE: nothing is broken and the work has not been judged, so it
        # goes to the user as what it is (`budget.escalate` writes the timeline entry,
        # the flag and the notification) rather than to `failed`.
        budget.escalate(store, wo, e.exhausted)
        return store.get_work_order(wo["id"])
    except claude_cli.ClaudeCliError as e:
        # A TURN THAT COULD NOT BE LAUNCHED IS NOT A WORK ORDER THAT FAILED. `failed` at
        # attempts=0 turned a blip in the transport into a terminal state and asked the
        # user to look at a work order nothing was wrong with — spec
        # docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §5. Back to
        # `pending` behind a backoff; only the ceiling is terminal, and it says why.
        outcome = store.release_dispatch_claim(wo["id"], str(e))
        store.add_event(wo["id"], "dispatch_failed",
                        {"error": str(e)[:500], "outcome": outcome})
        if outcome == "failed":
            store.flag_attention(
                wo["id"],
                f"could not be dispatched after {MAX_DISPATCH_ATTEMPTS} attempts — "
                f"`claude` was unreachable every time, so no turn ever ran: {e}")
            store.add_notification(
                title=f"Dispatch failed for {wo['id']}",
                body=f"`claude` could not be reached {MAX_DISPATCH_ATTEMPTS} times in "
                     f"a row. No turn has run.\n\n{e}",
                level="warning",
                wo_id=wo["id"],
                source="jarvisd",
            )
        raise

    store.clear_dispatch_attempts(wo["id"])
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
