"""Reading a pull request — its state for the poll loop, its whole self for the panel (docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md §2)"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bugreport import gh_bin, gh_missing_message

log = logging.getLogger("jarvis.github")

#: `gh pr view` is a single API round trip
GH_TIMEOUT = 30

#: Every `gh` subcommand this module is allowed to run
READ_ONLY_VERBS = (("pr", "view"), ("pr", "diff"), ("pr", "list"),
                   ("api", "--method"))

#: The git subcommands this module runs (#995)
GIT_READS = (("remote", "get-url", "origin"), ("ls-remote", "--heads"))


class GitHubError(RuntimeError):
    """The state of the pull request could not be read"""

    #: What the packet is allowed to say
    URL_REFUSED = ("the recorded pull-request URL is not one this project is allowed "
                   "to fetch")
    NO_GH = "the `gh` CLI is not installed where the OS can reach it"
    TIMEOUT = "the request to GitHub timed out"
    REFUSED = "GitHub refused the request, or the pull request does not exist"
    UNREADABLE = "GitHub's answer could not be understood"

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason or self.REFUSED


class UntrustedPullRequest(GitHubError):
    """The URL is not a pull request on this project's own repository"""


class GhUnavailable(GitHubError):
    """There is no `gh` to run at all — as opposed to one that ran and refused (#90)"""


#: The ONLY shape a pull-request URL may have before it becomes an argument
PR_URL_RE = re.compile(
    r"^https://([A-Za-z0-9.-]+)/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/[0-9]+$")


def checked_pr_url(url: str, cwd: Path | None = None) -> str:
    """`url` if this project may fetch it"""
    match = PR_URL_RE.match(url or "")
    if match is None:
        raise UntrustedPullRequest(
            f"{url!r} is not a pull-request URL this OS will fetch",
            GitHubError.URL_REFUSED)
    _host, owner, repo = match.group(1), match.group(2), match.group(3)
    origin = origin_repo(cwd)
    if origin is not None and origin != (owner.lower(), repo.lower()):
        raise UntrustedPullRequest(
            f"{url!r} is on {owner}/{repo}, but this project's origin is "
            f"{origin[0]}/{origin[1]}", GitHubError.URL_REFUSED)
    return url


