"""Reading a pull request's state — the one question the OS asks GitHub.

A work order that ends behind a pull request parks in `waiting_pr_merge` and stays on
the user's open list until the PR is dealt with. This module is how the daemon finds
out that it was: one `gh pr view <url> --json state,mergedAt` per parked work order,
turned into a three-way answer (open / merged / closed-unmerged).

Deliberately thin, and deliberately read-only. Jarvis never writes to GitHub from the
daemon — merging is a privileged action a human or a gate approval authorises, never
something a poll loop does on its own. Everything here is a question.

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

from .bugreport import gh_bin, gh_missing_message

#: `gh pr view` is a single API round trip; anything slower than this is a network
#: problem, and the daemon must not block its tick on one.
GH_TIMEOUT = 30


class GitHubError(RuntimeError):
    """The state of the pull request could not be read. Nothing is known about it."""


class GhUnavailable(GitHubError):
    """There is no `gh` to run at all — as opposed to one that ran and refused.

    Worth its own type because the two failures have opposite remedies and the daemon
    tells the user which: a missing binary is a PATH problem, while a `gh` that runs and
    fails is usually credentials. Advising `GH_TOKEN` at someone whose service PATH lost
    /snap/bin is how issue #90 went undiagnosed for a release.
    """


@dataclass(frozen=True)
class PullRequest:
    """What `gh pr view --json state,mergedAt` says, and nothing more.

    `state` is GitHub's own enum: OPEN, MERGED or CLOSED. The distinction that matters
    to the OS is MERGED (the work landed) versus CLOSED (the pull request was shut
    without landing — someone refused the work), which is why `closed_unmerged` is
    spelled out rather than left as `not merged`.
    """

    state: str
    merged_at: str | None = None

    @property
    def merged(self) -> bool:
        return self.state == "MERGED"

    @property
    def closed_unmerged(self) -> bool:
        return self.state == "CLOSED"


def pr_view(url: str, cwd: Path | None = None) -> PullRequest:
    """Read the state of the pull request at `url`. Raises `GitHubError` on any doubt.

    `cwd` is the project directory when there is one: an explicit URL needs no repo
    context, but running inside the repo is what picks up a per-repo host config on a
    GitHub Enterprise remote. A missing directory is ignored rather than raising —
    losing the poll over a moved checkout would be worse than polling from anywhere.
    """
    cmd = [gh_bin(), "pr", "view", url, "--json", "state,mergedAt"]
    where = str(cwd) if cwd and Path(cwd).is_dir() else None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT,
                              cwd=where)
    except FileNotFoundError as e:
        raise GhUnavailable(gh_missing_message(
            "so Jarvis cannot see when a pull request merges")) from e
    except subprocess.TimeoutExpired as e:
        raise GitHubError(f"`gh pr view` timed out after {GH_TIMEOUT}s") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise GitHubError(f"`gh pr view {url}` failed: {detail}")
    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError as e:
        raise GitHubError(
            f"`gh pr view {url}` returned no JSON ({(proc.stdout or '')[:200]!r})"
        ) from e
    state = str(payload.get("state") or "").upper()
    if not state:
        raise GitHubError(f"`gh pr view {url}` reported no state ({payload!r})")
    return PullRequest(state=state, merged_at=payload.get("mergedAt") or None)
