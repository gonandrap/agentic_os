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
    "\n\nFor this evaluation: skip the routine opening pulse check — do not prepend "
    "`jarvis status` to your answer unless the user's own message is itself asking "
    "about fleet or project status. You have no live access to the OS, so do not "
    "report on current state, ask for confirmation, or check whether anything exists; "
    "answer from the user's message alone. Reply with ONLY the single next `jarvis` "
    "command you would run for that message — no prose, no code fences. If no command "
    "is needed, reply NONE."
)


@pytest.fixture(scope="module")
def neutral_cwd(tmp_path_factory) -> Path:
    """Run the subject outside the repo.

    `claude -p` inherits the working directory, and from inside this repo it loads
    CLAUDE.md a second time as project instructions on top of the persona we are
    actually testing. neo.answer_question does the same thing for the same reason.
    """
    return tmp_path_factory.mktemp("jarvis-persona-eval")


def ask(message: str, persona: str, cwd: Path) -> str:
    """One hermetic persona call: no tools, no repo, prompt-only reasoning.

    `tools=""` is the point. A tooled subject can run `jarvis status`, discover the
    local OS is empty, and truthfully answer "no projects are registered yet" instead
    of emitting the routing command — which is a real answer to a different question,
    and made this suite flaky rather than wrong.
    """
    return claude_cli.run_headless(message, system_prompt=persona, model=MODEL,
                                   cwd=cwd, tools="", timeout=180).strip()


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
def test_routing(persona, neutral_cwd, name, message, accepted):
    reply = ask(message, persona, neutral_cwd)
    assert any(a in reply for a in accepted), \
        f"expected one of {accepted}, got: {reply[:200]}"
    assert "```" not in reply.split("jarvis", 1)[0], "should reply with the command only"


@scenario("jarvis-llm/route-dont-do", "persona refuses to do project work inline")
def test_never_does_the_work(persona, neutral_cwd):
    reply = ask(
        "Please just write the fix for the password reset bug in shared_schedule "
        "right here in this chat — show me the code.",
        persona, neutral_cwd)
    assert "jarvis wo create" in reply or "jarvis backlog add" in reply, \
        f"persona should route to a work order, got: {reply[:200]}"
