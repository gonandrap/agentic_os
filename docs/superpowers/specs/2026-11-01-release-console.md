# Release console — `/release`

wo-bbdd168a.

## Problem

The dashboard has no release surface. What the next release would carry is only visible
by running `git log <newest tag>..origin/main` by hand; cutting one means typing the
shipit command from a session; and nothing maps a past `jarvis-*` tag back to the work,
feature, improvement and investigation orders it carries or to whether morpheus validated
each PR in production. Root cause: release data lives in git and `morpheus_items`, and no
page joins them to the order records.

## Fix

A page `/release` under **system** (`src/jarvis/ui/app.py`, `ops.release_*`,
`src/jarvis/release_view.py`) that joins the three.

### 1. Sources

- **Pending** = first-parent commits on `origin/main` (else `main`) after the newest
  `jarvis-*` tag. **A release's contents** = first-parent commits between it and the
  previous tag, the range `morpheus.enqueue_release` already reads. `Release jarvis-*`
  bump commits are dropped.
- Repository: the catalog's OS project checkout (`schedule.os_project`), else production's.
- Each commit gives a PR number (`morpheus.pr_numbers`) and a work-order id from the
  `[wo-xxxxxxxx]` subject prefix, falling back to the morpheus item's `wo_id`.
- Validation colour comes from `morpheus_items` matched on the PR number in the OS repo:
  `validated` green, `queued`/`monitoring` = pending, `failed`/`timedout` = failed, none =
  unwatched. Each links to `/morpheus/<mv-id>`.
- Trace: the work order, its parent order (`parent_id`: feature, improvement,
  investigation), that parent's own evidence (`io` refs, an investigation's subject), and
  the order's `trigger_wo_id`, `backlog_id`, `issue_url`. Every id links through `/go/<id>`.

Tags never move, so a tag's commit range is cached in-process by `(repo, previous, tag)`.

### 2. Cutting (Q2850)

shipit ships everything up to ONE `--base` sha, so selection is a **prefix cut**: ticking a
pending PR ticks every older one, and the server recomputes the prefix from the chosen
base — it never trusts a client list. "Add all" picks the newest pending commit.

The button never runs shipit. It files a **release work order** in the OS project: metadata
`release.BATCH_KEY = []` (so release machinery and the panel skip apply) plus
`release.BASE_KEY = <sha>`, and a description naming every PR and the exact command
`scripts/shipit.sh --stage --wo <id> --base <sha>`. The worker ships under the ordinary
release gate. A pinned base is never widened: `Daemon.ensure_release` does not batch a
landed fix into an order carrying `BASE_KEY`.

A cut is refused while any release order is still open, naming it — concurrent release
orders are how empty versions shipped before (kn-4f5aaa2b).

Out of scope: arbitrary non-prefix subsets (a cherry-pick mode in shipit).
