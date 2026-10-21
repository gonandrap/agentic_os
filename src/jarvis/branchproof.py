"""What the LOCAL checkout can prove about a pull request's branch (docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §3.1)"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from pathlib import Path

log = logging.getLogger("jarvis.branchproof")

#: Same ceiling as `ci.CI_TIMEOUT`
GIT_TIMEOUT = 30

#: A ref BECOMES AN ARGUMENT, and one of the two callers passes `baseRefName` as GitHub answered it...
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

#: A commit, at any length `git` accepts
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _env() -> dict[str, str]:
    """`git` with nothing of the user's configuration and NOTHING INTERACTIVE"""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true",
            "GIT_CONFIG_NOSYSTEM": "1", "GCM_INTERACTIVE": "never"}


def attempt(repo: Path, *args: str, stdin: str | None = None) -> tuple[str | None, str]:
    """`run`, with GIT'S OWN STDERR beside the stdout — (stdout or None, stderr) (docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md §7)"""
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, errors="replace", timeout=GIT_TIMEOUT,
                              check=False, env=_env(), input=stdin)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("git %s in %s could not run: %s", args[0], repo, exc)
        return None, ""
    if proc.returncode != 0:
        log.debug("git %s in %s exited %d: %s", " ".join(args), repo, proc.returncode,
                  proc.stderr.strip()[:200])
        return None, proc.stderr.strip()
    return proc.stdout, ""


def run(repo: Path, *args: str, stdin: str | None = None) -> str | None:
    """`git -C repo args`, or None on any failure (docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md §2)"""
    return attempt(repo, *args, stdin=stdin)[0]


#: The in-module name, unchanged: every existing call site reads `_git`
_git = run


def _git_bytes(repo: Path, *args: str) -> bytes | None:
    """`_git`'s contract — None on any failure, never raises — with the OUTPUT AS BYTES"""
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              timeout=GIT_TIMEOUT, check=False, env=_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("git %s in %s could not run: %s", args[0], repo, exc)
        return None
    if proc.returncode != 0:
        log.debug("git %s in %s exited %d: %s", " ".join(args), repo, proc.returncode,
                  proc.stderr.decode("utf-8", "replace").strip()[:200])
        return None
    return proc.stdout


def fetch(repo: Path, *refs: str) -> bool:
    """Bring `refs` down from `origin`, once per carry attempt"""
    wanted = [r.split("/", 1)[1] if r.startswith("origin/") else r for r in refs]
    if not wanted or not all(REF_RE.match(r or "") for r in wanted):
        log.warning("refusing to fetch %r into %s — not a ref this may ask for",
                    list(refs), repo)
        return False
    if _git(repo, "fetch", "--quiet", "origin", *wanted) is None:
        log.warning("git fetch origin %s in %s failed — no local proof can be computed "
                    "about this branch", " ".join(wanted), repo)
        return False
    return True


#: The two pieces of a diff that a BASE MERGE legitimately moves without the branch's contribution...
_HUNK_RANGE_RE = re.compile(rb"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.M)
_INDEX_IDS_RE = re.compile(rb"^index [0-9a-f]+\.\.[0-9a-f]+", re.M)

#: A BINARY file's diff is exempt from the `index` rule
_INDEX_NEW_ID_RE = re.compile(rb"^index [0-9a-f]+\.\.([0-9a-f]+)", re.M)
_BINARY_RE = re.compile(rb"^(?:Binary files .* differ|GIT binary patch)$", re.M)
_PER_FILE_RE = re.compile(rb"^(?=diff --git )", re.M)


def _normalise(diff: bytes) -> bytes:
    """Drop what a base merge moves, per file, keeping binary content in the hash"""
    out = []
    for section in _PER_FILE_RE.split(diff):
        if _BINARY_RE.search(section):
            out.append(_INDEX_NEW_ID_RE.sub(rb"index \1", section))
        else:
            out.append(_INDEX_IDS_RE.sub(b"index", _HUNK_RANGE_RE.sub(b"@@ @@", section)))
    return b"".join(out)


def diff_fingerprint(repo: Path, base_ref: str, sha: str) -> str | None:
    """What `sha`'s branch ADDS on top of its merge base with `origin/<base_ref>`, as an id"""
    branch = base_ref.split("/", 1)[1] if base_ref.startswith("origin/") else base_ref
    if not REF_RE.match(branch or "") or not SHA_RE.match(sha or ""):
        log.warning("refusing to diff %r against %r in %s", sha, base_ref, repo)
        return None
    merge_base = _git(repo, "merge-base", f"origin/{branch}", sha)
    if not merge_base or not merge_base.strip():
        return None
    diff = _git_bytes(repo, "diff", "--full-index", "--no-ext-diff", "--no-textconv",
                      f"{merge_base.strip()}..{sha}")
    if not diff:
        return None
    return hashlib.sha256(_normalise(diff)).hexdigest()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """Is `ancestor` reachable from `descendant`?"""
    if not SHA_RE.match(ancestor or "") or not REF_RE.match(descendant or ""):
        log.debug("not asking whether %r is an ancestor of %r", ancestor, descendant)
        return False
    return _git(repo, "merge-base", "--is-ancestor", ancestor, descendant) is not None


def tip(repo: Path, ref: str) -> str:
    """What commit is `ref` at, locally, right now? (docs/superpowers/specs/2026-09-28-a-merge-checks-the-base-it-lands-on.md §3.1.)"""
    if not REF_RE.match(ref or ""):
        log.debug("not resolving %r in %s", ref, repo)
        return ""
    out = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return (out or "").strip()
