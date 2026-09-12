"""The packet when the artifact is a PULL REQUEST, and when it is a side effect.

Issue #200, docs/superpowers/specs/2026-09-12-the-pull-request-is-the-artifact.md.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from jarvis import evidence
from jarvis.testing import make_git_project

PR = "https://github.com/x/y/pull/12"
WO = {"id": "wo-1", "title": "Title", "description": "The brief",
      "result_summary": "the summary", "pr_url": "", "worktree": "wt"}

PR_DIFF = "diff --git a/from_pr.py b/from_pr.py\n@@ -0,0 +1 @@\n+from the pull request\n"


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


@pytest.fixture()
def project(tmp_path) -> Path:
    """A project with one worker worktree holding a change of its OWN.

    The worktree's change is deliberately different from the pull request's, so every
    test below can say WHICH source a packet came from by looking at the file names
    rather than by trusting `packet.source` to be set correctly.
    """
    proj = make_git_project(tmp_path, "proj")
    (proj / "app.py").write_text("app\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "base")
    worktree = proj / ".claude" / "worktrees" / "wt"
    _git(proj, "worktree", "add", "-q", "-b", "wo-branch", str(worktree))
    (worktree / "from_worktree.py").write_text("from the worktree\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "work")
    return proj


def _registered(fake_gh, **over):
    fake_gh.set_pr_artifact(PR, **{
        "title": "[wo-1] Do the thing", "body": "## Summary\nit does the thing",
        "diff": PR_DIFF,
        "files": [{"path": "from_pr.py", "additions": 1, "deletions": 0}],
        "checks": [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        **over})


def collect(project, *, declared="ran the tests", **over):
    wo = {**WO, **over}
    chars = wo.pop("diff_chars", evidence.DEFAULT_DIFF_CHARS)
    effects = wo.pop("side_effects", ())
    return evidence.collect_work_order(project, wo, declared=declared,
                                       diff_chars=chars, side_effects=effects)


# ------------------------------------------------------------ the pull request wins


def test_a_work_order_with_a_pull_request_is_judged_on_the_pull_request(project,
                                                                        fake_gh):
    """Spec §3, row 1 — and the file names prove it rather than the `source` field."""
    _registered(fake_gh)
    packet = collect(project, pr_url=PR)
    assert packet.source == "pull_request"
    assert packet.files == ("from_pr.py",)
    assert "from the pull request" in packet.diff
    assert "from_worktree.py" not in packet.diff


def test_the_packet_carries_what_a_reviewer_reads_and_a_diff_cannot_show(project,
                                                                         fake_gh):
    _registered(fake_gh)
    pr = collect(project, pr_url=PR).pr
    assert pr is not None
    assert pr["title"] == "[wo-1] Do the thing"
    assert "it does the thing" in pr["body"]
    assert pr["checks"] == [{"name": "tests", "status": "COMPLETED",
                             "conclusion": "SUCCESS"}]
    assert pr["base_ref"] and pr["head_ref"]


def test_the_packet_never_carries_the_untruncated_diff_in_the_pr_field(project,
                                                                       fake_gh):
    """kn-c8b9c7da's rejected `full_diff` option, arriving by a new door: the artifact
    holds an untruncated diff, and a packet field holding a second copy of it would be
    shipped by the first `asdict()` that builds a seat prompt, defeating `diff_chars`
    silently."""
    _registered(fake_gh, diff=PR_DIFF * 200)
    packet = collect(project, pr_url=PR, diff_chars=200)
    assert packet.diff_truncated
    assert packet.pr is not None and "diff" not in packet.pr
    assert len(str(packet.pr)) < 2000


def test_a_pull_request_with_no_checks_says_so_rather_than_carrying_nothing(project,
                                                                            fake_gh):
    _registered(fake_gh, checks=[])
    pr = collect(project, pr_url=PR).pr
    assert pr is not None and pr["checks"] == []


# ------------------------------------------------------ a pull request that cannot be read


def test_an_unreadable_pull_request_falls_back_to_the_worktree_and_says_so(project,
                                                                           fake_gh):
    """Spec §3, row 2. The fallback is right; doing it SILENTLY is not — a seat told
    neither would judge the worktree believing it was the artifact."""
    packet = collect(project, pr_url=PR)  # never registered with the fake
    assert packet.source == "worktree"
    assert packet.pr is None
    assert packet.pr_error
    assert packet.files == ("from_worktree.py",)


def test_a_gh_that_is_down_never_takes_the_round_with_it(project, fake_gh):
    _registered(fake_gh)
    fake_gh.fail("HTTP 502 bad gateway")
    packet = collect(project, pr_url=PR)
    assert packet.source == "worktree"
    assert "502" in packet.pr_error


def test_a_missing_gh_binary_is_a_thin_packet_and_not_an_exception(project,
                                                                   monkeypatch):
    monkeypatch.setenv("JARVIS_GH_BIN", "/nonexistent/gh")
    packet = collect(project, pr_url=PR)
    assert packet.source == "worktree" and packet.pr_error


def test_no_pull_request_collects_the_worktree_exactly_as_before(project, fake_gh):
    """Spec §3, row 3, and the negative control for the whole feature: a work order
    with no pull request must not have changed shape at all."""
    packet = collect(project)
    assert packet.source == "worktree"
    assert packet.pr is None and packet.pr_error == ""
    assert packet.files == ("from_worktree.py",)


def test_a_missing_worktree_still_never_falls_back_to_the_project_root(tmp_path,
                                                                       fake_gh):
    """The rule that predates all of this, re-asserted on the new branch structure:
    the project root is the USER's checkout and whatever is open in it is not evidence."""
    proj = make_git_project(tmp_path, "proj")
    (proj / "loose.py").write_text("the user's own uncommitted work\n")
    packet = evidence.collect_work_order(proj, {**WO, "worktree": "gone"},
                                         declared="")
    assert packet.files == () and packet.diff == ""


