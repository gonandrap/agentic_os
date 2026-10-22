"""Dispatch: turn a claimed work order into a running Claude Code worker"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import claude_cli
from .catalog import OsConfig, ProjectSpec
from .central_store import CentralStore, KnowledgeBrief
from .project_store import MAX_DISPATCH_ATTEMPTS, WORKER_CHILDREN, ProjectStore


def _worker_path() -> str:
    """Daemon PATH, with the directory holding `jarvis` prepended"""
    path = os.environ.get("PATH", "")
    exe = shutil.which("jarvis") or sys.executable
    bindir = str(Path(exe).parent)
    if bindir not in path.split(os.pathsep):
        path = f"{bindir}{os.pathsep}{path}"
    return path


# Serena's READ-ONLY tool surface
SERENA_READ_TOOLS = (
    "activate_project", "get_symbols_overview", "find_symbol",
    "find_referencing_symbols", "find_declaration", "find_implementations",
    "search_for_pattern", "find_file", "list_dir", "list_memories", "read_memory",
)

# A plugin install produces the long prefix, `claude mcp add serena` the short one
SERENA_TOOL_PREFIXES = ("mcp__serena__", "mcp__plugin_serena_serena__")


# The two undocumented keys in the 2.1.284 binary that decide `auto` mode's bash-first steer:... (docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md)
BASH_FIRST_ENV = "CLAUDE_CODE_THRIFTY_SONIC"
BASH_FIRST_VARIANT_ENV = "CLAUDE_CODE_COZY_TEAPOT"


def bash_first_env(bash_first: str) -> dict[str, str]:
    """The env for one `worker.bash_first` value — spec §2"""
    if bash_first == "cli":
        return {}
    if bash_first == "off":
        return {BASH_FIRST_ENV: "false"}
    return {BASH_FIRST_ENV: "true", BASH_FIRST_VARIANT_ENV: bash_first}


def cache_ttl_env(cache_ttl: str) -> dict[str, str]:
    """The env for one `worker.cache_ttl` value (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)"""
    if cache_ttl == "cli":
        return {}
    if cache_ttl == "1h":
        return {claude_cli.PROMPT_CACHE_1H_ENV_KEY: "1"}
    return dict(claude_cli.PROMPT_CACHE_5M_ENV)


# Whether Claude Code DEFERS MCP tools behind `ToolSearch` or lists them with full schemas. §4 of... (docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md)
TOOL_SEARCH_ENV = "ENABLE_TOOL_SEARCH"


def tool_search_env(tool_search: str) -> dict[str, str]:
    """The env for one `worker.tool_search` value — spec §4"""
    if tool_search == "cli":
        return {}
    if tool_search == "off":
        return {TOOL_SEARCH_ENV: "false"}
    return {TOOL_SEARCH_ENV: "true"}


def serena_allow_rules() -> list[str]:
    """`permissions.allow` entries for every read-only Serena tool, under both prefixes"""
    return [f"{prefix}{tool}"
            for prefix in SERENA_TOOL_PREFIXES for tool in SERENA_READ_TOOLS]


def _write_worker_settings(project: ProjectSpec, wo: dict[str, Any]) -> Path:
    """Merge the project's injected settings with per-work-order env and persist them for --settings"""
    import json as _json

    from . import agent_usage, concision, hooks, wiring, worker_session
    from .bootstrap import build_settings, deep_merge
    from .paths import jarvis_home

    settings = build_settings(project.settings_overrides)
    settings.pop("_jarvis", None)

    # WHAT THE PROJECT DESELECTED on /config
    settings = deep_merge(settings, wiring.settings_patch(project.wiring))
    serena = wiring.serena_wired(project.wiring)

    # Declarative worker permissions: full edit rights inside its own worktree
    proj_abs = str(project.path).lstrip("/")
    wt_abs = f"{proj_abs}/.claude/worktrees/{wo['id']}"
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    for rule in (
        f"Edit(//{wt_abs}/**)",
        f"Write(//{wt_abs}/**)",
        f"NotebookEdit(//{wt_abs}/**)",
        f"Read(//{proj_abs}/**)",
        # Read-only code navigation
        *(serena_allow_rules() if serena else ()),
    ):
        if rule not in allow:
            allow.append(rule)
    # A bare-name deny drops the tool's schema from the worker's starting context... (docs/os/architecture/worker-context.md)
    deny = settings["permissions"].setdefault("deny", [])
    for tool in project.worker.denied_tools:
        if tool not in deny:
            deny.append(tool)

    # THE prefix-stability lever
    settings["includeGitInstructions"] = False

    # ONE COPY OF THE PROJECT'S CLAUDE.md reaches the worker
    resolved = project.path.resolve() if project.path.exists() else project.path
    excludes = [str(p) for p in (settings.get("claudeMdExcludes") or [])]
    for path in hooks.claude_md_excludes(
            resolved, resolved / ".claude" / "worktrees" / wo["id"]):
        rel = path.relative_to(resolved)
        for root in dict.fromkeys((resolved, project.path)):
            if str(root / rel) not in excludes:
                excludes.append(str(root / rel))
    if excludes:
        settings["claudeMdExcludes"] = excludes

    env = dict(settings.get("env") or {})
    env.update({
        "JARVIS_WO_ID": wo["id"],
        "JARVIS_PROJECT": project.name,
        "JARVIS_PROJECT_PATH": str(project.path),
        # The worker's jarvis calls must hit the same central state as the daemon
        "JARVIS_HOME": str(jarvis_home()),
        # Where TOKEN ACCOUNTING goes, pinned separately from JARVIS_HOME and read by `agent_usage` alone (#103)
        agent_usage.SPEND_HOME_ENV: str(jarvis_home()),
        # Workers call `jarvis …` from Bash (contract)
        "PATH": _worker_path(),
        # Which privileged actions the PreToolUse gate mediates for this worker
        "JARVIS_GATES": project.gates.to_json(),
        # Whether the navigation briefing may say "Serena first"
        "JARVIS_SERENA": "1" if serena else "0",
        # Whether the navigation briefing may say the symbol tools are DEFERRED
        "JARVIS_TOOL_SEARCH": project.worker.tool_search,
        # Whether `hooks.py_nav_decision` refuses a source-navigating Bash call at a `.py` path (spec...
        "JARVIS_PY_NAV_HOOK": project.worker.py_nav_hook,
        # Whether `hooks.doc_nav_decision` refuses a whole-file or oversized read of a SPEC (docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md)
        "JARVIS_DOC_NAV_HOOK": project.worker.doc_nav_hook,
        "JARVIS_DOC_READ_LIMIT_LINES": str(project.worker.doc_read_limit_lines),
        # The `jarvis wo finish --summary` word cap the PreToolUse hook enforces (spec 2026-09-19 SS5.3)
        "JARVIS_SUMMARY_MAX_WORDS": str(project.concision.summary_max_words),
        # The character cap on a `jarvis wo send` body
        concision.MESSAGE_CAP_ENV: str(project.concision.message_max_chars),
        # The per-kind caps the hook refuses an over-cap assumption or question at (docs/superpowers/specs/2026-10-21-questions-to-neo-are-atomic.md §3)
        concision.ASSUME_CAP_ENV: str(project.concision.assume_max_chars),
        concision.QUESTION_CAP_ENV: str(project.concision.question_max_chars),
        # The standing worker instructions
        concision.STANDING_PROMPT_ENV: (wo.get("append_system_prompt")
                                        or project.worker.append_system_prompt or ""),
        # WHICH PROMPT-CACHE TTL THIS WORKER'S WRITES ARE BILLED AT (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)
        **cache_ttl_env(project.worker.cache_ttl),
        # WHAT A TURN IS, for every hook that would otherwise assume it
        claude_cli.TURN_TRANSPORT_ENV: worker_session.transport_of(project, wo),
        # WHAT ONE MCP TOOL CALL MAY TAKE (#845)
        "MCP_TOOL_TIMEOUT": str(project.worker.mcp_tool_timeout_ms),
        # WHETHER `auto` MODE'S BASH-FIRST STEER REACHES THIS WORKER (spec §2, same file)
        **bash_first_env(project.worker.bash_first),
        # WHETHER THIS WORKER'S SYMBOL TOOLS ARE LISTED OR DEFERRED (spec 2026-10-02-serena-the-cheap-path.md...
        **tool_search_env(project.worker.tool_search),
        # Whether the lead must delegate its file edits to the crew (§7 of that spec)
        "JARVIS_REQUIRE_CREW": "1" if project.worker.require_crew else "0",
        # The crew is the ordinary worker's; a planner has its own team prose
        "JARVIS_WO_KIND": str(wo.get("kind") or "worker"),
        # The tracked files an installed TOOL rewrites
        hooks.TOOL_MANAGED_PATHS_ENV: _json.dumps(
            list(project.worktree.tool_managed_paths)),
    })
    settings["env"] = env
    out = project.path / ".jarvis" / "worker-settings" / f"{wo['id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_json.dumps(settings, indent=2))
    return out


