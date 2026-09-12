"""`github.pr_artifact`, and the read-only property the panel's blind review rests on.

docs/superpowers/specs/2026-09-12-the-pull-request-is-the-artifact.md §2.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jarvis import github

PR = "https://github.com/x/y/pull/12"


@pytest.fixture()
def artifact(fake_gh):
    fake_gh.set_pr_artifact(
        PR, title="[wo-1] Do the thing", body="## Summary\nit does the thing",
        diff="diff --git a/a.py b/a.py\n@@\n+one\n",
        files=[{"path": "a.py", "additions": 3, "deletions": 1}],
        checks=[{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}])
    return fake_gh


# ------------------------------------------------------------------ the read is read-only


def test_every_gh_command_this_module_builds_is_a_declared_read_verb():
    """The whole mechanism, and the reason the seats stayed tool-free.

    Walks the module for list literals whose first element is a bare string — the shape
    every `gh` argument list in here has — and holds each one's opening (verb, subverb)
    against `READ_ONLY_VERBS`. A `["pr", "merge", ...]` added anywhere in this file, at
    any nesting depth, fails here; there is no way to add a write verb without also
    editing this test, which is exactly the point.
    """
    tree = ast.parse(Path(github.__file__).read_text())
    built = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or len(node.elts) < 2:
            continue
        head = node.elts[:2]
        if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in head):
            built.add((head[0].value, head[1].value))  # type: ignore[attr-defined]
    assert built, "found no gh argument lists at all — has the module been rewritten?"
    assert built <= set(github.READ_ONLY_VERBS), (
        f"a non-read gh command is built in github.py: "
        f"{sorted(built - set(github.READ_ONLY_VERBS))}")


def test_fetching_an_artifact_runs_only_read_verbs(artifact):
    github.pr_artifact(PR)
    for call in artifact.calls:
        assert tuple(call["argv"][:2]) in github.READ_ONLY_VERBS, call["argv"]


# ------------------------------------------------------------------------ what it returns


def test_the_artifact_carries_the_body_the_diff_and_the_checks(artifact):
    """The three things a git diff cannot show and a human reviewer reads first."""
    art = github.pr_artifact(PR)
    assert art.title == "[wo-1] Do the thing"
    assert "it does the thing" in art.body
    assert art.diff == "diff --git a/a.py b/a.py\n@@\n+one\n"
    assert art.files == ("a.py",)
    assert art.additions == 3 and art.deletions == 1
    assert art.checks == ({"name": "tests", "status": "COMPLETED",
                           "conclusion": "SUCCESS"},)


def test_the_diff_comes_from_pr_diff_and_not_from_the_json(artifact):
    """Two round trips, and the second one is load-bearing: the `files` array carries
    paths and counts and never the patch text, so a collector reading only the JSON
    would hand every seat an empty diff with a plausible file list beside it."""
    github.pr_artifact(PR)
    verbs = [tuple(c["argv"][:2]) for c in artifact.calls]
    assert ("pr", "view") in verbs and ("pr", "diff") in verbs


def test_a_legacy_commit_status_is_read_as_a_check(fake_gh):
    """GitHub answers a check run with `conclusion` and a commit status with `state`,
    and a repository can carry both. Reading only the first renders a green CI as an
    empty conclusion, which a seat has to treat as "not known to pass"."""
    fake_gh.set_pr_artifact(PR, checks=[{"context": "ci/legacy", "state": "SUCCESS"}])
    art = github.pr_artifact(PR)
    assert art.checks == ({"name": "ci/legacy", "status": "SUCCESS",
                           "conclusion": "SUCCESS"},)


def test_no_checks_is_an_empty_tuple_and_not_an_error(fake_gh):
    fake_gh.set_pr_artifact(PR, diff="x")
    assert github.pr_artifact(PR).checks == ()


def test_the_stat_block_is_rebuilt_from_githubs_counts(fake_gh):
    """GitHub reports per-file counts and renders no stat; the panel prompt has always
    carried one, so the section keeps its shape whichever source the diff came from."""
    fake_gh.set_pr_artifact(PR, files=[{"path": "a.py", "additions": 2, "deletions": 0},
                                       {"path": "b.py", "additions": 0, "deletions": 5}])
    stat = github.pr_artifact(PR).stat
    assert "a.py" in stat and "b.py" in stat
    assert "2 file(s) changed, 2 insertion(s)(+), 5 deletion(s)(-)" in stat


def test_a_huge_file_does_not_put_a_four_thousand_character_line_in_the_stat(fake_gh):
    """Real `git diff --stat` scales its bars to the terminal and GitHub hands back raw
    counts, so an uncapped run is a prompt-sized line per file."""
    fake_gh.set_pr_artifact(PR, files=[{"path": "big.py", "additions": 4000,
                                        "deletions": 0}])
    assert max(len(l) for l in github.pr_artifact(PR).stat.splitlines()) < 120


# --------------------------------------------------------------------------- the failures


def test_an_unknown_pull_request_raises_rather_than_thinning(fake_gh):
    """Raising is the contract the collector depends on: it has to be able to tell "the
    submitter pointed at something unreadable" from "the submitter changed nothing"."""
    with pytest.raises(github.GitHubError):
        github.pr_artifact("https://github.com/x/y/pull/404")


def test_a_gh_that_fails_raises(artifact):
    artifact.fail("HTTP 502")
    with pytest.raises(github.GitHubError):
        github.pr_artifact(PR)


def test_a_missing_gh_binary_raises_the_type_that_names_the_remedy(artifact,
                                                                   monkeypatch):
    """`GhUnavailable` exists because a PATH problem and a credentials problem want
    opposite advice — see the class."""
    monkeypatch.setenv("JARVIS_GH_BIN", "/nonexistent/gh")
    with pytest.raises(github.GhUnavailable):
        github.pr_artifact(PR)


def test_the_poll_loop_and_the_panel_ask_for_different_fields(artifact):
    """The two readers are separate on purpose: the poll asks four questions per tick
    and must not pay for the body and the file list to answer them."""
    assert "body" not in github.PR_FIELDS
    assert "body" in github.ARTIFACT_FIELDS
    github.pr_view(PR)
    asked = artifact.calls[-1]["argv"]
    assert github.PR_FIELDS in asked and github.ARTIFACT_FIELDS not in asked
