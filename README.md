# Jarvis — an agentic OS for Claude Code

Jarvis is an OS layer that every Claude Code session sits on top of. Instead of running
isolated sessions per project, you register your projects in a catalog and Jarvis:

- **Orchestrates** — a daemon polls each project's queue and spawns one native Claude
  Code background worker per *work order*, each in its own git worktree.
- **Unifies configuration** — one settings baseline injected into every project;
  project-specific needs are declared in the catalog, not scattered across repos.
- **Routes notifications** — workers, monitors, and cron jobs emit through one pipeline
  (`jarvis notify`) that fans out to your inbox, logs, Telegram, or desktop.
- **Centralizes the backlog** — deferred work from any project lands in one
  dependency-aware backlog you can promote into work orders with one command.
- **Shares knowledge** — learnings reported by workers in one project are injected into
  future work orders in every project.
- **Keeps you in the loop** — `jarvis status` (or the web dashboard) shows the whole
  fleet and flags exactly what needs your attention: assumptions to review, blocked
  workers, unacked alerts.

```
you ──┬── Jarvis persona (Claude session, incl. phone)
      ├── jarvis CLI
      ├── web dashboard (jarvis ui)
      └── Claude agents view (native)
              │
        jarvis CLI/API ── jarvisd daemon ── per-project queues (.jarvis/jarvis.db)
              │                                   │
        $JARVIS_HOME/os.db                claude --bg workers (one per work order,
        (inbox, backlog, knowledge)        own git worktree, visible in agents view)
```

## Requirements

- Linux or macOS, Python 3.11+
- [Claude Code](https://code.claude.com) CLI installed and authenticated
- Your projects are git repositories

## Install

```bash
uv tool install jarvis-os        # or: pipx install jarvis-os
# from a checkout:
uv tool install --editable .
```

`jarvis` must be on PATH (workers and hooks call it).

## Quick start

1. Describe your fleet in a catalog (see `catalog.example.json`):

```json
{
  "os": {
    "defaults": { "model": "sonnet", "permission_mode": "auto", "max_concurrent": 5 },
    "notifications": { "sinks": ["log", "telegram"] }
  },
  "projects": [
    { "name": "my_app", "path": "~/workspace/my_app",
      "description": "What this project is about" }
  ]
}
```

2. Start the OS:

```bash
jarvis start --catalog catalog.json
```

This bootstraps every project (README check, OPERATION.md contract, `.jarvis/` state
dir, injected `.claude/settings.json`, and workspace trust) and starts the daemon.
Listing a project in the catalog trusts its workspace for you — no per-project trust
dialog.

3. Create work:

```bash
jarvis wo create my_app "Add dark mode to the settings page"
jarvis status                     # what's going on, what needs me?
jarvis wo send wo-1a2b3c4d "Use CSS variables, not a theme lib"   # talk to the worker
jarvis ui                         # dashboard on http://127.0.0.1:8787
```

Workers run as native Claude Code background sessions named `[WO wo-…] …` — you can
also watch and join them from `claude agents`.

## Concepts

| Thing | What it is |
|---|---|
| **Catalog** | JSON file declaring projects, models, settings overrides |
| **Work order** | A unit of work; one worker agent, one git worktree, full audit trail |
| **Origin badge** | `jarvis`/`ui` = framework-created; `manual`/`adhoc` = flagged ⚠ in UI and status |
| **OPERATION.md** | Per-project contract every worker follows (assumptions, backlog, learnings, notify) |
| **ASSUMPTIONS.md** | Per-project log of decisions workers made autonomously, pending your review |
| **Neo** | OS-level answerer agent: workers ask (`jarvis wo ask`), Neo answers as you; you review its answers (UI neo tab) and corrections become its learnings |
| **Inbox** | Central notification stream (`jarvis inbox`), fanned out to sinks |
| **Backlog** | Central deferred-work list with dependencies (`jarvis backlog`) |
| **Knowledge** | Central learnings injected into future work orders (`jarvis learn`) |

## Worker contract

Every worker must (enforced by OPERATION.md + dispatch prompt):

```bash
jarvis wo assume  <wo-id> "assumed X because Y"      # every autonomous decision
jarvis wo ask     <wo-id> "blocking question"        # Neo (or you) answers next turn
jarvis backlog add <project> "deferred thing"        # instead of "future work" notes
jarvis learn add "reusable insight" --project <p>    # share with other projects
jarvis notify --level critical "prod is down" "..."  # human attention
jarvis wo finish  <wo-id> --summary "delivered ..."  # completion signal
```

Assumptions flip the work order to `needs_review` — visible in `jarvis status`, the
dashboard, and (if configured) Telegram.

## Onboarding existing projects

See [PROJECT_ONBOARDING.md](PROJECT_ONBOARDING.md). Short version: add the project to the catalog, run
`jarvis adopt <path>` (idempotent, backs up existing settings), replace any direct
notification scripts with `jarvis notify`.

## Development

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest                                   # unit tests
pytest evals -q                          # behavioral evals (scorecard; see evals/README.md)
playwright install chromium && pytest tests_browser -q   # browser tests
JARVIS_EVALS_LLM=1 pytest evals/llm -q   # LLM-graded evals (opt-in, spends tokens)
```

All PRs must pass the three CI checks (unit, evals, browser); `main` only takes
merges through PRs — direct pushes are blocked by a repository ruleset.

Design doc: `docs/superpowers/specs/2026-07-03-jarvis-os-design.md`.
Decisions made while building: [ASSUMPTIONS.md](ASSUMPTIONS.md).

## License

MIT
