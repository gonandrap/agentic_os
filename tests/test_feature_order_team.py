"""Phase 3 of feature orders: the planning team, and the postures that make it safe"""

from __future__ import annotations

import json

import pytest

from jarvis import gates, ops
from jarvis.bootstrap import ASSETS, install_agent_assets
from jarvis.hooks import handle_hook
from jarvis.project_store import ProjectStore

SEATS = ("jarvis-architect", "jarvis-test-lead")

# Every tool that can put bytes on disk, directly or through a shell
EDITING_TOOLS = ("Write", "Edit", "NotebookEdit", "Bash")

# Serena's own write and exec tools, unprefixed
SERENA_WRITE_TOOLS = (
    "create_text_file", "replace_symbol_body", "replace_content", "replace_in_files",
    "insert_after_symbol", "insert_before_symbol", "rename_symbol", "safe_delete_symbol",
    "execute_shell_command", "write_memory", "delete_memory", "edit_memory",
    "rename_memory", "onboarding",
)

# The read-only Serena tools a seat needs to navigate code by symbol rather than by text
SERENA_READ_TOOLS = (
    "activate_project", "get_symbols_overview", "find_symbol",
    "find_referencing_symbols", "search_for_pattern", "list_memories", "read_memory",
)

# Both naming schemes are listed in every seat
SERENA_PREFIXES = ("mcp__serena__", "mcp__plugin_serena_serena__")


def granted_tools(seat: str) -> set[str]:
    return {t.strip() for t in
            frontmatter((ASSETS / "agents" / f"{seat}.md").read_text())["tools"].split(",")}


def frontmatter(text: str) -> dict[str, str]:
    """The `---` block at the top of an agent definition, as key -> value"""
    assert text.startswith("---\n"), "an agent definition must open with frontmatter"
    block = text.split("---\n", 2)[1]
    out = {}
    for line in block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


# -- layer 1: the postures


@pytest.mark.parametrize("seat", SEATS)
def test_a_seat_declares_the_tools_it_may_use(seat):
    fm = frontmatter((ASSETS / "agents" / f"{seat}.md").read_text())

    assert fm["name"] == seat, "the CLI resolves a subagent type by its `name` key"
    assert fm["description"], "the lead picks a seat by its description; it cannot be blank"
    assert "tools" in fm, (
        "without a `tools:` key the seat inherits the lead's full capabilities, which is "
        "the exact posture this phase exists to remove"
    )


@pytest.mark.parametrize("seat", SEATS)
def test_a_seat_cannot_produce_code(seat):
    """The load-bearing assertion of the whole phase"""
    granted = granted_tools(seat)

    assert granted, "an empty tools list would make the seat useless, not safe"
    assert not granted & set(EDITING_TOOLS), (
        f"{seat} may use {granted & set(EDITING_TOOLS)}, so it can put code on disk"
    )


@pytest.mark.parametrize("seat", SEATS)
def test_a_seat_can_still_read_the_codebase(seat):
    """Sighted by design: an architect that cannot open a file cannot decompose anything"""
    granted = granted_tools(seat)

    assert "Read" in granted and "Grep" in granted


# -- navigating by symbol, not by text


@pytest.mark.parametrize("seat", SEATS)
def test_a_seat_is_granted_serenas_symbol_tools(seat):
    """`tools:` is an ALLOWLIST, so a seat that names only Read/Grep/Glob cannot reach Serena at all"""
    granted = granted_tools(seat)

    for prefix in SERENA_PREFIXES:
        missing = [t for t in SERENA_READ_TOOLS if f"{prefix}{t}" not in granted]
        assert not missing, f"{seat} is missing {missing} under {prefix}"


@pytest.mark.parametrize("seat", SEATS)
def test_serena_does_not_smuggle_write_access_back_in(seat):
    """The control for the grant above"""
    granted = granted_tools(seat)

    smuggled = [t for t in granted
                if any(t == f"{p}{w}" for p in SERENA_PREFIXES for w in SERENA_WRITE_TOOLS)]
    assert not smuggled, f"{seat} can write through Serena: {smuggled}"


