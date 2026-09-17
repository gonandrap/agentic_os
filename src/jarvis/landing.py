"""Did the code a work order wrote ever reach the default branch?

`completed` used to be reachable on the worker's word alone. A fleet-wide audit of 209
work orders (GitHub issue #232) found six whose code existed only on a remote branch,
two of them pull requests open for seven weeks carrying ~3,100 lines between them, and
every one was found by a human going looking. This module is the OS asking the question
instead, and it answers it at two very different distances.

## Two questions, two costs, and they must not be confused

**`authored()` is the settle-time predicate.** Has this work order's worktree produced
anything — commits over its merge base, or uncommitted files? Exact, local, two `git`
invocations, no network, no heuristic. It runs on every `jarvis wo finish`, so it is
allowed to cost nothing and allowed to be certain. It is what makes `ops.finish` and
`ops.review_work_order` refuse to settle over work nobody has undertaken to land
(docs/superpowers/specs/2026-09-13-a-finished-order-proves-its-code-landed.md §2).

**`assess()` is the audit.** Is what this order produced ON the default branch, months
later, for an order that settled long ago? That question has no exact answer in this
repository, for the reason §3 of the spec gives and the next section restates, so
`assess` is a ladder of tests from exact to heuristic and reports which rung answered.
It costs a `git` call per touched file, so it runs on `jarvis doctor`'s own slow cadence
and never on a worker's critical path.

## THE REPOSITORY SQUASH-MERGES, WHICH KILLS EVERY OBVIOUS TEST

A squash merge replays a branch's whole diff as ONE new commit with a new sha. So:

- commit reachability (`git merge-base --is-ancestor`) reports EVERY branch in the fleet
  as unmerged, including the ~96 that landed;
- `git rev-list --count base..branch` (the "ahead" count) says the same;
- `git cherry` / patch-ids do not survive the squash either.

A checker built on any of those flags everything, which is worse than flagging nothing:
it gets switched off within a day and the sixth stranded order becomes the seventh.

The test that DOES survive a squash is CONTENT. If a branch landed, the lines it added
are in the default branch's files; if it did not, they are nowhere. `_coverage` measures
exactly that — what fraction of the significant lines this branch added can be found in
the default branch's copy of the same file — and `LANDED_COVERAGE` / `STRANDED_COVERAGE`
are the two thresholds it is read against. They are not guesses: they were measured
against all 180-odd branches of this repository before they were written down, and
`docs/.../§4` carries the distribution. The stranded population topped out at 0.12 and
the landed population bottomed out at 0.79.

The commit-subject test the original audit used — does the default branch carry a commit
whose subject starts with `[<wo-id>]` — is here too, but only as a corroborating rung,
never alone. It is a FALSE-NEGATIVE MACHINE: only 96 of this repository's 173 default-
branch commits carry that prefix at all, because the convention postdates half of them,
so an order that landed before it existed looks stranded to a checker that trusts it.

## The negative controls are half the value

Most work orders produce no code at all: a planner whose deliverable is a plan, a
knowledge-base write, an investigation, a release. The audit found 60 such orders among
89 candidates — a check without that exclusion has a 67% false-positive rate. The
exclusion here is not a list of work-order kinds to skip, which would rot the first time
somebody invents a kind; it is derived from the same fact everything else is: a branch
with no commits over its base and a clean worktree produced nothing, so there is nothing
to land, so `NOT_PRODUCED`. A category list would also be wrong — an investigation that
does commit a script HAS produced something.

## THE DEFAULT BRANCH IS A REMOTE-TRACKING REF, AND NOTHING ELSE EVER MOVES IT

`base_ref` resolves to `origin/main`, and a merge is detected over the NETWORK — the
poll asks `gh`, GitHub says MERGED, and the work order completes while that local ref
still points at the commit before the squash. The content test then looks for the
branch's lines in a copy of the file that predates the merge, scores near zero and
reports `STRANDED`, every hour, until a human happens to fetch in that checkout (issue
#271: one false positive per merge, on the checker whose docstring above says a checker
that flags everything gets switched off within a day).

So `refresh_base` moves it, and `assess` will not say `STRANDED` or `PARTIAL` off a ref
nobody refreshed: `base_current=False` turns both into `UNKNOWN` at the `stale-base`
rung. The two halves are not interchangeable — the refresh alone still condemns a branch
whenever a fetch fails, and the guard alone would turn the false positive into a
permanent blind spot, since `UNKNOWN` is never cached and a fleet where nobody fetches
would never confirm a landing again.

Absence is what staleness breaks, never presence: lines FOUND on an out-of-date default
branch are on the up-to-date one too, because a branch only grows. That is why `LANDED`,
`merged-tail` and `pull-request-open` are left alone — the first is monotonic and the
other two never read the base at all.

## Why this module imports almost nothing

Same rule as `evidence`, for a weaker but real version of the same reason: this is the
thing that tells a worker it may not finish, so it must not be able to fail for a reason
that has nothing to do with the question. The standard library; `evidence` for the ONE
pinned merge-base ladder (`evidence.base_ref`) and its `ProjectSpec` stand-in, never a
second copy of either; and `worker_session` for the pure path helper that knows where a
worktree lives — the same two-module set `evidence` itself is held to. No store, no
catalog, no `gh`. What GitHub says about a pull request is passed IN by the caller that
already asked: see `assess`.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import worker_session
from .evidence import ProjectRef, base_ref

log = logging.getLogger("jarvis.landing")

#: At or above this fraction of its added lines present in the default branch, a branch
#: landed. At or below `STRANDED_COVERAGE`, it did not. Between them is `PARTIAL`, which
#: is a real answer and not a shrug: it is the exact shape of issue #232's Mode C, where
#: a first pull request merged and the work that came after it did not.
#:
#: MEASURED, not chosen. Across every branch in this repository the two populations
#: separate with a gap either side of 0.5 — see the module docstring. The thresholds sit
#: well inside that gap rather than on its edges, so a branch has to move a long way
#: before its verdict changes.
LANDED_COVERAGE = 0.75
STRANDED_COVERAGE = 0.25

#: How long one refresh of the default branch may take before it is abandoned. It is a
#: single ref with no tags, so this is generous rather than tight; what it is really
#: sized against is a daemon tick, which must not be held open by an unreachable remote.
#: Timing out is not an error here — it is `base_current=False`, which is `UNKNOWN`.
FETCH_TIMEOUT_SECONDS = 20

#: A line has to be long enough and wordy enough that finding it in another file means
#: something. `}`, `"""`, `return`, a blank line and a lone bracket all appear in every
#: Python file ever written, so counting them as "present on the default branch" would
#: drag every branch's coverage towards 1.0 — the direction that HIDES a strand.
SIGNIFICANT_CHARS = 12

#: Verdicts. `UNKNOWN` is deliberately distinct from `STRANDED`: "I could not tell"
#: and "this work is not on the default branch" are different things to put in front of
#: a user, and collapsing them is how a report earns its reputation for crying wolf.
LANDED = "landed"
STRANDED = "stranded"
PARTIAL = "partial"
NOT_PRODUCED = "not-produced"
UNKNOWN = "unknown"

#: Verdicts that mean a human owes this work order a decision.
UNSETTLED_VERDICTS = (STRANDED, PARTIAL)

#: Verdicts that will never change and may therefore be CACHED: a completed work order's
#: branch has stopped moving, and content on the default branch stays on it. `UNKNOWN` is
#: pointedly not here — it is the answer that a later push or a repaired remote fixes.
SETTLED_VERDICTS = (LANDED, NOT_PRODUCED)

#: A GitHub pull-request URL sitting in prose. Mode A of issue #232: four work orders
#: finished with a summary that NAMED a draft pull request and passed no `--pr`, so
#: `pr_url` stayed NULL and no poller ever watched it. Anchored on `/pull/<n>` rather
#: than on the host, because a URL is only a pull request if it points at one.
PR_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']*?/pull/\d+")


def pr_urls_in(text: str) -> tuple[str, ...]:
    """Every pull-request URL named in `text`, in order, deduplicated.

    Trailing punctuation is stripped: a URL at the end of a sentence in a `--summary`
    is written "…/pull/33." and the full stop is not part of it.
    """
    seen: dict[str, None] = {}
    for match in PR_URL_RE.finditer(text or ""):
        seen.setdefault(match.group(0).rstrip(".,;:"), None)
    return tuple(seen)


@dataclass(frozen=True)
class Authored:
    """What a work order's worktree has produced that is not yet anywhere else.

    The settle-time answer, and exact. `commits` counts what the branch carries over its
    merge base; `dirty` names what was never committed at all — the tail issue #232 lost
    when 172 worktrees were deleted in a disk sweep, and the reason `produced` is not
    just `commits`.
    """

    branch: str = ""
    base: str = ""
    commits: int = 0
    dirty: tuple[str, ...] = ()
    #: Why there is no answer, when there is none. "" when the worktree was read.
    unreadable: str = ""

    @property
    def produced(self) -> bool:
        """True when settling this work order would strand something.

        A missing worktree is NOT this. The worktree is deleted when a work order's
        branch is reclaimed, and by then the question has already been asked and
        answered; treating "I cannot look" as "there is unlanded work" would refuse
        every finish on a machine where the worktree was cleaned up early.
        """
        return bool(self.commits or self.dirty)

    def record(self) -> dict[str, object]:
        """This, as an event payload. One shape, so the two places that write it agree.

        `ops.park_unlanded` and `ops.finish --abandon` record the SAME fact about the
        same branch and are read back side by side on the timeline; issue #232's Mode B
        is what happens when the evidence a settling had is not written down anywhere.
        """
        return {"branch": self.branch, "base": self.base, "commits": self.commits,
                "dirty": list(self.dirty)}

    def describe(self) -> str:
        """One clause naming what exists, for the refusal that quotes it."""
        parts = []
        if self.commits:
            parts.append(f"{self.commits} commit{'s' if self.commits != 1 else ''} "
                         f"on `{self.branch or 'its branch'}`")
        if self.dirty:
            parts.append(f"{len(self.dirty)} uncommitted file"
                         f"{'s' if len(self.dirty) != 1 else ''}")
        return " and ".join(parts) or "nothing"


def worktree_of(project_path: Path, wo: dict[str, object]) -> Path | None:
    """Where this work order's worktree lives, or None if it is not on disk.

    One line, and it exists so that the two callers of this module — `ops.finish` and
    `invariants.check_work_lands` — do not each assemble `worker_session.worktree_path`'s
    `ProjectSpec` stand-in for themselves. Three copies of a two-attribute shim is how two
    checks end up disagreeing about where a work order's code is, which would be a
    peculiar bug for THIS module to have.
    """
    # type: ignore — `ProjectRef` carries the one attribute that helper reads.
    return worker_session.worktree_path(ProjectRef(project_path), wo)  # type: ignore[arg-type]


def authored(worktree: Path | None) -> Authored:
    """Has this worktree produced anything? Exact, local, and cheap enough to always run.

    Three `git` invocations and no network. Every failure — no worktree, no git, no base
    to compare against, a command that errored — comes back as `unreadable` with
    `produced` False, because this is the predicate a refusal is built on: a work order
    must never be unable to finish because the OS could not run `git`. The failure still
    goes in the log (`_git`), and an `unreadable` settling is not silent either — it is
    the one shape `assess` will look at again later.
    """
    if worktree is None or not worktree.is_dir():
        return Authored(unreadable="no worktree on disk")
    base = base_ref(worktree)
    if not base:
        # Rung 4 of the pinned ladder. `evidence` treats it as "diff against HEAD",
        # which is the right answer for a diff and the wrong one here: with no default
        # branch to compare against, "ahead of the default branch" has no meaning.
        return Authored(unreadable="no default branch to compare against")
    branch = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    count = _git(worktree, "rev-list", "--count", f"{base}..HEAD")
    # `--porcelain` lists untracked files too, and it must: a worker that wrote a new
    # module and never `git add`ed it has produced exactly the thing this exists to
    # catch. `--untracked-files=all` because the default COLLAPSES an untracked
    # directory to one entry — a worker that wrote a whole new package would report as
    # `src/`, and the refusal would say "1 uncommitted file" about forty.
    status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    if branch is None or count is None or status is None:
        return Authored(unreadable="git could not read the worktree")
    dirty = tuple(line[3:] for line in status.splitlines() if line[3:])
    return Authored(branch=branch.strip(), base=base,
                    commits=int(count.strip()) if count.strip().isdigit() else 0,
                    dirty=dirty)


@dataclass(frozen=True)
class Landing:
    """The audit's verdict on one work order, with the rung that produced it.

    `rung` is not decoration. A `STRANDED` from `pull-request-open` is a fact GitHub
    stated; one from `coverage` is a measurement with thresholds behind it, and a user
    deciding whether to go and look is entitled to know which they are reading.
    """

    wo_id: str
    verdict: str
    rung: str
    detail: str
    ref: str = ""
    base: str = ""
    pr_url: str = ""
    coverage: float = -1.0
    added_lines: int = 0
    #: Files this branch ADDED that do not exist on the default branch at all. The
    #: sharpest single line a report can show: "src/jarvis/launcher.py is nowhere".
    missing_files: tuple[str, ...] = ()
    tail_commits: int = 0
    dirty: tuple[str, ...] = ()

    @property
    def unsettled(self) -> bool:
        return self.verdict in UNSETTLED_VERDICTS


def refresh_base(repo: Path, *, allow_network: bool = True) -> bool:
    """Bring the default branch up to date, and say whether it may be measured against.

    The answer to `assess`'s `base_current`, and the ONLY thing in the OS that moves a
    remote-tracking ref. Call it once per sweep, never once per work order: it is a
    round trip, and the ref it updates is shared by every order in the project.

    True means the base holds everything the remote does, by one of two routes:

    * there is nothing to be behind — the ladder landed on a LOCAL branch (rung 3), so
      this project has no remote view of a default branch and its own is authoritative;
    * the fetch ran and exited 0.

    False is every other outcome — no default branch at all, `allow_network=False`, a
    remote that could not be reached, a fetch that timed out — and it is not an error.
    It is the input that makes `assess` answer `UNKNOWN` instead of condemning a branch
    on the strength of a ref nobody refreshed.

    `allow_network=False` is the read-only path: `check_project(repair=False)`, the
    `jarvis doctor` a human types without `--repair`. A fetch writes to the repository,
    and "read-only unless you asked for repair" is a promise `ops.run_doctor` makes in
    print. The cost is that a read-only doctor cannot report `STRANDED` by coverage, only
    by the rungs that read no base; the daemon's hourly sweep is what refreshes the ref
    and reports.
    """
    ref = base_ref(repo)
    if not ref:
        return False
    full = (_git(repo, "rev-parse", "--symbolic-full-name", ref) or "").strip()
    if not full.startswith("refs/remotes/"):
        # Rung 3, or a ref that could not be named at all: either way there is no remote
        # copy of the default branch in this clone, so nothing here can be out of date.
        return bool(full)
    remote, _, branch = full[len("refs/remotes/"):].partition("/")
    return bool(remote and branch) and allow_network and _fetch(repo, remote, branch)


def assess(repo: Path, wo_id: str, *, worktree: Path | None = None,
           pr_url: str = "", pr_merged: bool | None = None,
           pr_head_oid: str = "", base_current: bool = False) -> Landing:
    """Is this work order's code on the default branch? A ladder, exact rungs first.

    `pr_merged` and `pr_head_oid` are what GitHub said, passed IN — this module does not
    call `gh`, for the reason the module docstring gives. `pr_merged` is None when
    nobody asked or nobody could answer, which is NOT the same as False and is why the
    open-pull-request rung tests `is False` rather than `not`.

    The rungs, and each one only runs because the one above it could not answer:

    1. `not-produced` — the branch carries nothing over its base and the worktree is
       clean. The exclusion that keeps 60 planner and investigation orders out of the
       report; see the module docstring for why it is derived rather than listed.
    2. `pull-request-open` — there is a pull request and GitHub says it has not merged.
       Nothing to measure: the work is delivered and refused or forgotten.
    3. `merged-tail` — the pull request merged, and the branch carries commits AFTER the
       sha GitHub merged, or files never committed at all. Issue #232's Mode C, which is
       invisible to any audit keyed on `pr_url` because these orders HAVE one and it
       points at a pull request that DID merge. Skipped for every `pr_merged` event
       written before this change, which carries no `head_oid` and is not backfilled —
       those fall through to `coverage`, which still reports Mode C. Spec §7.
    4. `coverage` — the content test. See the module docstring.
    5. `subject` — the corroborating rung, reached only when the branch added no
       significant lines at all (a pure deletion, a rename, a config tweak). Absence of
       a `[<wo-id>]` commit is NOT evidence here, so a miss ends at `unknown`.

    `base_current` is `refresh_base`'s answer and DEFAULTS TO FALSE, because the ref this
    measures against is a remote-tracking one that nothing moves on its own: a caller
    that has not refreshed it has not earned a condemnation, and a default of True would
    hand one to every caller written after this. False demotes the two verdicts that read
    absence off the base — `STRANDED` and `PARTIAL` at the `coverage` rung — to `UNKNOWN`
    at `stale-base`. Nothing else moves; see the module docstring on why presence
    survives staleness and absence does not.

    A sixth rung, `unreadable`, is not part of that sequence: it is what a `git` command
    that ERRORED produces, at whichever rung it errored on. `_git` keeps that distinct
    from an empty result precisely so it can arrive here as `unknown` — which is
    re-derived every sweep — instead of as `stranded` or as a cached `not-produced`.
    """
    ref = _ref_for(repo, wo_id, worktree)
    base = base_ref(repo)
    if not (ref and base):
        return Landing(wo_id, UNKNOWN, "no-ref", pr_url=pr_url,
                       detail="no branch of this work order and no default branch to "
                              "compare it against")

    dirty = authored(worktree).dirty
    commits = _count(repo, f"{base}..{ref}")
    if commits is None:
        # NOT `NOT_PRODUCED`. That verdict is settled and cached, so a `rev-list` that
        # errored would drop this work order out of the audit for ever; `unknown` is
        # re-derived every sweep, which is what a transient failure deserves.
        return Landing(wo_id, UNKNOWN, "unreadable", ref=ref, base=base, pr_url=pr_url,
                       detail=f"could not count what `{ref}` carries over `{base}` — "
                              f"see the jarvis.landing log")
    if not commits and not dirty:
        return Landing(wo_id, NOT_PRODUCED, "no-commits", ref=ref, base=base,
                       pr_url=pr_url,
                       detail=f"`{ref}` carries nothing over `{base}` and its worktree "
                              f"is clean — this work order produced no code")

    if pr_url and pr_merged is False:
        return Landing(wo_id, STRANDED, "pull-request-open", ref=ref, base=base,
                       pr_url=pr_url, dirty=dirty,
                       detail=f"{commits} commit(s) on `{ref}` behind a pull request "
                              f"that has not merged: {pr_url}")

    if pr_merged and pr_head_oid:
        # A failed count here is usually a KNOWN shape — the merged sha was never fetched
        # into this clone — and it only costs the exact rung, so it falls through to
        # `coverage` rather than answering `unknown`. Coverage still catches Mode C from
        # the other side, through `dirty`, and an `unknown` would be a worse answer than
        # a measured one.
        tail = _count(repo, f"{pr_head_oid}..{ref}") or 0
        if tail or dirty:
            return Landing(wo_id, STRANDED, "merged-tail", ref=ref, base=base,
                           pr_url=pr_url, tail_commits=tail, dirty=dirty,
                           detail=f"{pr_url} merged, but `{ref}` carries {tail} "
                                  f"commit(s) and {len(dirty)} uncommitted file(s) "
                                  f"after the sha that merged")

    measured = _coverage(repo, base, ref)
    if measured is None:
        # The one that would otherwise "flag everything": a failed `ls-tree`, `diff` or
        # `show` scores 0, and 0 is `STRANDED`.
        return Landing(wo_id, UNKNOWN, "unreadable", ref=ref, base=base, pr_url=pr_url,
                       dirty=dirty,
                       detail=f"could not read what `{ref}` added or what `{base}` "
                              f"holds — see the jarvis.landing log")
    present, total, missing = measured
    if not total:
        # Nothing significant was added, so there is nothing to look for. The subject
        # rung is all that is left, and it may only CONFIRM.
        if _subject_landed(repo, base, wo_id):
            return Landing(wo_id, LANDED, "subject", ref=ref, base=base, pr_url=pr_url,
                           detail=f"`{base}` carries a commit titled [{wo_id}]")
        return Landing(wo_id, UNKNOWN, "subject", ref=ref, base=base, pr_url=pr_url,
                       dirty=dirty,
                       detail=f"`{ref}` added no lines that could be looked for on "
                              f"`{base}`, and no commit there is titled [{wo_id}]")

    cov = present / total
    verdict = (LANDED if cov >= LANDED_COVERAGE else
               STRANDED if cov <= STRANDED_COVERAGE else PARTIAL)
    # `==`, never `is`: the verdicts are plain strings, and identity holds here only
    # because `verdict` is bound to this module's own constant. The day it arrives from
    # an event payload or any other round trip, `is` goes quietly False and this branch —
    # the one that catches Mode C without a pull request to key on — is dead code.
    if verdict == LANDED and dirty:
        # The branch landed and the worktree still holds work that never left it. Mode C
        # again, arrived at without a pull request to key on.
        verdict, cov_note = PARTIAL, " but its worktree still holds uncommitted work"
    else:
        cov_note = ""
    if verdict != LANDED and not base_current:
        # Issue #271. A low score off a ref nobody refreshed is not evidence of anything:
        # the merge that landed this branch may already be on the remote's default branch
        # and simply not in this clone yet. `UNKNOWN` is re-derived every sweep, so the
        # answer arrives of its own accord as soon as a refresh succeeds. `missing_files`
        # is dropped on the way: "this file is nowhere" is exactly the claim a base that
        # may predate the merge cannot support.
        return Landing(wo_id, UNKNOWN, "stale-base", ref=ref, base=base, pr_url=pr_url,
                       coverage=cov, added_lines=total, dirty=dirty,
                       detail=f"only {present}/{total} of the lines `{ref}` added are on "
                              f"`{base}` ({cov:.0%}), but `{base}` was not refreshed — "
                              f"it may predate the merge that landed them")
    return Landing(wo_id, verdict, "coverage", ref=ref, base=base, pr_url=pr_url,
                   coverage=cov, added_lines=total, missing_files=missing, dirty=dirty,
                   detail=f"{present}/{total} of the lines `{ref}` added are on "
                          f"`{base}` ({cov:.0%}){cov_note}"
                          + (f"; {len(missing)} added file(s) are missing entirely, "
                             f"first is {missing[0]}" if missing else ""))


# --------------------------------------------------------------------------- internals

def _ref_for(repo: Path, wo_id: str, worktree: Path | None) -> str:
    """The branch this work order's code is on, or "".

    The worktree's own HEAD first, because it is the only answer that cannot be wrong.
    Failing that — and the worktree is usually gone by the time anyone audits — every
    branch name containing the work-order id, which covers all three shapes this fleet
    has produced: `worktree-wo-x`, `wo-x-some-slug` and `rescue/wo-x`.

    A REMOTE ref beats a local one, because a local branch can be behind what was
    actually pushed and it is the PUSHED work this module is asked about. That ordering
    is two separate queries and not one sorted list: `git for-each-ref` sorts by FULL
    refname, so `refs/heads/...` comes out ahead of `refs/remotes/...` and taking the
    first match would pick the local branch every time both exist — the stale one this
    paragraph exists to avoid. kn-47004b56: a rule written down is not a rule applied.
    """
    if worktree is not None and worktree.is_dir():
        head = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") or ""
        if head.strip() and head.strip() != "HEAD":
            return head.strip()
    for pattern in ("refs/remotes/", "refs/heads/"):
        names = (_git(repo, "for-each-ref", "--format=%(refname:short)", pattern)
                 or "").split()
        matches = [n for n in names if wo_id in n]
        if matches:
            return matches[0]
    return ""


def _count(repo: Path, rev_range: str) -> int | None:
    """Commits in `rev_range`, or None if git could not answer.

    None is NOT zero. Zero means "this branch carries nothing", which `assess` reads as
    `NOT_PRODUCED` — a settled verdict that `invariants.check_work_lands` caches and never
    recomputes. Letting a failed `rev-list` arrive there would drop a work order out of the
    audit permanently on the strength of a command that errored.
    """
    out = _git(repo, "rev-list", "--count", rev_range)
    if out is None:
        return None
    return int(out.strip()) if out.strip().isdigit() else 0


def _significant(line: str) -> bool:
    """Is this line worth looking for on the default branch? See `SIGNIFICANT_CHARS`."""
    text = line.strip()
    return len(text) >= SIGNIFICANT_CHARS and any(c.isalnum() for c in text)


def _coverage(repo: Path, base: str, ref: str
              ) -> tuple[int, int, tuple[str, ...]] | None:
    """(lines found on `base`, lines looked for, files added that `base` lacks entirely).

    None if any `git` call failed — see `_git`. Scoring 0 off a command that errored is
    the single most damaging thing this module could do, since 0 reads as `STRANDED`.

    Per FILE, never across the tree: a line is "present" only in the default branch's
    copy of the file the branch put it in. Searching the whole tree instead would score
    a moved import as landed work.

    The diff is `base...ref` — three dots, the MERGE BASE — so a branch cut months ago
    is measured against what it changed, not against everything `base` has done since.
    """
    changed = _git(repo, "diff", "--name-only", f"{base}...{ref}")
    tree = _git(repo, "ls-tree", "-r", "--name-only", base)
    if changed is None or tree is None:
        return None
    names = changed.split()
    if not names:
        return 0, 0, ()
    on_base = set(tree.split())
    present = total = 0
    missing: list[str] = []
    for name in names:
        diff = _git(repo, "diff", f"{base}...{ref}", "--", name)
        if diff is None:
            return None
        added = [line[1:] for line in diff.splitlines()
                 if line.startswith("+") and not line.startswith("+++")
                 and _significant(line[1:])]
        absent = name not in on_base
        if absent:
            missing.append(name)
        if not added:
            continue
        total += len(added)
        if absent:
            # `ls-tree` already said this file is not on `base`, so `git show base:name`
            # would fail for a KNOWN reason and none of these lines can be found. That is
            # the distinction `_git` exists to keep: an absence proved by another command,
            # not a failure read as one.
            continue
        blob = _git(repo, "show", f"{base}:{name}")
        if blob is None:
            return None
        present += sum(1 for line in added if line.strip() in blob)
    return present, total, tuple(missing)


def _subject_landed(repo: Path, base: str, wo_id: str) -> bool:
    """Does `base` carry a commit whose subject starts with `[<wo-id>]`?

    The convention every worker's pull-request title is held to, which a squash merge
    carries onto the default branch verbatim. CONFIRMS ONLY — see the module docstring
    on why its absence proves nothing, which is also why a failed `git log` may be False
    here and nowhere else: the rung's only outputs are `LANDED` and `UNKNOWN`, so losing
    it costs a confirmation and cannot manufacture a complaint.
    """
    return any(line.startswith(f"[{wo_id}]")
               for line in (_git(repo, "log", "--format=%s", base) or "").splitlines())


def _fetch(repo: Path, remote: str, branch: str) -> bool:
    """Update `refs/remotes/<remote>/<branch>` from the network. True if it worked.

    THE ONE COMMAND IN THIS MODULE THAT WRITES, and the only one that leaves the machine,
    which is why it is not `_git`: that helper is documented read-only and its callers
    read its output as data, while this one is called for its effect and answers a
    yes/no. Everything it does is narrowed on purpose — ONE explicit refspec so a
    single-branch clone is updated too and no other ref moves, `--no-tags` so a busy
    repository's tag list is not dragged across per sweep, `--quiet`, and a timeout,
    because an unreachable remote must not hold a daemon tick open.

    Never raises. A failure here is `base_current=False`, which is `UNKNOWN` — the
    verdict that costs a sweep its answer and never a branch its reputation.
    """
    spec = f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"
    try:
        proc = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "--no-tags",
                               remote, spec], capture_output=True, text=True,
                              errors="replace", check=False,
                              timeout=FETCH_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not refresh %s in %s: %s", spec, repo, exc)
        return False
    if proc.returncode != 0:
        log.warning("refreshing %s in %s exited %d: %s", spec, repo, proc.returncode,
                    proc.stderr.strip()[:200])
        return False
    return True


def _git(repo: Path, *args: str) -> str | None:
    """One read-only git command in `repo`. Its stdout, or None if it FAILED.

    Never raises, for `evidence._git`'s reason and one of its own: this module's caller is
    often a worker trying to finish, and a repository with no `origin`, no commits or no
    git at all must produce a thin answer rather than an exception that strands it.

    But it does not return "" for a failure either, because every caller here reads the
    output as DATA and "" is a meaningful datum: no commits ahead, no files changed, the
    lines are nowhere on the default branch. A transient failure smuggled in as "" makes
    `_coverage` score 0 and report `STRANDED` — "flags everything" (module docstring)
    arriving by the back door — and makes `_count` report `NOT_PRODUCED`, which is cached
    for ever. So the failure is a separate value the callers have to handle, and it is
    logged: this module is otherwise silent by design, and a fleet-wide audit that quietly
    stopped working would look exactly like a fleet with nothing stranded.
    """
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, errors="replace", check=False)
    except OSError as exc:
        log.warning("git %s in %s could not run: %s", " ".join(args), repo, exc)
        return None
    if proc.returncode != 0:
        log.warning("git %s in %s exited %d: %s", " ".join(args), repo,
                    proc.returncode, proc.stderr.strip()[:200])
        return None
    return proc.stdout
