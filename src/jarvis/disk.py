"""Reclaim a settled order's checkout, and read the volumes OS state lives on (docs/superpowers/specs/2026-10-22-a-settled-checkout-is-reclaimed.md)"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import branchproof, db, landing
from .paths import jarvis_home
from .project_store import WORKTREE_RECLAIMED_EVENT, submission_artifacts

log = logging.getLogger("jarvis.disk")

EVENT = WORKTREE_RECLAIMED_EVENT

#: Statuses whose checkout is never resumed — not `failed`, which `wo retry` resumes in place
RECLAIMABLE_STATUSES = ("completed", "cancelled")

#: Removals one sweep performs for one project — each is a `git worktree remove` subprocess
MAX_PER_SWEEP = 20

SECONDS_PER_HOUR = 3600
BYTES_PER_GB = 1024 ** 3

#: Untracked directories a worker's own tooling regenerates — never its work (#1257)
REGENERABLE_NAMES = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".ipynb_checkpoints", ".hypothesis", "htmlcov", ".coverage", ".DS_Store",
    "node_modules",
})

#: …and the files those tools drop beside the sources
REGENERABLE_SUFFIXES = (".pyc", ".pyo", ".egg-info")


def pytest_temproot(wo_id: str) -> Path:
    """The order's own PYTEST_DEBUG_TEMPROOT, so a sibling's pytest never garbage-collects its tmp_path (#723)"""
    return Path(tempfile.gettempdir()) / "jarvis-pytest" / wo_id


def make_dirs_writable(worktree: Path) -> int:
    """Owner-write on read-only dirs, whose files a removal cannot unlink (EACCES, wo-b6e2ddda)"""
    fixed = 0
    for path in [worktree, *worktree.rglob("*")]:
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            mode = path.stat().st_mode
            if not mode & 0o200:
                path.chmod(mode | 0o200)
                fixed += 1
        except OSError as exc:
            log.debug("could not make %s writable: %s", path, exc)
    return fixed


def prune_build_caches(worktree: Path, artifacts: Iterable[str] = ()) -> list[str]:
    """Delete UNTRACKED regenerable caches and consumed `artifacts`, which read as work (#1257)"""
    consumed = set(artifacts)
    # `=all` so an untracked DIRECTORY cannot hide what it holds behind its own name
    status = branchproof.run(worktree, "--no-optional-locks", "status", "--porcelain",
                             "-z", "--untracked-files=all")
    if not status:
        return []
    pruned: list[str] = []
    for entry in status.split("\0"):
        if not entry.startswith("??"):
            continue
        rel = entry[3:] if entry[3:] in consumed else _regenerable_root(entry[3:])
        if rel is None or rel in pruned:
            continue
        target = worktree / rel
        if not _inside(worktree, target):
            continue
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("could not prune %s: %s", target, exc)
            continue
        pruned.append(rel)
    return pruned


def _regenerable_root(rel: str) -> str | None:
    """The cache this untracked path belongs to — the directory, not the file inside it"""
    parts = [p for p in rel.rstrip("/").split("/") if p]
    for i, part in enumerate(parts):
        if part in REGENERABLE_NAMES:
            return "/".join(parts[:i + 1])
    return rel.rstrip("/") if parts and parts[-1].endswith(REGENERABLE_SUFFIXES) else None


def _inside(worktree: Path, target: Path) -> bool:
    """git never reports a path outside the worktree; a symlinked one must not be followed"""
    try:
        return target.resolve().is_relative_to(worktree.resolve())
    except OSError:
        return False


def sweep(store: Any, project: Any, *, skip: Iterable[str] = (),
          now: float | None = None,
          limit: int = MAX_PER_SWEEP) -> tuple[list[str], list[str]]:
    """Reclaim the checkouts of orders settled past the grace period: (reclaimed, kept) (spec §1)"""
    hours = getattr(project.worktree, "reclaim_after_hours", 0)
    if hours <= 0:
        return [], []
    held = set(skip)
    now = db.now() if now is None else now
    rows = store.settled_worktrees(RECLAIMABLE_STATUSES, now - hours * SECONDS_PER_HOUR,
                                   limit=limit + len(held))
    reclaimed: list[str] = []
    kept: list[str] = []
    for row in rows:
        if row["id"] in held:
            continue
        if len(reclaimed) >= limit:
            break
        payload, why = _reclaim_one(Path(project.path), row)
        if payload is None:
            log.info("[%s] keeping %s's checkout: %s", project.name, row["id"], why)
            kept.append(row["id"])
            continue
        store.add_event(row["id"], EVENT, payload)
        reclaimed.append(row["id"])
    return reclaimed, kept


def _reclaim_one(project_path: Path,
                 row: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """(the event payload for a checkout that is gone now, "") or (None, why it was kept)"""
    name = str(row["worktree"])
    if Path(name).is_absolute():
        return None, "a launcher harness opened it, and the harness removes its own worktree"
    path = project_path / ".claude" / "worktrees" / name
    if not path.exists():
        return {"worktree": name, "removed": False, "why": "already gone"}, ""
    authored = landing.authored(path, submission_artifacts(row))
    if authored.unreadable:
        # Never remove what nobody could inspect (spec §1)
        return None, authored.unreadable
    if authored.dirty:
        return None, f"{len(authored.dirty)} uncommitted file(s): {authored.describe()}"
    make_dirs_writable(path)
    out, stderr = branchproof.attempt(project_path, "worktree", "remove", "--force",
                                      str(path))
    if out is None:
        return None, f"git would not remove it: {' '.join(stderr.split())[:200]}"
    shutil.rmtree(pytest_temproot(str(row["id"])), ignore_errors=True)
    return {"worktree": name, "removed": True, "branch": authored.branch,
            "commits": authored.commits}, ""


@dataclass(frozen=True)
class Volume:
    """What one filesystem holding OS state has left"""

    path: Path
    free_bytes: int
    total_bytes: int

    @property
    def free_gb(self) -> float:
        return self.free_bytes / BYTES_PER_GB

    @property
    def total_gb(self) -> float:
        return self.total_bytes / BYTES_PER_GB


def state_volumes(catalog: Any) -> list[Volume]:
    """One reading per distinct filesystem the OS keeps state on, least free first (spec §2)"""
    seen: set[int] = set()
    found: list[Volume] = []
    for path in [jarvis_home(), *(Path(p.path) for p in catalog.projects)]:
        try:
            device = path.stat().st_dev
            if device in seen:
                continue
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        seen.add(device)
        found.append(Volume(path, usage.free, usage.total))
    return sorted(found, key=lambda v: v.free_bytes)
