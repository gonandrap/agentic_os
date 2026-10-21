"""A dispatched worker's first Serena call must not fail with `No active project` (docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md §5.)"""

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
    """The path has to be IN the text"""
    tree = worktree(project, wo["id"])
    payload = session_start(tree)

    text = context(project, wo["id"], cwd=tree)

    assert payload["cwd"] in text
    assert ACTIVATE in text
    # Every mention of the project root is part of the worktree path
    assert text.replace(str(tree), "") .count(str(project)) == 0


def test_no_activate_context_outside_a_worker_session(project):
    """A session the user opened themselves is theirs"""
    assert handle_hook(session_start(project), {}) is None


def test_the_activate_call_is_injected_with_no_serena_project_file_present(wo, project):
    """Neo's ruling on question 1259: `activate_project` WRITES `.serena/project.yml` itself"""
    tree = worktree(project, wo["id"])
    assert not (tree / ".serena" / "project.yml").exists()

    assert ACTIVATE in context(project, wo["id"], cwd=tree)


def test_no_activate_context_when_the_project_deselected_serena(wo, project):
    """`JARVIS_SERENA=0` means the server was removed from this worker's settings file"""
    text = context(project, wo["id"], JARVIS_SERENA="0")

    assert "activate_project" not in text
    assert concision.HOUSE_STYLE_BEGIN in text


def test_the_house_style_still_rides_the_same_session_start_context(wo, project):
    """The existing injection is unchanged and comes FIRST: it is the cached shape"""
    text = context(project, wo["id"])

    assert text.startswith(concision.house_style())
    assert "nobody asked for" in text
    assert ACTIVATE in text


def test_the_short_prefix_spelling_is_offered_as_the_alternative():
    """Both spellings for one server, mirroring why `dispatch.SERENA_TOOL_PREFIXES` has two entries: a..."""
    text = serena_activation_context(Path("/tmp/wt"))

    assert ACTIVATE in text
    assert "mcp__serena__activate_project" in text
    assert "/tmp/wt" in text
    assert "session_id" in text
    assert len(text.splitlines()) <= 8, "it rides every turn"
