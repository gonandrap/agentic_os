"""What the next release would carry and what each past one did (docs/superpowers/specs/2026-11-01-release-console.md)"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import morpheus, release

#: (repo, previous tag, tag) to its commits; tags never move, so this never goes stale
_RANGE_CACHE: dict[tuple[str, str, str], list[dict[str, Any]]] = {}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, timeout=60).stdout


def main_ref(repo: Path) -> str:
    """`origin/main` when the clone has it, else `main`"""
    for ref in ("origin/main", "main"):
        try:
            _git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
            return ref
        except subprocess.CalledProcessError:
            continue
    return "HEAD"


def tags(repo: Path) -> list[dict[str, Any]]:
    """Every `jarvis-*` tag, newest version first, each with its previous one"""
    out = _git(repo, "for-each-ref", "--sort=version:refname",
               "--format=%(refname:short)%1f%(creatordate:unix)",
               f"refs/tags/{release.RELEASE_TAG_GLOB}")
    rows, previous = [], ""
    for line in out.splitlines():
        name, _, ts = line.partition("\x1f")
        if not name:
            continue
        rows.append({"tag": name, "ts": float(ts or 0), "previous": previous})
        previous = name
    return rows[::-1]


def commits(repo: Path, from_ref: str, to_ref: str) -> list[dict[str, Any]]:
    """First-parent commits of `from..to`, newest first, release bump commits dropped"""
    span = f"{from_ref}..{to_ref}" if from_ref else to_ref
    out = _git(repo, "log", "--first-parent", "--format=%H%x1f%ct%x1f%s", span)
    rows = []
    for line in out.splitlines():
        sha, ts, subject = (line.split("\x1f", 2) + ["", ""])[:3]
        if not sha or subject.startswith("Release jarvis-"):
            continue
        numbers = morpheus.pr_numbers([subject])
        rows.append({"sha": sha, "short": sha[:8], "ts": float(ts or 0), "subject": subject,
                     "pr": numbers[0] if numbers else None,
                     "wo_id": morpheus.wo_of_title(subject)})
    return rows


def release_commits(repo: Path, tag: str, previous: str) -> list[dict[str, Any]]:
    key = (str(repo), previous, tag)
    if key not in _RANGE_CACHE:
        _RANGE_CACHE[key] = commits(repo, previous, tag)
    return [dict(r) for r in _RANGE_CACHE[key]]


def prefix(pending: list[dict[str, Any]], base: str) -> list[dict[str, Any]]:
    """The pending commits a release cut at `base` ships: `base` and every older one (Q2850)"""
    for i, row in enumerate(pending):  # newest first
        if row["sha"] == base:
            return pending[i:]
    return []


#: morpheus item state to the colour a release row shows
VALIDATION = {"validated": "validated", "queued": "pending", "monitoring": "pending",
              "failed": "failed", "timedout": "failed"}


def validation_of(item: dict[str, Any] | None) -> str:
    return VALIDATION.get(str((item or {}).get("state") or ""), "unwatched")


def summarize(rows: list[dict[str, Any]]) -> str:
    """One word for a whole release: failed beats pending beats validated"""
    states = {r["validation"] for r in rows}
    for s in ("failed", "pending", "validated"):
        if s in states:
            return s
    return "unwatched"
