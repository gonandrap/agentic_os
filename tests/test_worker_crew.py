"""The crew an ordinary worker must delegate to.

docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md SS5: two subagent
definitions ship to every `kind == "worker"` dispatch, in a THIRD assets root of their
own — not the planner's `agent-seats`, not the shared `agent-skills` — because each
dispatch rebuilds its whole destination tree and a shared root means one population's
rebuild lands while the other's concurrently-dispatched turn is reading it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import bootstrap
from jarvis.catalog import ProjectSpec, WiringConfig

CREW = ("jarvis-spec-writer", "jarvis-implementer")


def _crew_root(roots: list[Path]) -> Path:
    return next(r for r in roots if r.name == "agent-crew")


def test_worker_kind_installs_crew_root(project, jarvis_home):
    roots = bootstrap.install_agent_assets(project, kind="worker")
    root = _crew_root(roots)
    names = {p.stem for p in (root / ".claude" / "agents").glob("*.md")}
    assert names == set(CREW)


def test_crew_root_is_separate_from_planner_seats(project, jarvis_home):
    worker = _crew_root(bootstrap.install_agent_assets(project, kind="worker"))
    planner = bootstrap.install_agent_assets(project, kind="planner")[-1]
    assert worker != planner
    assert "agent-skills" not in str(worker)
    # the planner's own rebuild must not touch the crew
    assert (worker / ".claude" / "agents" / "jarvis-implementer.md").exists()


def test_planner_kind_does_not_get_crew(project, jarvis_home):
    roots = bootstrap.install_agent_assets(project, kind="planner")
    assert not [r for r in roots if r.name == "agent-crew"]
    seats = {p.stem for p in (roots[-1] / ".claude" / "agents").glob("*.md")}
    assert "jarvis-implementer" not in seats


def test_briefing_add_dirs_include_crew_for_worker(project, jarvis_home):
    from jarvis.worker_session import briefing_for
    spec = ProjectSpec(name="proj_a", path=project)
    dirs = briefing_for(spec, {"id": "wo-crew01", "title": "t", "kind": "worker"})
    assert any(Path(d).name == "agent-crew" for d in dirs["add_dirs"])
    planner = briefing_for(spec, {"id": "wo-crew02", "title": "t", "kind": "planner"})
    assert not any(Path(d).name == "agent-crew" for d in planner["add_dirs"])


@pytest.mark.parametrize("seat,expected", [
    ("jarvis-spec-writer", ("Read", "Grep", "Glob", "Write")),
    ("jarvis-implementer", ("Read", "Edit", "Write", "Bash", "Glob", "Grep")),
])
def test_crew_definitions_parse_with_expected_tools(project, jarvis_home, seat, expected):
    root = _crew_root(bootstrap.install_agent_assets(project, kind="worker"))
    text = (root / ".claude" / "agents" / f"{seat}.md").read_text()
    assert text.startswith("---\n")
    front = text.split("---\n")[1]
    tools = [t.strip() for t in
             next(line for line in front.splitlines()
                  if line.startswith("tools:")).removeprefix("tools:").split(",")]
    assert f"name: {seat}" in front
    for tool in expected:
        assert tool in tools, f"{seat} is missing {tool}"
    # Serena read tools copied verbatim from the architect seat, so the two cannot drift
    architect = (bootstrap.ASSETS / "agents" / "jarvis-architect.md").read_text()
    serena = [t for t in architect.split("tools:")[1].split("\n")[0].split(",")
              if "serena" in t]
    for t in serena:
        assert t.strip() in tools


def test_spec_writer_has_no_bash_or_edit(project, jarvis_home):
    root = _crew_root(bootstrap.install_agent_assets(project, kind="worker"))
    front = (root / ".claude" / "agents" / "jarvis-spec-writer.md").read_text()
    tools = next(line for line in front.splitlines() if line.startswith("tools:"))
    parts = [t.strip() for t in tools.removeprefix("tools:").split(",")]
    assert "Bash" not in parts and "Edit" not in parts


def test_serena_stripped_when_unwired(project, jarvis_home):
    roots = bootstrap.install_agent_assets(project, kind="worker", serena=False)
    for seat in (_crew_root(roots) / ".claude" / "agents").glob("*.md"):
        text = seat.read_text()
        assert "mcp__serena__" not in text
        assert "mcp__plugin_serena_serena__" not in text
        assert "not wired for this project" in text


def test_implementer_states_tdd_no_background_and_no_lead_commands(project, jarvis_home):
    root = _crew_root(bootstrap.install_agent_assets(project, kind="worker"))
    text = (root / ".claude" / "agents" / "jarvis-implementer.md").read_text()
    assert "superpowers:test-driven-development" in text
    assert "run_in_background" in text
    assert "jarvis wo finish" in text and "gh pr create" in text


def test_spec_writer_states_the_problem_and_fix_contract(project, jarvis_home):
    root = _crew_root(bootstrap.install_agent_assets(project, kind="worker"))
    text = (root / ".claude" / "agents" / "jarvis-spec-writer.md").read_text()
    assert "root cause" in text.lower()
    assert "refused" in text.lower()      # the hook is named as the check, not built here


def test_unwired_serena_does_not_touch_the_wired_copy(project, jarvis_home):
    """`_rebuild` owns the whole destination, so the strip can never leak into the
    next dispatch."""
    bootstrap.install_agent_assets(project, kind="worker", serena=False)
    roots = bootstrap.install_agent_assets(project, kind="worker", serena=True)
    text = (_crew_root(roots) / ".claude" / "agents" / "jarvis-implementer.md").read_text()
    assert "not wired for this project" not in text
    assert "mcp__plugin_serena_serena__find_symbol" in text


def test_wiring_config_deselection_reaches_the_crew(project, jarvis_home):
    from jarvis import wiring
    spec = ProjectSpec(name="proj_a", path=project,
                       wiring=WiringConfig(disabled_plugins=("serena@claude-plugins-official",)))
    assert wiring.serena_wired(spec.wiring) is False
