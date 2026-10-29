---
type: Jarvis Operator Reference
title: Projects
description: Putting a project into the OS and taking it back out — adopt, offboard, archive versus purge, re-adopting.
tags: [operator, cli, projects]
---
# Projects

A project lives in three places: its entry in the catalog file, its `projects` row (plus
project-scoped rows) in `$JARVIS_HOME/os.db` and `neo.db`, and the files the OS put in its
repo. Spec: `docs/superpowers/specs/2026-10-28-offboard-a-project.md`.

## Adopting

- `jarvis adopt <path> [--dry-run]` bootstraps the repo only: `.claude/settings.json`
  hooks, `.claude/skills/`, `OPERATION.md`, `.jarvis/`, a `.gitignore` line and workspace
  trust. It registers nothing.
- Registration is the catalog entry plus `jarvis start --catalog <file>`, which bootstraps
  and registers every catalog project. `jarvis onboard start <path>` does both through an
  interview ([Onboarding](/operator/onboarding.md)).

## Offboarding

```
jarvis offboard <name> [--purge --yes] [--force] [--dry-run] [--reason "…"] [--catalog <path>] [--json]
```

Takes a **name**, not a path. Always run `--dry-run` first: it prints the plan — open
orders, files the strip would remove or refuse, the catalog change — and writes nothing.

In order: refuse if any order is open (unless `--force`), cancel open orders and their
sessions, drop open backlog rows and ack unacked inbox rows, strip the repo, archive or
purge the registry, remove the catalog entry as one config version (`jarvis config
history`), and file one inbox row under `os`.

- Open orders are listed by id and status and refused; `--force` cancels them.
- `--reason` goes on the config version row. Removing a project removes its safety
  settings, so a reason is required whenever the project is in the catalog.
- The project that runs the OS itself is refused. No flag overrides that.
- A worker session may not offboard.
- The strip never removes a file you edited: a hand-edited `settings.json`, a committed or
  edited `OPERATION.md`, a changed skill stay, each named as a warning. A missing project
  directory is a warning too; the registry and catalog still move. Run it again to finish a
  strip that failed.

## Archive versus purge

| | archive (default) | purge (`--purge --yes`) |
|---|---|---|
| `projects` row | kept, `status='archived'` | deleted |
| `<project>/.jarvis/` (every order, event, turn) | kept | deleted, with its `.gitignore` line |
| knowledge, backlog, steward, Neo learnings | kept | deleted |
| `jarvis search` | still finds it, marked archived | finds nothing |
| reversible | yes | **no** |

Purge is irreversible. `--purge` without `--yes` is refused. Purging an archived project is
supported: archive first, purge later. `--dry-run --purge` names the purge but does not
count its rows; the real run reports per-table counts under `purged`.

## Re-adopting

Archived only: put the entry back in the catalog and run `jarvis start --catalog <file>`.
`start` re-bootstraps the repo and sets the row back to `active`. No un-archive verb
exists. A purged project starts from nothing.
