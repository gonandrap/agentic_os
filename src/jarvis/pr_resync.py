"""Forcing GitHub to recompute one pull request: close it, reopen it, nothing else (#995)"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .bugreport import gh_bin, gh_missing_message
from .github import GhUnavailable, GitHubError, checked_pr_url

log = logging.getLogger("jarvis.pr_resync")

#: `github.GH_TIMEOUT`'s ceiling, separate for `ci.CI_TIMEOUT`'s reason: these calls share the...
RESYNC_TIMEOUT = 30

#: Every `gh` subcommand this module may run
VERBS = (("pr", "close"), ("pr", "reopen"))

#: Of those, the ones that change something at GitHub: all of them
WRITE_VERBS = VERBS


def _run(args: list[str], *, url: str, missing_hint: str,
         cwd: Path | None) -> str:
    """One `gh` write, as stdout"""
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
    """Close the pull request at `url`"""
    url = checked_pr_url(url, cwd)
    _run(["pr", "close", url], url=url, cwd=cwd,
         missing_hint="so Jarvis cannot make GitHub recompute a stalled pull request")


def pr_reopen(url: str, *, cwd: Path | None = None) -> None:
    """Reopen the pull request at `url` — the half that makes the close harmless"""
    url = checked_pr_url(url, cwd)
    _run(["pr", "reopen", url], url=url, cwd=cwd,
         missing_hint="so Jarvis cannot reopen a pull request it closed to resync it")
