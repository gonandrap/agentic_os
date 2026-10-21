"""`worker.tool_search`: the env Jarvis writes to decide whether the symbol tools are in a worker's... (docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md §4.)"""

import json

import pytest

from jarvis.catalog import ProjectSpec, WorkerDefaults
from jarvis.dispatch import _write_worker_settings

TOOL_SEARCH = "ENABLE_TOOL_SEARCH"


def _env(project, tool_search: str) -> dict:
    spec = ProjectSpec(name="proj_a", path=project, description="",
                       worker=WorkerDefaults(tool_search=tool_search))
    written = json.loads(
        _write_worker_settings(spec, {"id": f"wo-ts-{tool_search}"}).read_text())
    return written["env"]


def test_off_lists_every_tool_and_writes_false(project, jarvis_home):
    """The state the feature exists to reach: every symbol tool in the tool list with a full schema"""
    env = _env(project, "off")
    assert env[TOOL_SEARCH] == "false"


def test_on_restores_the_vendor_default_explicitly(project, jarvis_home):
    """`on` is not the same answer as `cli`: it pins the deferral regardless of what the binary would have..."""
    env = _env(project, "on")
    assert env[TOOL_SEARCH] == "true"


def test_cli_writes_the_key_at_all(project, jarvis_home):
    """The one value under which Jarvis asserts NO answer about a vendor behaviour it does not own"""
    assert TOOL_SEARCH not in _env(project, "cli")


def test_the_project_default_is_the_shipped_state(project, jarvis_home):
    """§7's flip, measured: `on` — deferral PINNED, writing `"true"`"""
    from jarvis.catalog import DEFAULT_WORKER_TOOL_SEARCH

    assert DEFAULT_WORKER_TOOL_SEARCH == "on"
    spec = ProjectSpec(name="proj_a", path=project, description="")
    env = json.loads(
        _write_worker_settings(spec, {"id": "wo-ts-default"}).read_text())["env"]
    assert env[TOOL_SEARCH] == "true"


@pytest.mark.parametrize("tool_search", ["off", "on", "cli"])
def test_every_env_value_is_a_string(project, jarvis_home, tool_search):
    """Claude Code's `env` is a `Record<string,string>`"""
    for key, value in _env(project, tool_search).items():
        assert isinstance(value, str), f"settings['env'][{key!r}] is {type(value)}"