def _origin_url(cwd: Path | None) -> str:
    """This checkout's `origin` remote URL, or "" if unreadable; the one `LOCAL_GIT_READS` site"""
    if cwd is None or not Path(cwd).is_dir():
        return ""
    try:
        proc = subprocess.run(["git", "-C", str(cwd), "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=GH_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def origin_repo(cwd: Path | None) -> tuple[str, str] | None:
    """`(owner, repo)` lowercased for this checkout's `origin`, or None if unreadable; host discarded"""
    remote = _origin_url(cwd).removesuffix(".git")
    if not remote:
        return None
    parts = remote.replace(":", "/").rstrip("/").split("/")
    if len(parts) < 2 or not parts[-1] or not parts[-2]:
        return None
    return parts[-2].lower(), parts[-1].lower()


def origin_host(cwd: Path | None) -> str:
    """The host `origin` points at, lowercased; "" for a local path remote or none"""
    remote = _origin_url(cwd)
    if not remote:
        return ""
    authority = remote.split("://", 1)[-1].split("@", 1)[-1]
    return authority.split("/", 1)[0].split(":", 1)[0].lower()


GITHUB_HOST = "github.com"


def compare_url(cwd: Path | None, base: str, branch: str) -> str | None:
    """GitHub compare URL for `base...branch` from local git only, or None (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md §3)"""
    if cwd is None or not base or not branch:
        return None
    if origin_host(cwd) != GITHUB_HOST:
        return None
    origin = origin_repo(cwd)
    if origin is None:
        return None
    return (f"https://{GITHUB_HOST}/{origin[0]}/{origin[1]}/compare/"
            f"{base.removeprefix('origin/')}...{branch}?expand=1")


def blob_url(cwd: Path | None, repo_path: str) -> str | None:
    """`https://github.com/{owner}/{repo}/blob/{sha}/{repo_path}`, or None (docs/superpowers/specs/2026-09-28-a-feature-spec-you-can-open.md)"""
    from .landing import base_ref, default_branch_head  # §6: landing's, not evidence's

    if cwd is None or not repo_path:
        return None
    origin = origin_repo(cwd)
    if origin is None:
        return None
    if not base_ref(Path(cwd)):
        return None
    sha = default_branch_head(Path(cwd))
    if not sha:
        return None
    return f"https://github.com/{origin[0]}/{origin[1]}/blob/{sha}/{repo_path}"


#: The fields of one `gh pr view --json …` (#232)
PR_FIELDS = ("state,mergedAt,mergeable,mergeStateStatus,baseRefName,baseRefOid,"
             "statusCheckRollup,headRefOid,headRefName,mergeCommit")

#: A check conclusion that means THE CODE IS WRONG — as opposed to merely not green
RED_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"})

#: A check that stopped without judging the code: re-run it, never blame the worker (#1123)
ABORTED_CONCLUSIONS = frozenset({"CANCELLED", "STARTUP_FAILURE", "STALE"})

#: A check that has not finished
UNFINISHED_STATUSES = frozenset({"QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED",
                                 "PENDING", "EXPECTED"})


def read_checks(payload: dict[str, Any]) -> tuple[dict[str, str], ...]:
    """`statusCheckRollup`, normalised (#224)"""
    return tuple(
        {"name": str(c.get("name") or c.get("context") or ""),
         "status": str(c.get("status") or c.get("state") or ""),
         "conclusion": str(c.get("conclusion") or c.get("state") or ""),
         "started_at": str(c.get("startedAt") or c.get("createdAt") or ""),
         "workflow": str(c.get("workflowName") or "")}
        for c in (payload.get("statusCheckRollup") or [])
        if isinstance(c, dict))


def red_checks(checks: tuple[dict[str, str], ...]) -> tuple[dict[str, str], ...]:
    """The checks that say the code is wrong, WHOLE (#224)"""
    return tuple(c for c in checks if c["conclusion"].upper() in RED_CONCLUSIONS)


def failing_checks(checks: tuple[dict[str, str], ...]) -> tuple[str, ...]:
    """The names of the checks that say the code is wrong"""
    return tuple(c["name"] or "(unnamed check)" for c in red_checks(checks))


@dataclass(frozen=True)
class PullRequest:
    """What `gh pr view --json <PR_FIELDS>` says, and nothing more"""

    state: str
    merged_at: str | None = None
    #: GitHub's three-way mergeability: CONFLICTING, MERGEABLE or UNKNOWN
    mergeable: str | None = None
    base_ref: str | None = None
    #: `mergeStateStatus`: BEHIND, BLOCKED, CLEAN, DIRTY, DRAFT, HAS_HOOKS, UNKNOWN or UNSTABLE
    merge_state: str | None = None
    #: One entry per check, through `read_checks`
    checks: tuple[dict[str, str], ...] = ()
    #: `headRefOid`: the commit at the head of the pull request AT VIEW TIME
    head_oid: str = ""
    #: `baseRefOid`: the base branch's head commit as GitHub sees it, on the same read (docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §5.1)
    base_oid: str = ""
    #: `headRefName`: the branch this pull request is FROM (#995)
    head_ref: str = ""
    #: `mergeCommit.oid`: the commit THIS PULL REQUEST PUT ON ITS BASE (docs/superpowers/specs/2026-09-26-a-release-order-overtaken-mid-ci-wait-settles-itself.md §2)
    merge_commit_oid: str = ""

    @property
    def merged(self) -> bool:
        return self.state == "MERGED"

    @property
    def closed_unmerged(self) -> bool:
        return self.state == "CLOSED"

    @property
    def conflicting(self) -> bool:
        """The branch cannot be merged into its base"""
        return self.mergeable == "CONFLICTING"

    @property
    def mergeable_now(self) -> bool:
        """GitHub positively says it merges — as opposed to "not known to conflict\""""
        return self.mergeable == "MERGEABLE"

    @property
    def failing(self) -> tuple[str, ...]:
        """The checks that say the code is wrong, by name"""
        return failing_checks(self.checks)

    @property
    def red(self) -> tuple[dict[str, str], ...]:
        """The same checks, whole — what `ci.inherited` needs"""
        return red_checks(self.checks)

    @property
    def unfinished(self) -> tuple[str, ...]:
        """The checks that have not finished yet, by name (#224)"""
        return tuple(c["name"] or "(unnamed check)" for c in self.checks
                     if c["status"].upper() in UNFINISHED_STATUSES)

    @property
    def aborted(self) -> tuple[dict[str, str], ...]:
        """The checks that finished without a verdict, whole (#1123)"""
        return tuple(c for c in self.checks
                     if c["conclusion"].upper() in ABORTED_CONCLUSIONS)

    @property
    def checks_green(self) -> bool:
        """CI positively passed — as opposed to "nothing is currently failing\""""
        return bool(self.checks) and not self.failing and not self.unfinished

    @property
    def behind(self) -> bool:
        """The branch is behind its base, WHEN GITHUB SAYS SO — which it usually does not"""
        return self.merge_state == "BEHIND"


def _run(args: list[str], *, url: str, cwd: Path | None,
         missing_hint: str) -> str:
    """One `gh` read, as stdout"""
    where = str(cwd) if cwd and Path(cwd).is_dir() else None
    shown = "`gh " + " ".join(args[:2]) + f" {url}`"
    try:
        proc = subprocess.run([gh_bin(), *args], capture_output=True, text=True,
                              timeout=GH_TIMEOUT, cwd=where)
    except FileNotFoundError as e:
        raise GhUnavailable(gh_missing_message(missing_hint),
                            GitHubError.NO_GH) from e
    except subprocess.TimeoutExpired as e:
        raise GitHubError(f"{shown} timed out after {GH_TIMEOUT}s",
                          GitHubError.TIMEOUT) from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        # The remote text is LOGGED and not raised into `reason` — see `GitHubError`
        log.info("%s failed: %s", shown, detail)
        raise GitHubError(f"{shown} failed: {detail}", GitHubError.REFUSED)
    return proc.stdout or ""


def pr_view(url: str, cwd: Path | None = None) -> PullRequest:
    """Read the state of the pull request at `url`"""
    url = checked_pr_url(url, cwd)
    stdout = _run(["pr", "view", url, "--json", PR_FIELDS], url=url, cwd=cwd,
                  missing_hint="so Jarvis cannot see when a pull request merges")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(
            f"`gh pr view {url}` returned no JSON ({stdout[:200]!r})",
            GitHubError.UNREADABLE) from e
    state = str(payload.get("state") or "").upper()
    if not state:
        raise GitHubError(f"`gh pr view {url}` reported no state ({payload!r})",
                          GitHubError.UNREADABLE)
    # Only `state` is required above: `mergeable` arrives as null on a merged or closed pull request and...
    return PullRequest(
        state=state,
        merged_at=payload.get("mergedAt") or None,
        mergeable=str(payload["mergeable"]).upper() if payload.get("mergeable") else None,
        base_ref=payload.get("baseRefName") or None,
        merge_state=(str(payload["mergeStateStatus"]).upper()
                     if payload.get("mergeStateStatus") else None),
        checks=read_checks(payload),
        head_oid=str(payload.get("headRefOid") or ""),
        base_oid=str(payload.get("baseRefOid") or ""),
        head_ref=str(payload.get("headRefName") or ""),
        # A nested object, so one `or {}` guard — spec §2
        merge_commit_oid=str((payload.get("mergeCommit") or {}).get("oid") or ""),
    )


def ls_remote(cwd: Path | None = None, remote: str = "origin") -> dict[str, str]:
    """`{branch: sha}` for every head on the REMOTE (#995)"""
    where = str(cwd) if cwd and Path(cwd).is_dir() else None
    if not BRANCH_RE.match(remote):
        raise GitHubError(f"refusing to ask about remote {remote!r}",
                          GitHubError.URL_REFUSED)
    try:
        proc = subprocess.run(["git", "-C", where or ".", "ls-remote", "--heads",
                               remote],
                              capture_output=True, text=True, timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise GitHubError(f"`git ls-remote {remote}` timed out after {GH_TIMEOUT}s",
                          GitHubError.TIMEOUT) from e
    except (OSError, subprocess.SubprocessError) as e:
        raise GitHubError(f"`git ls-remote {remote}` could not be run: {e}",
                          GitHubError.REFUSED) from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        log.info("`git ls-remote %s` failed: %s", remote, detail)
        raise GitHubError(f"`git ls-remote {remote}` failed: {detail}",
                          GitHubError.REFUSED)
    heads: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        sha, _, ref = line.partition("\t")
        if sha.strip() and ref.startswith("refs/heads/"):
            heads[ref[len("refs/heads/"):].strip()] = sha.strip()
    return heads


def open_pull_request_for_branch(branch: str, cwd: Path | None = None, *,
                                 head_sha: str = "") -> str:
    """The OPEN pull request on `branch`, or "" when there is none (docs/superpowers/specs/2026-09-27-a-planner-submits-behind-its-spec-pull-request.md)"""
    if not BRANCH_RE.match(branch or ""):
        raise GitHubError(f"{branch!r} is not a branch this may ask about",
                          GitHubError.URL_REFUSED)
    fields = "url,isCrossRepository,headRefOid" if head_sha else "url"
    stdout = _run(["pr", "list", "--head", branch, "--state", "open", "--json", fields],
                  url=branch, cwd=cwd,
                  missing_hint="so Jarvis cannot confirm a planner's pull request")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(f"`gh pr list {branch}` returned no JSON ({stdout[:200]!r})",
                          GitHubError.UNREADABLE) from e
    if not isinstance(payload, list):
        raise GitHubError(f"`gh pr list {branch}` returned no array ({payload!r})",
                          GitHubError.UNREADABLE)
    if head_sha:
        # `--head` matches a fork's branch of the same name too
        payload = [row for row in payload if isinstance(row, dict)
                   and row.get("isCrossRepository") is False
                   and row.get("headRefOid") == head_sha]
        if len(payload) != 1:
            return ""
    if not payload:
        return ""
    first = payload[0] if isinstance(payload[0], dict) else {}
    url = str(first.get("url") or "")
    if not url:
        raise GitHubError(f"`gh pr list {branch}` reported no url ({payload!r})",
                          GitHubError.UNREADABLE)
    return checked_pr_url(url, cwd)


#: A branch ref that may become a path segment
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

#: GitHub's own 404 body for a branch nobody protects
NOT_PROTECTED_RE = re.compile(r"not protected", re.IGNORECASE)


@dataclass(frozen=True)
class BranchProtection:
    """What protects a branch, reduced to the one fact the auto-merge request states"""

    required_checks: tuple[str, ...] = ()


def branch_protection(owner_repo: tuple[str, str], base: str,
                      cwd: Path | None = None) -> BranchProtection | None:
    """What protects `base`"""
    owner, repo = owner_repo
    if not BRANCH_RE.match(base or ""):
        raise GitHubError(f"{base!r} is not a branch this may ask about",
                          GitHubError.URL_REFUSED)
    path = f"repos/{owner}/{repo}/branches/{base}/protection"
    try:
        stdout = _run(["api", "--method", "GET", path], url=path, cwd=cwd,
                      missing_hint="so Jarvis cannot read branch protection")
    except GhUnavailable:
        raise
    except GitHubError as e:
        if NOT_PROTECTED_RE.search(str(e)):
            return None
        raise
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(f"`gh api {path}` returned no JSON ({stdout[:200]!r})",
                          GitHubError.UNREADABLE) from e
    required = payload.get("required_status_checks") or {}
    names = [str(c) for c in (required.get("contexts") or [])]
    names += [str(c.get("context") or "") for c in (required.get("checks") or [])
              if isinstance(c, dict)]
    return BranchProtection(required_checks=tuple(dict.fromkeys(n for n in names if n)))


#: The fields of one `gh pr view --json …` for the PANEL
ARTIFACT_FIELDS = ("number,title,body,state,isDraft,baseRefName,headRefName,headRefOid,"
                   "url,additions,deletions,changedFiles,files,statusCheckRollup")


@dataclass(frozen=True)
class PullRequestArtifact:
    """A pull request as the validation panel judges it"""

    url: str
    number: int
    title: str
    body: str
    state: str
    draft: bool
    base_ref: str
    head_ref: str
    #: `headRefOid` — the commit this artifact IS
    head_oid: str
    additions: int
    deletions: int
    files: tuple[str, ...]
    #: `git diff --stat`-shaped, rebuilt from the `files` GitHub reports
    stat: str
    #: One entry per check run: `{"name"
    checks: tuple[dict[str, str], ...]
    #: The unified diff, exactly as `gh pr diff` prints it
    diff: str


def _stat_block(files: list[dict[str, Any]]) -> str:
    """`git diff --stat`'s shape, rebuilt from GitHub's per-file counts"""
    if not files:
        return ""
    width = max(len(str(f.get("path") or "")) for f in files)
    lines, adds, dels = [], 0, 0
    for f in files:
        a, d = int(f.get("additions") or 0), int(f.get("deletions") or 0)
        adds, dels = adds + a, dels + d
        lines.append(f" {str(f.get('path') or ''):<{width}} | {a + d:>5} "
                     f"{'+' * min(a, 40)}{'-' * min(d, 40)}")
    lines.append(f" {len(files)} file(s) changed, {adds} insertion(s)(+), "
                 f"{dels} deletion(s)(-)")
    return "\n".join(lines) + "\n"


def pr_artifact(url: str, cwd: Path | None = None) -> PullRequestArtifact:
    """The pull request at `url`, whole"""
    url = checked_pr_url(url, cwd)
    stdout = _run(["pr", "view", url, "--json", ARTIFACT_FIELDS], url=url, cwd=cwd,
                  missing_hint="so the validation panel cannot read a pull request")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(
            f"`gh pr view {url}` returned no JSON ({stdout[:200]!r})",
            GitHubError.UNREADABLE) from e
    if not payload.get("state"):
        raise GitHubError(f"`gh pr view {url}` reported no state ({payload!r})",
                          GitHubError.UNREADABLE)
    diff = _run(["pr", "diff", url], url=url, cwd=cwd,
                missing_hint="so the validation panel cannot read a pull request")
    files = [f for f in (payload.get("files") or []) if isinstance(f, dict)]
    return PullRequestArtifact(
        url=str(payload.get("url") or url),
        number=int(payload.get("number") or 0),
        title=str(payload.get("title") or ""),
        body=str(payload.get("body") or ""),
        state=str(payload.get("state") or "").upper(),
        draft=bool(payload.get("isDraft")),
        base_ref=str(payload.get("baseRefName") or ""),
        head_ref=str(payload.get("headRefName") or ""),
        head_oid=str(payload.get("headRefOid") or ""),
        additions=int(payload.get("additions") or 0),
        deletions=int(payload.get("deletions") or 0),
        files=tuple(str(f.get("path") or "") for f in files if f.get("path")),
        stat=_stat_block(files),
        checks=read_checks(payload),
        diff=diff,
    )
