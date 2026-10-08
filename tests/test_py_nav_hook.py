"""A source-navigation Bash call at a `.py` path is refused, with the symbol call named.

§6 of docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md. The deny message IS the
mitigation: a refusal that does not name `find_symbol` and the `activate_project`
fallback buys a reworded retry, not a symbol call.
"""

from __future__ import annotations

import json

import pytest

from jarvis import hooks
from jarvis.catalog import ProjectSpec, WorkerDefaults
from jarvis.dispatch import _write_worker_settings

ON = {"JARVIS_PY_NAV_HOOK": "on", "JARVIS_WO_ID": "wo-1"}


@pytest.fixture
def repo(tmp_path):
    """A managed project WITH a symbol index: `.jarvis/` for `find_project_root`."""
    (tmp_path / ".jarvis").mkdir()
    (tmp_path / ".serena").mkdir()
    (tmp_path / ".serena" / "project.yml").write_text("project_name: a\n")
    return tmp_path


def _payload(command, cwd) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def _reason(decision) -> str:
    return decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_grep_at_a_py_path_is_refused(repo):
    decision = hooks.py_nav_decision(
        _payload('grep -rn "def total_for" src/pricing.py', repo), ON)

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("command", [
    "sed -n '1,40p' src/a.py",
    "cat src/a.py",
    "head -50 src/a.py",
])
def test_sed_n_and_cat_and_head_at_a_py_path_are_refused(repo, command):
    decision = hooks.py_nav_decision(_payload(command, repo), ON)

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_grep_for_a_word_in_a_markdown_file_is_allowed(repo):
    """The negative control. A hook that refuses genuine text search is a regression,
    not a stricter version of this one."""
    assert hooks.py_nav_decision(_payload("grep -rn coupon README.md", repo), ON) is None


def test_a_non_py_source_file_is_not_refused(repo):
    """The suffix set is `(".py",)` only — narrower than the counter's wide set."""
    assert hooks.py_nav_decision(_payload("grep -rn x app.ts", repo), ON) is None


@pytest.mark.parametrize("command", [
    "uv run pytest tests/test_x.py",
    "git diff src/a.py",
    "jarvis wo show wo-1",
])
def test_pytest_and_git_and_jarvis_are_untouched(repo, command):
    assert hooks.py_nav_decision(_payload(command, repo), ON) is None


def test_the_deny_message_names_the_symbol_call_and_the_activation_fallback(repo):
    reason = _reason(hooks.py_nav_decision(_payload("cat src/a.py", repo), ON))

    assert "mcp__plugin_serena_serena__find_symbol" in reason
    assert "mcp__serena__" in reason
    assert "activate_project" in reason


def test_an_interactive_session_is_untouched(repo):
    """Keyed on `JARVIS_WO_ID`: a session the user opened in a managed project is theirs."""
    assert hooks.py_nav_decision(_payload("cat src/a.py", repo),
                                 {"JARVIS_PY_NAV_HOOK": "on"}) is None


def test_a_project_with_no_serena_index_is_untouched(tmp_path):
    """No `.serena/project.yml` means no symbol index, and a worker with neither that nor
    grep cannot read code at all."""
    (tmp_path / ".jarvis").mkdir()

    assert hooks.py_nav_decision(_payload("cat src/a.py", tmp_path), ON) is None


@pytest.mark.parametrize("env", [
    {"JARVIS_WO_ID": "wo-1"},
    {"JARVIS_PY_NAV_HOOK": "off", "JARVIS_WO_ID": "wo-1"},
])
def test_the_key_is_off_unless_turned_on(repo, env):
    assert hooks.py_nav_decision(_payload("cat src/a.py", repo), env) is None


def test_the_key_ships_off(project, jarvis_home):
    """The off literal Jarvis actually writes into a worker's settings file."""
    spec = ProjectSpec(name="proj_a", path=project, description="",
                       worker=WorkerDefaults())
    env = json.loads(
        _write_worker_settings(spec, {"id": "wo-nav01", "title": "t"}).read_text())["env"]

    assert env["JARVIS_PY_NAV_HOOK"] == "off"


def test_the_refusal_is_reached_before_the_jarvis_auto_allow(repo):
    """The one that matters. `is_jarvis_command_chain` waves every `jarvis …` chain
    through, so an arm after it passes its unit test and does nothing in production.
    `navigates_source` masks quoted spans, so the unquoted `src/a.py` still denies."""
    decision = hooks.preflight_decision(
        _payload(f'cd {repo} && grep -rn "def total_for" src/a.py', repo), ON)

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_py_nav_runs_after_the_investigator_refusal(repo):
    """Position: immediately AFTER `investigator_bash_decision`, so an investigator's
    mutating command gets the investigator's own message."""
    env = {**ON, hooks.WO_KIND_ENV: "investigator"}
    decision = hooks.preflight_decision(_payload("sed -i s/a/b/ src/a.py", repo), env)

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "An investigation READS" in _reason(decision)
    assert "find_symbol" not in _reason(decision)