@pytest.mark.parametrize("seat", SEATS)
def test_a_seat_is_told_to_reach_for_serena_before_grep(seat):
    """The capability is necessary and not sufficient: a seat holding both `Grep` and `find_symbol` will..."""
    text = (ASSETS / "agents" / f"{seat}.md").read_text()
    body = text.split("---\n", 2)[2].lower()

    assert "serena" in body, "the seat is never told the symbol tools exist"
    assert "do not grep for code" in body, "the ranking is never stated outright"
    assert "find_referencing_symbols" in body, (
        "the one question grep cannot answer is the one most worth naming"
    )
    # The fallback, so a seat in a project with no index is not stuck refusing to look
    assert "fallback" in body and "no serena" in body


def test_the_worker_briefing_ranks_serena_over_grep(project):
    """Not just the seats"""
    from jarvis.catalog import ProjectSpec
    from jarvis.dispatch import build_worker_prompt

    spec = ProjectSpec(name="proj_a", path=project, description="")
    wo = {"id": "wo-1", "title": "t", "description": "d", "kind": "worker"}

    prompt = build_worker_prompt(wo, spec, [])

    # The worker core carries the posture as a hook
    assert "Serena first, grep second" in prompt
    assert "`navigation`" in prompt

    from jarvis import worker_brief
    section = worker_brief.render_section("navigation")
    assert "find_referencing_symbols" in section
    # Conditionality lives in the flag
    assert "Serena is wired to this project" in section
    assert "DEFERRED" in section
    no_serena = worker_brief.render_section("navigation", serena=False)
    assert "does not wire Serena" in no_serena
    assert "find_referencing_symbols" not in no_serena


def test_the_planner_briefing_ranks_serena_too(project):
    """The planner reads more code than anyone — it is deciding what the pieces ARE"""
    from jarvis.catalog import ProjectSpec
    from jarvis.dispatch import build_worker_prompt

    spec = ProjectSpec(name="proj_a", path=project, description="")
    wo = {"id": "wo-1", "title": "t", "description": "d", "kind": "planner",
          "parent_id": "fo-1"}

    prompt = build_worker_prompt(wo, spec, [])

    assert "Serena first, grep second" in prompt


def test_the_operation_contract_ranks_serena_too(project):
    """OPERATION.md is what a session reads when it goes looking for the contract"""
    from jarvis.bootstrap import ASSETS as A

    text = (A / "OPERATION.md.tmpl").read_text()

    assert "Serena first, grep second" in text
    assert "find_referencing_symbols" in text
    assert "no grep equivalent" in text


# -- delivery: who gets the seats


def test_the_seats_are_laid_out_where_add_dir_finds_them(project):
    """`--add-dir X` loads subagent definitions from `X/.claude/agents/`"""
    roots = install_agent_assets(project, "planner")

    seats = [r for r in roots if (r / ".claude" / "agents").is_dir()]
    assert len(seats) == 1
    for seat in SEATS:
        assert (seats[0] / ".claude" / "agents" / f"{seat}.md").is_file()


def test_a_planner_is_handed_exactly_these_seats_and_nothing_else(project):
    """EXACTLY, not "at least" — the assertion above says these two are present"""
    roots = install_agent_assets(project, "planner")
    seats = next(r for r in roots if (r / ".claude" / "agents").is_dir())

    delivered = {p.relative_to(seats / ".claude" / "agents").as_posix()
                 for p in (seats / ".claude" / "agents").rglob("*") if p.is_file()}
    assert delivered == {f"{s}.md" for s in SEATS}


def test_an_ordinary_worker_is_handed_no_planning_seats(project):
    """Design decision 4 still holds for the PLANNING seats: a worker is not a planning team"""
    roots = install_agent_assets(project, "worker")

    names = {p.name for r in roots for p in (r / ".claude" / "agents").glob("*.md")}
    assert "jarvis-architect.md" not in names and "jarvis-test-lead.md" not in names
    assert names == {"jarvis-spec-writer.md", "jarvis-implementer.md"}
    # ...and it still gets the skills, which are for everybody
    assert any((r / ".claude" / "skills").is_dir() for r in roots)


def test_the_seats_live_in_their_own_root(project):
    """Two roots, not one"""
    worker_roots = install_agent_assets(project, "worker")
    planner_roots = install_agent_assets(project, "planner")

    # Rebuilding for a worker did not disturb the seats a planner was given
    assert all((r / ".claude" / "agents").is_dir()
               for r in planner_roots if "seats" in r.name)
    install_agent_assets(project, "worker")
    assert all((r / ".claude" / "agents").is_dir()
               for r in planner_roots if "seats" in r.name)
    assert worker_roots[0] == planner_roots[0], "the skills root is shared, and unchanged"


