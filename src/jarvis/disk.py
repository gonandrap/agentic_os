"""Reclaim a settled order's checkout, and read the volumes OS state lives on (docs/superpowers/specs/2026-10-22-a-settled-checkout-is-reclaimed.md)"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import branchproof, db, landing
from .paths import jarvis_home
from .project_store import WORKTREE_RECLAIMED_EVENT, submission_artifacts

log = logging.getLogger("jarvis.disk")

EVENT = WORKTREE_RECLAIMED_EVENT

#: Statuses whose checkout is never resumed. `failed` is absent on purpose: `wo retry` resumes in place
RECLAIMABLE_STATUSES = ("completed", "cancelled")

#: Removals one sweep performs for one project — each is a `git worktree remove` subprocess
MAX_PER_SWEEP = 20

SECONDS_PER_HOUR = 3600
BYTES_PER_GB = 1024 ** 3


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
    path = project_path / ".claude" / "worktrees" / name
    if not path.exists():
        return {"worktree": name, "removed": False, "why": "already gone"}, ""
    authored = landing.authored(path, submission_artifacts(row))
    if authored.unreadable:
        # Never remove what nobody could inspect (spec §1)
        return None, authored.unreadable
    if authored.dirty:
        return None, f"{len(authored.dirty)} uncommitted file(s): {authored.describe()}"
    out, stderr = branchproof.attempt(project_path, "worktree", "remove", "--force",
                                      str(path))
    if out is None:
        return None, f"git would not remove it: {' '.join(stderr.split())[:200]}"
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
