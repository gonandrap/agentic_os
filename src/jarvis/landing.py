"""Did the code a work order wrote ever reach the default branch? (docs/superpowers/specs/2026-09-13-a-finished-order-proves-its-code-landed.md §2)"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import worker_session
from .evidence import ProjectRef, base_ref, default_branch_head

log = logging.getLogger("jarvis.landing")

#: Verdicts
LANDED = "landed"
AWAITING_MERGE = "awaiting-merge"
REFUSED = "refused"
NO_PULL_REQUEST = "no-pull-request"

#: Verdicts that mean a human owes this work order a decision
UNSETTLED_VERDICTS = (AWAITING_MERGE, REFUSED)

#: How long a reading of a pull request stands before it is stale — a week (#232)
FRESH_FOR_SECONDS = 7 * 24 * 3600

#: How many work orders one landing sweep may ask GitHub about
REFRESH_PER_SWEEP = 25

#: URL userinfo — `https://x-access-token:TOKEN@github.com/…`
_CREDENTIALS_RE = re.compile(r"://[^/\s@]+@")

#: A GitHub pull-request URL sitting in prose (#232)
PR_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']*?/pull/\d+")


def pr_urls_in(text: str) -> tuple[str, ...]:
    """Every pull-request URL named in `text`, in order, deduplicated (#33)"""
    seen: dict[str, None] = {}
    for match in PR_URL_RE.finditer(text or ""):
        seen.setdefault(match.group(0).rstrip(".,;:"), None)
    return tuple(seen)


@dataclass(frozen=True)
class Authored:
    """What a work order's worktree has produced that is not yet anywhere else (#232)"""

    branch: str = ""
    base: str = ""
    commits: int = 0
    dirty: tuple[str, ...] = ()
    #: Why there is no answer, when there is none
    unreadable: str = ""

    @property
    def produced(self) -> bool:
        """True when settling this work order would strand something"""
        return bool(self.commits or self.dirty)

    def record(self) -> dict[str, object]:
        """This, as an event payload (#232)"""
        return {"branch": self.branch, "base": self.base, "commits": self.commits,
                "dirty": list(self.dirty)}

    def describe(self) -> str:
        """One clause naming what exists, for the refusal that quotes it"""
        parts = []
        if self.commits:
            parts.append(f"{self.commits} commit{'s' if self.commits != 1 else ''} "
                         f"on `{self.branch or 'its branch'}`")
        if self.dirty:
            parts.append(f"{len(self.dirty)} uncommitted file"
                         f"{'s' if len(self.dirty) != 1 else ''}")
        return " and ".join(parts) or "nothing"


#: The events a settlement writes, any of which may carry an `Authored` record
SETTLEMENT_EVENTS = ("finished", "abandoned", "work_unlanded", "feature_settled")


def authored_in(payload: dict[str, object]) -> Authored | None:
    """The `Authored` a settlement event recorded, or None if it recorded none"""
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
    """The newest `Authored` among these settlement events, or None if none carries one"""
    found: Authored | None = None
    newest = float("-inf")
    for ts, payload in events:
        record = authored_in(payload)
        if record is not None and ts >= newest:
            found, newest = record, ts
    return found


def worktree_of(project_path: Path, wo: dict[str, object]) -> Path | None:
    """Where this work order's worktree lives, or None if it is not on disk"""
    # type: ignore — `ProjectRef` carries the one attribute that helper reads.
    return worker_session.worktree_path(ProjectRef(project_path), wo)  # type: ignore[arg-type]


def authored(worktree: Path | None, artifacts: tuple[str, ...] = ()) -> Authored:
    """Has this worktree produced anything, its kind's own `artifacts` aside? (io-b521d17b)"""
    if worktree is None or not worktree.is_dir():
        return Authored(unreadable="no worktree on disk")
    base = base_ref(worktree)
    if not base:
        # Rung 4 of the pinned ladder
        return Authored(unreadable="no default branch to compare against")
    branch = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    count = _git(worktree, "rev-list", "--count", f"{base}..HEAD")
    # `--porcelain` lists untracked files too
    status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    if branch is None or count is None or status is None:
        return Authored(unreadable="git could not read the worktree")
    dirty = tuple(line[3:] for line in status.splitlines()
                  if line[3:] and line[3:] not in artifacts)
    return Authored(branch=branch.strip(), base=base,
                    commits=int(count.strip()) if count.strip().isdigit() else 0,
                    dirty=dirty)


def committed_text(project_path: Path, planner_wo: dict[str, Any] | None,
                   repo_path: str) -> tuple[str, str] | None:
    """One file as COMMITTED, with human-readable provenance, or None (docs/superpowers/specs/2026-09-25-plan-review-reads-the-spec-the-os-holds.md)"""
    worktree = worktree_of(project_path, planner_wo) if planner_wo else None
    if worktree is not None:
        branch = authored(worktree).branch
        if branch:
            text = _git(worktree, "show", f"{branch}:{repo_path}")
            if text is not None:
                sha = (_git(worktree, "rev-parse", "--short", branch) or "").strip()
                return text, f"branch {branch} @ {sha}" if sha else f"branch {branch}"
    ref = base_ref(project_path)
    if not ref:
        return None
    _fetch_ref(project_path, ref)
    text = _git(project_path, "show", f"{ref}:{repo_path}")
    if text is None:
        return None
    head = default_branch_head(project_path)
    return text, f"{ref} @ {head[:7]}" if head else ref


@dataclass(frozen=True)
class CommittedAt:
    """Which committed ref answered an existence question (docs/superpowers/specs/2026-10-14-inv-spec-pointer-real-reads-the-integration-branch.md)"""
    repo: Path
    ref: str
    present: bool


def committed_exists(project_path: Path, planner_wo: dict[str, Any] | None,
                     repo_path: str, fetched: set[str]) -> CommittedAt | None:
    """Does this path exist on a COMMITTED ref? (docs/superpowers/specs/2026-10-14-inv-spec-pointer-real-reads-the-integration-branch.md)"""
    answered: CommittedAt | None = None
    worktree = worktree_of(project_path, planner_wo) if planner_wo else None
    if worktree is not None:
        branch = authored(worktree).branch
        if branch:
            seen = _object_exists(worktree, branch, repo_path)
            if seen:
                return CommittedAt(worktree, branch, True)
            if seen is False:
                answered = CommittedAt(worktree, branch, False)
    ref = base_ref(project_path)
    if not ref:
        return answered
    if ref not in fetched:
        fetched.add(ref)
        _fetch_ref(project_path, ref)
    seen = _object_exists(project_path, ref, repo_path)
    if seen is None:
        return answered
    return CommittedAt(project_path, ref, seen)


def _object_exists(repo: Path, ref: str, repo_path: str) -> bool | None:
    """`git cat-file -e <ref>:<path>`, silent on the miss"""
    try:
        proc = subprocess.run(["git", "-C", str(repo), "cat-file", "-e",
                               f"{ref}:{repo_path}"], capture_output=True, text=True,
                              errors="replace", check=False)
    except OSError as exc:
        log.warning("git cat-file -e %s:%s in %s could not run: %s", ref, repo_path,
                    repo, _scrub(str(exc)))
        return None
    return proc.returncode == 0


def _fetch_ref(repo: Path, ref: str) -> None:
    """Refresh the default branch before reading it, best-effort. §8"""
    branch = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
    try:
        proc = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin",
                               branch], capture_output=True, text=True,
                              errors="replace", timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("git fetch origin %s in %s could not run: %s", branch, repo,
                    _scrub(str(exc)))
        return
    if proc.returncode != 0:
        log.debug("git fetch origin %s in %s exited %d: %s", branch, repo,
                  proc.returncode, _scrub(proc.stderr.strip())[:200])