def worker_name(wo: dict[str, Any]) -> str:
    """Background session display name"""
    return f"[WO {wo['id']}] {wo['title'][:60]}"


#: Largest overflow topics named in the roll-call; the rest are one `jarvis learn topics` away
OVERFLOW_TOPICS_SHOWN = 12


def render_knowledge_block(brief: KnowledgeBrief, project_name: str) -> list[str]:
    """The knowledge base as a map plus a retrieval verb, not as a payload"""
    lines = [
        "",
        f"# Knowledge base — {brief.total} entries visible to `{project_name}` "
        f"(this project + global)",
        "**This section is an INDEX, not the knowledge.** Headlines are truncated; the "
        "full text of an entry arrives only when you ask for it. If a headline below "
        "touches what you are about to do, FETCH IT — before you act on it, and before "
        "you ask Neo or record an assumption about it:",
        "```bash",
        f'jarvis learn search "<term>" --project {project_name}  # finds and ranks '
        f"matches: headline, id and a matching line",
        "jarvis learn show <id> [<id> ...]  # full text of specific entries — the ONLY "
        "verb that returns a body",
        f"jarvis learn list --project {project_name} --topic <t>  # everything in a topic",
        f"jarvis learn topics --project {project_name}  # what topics exist",
        "```",
        "Entries marked `(global)` came from another project; the rest are this one's.",
    ]
    if brief.pinned:
        cap = brief.pinned_chars
        lines += ["", "## Pinned — read these now" + (
            " (the rule; `jarvis learn show <id>` for the rest)" if cap
            else " (full text)")]
        for k in brief.pinned:
            topic = f" [{k['topic']}]" if k["topic"] else ""
            body = k["content"]
            if cap and len(body) > cap:
                body = body[:cap].rstrip() + f"… (`jarvis learn show {k['id']}`)"
            lines.append(f"- ({k['project'] or 'global'}{topic}) {body}")
    if brief.hints:
        # Above the index, because it is the most relevant thing in the section
        lines += [
            "",
            "## Matched your work order's TITLE — a HINT, not an instruction",
            "These entries' text matches words from your title. That is EVIDENCE they may "
            "be relevant, NOT a verdict that they apply: read them and decide. Headline "
            "and id only — `jarvis learn show <id>` for the body.",
        ]
        for k in brief.hints:
            scope = "" if k["project"] == project_name else " (global)"
            lines.append(f"- `{k['id']}`{scope} {k['headline']}")
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
        top = brief.overflow[:OVERFLOW_TOPICS_SHOWN]
        listed = ", ".join(f"{t or '(no topic)'} ({n})" for t, n in top)
        if len(brief.overflow) > len(top):
            listed += (f", and {len(brief.overflow) - len(top)} more topics "
                       f"(`jarvis learn topics --project {project_name}`)")
        lines += [
            "",
            f"## Not indexed above — {brief.overflow_count} further entries, by topic",
            f"{listed}",
            f"Reach them with `jarvis learn list --project {project_name} --topic <topic>` "
            f"or `jarvis learn search \"<term>\"`, which ranks matches and gives you ids to "
            f"`jarvis learn show`.",
        ]
    return lines


