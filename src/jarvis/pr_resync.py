"""Forcing GitHub to recompute one pull request: close it, reopen it, nothing else.

PR #995's shape, which `daemon.pr_recompute_stalled` recognises: a push moved the branch
to 88cac10, the pull request record's head stayed at 0488753, mergeability sat UNKNOWN
for ~7h, and every acting branch of the merge poll declined for all of it. A hand
`gh pr close` followed by `gh pr reopen` fixed it in one go — GitHub rebuilds the pull
request's head and its mergeability when it reopens — so that pair is the remedy the OS
performs for itself (wo-7ec57748, Neo question 1911 option B).

**THIS MODULE IS SEPARATE FROM `github.py` BECAUSE IT WRITES**, which is `ci.py`'s reason
verbatim: that module's read-only property is load-bearing rather than tidy. Neo
(question 251) put the validation panel's fetch there precisely so "a judging seat cannot
write to GitHub" is a property of the code, and `tests/test_github_artifact.py` walks its
AST to prove it against `github.READ_ONLY_VERBS`. A write added there would spend that
guarantee to save a file. `ci.py` could not host these either: its `WRITE_VERBS` is
asserted by EQUALITY in two tests, on the claim that the OS updates a branch and does
nothing else there. So the pair lives here, under its own allowlist and its own AST test,
exactly as the base-update write does.

**NEITHER CALL IS A GATED ACTION**, decided on what they do rather than on how they
read: closing and reopening a pull request merges nothing into the default branch, ships
no release, restarts no service, adds no authored content (no `--comment`), rewrites no
history and deletes no branch (no `--delete-branch`, deliberately — that would destroy
the work). The pair is also its own inverse: the state after a successful reopen is the
state before the close. Contrast the merge in `automerge`, which is gated because it is
irreversible and lands code on `main`. The bound is arithmetic instead — at most one pair
per stall episode, held by `Daemon.resync_pull_request` off the timeline.

Both raise `GitHubError` like every other `gh` call in the OS. The caller treats a
failure as "not resynced", records which stage failed and stops: see the hazard
`Daemon.resync_pull_request`'s docstring states, which is why the close and the reopen
must be the only two things that happen between the episode's events.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .bugreport import gh_bin, gh_missing_message
from .github import GhUnavailable, GitHubError, checked_pr_url

log = logging.getLogger("jarvis.pr_resync")

#: `github.GH_TIMEOUT`'s ceiling, separate for `ci.CI_TIMEOUT`'s reason: these calls
#: share the daemon's tick and must not block it, and a future need to give one of them
#: longer must not silently lengthen the merge poll's own reads.
RESYNC_TIMEOUT = 30

#: Every `gh` subcommand this module may run. BOTH ARE WRITES and there are no others —
#: `tests/test_pr_checks.py` walks this module's AST against this tuple, the way
#: `tests/test_base_heal.py` does for `ci.py` and `tests/test_github_artifact.py` does for
#: the read-only module. A third verb has to be added in a commit that also changes that
#: test.
VERBS = (("pr", "close"), ("pr", "reopen"))

#: Of those, the ones that change something at GitHub: all of them. Named apart anyway so
#: the claim a reader wants to check — "this module closes and reopens a pull request and
#: does nothing else" — is one tuple rather than the difference of two.
WRITE_VERBS = VERBS


def _run(args: list[str], *, url: str, missing_hint: str,
         cwd: Path | None) -> str:
    """One `gh` write, as stdout. Raises `GitHubError` (or `GhUnavailable`) on any doubt.

    `github._run`'s body, including the rule that `gh`'s stderr is LOGGED and never
    raised into `GitHubError.reason`: the remote's text reaches a timeline payload from
    here, so the fixed vocabulary is what the record gets.
    """
    where = str(cwd) if cwd and Path(cwd).is_dir() else None
    shown = "`gh " + " ".join(args[:2]) + f" {url}`"
    try:
        proc = subprocess.run([gh_bin(), *args], capture_output=True, text=True,
                              timeout=RESYNC_TIMEOUT, cwd=where)
    except FileNotFoundError as e:
        raise GhUnavailable(gh_missing_message(missing_hint),
                            GitHubError.NO_GH) from e
    except subprocess.TimeoutExpired as e:
        raise GitHubError(f"{shown} timed out after {RESYNC_TIMEOUT}s",
                          GitHubError.TIMEOUT) from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        log.info("%s failed: %s", shown, detail)
        raise GitHubError(f"{shown} failed: {detail}", GitHubError.REFUSED)
    return proc.stdout or ""


def pr_close(url: str, *, cwd: Path | None = None) -> None:
    """Close the pull request at `url`. HALF AN OPERATION — see the module docstring.

    NO `--delete-branch`: the branch carries the work, and the reopen needs it. NO
    `--comment`: nothing here is authored content, and the timeline is where this is
    recorded.

    The URL is checked BEFORE it becomes an argument, `github.pr_view`'s reason exactly
    and with more at stake: `pr_url` is written by the submitter and this command WRITES.
    """
    url = checked_pr_url(url, cwd)
    _run(["pr", "close", url], url=url, cwd=cwd,
         missing_hint="so Jarvis cannot make GitHub recompute a stalled pull request")


def pr_reopen(url: str, *, cwd: Path | None = None) -> None:
    """Reopen the pull request at `url` — the half that makes the close harmless.

    Reopening is what rebuilds the pull request's head and its mergeability, so this is
    the call the remedy exists for; the close is only how GitHub is made to do it.
    """
    url = checked_pr_url(url, cwd)
    _run(["pr", "reopen", url], url=url, cwd=cwd,
         missing_hint="so Jarvis cannot reopen a pull request it closed to resync it")