# ------------------------------------------------------------------------ side effects


EFFECT = {"kind": "knowledge_retracted", "id": "kn-1",
          "summary": "retired kn-1: it told every worker the wrong path",
          "detail": "always call /snap/bin/gh"}


def test_a_submission_with_no_files_but_a_side_effect_is_not_empty(project, fake_gh):
    """Issue #200's live case: wo-28405ea1 retracted a fleet-wide instruction and the
    packet said it had changed nothing."""
    packet = evidence.collect_work_order(
        project, {**WO, "worktree": "gone"}, declared="", side_effects=[EFFECT])
    assert packet.files == ()
    assert packet.side_effects == (EFFECT,)


def test_side_effects_ride_alongside_a_pull_request(project, fake_gh):
    """Not either/or: a work order can ship code AND retract an entry, which is exactly
    what the work order that found this bug did."""
    _registered(fake_gh)
    packet = collect(project, pr_url=PR, side_effects=[EFFECT])
    assert packet.source == "pull_request" and packet.side_effects == (EFFECT,)


# ------------------------------------------------------------------------ the fingerprint


def _fp(project, **over):
    return evidence.fingerprint(collect(project, **over))


def test_two_rounds_retracting_different_entries_do_not_fingerprint_alike(project):
    """The guard one step past the one issue #200 named. Without this the empty-diff
    fix just moves the false escalation to `_preceding_round` (spec §5)."""
    one = _fp(project, worktree="gone", side_effects=[EFFECT])
    two = _fp(project, worktree="gone",
              side_effects=[{**EFFECT, "id": "kn-2", "summary": "retired kn-2"}])
    assert one != two


def test_the_same_side_effect_twice_fingerprints_identically(project):
    """The other half: resubmitting the identical thing must still be caught."""
    assert _fp(project, worktree="gone", side_effects=[EFFECT]) == \
           _fp(project, worktree="gone", side_effects=[EFFECT])


def test_no_side_effects_fingerprints_exactly_as_it_did_before_the_field_existed(
        project):
    """`side_effects_digest(())` is "" and NOT the sha of the empty string. Every packet
    collected before this field existed carries "", so giving "nothing" a non-empty
    digest would change every stored fingerprint and make the next round of every open
    work order read as new evidence."""
    assert evidence.side_effects_digest(()) == ""
    assert _fp(project) == _fp(project, side_effects=[])


def test_the_exclusion_list_is_unchanged(project, fake_gh):
    """Neo's question-133 ruling narrowed, not widened: `side_effects_sha` joined the
    hash and `head`, `base`, `summary` and `pr_url` stayed out (question 253)."""
    _registered(fake_gh)
    base = _fp(project, pr_url=PR)
    assert base == _fp(project, pr_url=PR, result_summary="a completely different story")
    assert base == _fp(project, pr_url=PR, title="renamed")


def test_the_side_effect_digest_does_not_depend_on_key_order():
    a = {"kind": "knowledge_added", "id": "kn-1", "detail": "x"}
    b = {"detail": "x", "id": "kn-1", "kind": "knowledge_added"}
    assert evidence.side_effects_digest([a]) == evidence.side_effects_digest([b])
