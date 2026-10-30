"""Bug reporting: one command turns a fleet agent's observation into a tracked issue"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import MutableMapping
from functools import lru_cache
from pathlib import Path
from typing import Any

#: The OS's own issue tracker
DEFAULT_BUG_REPO = "gonandrap/agentic_os"
BUG_REPO_ENV = "JARVIS_BUG_REPO"
BUG_LABEL = "bug"

#: The last line of every body this module files (docs/superpowers/specs/2026-10-14-a-red-main-dispatches-its-own-fix.md §4)
FILED_MARKER = "<!-- Filed automatically by `jarvis bug report`. -->"

#: `gh` location override, mirroring JARVIS_CLAUDE_BIN
GH_BIN_ENV = "JARVIS_GH_BIN"

#: Where to look for `gh` when PATH does not have it (#41)
GH_SEARCH_DIRS = ("/snap/bin", "~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
                  "/bin")


def heal_path(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Put the missing `GH_SEARCH_DIRS` back on PATH, so spawned children inherit them (#41)"""
    env = os.environ if environ is None else environ
    current = env.get("PATH", "")
    present = {p for p in current.split(os.pathsep) if p}
    added = [d for d in (str(Path(d).expanduser()) for d in GH_SEARCH_DIRS)
             if d not in present and Path(d).is_dir()]
    if added:
        env["PATH"] = os.pathsep.join([*(p for p in current.split(os.pathsep) if p),
                                       *added])
    return added


class BugReportError(RuntimeError):
    """Filing the bug failed; nothing was reported to anyone"""


# -- which Jarvis is running

#: Release tags are `jarvis-X.Y.Z`; the deploy leaves production detached at one
RELEASE_TAG_PREFIX = "jarvis-"


def _source_dir() -> Path:
    """The directory holding this module"""
    return Path(__file__).resolve().parent


def _git(cwd: Path, *args: str) -> str | None:
    """Run git in `cwd` and return stripped stdout, or None if it did not work out"""
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):  # no git binary, or it hung
        return None
    if proc.returncode != 0:  # not a repo, no matching tag, whatever — same answer
        return None
    return proc.stdout.strip() or None


def _jarvis_checkout() -> Path | None:
    """The Jarvis git checkout this code runs from, or None if it is not running from one"""
    src = _source_dir()
    top = _git(src, "rev-parse", "--show-toplevel")
    if not top:
        return None
    root = Path(top).resolve()
    return root if root / "src" / "jarvis" == src else None


def _dev_version(root: Path, suffix: str) -> str:
    """How a build that is not a release identifies itself"""
    sha = _git(root, "rev-parse", "--short", "HEAD") or "unknown"
    return f"dev-{sha}{suffix}"


def _installed_version() -> str:
    """Dist metadata, then the in-tree constant"""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("jarvis-os")
    except PackageNotFoundError:  # running from a source tree without an install
        from . import __version__
        return f"{__version__} (uninstalled source tree)"


@lru_cache(maxsize=1)
def jarvis_version() -> str:
    """The version of Jarvis OS actually running"""
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
    """Where `gh` is: the override, then PATH, then the well-known locations"""
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
    """Why `gh` could not be run — written for someone who probably HAS it installed (#41)"""
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
                project: str = "", wo_id: str = "", steps: str = "",
                priority: str = "") -> str:
    """The bug template"""
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
        # The CLAIM, permanently
        *([f"- **Priority claimed by the reporter:** `{priority}`"] if priority else []),
        f"- **Jarvis OS version:** `{version}`",
        f"- **Reported by:** {reporter}",
        f"- **Reported at:** {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        FILED_MARKER,
    ]
    return "\n".join(parts)


def create_issue(title: str, body: str, repo: str, label: str = BUG_LABEL) -> str:
    """Create the GitHub issue and return its URL"""
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


#: How the fleet writes a CI run: the run url, and the daemon brief's `run id <n>`
RUN_REF_RE = re.compile(r"actions/runs/(\d+)|run id (\d+)", re.IGNORECASE)

#: The first line of the comment a deduped report leaves on the issue that already has it
DUPLICATE_NOTE = ("A second report of this break arrived while this issue was open, so "
                  "its evidence is here rather than in a new issue.")


def run_ids_in(*texts: str) -> list[str]:
    """The CI runs a report cites, first mention first — what a break is deduped on"""
    found: list[str] = []
    for text in texts:
        for match in RUN_REF_RE.finditer(text or ""):
            run_id = match.group(1) or match.group(2)
            if run_id not in found:
                found.append(run_id)
    return found


def open_os_filed_issue(repo: str, run_id: Any) -> str:
    """An OPEN issue THE OS ITSELF filed citing this failing run, or `""` (docs/superpowers/specs/2026-10-14-a-red-main-dispatches-its-own-fix.md §4)"""
    from . import issues

    if not run_id:
        return ""
    login = issues.gh_login()
    if not login:
        return ""
    eligible = [
        r for r in issues.issues_mentioning(repo, str(run_id))
        if str(r.get("state") or "").upper() == "OPEN"
        and str((r.get("author") or {}).get("login") or "") == login
        and FILED_MARKER in str(r.get("body") or "")
    ]
    if not eligible:
        return ""
    return str(min(eligible, key=lambda r: int(r.get("number") or 0))["url"])