def test_the_seats_are_generated_not_authored(project):
    """They live under the gitignored state dir and heal themselves, like the skills"""
    roots = install_agent_assets(project, "planner")
    seats = next(r for r in roots if (r / ".claude" / "agents").is_dir())
    assert ".jarvis" in seats.relative_to(project).parts

    defn = seats / ".claude" / "agents" / "jarvis-architect.md"
    defn.write_text("locally mangled")
    install_agent_assets(project, "planner")
    assert defn.read_text() != "locally mangled"


def test_a_planner_turn_is_launched_with_the_seat_directory(project, fake_claude,
                                                            catalog_file, jarvis_home):
    """The whole delivery path, end to end: a planner's briefing flags reach the CLI with the seats'..."""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    ops.create_feature_order("proj_a", "CSV export", description=(
        "Add a CSV exporter to the reporting module, with a command that calls it and "
        "tests over the happy path and an empty result set."))
    daemon.tick()

    call = fake_claude.wait_calls(lambda c: "--session-id" in c["argv"], count=1)[0]
    dirs = [call["argv"][i + 1] for i, a in enumerate(call["argv"]) if a == "--add-dir"]
    assert any(d.endswith("agent-seats") for d in dirs), dirs
    assert any(d.endswith("agent-skills") for d in dirs), dirs


def test_the_planner_briefing_names_its_seats(project, fake_claude, catalog_file,
                                              jarvis_home):
    """Two definitions nothing ever invokes are two files on disk"""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    ops.create_feature_order("proj_a", "CSV export", description=(
        "Add a CSV exporter to the reporting module, with a command that calls it and "
        "tests over the happy path and an empty result set."))
    daemon.tick()

    prompt = fake_claude.wait_calls(
        lambda c: "--session-id" in c["argv"], count=1)[0]["argv"][-1]
    for seat in SEATS:
        assert seat in prompt, f"the planner is never told {seat} exists"
    # And that they cannot run anything for it
    assert "anything that needs a shell is yours to run" in prompt


# -- layer 2: whose attempt was it


def gate_payload(command: str, agent_type: str | None = None) -> dict:
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": command}}
    # Absence is the discriminator, not a sentinel: the lead's own calls carry no `agent_type` key at all
    if agent_type is not None:
        payload["agent_type"] = agent_type
    return payload


@pytest.fixture()
def gated(jarvis_home, fake_claude, tmp_path, project):
    """A dispatched work order in a project that gates every privileged action"""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    catalog = tmp_path / "catalog-gated.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet"},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project",
                      "gates": {"enabled": list(gates.KIND_NAMES)}}],
    }))
    ops.start_os(str(catalog), foreground=True)
    daemon = Daemon(load_catalog(catalog))
    wo = ops.create_work_order("proj_a", "ship it", description="merge the PR")
    daemon.tick()

    settings = json.loads(
        (project / ".jarvis" / "worker-settings" / f"{wo['id']}.json").read_text())
    store = ProjectStore(project)
    yield store, wo, settings["env"]
    store.close()


def test_a_gate_the_lead_trips_records_no_seat(gated):
    store, wo, env = gated

    handle_hook(gate_payload("gh pr merge 12 --squash"), env)

    approval = store.list_approvals(wo["id"])[0]
    assert approval["agent_type"] is None


def test_a_gate_a_seat_trips_says_which_seat(gated):
    """`JARVIS_WO_ID` is per-session, so the request is filed against the work order whichever agent..."""
    store, wo, env = gated

    handle_hook(gate_payload("gh pr merge 12 --squash", "jarvis-architect"), env)

    approval = store.list_approvals(wo["id"])[0]
    assert approval["wo_id"] == wo["id"]
    assert approval["agent_type"] == "jarvis-architect"


def test_the_reviewer_is_told_a_seat_attempted_it(gated):
    """Whoever decides sees only the question text, never the session"""
    store, wo, env = gated

    handle_hook(gate_payload("gh pr merge 12 --squash", "jarvis-architect"), env)
    # The hook only records it; the lead makes the case, which is what writes the reviewer's question
    ops.request_gate_approval(wo["id"], "gh pr merge 12 --squash", why="CI is green")

    approval = store.list_approvals(wo["id"])[0]
    question = ops.show_gate(approval["id"])["neo_question"]
    assert "jarvis-architect" in question["question"]
    assert "seat" in question["question"]


