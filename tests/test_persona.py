"""Structural guards on CLAUDE.md — the Jarvis persona file.

These are cheap, deterministic checks. The LLM-graded behavioural evals live in
evals/llm/test_jarvis_judgment.py and are opt-in (they cost real model calls).

The invariant that matters: the eval loads CLAUDE.md as a *bare system prompt* with no
cwd, no git, no repo. So the operator persona must come first and dominate; the dev-mode
override must stay scoped and below it. Invert that ordering and the routing scenarios
regress in a way only a paid eval run would catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PERSONA_PATH = REPO_ROOT / "CLAUDE.md"
MEMORY_DIR = REPO_ROOT / ".serena" / "memories"

DEV_MODE_HEADING = "## Development mode"


@pytest.fixture(scope="module")
def persona() -> str:
    return PERSONA_PATH.read_text()


def test_persona_file_exists() -> None:
    assert PERSONA_PATH.is_file(), f"the evals load {PERSONA_PATH} as their system prompt"


@pytest.mark.parametrize(
    "directive",
    [
        "Route, don't do",
        "jarvis wo create",
        "The CLI is the OS",
        "Reviews are sacred",
    ],
)
def test_operator_directives_precede_dev_mode(persona: str, directive: str) -> None:
    """Operator content must be readable before any dev-mode override is reached."""
    assert directive in persona, f"operator directive went missing: {directive!r}"
    dev_at = persona.index(DEV_MODE_HEADING)
    assert persona.index(directive) < dev_at, (
        f"{directive!r} must appear before {DEV_MODE_HEADING!r}; the persona evals read "
        "this file with no environment context and would fall through to dev behaviour"
    )


def test_dev_mode_override_is_present_and_scoped(persona: str) -> None:
    assert DEV_MODE_HEADING in persona
    head, _, tail = persona.partition(DEV_MODE_HEADING)
    assert len(head) > len(tail), (
        "the dev-mode override has outgrown the operator persona; keep it a scoped "
        "override, not a top-level fork"
    )


def test_dev_mode_documents_how_to_detect_the_checkout(persona: str) -> None:
    """A mode switch nobody can evaluate is just prose."""
    assert "git symbolic-ref" in persona


def test_referenced_serena_memories_exist(persona: str) -> None:
    """CLAUDE.md points sessions at the committed code map instead of re-exploring.

    If a memory is renamed or dropped, that pointer rots silently and sessions go back to
    burning tokens on rediscovery — which is the whole thing this setup prevents.
    """
    assert MEMORY_DIR.is_dir(), f"{MEMORY_DIR} must be committed so it ships to production"
    for name in ("codebase-map", "work-order-lifecycle",
                 "dev-vs-prod-environments", "testing"):
        assert f"`{name}`" in persona, f"CLAUDE.md no longer points at the {name!r} memory"
        assert (MEMORY_DIR / f"{name}.md").is_file(), f"missing memory file: {name}.md"