def materialize_design_doc(store: ProjectStore, project: ProjectSpec,
                           wo: dict[str, Any]) -> dict[str, str] | None:
    """Put this child's SECTION of the feature spec, and the spec, where it can read them (docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md)"""
    from . import specs

    spec = specs.spec_of(store, wo)
    if spec is None:
        return None
    return specs.materialize(project.path, wo["parent_id"], wo["id"], spec)


def dependency_context(store: ProjectStore, wo: dict[str, Any]) -> list[dict[str, Any]]:
    """What each dependency ACTUALLY recorded"""
    if wo.get("kind") not in ("worker", None):
        return []
    out: list[dict[str, Any]] = []
    for dep_id in store.dependencies(wo):
        try:
            dep = store.get_work_order(dep_id)
        except KeyError:
            # Same rule `feature_context` states for a deleted parent
            out.append({"id": dep_id, "status": "missing"})
            continue
        out.append({k: dep.get(k) for k in
                    ("id", "status", "title", "result_summary", "pr_url")})
    return out


def _dependency_block(dependencies: list[dict[str, Any]]) -> list[str]:
    """kn-5368cc71: three children of one feature order each burned a Neo question on the same false claim..."""
    lines = [
        "# What your dependencies ACTUALLY shipped",
        "Your brief's claims about what a dependency shipped are a HINT; this block is "
        "the record. Check it before you build on one.",
    ]
    for dep in dependencies:
        if dep.get("status") == "missing":
            lines.append(f"- `{dep['id']}` [missing] — this dependency's record is gone")
            continue
        lines.append(
            f"- `{dep['id']}` [{dep.get('status') or 'unknown'}] {dep.get('title') or ''}"
            f" — result: {dep.get('result_summary') or '(none recorded)'}"
            f" — PR: {dep.get('pr_url') or '(none)'}")
    return lines


