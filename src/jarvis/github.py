"""Reading a pull request — its state for the poll loop, its whole self for the panel.

A work order that ends behind a pull request parks in `waiting_pr_merge` and stays on
the user's open list until the PR is dealt with. This module is how the daemon finds
out that it was: one `gh pr view <url>` per parked work order, turned into a three-way
answer (open / merged / closed-unmerged) plus whether the branch can still be merged at
all (docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md §2).

`pr_artifact` is the second reader and a much bigger one: the validation panel judges
the pull request itself, so the evidence collector fetches the diff, the body and the
checks through here
(docs/superpowers/specs/2026-09-12-the-pull-request-is-the-artifact.md §2).

Deliberately thin, and deliberately read-only. Jarvis never writes to GitHub from the
daemon — merging is a privileged action a human or a gate approval authorises, never
something a poll loop does on its own. Everything here is a question.

**READ-ONLY IS LOAD-BEARING NOW, NOT JUST TIDY.** The panel's blind review rests on it:
a seat that could comment on the pull request could talk to the implementor it is
judging. Neo (question 251) chose to keep the seats tool-free and put the fetch HERE
precisely so that "the judge cannot write to GitHub" is a property of the code rather
than of an allowlist string a model is asked to respect. `READ_ONLY_VERBS` names every
`gh` subcommand this module may run and a test asserts the module against it.

`gh` is the transport rather than the REST API because it is already how this OS talks
to GitHub (`bugreport.create_issue`), which means one auth story, one binary override
(`JARVIS_GH_BIN`) and one test-isolation gate instead of two. It also means the same two
failure modes, which want opposite remedies and so have separate exception types here:
the daemon's PATH may not contain `gh` at all (`GhUnavailable` — the service unit's
PATH, see `bugreport.GH_SEARCH_DIRS`), or it may run `gh` and fail to reach its keyring
credentials (plain `GitHubError` — the service environment needs `GH_TOKEN`). Callers
are expected to treat either as "this project cannot be polled", not as a work-order
failure.
"""

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

#: `gh pr view` is a single API round trip; anything slower than this is a network
#: problem, and the daemon must not block its tick on one.
GH_TIMEOUT = 30

#: Every `gh` subcommand this module is allowed to run, as the (verb, subverb) pair that
#: opens the argument list. ALL OF THEM ARE READS, and `tests/test_github_artifact.py`
#: walks this module's AST to prove that the set of commands it actually builds is a
#: subset of this one. A write verb added here would have to be added deliberately, in a
#: commit that also changes the test — which is the whole mechanism: see the module
#: docstring for why the panel's blind review depends on it.
READ_ONLY_VERBS = (("pr", "view"), ("pr", "diff"))

#: The LOCAL git subcommands this module runs, held to the same standard by the same
#: test. `origin_repo` shells out to git to learn which repository this checkout belongs
#: to; that never touches the network and can mutate nothing, but it puts a second
#: command family in a module whose whole claim is "everything here is a question", so
#: it is declared rather than left for a reader to audit.
LOCAL_GIT_READS = (("remote", "get-url", "origin"),)


class GitHubError(RuntimeError):
    """The state of the pull request could not be read. Nothing is known about it.

    `reason` is a SHORT PHRASE THIS MODULE WROTE, from the fixed vocabulary below. It
    exists because `str(e)` carries `gh`'s stderr — remote text — and
    `evidence.collect_work_order` puts the failure into `packet.pr_error`, which is
    rendered verbatim into five seat prompts. A judge's prompt is not a place to
    interpolate a string a remote server chose. The full detail stays on the exception
    and in the log, where a human reads it; the packet gets `reason` and nothing else.
    """

    #: What the packet is allowed to say. Every raise below picks one.
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
    """The URL is not a pull request on this project's own repository.

    `pr_url` is written by the SUBMITTER (`jarvis wo finish --pr …`) and read back here
    into a command that runs with the operator's GitHub credentials. Two things follow,
    and neither is hypothetical:

    * A URL that does not start with `https://` can be read by `gh` as a FLAG. The
      argument list is passed to `subprocess.run` without a shell, which stops shell
      injection and does nothing at all about `--repo=someone/else` sitting where a URL
      should be.
    * A well-formed URL pointing somewhere else is a fetch of a stranger's pull request
      with the operator's credentials, presented to the panel as this work order's
      evidence.

    So the URL is checked against a strict shape AND against the project's own `origin`
    before it reaches an argument list. Refusing is safe: the collector records it in
    `pr_error` and falls back to the worktree, which is the same path a deleted pull
    request already took.
    """


