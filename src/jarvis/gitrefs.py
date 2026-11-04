"""A clone is a cache of origin — the one layer that refreshes it (io-c1bbfff4)"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("jarvis.gitrefs")

#: A ref BECOMES AN ARGUMENT, and one of the callers passes `baseRefName` as GitHub answered it
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

#: A token sits in the remote URL git prints back, so scrub BEFORE truncating
_CREDENTIALS_RE = re.compile(r"://[^/\s@]+@")

#: Same ceiling as `branchproof.GIT_TIMEOUT` for one named ref; a tag refresh may walk more
FETCH_TIMEOUT = 60

#: How long one repo's refresh stands for every reader of that repo
TTL = 300.0

@dataclass(frozen=True)
class Refresh:
    """What the last refresh of one repo did, for a caller that wants to say so"""

    ok: bool
    ts: float
    error: str = ""


_LAST: dict[str, Refresh] = {}


def scrub(text: str) -> str:
    """`text` with any URL userinfo replaced"""
    return _CREDENTIALS_RE.sub("://<redacted>@", text)


def env() -> dict[str, str]:
    """`git` with nothing of the user's configuration and NOTHING INTERACTIVE"""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true",
            "GIT_CONFIG_NOSYSTEM": "1", "GCM_INTERACTIVE": "never"}


def forget(repo: Path | None = None) -> None:
    """Drop the memory of a refresh — the next read of `repo` (or of any repo) fetches again"""
    _LAST.pop(str(repo), None) if repo is not None else _LAST.clear()


def refresh(repo: Path, *, force: bool = False) -> Refresh:
    """Branches and tags down from origin, at most once per `TTL` — failure cached too"""
    cached = _LAST.get(str(repo))
    if not force and cached and time.time() - cached.ts < TTL:
        return cached
    error = _fetch(repo, "--tags", "--force", "--quiet", "origin")
    if error:
        log.debug("could not refresh %s from origin: %s", repo, error)
    out = Refresh(ok=not error, ts=time.time(), error=error)
    _LAST[str(repo)] = out
    return out


def fetch_ref(repo: Path, *refs: str) -> bool:
    """Bring named `refs` down from origin — no tags, so this is never the repo's refresh"""
    wanted = [r.split("/", 1)[1] if r.startswith("origin/") else r for r in refs]
    if not wanted or not all(REF_RE.match(r or "") for r in wanted):
        log.warning("refusing to fetch %r into %s — not a ref this may ask for",
                    list(refs), repo)
        return False
    error = _fetch(repo, "--quiet", "origin", *wanted)
    if error:
        log.warning("git fetch origin %s in %s failed: %s", " ".join(wanted), repo, error)
    return not error


def read(repo: Path, *args: str, timeout: int = 60) -> str:
    """`git -C repo args` on a repo refreshed first, raising as `subprocess.run(check=True)` does"""
    refresh(repo)
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                          capture_output=True, timeout=timeout, env=env()).stdout


def _fetch(repo: Path, *args: str) -> str:
    """The one spawn of `git fetch` in the OS — '' when it worked, else why it did not"""
    try:
        proc = subprocess.run(["git", "-C", str(repo), "fetch", *args], check=False,
                              capture_output=True, text=True, errors="replace",
                              timeout=FETCH_TIMEOUT, env=env())
    except (OSError, subprocess.SubprocessError) as exc:
        return scrub(f"{type(exc).__name__}: {exc}")[:200]
    return "" if proc.returncode == 0 else (
        scrub((proc.stderr or proc.stdout).strip().replace("\n", " "))[:200]
        or f"git fetch exited {proc.returncode}")
