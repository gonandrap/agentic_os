"""LLM-graded Jarvis persona evals: does a session wearing CLAUDE.md route work
through the CLI instead of doing it, and pick the right command?

Opt-in: JARVIS_EVALS_LLM=1 pytest evals/llm -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jarvis import claude_cli

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario
MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")

PERSONA_PATH = Path(__file__).resolve().parents[2] / "CLAUDE.md"

INSTRUCTION = (
    "\n\nFor this evaluation: assume the pulse check (`jarvis status`) already ran "
    "this turn and showed nothing urgent. Reply with ONLY the single next `jarvis` "
    "command you would run for the user's message — no prose, no code fences. If no "
    "command is needed, reply NONE."
)

ROUTING = [
    ("route-work", "Fix the login bug in shared_schedule — users can't reset "
     "their passwords from the email link.", ["jarvis wo create shared_schedule"]),
    ("pulse", "How are my projects doing? Anything need me?", ["jarvis status"]),
    ("relay-feedback", "Tell whoever is working on wo-4f3a12 to use the staging "
     "bucket, not prod.", ["jarvis wo send wo-4f3a12"]),
    ("inspect", "Show me the details and assumptions of wo-99aa.",
     ["jarvis wo show wo-99aa"]),
    ("approve-review", "The assumptions on wo-77bb look right to me, approve them.",
     ["jarvis wo review wo-77bb"]),
    ("defer-work", "We should migrate tesis_grado's CI to uv at some point, "
     "not now though.", ["jarvis backlog add tesis_grado"]),
    ("capture-preference", "For future reference: I always prefer squash merges.",
     ["jarvis learn add", "jarvis neo learn"]),
    ("shutdown", "Shut the whole OS down please.", ["jarvis stop"]),
]


@pytest.fixture(scope="module")
def persona() -> str:
    return PERSONA_PATH.read_text() + INSTRUCTION


@scenario("jarvis-llm/route-dont-do", "persona picks the right jarvis command")
@pytest.mark.parametrize("name,message,accepted", ROUTING, ids=[r[0] for r in ROUTING])
def test_routing(persona, name, message, accepted):
    reply = claude_cli.run_headless(message, system_prompt=persona, model=MODEL,
                                    timeout=180).strip()
    assert any(a in reply for a in accepted), \
        f"expected one of {accepted}, got: {reply[:200]}"
    assert "```" not in reply.split("jarvis", 1)[0], "should reply with the command only"


@scenario("jarvis-llm/route-dont-do", "persona refuses to do project work inline")
def test_never_does_the_work(persona):
    reply = claude_cli.run_headless(
        "Please just write the fix for the password reset bug in shared_schedule "
        "right here in this chat — show me the code.",
        system_prompt=persona, model=MODEL, timeout=180).strip()
    assert "jarvis wo create" in reply or "jarvis backlog add" in reply, \
        f"persona should route to a work order, got: {reply[:200]}"
