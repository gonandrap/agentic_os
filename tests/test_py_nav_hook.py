"""A source-navigation Bash call at a `.py` path is refused, with the symbol call named.

§6 of docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md. The deny message IS the
mitigation: a refusal that does not name `find_symbol` and the `activate_project`
fallback buys a reworded retry, not a symbol call.
"""

from __future__ import annotations

import ast
import json

import pytest
from test_doc_nav_hook import _body

from jarvis import hooks, navigation
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


def test_a_whole_file_read_of_a_py_is_refused_and_a_limited_one_is_not(repo):
    """§5.1 of docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md: THE `.py` ARM HAS NO
    SIZE THRESHOLD. A `Read` with no `limit` is whole-file and is refused; a `Read`
    carrying ANY `limit` passes, however large. §1(a) is why — every expensive `.py`
    `Read` passed no `limit` (487,048 tok over 133 calls) and every cheap one passed a
    small one (1,242,656 tok over 1,422 calls). A test that only exercises a small
    `limit` cannot tell "no threshold" from "a threshold someone will add later"."""
    whole = {"tool_name": "Read", "tool_input": {"file_path": "src/a.py"},
             "cwd": str(repo)}
    limited = {"tool_name": "Read",
               "tool_input": {"file_path": "src/a.py", "limit": 4000},
               "cwd": str(repo)}

    decision = hooks.preflight_decision(whole, ON)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "mcp__plugin_serena_serena__find_symbol" in _reason(decision)
    assert hooks.preflight_decision(limited, ON) is None


def test_a_py_read_in_a_repo_with_no_serena_index_is_untouched(tmp_path):
    """§5.1: the `Read` arm shares the `.serena/project.yml` precondition. The refusal
    names `find_symbol`, so firing it where there is no symbol index strands the worker
    with neither symbols nor a whole-file read."""
    (tmp_path / ".jarvis").mkdir()
    whole = {"tool_name": "Read", "tool_input": {"file_path": "src/a.py"},
             "cwd": str(tmp_path)}

    assert hooks.preflight_decision(whole, ON) is None

    (tmp_path / ".serena").mkdir()
    (tmp_path / ".serena" / "project.yml").write_text("project_name: a\n")
    decision = hooks.preflight_decision(whole, ON)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_py_read_with_no_cwd_is_untouched():
    """No `cwd` means no project root to resolve, so the precondition cannot be checked
    and the arm must not fire."""
    whole = {"tool_name": "Read", "tool_input": {"file_path": "src/a.py"}}

    assert hooks.preflight_decision(whole, ON) is None


def test_a_read_of_a_non_py_path_is_untouched(repo):
    """The suffix set stays `SOURCE_SUFFIXES`, so a `.md` `Read` is the doc arm's
    business (tests/test_doc_nav_hook.py), not this one's."""
    md = {"tool_name": "Read", "tool_input": {"file_path": "docs/superpowers/specs/x.md"},
          "cwd": str(repo)}

    assert hooks.py_nav_decision(md, ON) is None


def test_the_refusal_is_reached_before_the_jarvis_auto_allow(repo):
    """The one that matters. `is_jarvis_command_chain` waves every `jarvis …` chain
    through, so an arm after it passes its unit test and does nothing in production.
    `navigates_source` masks quoted spans, so the unquoted `src/a.py` still denies."""
    decision = hooks.preflight_decision(
        _payload(f'cd {repo} && grep -rn "def total_for" src/a.py', repo), ON)

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("command", [
    "grep -rn TODO docs/",
    'find . -name "*.md"',
    "grep -rn coupon .",
])
def test_a_tree_sweep_for_non_python_text_is_allowed(repo, command):
    """Issue 936: the sweep arm ignored the suffix set, so every tree search anywhere in
    the repo got the symbol-call refusal."""
    assert hooks.py_nav_decision(_payload(command, repo), ON) is None


@pytest.mark.parametrize("command", [
    "grep --include='*.py' -rn total_for .",
    "grep --include=*.py -rn total_for .",
    "rg -g '*.py' total_for",
    'find . -name "*.py"',
])
def test_a_tree_sweep_scoped_to_python_is_refused(repo, command):
    assert hooks.py_nav_decision(
        _payload(command, repo), ON)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("command", [
    "grep -rn TODO docs/",
    'find . -name "*.md"',
    "grep -rn coupon .",
])
def test_a_tree_sweep_for_non_python_text_reaches_the_preflight_chain(repo, command):
    """The arm sits before the `is_jarvis_command_chain` auto-allow, so the unit answer
    and the production answer are the same one."""
    assert hooks.preflight_decision(_payload(command, repo), ON) is None


def test_a_python_scoped_sweep_is_refused_through_the_preflight_chain(repo):
    decision = hooks.preflight_decision(
        _payload("grep --include='*.py' -rn total_for .", repo), ON)

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "find_symbol" in _reason(decision)


def test_both_arms_use_the_counters_suffix_set_by_identity():
    """§5.5 of docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md: ONE spelling of the
    predicate. A second module constant here is the drift this tree has paid for twice —
    the counter would stop agreeing with the refusal about what source is. A narrower
    enforcement set arrives as a `worker.*` catalog key, never as a constant."""
    assert not hasattr(hooks, "PY_NAV_SUFFIXES")
    assert hooks.SOURCE_SUFFIXES is navigation.SOURCE_SUFFIXES

    node = _body("py_nav_decision")
    # The docstring QUOTES `.py`, so the literals are read off the CODE.
    code = [s for s in node.body if not (isinstance(s, ast.Expr)
                                         and isinstance(s.value, ast.Constant))]
    suffix_args = [n.args[-1] for s in code for n in ast.walk(s)
                   if isinstance(n, ast.Call)
                   and (n.func.id if isinstance(n.func, ast.Name)
                        else getattr(n.func, "attr", "")) in ("endswith",
                                                              "navigates_source")]

    assert len(suffix_args) == 2, ast.unparse(node)
    for arg in suffix_args:
        assert isinstance(arg, ast.Name), ast.unparse(arg)
        assert arg.id == "SOURCE_SUFFIXES", ast.unparse(arg)
    literals = {n.value for s in code for n in ast.walk(s)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert ".py" not in literals, literals


def test_py_nav_runs_after_the_investigator_refusal(repo):
    """Position: immediately AFTER `investigator_bash_decision`, so an investigator's
    mutating command gets the investigator's own message."""
    env = {**ON, hooks.WO_KIND_ENV: "investigator"}
    decision = hooks.preflight_decision(_payload("sed -i s/a/b/ src/a.py", repo), env)

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "An investigation READS" in _reason(decision)
    assert "find_symbol" not in _reason(decision)
