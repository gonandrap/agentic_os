"""Did the code a work order wrote ever reach the default branch?

`completed` used to be reachable on the worker's word alone. A fleet-wide audit of 209
work orders (GitHub issue #232) found six whose code existed only on a remote branch,
two of them pull requests open for seven weeks carrying ~3,100 lines between them, and
every one was found by a human going looking. This module is the OS asking the question
instead, and it answers it at two very different distances.

## Two questions, two costs, and they must not be confused

**`authored()` is the settle-time predicate.** Has this work order's worktree produced
anything — commits over its merge base, or uncommitted files? Exact, local, three `git`
invocations, no network, no heuristic. It runs on every `jarvis wo finish`, so it is
allowed to cost nothing and allowed to be certain. It is what makes `ops.finish` and
`ops.review_work_order` refuse to settle over work nobody has undertaken to land
(docs/superpowers/specs/2026-09-13-a-finished-order-proves-its-code-landed.md §2).

**`judge()` is the audit.** Months later, for an order that settled long ago: did this
work order's pull request land? That is one question about one artifact — the pull
request the order recorded — and it is answered from what GitHub said about it, never
from the content of the repository.

## THE AUDIT ASKS ABOUT THE PULL REQUEST. IT USED TO MEASURE THE DIFF, AND IT WAS WRONG

Until 2026-09-18 this module answered by CONTENT: what fraction of the lines a branch
added can still be found in the default branch's current copy of the same file. The
reasoning was that a squash merge destroys every obvious test — commit reachability, the
ahead-count and patch-ids all report the ~96 branches that landed as unmerged — so
content is the only thing that survives it.

Content survives a squash. It does not survive a REFACTOR. Run live against the
`jarvis_os` records, INV-WORK-LANDED named seven completed orders as unlanded and five of
them were false positives: three had merged months earlier and been rewritten since
(scoring 44%, 31% and 72%), one had its file renamed and read as "missing entirely", and
one was measured against a pull request the work order had recorded while a NEWER pull
request on the same branch was the live one. Four distinct defects, every one of them
downstream of measuring content instead of asking about the pull request.

So the user narrowed the invariant, on 2026-09-18, after reading that report: *"checking
branches for which no PR were created shouldn't be checked by the invariant, instead,
should be rejected during the validation. The invariant only should focus on making sure
that the PR for that order has landed in main if the order is completed."*

**The scope is now completed orders that HAVE a pull request, and the predicate is that
pull request's state.** Merged is the answer — no line arithmetic, no coverage score, no
missing-file list. Open says the work is delivered and waiting on a merge. Closed
unmerged says it was delivered and refused. No pull request at all is OUT OF SCOPE and
silent: whether an order should have produced one is validation's question, asked while
the work is still live, not an audit's months afterwards.

**What that gives up, deliberately.** A completed order whose only product sits as a WIP
commit on a branch nobody ever opened a pull request for is now reported by nothing here
— `wo-5a6b2d6d`, a config-console design document on `rescue/wo-5a6b2d6d`, is the live
example. The user accepts that, because the place to catch it is the validation round
that let the order settle.

## `pr_url` IS THE POPULATION FILTER, AND ANOTHER INVARIANT IS WHAT MAKES IT TRUSTWORTHY

`pr_url` is what a worker typed at `jarvis wo finish --pr`, once, so it is only as good
as that habit — and in the production records it is not good: NULL on `wo-5eedc84d`,
which merged pull request #42 all along, and on `wo-cd73c537` it names a merged #81 while
#116 carries the rest of that order's work and is open.

That is a REAL defect and it is deliberately not fixed here. The user's second ruling,
2026-09-18: *"If there is any code change made in an order, then pr_url must not be
empty, and the invariant should rely on that pr_url to check the change lands on main
once the order gets completed."* So the column is made trustworthy at its source, by
INV-PR-RECORDED (work order `wo-2005a89b`), which refuses to let an order that wrote code
settle without one; and this audit reads it and judges THAT pull request. Two checks, one
chain, neither of them re-deriving the other's half:

- **no `pr_url` -> out of scope here, silent.** Not an unknown and not a shrug. Whether an
  order should have had one is INV-PR-RECORDED's question, asked while the work is live.
- **a `pr_url` that names the wrong pull request** — the `wo-cd73c537` shape — is also
  INV-PR-RECORDED's. This module judges the pull request the order recorded and says so;
  it does not go looking for a better one.

The state of that pull request reaches the timeline as a `landing_seen` event, written by
`Daemon.refresh_landings` — the half with a network. `judge` reads that event and nothing
else, and an order it has no CURRENT event for (`FRESH_FOR_SECONDS`) is counted and
reported as unread by `INV-LANDING-AUDIT-FRESH` rather than passed over: an audit with no
data must not render as a clean bill of health.

## Why this module imports almost nothing

Same rule as `evidence`, for a weaker but real version of the same reason: this is the
thing that tells a worker it may not finish, so it must not be able to fail for a reason
that has nothing to do with the question. The standard library; `evidence` for the ONE
pinned merge-base ladder (`evidence.base_ref`) and its `ProjectSpec` stand-in, never a
second copy of either; and `worker_session` for the pure path helper that knows where a
worktree lives — the same two-module set `evidence` itself is held to. No store, no
catalog, and above all NO `gh`: what GitHub says about a pull request is passed in by the
caller that already asked.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import worker_session
from .evidence import ProjectRef, base_ref

log = logging.getLogger("jarvis.landing")

#: Verdicts. Every one of them is a statement about a PULL REQUEST, which is why none of
#: the old content vocabulary survived: `stranded`, `partial` and `unknown` were shapes
#: of a measurement, and there is no measurement here any more.
#:
#: `NO_PULL_REQUEST` is not a shrug and not an unknown — it is this invariant saying the
#: question is not its to ask. See the module docstring on what that gives up.
LANDED = "landed"
AWAITING_MERGE = "awaiting-merge"
REFUSED = "refused"
NO_PULL_REQUEST = "no-pull-request"

#: Verdicts that mean a human owes this work order a decision. `REFUSED` is one of them
#: for a reason worth spelling out: a closed pull request is a decision somebody ALREADY
#: took, but they took it on GitHub, where the work order cannot see it — so the order is
#: still claiming `completed` over work nothing landed.
UNSETTLED_VERDICTS = (AWAITING_MERGE, REFUSED)

#: How long a reading of a pull request stands before it is stale — a week. It lives HERE
#: rather than beside the daemon's cadences because BOTH halves need it and they must not
#: disagree: `Daemon.refresh_landings` re-asks past it, and `invariants.check_work_lands`
#: reports that it has no current answer past it. Two copies of this number would let the
#: audit go quiet at exactly the moment it stopped knowing anything.
#:
#: Not "for ever", which is what a MERGED answer looks like it could be: `landed` is the
#: verdict that SILENCES the check, so the one answer nobody would re-read is the one that
#: would hide a revert. A week is far inside the window that matters — the orders GitHub
#: issue #232 found had been unmerged for seven — and costs a mature project a handful of
#: round trips a day.
FRESH_FOR_SECONDS = 7 * 24 * 3600

#: How many work orders one landing sweep may ask GitHub about. The cap is about the
#: TICK, not about the day: a project with two hundred completed orders and a cold
#: timeline would otherwise spend two hundred round trips inside one tick, and the daemon
#: has everything else to do. At this size a cold project fills in over about eight hourly
#: sweeps and a warm one never touches the cap.
#:
#: Here rather than beside the daemon's cadences for `FRESH_FOR_SECONDS`' reason: the
#: audit quotes it when it reports how much it has not read yet, so a reader can tell "the
#: backlog is draining" from "the daemon is dead".
REFRESH_PER_SWEEP = 25

#: URL userinfo — `https://x-access-token:TOKEN@github.com/…` — which is the ONLY place
#: a credential appears in git's text, so one pattern covers it rather than a list of
#: message shapes. See `_scrub`.
_CREDENTIALS_RE = re.compile(r"://[^/\s@]+@")

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

        `ops.finish` now writes it on EVERY settling, not only the two that refuse —
        this is the only durable answer to "did that order write code", and the worktree
        it is read from is deleted long before anyone audits (`authored_in`).
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


#: The events a settlement writes, any of which may carry an `Authored` record. Read
#: newest-first by `latest_authorship`, which is how INV-PR-RECORDED knows months later
#: that an order wrote code without going anywhere near the repository.
SETTLEMENT_EVENTS = ("finished", "abandoned", "work_unlanded", "feature_settled")


def authored_in(payload: dict[str, object]) -> Authored | None:
    """The `Authored` a settlement event recorded, or None if it recorded none.

    TWO SHAPES, because the three writers were not built together and unifying them now
    would rewrite payloads `work_unlanded_open`, `true_blockers` and the timeline
    renderer already read. `ops.finish` nests it under `authored` beside its summary;
    `park_unlanded` and `finish --abandon` spread it at the top level beside their own
    `was`/`reason`. Both are the output of `Authored.record`, so one reader serves both.

    None means NOBODY LOOKED — an order that settled before this shipped, or one whose
    worktree could not be read. It is not "produced nothing": `commits: 0` says that, and
    the two must stay distinguishable or the invariant that reads this would treat an
    unreadable settling as an exoneration.
    """
    inner = payload.get("authored")
    if isinstance(inner, dict):
        payload = inner
    if "commits" not in payload:
        return None
    dirty = payload.get("dirty")
    count = payload.get("commits")
    return Authored(branch=str(payload.get("branch") or ""),
                    base=str(payload.get("base") or ""),
                    commits=count if isinstance(count, int) else 0,
                    dirty=tuple(str(f) for f in dirty) if isinstance(dirty, list) else ())


def latest_authorship(events: list[tuple[float, dict[str, object]]]) -> Authored | None:
    """The newest `Authored` among these settlement events, or None if none carries one.

    NEWEST WINS, the episode arithmetic `ProjectStore.work_abandoned` uses and for its
    reason: a work order sent back and re-delivered has authored something different by
    the end, and the record of what it produced must move with it. Ties go to the last
    one seen, which is `events_of_kind`'s own order.
    """
    found: Authored | None = None
    newest = float("-inf")
    for ts, payload in events:
        record = authored_in(payload)
        if record is not None and ts >= newest:
            found, newest = record, ts
    return found


def worktree_of(project_path: Path, wo: dict[str, object]) -> Path | None:
    """Where this work order's worktree lives, or None if it is not on disk.

    One line, and it exists so that the callers of this module do not each assemble
    `worker_session.worktree_path`'s `ProjectSpec` stand-in for themselves. Three copies
    of a two-attribute shim is how two checks end up disagreeing about where a work
    order's code is, which would be a peculiar bug for THIS module to have.
    """
    # type: ignore — `ProjectRef` carries the one attribute that helper reads.
    return worker_session.worktree_path(ProjectRef(project_path), wo)  # type: ignore[arg-type]


def authored(worktree: Path | None) -> Authored:
    """Has this worktree produced anything? Exact, local, and cheap enough to always run.

    Three `git` invocations and no network. Every failure — no worktree, no git, no base
    to compare against, a command that errored — comes back as `unreadable` with
    `produced` False, because this is the predicate a refusal is built on: a work order
    must never be unable to finish because the OS could not run `git`. The failure still
    goes in the log (`_git`).
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
    # module and never staged it has produced exactly the thing this exists to catch.
    # `--untracked-files=all` because the default COLLAPSES an untracked directory to one
    # entry — a worker that wrote a whole new package would report as `src/`, and the
    # refusal would say "1 uncommitted file" about forty.
    status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    if branch is None or count is None or status is None:
        return Authored(unreadable="git could not read the worktree")
    dirty = tuple(line[3:] for line in status.splitlines() if line[3:])
    return Authored(branch=branch.strip(), base=base,
                    commits=int(count.strip()) if count.strip().isdigit() else 0,
                    dirty=dirty)


