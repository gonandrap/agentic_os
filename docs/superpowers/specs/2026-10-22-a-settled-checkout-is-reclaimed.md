# A settled checkout is reclaimed, and the disk says so before SQLite does

Issue #1092, wo-4b6179fb. Neo questions 2199 (when a checkout is reclaimed) and 2200
(which volume is watched, and the threshold).

## Problem

Every dispatched order gets `<project>/.claude/worktrees/<wo-id>`, written by
`worker_session.start` (`src/jarvis/worker_session.py:196`), each with its own virtualenv.
The only code that ever removes one is `claude rm` in `ops.delete_work_order`
(`src/jarvis/ops.py:6015`) — i.e. only when the user erases the record. Settlement removes
nothing.

Root cause: no owner for the checkout's lifetime after settlement. One host held 355 of
them (~17G), 268 of completed orders and 31 of cancelled ones; the disk hit 100% and for
~40 minutes every SQLite write failed — `neo_store` raised `disk I/O error`, the logger hit
ENOSPC.

Second, independent gap: nothing measures free space. The first signal was one notification
per invariant from `Daemon.check_invariants` (`src/jarvis/daemon.py:4072`) reading
`invariant raised OperationalError(database or disk is full)` — the symptom, not the cause.
No module in `src/jarvis/` calls `shutil.disk_usage` or `statvfs` today.

## Fix

Both halves are fixed, not one; neither is a symptom patch.

### 1. The reclaim sweep (`disk.sweep`, new `src/jarvis/disk.py`)

A daemon pass on the reconcile cadence, re-derived from settled state rather than hooked
into each settlement route — the route audit is the half nobody does (kn-f602db09 found two
routes to `completed` that bypassed the one `ops.land_finished`'s docstring claimed), and a
sweep also reclaims the checkouts already on the host.

`store.settled_worktrees(statuses, settled_before, limit)` returns the orders that are
settled, still record a `worktree`, and carry no `worktree_reclaimed` event, oldest
`updated_at` first. `updated_at` is the settlement proxy: a terminal order's row is not
written again.

* **Statuses**: `completed`, `cancelled`. `failed` is kept — `wo retry` resumes in place.
* **Grace period**: `os.worktree.reclaim_after_hours`, default 24, so the user can open a
  just-settled checkout the next morning. `0` turns the sweep off.
* **Kept**: a checkout whose `landing.authored` reports uncommitted non-noise files (that
  predicate already excludes the kind's submission artifact, and the tool-managed paths are
  `skip-worktree` in that index), and one git could not read at all — nothing is removed
  that nobody could inspect. Neither writes an event, so a later sweep looks again; the
  daemon holds the ids it kept for the life of the process, so a permanently dirty checkout
  neither starves the queue nor costs two git calls a sweep.
* **The branch is never deleted**, so committed work survives the removal: only uncommitted
  files are at risk, which is what the keep rule protects.
* Removal is `git worktree remove --force <path>` run in the project root. `--force`
  because the checkout holds its own ignored virtualenv; the dirty check above is this
  code's own, not git's.
* Bound: `MAX_PER_SWEEP = 20` removals per project per sweep — each is a subprocess.
* One `worktree_reclaimed` event per order, with a `timeline._describe` branch and NOT in
  `DEBUG_KINDS` (kn-3f133363): once per order lifetime, and it changes what the user can
  still open.

### 2. The warning (`Daemon.check_disk_space`)

`disk.state_volumes(catalog)` reads `$JARVIS_HOME` and every catalog project path,
deduplicated by `st_dev` — the three SQLite databases do not share one path — and returns a
reading per filesystem, least free first. Under `os.disk_free_warn_gb` (default 5.0) the
daemon raises ONE central inbox item naming the worst volume, the floor, and the cause.
Gigabytes rather than a percentage: a percentage of a large volume is not a number anyone
can act on.

Re-armed only on recovery: the `disk_low_warned` central state key holds the warning and is
cleared when free space rises back above the floor. A standing shortage says nothing
further.

Run on the reconcile cadence beside `check_ui_log`, under the same discipline — a failure
here must never stall the tick.

### 3. Tests

`tests/test_worktree_reclaim.py`, against a real git worktree: reclaimed past the grace
period, kept inside it, kept while dirty, kept when `failed`, the event written once, the
branch still present after the removal, the sweep off at `0`, and the warning raised once,
re-armed on recovery, naming the volume with least free space.
