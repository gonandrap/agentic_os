"""Nothing verifies that a finished order's code ever landed (GitHub issue #232)"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from jarvis import landing, project_store
from jarvis.testing import make_git_project

WO = "wo-abc12345"
PR = "https://github.com/acme/proj/pull/42"


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def _feature(salt: str, n: int = 40) -> str:
    """A file body distinctive enough that finding its lines somewhere means something"""
    return "".join(f"def {salt}_helper_{i}(argument):  # {salt} feature line {i}\n"
                   f"    return compute_{salt}_result({i}, argument)\n"
                   for i in range(n))


@dataclass
class Repo:
    """A project repo on `trunk`, with a bare origin, and worktrees cut on demand"""

    path: Path
    origin: Path

    def worktree(self, wo_id: str, branch: str | None = None) -> Path:
        wt = self.path / ".claude" / "worktrees" / wo_id
        _git(self.path, "worktree", "add", "-q", "-b", branch or f"worktree-{wo_id}",
             str(wt), "trunk")
        return wt

    def commit(self, wt: Path, name: str, body: str, message: str = "work") -> None:
        (wt / name).parent.mkdir(parents=True, exist_ok=True)
        (wt / name).write_text(body)
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", message)

    def push(self) -> None:
        _git(self.path, "push", "-q", "origin", "trunk")


@pytest.fixture()
def repo(tmp_path) -> Repo:
    path = make_git_project(tmp_path, "proj")
    _git(path, "symbolic-ref", "HEAD", "refs/heads/trunk")
    (path / "app.py").write_text(_feature("base", 20))
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", str(origin))
    _git(path, "remote", "add", "origin", str(origin))
    _git(path, "push", "-q", "origin", "trunk")
    # Rung 1 of the pinned ladder, built from local refs (kn-4b6f18f5): `set-head` makes `origin/HEAD` a...
    _git(path, "remote", "set-head", "origin", "trunk")
    return Repo(path, origin)


# -- the settle-time predicate: has this worktree produced anything?


def test_an_order_that_produced_nothing_is_not_flagged_and_one_that_did_is(repo):
    """The pairing that decides whether the settle-time refusal survives the fleet"""
    planner = repo.worktree("wo-planner")
    coder = repo.worktree("wo-coder")
    repo.commit(coder, "src/feature.py", _feature("coder"))

    assert landing.authored(planner).produced is False
    assert landing.authored(coder).produced is True


def test_uncommitted_work_counts_as_produced_and_a_clean_tree_does_not(repo):
    """The tail issue #232 nearly lost: 851 lines that were never committed at all"""
    wt = repo.worktree(WO)
    assert landing.authored(wt).produced is False

    (wt / "src").mkdir()
    (wt / "src" / "never_added.py").write_text(_feature("orphan"))

    produced = landing.authored(wt)
    assert produced.produced is True
    assert produced.commits == 0          # nothing was committed...
    assert "src/never_added.py" in produced.dirty   # ...and it still counts


@pytest.mark.parametrize("kind", project_store.WO_KINDS)
def test_a_kinds_own_artifact_is_never_stranded_work(repo, kind):
    """io-b521d17b: each worktree built WITH the file its kind's contract command consumes"""
    wo = {"id": f"wo-{kind}", "kind": kind}
    artifacts = project_store.submission_artifacts(wo)
    wt = repo.worktree(wo["id"])
    for name in artifacts:
        (wt / name).write_text("{}")
        # The collision the table exists to close: unexcluded, the artifact reads as stranded code
        assert landing.authored(wt).produced is True
    assert landing.authored(wt, artifacts).produced is False, kind
    # …and only the artifact: anything else it wrote still counts
    (wt / "notes.txt").write_text("x")
    assert landing.authored(wt, artifacts).dirty == ("notes.txt",), kind


def test_a_missing_worktree_is_unreadable_not_produced(repo):
    """"I cannot look" must never be "there is unlanded work\""""
    gone = landing.authored(repo.path / ".claude" / "worktrees" / "wo-vanished")

    assert gone.produced is False
    assert gone.unreadable == "no worktree on disk"