@dataclass(frozen=True)
class Landing:
    """The audit's verdict on one work order, and the pull request behind it.

    `pr_url` is the pull request the verdict is ABOUT, and it is always the one the work
    order recorded — never one this module went looking for. See the module docstring on
    why that is a layering decision and not an oversight.
    """

    wo_id: str
    verdict: str
    detail: str
    pr_url: str = ""
    #: GitHub's own enum as `github.PullRequest` reports it: OPEN, MERGED or CLOSED.
    pr_state: str = ""

    @property
    def unsettled(self) -> bool:
        return self.verdict in UNSETTLED_VERDICTS

    def record(self) -> dict[str, object]:
        """This, as a `landing_seen` payload. `from_record` is the other half."""
        return {"verdict": self.verdict, "detail": self.detail, "pr_url": self.pr_url,
                "pr_state": self.pr_state}


def from_record(wo_id: str, payload: dict[str, Any]) -> Landing:
    """A `landing_seen` payload read back. The inverse of `Landing.record`.

    Defensive about every field, because this crosses a JSON round trip and an event
    written by an older release is a shape nobody can change afterwards. An unreadable
    payload becomes `NO_PULL_REQUEST` — the silent verdict — for the module docstring's
    reason: this audit errs towards saying nothing, never towards a complaint it cannot
    substantiate.
    """
    verdict = str(payload.get("verdict") or "")
    if verdict not in (LANDED, AWAITING_MERGE, REFUSED, NO_PULL_REQUEST):
        return Landing(wo_id, NO_PULL_REQUEST,
                       detail=f"unreadable landing record ({payload!r})")
    return Landing(wo_id, verdict, detail=str(payload.get("detail") or ""),
                   pr_url=str(payload.get("pr_url") or ""),
                   pr_state=str(payload.get("pr_state") or ""))