@dataclass(frozen=True)
class Landing:
    """The audit's verdict on one work order, and the pull request behind it"""

    wo_id: str
    verdict: str
    detail: str
    pr_url: str = ""
    #: GitHub's own enum as `github.PullRequest` reports it: OPEN, MERGED or CLOSED
    pr_state: str = ""

    @property
    def unsettled(self) -> bool:
        return self.verdict in UNSETTLED_VERDICTS

    def record(self) -> dict[str, object]:
        """This, as a `landing_seen` payload"""
        return {"verdict": self.verdict, "detail": self.detail, "pr_url": self.pr_url,
                "pr_state": self.pr_state}


def from_record(wo_id: str, payload: dict[str, Any]) -> Landing:
    """A `landing_seen` payload read back"""
    verdict = str(payload.get("verdict") or "")
    if verdict not in (LANDED, AWAITING_MERGE, REFUSED, NO_PULL_REQUEST):
        return Landing(wo_id, NO_PULL_REQUEST,
                       detail=f"unreadable landing record ({payload!r})")
    return Landing(wo_id, verdict, detail=str(payload.get("detail") or ""),
                   pr_url=str(payload.get("pr_url") or ""),
                   pr_state=str(payload.get("pr_state") or ""))


def judge(wo_id: str, pr_url: str, state: str) -> Landing:
    """Did this work order's recorded pull request land?"""
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
    """`text` with any URL userinfo replaced"""
    return _CREDENTIALS_RE.sub("://<redacted>@", text)


def _git(repo: Path, *args: str) -> str | None:
    """One read-only git command in `repo`"""
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, errors="replace", check=False)
    except OSError as exc:
        log.warning("git %s in %s could not run: %s", " ".join(args), repo,
                    _scrub(str(exc)))
        return None
    if proc.returncode != 0:
        # `_scrub` BEFORE the truncation and not after: a token sits in the URL on the first line
        log.warning("git %s in %s exited %d: %s", " ".join(args), repo,
                    proc.returncode, _scrub(proc.stderr.strip())[:200])
        return None
    return proc.stdout