def _already_filed(repo: str, body: str, title: str) -> str:
    """The open OS-filed issue for a run this report cites, or `""` (issue 1212)"""
    for run_id in run_ids_in(title, body):
        try:
            existing = open_os_filed_issue(repo, run_id)
        except Exception:  # noqa: BLE001 — an unreadable tracker files the report as usual
            return ""
        if existing:
            return existing
    return ""


def _rubric() -> str:
    """`issues.PRIORITY_RUBRIC`, imported late"""
    from .issues import PRIORITY_RUBRIC
    return PRIORITY_RUBRIC


def _route(url: str, title: str, body: str, priority: str,
           expedite: bool = False) -> dict[str, Any]:
    """Put the freshly filed issue on the OS's rails"""
    from .issues import route_filing
    try:
        return route_filing(url, title, body, priority, expedite=expedite)
    except Exception as e:  # noqa: BLE001 — see the docstring: the issue is already filed
        return {"wo_id": "", "backlog_id": "", "project": "", "priority": priority,
                "reason": f"the OS could not route it ({e}) — it is filed and untracked"}


def pickup_note(pickup: dict[str, Any]) -> str:
    """The one line the user reads about what happened after the issue was created"""
    level = pickup.get("priority") or "?"
    if pickup.get("wo_id"):
        # `expedited` and `confirmed` are different facts about the same work order: one says the user asked...
        verb = "expedited" if pickup.get("expedited") else "confirmed"
        head = (f"`{level}` {verb} — work order {pickup['wo_id']} in "
                f"{pickup.get('project') or '?'}")
    elif pickup.get("project"):
        head = f"`{level}`"
    else:
        head = f"`{level}` — not tracked"
    line = f"{head}: {pickup.get('reason') or ''}".rstrip(": ")
    if pickup.get("label_error"):
        line += (f" (the priority label did NOT reach the issue: "
                 f"{pickup['label_error']}; Jarvis retries on its next tick)")
    return line


def _notify(project: str, title: str, url: str, version: str, wo_id: str,
            pickup: dict[str, Any] | None = None, heading: str = "Bug filed") -> None:
    """Put the issue link in front of the user via the normal inbox -> sinks path"""
    from .central_store import CentralStore
    from .daemon import daemon_running

    body = f"{url}\nJarvis OS {version}" + (f" · {wo_id}" if wo_id else "")
    if pickup is not None:
        body += f"\n{pickup_note(pickup)}"
    central = CentralStore()
    try:
        central.add_inbox(project or "os",f"{heading}: {title}",
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


AGENT_EXPEDITE_NOTE = ("--expedite ignored: filed from a work-order session, so the issue "
                       "is filed only — the user decides whether to start it")


def report_bug(*, title: str, description: str, expected: str, actual: str,
               priority: str = "", steps: str = "", project: str = "",
               wo_id: str = "", expedite: bool = False) -> dict[str, Any]:
    """File a Jarvis OS bug, put it on the OS's rails, and tell the user about it"""
    from .issues import checked_priority

    if not title.strip():
        raise BugReportError("a bug report needs a title")
    if not (priority or "").strip():
        raise BugReportError(
            "a bug report needs a priority — there is no default, because the priority "
            "is the only thing that decides what happens to the report.\n\n"
            + _rubric())
    try:
        priority = checked_priority(priority)
    except ValueError as e:
        raise BugReportError(str(e)) from e
    project = project or os.environ.get("JARVIS_PROJECT", "")
    note = ""
    if expedite and os.environ.get("JARVIS_WO_ID"):
        expedite = False  # an agent files only; expediting is the user's call
        note = AGENT_EXPEDITE_NOTE
    wo_id = wo_id or os.environ.get("JARVIS_WO_ID", "")
    version = jarvis_version()
    repo = bug_repo()
    body = render_body(description=description, expected=expected, actual=actual,
                       version=version, project=project, wo_id=wo_id, steps=steps,
                       priority=priority)
    existing = _already_filed(repo, body, title)
    if existing:
        from .issues import comment
        try:
            comment(existing, f"{DUPLICATE_NOTE}\n\n### {title}\n\n{body}", repo)
        except Exception as e:  # noqa: BLE001 — never file a second issue for this break
            raise BugReportError(
                f"this break is already filed as {existing}, and the evidence could "
                f"not be added there ({e}) — retry the report") from e
        _notify(project, title, existing, version, wo_id, heading="Bug evidence added")
        out = {"url": existing, "duplicate_of": existing, "title": title, "repo": repo,
               "version": version, "project": project, "wo_id": wo_id, "pickup": {}}
        if note:
            out["note"] = note
        return out
    url = create_issue(title, body, repo)
    # Between creating the issue and telling anyone about it (#240)
    pickup = _route(url, title, body, priority, expedite=expedite)
    _notify(project, title, url, version, wo_id, pickup)
    out = {"url": url, "title": title, "repo": repo, "version": version,
           "project": project, "wo_id": wo_id, "pickup": pickup}
    if note:
        out["note"] = note
    return out