def feature_context(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """The parent order a MANAGER or an ANALYST needs"""
    if (wo.get("kind") not in ("manager", "analyst", "investigator")
            or not wo.get("parent_id")):
        return None
    try:
        fo = store.get_feature_order(wo["parent_id"])
    except KeyError:
        return None  # a briefing is not the place to raise on a deleted parent
    if wo.get("kind") in ("analyst", "investigator"):
        # Neither has children: an improvement order and an investigation are one row plus one session
        return {"fo": fo, "children": []}
    return {"fo": fo, "children": store.feature_children(fo["id"], kinds=WORKER_CHILDREN)}


def build_worker_prompt(wo: dict[str, Any], project: ProjectSpec,
                        knowledge: KnowledgeBrief | None = None,
                        design_doc: dict[str, str] | None = None,
                        feature: dict[str, Any] | None = None,
                        dependencies: list[dict[str, Any]] | None = None) -> str:
    """What the worker is told, composed from the work order and its project (docs/superpowers/specs/2026-09-27-investigation-orders.md)"""
    if wo.get("kind") == "planner":
        return _planner_prompt(wo, project, knowledge)
    if wo.get("kind") == "manager":
        return _manager_prompt(wo, project, knowledge, feature)
    if wo.get("kind") == "analyst":
        return _analyst_prompt(wo, project, knowledge, feature)
    if wo.get("kind") == "investigator":
        return _investigator_prompt(wo, project, knowledge, feature)
    from . import os_map, wiring, worker_brief, worker_session
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
            # Spec 2026-10-06-navigate-specs-like-code.md §6: the path is an ARGUMENT
            *([f"The whole spec is snapshotted at {design_doc['path']} — navigate it, "
               f"never open it whole: `jarvis spec toc <path>` for its headings, "
               f"`jarvis spec section <path> <n|name>` for another section, "
               f"`jarvis spec search \"<words>\"` to find which spec covers a thing. "
               f"Both files are read-only snapshots; the authoritative copy is on the "
               f"planner's branch.",
               ] if design_doc.get("path") else []),
        ] if design_doc else []),
        # kn-5368cc71: the record, so the brief's dependency claims are checkable
        *(["", *_dependency_block(dependencies)] if dependencies else []),
        "",
        *worker_brief.core_contract(wo["id"], wo["title"], project.name,
                                    bool(knowledge), live_gates,
                                    kind=str(wo.get("kind") or "worker"),
                                    transport=worker_session.transport_of(project, wo),
                                    require_crew=project.worker.require_crew),
        "",
        *worker_brief.section_index(wo["id"], gated=bool(project.gates),
                                    serena=wiring.serena_wired(project.wiring)),
        # Spec 2026-10-01-the-steer-that-beat-the-brief.md §3
        *(["", *nav] if (nav := worker_brief.navigation_core(
            wiring.serena_wired(project.wiring),
            tool_search=project.worker.tool_search)) else []),
        *(["", *omap] if (omap := os_map.brief_block(wo, project.path)) else []),
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


def _navigation_briefing(serena: bool = True, tool_search: str = "cli") -> list[str]:
    """Serena before grep — the full text lives in `worker_brief` (single source with `jarvis brief..."""
    from . import worker_brief

    return worker_brief.navigation_section(serena, tool_search=tool_search).splitlines()


#: Kinds whose hooks refuse every write, so they never reach a gate
READ_ONLY_KINDS = ("investigator", "analyst")


def _common_briefing(parts: list[str], wo: dict[str, Any], project: ProjectSpec,
                     knowledge: KnowledgeBrief | None = None) -> list[str]:
    """The full-briefing tail — now only the PLANNER's prompt carries it inline"""
    from . import os_map, wiring

    parts += ["", *_navigation_briefing(wiring.serena_wired(project.wiring),
                                       tool_search=project.worker.tool_search)]
    if omap := os_map.brief_block(wo, project.path):
        parts += ["", *omap]
    pre_approved = _pre_approval(wo)
    if pre_approved:
        parts += ["", *_pre_approved_briefing(pre_approved)]
    if project.gates and wo.get("kind") in READ_ONLY_KINDS:
        # A read-only order cannot run a gated command; the protocol is one fetch away
        parts += ["", f"Privileged actions are gated; protocol: `jarvis brief gates --wo "
                      f"{wo['id']}`."]
    elif project.gates:
        parts += ["", *_gate_briefing(wo, project)]
    if knowledge:
        parts += render_knowledge_block(knowledge, project.name)
    return parts


