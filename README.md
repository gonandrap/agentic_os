# Jarvis — an agentic OS for Claude Code

Jarvis is an OS layer that every Claude Code session sits on top of: register your
projects in one catalog, hand it work orders, and it runs a Claude Code worker per task
in its own git worktree — while `jarvis status` tells you the one thing that needs you.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/gonandrap/agentic_os/main/install.sh | bash
```

That installs the **latest release** into its own environment, puts `jarvis` on your
PATH (`~/.local/bin`), and writes a starter catalog at `~/.jarvis/catalog.json`.
Re-run it any time to upgrade — your catalog and state are never touched.
You need Linux or macOS, `git`, Python 3.11+ (or [uv](https://docs.astral.sh/uv/)), and
the [Claude Code](https://code.claude.com) CLI installed and authenticated.

**Then onboard your first project** (it must be a git repository):

```bash
# 1. add it to ~/.jarvis/catalog.json:
#      "projects": [ { "name": "my_app", "path": "~/workspace/my_app",
#                      "description": "what this project is" } ]

jarvis start --catalog ~/.jarvis/catalog.json     # 2. adopts every catalog project, starts the daemon
jarvis wo create my_app "Add dark mode to the settings page"   # 3. give it work
jarvis status                                     # what's running, what needs me?
```

Install options (pin a version, no dashboard, from source) are in
[Install options](#install-options); what adoption does to a project is in
[PROJECT_ONBOARDING.md](PROJECT_ONBOARDING.md).

## What Jarvis does

Instead of running isolated sessions per project, you register your projects in a
catalog and Jarvis:

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

## Install options

The installer (`install.sh`, also runnable from a checkout) resolves the newest
`jarvis-X.Y.Z` tag on the repo and installs *that* — the script is fetched from `main`,
but what you run is always a release. It uses `uv tool install` when uv is present,
then `pipx`, then a plain `python3 -m venv` + pip, whichever it finds first.

```bash
curl -fsSL https://raw.githubusercontent.com/gonandrap/agentic_os/main/install.sh | bash -s -- --help

# pass flags after `-s --` when piping:
… | bash -s -- --tag jarvis-0.1.8      # pin an exact release
… | bash -s -- --no-ui                 # skip the [ui] extra (no web dashboard)
… | bash -s -- --bin-dir ~/bin         # where the `jarvis` executable lands
… | bash -s -- --dry-run               # print the plan, change nothing
```

`jarvis` must be on PATH — workers and hooks call it by name; the installer warns with
the exact `export PATH=…` line if its bin dir isn't there. `jarvis --version` tells you
which release you're on. Uninstall with `jarvis stop && uv tool uninstall jarvis-os`
(state lives in `~/.jarvis` and each project's `.jarvis/`, so removing those is a
separate, deliberate step).

There is **no PyPI release**: the name `jarvis-os` on PyPI belongs to an unrelated
project. Install from a release tag (above) or from a checkout:

```bash
git clone https://github.com/gonandrap/agentic_os.git && cd agentic_os
uv tool install --editable ".[ui]"       # or: ./install.sh --tag jarvis-X.Y.Z
```

## Quick start

1. Describe your fleet in a catalog — the installer left one at `~/.jarvis/catalog.json`;
   `catalog.example.json` in this repo shows every option:

```json
{
  "os": {
    "defaults": { "model": "claude-opus-5", "permission_mode": "auto", "max_concurrent": 5 },
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
jarvis start --catalog ~/.jarvis/catalog.json
```

This bootstraps every project (README check, OPERATION.md contract, `.jarvis/` state
dir, injected `.claude/settings.json` and `.claude/skills/`, and workspace trust) and
starts the daemon.
Listing a project in the catalog trusts its workspace for you — no per-project trust
dialog.

3. Create work:

```bash
jarvis wo create my_app "Add dark mode to the settings page"
jarvis status                     # what's going on, what needs me?
jarvis wo send wo-1a2b3c4d "Use CSS variables, not a theme lib"   # talk to the worker
jarvis ui                         # dashboard on http://127.0.0.1:8787
```

Telegram notifications carry the work order id as a link straight to that work
order's page in the dashboard, anchored at whatever is waiting on you. The link is
built from `os.ui.base_url` (falling back to `http://127.0.0.1:<os.ui.port>`) — set
`base_url` when you read Telegram on your phone and reach the UI through a tunnel or
LAN address.

Workers run as native Claude Code background sessions named `[WO wo-…] …` — you can
also watch and join them from `claude agents`.

## Concepts

| Thing | What it is |
|---|---|
| **Catalog** | JSON file declaring projects, models, settings overrides |
| **Work order** | A unit of work; one worker agent, one git worktree, full audit trail |
| **Origin badge** | `jarvis`/`ui`/`injected` = you or the framework put it there; `manual`/`adhoc` = flagged ⚠ in UI and status |
| **Injection** | Your own Claude sessions are yours: Jarvis never adopts one it finds. Hand one over with `jarvis wo inject <session-id>` (or the button on the project page) and it gets a work order that shows up in status and on the dashboard. That's a mirror, not a dispatch: it never got the worker contract, so it owes no `jarvis wo finish` and its session ending is not a failure. Injecting writes nothing into the session — the first write is your own `jarvis wo send` |
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
jarvis wo finish  <wo-id> --summary "..." --pr <url> # …and it's waiting on your merge
```

The PR title must start with the work order id — `[wo-1234abcd] what it does` — so the
pull request is traceable back to the work order by people who never see Jarvis. The
`gh pr create` hook enforces it.

A work order is the representation of its worker's conversation. The final assistant
message of every worker turn is captured verbatim into the record, so `jarvis wo show`
and the dashboard carry the full answer — you and Neo decide from that record and never
have to open the session. `--summary` is the one-line headline for it, not a
replacement.

Assumptions flip the work order to `needs_review` — visible in `jarvis status`, the
dashboard, and (if configured) Telegram.

A work order finished with `--pr` lands in `waiting_pr_merge` instead of `completed`:
it stays on the open list with its link until you merge and `jarvis wo done` it (nothing
polls GitHub). It never raises the attention flag — it is a merge queue, not a blocker —
and the dashboard gives those a row each, right after the running workers, with
everything else open folded into a count.

Attention is re-derived from state on every reconcile tick, so it can't be cleared by
hand — the next tick puts it back. `jarvis wo ack <id>` (or **Got it** on the work order
page) records what you dismissed and keeps it down; a *different* blocker still surfaces.
Pending assumptions can't be acked: they want `jarvis wo review`. Note that acking is not
the same as `jarvis inbox ack`, which clears notifications — a different stream.

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
