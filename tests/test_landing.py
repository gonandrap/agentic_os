"""Nothing verifies that a finished order's code ever landed (GitHub issue #232).

Two halves, and they are answered at completely different distances — see the module
docstring of `src/jarvis/landing.py`:

* `authored()`, the settle-time predicate, which reads a worktree and is exact;
* `judge()`, the audit, which says what one pull request's state means for a work order
  that has already claimed `completed`.

THE AUDIT NO LONGER MEASURES CONTENT, and the tests that did are gone with it. The user
narrowed INV-WORK-LANDED on 2026-09-18 after five of its seven live findings turned out
to be work that had merged months earlier and been refactored since; the coverage ladder,
the commit-subject rung, the merged-tail rung and the default-branch refresh they were
all measured against went with the verdict they produced.

NOR DOES IT GO LOOKING FOR A PULL REQUEST. It judges the one the order recorded, on the
user's second ruling the same day, and an order with no `pr_url` is INV-PR-RECORDED's
(work order `wo-2005a89b`). So `judge` takes a url and a state and is pure: no git, no
`gh`, no store. `tests/test_work_lands.py` drives the real `gh` read through the fake CLI
and joins the two halves end to end.

NOTHING HERE FAKES GIT, for `tests/test_evidence.py`'s reason: `authored` is a reading of
what git actually says, and a fake would only test the fake.

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
    """A file body distinctive enough that finding its lines somewhere means something."""
    return "".join(f"def {salt}_helper_{i}(argument):  # {salt} feature line {i}\n"
                   f"    return compute_{salt}_result({i}, argument)\n"
                   for i in range(n))


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


# -- the audit: what GitHub says about the pull request the order recorded --------------


def test_a_merged_pull_request_is_the_whole_answer():
    """Merged is the answer. No arithmetic, and nothing about the repository is read.

    The five false positives of 2026-09-18 were all this shape: work that merged and was
    refactored afterwards, which the content test scored at 44%, 31% and 72%.
    """
    found = landing.judge(WO, PR, "MERGED")

    assert found.verdict == landing.LANDED
    assert found.unsettled is False
    assert found.pr_url == PR


def test_an_open_pull_request_says_the_work_is_waiting_on_a_merge():
    """A violation, but not a STRANDED one — the word the user struck out.

    Delivered work sitting in an open pull request needs a merge, not a rescue, and a
    report that says "its code is not on main" sends the reader hunting for a branch.
    """
    found = landing.judge(WO, PR, "OPEN")

    assert found.verdict == landing.AWAITING_MERGE
    assert found.unsettled is True
    assert "waiting on a merge" in found.detail


def test_a_pull_request_closed_unmerged_is_delivered_and_refused():
    """wo-69a06ff4's shape: pull request #231, closed, "superseded, not for merge".

    A decision somebody took on GitHub, where the work order cannot see it — so the order
    is still claiming `completed` over work nothing landed.
    """
    found = landing.judge(WO, PR, "CLOSED")

    assert found.verdict == landing.REFUSED
    assert found.unsettled is True
    assert "refused" in found.detail


def test_no_recorded_pull_request_is_out_of_scope_and_silent():
    """THE NARROWING, stated as a test, and the layering with it.

    An order with no `pr_url` is INV-PR-RECORDED's (wo-2005a89b), which refuses to let an
    order that changed code settle without one. This half says nothing rather than
    guessing — including about wo-5a6b2d6d, whose only product is a WIP commit on
    `rescue/wo-5a6b2d6d`, and which the user accepts giving up here.
    """
    found = landing.judge(WO, "", "")

    assert found.verdict == landing.NO_PULL_REQUEST
    assert found.unsettled is False


def test_a_state_github_has_never_answered_is_reported_rather_than_swallowed():
    """A state this module does not recognise is a reason to LOOK, not to fall silent.

    The silent verdict is reserved for "there is no pull request", which is a fact about
    the work order. An unrecognised state would otherwise exempt it for ever, invisibly.
    """
    found = landing.judge(WO, PR, "SOMETHING_NEW")

    assert found.unsettled is True
    assert found.pr_state == "SOMETHING_NEW"


def test_a_verdict_survives_the_round_trip_through_an_event_payload():
    """`record()` -> JSON -> `from_record()`. The producer and the consumer are joined by
    dict keys, and a key that did not match would make INV-WORK-LANDED silently, and
    permanently, quiet — indistinguishable from a fleet with nothing unmerged."""
    import json

    original = landing.judge(WO, PR, "OPEN")

    read_back = landing.from_record(WO, json.loads(json.dumps(original.record())))

    assert read_back == original


def test_an_unreadable_record_is_silent_rather_than_a_complaint():
    """An event written by a release that spelled the payload differently.

    It errs towards saying nothing: a hygiene sweep that invents a violation out of a
    shape it cannot parse is worse than one that waits for the next refresh.
    """
    assert landing.from_record(WO, {"nonsense": 1}).verdict == landing.NO_PULL_REQUEST
    assert landing.from_record(WO, {}).unsettled is False


# -- credentials: git quotes the remote URL back at you, and this module logs its stderr -


def test_the_authentication_failure_git_does_not_redact_is_scrubbed_verbatim():
    """THE HALF THAT ACTUALLY MATTERS, and it is a unit assertion for a reason.

    git SELF-redacts the userinfo on a CONNECTION failure and does NOT on an
    AUTHENTICATION failure — and an authentication failure cannot be provoked offline,
    because it needs a server that refuses a login. So an end-to-end test alone would
    pass against code that logged stderr raw. The string below is git's, quoted rather
    than invented (kn-4bba177d, restored by review round 1 of wo-16a488ee).

    Truncation is not a defence: the URL is on the FIRST line, so `stderr[:200]` keeps
    the token. `_git` therefore scrubs BEFORE it slices.
    """
    secret = "ghp_FAKETOKEN0123456789abcdefghijklmnop"
    raw = (f"fatal: Authentication failed for "
           f"'https://x-access-token:{secret}@github.com/acme/proj.git/'")

    scrubbed = landing._scrub(raw)

    assert secret not in scrubbed
    assert "x-access-token" not in scrubbed
    assert secret not in scrubbed[:200]
    assert "github.com/acme/proj.git" in scrubbed   # still says WHICH remote refused


def test_no_git_failure_this_module_logs_can_carry_a_credential(repo, caplog):
    """The guard is on `_git`, not on today's callers, and that is the point.

    `landing._fetch` — the one caller that touched a remote — was deleted on 2026-09-18
    with the rest of the content machinery, and with it went the fleet's only credential
    scrub and this assertion. Nothing in this module reaches the network today. The next
    thing that does will go through `_git` and will not think about it, so the test drives
    `_git` with a remote-touching command directly rather than through a caller that
    happens to exist this month.

    Captured at DEBUG so that "appears in no emitted record" means ANY record.
    """
    secret = "ghp_FAKETOKEN0123456789abcdefghijklmnop"
    # Port 1 rather than 443 so the connection fails at once instead of after a timeout.
    _git(repo.path, "remote", "set-url", "origin",
         f"https://x-access-token:{secret}@127.0.0.1:1/acme/proj.git")

    with caplog.at_level("DEBUG", logger="jarvis.landing"):
        assert landing._git(repo.path, "ls-remote", "origin") is None

    assert caplog.records, "a failure that logs nothing proves nothing"
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert "x-access-token" not in record.getMessage()
