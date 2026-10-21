"""The worker's system prompt must not move between turns"""

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
    """Asserting the literal key, not a constant: this is a measured cost decision and reversing it should..."""
    assert _settings(project)["includeGitInstructions"] is False


def test_the_briefing_replaces_what_the_setting_removed(project, jarvis_home):
    """Both attribution lines survive the switch-off"""
    spec = ProjectSpec(name="proj_a", path=project, description="")
    prompt = briefing_for(spec, {"id": "wo-prefix", "title": "t"})["append_system_prompt"]

    assert _settings(project)["includeGitInstructions"] is False, "flag and briefing ship together"
    assert "Co-Authored-By:" in prompt
    assert "<noreply@anthropic.com>" in prompt
    assert PR_ATTRIBUTION in prompt
    # the snapshot is gone, so the worker has to be told to look for itself
    assert "git status" in prompt


def test_the_briefing_is_identical_on_every_turn(project, jarvis_home):
    """The point of the exercise"""
    spec = ProjectSpec(name="proj_a", path=project, description="")
    wo = {"id": "wo-prefix", "title": "t"}

    first = briefing_for(spec, wo)["append_system_prompt"]
    (project / "scratch.txt").write_text("a worker does some work")
    second = briefing_for(spec, wo)["append_system_prompt"]

    assert first == second


def test_a_projects_standing_instructions_still_reach_the_worker(project, jarvis_home):
    """The briefing is composed with them, not in place of them"""
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
    """A generic trailer is correct and a wrong model name is not"""
    assert attribution_name(model) == expected
    assert f"Co-Authored-By: {expected} <noreply@anthropic.com>" in git_briefing(model)