class GhUnavailable(GitHubError):
    """There is no `gh` to run at all — as opposed to one that ran and refused.

    Worth its own type because the two failures have opposite remedies and the daemon
    tells the user which: a missing binary is a PATH problem, while a `gh` that runs and
    fails is usually credentials. Advising `GH_TOKEN` at someone whose service PATH lost
    /snap/bin is how issue #90 went undiagnosed for a release.
    """


#: The ONLY shape a pull-request URL may have before it becomes an argument. Anchored at
#: both ends, `https://` required — which is also what makes a leading `-` unreachable,
#: so no argument can be read as a `gh` flag. See `UntrustedPullRequest`.
PR_URL_RE = re.compile(
    r"^https://([A-Za-z0-9.-]+)/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/[0-9]+$")


def checked_pr_url(url: str, cwd: Path | None = None) -> str:
    """`url` if this project may fetch it. Raises `UntrustedPullRequest` otherwise.

    Two checks, and the second is skipped rather than failed when `origin` cannot be
    read: a project with no resolvable remote is a broken checkout, not an attack, and
    losing PR polling over one would be a worse failure than the one being prevented.
    Nothing an untrusted party controls can remove a project's git remote.
    """
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


def origin_repo(cwd: Path | None) -> tuple[str, str] | None:
    """`(owner, repo)` lowercased for this checkout's `origin`, or None if unreadable.

    Both remote spellings, because a project cloned over ssh and one cloned over https
    are the same repository and only one of them looks like a URL:
    `git@host:owner/repo.git` and `https://host/owner/repo.git`.
    """
    if cwd is None or not Path(cwd).is_dir():
        return None
    try:
        proc = subprocess.run(["git", "-C", str(cwd), "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=GH_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    remote = (proc.stdout or "").strip().removesuffix(".git")
    parts = remote.replace(":", "/").rstrip("/").split("/")
    if len(parts) < 2 or not parts[-1] or not parts[-2]:
        return None
    return parts[-2].lower(), parts[-1].lower()


#: The fields of one `gh pr view --json …`. Four questions in one round trip: did this
#: land, can it still land, is what it would land green, and — months later — is what
#: landed all of it? See the spec's §2 for the second,
#: 2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md §2 for the third, and
#: 2026-09-13-a-finished-order-proves-its-code-landed.md §7 for the fourth — including
#: why this is still NOT the same list as `ARTIFACT_FIELDS` below.
#:
#: `headRefOid` is the sha GitHub merged, and it is the only exact answer to issue
#: #232's Mode C: an order whose pull request merged and whose branch then carried
#: MORE commits. It is recorded on the `pr_merged` event so the landing sweep can ask
#: that question months later without a second round trip (`landing.assess`).
PR_FIELDS = ("state,mergedAt,mergeable,mergeStateStatus,baseRefName,"
             "statusCheckRollup,headRefOid")

#: A check conclusion that means THE CODE IS WRONG — as opposed to merely not green. The
#: distinction is the whole of the red-pull-request spec's §2: a run that is PENDING,
#: CANCELLED, SKIPPED, NEUTRAL or STALE tells nobody the work is broken, and a worker
#: asked to "fix" one has nothing to edit. ERROR is a legacy commit status's FAILURE.
RED_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"})

#: A check that has not finished. `status` on a check run, `state` on a legacy commit
#: status — `read_checks` normalises both into the same keys, so one set covers both.
UNFINISHED_STATUSES = frozenset({"QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED",
                                 "PENDING", "EXPECTED"})


def read_checks(payload: dict[str, Any]) -> tuple[dict[str, str], ...]:
    """`statusCheckRollup`, normalised. THE ONLY READER OF A CHECK RUN IN THIS OS.

    GitHub answers a check run and a legacy commit status with different keys —
    `status`/`conclusion` on the first, `state` on the second — and a repository can
    carry both at once. Reading either shape keeps a green CI from rendering as an empty
    conclusion, which a seat would have to read as "not known to pass".

    Shared by both readers below on purpose. The FIELD SETS stay separate (the poll must
    not pay for the body, the file list and the diff on every tick), but "what does this
    check say" has one answer, or the OS judges a submission by a standard it does not
    police while it waits for the merge — which is exactly issue #224.
    """
    return tuple(
        {"name": str(c.get("name") or c.get("context") or ""),
         "status": str(c.get("status") or c.get("state") or ""),
         "conclusion": str(c.get("conclusion") or c.get("state") or "")}
        for c in (payload.get("statusCheckRollup") or [])
        if isinstance(c, dict))


def failing_checks(checks: tuple[dict[str, str], ...]) -> tuple[str, ...]:
    """The names of the checks that say the code is wrong. Empty is the green answer.

    Empty also covers the two states that are neither green nor red: nothing has run
    yet, and nothing runs on this repository at all. Both must nudge nobody, which is
    why this returns what IS failing rather than a "not passing" verdict.
    """
    return tuple(c["name"] or "(unnamed check)" for c in checks
                 if c["conclusion"].upper() in RED_CONCLUSIONS)


@dataclass(frozen=True)
class PullRequest:
    """What `gh pr view --json <PR_FIELDS>` says, and nothing more.

    `state` is GitHub's own enum: OPEN, MERGED or CLOSED. The distinction that matters
    to the OS is MERGED (the work landed) versus CLOSED (the pull request was shut
    without landing — someone refused the work), which is why `closed_unmerged` is
    spelled out rather than left as `not merged`.
    """

    state: str
    merged_at: str | None = None
    #: GitHub's three-way mergeability: CONFLICTING, MERGEABLE or UNKNOWN — and None
    #: when the field was not asked for or not answered.
    mergeable: str | None = None
    base_ref: str | None = None
    #: `mergeStateStatus`: BEHIND, BLOCKED, CLEAN, DIRTY, DRAFT, HAS_HOOKS, UNKNOWN or
    #: UNSTABLE. Only BEHIND is read, and only to SAY so — never to act (spec §5).
    merge_state: str | None = None
    #: One entry per check, through `read_checks`. Empty is a repository that runs no
    #: checks, which is not the same as every check failing.
    checks: tuple[dict[str, str], ...] = ()
    #: The sha at the head of the pull request's branch. On a MERGED pull request
    #: this is what was merged, which is what a later tail is measured against.
    head_oid: str = ""

    @property
    def merged(self) -> bool:
        return self.state == "MERGED"

    @property
    def closed_unmerged(self) -> bool:
        return self.state == "CLOSED"

    @property
    def conflicting(self) -> bool:
        """The branch cannot be merged into its base. UNKNOWN is not this — spec §2."""
        return self.mergeable == "CONFLICTING"

    @property
    def mergeable_now(self) -> bool:
        """GitHub positively says it merges — as opposed to "not known to conflict"."""
        return self.mergeable == "MERGEABLE"

    @property
    def failing(self) -> tuple[str, ...]:
        """The checks that say the code is wrong, by name. Empty is the green answer."""
        return failing_checks(self.checks)

    @property
    def checks_green(self) -> bool:
        """CI positively passed — as opposed to "nothing is currently failing".

        The same distinction `mergeable_now` draws, and it exists for the same reason.
        A worker that has just pushed a fix leaves every check QUEUED, and "not failing"
        would read that as success and close the repair episode — resetting the attempt
        budget, so a fix that does not work buys three fresh attempts every round and
        the cap stops capping anything. Only a finished, unanimous pass clears it.

        A pull request with no checks at all is not green either. It cannot have had a
        red episode to close, so this never has to answer for one.
        """
        return bool(self.checks) and not self.failing and not any(
            c["status"].upper() in UNFINISHED_STATUSES for c in self.checks)

    @property
    def behind(self) -> bool:
        """The branch is behind its base. REPORTED, NEVER ACTED ON — spec §5."""
        return self.merge_state == "BEHIND"


def _run(args: list[str], *, url: str, cwd: Path | None,
         missing_hint: str) -> str:
    """One `gh` read, as stdout. Raises `GitHubError` (or `GhUnavailable`) on any doubt.

    `cwd` is the project directory when there is one: an explicit URL needs no repo
    context, but running inside the repo is what picks up a per-repo host config on a
    GitHub Enterprise remote. A missing directory is ignored rather than raising —
    losing the read over a moved checkout would be worse than reading from anywhere.
    """
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
        # The remote text is LOGGED and not raised into `reason` — see `GitHubError`.
        log.info("%s failed: %s", shown, detail)
        raise GitHubError(f"{shown} failed: {detail}", GitHubError.REFUSED)
    return proc.stdout or ""


def pr_view(url: str, cwd: Path | None = None) -> PullRequest:
    """Read the state of the pull request at `url`. Raises `GitHubError` on any doubt.

    The URL is checked BEFORE it becomes an argument — the poll loop reads the same
    submitter-written column the panel does, so it carries the same exposure.
    """
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
    # Only `state` is required above: `mergeable` arrives as null on a merged or closed
    # pull request and on one GitHub has not computed yet, and None reads everywhere as
    # "no conflict known" — the safe direction (spec §2).
    return PullRequest(
        state=state,
        merged_at=payload.get("mergedAt") or None,
        mergeable=str(payload["mergeable"]).upper() if payload.get("mergeable") else None,
        base_ref=payload.get("baseRefName") or None,
        merge_state=(str(payload["mergeStateStatus"]).upper()
                     if payload.get("mergeStateStatus") else None),
        checks=read_checks(payload),
        head_oid=str(payload.get("headRefOid") or ""),
    )


#: The fields of one `gh pr view --json …` for the PANEL, which needs the pull request
#: as a human reviewer reads it rather than the two questions the poll loop asks.
#:
#: `statusCheckRollup` is the one worth naming: it is what lets a seat check the
#: submitter's DECLARED testing evidence against what CI actually reported, instead of
#: taking the claim on the submitter's word. `body` is the second: it carries the
#: reasoning, the screenshots and the "remaining work" section the PR template asks for,
#: none of which appear in a diff.
ARTIFACT_FIELDS = ("number,title,body,state,isDraft,baseRefName,headRefName,url,"
                   "additions,deletions,changedFiles,files,statusCheckRollup")


@dataclass(frozen=True)
class PullRequestArtifact:
    """A pull request as the validation panel judges it.

    Everything here was fetched with a read verb (`READ_ONLY_VERBS`) and nothing in this
    module can write back — see the module docstring, and spec §2.
    """

    url: str
    number: int
    title: str
    body: str
    state: str
    draft: bool
    base_ref: str
    head_ref: str
    additions: int
    deletions: int
    files: tuple[str, ...]
    #: `git diff --stat`-shaped, rebuilt from the `files` GitHub reports. GitHub gives
    #: per-file counts and no rendered stat, and the panel's prompt has always carried a
    #: stat block; rebuilding it here keeps that section identical in shape whichever
    #: source the diff came from.
    stat: str
    #: One entry per check run: `{"name", "status", "conclusion"}`, through the same
    #: `read_checks` the poll loop uses. Empty when the repository runs no checks, which
    #: is NOT the same as every check failing and must not be rendered as though it were.
    checks: tuple[dict[str, str], ...]
    #: The unified diff, exactly as `gh pr diff` prints it. UNTRUNCATED here: truncation
    #: is the evidence collector's job and its limit is a config value this module has
    #: no opinion about.
    diff: str


def _stat_block(files: list[dict[str, Any]]) -> str:
    """`git diff --stat`'s shape, rebuilt from GitHub's per-file counts.

    The `+`/`-` runs are capped because GitHub reports raw counts where git scales them
    to the terminal: an uncapped run would put a 4000-character line in a seat prompt.
    """
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
    """The pull request at `url`, whole. Raises `GitHubError` on any doubt.

    TWO round trips, both reads: `gh pr view --json` for everything structured, and
    `gh pr diff` for the patch. There is no single `gh` call that answers both — the
    JSON `files` array carries paths and counts but never the patch text.

    **Raising is the contract, and the collector depends on it.** A pull request that
    cannot be read must not degrade into a thinner artifact here, because the caller has
    to be able to tell "the submitter pointed at something unreadable" from "the
    submitter changed nothing": the first is a fetch problem and the second is a verdict.
    `evidence.collect_work_order` catches this and records the reason in
    `packet.pr_error` rather than passing the worktree off as the pull request.

    The URL is checked BEFORE it becomes an argument (`checked_pr_url`): it is written
    by the submitter and this runs with the operator's credentials.
    """
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
        additions=int(payload.get("additions") or 0),
        deletions=int(payload.get("deletions") or 0),
        files=tuple(str(f.get("path") or "") for f in files if f.get("path")),
        stat=_stat_block(files),
        checks=read_checks(payload),
        diff=diff,
    )