# -- a pushed branch with no pull request is a state (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md)


def _push(repo: Repo, wt: Path, branch: str) -> None:
    """What the worker does: push the branch, which writes `refs/remotes/origin/…`."""
    _git(wt, "push", "-q", "origin", branch)


def test_a_branch_nobody_pushed_is_not_pushed(repo):
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("coder"))

    work = landing.authored(wt)

    assert work.produced is True
    assert work.pushed is False


def test_a_branch_whose_every_commit_is_on_the_remote_is_pushed(repo):
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("coder"))
    _push(repo, wt, f"worktree-{WO}")

    work = landing.authored(wt)

    assert work.produced is True
    assert work.pushed is True


def test_a_pushed_branch_records_the_sha_the_remote_carries(repo):
    """The auto-attach matches a pull request's head against this SHA"""
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("coder"))
    _push(repo, wt, f"worktree-{WO}")

    work = landing.authored(wt)

    assert work.head_sha == _git(wt, "rev-parse", f"refs/remotes/origin/worktree-{WO}").strip()
    assert work.record()["head_sha"] == work.head_sha
    assert landing.authored_in(work.record()).head_sha == work.head_sha


def test_an_unpushed_branch_records_no_sha(repo):
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("coder"))

    assert landing.authored(wt).head_sha == ""


def test_a_remote_behind_the_branch_is_not_pushed(repo):
    """One commit made after the push. The remote does NOT carry every commit HEAD has."""
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("coder"))
    _push(repo, wt, f"worktree-{WO}")
    repo.commit(wt, "src/more.py", _feature("more"), message="more")

    assert landing.authored(wt).pushed is False


def test_uncommitted_files_are_never_pushed(repo):
    """§1"""
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("coder"))
    _push(repo, wt, f"worktree-{WO}")
    (wt / "src" / "never_added.py").write_text(_feature("orphan"))

    work = landing.authored(wt)

    assert work.dirty
    assert work.pushed is False


def test_the_fixtures_local_origin_yields_no_compare_url_and_still_reads_as_pushed(repo):
    """THE REGRESSION TEST FOR §3's HOST CHECK, and the reason it is this fixture"""
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("coder"))
    _push(repo, wt, f"worktree-{WO}")

    work = landing.authored(wt)

    assert work.pushed is True
    assert work.compare_url == ""


def test_the_record_round_trips_both_new_facts(repo):
    """`record()` -> JSON -> `authored_in()`"""
    import json

    original = landing.Authored(branch="worktree-wo-abc12345", base="origin/trunk",
                                commits=2, dirty=(), pushed=True,
                                compare_url="https://github.com/acme/proj/compare/"
                                            "trunk...worktree-wo-abc12345?expand=1")

    read_back = landing.authored_in(json.loads(json.dumps(original.record())))

    assert read_back == original


def test_an_event_written_before_this_shipped_reads_as_not_pushed():
    """§3's backward compatibility, by construction: not-pushed is the safe direction."""
    legacy = landing.authored_in({"branch": "worktree-wo-abc12345",
                                  "base": "origin/trunk", "commits": 1, "dirty": []})

    assert legacy is not None
    assert legacy.pushed is False
    assert legacy.compare_url == ""


# -- the compare URL itself: local git only, and github.com only


def _repo_with_origin(tmp_path: Path, remote: str) -> Path:
    path = make_git_project(tmp_path, "withorigin")
    _git(path, "remote", "add", "origin", remote)
    return path


def test_the_compare_url_is_built_from_the_origin_and_the_stripped_base(tmp_path):
    from jarvis import github

    path = _repo_with_origin(tmp_path, "https://github.com/Acme/Proj.git")

    assert github.compare_url(path, "origin/trunk", "worktree-wo-abc12345") == (
        "https://github.com/acme/proj/compare/trunk...worktree-wo-abc12345?expand=1")


def test_a_non_github_origin_has_no_compare_url(tmp_path):
    """§3"""
    from jarvis import github

    path = _repo_with_origin(tmp_path, "git@gitlab.com:acme/proj.git")

    assert github.compare_url(path, "origin/main", "worktree-wo-abc12345") is None


