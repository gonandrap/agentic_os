"""Nothing verifies that a finished order's code ever landed (GitHub issue #232).

One test per failure mode the audit found, each with the negative control beside it,
because the negative control is the half that rots (kn-67364b3a) and the half that
decides whether anyone leaves this check switched on. The audit found 60 work orders
among 89 candidates that legitimately produced no code — planners whose deliverable was
a plan, knowledge-base writes, investigations, releases — so a check without that
exclusion has a 67% false-positive rate.

NOTHING HERE FAKES GIT, for `tests/test_evidence.py`'s reason: the whole module is a
reading of what git actually says, and a fake would only test the fake. In particular
`squash_merge` is a real `git merge --squash`, because a squash is what makes commit
reachability useless as a landed-test and a fixture that fast-forwarded instead would
quietly make the naive implementation pass.

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


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def _feature(salt: str, n: int = 40) -> str:
    """A file body distinctive enough that finding its lines somewhere means something.

    Every line is over `landing.SIGNIFICANT_CHARS` and carries `salt`, so a branch's
    lines cannot be confused with the base file's or with another branch's.
    """
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

    def squash_merge(self, branch: str, subject: str) -> str:
        """What GitHub does to this repository: one new commit, a brand-new sha.

        Returns the sha of the branch tip that was merged — GitHub's `headRefOid`, and
        what Mode C's exact test measures a tail against.
        """
        head = _git(self.path, "rev-parse", branch).strip()
        _git(self.path, "merge", "--squash", "-q", branch)
        _git(self.path, "commit", "-qm", subject)
        self.push()
        return head

    def push(self) -> None:
        _git(self.path, "push", "-q", "origin", "trunk")


def _assess(repo: Repo, wo_id: str = WO, **kw) -> landing.Landing:
    """`landing.assess` against a default branch this fixture has just pushed to.

    `base_current=True` is a FACT about the fixture and not a convenience: `Repo.push`
    updates `origin/trunk` in this clone, so the ref every test below measures against
    holds everything the bare origin does. Issue #271 is what happens when that is NOT
    true in production and nobody says so — the two tests at the end of this file are
    the ones that pass `base_current=False`.
    """
    return landing.assess(repo.path, wo_id, base_current=True, **kw)


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


# -- the exclusion: an order that produced no code is never flagged ---------------------


def test_an_order_that_produced_nothing_is_not_flagged_and_one_that_did_is(repo):
    """The pairing that decides whether this check survives contact with the fleet.

    The planner half is the 60-of-89 case: a work order whose deliverable was a plan, a
    knowledge-base entry or an answer touches no file, so its worktree carries nothing
    over its base. The exclusion is DERIVED from that, never a list of work-order kinds
    — an investigation that does commit a script has produced something and must be
    flagged like anything else.
    """
    planner = repo.worktree("wo-planner")
    coder = repo.worktree("wo-coder")
    repo.commit(coder, "src/feature.py", _feature("coder"))

    assert landing.authored(planner).produced is False
    assert landing.authored(coder).produced is True
    assert _assess(repo, "wo-planner",
                          worktree=planner).verdict == landing.NOT_PRODUCED
    assert _assess(repo, "wo-coder",
                          worktree=coder).verdict == landing.STRANDED


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


# -- the landed test, against a branch that DID land and one that did not --------------


def test_a_squash_merged_branch_reads_as_landed_and_an_unmerged_one_as_stranded(repo):
    """THE test the whole module stands or falls on.

    The repository squash-merges, so the landed branch's OWN commits are not reachable
    from `trunk` and its ahead-count is non-zero — identical, by those measures, to the
    branch that never landed. Only the content test tells them apart.
    """
    landed_wt = repo.worktree("wo-landed")
    repo.commit(landed_wt, "src/landed.py", _feature("landed"))
    stranded_wt = repo.worktree("wo-strand")
    repo.commit(stranded_wt, "src/strand.py", _feature("strand"))
    repo.squash_merge("worktree-wo-landed", "[wo-landed] the feature (#1)")

    # The naive tests cannot tell these apart: assert that, so a later change that
    # reaches for one of them fails here with the reason written down.
    for branch in ("worktree-wo-landed", "worktree-wo-strand"):
        ahead = _git(repo.path, "rev-list", "--count", f"trunk..{branch}").strip()
        assert ahead == "1", f"{branch} is ahead of trunk even though it landed"

    good = _assess(repo, "wo-landed", worktree=landed_wt)
    bad = _assess(repo, "wo-strand", worktree=stranded_wt)

    assert good.verdict == landing.LANDED, good.detail
    assert good.coverage >= landing.LANDED_COVERAGE
    assert bad.verdict == landing.STRANDED, bad.detail
    assert bad.missing_files == ("src/strand.py",)


def test_half_a_branch_on_trunk_is_partial_and_neither_landed_nor_stranded(repo):
    """THE RUNG THAT REPORTS THE FLEET THAT ALREADY EXISTS.

    Every `pr_merged` event written before this change carries no `head_oid`, so the
    exact `merged-tail` rung cannot fire for the orders that prompted issue #232 and
    they fall through to here (spec §7). `PARTIAL` is the only verdict that names them,
    and its failure direction is permanent: a `PARTIAL` that comes out `LANDED` is
    SETTLED, so `check_work_lands` writes it to `landing_checked` and never looks again
    — a silent all-clear that is indistinguishable from a fleet with nothing stranded.

    The shape is the real one: a first pull request squash-merges, the worker keeps
    going, and the second half never leaves the branch. Half the lines it added are on
    `trunk` and half are nowhere, so coverage lands squarely in the band between the two
    thresholds — which is neither of the answers the other tests in this file assert.
    """
    wt = repo.worktree(WO)
    repo.commit(wt, "src/first.py", _feature("first"))
    repo.squash_merge(f"worktree-{WO}", f"[{WO}] the first half (#5)")
    repo.commit(wt, "src/second.py", _feature("second"), "the tail nobody merged")

    found = _assess(repo, WO, worktree=wt)

    assert found.verdict == landing.PARTIAL, found.detail
    assert found.rung == "coverage"
    # Strictly INSIDE the band, both ends: an implementation that moved a threshold or
    # flipped a comparison to `>=`/`<=` would land on an edge and this would catch it.
    assert landing.STRANDED_COVERAGE < found.coverage < landing.LANDED_COVERAGE
    assert found.missing_files == ("src/second.py",)


def test_a_landed_branch_whose_worktree_still_holds_work_is_partial_not_landed(repo):
    """The second route into `PARTIAL`, and the one the cache makes unforgiving.

    The content DID land — coverage is 1.0, well over `LANDED_COVERAGE` — and the branch
    would read `landed` on the thresholds alone. What is left is a file that was never
    committed at all, which is the tail issue #232 nearly lost when 172 worktrees were
    deleted in a disk sweep: 851 lines that existed in exactly one place on disk.

    Demoting `LANDED` to `PARTIAL` here is the whole difference between a report and a
    permanent silence, because `LANDED` is settled and cached and `PARTIAL` is not.
    """
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("feature"))
    repo.squash_merge(f"worktree-{WO}", f"[{WO}] the feature (#6)")

    # The pairing, asserted first: with a clean worktree this exact branch is LANDED.
    clean = _assess(repo, WO, worktree=wt)
    assert clean.verdict == landing.LANDED, clean.detail

    (wt / "src" / "never_added.py").write_text(_feature("orphan"))

    found = _assess(repo, WO, worktree=wt)

    assert found.verdict == landing.PARTIAL, found.detail
    assert found.rung == "coverage"
    assert found.coverage >= landing.LANDED_COVERAGE   # the content really did land
    assert "src/never_added.py" in found.dirty
    assert found.verdict not in landing.SETTLED_VERDICTS   # so it is looked at again


def test_the_commit_subject_rung_confirms_but_never_condemns(repo):
    """Only 96 of this repository's 173 `trunk` commits carry a `[wo-…]` subject.

    So a checker that trusts the subject reports every order predating the convention as
    stranded. The rung may say LANDED and may say UNKNOWN; it may never say STRANDED.
    """
    wt = repo.worktree(WO)
    # A branch whose whole change is a DELETION: nothing was added, so there is nothing
    # to look for on `trunk` and the content test has no opinion.
    (wt / "app.py").unlink()
    _git(wt, "commit", "-aqm", "drop the module")

    unconfirmed = _assess(repo, WO, worktree=wt)
    assert unconfirmed.verdict == landing.UNKNOWN
    assert unconfirmed.rung == "subject"

    repo.squash_merge(f"worktree-{WO}", f"[{WO}] drop the module (#2)")
    assert _assess(repo, WO, worktree=wt).verdict == landing.LANDED


# -- Mode C: a merged pull request, and the work that came after it --------------------


def test_mode_c_a_merged_pr_with_a_tail_is_stranded_and_one_without_is_landed(repo):
    """These orders HAVE a pr_url and it points at a PR that DID merge.

    Any audit keyed on `pr_url` passes them. The exact test is the sha GitHub merged:
    the branch carrying commits after `headRefOid` is work that was never in the pull
    request anybody approved.
    """
    wt = repo.worktree(WO)
    repo.commit(wt, "src/first.py", _feature("first"))
    head = repo.squash_merge(f"worktree-{WO}", f"[{WO}] the first half (#3)")
    pr = "https://github.com/acme/proj/pull/3"

    settled = _assess(repo, WO, worktree=wt, pr_url=pr,
                             pr_merged=True, pr_head_oid=head)
    assert settled.verdict == landing.LANDED, settled.detail

    repo.commit(wt, "src/second.py", _feature("second"), "the tail nobody merged")

    tail = _assess(repo, WO, worktree=wt, pr_url=pr,
                          pr_merged=True, pr_head_oid=head)
    assert tail.verdict == landing.STRANDED
    assert tail.rung == "merged-tail"
    assert tail.tail_commits == 1


def test_an_open_pull_request_is_stranded_and_an_unasked_one_is_not_assumed_open(repo):
    """`pr_merged is None` means nobody asked, which is not the same as "not merged".

    Issue #232's two seven-week-old pull requests are the `False` case. The `None` case
    is a project with no `gh` on the daemon's PATH, and reading it as an open pull
    request would flag every order in that project.
    """
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("feature"))
    repo.squash_merge(f"worktree-{WO}", f"[{WO}] the feature (#4)")
    pr = "https://github.com/acme/proj/pull/4"

    assert _assess(repo, WO, worktree=wt, pr_url=pr,
                          pr_merged=False).verdict == landing.STRANDED
    unasked = _assess(repo, WO, worktree=wt, pr_url=pr, pr_merged=None)
    assert unasked.verdict == landing.LANDED, unasked.detail


def test_a_branch_is_found_by_name_when_the_worktree_is_gone(repo):
    """The audit ran months later, when every worktree had been reclaimed.

    All three naming shapes this fleet has produced carry the work-order id, which is
    how the original audit found the six.
    """
    wt = repo.worktree(WO, branch=f"rescue/{WO}")
    repo.commit(wt, "src/rescued.py", _feature("rescued"))
    _git(repo.path, "worktree", "remove", "--force", str(wt))

    found = _assess(repo, WO)
    assert found.ref == f"rescue/{WO}"
    assert found.verdict == landing.STRANDED


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


# -- the ref that is read, and what happens when git cannot answer ---------------------


def _drop_object(repo: Repo, sha: str) -> None:
    """Delete one loose object, which is what a half-fetched or damaged clone looks like.

    Still not faking git (module docstring): the object store is git's own, and every
    command below really does exit non-zero. There is no other portable way to make a
    `git` that exists and works fail on one repository, and "what does this module do
    with a non-zero exit" is exactly the question.
    """
    (repo.path / ".git" / "objects" / sha[:2] / sha[2:]).unlink()


def test_the_pushed_branch_is_read_and_not_a_local_one_left_behind(repo):
    """`refs/heads/` sorts BEFORE `refs/remotes/`, so a single sorted list picks wrong.

    The audit reads branches months later, in a clone whose local copy may be anything.
    What was PUSHED is the work; a local branch rewound by a rebase, a reset or a
    `worktree remove` is not, and scoring it would under-report a strand — the failure
    direction that hides issue #232 rather than the one that cries wolf.
    """
    wt = repo.worktree(WO)
    repo.commit(wt, "src/early.py", _feature("early"))
    early = _git(wt, "rev-parse", "HEAD").strip()
    repo.commit(wt, "src/late.py", _feature("late"))
    _git(repo.path, "push", "-q", "origin", f"worktree-{WO}")
    _git(repo.path, "worktree", "remove", "--force", str(wt))
    _git(repo.path, "branch", "-f", f"worktree-{WO}", early)   # local now behind origin

    found = _assess(repo, WO)

    assert found.ref == f"origin/worktree-{WO}"
    # The proof it is not the stale one: `src/late.py` exists only on what was pushed.
    assert "src/late.py" in found.missing_files
    assert found.verdict == landing.STRANDED


def test_a_failed_content_read_is_unknown_and_never_stranded(repo):
    """A `git` error must not score 0, because 0 is `stranded` — "flags everything"."""
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("feature"))
    blob = _git(wt, "rev-parse", f"HEAD:src/feature.py").strip()
    _git(repo.path, "worktree", "remove", "--force", str(wt))
    _drop_object(repo, blob)

    found = _assess(repo, WO)

    assert found.verdict == landing.UNKNOWN
    assert found.rung == "unreadable"


def test_a_failed_commit_count_is_unknown_and_never_a_cached_not_produced(repo):
    """`not-produced` is SETTLED and cached, so arriving there by error is permanent.

    `invariants.check_work_lands` writes a settled verdict to `landing_checked` and never
    recomputes it: a `rev-list` that errored once would drop this work order out of the
    audit for good. `unknown` is re-derived every sweep, which is what an error deserves.
    """
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("feature"))
    tip = _git(wt, "rev-parse", "HEAD").strip()
    _drop_object(repo, tip)

    # And the settle-time predicate, which is what a refusal is built on, stays silent.
    work = landing.authored(wt)
    assert work.unreadable and not work.produced

    _git(repo.path, "worktree", "remove", "--force", str(wt))
    found = _assess(repo, WO)

    assert found.verdict == landing.UNKNOWN
    assert found.verdict not in landing.SETTLED_VERDICTS


# -- the default branch nothing ever moved (issue #271) --------------------------------


def _rewind_base(repo: Repo, sha: str) -> None:
    """Put `origin/trunk` back where it was, leaving the merge on the bare origin.

    A clone that has not fetched since somebody else's pull request landed, which is
    every clone in the fleet at the moment a merge completes a work order: the poll sees
    the merge over the network, and nothing here moves this ref.
    """
    _git(repo.path, "update-ref", "refs/remotes/origin/trunk", sha)


def test_a_branch_that_landed_since_the_last_fetch_is_unknown_and_never_stranded(repo):
    """The false positive issue #271 measured at one per merge, repeating every hour.

    The verdict is WITHHELD rather than reversed — `stale-base` is `UNKNOWN`, which is
    re-derived every sweep — and the second half is what makes that honest rather than
    evasive: `refresh_base` really does move the ref, so the answer arrives.
    """
    stale = _git(repo.path, "rev-parse", "origin/trunk").strip()
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("feature"))
    repo.squash_merge(f"worktree-{WO}", f"[{WO}] the feature (#7)")
    _rewind_base(repo, stale)

    withheld = landing.assess(repo.path, WO, worktree=wt, base_current=False)

    assert withheld.verdict == landing.UNKNOWN, withheld.detail
    assert withheld.rung == "stale-base"
    assert withheld.verdict not in landing.SETTLED_VERDICTS   # asked again next sweep
    # "src/feature.py is nowhere" is the sharpest line the report can show and the one
    # a base that may predate the merge cannot support.
    assert not withheld.missing_files

    assert landing.refresh_base(repo.path) is True
    assert _git(repo.path, "rev-parse", "origin/trunk").strip() != stale
    assert _assess(repo, WO, worktree=wt).verdict == landing.LANDED


def test_a_stale_base_withholds_absence_and_never_presence(repo):
    """Two branches, one out-of-date ref: the earlier one is still `LANDED`.

    The asymmetry the whole guard rests on. A branch only grows, so lines FOUND on an
    out-of-date default branch are on the up-to-date one too. Demoting this half as well
    would be the blind spot rather than the fix: on a fleet where no refresh ever
    succeeds nothing would be confirmed as landed again, and an audit that has quietly
    stopped working looks exactly like a fleet with nothing stranded.
    """
    first = repo.worktree("wo-first")
    repo.commit(first, "src/first.py", _feature("first"))
    repo.squash_merge("worktree-wo-first", "[wo-first] the first half (#8)")
    after_first = _git(repo.path, "rev-parse", "origin/trunk").strip()

    second = repo.worktree("wo-second")
    repo.commit(second, "src/second.py", _feature("second"))
    repo.squash_merge("worktree-wo-second", "[wo-second] the second half (#9)")
    _rewind_base(repo, after_first)

    present = landing.assess(repo.path, "wo-first", worktree=first, base_current=False)
    absent = landing.assess(repo.path, "wo-second", worktree=second, base_current=False)

    assert present.verdict == landing.LANDED, present.detail
    assert present.rung == "coverage"           # answered, not withheld
    assert absent.verdict == landing.UNKNOWN, absent.detail


def test_a_stale_base_keeps_the_partial_that_rests_on_a_dirty_worktree(repo):
    """The guard is keyed on the EVIDENCE, and `PARTIAL` is where that stops being
    pedantry: two different facts arrive at it.

    A middling score is a claim about what is missing from the base and is withheld. A
    FULL score beside uncommitted work is Mode C — presence, plus a fact about the
    worktree — and an out-of-date base makes it no less true. Keyed on the verdict's name
    instead, this one would be silenced on the read-only doctor and would print "only
    80/80 (100%)" on its way out.
    """
    wt = repo.worktree(WO)
    repo.commit(wt, "src/feature.py", _feature("feature"))
    repo.squash_merge(f"worktree-{WO}", f"[{WO}] the feature (#10)")
    after_wo = _git(repo.path, "rev-parse", "origin/trunk").strip()
    # Somebody else's work lands on top, so the ref really is out of date — and this
    # branch is nonetheless fully present on the copy we have.
    other = repo.worktree("wo-other")
    repo.commit(other, "src/other.py", _feature("other"))
    repo.squash_merge("worktree-wo-other", "[wo-other] somebody else (#11)")
    _rewind_base(repo, after_wo)
    (wt / "src" / "later.py").write_text(_feature("later"))   # never committed

    kept = landing.assess(repo.path, WO, worktree=wt, base_current=False)

    assert kept.verdict == landing.PARTIAL, kept.detail
    assert kept.rung == "coverage"
    assert "uncommitted" in kept.detail


def test_a_failed_refresh_never_logs_a_credential_out_of_the_remote_url(repo, caplog):
    """A failing fetch prints the remote URL back at you, and this sweep runs hourly.

    So a project whose `origin` carries a token would write it to the log once an hour
    for ever, and daemon text leaves the machine through alarms and `jarvis bug report`.
    Captured at DEBUG, not WARNING: "does not appear in any emitted record" has to mean
    any, and the message is kept at DEBUG for whoever is debugging.

    THE SECOND HALF IS WHAT MAKES THE FIRST MEAN ANYTHING. Which message git chooses is
    git's business — the connection failure provoked here happens to redact the userinfo
    itself, so this assertion would pass against code that logged stderr raw. The
    message git does NOT redact is the authentication failure, and it cannot be provoked
    without a server that refuses a login, so `_cause` is held to that string directly,
    quoted verbatim rather than invented.
    """
    secret = "ghp_FAKETOKEN0123456789abcdefghijklmnop"
    # Port 1 rather than 443 so the connection fails at once instead of after a timeout.
    _git(repo.path, "remote", "set-url", "origin",
         f"https://x-access-token:{secret}@127.0.0.1:1/acme/proj.git")

    with caplog.at_level("DEBUG", logger="jarvis.landing"):
        assert landing.refresh_base(repo.path) is False

    assert caplog.records
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert "x-access-token" not in record.getMessage()
    warnings = "\n".join(r.getMessage() for r in caplog.records
                         if r.levelname == "WARNING")
    # Nothing of the remote's own text, and still a cause a reader can act on.
    assert "127.0.0.1" not in warnings
    assert "could not be reached" in warnings

    verbatim = (f"fatal: Authentication failed for "
                f"'https://x-access-token:{secret}@github.com/acme/proj.git/'")
    assert landing._cause(verbatim) == "the remote refused our credentials"
    assert secret not in landing._cause(verbatim)
    assert secret not in landing._scrub(verbatim)      # the DEBUG line's second defence


def test_a_refresh_that_cannot_run_is_false_rather_than_an_error(repo, tmp_path):
    """Three ways to have no fresh ref, and not one of them may raise or condemn.

    `allow_network=False` is the read-only `jarvis doctor`, which may not write to a
    repository at all; the unreachable remote is an offline machine or a deleted fork.
    The third is the case with nothing to be behind — a project whose default branch is
    LOCAL has no remote copy to be out of date against, and demoting it would break
    every project that was never cloned from anywhere.
    """
    before = _git(repo.path, "rev-parse", "origin/trunk").strip()
    assert landing.refresh_base(repo.path, allow_network=False) is False
    assert _git(repo.path, "rev-parse", "origin/trunk").strip() == before

    # `main` and not `trunk` (the rule at the top of this file): rung 3 of the pinned
    # ladder knows exactly two names, so this one is the ladder's, not git's default.
    solo = make_git_project(tmp_path, "solo")
    _git(solo, "symbolic-ref", "HEAD", "refs/heads/main")
    (solo / "app.py").write_text(_feature("solo", 4))
    _git(solo, "add", "-A")
    _git(solo, "commit", "-qm", "base")
    assert landing.base_ref(solo) == "main"
    assert landing.refresh_base(solo, allow_network=False) is True

    # ...and the pairing, because "there is nothing to be behind" is a question about the
    # CLONE and not about the ref. Add a remote and that local `main` can be months old
    # with nothing able to move it — a single-branch clone of another branch lands here.
    _git(solo, "remote", "add", "origin", str(tmp_path / "elsewhere.git"))
    assert landing.base_ref(solo) == "main"
    assert landing.refresh_base(solo) is False

    _git(repo.path, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert landing.refresh_base(repo.path) is False
