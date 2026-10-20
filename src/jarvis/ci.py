"""A pull request red only because its base was: recognising it, and healing it (docs/superpowers/specs/2026-09-18-a-red-base-heals-itself.md)"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .bugreport import gh_bin, gh_missing_message
from .github import PR_URL_RE, GhUnavailable, GitHubError, checked_pr_url

log = logging.getLogger("jarvis.ci")

#: Same ceiling as `github.GH_TIMEOUT`
CI_TIMEOUT = 30

#: Every `gh` subcommand this module may run
VERBS = (("run", "list"), ("api", "--method"), ("pr", "update-branch"))

#: Of those, the ones that change something at GitHub
WRITE_VERBS = (("pr", "update-branch"),)

#: What `gh run list --json` is asked for
RUN_FIELDS = "databaseId,headSha,conclusion,status,startedAt,workflowName"

#: How far back the base's history is read
BASE_RUN_LIMIT = 20

#: A `conclusion` on a WORKFLOW RUN that means the base itself was broken
RED_RUN_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

#: The shape a branch name must have before it becomes an argument
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

#: The same rule for a commit, one field along again: `headRefOid` is GitHub's answer about a pull...
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

#: One segment of `owner/repo`, for the two reads that take them apart rather than through a pull...
REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class Run:
    """One workflow run on the BASE branch"""

    run_id: int
    head_sha: str
    #: Lower case, as `gh run list` answers it
    conclusion: str
    #: `queued`, `in_progress` or `completed`
    status: str
    #: Epoch seconds, or None when GitHub gave no parseable time
    started_at: float | None
    workflow: str

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def red(self) -> bool:
        return self.conclusion in RED_RUN_CONCLUSIONS

    @property
    def green(self) -> bool:
        return self.conclusion == "success"


def parse_ts(value: str | None) -> float | None:
    """An ISO-8601 instant as epoch seconds, or None"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _run(args: list[str], *, cwd: Path | None, missing_hint: str) -> str:
    """One `gh` call, as stdout"""
    where = str(cwd) if cwd and Path(cwd).is_dir() else None
    shown = "`gh " + " ".join(args) + "`"
    try:
        proc = subprocess.run([gh_bin(), *args], capture_output=True, text=True,
                              timeout=CI_TIMEOUT, cwd=where)
    except FileNotFoundError as e:
        raise GhUnavailable(gh_missing_message(missing_hint), GitHubError.NO_GH) from e
    except subprocess.TimeoutExpired as e:
        raise GitHubError(f"{shown} timed out after {CI_TIMEOUT}s",
                          GitHubError.TIMEOUT) from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        log.info("%s failed: %s", shown, detail)
        raise GitHubError(f"{shown} failed: {detail}", GitHubError.REFUSED)
    return proc.stdout or ""


def base_runs(base_ref: str, cwd: Path | None = None,
              limit: int = BASE_RUN_LIMIT) -> tuple[Run, ...]:
    """The base branch's recent CI, newest first"""
    if not BRANCH_RE.match(base_ref or ""):
        raise GitHubError(f"{base_ref!r} is not a branch name this may ask about",
                          GitHubError.URL_REFUSED)
    stdout = _run(["run", "list", "--branch", base_ref, "--limit", str(limit),
                   "--json", RUN_FIELDS], cwd=cwd,
                  missing_hint="so Jarvis cannot tell a red base from a red branch")
    try:
        rows = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(f"`gh run list --branch {base_ref}` returned no JSON "
                          f"({stdout[:200]!r})", GitHubError.UNREADABLE) from e
    if not isinstance(rows, list):
        raise GitHubError(f"`gh run list --branch {base_ref}` returned {rows!r}",
                          GitHubError.UNREADABLE)
    runs = [
        Run(run_id=int(r.get("databaseId") or 0),
            head_sha=str(r.get("headSha") or ""),
            conclusion=str(r.get("conclusion") or "").lower(),
            status=str(r.get("status") or "").lower(),
            started_at=parse_ts(r.get("startedAt")),
            workflow=str(r.get("workflowName") or ""))
        for r in rows if isinstance(r, dict)
    ]
    # Newest first, by the OS's own ordering rather than by trusting the page order
    return tuple(sorted(runs, key=lambda r: (r.started_at is not None, r.started_at or 0),
                        reverse=True))


def update_branch(pr_url: str, cwd: Path | None = None) -> None:
    """Rebuild this pull request's merge ref against its base"""
    url = checked_pr_url(pr_url, cwd)
    _run(["pr", "update-branch", url], cwd=cwd,
         missing_hint="so Jarvis cannot rebuild a pull request against a fixed base")


def commit_parents(pr_url: str, sha: str, cwd: Path | None = None) -> tuple[str, ...]:
    """The parents of `sha`, in order, in the pull request's own repository"""
    url = checked_pr_url(pr_url, cwd)
    if not SHA_RE.match(sha or ""):
        raise GitHubError(f"{sha!r} is not a commit this may ask about",
                          GitHubError.URL_REFUSED)
    m = PR_URL_RE.match(url)
    if not m:  # unreachable — `checked_pr_url` anchors on the same pattern
        raise GitHubError(f"{url!r} is not a pull request URL",
                          GitHubError.URL_REFUSED)
    owner, repo = m.group(2), m.group(3)
    stdout = _run(["api", "--method", "GET", f"repos/{owner}/{repo}/commits/{sha}",
                   "--jq", ".parents[].sha"], cwd=cwd,
                  missing_hint="so Jarvis cannot prove what a branch update merged")
    return tuple(line.strip() for line in stdout.splitlines() if line.strip())