def test_an_unreadable_origin_has_no_compare_url(tmp_path):
    from jarvis import github

    path = make_git_project(tmp_path, "noorigin")

    assert github.compare_url(path, "origin/main", "b") is None
    assert github.compare_url(tmp_path / "nowhere", "origin/main", "b") is None


# -- the audit: what GitHub says about the pull request the order recorded


def test_a_merged_pull_request_is_the_whole_answer():
    """Merged is the answer"""
    found = landing.judge(WO, PR, "MERGED")

    assert found.verdict == landing.LANDED
    assert found.unsettled is False
    assert found.pr_url == PR


def test_an_open_pull_request_says_the_work_is_waiting_on_a_merge():
    """A violation, but not a STRANDED one — the word the user struck out"""
    found = landing.judge(WO, PR, "OPEN")

    assert found.verdict == landing.AWAITING_MERGE
    assert found.unsettled is True
    assert "waiting on a merge" in found.detail


def test_a_pull_request_closed_unmerged_is_delivered_and_refused():
    """wo-69a06ff4's shape: pull request #231, closed, "superseded, not for merge\""""
    found = landing.judge(WO, PR, "CLOSED")

    assert found.verdict == landing.REFUSED
    assert found.unsettled is True
    assert "refused" in found.detail


def test_no_recorded_pull_request_is_out_of_scope_and_silent():
    """THE NARROWING, stated as a test, and the layering with it"""
    found = landing.judge(WO, "", "")

    assert found.verdict == landing.NO_PULL_REQUEST
    assert found.unsettled is False


def test_a_state_github_has_never_answered_is_reported_rather_than_swallowed():
    """A state this module does not recognise is a reason to LOOK, not to fall silent"""
    found = landing.judge(WO, PR, "SOMETHING_NEW")

    assert found.unsettled is True
    assert found.pr_state == "SOMETHING_NEW"


def test_a_verdict_survives_the_round_trip_through_an_event_payload():
    """`record()` -> JSON -> `from_record()`"""
    import json

    original = landing.judge(WO, PR, "OPEN")

    read_back = landing.from_record(WO, json.loads(json.dumps(original.record())))

    assert read_back == original


def test_an_unreadable_record_is_silent_rather_than_a_complaint():
    """An event written by a release that spelled the payload differently"""
    assert landing.from_record(WO, {"nonsense": 1}).verdict == landing.NO_PULL_REQUEST
    assert landing.from_record(WO, {}).unsettled is False


# -- credentials: git quotes the remote URL back at you, and this module logs its stderr


def test_the_authentication_failure_git_does_not_redact_is_scrubbed_verbatim():
    """THE HALF THAT ACTUALLY MATTERS, and it is a unit assertion for a reason"""
    secret = "ghp_FAKETOKEN0123456789abcdefghijklmnop"
    raw = (f"fatal: Authentication failed for "
           f"'https://x-access-token:{secret}@github.com/acme/proj.git/'")

    scrubbed = landing._scrub(raw)

    assert secret not in scrubbed
    assert "x-access-token" not in scrubbed
    assert secret not in scrubbed[:200]
    assert "github.com/acme/proj.git" in scrubbed   # still says WHICH remote refused


def test_no_git_failure_this_module_logs_can_carry_a_credential(repo, caplog):
    """The guard is on `_git`, not on today's callers, and that is the point"""
    secret = "ghp_FAKETOKEN0123456789abcdefghijklmnop"
    # Port 1 rather than 443 so the connection fails at once instead of after a timeout
    _git(repo.path, "remote", "set-url", "origin",
         f"https://x-access-token:{secret}@127.0.0.1:1/acme/proj.git")

    with caplog.at_level("DEBUG", logger="jarvis.landing"):
        assert landing._git(repo.path, "ls-remote", "origin") is None

    assert caplog.records, "a failure that logs nothing proves nothing"
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert "x-access-token" not in record.getMessage()
