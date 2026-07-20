"""Bug reporting: one command turns a fleet agent's observation into a tracked issue.

Any agent working under the OS — a worker in its worktree, Jarvis in the terminal —
runs `jarvis bug report` when Jarvis OS itself misbehaves. That does two things and
neither is optional: it files a GitHub issue on the OS repo from a fixed template, and
it puts the issue link in front of the user through the normal notification pipeline.

The template is fixed on purpose. Bugs found by agents are read later by an agent
fixing them, and "what I expected vs what I got" plus the exact running version is what
makes a report actionable months later. The version comes from the installed
distribution rather than a constant, because main's pyproject deliberately lags the
shipped version (see scripts/shipit.sh) — a hardcoded string would lie in production.

A filing that fails raises. Half-succeeding — pinging the user about an issue that
does not exist — teaches them to distrust the pings.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any

#: The OS's own issue tracker. Overridable so forks (and tests) target their own repo.
DEFAULT_BUG_REPO = "gonandrap/agentic_os"
BUG_REPO_ENV = "JARVIS_BUG_REPO"
BUG_LABEL = "bug"

#: `gh` location override, mirroring JARVIS_CLAUDE_BIN.
GH_BIN_ENV = "JARVIS_GH_BIN"


class BugReportError(RuntimeError):
    """Filing the bug failed; nothing was reported to anyone."""


def jarvis_version() -> str:
    """The version of Jarvis OS actually running, from installed dist metadata."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("jarvis-os")
    except PackageNotFoundError:  # running from a source tree without an install
        from . import __version__
        return f"{__version__} (uninstalled source tree)"


def bug_repo() -> str:
    return os.environ.get(BUG_REPO_ENV) or DEFAULT_BUG_REPO


def gh_bin() -> str:
    return os.environ.get(GH_BIN_ENV) or shutil.which("gh") or "gh"


def render_body(*, description: str, expected: str, actual: str, version: str,
                project: str = "", wo_id: str = "", steps: str = "") -> str:
    """The bug template. Every report on the tracker has this shape."""
    parts = [
        "### Description",
        "",
        description.strip() or "_(none given)_",
        "",
        "### Expected",
        "",
        expected.strip() or "_(none given)_",
        "",
        "### Actual",
        "",
        actual.strip() or "_(none given)_",
        "",
    ]
    if steps.strip():
        parts += ["### Steps to reproduce", "", steps.strip(), ""]

    reporter = "a human, via `jarvis bug report`"
    if wo_id:
        reporter = f"work order `{wo_id}`" + (f" in project `{project}`" if project else "")
    elif project:
        reporter = f"project `{project}`"

    parts += [
        "---",
        "",
        f"- **Jarvis OS version:** `{version}`",
        f"- **Reported by:** {reporter}",
        f"- **Reported at:** {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        "<!-- Filed automatically by `jarvis bug report`. -->",
    ]
    return "\n".join(parts)


def create_issue(title: str, body: str, repo: str, label: str = BUG_LABEL) -> str:
    """Create the GitHub issue and return its URL. Raises BugReportError on failure.

    The body travels over stdin (`--body-file -`): reports carry logs and tracebacks,
    and argv has a length limit that stdin does not.
    """
    cmd = [gh_bin(), "issue", "create", "--repo", repo,
           "--title", title, "--label", label, "--body-file", "-"]
    try:
        proc = subprocess.run(cmd, input=body, capture_output=True, text=True,
                              timeout=60)
    except FileNotFoundError as e:
        raise BugReportError(
            f"`gh` not found ({gh_bin()}) — install the GitHub CLI "
            "(https://cli.github.com) so Jarvis can file issues"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise BugReportError("`gh issue create` timed out after 60s") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise BugReportError(
            f"`gh issue create` failed: {detail}\n"
            "If this is a daemon-spawned worker, `gh`'s keyring credentials may be "
            f"unavailable — set GH_TOKEN in the service environment."
        )
    url = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else ""
    if not url.startswith("http"):
        raise BugReportError(f"`gh issue create` reported no issue URL (got {url!r})")
    return url


def _notify(project: str, title: str, url: str, version: str, wo_id: str) -> None:
    """Put the issue link in front of the user via the normal inbox -> sinks path.

    The daemon routes the inbox on each tick; when it is not running (a bug reported
    from an interactive Jarvis session on a stopped OS) we route inline so the ping is
    never silently lost.
    """
    from .central_store import CentralStore
    from .daemon import daemon_running

    body = f"{url}\nJarvis OS {version}" + (f" · {wo_id}" if wo_id else "")
    central = CentralStore()
    try:
        central.add_inbox(project or "jarvis-os", f"Bug filed: {title}",
                          body=body, level="warning")
        if daemon_running() is None:
            from .notify import route_new_inbox
            from .ops import resolve_catalog
            try:
                route_new_inbox(central, resolve_catalog())
            except Exception:  # noqa: BLE001 — the issue is filed; delivery is best effort
                pass
    finally:
        central.close()


def report_bug(*, title: str, description: str, expected: str, actual: str,
               steps: str = "", project: str = "", wo_id: str = "") -> dict[str, Any]:
    """File a Jarvis OS bug and tell the user about it. Raises BugReportError if the
    issue could not be created — in which case nobody is notified."""
    if not title.strip():
        raise BugReportError("a bug report needs a title")
    project = project or os.environ.get("JARVIS_PROJECT", "")
    wo_id = wo_id or os.environ.get("JARVIS_WO_ID", "")
    version = jarvis_version()
    repo = bug_repo()
    body = render_body(description=description, expected=expected, actual=actual,
                       version=version, project=project, wo_id=wo_id, steps=steps)
    url = create_issue(title, body, repo)
    _notify(project, title, url, version, wo_id)
    return {"url": url, "title": title, "repo": repo, "version": version,
            "project": project, "wo_id": wo_id}
