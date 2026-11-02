# An expedited merge ships alone

Work order wo-afb1c9e3. Design approved by Neo, Q2841 ("all five as proposed", plus the
squash-only condition). Supersedes the trigger half of
`2026-09-27-an-expedited-order-that-lands-ships-a-release.md`.

## What is broken

User wants to flag any open work order from the dashboard so its merge reaches
production at once, carrying ONLY that order, not what is stacked on main. A later normal
release must go out as usual, no conflict. Today:

- `expedited` is set only at filing, by `bug report --expedite`
  (`issues.route_filing`, `issues.promote_confirmed`). No CLI or UI sets it on an open order.
- The release fires from `Daemon.sync_issues`, on tracker close. An order with no
  `issue_url` never reaches it, so it never ships.
- `RELEASE_BRIEF` cuts main's head, so the release carries everything merged since the
  last tag.

## Root cause

Expedite was modelled as an issue property and its release as "ship main sooner". Neither
the trigger (tracker) nor the cut (main's head) can express "this merge, alone".

## Fix

### §1 Flag

One flag, one meaning: `metadata.expedited` (`issues.EXPEDITED_KEY`), same key bug-report
sets. Toggled by `ops.expedite_work_order` from:

- `jarvis wo expedite <id> [--off]`
- the Expedite / Un-expedite button on the work order page (`POST /wo/{p}/{id}/expedite`)

Refused on completed, failed or cancelled orders (merge already behind them) and on
release orders. Each toggle writes an `expedited` / `unexpedited` event.

### §2 Trigger

Merge observation, not the tracker: `Daemon.ship_expedited` runs after
`ops.complete_merged` in `poll_pull_requests` and in `auto_merge`. It re-reads the order
and calls `ensure_release` when expedited. `sync_issues` keeps only the confirmed
dispatching-priority clause and skips expedited orders, so one never ships twice.
Self-dispatch off still files nothing (unchanged gate in `ensure_release`).

### §3 Release order

`ensure_release` batches by issue url, falling back to `pr_url` for an order with no
issue. An expedited order gets a release order of its own kind: `metadata.cherry_pick =
true`, title `Ship <title> alone`, brief `HOTFIX_BRIEF`. Cherry-pick orders batch only with
other cherry-pick orders; main releases only with main releases.

### §4 Release script

`shipit.sh --cherry-pick <sha>[,<sha>...]` (with `--stage --wo` as usual):

- release branch cut from the latest tag's commit, not main; each sha applied with
  `cherry-pick -x`, then the usual bump, tag, push, deploy.
- each sha: full 40 hex, a commit here, ancestor of origin/main, not a merge commit, subject
  ends in `(#N)`. Anything else dies with the reason (Neo's condition: never guess a range).
- conflict: abort, drop the local branch, die. Nothing tagged or pushed. No fallback to a
  main release.
- excludes `--base` and `--deploy`.
- notes and Morpheus read `<previous tag>..<new tag>`.

Why there: main is never touched and tags live on release branches, so the next normal
release cuts main as always. Its notes list the expedited PR again (it is on main past the
previous tag); accepted.
