"""Nothing verifies that a finished order's code ever landed (GitHub issue #232).

Two halves, and they are answered at completely different distances — see the module
docstring of `src/jarvis/landing.py`:

* `authored()`, the settle-time predicate, which reads a worktree and is exact;
* `branches_for()` + `judge()`, the audit, which names the branches a work order's code
  could be on and then judges the PULL REQUESTS somebody else asked GitHub about.

THE AUDIT NO LONGER MEASURES CONTENT, and the tests that did are gone with it. The user
narrowed INV-WORK-LANDED on 2026-09-18 after five of its seven live findings turned out
to be work that had merged months earlier and been refactored since; the coverage ladder,
the commit-subject rung, the merged-tail rung and the default-branch refresh they were
all measured against went with the verdict they produced.

NOTHING HERE FAKES GIT, for `tests/test_evidence.py`'s reason: `branches_for` is a
reading of what git actually says about refs, and a fake would only test the fake. What
GitHub says is passed IN to `judge`, so no test here needs a `gh` at all —
`tests/test_work_lands.py` drives the real discovery read through the fake CLI.

THE BRANCH NAME IS ALWAYS EXPLICIT, and it is `trunk` rather than `main` or `master`:
kn-4b6f18f5 is a whole CI-only failure caused by a git fixture inheriting
`init.defaultBranch`, and a fixture that starts depending on a default branch name
should fail on the author's machine rather than on somebody else's.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from jarvis import landing
from jarvis.github import BranchPullRequest
from jarvis.testing import make_git_project

WO = "wo-abc12345"


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def _feature(salt: str, n: int = 40) -> str:
    """A file body distinctive enough that finding its lines somewhere means something."""
    return "".join(f"def {salt}_helper_{i}(argument):  # {salt} feature line {i}\n"
                   f"    return compute_{salt}_result({i}, argument)\n"
                   for i in range(n))


def _pr(number: int, state: str) -> BranchPullRequest:
    """One pull request as `github.pr_list_for_branch` returns it."""
    return BranchPullRequest(number=number,
                             url=f"https://github.com/acme/proj/pull/{number}",
                             state=state)


@dataclass
class Repo:
    """A project repo on `trunk`, with a bare origin, and worktrees cut on demand."""

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
    # Rung 1 of the pinned ladder, built from local refs (kn-4b6f18f5): `set-head`
    # makes `origin/HEAD` a real symref without the fixture having to guess a name.
    _git(path, "remote", "set-head", "origin", "trunk")
    return Repo(path, origin)


# -- the settle-time predicate: has this worktree produced anything? --------------------


def test_an_order_that_produced_nothing_is_not_flagged_and_one_that_did_is(repo):
    """The pairing that decides whether the settle-time refusal survives the fleet.

    The planner half is the 60-of-89 case: a work order whose deliverable was a plan, a
    knowledge-base entry or an answer touches no file, so its worktree carries nothing
    over its base. The exclusion is DERIVED from that, never a list of work-order kinds
    — an investigation that does commit a script has produced something and must be
    refused like anything else.
    """
    planner = repo.worktree("wo-planner")
    coder = repo.worktree("wo-coder")
    repo.commit(coder, "src/feature.py", _feature("coder"))

    assert landing.authored(planner).produced is False
    assert landing.authored(coder).produced is True


def test_uncommitted_work_counts_as_produced_and_a_clean_tree_does_not(repo):
    """The tail issue #232 nearly lost: 851 lines that were never committed at all.

    wo-69a06ff4's code survived only because its worktree happened to still be on disk
    while 172 others were deleted the same day.
    """
    wt = repo.worktree(WO)
    assert landing.authored(wt).produced is False

    (wt / "src").mkdir()
    (wt / "src" / "never_added.py").write_text(_feature("orphan"))

    produced = landing.authored(wt)
    assert produced.produced is True
    assert produced.commits == 0          # nothing was committed...
    assert "src/never_added.py" in produced.dirty   # ...and it still counts


def test_a_missing_worktree_is_unreadable_not_produced(repo):
    """"I cannot look" must never be "there is unlanded work".

    A worktree is deleted when its branch is reclaimed. Reading that as evidence would
    refuse every `finish` on a machine that cleans up early — the failure mode that gets
    a check turned off within a day.
    """
    gone = landing.authored(repo.path / ".claude" / "worktrees" / "wo-vanished")

    assert gone.produced is False
    assert gone.unreadable == "no worktree on disk"


# -- the audit, half one: which branches could this work order's code be on? ------------


def test_every_branch_naming_the_order_is_found_including_after_the_worktree_is_gone(
        repo):
    """The audit runs months later, when every worktree has been reclaimed.

    BOTH branches, not the first. `_ref_for`, which this replaced, answered one match,
    and that was a defect the moment a work order used two: `wo-f1ce0f24` has
    `worktree-wo-f1ce0f24` (#225) and `worktree-wo-f1ce0f24-memory` (#226), and either
    alone reports on half the work. All three naming shapes this fleet has produced
    carry the work-order id, which is how the original audit found the six.
    """
    first = repo.worktree(WO)
    repo.commit(first, "src/a.py", _feature("a"))
    second = repo.worktree(f"{WO}-memory", branch=f"rescue/{WO}")
    repo.commit(second, "src/b.py", _feature("b"))
    _git(repo.path, "worktree", "remove", "--force", str(first))
    _git(repo.path, "worktree", "remove", "--force", str(second))

    assert sorted(landing.branches_for(repo.path, WO)) == [
        f"rescue/{WO}", f"worktree-{WO}"]


def test_a_remote_branch_is_named_as_github_names_it_and_not_twice(repo):
    """`gh pr list --head` wants `rescue/wo-x`, never `origin/rescue/wo-x`.

    And a branch that exists locally AND on the remote is ONE head as far as GitHub is
    concerned, so it must not be asked about twice — that is a round trip per sweep per
    order, paid to get the same answer.
    """
    wt = repo.worktree(WO, branch=f"rescue/{WO}")
    repo.commit(wt, "src/rescued.py", _feature("rescued"))
    _git(repo.path, "push", "-q", "origin", f"rescue/{WO}")

    assert landing.branches_for(repo.path, WO) == (f"rescue/{WO}",)


def test_an_order_with_no_branch_anywhere_names_nothing(repo):
    """Which reaches `judge` as `no-pull-request` — silent, and costing no `gh` call.

    The majority case on a mature project, and the reason discovery is affordable at all.
    """
    assert landing.branches_for(repo.path, "wo-neverexisted") == ()


# -- the audit, half two: what the pull requests say --------------------------------


def test_a_merged_pull_request_is_the_whole_answer():
    """Merged is the answer. No arithmetic, and nothing about the repository is read.

    The five false positives of 2026-09-18 were all this shape: work that merged and was
    refactored afterwards, which the content test scored at 44%, 31% and 72%.
    """
    found = landing.judge(WO, [_pr(42, "MERGED")], [f"worktree-{WO}"])

    assert found.verdict == landing.LANDED
    assert found.unsettled is False
    assert found.pr_url.endswith("/pull/42")


def test_an_open_pull_request_says_the_work_is_waiting_on_a_merge():
    """A violation, but not a STRANDED one — the word the user struck out.

    Delivered work sitting in an open pull request needs a merge, not a rescue, and a
    report that says "its code is not on main" sends the reader hunting for a branch.
    """
    found = landing.judge(WO, [_pr(7, "OPEN")], [f"worktree-{WO}"])

    assert found.verdict == landing.AWAITING_MERGE
    assert found.unsettled is True
    assert "waiting on a merge" in found.detail


def test_a_pull_request_closed_unmerged_is_delivered_and_refused():
    """wo-69a06ff4's shape: PR #231, closed, titled "superseded, not for merge".

    A decision somebody took on GitHub, where the work order cannot see it — so the order
    is still claiming `completed` over work nothing landed.
    """
    found = landing.judge(WO, [_pr(231, "CLOSED")], [f"rescue/{WO}"])

    assert found.verdict == landing.REFUSED
    assert found.unsettled is True
    assert "refused" in found.detail


def test_no_pull_request_at_all_is_out_of_scope_and_silent():
    """THE NARROWING, stated as a test. wo-5a6b2d6d is the case it gives up.

    Its only product is a WIP commit on `rescue/wo-5a6b2d6d` with no pull request, and
    under this rule nothing here reports it. The user accepts that: the place to catch an
    order settling with nothing delivered is the validation round that let it settle.
    """
    found = landing.judge(WO, [], [f"rescue/{WO}"])

    assert found.verdict == landing.NO_PULL_REQUEST
    assert found.unsettled is False


def test_an_open_pull_request_outranks_an_earlier_merge():
    """wo-cd73c537 EXACTLY: #81 merged, then #116 opened on the same branch.

    The order's recorded `pr_url` names #81 and the old check called it stranded off a
    content score of 2%. The truth is both simpler and different: there is an open pull
    request carrying the rest of the work, and that is what the report must say.

    Open BEATS merged rather than "newest wins", because those two rules disagree on the
    other arrangement — an order that merged and then had a follow-up opened and closed —
    and only one of them gets it right. The next test is that one.
    """
    found = landing.judge(WO, [_pr(81, "MERGED"), _pr(116, "OPEN")], [f"worktree-{WO}"])

    assert found.verdict == landing.AWAITING_MERGE
    assert found.pr_url.endswith("/pull/116")
    assert found.detail.endswith("(#116 OPEN, #81 MERGED)")   # newest first, both shown


def test_a_later_closed_pull_request_does_not_un_land_a_merge():
    """The arrangement "newest wins" would get wrong, and it is not hypothetical:
    a follow-up opened against a branch and then abandoned is an ordinary week.

    The work landed. A closed pull request is not evidence against a merge that happened.
    """
    found = landing.judge(WO, [_pr(42, "MERGED"), _pr(58, "CLOSED")], [f"worktree-{WO}"])

    assert found.verdict == landing.LANDED
    assert found.pr_url.endswith("/pull/42")


def test_a_verdict_survives_the_round_trip_through_an_event_payload():
    """`record()` -> JSON -> `from_record()`. The producer and the consumer are joined by
    dict keys, and a key that did not match would make INV-WORK-LANDED silently, and
    permanently, quiet — indistinguishable from a fleet with nothing unmerged."""
    import json

    original = landing.judge(WO, [_pr(81, "MERGED"), _pr(116, "OPEN")],
                             [f"worktree-{WO}"])

    read_back = landing.from_record(WO, json.loads(json.dumps(original.record())))

    assert read_back == original


def test_an_unreadable_discovery_record_is_silent_and_never_a_complaint():
    """An event written by some other release is a shape nobody can change afterwards.

    Silence is the only safe answer: this audit may say nothing, and may never make a
    complaint it cannot substantiate.
    """
    found = landing.from_record(WO, {"verdict": "coverage-was-0.02"})

    assert found.verdict == landing.NO_PULL_REQUEST
    assert found.unsettled is False


# -- Mode A: the pull request named in prose and not passed as --pr --------------------


def test_a_pr_url_is_recognised_in_prose_and_ordinary_prose_yields_none():
    """Four of the six stranded orders finished with a summary naming a draft PR.

    Two were recovered only because the user personally noticed and filed work orders
    titled "circle back on this PR .../pull/33".
    """
    found = landing.pr_urls_in(
        "Opened a draft at https://github.com/acme/proj/pull/33. "
        "See also https://github.com/acme/proj/pull/33 and the issue "
        "https://github.com/acme/proj/issues/99.")

    assert found == ("https://github.com/acme/proj/pull/33",)   # deduped, no issue URL
    assert landing.pr_urls_in("Answered the question; no code was needed.") == ()
    assert landing.pr_urls_in("") == ()