def _planner_prompt(wo: dict[str, Any], project: ProjectSpec,
                    knowledge: KnowledgeBrief | None = None) -> str:
    """The briefing for a feature order's planner"""
    from . import worker_brief
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
        '  "design_doc": "docs/superpowers/specs/<feature>.md — the spec you wrote. '
        'Specs live ONLY in docs/superpowers/specs/; never create another spec '
        'directory. Relative to the repo root, REQUIRED, and it must already be '
        'COMMITTED on your branch — the reviewer is sent the committed text, never '
        'your working tree",',
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
        "Write the feature's spec FIRST — a markdown file in your worktree at "
        "`docs/superpowers/specs/<YYYY-MM-DD>-<slug>.md`, the ONE directory specs live "
        "in — with numbered sections, and name it in `design_doc`. Commit it "
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
        # §5, docs/superpowers/specs/2026-10-14-an-unanswered-refusal-must-name-itself.md
        f"- `jarvis fo plan {fo_id} --from-file <file>` IS your finish, and you submit "
        f"the plan ONCE. If the user refuses an assumption afterwards, do NOT resubmit "
        f"the plan: it would replace the stored one and discard the decisions already "
        f"recorded on it. Answer the refusal in this session, then run `jarvis wo "
        f"finish --summary \"…\"`, which records the re-delivery and does not touch the "
        f"plan.",
        f"- **Neo is your first responder. Any doubt goes to it.** `jarvis wo ask "
        f"{wo['id']} \"<your question>\"`, then END YOUR TURN; the answer arrives as your "
        f"next user turn, usually within a minute. The trigger is DOUBT, not importance. "
        f"For a planner the highest-value questions are about SCOPE — whether a piece "
        f"belongs in this feature at all — because that is the one thing you cannot "
        f"recover from by revising the decomposition. A question is one paragraph: the "
        f"decision, the options, your recommendation — arguing from your design "
        f"document by section in-text (e.g. `from section 3 of design doc "
        f"\"docs/superpowers/specs/feature.md\": …`), never by pasting it; the referenced section "
        f"is delivered to whoever answers automatically.",
        worker_brief.assume_shape(wo["id"],
                                  tail=" Record every one, including the small ones."),
        f"- Work only inside your worktree (you start in it).",
        f"- File deferred work instead of leaving notes: `jarvis backlog add "
        f"{project.name} \"...\"` — including anything you decided was OUT of this "
        f"feature's scope.",
        # conditional for the same reason as the worker's: no index, no instruction to go and read one
        *([f"- READ the OS knowledge base before you decompose — it is indexed at the "
           f"end of this prompt, not pasted into it: `jarvis learn search \"<term>\" "
           f"--project {project.name}` finds and ranks matching entries, then "
           f"`jarvis learn show <id>` returns the body. A plan built "
           f"without it will hand children the lessons the fleet already paid for, "
           f"again."] if knowledge else []),
        # Spec 2026-10-06-navigate-specs-like-code.md §6
        "- NAVIGATE the specs you read, never open one whole — you are the fleet's "
        "heaviest spec reader: `jarvis spec toc <path>` for the headings, "
        "`jarvis spec section <path> <n|name>` for the one section you need, "
        "`jarvis spec search \"<words>\"` to find which spec says a thing.",
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
    """One line per stored evidence ref, each naming the command that reads that shape (docs/superpowers/specs/2026-09-23-improvement-orders.md)"""
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
    """The briefing for an improvement order's analyst (docs/superpowers/specs/2026-09-23-improvement-orders.md)"""
    from . import db, findings, worker_brief
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
        "- `jarvis learn search \"<term>\"` — which of the fleet's entries match, by "
        "headline and id; `jarvis learn show <id>` then returns the body",
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
        # §5, docs/superpowers/specs/2026-10-14-an-unanswered-refusal-must-name-itself.md
        "Write it to a JSON file in your worktree and submit it with the command below, "
        "which IS your finish, and submit it ONCE. If the user refuses an assumption "
        "afterwards, do NOT resubmit the report: the OS refuses it, and it would replace "
        "the document and discard the decisions already recorded on it. Answer the "
        "refusal in this session, then run `jarvis wo finish --summary \"…\"`, which "
        "records the re-delivery and does not touch the report. `--from-file` and not an "
        "inline argument, "
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
        "# Write it short",
        # findings.MAX_FIELD_CHARS owns these numbers; the validator refuses a longer report
        f"The user decides every finding by reading it, so a finding is dense, not long. "
        f"Hard caps, enforced: `summary` {findings.MAX_SUMMARY_CHARS} characters, "
        f"`symptom` {findings.MAX_FIELD_CHARS['symptom']}, `root_cause` "
        f"{findings.MAX_FIELD_CHARS['root_cause']}, `why_insufficient` "
        f"{findings.MAX_FIELD_CHARS['why_insufficient']}, `recommendation` "
        f"{findings.MAX_FIELD_CHARS['recommendation']}; at most "
        f"{findings.MAX_EVIDENCE} quotes per finding, each at most "
        f"{findings.MAX_QUOTE_CHARS} characters.",
        "- Rich means specific: the mechanism, the file:line, the measured number. "
        "Not the story of how you found it, not what you ruled out, not the brief "
        "restated back.",
        "- One quote per claim, the single line that proves it. Long logs stay in "
        "their source; name the command that prints them.",
        "- Detail a worker needs goes in the proposed order's `description`, not in "
        "the finding.",
        "",
        "# Scope",
        # §4.3 of the improvement-orders spec owns this cap
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
        worker_brief.assume_shape(wo["id"],
                                  tail=" Record every one, including the small ones."),
        "- Work only inside your worktree (you start in it).",
        *([f"- READ the OS knowledge base before you diagnose — it is indexed at the end "
           f"of this prompt, not pasted into it: `jarvis learn search \"<term>\" "
           f"--project {project.name}` ranks the matching entries and gives you their "
           f"ids, `jarvis learn show <id>` returns a body."] if knowledge else []),
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


