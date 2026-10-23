"""A push a worker makes is held to the one-line-comment rule its repo puts in required CI (issue 1231)"""
from __future__ import annotations

import shutil
from pathlib import Path

from jarvis import hooks

CHECKER = Path(__file__).resolve().parent.parent / "scripts" / "trim_comments.py"


def _repo(tmp_path: Path, source: str, with_checker: bool = True) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text(source, encoding="utf-8")
    if with_checker:
        (tmp_path / "scripts").mkdir()
        shutil.copy(CHECKER, tmp_path / "scripts" / "trim_comments.py")
    return tmp_path


def _preflight(command: str, cwd: Path, **env):
    return hooks.preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)},
        {"JARVIS_WO_ID": "wo-tc01", **env})


#: Fixtures the real checker is run against, so what the hook asserts is what CI asserts
OVERLONG = '''def f():
    # The first line of the explanation
    # The second line, which CI refuses
    return 1
'''

ONE_LINE = '''def f():
    # The whole explanation, on one line
    return 1
'''


def test_a_push_carrying_a_multi_line_comment_is_refused_and_the_file_is_named(tmp_path):
    decision = _preflight("git push -u origin HEAD", _repo(tmp_path, OVERLONG))
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "src/thing.py" in reason
    # The trap kn-17c9af66 recorded: the blind fix deletes the half the comment existed for
    assert "--check" in reason and "truncates" in reason


def test_a_clean_tree_is_not_this_hooks_business(tmp_path):
    assert _preflight("git push", _repo(tmp_path, ONE_LINE)) is None


def test_a_repo_without_the_checker_is_left_alone(tmp_path):
    repo = _repo(tmp_path, OVERLONG, with_checker=False)
    assert _preflight("git push", repo) is None


def test_an_interactive_session_is_never_refused(tmp_path):
    repo = _repo(tmp_path, OVERLONG)
    assert hooks.preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(repo)},
        {}) is None


def test_only_a_push_is_checked(tmp_path):
    repo = _repo(tmp_path, OVERLONG)
    for command in ("git commit -m x", "git log --oneline", "git status", "pushd /tmp",
                    "echo git push"):
        assert _preflight(command, repo) is None, command


def test_a_push_reached_through_a_chain_or_an_absolute_path_still_counts(tmp_path):
    repo = _repo(tmp_path, OVERLONG)
    for command in ("git add -A && git push", "/usr/bin/git push", "git -C . push"):
        assert _preflight(command, repo) is not None, command


def test_an_unparseable_command_is_not_read_as_a_push(tmp_path):
    assert _preflight('git push "unclosed', _repo(tmp_path, OVERLONG)) is None
