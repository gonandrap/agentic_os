"""`worker.bash_first`: the env Jarvis writes into a worker's settings file.

Spec: docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md §1, §2.

Claude Code's `auto` permission mode appends a bash-first steer to the system prompt
AFTER Jarvis's own `--append-system-prompt`, naming `cat`, `head`, `sed -n`, `grep` and
`find` — which is the fleet's measured tool mix, and why "Serena first, grep second" had
a 0% hit rate over 276 transcripts. Two undocumented env keys turn it off and pin its
variant; this is where Jarvis writes them.

What these tests assert is the STRING WRITTEN, never CLI behaviour. That distinction is
deliberate: the vendor's reading of the value is a probe's job (§2 records the probes),
and an assertion about a vendor binary would make CI hostage to it.
"""

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
    """The default, and the only value the fleet will run. `"false"` is the probed
    working value (§2: `/tmp/bf6` produced no `auto_mode` attachment at all)."""
    env = _env(project, "off")
    assert env[THRIFTY] == "false"
    assert COZY not in env


@pytest.mark.parametrize("variant", ["relaxed", "strict"])
def test_relaxed_and_strict_pin_the_variant_on(project, jarvis_home, variant):
    """`strict` exists for the eval's deterministic arm, not for symmetry: the variant is
    otherwise a per-session statsig cohort draw, and an arm whose strength comes from a
    draw is not a measurement (§1)."""
    env = _env(project, variant)
    assert env[THRIFTY] == "true"
    assert env[COZY] == variant


def test_cli_writes_neither_key(project, jarvis_home):
    """The one value under which Jarvis asserts NO answer about a vendor behaviour it
    does not own — which is why the setting is a string enum and not a boolean (§1)."""
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
    """Claude Code's `env` is a `Record<string,string>` — an integer risks the CLI
    rejecting the whole settings file. The `Record<string,string>` rule as a test instead
    of as a comment, so it catches the next integer too (§5.1)."""
    for key, value in _env(project, bash_first).items():
        assert isinstance(value, str), f"settings['env'][{key!r}] is {type(value)}"