def _investigator_prompt(wo: dict[str, Any], project: ProjectSpec,
                         knowledge: KnowledgeBrief | None = None,
                         feature: dict[str, Any] | None = None) -> str:
    """The briefing for an investigation order's investigator (docs/superpowers/specs/2026-09-27-investigation-orders.md)"""
    from . import db, gaps, verdicts, worker_brief
    from .ops import SUBJECT_KEY  # lazy: ops imports dispatch

    inv_id = wo.get("parent_id") or "?"
    fo = (feature or {}).get("fo") or {}
    metadata = db.from_json(fo.get("metadata"), {}) or {}
    subject = str(metadata.get(SUBJECT_KEY) or "")
    read_subject = (f"jarvis fo show {subject}" if subject.startswith(("fo-", "io-"))
                    else f"jarvis wo show {subject}")
    parts = [
        f"You are the INVESTIGATOR for Jarvis investigation `{inv_id}` in project "
        f"`{project.name}`, running as work order `{wo['id']}`.",
        "",
        f"# The subject: {subject or '(named in the symptom below)'}",
        "",
        wo.get("description") or "(no further description — the title is the ask)",
        "",
        "# Your job: diagnose ONE subject and classify it",
        f"You READ. You change nothing — no file, no commit, no pull request, nothing "
        f"filed. That is not a request: two PreToolUse hooks refuse your writes and your "
        f"mutating shell commands, so a session that tries to fix what it found spends "
        f"its turns being denied. Your output is a VERDICT about "
        f"{subject or 'the subject'} and nothing else.",
        "",
        "# Read the evidence through the CLI, never the databases",
        "Every record you need has a verb. Do not open a SQLite file, and do not read "
        "state under `.jarvis/` by hand:",
        f"- `{read_subject}` — the subject itself: its timeline, messages and assumptions",
        "- `jarvis validation show <wo-id|fo-id>` — every panel round, seat verdict and "
        "the commit each judged",
        "- `jarvis inspect <wo-id|fo-id>` — where the time went, turn by turn",
        "- `jarvis cost <project|wo-id|fo-id>` — what it cost, split worker vs Jarvis",
        "- `jarvis alarms [project]` — turns the OS raised while they burned",
        "- `jarvis gate list --pending` / `jarvis gate show <id>` — a privileged action "
        "waiting on somebody",
        "- `jarvis neo list` / `jarvis neo show <qid>` — a question that may never have "
        "been delivered",
        "- `jarvis doctor [project]` — the OS's own post-conditions",
        "- `jarvis search \"<words>\"` / `jarvis issues [project]` — settled records, and "
        "what the fleet keeps hitting",
        "- `gh pr view <url>` / `gh pr diff <url>` — the PR, its checks and its diff",
        "- `git log|show|diff` — the branch, and what landed on it",
        "",
        "# Quote your evidence, verbatim",
        "A root cause asserted without a verbatim line — from a timeline, an `inspect` "
        "reading, a validation transcript, a diff — is an opinion, not a diagnosis. "
        "Paraphrase is not a quote. Copy the line as it is printed, name the command that "
        f"printed it, and keep it over {verdicts.MIN_QUOTE_CHARS} characters, which the "
        f"validator enforces — under that it is a fragment nobody can find again.",
        "",
        "# The four classifications",
        "A classification is a decision about THE SUBJECT, not about the codebase in "
        "general. Exactly one:",
        # kn-4fb740a9: a stuck subject is the common case, never the test
        "- **GAP** — the OS is missing a mechanism. A stuck subject is the common case, "
        "not the test: a DESIGNED WAIT the sweep investigated anyway is a GAP too, where "
        "the missing mechanism is an `excluded_cause` on the SWEEP — plus the discount of "
        "held seconds wherever the status is judged on time in status — and the stuck "
        "entity is the sweep, not the subject (`kn-4fb740a9`). Name the bug to file in "
        "`proposed_fix`. THE OS FILES IT as an issue, after its own duplicate search — "
        "which is why you do not file it and cannot.",
        "- **WAITING_ON_USER** — nothing is broken; a person owes a decision. Name it BY "
        "ID in `user_owes`: the assumption, the gate, the Neo question. This is the ONE "
        "classification that spends the user's attention.",
        "- **TRANSIENT** — it clears itself. Say in `unsticks` WHAT mechanism clears it "
        "and WHEN. \"It will sort itself out\" with no mechanism is not this "
        "classification.",
        "- **ALREADY_TRACKED** — the cause is already on the tracker or already has a live "
        "order. Put the issue (`#790` or its URL) or the order id in `duplicate_of`.",
        "",
        "# Name the gap class — required on all four",
        gaps.render_registry(),
        "",
        "",
        "# A GAP also owes a detector and a remedy",
        "`proposed_fix.detector`: the predicate over STATE that would have recognised "
        "this — a reconciler invariant or a `jarvis doctor` check, MECHANICAL, no model "
        "call. `proposed_fix.remedy`: which existing remedy id makes the next occurrence "
        "self-heal, or the word `none` with the reason it is unsafe to automate. Both are "
        "the fix order's acceptance criteria: a fix that turns the symptom green and "
        "leaves the OS blind is refused.",
        "",
        "# The verdict",
        f"Create it with the **`Write` tool** on `verdict.json` in your worktree root — "
        f"that filename exactly, it is the one path you are permitted to write, and "
        f"`Write` is the one tool that may. Not `cat >`, not a heredoc, not `python3 -`: a "
        f"shell heredoc that writes a file is refused for every worker in the fleet "
        f"(`hooks.heredoc_write_decision`). Submit it with the command below, "
        f"which IS your finish. Do not call `jarvis wo finish`: submitting the verdict "
        f"settles this work order for you. `--from-file` and not inline flags, because "
        f"the gate classifier's quote-blanking fails on nested and mixed quoting and a "
        f"verdict is full of repo paths and quoted log lines.",
        "",
        f"    jarvis investigate verdict {inv_id} --from-file verdict.json",
        "",
        "```json",
        "{",
        f'  "subject": "{subject or "wo-…"}",',
        '  "classification": "GAP",',
        '  "gap_class": "stale-hold",   // a registry slug, or a new well-formed one',
        '  "root_cause": "one paragraph, in mechanism terms",',
        '  "evidence": [',
        f'    {{"source": "{read_subject}", "quote": "the verbatim line"}}',
        "  ],",
        '  "proposed_fix": {          // GAP only, and all seven fields',
        '    "title": "what the bug report is titled",',
        '    "description": "the brief, standing alone",',
        '    "expected": "what should happen",',
        '    "actual": "what happens",',
        '    "priority": "high",',
        '    "detector": "the state predicate that recognises it, in mechanism terms",',
        '    "remedy": "an existing remedy id, or none with the reason"',
        "  },",
        '  "user_owes": "…",          // WAITING_ON_USER only, naming an id',
        '  "unsticks": {"what": "…", "when": "…"},   // TRANSIENT only',
        '  "duplicate_of": "#790"     // ALREADY_TRACKED only',
        "}",
        "```",
        "",
        "`subject`, `classification`, `gap_class`, `root_cause` and at least one "
        "`evidence` entry are "
        "required for all four. CARRY ONLY YOUR CLASSIFICATION'S FIELD: the validator "
        "refuses a GAP with no `proposed_fix`, and a TRANSIENT that also says the user "
        "owes something. It names every problem at once, so one revision fixes all of "
        "them. Comments are not JSON — the `//` notes above are for you, not for the file.",
        "",
        "# Bounded inputs",
        f"- Never paste a diff, a transcript or a log into the verdict: cite the command "
        f"and quote the decisive line. Over {verdicts.MAX_VERDICT_CHARS} characters the "
        f"whole document is refused.",
        "- One subject, one root cause, one classification. A symptom with two independent "
        "causes is an improvement order, and belongs in the verdict as that recommendation.",
        "",
        "# What you may run",
        "Reads: `git log|show|diff|status|blame`, `gh pr view|diff|checks`, "
        "`gh issue view|list`, `gh run view|list`, every `jarvis … show|list` verb, and "
        "`cat`/`grep`/`jq`. Serena's read-only tools are wired for code navigation and "
        "are faster than grep.",
        "Writes — EXACTLY four, and nothing else is permitted:",
        f"- `jarvis wo ask {wo['id']} \"<question>\"`",
        f"- `jarvis wo assume {wo['id']} \"<ONE decision you made with no doubt, citing "
        f"any spec by section in-text>\"`",
        f"- `jarvis learn add \"...\" --project {project.name}`",
        f"- `jarvis investigate verdict {inv_id} --from-file verdict.json`",
        "**`jarvis bug report` and `jarvis issues start` are refused for this kind**, and "
        "deliberately: the OS files a GAP itself after a duplicate search, and a filing "
        "route around that check is how one cause gets two issues. So a bug you hit in "
        "Jarvis OS ITSELF while investigating — even one unrelated to your subject — "
        "cannot be filed from here and does not use your `report-jarvis-bug` skill. Put it "
        "in the verdict: in `proposed_fix` if it IS the root cause, and in `root_cause`'s "
        "last sentence if it is not.",
        "",
        "# Operating contract",
        f"- **Neo is your first responder. Any doubt goes to it.** `jarvis wo ask "
        f"{wo['id']} \"<your question>\"`, then END YOUR TURN; the answer arrives as your "
        f"next user turn, usually within a minute. The trigger is DOUBT, not importance.",
        worker_brief.assume_shape(wo["id"],
                                  tail=" Record every one, including the small ones."),
        "- Work only inside your worktree (you start in it).",
        *([f"- READ the OS knowledge base before you diagnose — it is indexed at the end "
           f"of this prompt, not pasted into it: `jarvis learn search \"<term>\" "
           f"--project {project.name}` ranks the matching entries and gives you their "
           f"ids, `jarvis learn show <id>` returns a body."] if knowledge else []),
        f"- The OS knowledge base is the ONLY memory that survives you: "
        f"`jarvis learn add \"...\" --project {project.name} --topic \"<topic>\"`.",
        "",
        "# What the outside world sees",
        "The work order record IS this conversation, as far as anyone else is concerned. "
        "The last message of every turn you take is captured verbatim into it, and the "
        "user decides from that record — they will never open this session. End every "
        "turn with the complete answer: what you found, what you could not read, what you "
        "are unsure about, and absolute paths.",
        "",
        "Work autonomously toward a submitted verdict. User feedback may arrive as new "
        "user turns; treat it as authoritative.",
    ]
    return "\n".join(_common_briefing(parts, wo, project, knowledge))


