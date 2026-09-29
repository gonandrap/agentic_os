"""A heredoc that writes a file is a file edit, and belongs in `Edit`/`Write`.

Fix 1 of docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md. The
incident: a subagent appended tests to a file with `python3 - <<'PYEOF'`, the test TEXT
contained `gh pr merge`, and the command classifier — which is right about what a
heredoc handed to an interpreter is — filed a gate request nobody ever argued.

Both directions are pinned here, and the negatives are the point: this rule denies ONLY
on positive evidence of a file write inside the worker's own worktree. A heredoc that
computes and prints, `git commit -F - <<EOF`, and a write outside the worktree are all
somebody else's business.
"""

from __future__ import annotations

import json

import pytest

from jarvis import gates
from jarvis.hooks import preflight_decision
from jarvis.project_store import ProjectStore

ALL_GATES = gates.GateConfig(enabled=frozenset(gates.KIND_NAMES))


def _decision(result):
    if result is None:
        return None
    return result["hookSpecificOutput"]["permissionDecision"]


def _reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.fixture()
def worker(jarvis_home, project):
    """A running work order whose worktree is `project`, with every gate live."""
    store = ProjectStore(project)
    wo = store.create_work_order("write some tests", description="append to a test file")
    store.set_status(wo["id"], "running")
    env = {
        "JARVIS_WO_ID": wo["id"],
        "JARVIS_PROJECT": "proj_a",
        "JARVIS_PROJECT_PATH": str(project),
        "JARVIS_GATES": ALL_GATES.to_json(),
    }

    class Handle:
        def __init__(self):
            self.store, self.wo, self.env, self.project = store, wo, env, project

        def attempt(self, command, **extra):
            return preflight_decision(
                {"tool_name": "Bash", "tool_input": {"command": command},
                 "cwd": str(project), "session_id": "s-1", **extra}, env)

        def events(self, kind):
            return [e for e in store.list_events(wo["id"]) if e["kind"] == kind]

    yield Handle()
    store.close()


# -- the deny -------------------------------------------------------------------------


def test_a_heredoc_that_writes_a_worktree_file_is_denied_before_a_gate_is_filed(worker):
    """The incident, in one call: a file edit routed through an interpreter, whose
    CONTENT happens to name a privileged action. No approval row may exist."""
    target = worker.project / "tests" / "test_automerge.py"
    command = (
        f"python3 - <<'PYEOF'\n"
        f"body = \"assert argv == ['pr','merge']\\n\"\n"
        f"open({str(target)!r}, 'a').write(body)\n"
        f"PYEOF"
    )

    result = worker.attempt(command, agent_type="jarvis-implementer")

    assert _decision(result) == "deny"
    assert "Edit" in _reason(result) and "Write" in _reason(result)
    # The whole point of running before `gate_decision`: nothing is FILED for this class.
    assert worker.store.list_approvals(worker.wo["id"]) == []
    refused = worker.events("heredoc_write_refused")
    assert len(refused) == 1
    payload = json.loads(refused[0]["payload"])
    assert payload["agent_type"] == "jarvis-implementer"
    assert payload["session_id"] == "s-1"


def test_a_shell_redirect_of_a_heredoc_into_a_worktree_file_is_denied(worker):
    result = worker.attempt("cat <<'EOF' > tests/test_x.py\nimport os\nEOF")

    assert _decision(result) == "deny"
    assert worker.store.list_approvals(worker.wo["id"]) == []


def test_a_heredoc_piped_into_sed_in_place_is_denied(worker):
    command = f"cat <<'EOF' | sed -i 's/a/b/' {worker.project}/tests/test_x.py\na\nEOF"

    assert _decision(worker.attempt(command)) == "deny"


def test_write_text_in_a_heredoc_body_is_denied(worker):
    command = ("python3 - <<'PY'\n"
               "from pathlib import Path\n"
               "Path('tests/test_x.py').write_text('x')\n"
               "PY")

    assert _decision(worker.attempt(command)) == "deny"


# -- the negatives, which are the reason this rule is narrow ---------------------------


def test_a_compute_and_print_heredoc_is_left_alone(worker):
    command = "python3 - <<'PY'\nprint(sum(range(10)))\nPY"

    assert worker.attempt(command) is None
    assert worker.events("heredoc_write_refused") == []


def test_git_commit_with_a_heredoc_is_untouched(worker):
    """Data, not a program — the distinction `gate_rules.program_spans` already owns."""
    command = "git commit -F - <<'EOF'\nfix the thing\nEOF"

    assert _decision(worker.attempt(command)) != "deny"


def test_a_heredoc_writing_outside_the_worktree_is_not_this_rules_business(worker, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    command = f"python3 - <<'PY'\nopen({str(outside)!r}, 'w').write('x')\nPY"

    assert worker.attempt(command) is None


def test_a_plain_command_with_no_heredoc_is_left_alone(worker):
    assert worker.attempt("uv run pytest tests/ -q") is None


def test_an_interactive_session_is_not_governed(project):
    command = "python3 - <<'PY'\nopen('tests/x.py','w').write('x')\nPY"

    assert preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(project)},
        {}) is None


# -- what must stay true --------------------------------------------------------------


def test_a_privileged_command_that_also_writes_a_file_stays_blocked(worker):
    """Denied HERE instead of gated — still blocked, and a retry without the file write
    reaches the gate normally."""
    command = ("python3 - <<'PY' > notes.txt\nprint('done')\nPY\n"
               "gh pr merge 31 --squash")

    assert _decision(worker.attempt(command)) == "deny"

    retry = worker.attempt("gh pr merge 31 --squash")
    assert _decision(retry) == "deny"
    assert [a["kind"] for a in worker.store.list_approvals(worker.wo["id"])] == ["pr_merge"]