def test_the_timeline_says_which_seat_asked(gated):
    """A timeline event whose renderer reads a key nobody writes is blank forever"""
    from jarvis import timeline

    store, wo, env = gated
    handle_hook(gate_payload("gh pr merge 12 --squash", "jarvis-architect"), env)

    rows = timeline.build_timeline(wo, store.list_events(wo["id"]), [])
    asked = [r for r in rows if r["kind"] == "gate_requested"]
    assert asked and "jarvis-architect" in asked[0]["label"], asked


def test_the_lead_still_gets_an_unqualified_line(gated):
    """The overwhelming majority of gates are tripped by a plain worker with no team at all"""
    from jarvis import timeline

    store, wo, env = gated
    handle_hook(gate_payload("gh pr merge 12 --squash"), env)
    ops.request_gate_approval(wo["id"], "gh pr merge 12 --squash", why="CI is green")

    rows = timeline.build_timeline(wo, store.list_events(wo["id"]), [])
    asked = [r for r in rows if r["kind"] == "gate_requested"]
    assert asked and "seat" not in asked[0]["label"], asked
    approval = store.list_approvals(wo["id"])[0]
    question = ops.show_gate(approval["id"])["neo_question"]
    assert question["question"].startswith("PRIVILEGED ACTION REQUEST")
    assert "The worker for work order" in question["question"]


def test_gate_request_from_the_cli_never_carries_a_seat(gated):
    """`jarvis gate request` is a shell command, so whoever ran it had a shell — and the seats have none"""
    store, wo, env = gated

    ops.request_gate_approval(wo["id"], "gh pr merge 12 --squash",
                              why="tests pass", evidence="PR 12")

    assert store.list_approvals(wo["id"])[0]["agent_type"] is None


def test_the_column_is_additive_for_a_database_that_predates_it(project, jarvis_home):
    """`approvals` is migrated through ADDED_COLUMNS"""
    store = ProjectStore(project)
    try:
        cols = {r["name"] for r in
                store.conn.execute("PRAGMA table_info(approvals)").fetchall()}
        assert "agent_type" in cols
    finally:
        store.close()


def test_json_of_a_gate_shows_the_seat(gated):
    """`jarvis gate show --json` is what an operator reads"""
    store, wo, env = gated
    handle_hook(gate_payload("gh pr merge 12 --squash", "jarvis-test-lead"), env)

    data = ops.show_gate(store.list_approvals(wo["id"])[0]["id"])
    assert data["agent_type"] == "jarvis-test-lead"
    assert json.dumps(data, default=str)  # and it survives the CLI's serialisation


def test_a_worker_may_actually_run_serenas_read_tools(project, jarvis_home):
    """Availability and permission are two different gates, and the second one is the one that bit"""
    import json as _json

    from jarvis.catalog import ProjectSpec
    from jarvis.dispatch import (
        SERENA_READ_TOOLS,
        SERENA_TOOL_PREFIXES,
        _write_worker_settings,
    )

    spec = ProjectSpec(name="proj_a", path=project, description="")
    out = _write_worker_settings(spec, {"id": "wo-nav", "title": "t"})
    allow = _json.loads(out.read_text())["permissions"]["allow"]

    for prefix in SERENA_TOOL_PREFIXES:
        for tool in SERENA_READ_TOOLS:
            assert f"{prefix}{tool}" in allow, f"{prefix}{tool} is visible but unrunnable"


def test_dispatch_never_allows_serenas_write_tools(project, jarvis_home):
    """The control"""
    import json as _json

    from jarvis.catalog import ProjectSpec
    from jarvis.dispatch import _write_worker_settings

    spec = ProjectSpec(name="proj_a", path=project, description="")
    out = _write_worker_settings(spec, {"id": "wo-nav", "title": "t"})
    allow = _json.loads(out.read_text())["permissions"]["allow"]

    smuggled = [r for r in allow
                if any(r.endswith(w) for w in SERENA_WRITE_TOOLS)]
    assert not smuggled, f"dispatch allows Serena write/exec tools: {smuggled}"
