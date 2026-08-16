"""The worker's system prompt must not move between turns.

Claude Code builds a git-status snapshot into the dynamic half of the system prompt and
rebuilds it once per process — and a worker turn IS a process (`claude -p --resume`). A
worker therefore invalidated its own cached prefix by working: it edited a file, the
snapshot changed, and the whole conversation had to be re-written at the next turn.
Measured live on CLI 2.1.233, two arms differing only in this setting, each run twice:

    default                       turn 2: 10,983 written / 15,995 read  (the static
                                          system prompt alone — a cold boundary)
    includeGitInstructions=false  turn 2:     552 written / 26,113 read  (warm)

The setting is the only switch that removes the snapshot, and it takes the CLI's own git
and commit/PR instruction blocks with it — so `worker_brief.git_briefing` restates them
as static text. These tests hold the two halves together: the flag without the briefing
silently drops the attribution trailers from every commit and PR the fleet writes, and
the briefing without the flag pays for text the CLI already supplies.
"""

from __future__ import annotations

import json

import pytest

from jarvis.catalog import ProjectSpec
from jarvis.dispatch import _write_worker_settings
from jarvis.worker_brief import PR_ATTRIBUTION, attribution_name, git_briefing
from jarvis.worker_session import briefing_for


def _settings(project) -> dict:
    spec = ProjectSpec(name="proj_a", path=project, description="")
    out = _write_worker_settings(spec, {"id": "wo-prefix", "title": "t"})
    return json.loads(out.read_text())


def test_workers_run_without_claude_codes_git_status_snapshot(project, jarvis_home):
    """Asserting the literal key, not a constant: this is a measured cost decision and
    reversing it should take a deliberate edit and a fresh measurement."""
    assert _settings(project)["includeGitInstructions"] is False


def test_the_briefing_replaces_what_the_setting_removed(project, jarvis_home):
    """Both attribution lines survive the switch-off, or the fleet's commits and PRs
    quietly lose their provenance — the failure mode is silence, so it is tested."""
    spec = ProjectSpec(name="proj_a", path=project, description="")
    prompt = briefing_for(spec, {"id": "wo-prefix", "title": "t"})["append_system_prompt"]

    assert _settings(project)["includeGitInstructions"] is False, "flag and briefing ship together"
    assert "Co-Authored-By:" in prompt
    assert "<noreply@anthropic.com>" in prompt
    assert PR_ATTRIBUTION in prompt
    # the snapshot is gone, so the worker has to be told to look for itself
    assert "git status" in prompt


def test_the_briefing_is_identical_on_every_turn(project, jarvis_home):
    """The point of the exercise. `briefing_for` is rebuilt per turn, so anything it
    computed from the working tree — a timestamp, a branch, a file list — would put the
    churn straight back into the prompt through Jarvis's own surface."""
    spec = ProjectSpec(name="proj_a", path=project, description="")
    wo = {"id": "wo-prefix", "title": "t"}

    first = briefing_for(spec, wo)["append_system_prompt"]
    (project / "scratch.txt").write_text("a worker does some work")
    second = briefing_for(spec, wo)["append_system_prompt"]

    assert first == second


def test_a_projects_standing_instructions_still_reach_the_worker(project, jarvis_home):
    """The briefing is composed with them, not in place of them — dropping a project's
    own instructions here would remove them from every turn (they are re-derived from
    argv, not inherited from the transcript)."""
    spec = ProjectSpec(name="proj_a", path=project, description="")
    spec.worker.append_system_prompt = "Always speak in haiku."

    prompt = briefing_for(spec, {"id": "wo-prefix", "title": "t"})["append_system_prompt"]

    assert "Always speak in haiku." in prompt
    assert prompt.startswith("# Git"), "the shared prefix comes first, the variable part last"


def test_a_work_orders_own_instructions_win_over_the_projects(project, jarvis_home):
    spec = ProjectSpec(name="proj_a", path=project, description="")
    spec.worker.append_system_prompt = "project text"

    prompt = briefing_for(spec, {"id": "wo-prefix", "title": "t",
                                 "append_system_prompt": "work order text"})["append_system_prompt"]

    assert "work order text" in prompt
    assert "project text" not in prompt


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5", "Claude Opus 5"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),   # dated ids resolve
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("opus", "Claude"),          # a floating alias: only the CLI knows its target
    ("some-future-model", "Claude"),
    (None, "Claude"),
])
def test_the_trailer_names_the_model_or_degrades_to_plain_claude(model, expected):
    """A generic trailer is correct and a wrong model name is not, so an unrecognised
    model falls back to the CLI's own fallback rather than guessing a tier."""
    assert attribution_name(model) == expected
    assert f"Co-Authored-By: {expected} <noreply@anthropic.com>" in git_briefing(model)