def _checked_repo(owner: str, repo: str) -> tuple[str, str]:
    if not (REPO_SEGMENT_RE.match(owner or "") and REPO_SEGMENT_RE.match(repo or "")):
        raise GitHubError(f"{owner!r}/{repo!r} is not a repository this may ask about",
                          GitHubError.URL_REFUSED)
    return owner, repo


def default_branch(owner: str, repo: str, cwd: Path | None = None) -> str:
    """This repository's default branch name (docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md)"""
    _checked_repo(owner, repo)
    stdout = _run(["api", "--method", "GET", f"repos/{owner}/{repo}",
                   "--jq", ".default_branch"], cwd=cwd,
                  missing_hint="so Jarvis cannot tell which branch is this project's base")
    name = (stdout or "").strip()
    if not BRANCH_RE.match(name):
        raise GitHubError(f"{name!r} is not a branch name this may ask about",
                          GitHubError.URL_REFUSED)
    return name


def commit_subject(owner: str, repo: str, sha: str, cwd: Path | None = None) -> str:
    """The first line of `sha`'s commit message"""
    _checked_repo(owner, repo)
    if not SHA_RE.match(sha or ""):
        raise GitHubError(f"{sha!r} is not a commit this may ask about",
                          GitHubError.URL_REFUSED)
    stdout = _run(["api", "--method", "GET", f"repos/{owner}/{repo}/commits/{sha}",
                   "--jq", ".commit.message"], cwd=cwd,
                  missing_hint="so Jarvis cannot say which work order broke the base")
    return (stdout or "").strip().splitlines()[0].strip() if (stdout or "").strip() else ""


def run_url(owner: str, repo: str, run_id: int) -> str:
    """Where a person looks at a workflow run"""
    return f"https://github.com/{owner}/{repo}/actions/runs/{int(run_id)}"


#: How far back the carry's parentage walk reads, in commits and therefore in API calls
CHAIN_LIMIT = 10


def base_merge_chain(pr_url: str, judged: str, head: str, *, base_ref: str,
                     cwd: Path | None = None) -> tuple[tuple[str, str, bool], ...]:
    """`judged..head` as a chain of MERGES THAT ADDED NOTHING UNJUDGED, oldest first, or `()` (docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §3.2)"""
    from . import branchproof

    if not SHA_RE.match(judged or "") or not SHA_RE.match(head or "") or cwd is None:
        return ()
    chain: list[tuple[str, str, bool]] = []
    cursor = head
    for _ in range(CHAIN_LIMIT):
        parents = commit_parents(pr_url, cursor, cwd=cwd)
        if len(parents) != 2:
            log.info("%s is not a base merge — its parents are %s", cursor[:10],
                     [p[:10] for p in parents] or "unreadable")
            return ()
        is_base = branchproof.is_ancestor(cwd, parents[1], f"origin/{base_ref}")
        # §3.2 item 6, widened by Neo question 806: a base commit
        if not is_base and not branchproof.is_ancestor(cwd, parents[1], judged):
            log.info("%s merged %s, which is an ancestor of neither origin/%s nor %s",
                     cursor[:10], parents[1][:10], base_ref, judged[:10])
            return ()
        chain.append((cursor, parents[1], is_base))
        cursor = parents[0]
        if cursor == judged:
            return tuple(reversed(chain))
    log.info("more than %d commits between %s and %s — not walking further", CHAIN_LIMIT,
             judged[:10], head[:10])
    return ()


# -- the decision, pure


def latest(runs: tuple[Run, ...], workflow: str) -> Run | None:
    """The newest COMPLETED base run of this workflow, or None"""
    for r in runs:
        if r.workflow == workflow and r.completed:
            return r
    return None


def head_when(runs: tuple[Run, ...], workflow: str, at: float | None) -> Run | None:
    """The base run of this workflow that was the base's head at instant `at`"""
    if at is None:
        return None
    for r in runs:
        if r.workflow == workflow and r.started_at is not None and r.started_at <= at:
            return r
    return None


def base_is_red(runs: tuple[Run, ...], workflows: tuple[str, ...]) -> bool:
    """Is the base broken RIGHT NOW, for any workflow this pull request is failing?"""
    return any(r is not None and r.red for r in (latest(runs, w) for w in workflows))


def inherited(check: dict[str, str], runs: tuple[Run, ...]) -> bool:
    """Did this failing check fail because its BASE was broken, and is the base well now? (#298)"""
    workflow = check.get("workflow") or ""
    if not workflow:
        return False
    was = head_when(runs, workflow, parse_ts(check.get("started_at")))
    return bool(was and was.red and (n := latest(runs, workflow)) and n.green)


def base_sha(runs: tuple[Run, ...], workflows: tuple[str, ...]) -> str:
    """The base commit this heal is being attempted AGAINST — what bounds the attempt"""
    greens = [r for w in workflows if (r := latest(runs, w)) and r.green]
    greens.sort(key=lambda r: (r.started_at is not None, r.started_at or 0), reverse=True)
    return greens[0].head_sha if greens else ""