def _manager_prompt(wo: dict[str, Any], project: ProjectSpec,
                    knowledge: KnowledgeBrief | None = None,
                    feature: dict[str, Any] | None = None) -> str:
    """The briefing for a feature order's project manager"""
    from . import worker_brief

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
        worker_brief.assume_shape(
            wo["id"], tail=" Record every one: this work order's record is the only "
                           "account anyone gets of why the feature changed shape."),
        "- Do not run `jarvis wo finish` and do not open a pull request. Neither applies "
        "to you.",
        f"- Read what the fleet already knows before you decide anything: `jarvis learn "
        f"search \"<term>\" --project {project.name}` for the matching headlines and ids, "
        f"then `jarvis learn show <id>` for a body."
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
    """The pre-approval marker, if this work order carries one"""
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
    """Tell the worker the decision it would otherwise ask about is already made"""
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
    """Tell the worker that shipping is reachable, and how"""
    from . import worker_brief

    return worker_brief.gates_section(
        wo["id"], enabled=tuple(project.gates.enabled)).splitlines()


def resolved_choices(project: ProjectSpec, wo: dict[str, Any]) -> dict[str, Any]:
    """The model and effort this work order runs on — PER KIND, then per project (docs/superpowers/specs/2026-09-27-investigation-orders.md)"""
    by_kind = {"investigator": (project.worker.investigation_model,
                                project.worker.investigation_effort)}
    model, effort = by_kind.get(wo.get("kind") or "worker",
                                (project.worker.model, project.worker.effort))
    return {"model": wo.get("model") or model,
            "effort": wo.get("effort") or effort}


