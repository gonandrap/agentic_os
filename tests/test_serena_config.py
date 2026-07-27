"""Guards on the committed Serena configuration.

`.serena/project.yml` and `.serena/memories/` are committed on purpose: they ride the
release tags into production, so a production session troubleshooting an incident gets
the code map without re-exploring the tree.

Assertions are text-based rather than YAML-parsed so the suite needs no new dependency
(pyyaml is not in the dev extra).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SERENA_DIR = REPO_ROOT / ".serena"
PROJECT_YML = SERENA_DIR / "project.yml"
MEMORY_DIR = SERENA_DIR / "memories"

MEMORIES = ("codebase-map", "work-order-lifecycle", "dev-vs-prod-environments", "testing")


@pytest.fixture(scope="module")
def project_yml() -> str:
    return PROJECT_YML.read_text()


def test_serena_project_is_committed() -> None:
    assert PROJECT_YML.is_file(), (
        "the Serena project config must be committed so it ships with the release tag "
        "and production sessions can navigate the code"
    )


def test_worktrees_are_excluded_from_the_symbol_index(project_yml: str) -> None:
    """`.claude/worktrees/` holds full copies of this repo.

    They are untracked but *not* gitignored, so `ignore_all_files_in_gitignore` does not
    filter them. Without an explicit exclusion the index carries one copy of every symbol
    per live worktree, and `find_symbol` returns N duplicate hits for one definition.
    """
    assert ".claude/worktrees/**" in project_yml, (
        "ignored_paths must exclude .claude/worktrees/** — see the comment in project.yml"
    )


def test_activation_prompt_routes_sessions_to_the_memories(project_yml: str) -> None:
    """The initial_prompt fires on every activation, including in production.

    It is the one lever that does not depend on the session having read CLAUDE.md.
    """
    _, sep, tail = project_yml.partition("initial_prompt:")
    assert sep, "initial_prompt key went missing from project.yml"
    body = tail.partition("\n#")[0]
    assert 'initial_prompt: ""' not in project_yml, "initial_prompt was reset to empty"
    assert "list_memories" in body, "the activation prompt must point at the memories"
    assert "find_symbol" in body, "the activation prompt must route symbol lookups to Serena"


@pytest.mark.parametrize("name", MEMORIES)
def test_memory_is_committed_and_non_empty(name: str) -> None:
    path = MEMORY_DIR / f"{name}.md"
    assert path.is_file(), f"missing committed memory: {path}"
    assert path.read_text().strip(), f"{path} is empty — it answers nothing"


def test_memories_named_in_the_activation_prompt_exist(project_yml: str) -> None:
    """A pointer to a renamed memory rots silently and sessions go back to exploring."""
    for name in MEMORIES:
        if name in project_yml:
            assert (MEMORY_DIR / f"{name}.md").is_file(), (
                f"project.yml points at the {name!r} memory but the file is gone"
            )
