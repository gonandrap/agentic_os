"""A dispatched worker's first Serena call must not fail with `No active project`.

Spec docs/specs/2026-10-02-serena-the-cheap-path.md §5. The Claude Code plugin starts the
Serena MCP server with fixed args carrying neither `--project` nor `--project-from-cwd`,
and no Serena env var selects a project — so the first `find_symbol` of a dispatched turn
fails and the worker falls back to grep. Jarvis therefore injects the activation call at
`SessionStart`, as `additionalContext`, with the worktree path already filled in.

Two measured facts these tests rely on and do not re-probe:
  · `activate_project` accepts an ARBITRARY `session_id` string (`00000000` worked), so
    the injected call needs no prior `initial_instructions` round-trip.
  · activating by ABSOLUTE PATH works even though 108 registry entries share the name
    `jarvis-os`, and it creates the registry entry itself when absent.

`SessionStart` is driven the way tests/test_prefix_drift_hook.py drives it — the same
`handle_hook` payload against the same project fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import concision, ops
from jarvis.hooks import handle_hook, serena_activation_context

ACTIVATE = "mcp__plugin_serena_serena__activate_project"


@pytest.fixture()
def wo(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return ops.create_work_order("proj_a", "make the thing work", origin="jarvis",
                                 description="The user's original ask, verbatim.")


def worktree(project: Path, wo_id: str) -> Path:
    path = project / ".claude" / "worktrees" / wo_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def env(project: Path, wo_id: str, **extra: str) -> dict[str, str]:
    return {
        "JARVIS_WO_ID": wo_id,
        "JARVIS_PROJECT": "proj_a",
        "JARVIS_PROJECT_PATH": str(project),
        **extra,
    }


def session_start(cwd: Path, source: str = "resume") -> dict[str, object]:
    return {"hook_event_name": "SessionStart", "session_id": "sess-1",
            "source": source, "cwd": str(cwd)}


def context(project: Path, wo_id: str, cwd: Path | None = None, **extra: str) -> str:
    decision = handle_hook(session_start(cwd or worktree(project, wo_id)),
                           env(project, wo_id, **extra))
    assert decision is not None
    return decision["hookSpecificOutput"]["additionalContext"]


def test_session_start_injects_the_activate_call_with_the_worktree_path(wo, project):
    """The path has to be IN the text. A worker told to activate "this project" has to
    guess, and the main checkout is the wrong guess: its CLAUDE.md and its index are not
    the branch the work order is on."""
    tree = worktree(project, wo["id"])
    payload = session_start(tree)

    text = context(project, wo["id"], cwd=tree)

    assert payload["cwd"] in text
    assert ACTIVATE in text
    # Every mention of the project root is part of the worktree path, never the root
    # alone: the main checkout is a different branch and a different index.
    assert text.replace(str(tree), "") .count(str(project)) == 0


def test_no_activate_context_outside_a_worker_session(project):
    """A session the user opened themselves is theirs — the `JARVIS_WO_ID` /
    `find_by_session` guard every other hook branch uses."""
    assert handle_hook(session_start(project), {}) is None


def test_the_activate_call_is_injected_with_no_serena_project_file_present(wo, project):
    """Neo's ruling on question 1259: `activate_project` WRITES `.serena/project.yml`
    itself, so gating on that file existing would skip the fresh worktree this whole
    order exists for."""
    tree = worktree(project, wo["id"])
    assert not (tree / ".serena" / "project.yml").exists()

    assert ACTIVATE in context(project, wo["id"], cwd=tree)


def test_no_activate_context_when_the_project_deselected_serena(wo, project):
    """`JARVIS_SERENA=0` means the server was removed from this worker's settings file.
    Telling it to activate a project on a server it does not have is the incoherence
    `wiring.serena_wired` exists to prevent — and the house style still rides."""
    text = context(project, wo["id"], JARVIS_SERENA="0")

    assert "activate_project" not in text
    assert concision.HOUSE_STYLE_BEGIN in text


def test_the_house_style_still_rides_the_same_session_start_context(wo, project):
    """The existing injection is unchanged and comes FIRST: it is the cached shape, and
    the activation block is appended after it rather than in front of it."""
    text = context(project, wo["id"])

    assert text.startswith(concision.house_style())
    assert "Say each thing ONCE" in text
    assert ACTIVATE in text


def test_the_short_prefix_spelling_is_offered_as_the_alternative():
    """Both spellings for one server, mirroring why `dispatch.SERENA_TOOL_PREFIXES` has
    two entries: a plugin install produces the long prefix, `claude mcp add serena` the
    short one, and Jarvis configures no MCP server itself so it cannot know which."""
    text = serena_activation_context(Path("/tmp/wt"))

    assert ACTIVATE in text
    assert "mcp__serena__activate_project" in text
    assert "/tmp/wt" in text
    assert "session_id" in text
    assert len(text.splitlines()) <= 8, "it rides every turn"
