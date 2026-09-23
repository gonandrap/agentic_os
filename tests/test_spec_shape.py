"""A spec without a problem and a fix is refused at the Write.

§8 of docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md. At `jarvis wo
finish` the spec is already committed and in the pull request, so the correction costs a
validation round; at the Write it costs one retry inside the turn.
"""

from __future__ import annotations

from jarvis import hooks

ENV = {"JARVIS_WO_ID": "wo-spec01", "JARVIS_WO_KIND": "worker"}
CONFORMING = "# Title\n\n## 1. The problem\n\nBroken.\n\n## 2. The fix\n\nMechanism.\n"


def _write(path: str, content: str, tool: str = "Write", env=None):
    return hooks.spec_shape_decision(
        {"tool_name": tool,
         "tool_input": {"file_path": path, "content": content}},
        ENV if env is None else env)


def _decision(result):
    return None if result is None else result["hookSpecificOutput"]["permissionDecision"]


SPEC = "/repo/docs/superpowers/specs/2026-09-23-thing.md"


def test_spec_write_without_problem_heading_denied():
    result = _write(SPEC, "# Title\n\n## The fix\n\nDo it.\n")

    assert _decision(result) == "deny"
    assert "problem" in result["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_spec_write_without_fix_heading_denied():
    assert _decision(_write(SPEC, "# Title\n\n## The problem\n\nBroken.\n")) == "deny"


def test_conforming_spec_allowed():
    assert _write(SPEC, CONFORMING) is None


def test_heading_match_is_case_insensitive():
    assert _write(SPEC, "# T\n\n### WHAT IS BROKEN\n\nx\n\n### Solution\n\ny\n") is None


def test_body_mention_is_not_a_heading():
    """A spec that merely says "the problem" in a sentence has not got a section."""
    body = "# Title\n\nThe problem is real and the fix is obvious.\n\n## Notes\n\nx\n"

    assert _decision(_write(SPEC, body)) == "deny"


def test_edit_not_checked():
    """An Edit payload carries a fragment, not the document — checking it would refuse
    every legitimate incremental edit to a conforming spec."""
    assert _write(SPEC, "one changed paragraph", tool="Edit") is None


def test_non_spec_markdown_allowed():
    assert _write("/repo/docs/DEPLOYMENT.md", "# Deploy\n\nsteps\n") is None
    assert _write("/repo/specs/notes.txt", "no headings") is None


def test_no_op_for_non_worker_session():
    assert _write(SPEC, "# Title\n", env={}) is None


def test_planner_design_doc_is_not_checked():
    """A feature spec is a different shape — sections a child each implements, plus the
    `Agent profile` appendix — and `plans` validates it on those terms."""
    env = dict(ENV, JARVIS_WO_KIND="planner")
    assert _write(SPEC, "# Title\n", env=env) is None


def test_either_missing_is_a_refusal_not_only_both():
    """OR, not AND. A document with a problem and no fix is the exact defect the user
    raised on wo-dd8668fa; requiring both to be absent would let it straight through."""
    problem_only = "# T\n\n## The problem\n\nBroken.\n"
    fix_only = "# T\n\n## The fix\n\nMechanism.\n"
    neither = "# T\n\n## Notes\n\nx\n"

    assert _decision(_write(SPEC, problem_only)) == "deny"
    assert _decision(_write(SPEC, fix_only)) == "deny"
    assert _decision(_write(SPEC, neither)) == "deny"
    assert _write(SPEC, CONFORMING) is None
