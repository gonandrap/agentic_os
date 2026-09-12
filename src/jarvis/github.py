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
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bugreport import gh_bin, gh_missing_message

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


class GitHubError(RuntimeError):
    """The state of the pull request could not be read. Nothing is known about it."""


class GhUnavailable(GitHubError):
    """There is no `gh` to run at all — as opposed to one that ran and refused.

    Worth its own type because the two failures have opposite remedies and the daemon
    tells the user which: a missing binary is a PATH problem, while a `gh` that runs and
    fails is usually credentials. Advising `GH_TOKEN` at someone whose service PATH lost
    /snap/bin is how issue #90 went undiagnosed for a release.
    """


#: The fields of one `gh pr view --json …`. Two questions in one round trip: did this
#: land, and can it still land? See the spec's §2 for the second half.
PR_FIELDS = "state,mergedAt,mergeable,baseRefName"


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
        raise GhUnavailable(gh_missing_message(missing_hint)) from e
    except subprocess.TimeoutExpired as e:
        raise GitHubError(f"{shown} timed out after {GH_TIMEOUT}s") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise GitHubError(f"{shown} failed: {detail}")
    return proc.stdout or ""


def pr_view(url: str, cwd: Path | None = None) -> PullRequest:
    """Read the state of the pull request at `url`. Raises `GitHubError` on any doubt."""
    stdout = _run(["pr", "view", url, "--json", PR_FIELDS], url=url, cwd=cwd,
                  missing_hint="so Jarvis cannot see when a pull request merges")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(
            f"`gh pr view {url}` returned no JSON ({stdout[:200]!r})") from e
    state = str(payload.get("state") or "").upper()
    if not state:
        raise GitHubError(f"`gh pr view {url}` reported no state ({payload!r})")
    # Only `state` is required above: `mergeable` arrives as null on a merged or closed
    # pull request and on one GitHub has not computed yet, and None reads everywhere as
    # "no conflict known" — the safe direction (spec §2).
    return PullRequest(
        state=state,
        merged_at=payload.get("mergedAt") or None,
        mergeable=str(payload["mergeable"]).upper() if payload.get("mergeable") else None,
        base_ref=payload.get("baseRefName") or None,
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
    #: One entry per check run: `{"name", "status", "conclusion"}`. Empty when the
    #: repository runs no checks, which is NOT the same as every check failing and must
    #: not be rendered as though it were.
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
    """
    stdout = _run(["pr", "view", url, "--json", ARTIFACT_FIELDS], url=url, cwd=cwd,
                  missing_hint="so the validation panel cannot read a pull request")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(
            f"`gh pr view {url}` returned no JSON ({stdout[:200]!r})") from e
    if not payload.get("state"):
        raise GitHubError(f"`gh pr view {url}` reported no state ({payload!r})")
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
        # GitHub answers a check run and a legacy commit status with different keys —
        # `conclusion`/`status` on the first, `state` on the second — and a repository
        # can carry both at once. Reading either shape keeps a green CI from rendering
        # as an empty conclusion, which a seat would have to read as "not known to pass".
        checks=tuple(
            {"name": str(c.get("name") or c.get("context") or ""),
             "status": str(c.get("status") or c.get("state") or ""),
             "conclusion": str(c.get("conclusion") or c.get("state") or "")}
            for c in (payload.get("statusCheckRollup") or [])
            if isinstance(c, dict)),
        diff=diff,
    )
