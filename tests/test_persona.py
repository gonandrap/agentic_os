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


# improvement orders: docs/superpowers/specs/2026-09-23-improvement-orders.md
@pytest.mark.parametrize(
    "verb",
    [
        "jarvis io create",
        "jarvis io list [project] [--all] / show <id>",
        "jarvis io review",
        "jarvis io cancel",
    ],
)
def test_crib_sheet_documents_every_improvement_order_verb(persona: str, verb: str) -> None:
    """An operator verb missing from the crib sheet is a verb the operator never uses."""
    assert verb in persona, f"crib sheet no longer documents {verb!r}"


def test_improvement_order_block_expands_the_abbreviation(persona: str) -> None:
    """Unexpanded, `io` reads as input/output and the whole block means nothing."""
    assert '"io" = improvement' in persona, (
        'the io block must spell out that "io" = improvement order'
    )


def test_improvement_orders_sit_with_the_other_order_types(persona: str) -> None:
    """Order types are read as a group; a stray block is one the operator skips."""
    assert persona.index("jarvis io create") > persona.index("jarvis fo create"), (
        "the io block must follow the fo block, not precede it"
    )
    assert persona.index("jarvis io create") < persona.index(DEV_MODE_HEADING), (
        "the io block is operator content and must stay above the dev-mode override"
    )


def test_prime_directives_still_precede_the_improvement_order_block(persona: str) -> None:
    """Routing is the persona's first job; no command block may outrank it."""
    assert persona.index("Route, don't do") < persona.index("jarvis io create"), (
        "the prime directives must still dominate the crib sheet"
    )


def test_improvement_order_block_is_shorter_than_the_feature_order_block(persona: str) -> None:
    """Crib-sheet bloat pushes the directives down the page and regresses the LLM evals."""
    fo_at = persona.index("jarvis fo create")
    io_at = persona.index("jarvis io create")
    backlog_at = persona.index("jarvis backlog promote")
    fo_block = persona[fo_at:io_at]
    io_block = persona[io_at:backlog_at]
    assert len(io_block) < len(fo_block), (
        f"io block ({len(io_block)} chars) must stay shorter than the fo block "
        f"({len(fo_block)} chars): evals/llm/test_jarvis_judgment.py loads CLAUDE.md as a "
        "bare system prompt and a longer crib sheet buries the prime directives"
    )