def dispatch_work_order(
    store: ProjectStore,
    central: CentralStore,
    project: ProjectSpec,
    wo: dict[str, Any],
    os_config: OsConfig | None = None,
) -> dict[str, Any]:
    """Open the worker's conversation for a work order already in `dispatching` state"""
    from . import budget, context, worker_session

    cfg = os_config or OsConfig()
    knowledge = central.knowledge_brief(
        project.name,
        pinned_limit=cfg.knowledge_inject_limit,
        digest_limit=cfg.knowledge_digest_limit,
        digest_chars=cfg.knowledge_digest_chars,
        title=wo["title"],
        hint_limit=cfg.knowledge_hint_limit,
        hint_chars=cfg.knowledge_hint_chars,
        pinned_chars=cfg.knowledge_pinned_chars,
    )
    prompt = build_worker_prompt(wo, project, knowledge,
                                 design_doc=materialize_design_doc(store, project, wo),
                                 feature=feature_context(store, wo),
                                 dependencies=dependency_context(store, wo))

    # Resolved onto the row before the turn is launched
    resolved = {
        **resolved_choices(project, wo),
        "permission_mode": wo.get("permission_mode") or project.worker.permission_mode,
        # Which configuration this ran under
        "config_version": (central.head_config_version() or {}).get("id"),
    }
    store.update_work_order(wo["id"], **resolved)
    # A CHILD TAKES ITS SLICE OF THE FEATURE'S BUDGET HERE
    budget.reserve(store, central, wo)
    wo = store.get_work_order(wo["id"])

    try:
        # COMPACT FIRST IF THIS IS A SECOND DISPATCH INTO A COLD CONVERSATION (docs/superpowers/specs/2026-09-29-one-compaction-decision-on-every-relaunch.md)
        queued: dict[str, Any] = {}
        compacted = worker_session.compact_before_relaunch(
            store, project, wo, cfg.compact_min_context, queue=prompt,
            queued_out=queued)
        if compacted is None:
            turn, briefing = worker_session.start(store, project, wo, prompt)
    except budget.BudgetExhausted as e:
        # Spent before it ever ran a turn
        budget.escalate(store, wo, e.exhausted)
        return store.get_work_order(wo["id"])
    except claude_cli.ClaudeCliError as e:
        # A TURN THAT COULD NOT BE LAUNCHED IS NOT A WORK ORDER THAT FAILED (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §5.)
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
                paired=True,
            )
        raise

    if compacted is not None:
        # NO WORKER PROMPT HAS BEEN SENT: it is the queued message
        store.clear_dispatch_attempts(wo["id"])
        store.set_status(wo["id"], "running")
        store.add_event(wo["id"], "dispatch_deferred_for_compaction", {
            "turn": compacted["seq"], "msg_id": queued.get("msg_id"), **resolved})
        central.touch_project(project.name)
        return store.get_work_order(wo["id"])

    # THE CONTEXT LEDGER for the seq-1 dispatch turn
    if turn["seq"] == 1:
        # `worktree` is written by `start` above, AFTER the row this `wo` was read from
        context.record(store, project, {**wo, "worktree": wo["id"]}, turn, briefing,
                       knowledge=knowledge)
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
