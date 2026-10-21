"""`worker.bash_first`: the env Jarvis writes into a worker's settings file (docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md §1)"""

import json

import pytest

from jarvis.catalog import ProjectSpec, WorkerDefaults
from jarvis.dispatch import _write_worker_settings

THRIFTY = "CLAUDE_CODE_THRIFTY_SONIC"
COZY = "CLAUDE_CODE_COZY_TEAPOT"


def _env(project, bash_first: str) -> dict:
    spec = ProjectSpec(name="proj_a", path=project, description="",
                       worker=WorkerDefaults(bash_first=bash_first))
    written = json.loads(
        _write_worker_settings(spec, {"id": f"wo-bf-{bash_first}"}).read_text())
    return written["env"]


def test_off_disables_the_steer_and_writes_no_variant(project, jarvis_home):
    """The default, and the only value the fleet will run"""
    env = _env(project, "off")
    assert env[THRIFTY] == "false"
    assert COZY not in env


@pytest.mark.parametrize("variant", ["relaxed", "strict"])
def test_relaxed_and_strict_pin_the_variant_on(project, jarvis_home, variant):
    """`strict` exists for the eval's deterministic arm"""
    env = _env(project, variant)
    assert env[THRIFTY] == "true"
    assert env[COZY] == variant


def test_cli_writes_neither_key(project, jarvis_home):
    """The one value under which Jarvis asserts NO answer about a vendor behaviour it does not own"""
    env = _env(project, "cli")
    assert THRIFTY not in env
    assert COZY not in env


def test_the_project_default_is_off(project, jarvis_home):
    spec = ProjectSpec(name="proj_a", path=project, description="")
    env = json.loads(
        _write_worker_settings(spec, {"id": "wo-bf-default"}).read_text())["env"]
    assert env[THRIFTY] == "false"


@pytest.mark.parametrize("bash_first", ["off", "relaxed", "strict", "cli"])
def test_every_env_value_is_a_string(project, jarvis_home, bash_first):
    """Claude Code's `env` is a `Record<string,string>`"""
    for key, value in _env(project, bash_first).items():
        assert isinstance(value, str), f"settings['env'][{key!r}] is {type(value)}"