def judge(wo_id: str, pr_url: str, state: str) -> Landing:
    """Did this work order's recorded pull request land? `state` is what GitHub said.

    One pull request, one state, no arithmetic. `state` is `github.PullRequest.state`,
    fetched by the caller — this module never asks for it itself, for the reason the
    module docstring gives.

    - **MERGED** -> `LANDED`. Merged is the answer. Nothing is measured against it: the
      whole point of the 2026-09-18 rewrite is that a merged pull request stays merged
      however far the default branch is refactored afterwards.
    - **OPEN** -> `AWAITING_MERGE`. The work is delivered and waiting on a merge, which
      is a different sentence from "stranded" and the remedy differs with it.
    - **CLOSED**, or anything else GitHub says -> `REFUSED`. Delivered, and somebody said
      no on GitHub where the work order cannot see it. An unrecognised state lands here
      rather than in silence: a state this module does not know is a reason to look.
    - **no `pr_url`** -> `NO_PULL_REQUEST`, out of scope and silent. INV-PR-RECORDED's
      question, not this one's.
    """
    pr_url = (pr_url or "").strip()
    state = (state or "").strip().upper()
    if not pr_url:
        return Landing(wo_id, NO_PULL_REQUEST,
                       detail="this work order recorded no pull request")
    if state == "MERGED":
        return Landing(wo_id, LANDED, pr_url=pr_url, pr_state=state,
                       detail=f"{pr_url} merged")
    if state == "OPEN":
        return Landing(wo_id, AWAITING_MERGE, pr_url=pr_url, pr_state=state,
                       detail=f"the work is delivered and waiting on a merge: {pr_url} "
                              f"is still open")
    return Landing(wo_id, REFUSED, pr_url=pr_url, pr_state=state or "UNKNOWN",
                   detail=f"the work was delivered and refused: {pr_url} was closed "
                          f"without merging")


