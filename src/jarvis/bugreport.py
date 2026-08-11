"""Bug reporting: one command turns a fleet agent's observation into a tracked issue.

Any agent working under the OS — a worker in its worktree, Jarvis in the terminal —
runs `jarvis bug report` when Jarvis OS itself misbehaves. That does two things and
neither is optional: it files a GitHub issue on the OS repo from a fixed template, and
it puts the issue link in front of the user through the normal notification pipeline.

The template is fixed on purpose. Bugs found by agents are read later by an agent
fixing them, and "what I expected vs what I got" plus the exact running version is what
makes a report actionable months later. Getting that version right is fiddly enough to
have its own section below; the one rule it answers to is that a report must never carry
a number that was never released.

A filing that fails raises. Half-succeeding — pinging the user about an issue that
does not exist — teaches them to distrust the pings.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

#: The OS's own issue tracker. Overridable so forks (and tests) target their own repo.
DEFAULT_BUG_REPO = "gonandrap/agentic_os"
BUG_REPO_ENV = "JARVIS_BUG_REPO"
BUG_LABEL = "bug"

#: `gh` location override, mirroring JARVIS_CLAUDE_BIN.
GH_BIN_ENV = "JARVIS_GH_BIN"

#: Where to look for `gh` when PATH does not have it. A systemd --user service inherits
#: none of a login shell's PATH, so a snap-installed `gh` (/snap/bin/gh — the default on
#: Ubuntu) is invisible to the daemon and to every worker it spawns: PR-merge polling
#: goes silently off and `jarvis bug report` fails fleet-wide (issues #41, #90).
#:
#: The service PATH is the real fix and `scripts/install_prod_service.sh` writes it —
#: THIS LIST IS MIRRORED THERE, keep the two in step. This scan is the second half: it
#: heals a daemon whose unit was rendered before that fix, without a re-install. It
#: cannot help workers, which shell out to a bare `gh` from bash; only the unit's PATH
#: reaches those.
GH_SEARCH_DIRS = ("/snap/bin", "~/.local/bin", "/usr/local/bin", "/usr/bin", "/bin")


class BugReportError(RuntimeError):
    """Filing the bug failed; nothing was reported to anyone."""


# -- which Jarvis is running ----------------------------------------------------
#
# Three surfaces show this string — the bug template below, `jarvis --version`, and the
# dashboard's environment badge — and all three are read by someone trying to correlate
# a symptom with a release. So the one hard requirement is: NEVER report a number that
# was never released.
#
# Installed dist metadata alone cannot meet that. It is exactly right in production
# (the deploy checks out the tag, whose pyproject carries the bump) and misleading in
# the dev checkout, because the bump lives only on release branches — main's pyproject
# sits on whatever number it was last left at, and reporting that invents a release.
#
# Git settles it, with one catch worth knowing before touching this code: the release
# branch is cut FROM main and the bump commit + tag land on that branch, never merged
# back. So a release tag is a DESCENDANT of main, not an ancestor, and plain
# `git describe` from the dev checkout resolves nothing at all ("No tags can describe").
# Asking whether HEAD *is* a release tag is the question that has an answer:
#
#   detached exactly at jarvis-X.Y.Z  -> a real release, report X.Y.Z
#   a Jarvis checkout, but not at a tag -> a dev build, say so with the commit
#   no checkout (wheel install)         -> dist metadata, which is right there anyway

#: Release tags are `jarvis-X.Y.Z`; the deploy leaves production detached at one.
RELEASE_TAG_PREFIX = "jarvis-"


def _source_dir() -> Path:
    """The directory holding this module. Seam for tests."""
    return Path(__file__).resolve().parent


def _git(cwd: Path, *args: str) -> str | None:
    """Run git in `cwd` and return stripped stdout, or None if it did not work out.

    Never raises and never inspects the process cwd: `jarvis bug report` is routinely
    run by a worker sitting in ITS OWN project's worktree, so asking git about the
    working directory would report some unrelated project's version.
    """
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):  # no git binary, or it hung
        return None
    if proc.returncode != 0:  # not a repo, no matching tag, whatever — same answer
        return None
    return proc.stdout.strip() or None


def _jarvis_checkout() -> Path | None:
    """The Jarvis git checkout this code runs from, or None if it is not running from one.

    The layout check is the point. Production and dev both install editable, so the
    package sits at <checkout>/src/jarvis and git can be trusted. A wheel install puts
    it in site-packages — which may itself sit inside some *unrelated* repo (a venv
    under a user's own project), and git would happily describe that one. Requiring the
    repo root to be the parent of our own src/jarvis keeps that mistake impossible.
    """
    src = _source_dir()
    top = _git(src, "rev-parse", "--show-toplevel")
    if not top:
        return None
    root = Path(top).resolve()
    return root if root / "src" / "jarvis" == src else None


def _dev_version(root: Path, suffix: str) -> str:
    """How a build that is not a release identifies itself.

    Deliberately carries no X.Y.Z: the dev checkout is not "0.2.1 plus a bit", it is a
    commit that sits *behind* the latest tag (the release bump is not on main), so any
    number here would be the invention this whole module is trying to avoid.
    """
    sha = _git(root, "rev-parse", "--short", "HEAD") or "unknown"
    return f"dev-{sha}{suffix}"


def _installed_version() -> str:
    """Dist metadata, then the in-tree constant. The no-git fallback."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("jarvis-os")
    except PackageNotFoundError:  # running from a source tree without an install
        from . import __version__
        return f"{__version__} (uninstalled source tree)"


@lru_cache(maxsize=1)
def jarvis_version() -> str:
    """The version of Jarvis OS actually running.

    A shipped release reports its bare number; anything else is self-evidently not one.
    A tree with uncommitted changes to tracked files is marked `-dirty`, in the release
    case too — a hand-edited production checkout is no longer the release it claims to
    be, and that is worth seeing on a bug report.

    Cached: the dashboard badge asks once per page render, and a deploy restarts the
    process anyway. Never raises — a version string must not be why a page 500s.
    """
    try:
        root = _jarvis_checkout()
        if root is not None:
            suffix = "-dirty" if _git(root, "status", "--porcelain",
                                      "--untracked-files=no") else ""
            tag = _git(root, "describe", "--tags", "--exact-match",
                       "--match", f"{RELEASE_TAG_PREFIX}*")
            if tag and tag.startswith(RELEASE_TAG_PREFIX):
                return f"{tag[len(RELEASE_TAG_PREFIX):]}{suffix}"
            return _dev_version(root, suffix)
    except Exception:  # noqa: BLE001 — fall back rather than break every caller
        pass
    return _installed_version()


def bug_repo() -> str:
    return os.environ.get(BUG_REPO_ENV) or DEFAULT_BUG_REPO


def gh_bin() -> str:
    """Where `gh` is: the override, then PATH, then the well-known locations.

    Falls back to the bare name so callers still produce a recognisable command line in
    an error; `gh_missing_message` is what explains it. Deliberately uncached — a
    long-running daemon should pick up a `gh` that appeared after it started.
    """
    override = os.environ.get(GH_BIN_ENV)
    if override:
        return override
    found = shutil.which("gh")
    if found:
        return found
    for d in GH_SEARCH_DIRS:
        candidate = Path(d).expanduser() / "gh"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return "gh"


def gh_missing_message(consequence: str) -> str:
    """Why `gh` could not be run — written for someone who probably HAS it installed.

    The old wording ("install the GitHub CLI") sent the user to reinstall software that
    was already there and authenticated; on this fleet the cause has never once been a
    missing CLI, it has been a PATH that a systemd unit never inherited (#41, #90). So
    name what was searched, put the PATH fix first, and leave "install it" as the last
    resort it actually is.
    """
    searched = os.environ.get("PATH", "") or "(empty)"
    extra = ", ".join(GH_SEARCH_DIRS)
    return (
        f"`gh` is not on this process's PATH — {consequence}.\n"
        f"PATH searched: {searched}\n"
        f"Also checked: {extra}\n"
        "If `command -v gh` finds it in a login shell (a snap install lands in "
        "/snap/bin), this is a PATH problem and not a missing tool: re-run "
        "scripts/install_prod_service.sh to rebuild the service PATH, or set "
        f"{GH_BIN_ENV} to gh's absolute path. Install the GitHub CLI "
        "(https://cli.github.com) only if that lookup finds nothing either."
    )


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
        raise BugReportError(gh_missing_message("so Jarvis cannot file issues")) from e
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
