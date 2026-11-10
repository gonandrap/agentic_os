"""What the next release would carry and what each past one did (docs/superpowers/specs/2026-11-01-release-console.md)"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import morpheus, release

#: (repo, tag, the tags it follows) to its commits; tags never move, so this never goes stale
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


def _is_ancestor(repo: Path, a: str, b: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", a, b],
                          capture_output=True, timeout=60).returncode == 0


def tags(repo: Path, glob: str = release.RELEASE_TAG_GLOB, main: str = "") -> list[dict[str, Any]]:
    """Tags matching `glob`, newest anchor first, each with the tags it `follows` (spec §3)"""
    out = _git(repo, "for-each-ref", "--sort=version:refname",
               "--format=%(refname:short)%1f%(creatordate:unix)%1f%(*objectname)%1f%(objectname)",
               f"refs/tags/{glob}")
    rows = []
    for line in out.splitlines():
        name, ts, peeled, sha = (line.split("\x1f") + ["", "", ""])[:4]
        if name:
            rows.append({"tag": name, "ts": float(ts or 0), "sha": peeled or sha})
    rows.reverse()  # newest version first: the tie-break among tags on one anchor
    for i, r in enumerate(rows):
        r["rank"] = i
    if not rows:
        return rows
    main = main or main_ref(repo)
    spine = {sha: i for i, sha in enumerate(
        _git(repo, "rev-list", "--first-parent", main).split())}
    side: dict[str, str] = {}  # release-branch commit to its first parent
    for line in _git(repo, "log", "--first-parent", "--format=%H %P",
                     *{r["sha"] for r in rows}, "--not", main).splitlines():
        sha, *parents = line.split()
        side[sha] = parents[0] if parents else ""
    for r in rows:  # anchor: the main commit the tag was cut from
        c = r["sha"]
        while c in side:
            c = side[c]
        r["pos"] = spine.get(c)
    off = len(spine) + 1
    rows.sort(key=lambda r: off if r["pos"] is None else r["pos"])
    for r in rows:
        r["follows"] = [o["tag"] for o in rows if o is not r and _precedes(repo, o, r)]
        r["previous"] = r["follows"][0] if r["follows"] else ""
    for r in rows:
        del r["pos"], r["sha"], r["rank"]
    return rows


def _precedes(repo: Path, a: dict[str, Any], b: dict[str, Any]) -> bool:
    """`a` shipped before `b`: older anchor, else older version, off main real ancestry"""
    if a["pos"] is not None and b["pos"] is not None:
        return a["pos"] > b["pos"] if a["pos"] != b["pos"] else a["rank"] > b["rank"]
    return a["sha"] != b["sha"] and _is_ancestor(repo, a["sha"], b["sha"])


def commits(repo: Path, to_ref: str, exclude: list[str]) -> list[dict[str, Any]]:
    """First-parent commits of `to_ref` no `exclude` reaches, newest first, bumps dropped"""
    args = [to_ref, "--not", *exclude] if exclude else [to_ref]
    out = _git(repo, "log", "--first-parent", "--format=%H%x1f%ct%x1f%s", *args)
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


def release_commits(repo: Path, tag: str, follows: list[str]) -> list[dict[str, Any]]:
    key = (str(repo), tag, tuple(follows))
    if key not in _RANGE_CACHE:
        _RANGE_CACHE[key] = commits(repo, tag, follows)
    return [dict(r) for r in _RANGE_CACHE[key]]


def prefix(pending: list[dict[str, Any]], base: str) -> list[dict[str, Any]]:
    """The pending commits a release cut at `base` ships: `base` and every older one (Q2850)"""
    for i, row in enumerate(pending):  # newest first
        if row["sha"] == base:
            return pending[i:]
    return []


#: morpheus item state to the colour a release row shows
VALIDATION = {"validated": "validated", "unverified": "unverified", "queued": "pending",
              "monitoring": "pending", "failed": "failed", "timedout": "failed"}


def validation_of(item: dict[str, Any] | None) -> str:
    return VALIDATION.get(str((item or {}).get("state") or ""), "unwatched")


def summarize(rows: list[dict[str, Any]]) -> str:
    """One word for a whole release: failed beats pending beats unverified beats validated"""
    states = {r["validation"] for r in rows}
    for s in ("failed", "pending", "unverified", "validated"):
        if s in states:
            return s
    return "unwatched"