# --------------------------------------------------------------------------- internals

def _scrub(text: str) -> str:
    """`text` with any URL userinfo replaced. Every log line carrying git's stderr uses it.

    **KEPT AFTER THE CODE THAT MOTIVATED IT WAS DELETED, DELIBERATELY.** It arrived with
    `landing._fetch` (kn-4bba177d): git quotes the remote URL back on failure —
    `fatal: Authentication failed for https://x-access-token:TOKEN@github.com/...` — so
    any line carrying fetch stderr writes a token into the daemon log for every project
    whose `origin` embeds one. Truncating to 200 characters does not help: the URL is on
    the FIRST line. `_fetch` went with the content machinery on 2026-09-18 and no caller
    of `_git` touches a remote today, which is exactly the argument for keeping this
    rather than dropping it — the guard is four lines, the next remote-touching caller
    will not think about it, and it was the fleet's ONLY credential scrub.

    One pattern, because credentials only ever appear as userinfo.

    NOTE for whoever tests the next leak: git SELF-redacts on a connection failure and
    does NOT on an authentication failure, so an offline end-to-end test cannot reach the
    leaking message. Pair it with a unit assertion against the verbatim string.
    """
    return _CREDENTIALS_RE.sub("://<redacted>@", text)


def _git(repo: Path, *args: str) -> str | None:
    """One read-only git command in `repo`. Its stdout, or None if it FAILED.

    Never raises, for `evidence._git`'s reason and one of its own: this module's caller is
    often a worker trying to finish, and a repository with no `origin`, no commits or no
    git at all must produce a thin answer rather than an exception that strands it.

    But it does not return "" for a failure either, because every caller here reads the
    output as DATA and "" is a meaningful datum: no commits ahead, nothing uncommitted. A
    transient failure smuggled in as "" would make `authored` report `produced=False` and
    let a settling strand real work. So the failure is a separate value the caller has to
    handle, and it is logged: this module is otherwise silent by design, and a check that
    quietly stopped working would look exactly like a fleet with nothing stranded.
    """
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, errors="replace", check=False)
    except OSError as exc:
        log.warning("git %s in %s could not run: %s", " ".join(args), repo,
                    _scrub(str(exc)))
        return None
    if proc.returncode != 0:
        # `_scrub` BEFORE the truncation and not after: a token sits in the URL on the
        # first line, so slicing to 200 characters keeps it rather than cutting it off.
        log.warning("git %s in %s exited %d: %s", " ".join(args), repo,
                    proc.returncode, _scrub(proc.stderr.strip())[:200])
        return None
    return proc.stdout
