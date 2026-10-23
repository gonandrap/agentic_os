# Session dispatch: a project's own launcher for its worker sessions

*2026-10-22*

## Problem

Every worker turn was a bare `claude` process: `claude --bg …` on the background
transport, `claude -p …` on the headless one. The binary was global
(`JARVIS_CLAUDE_BIN`), the same for every project.

Some repositories only work behind a wrapper. agentic-env (ASE) needs `claude-ase`,
shining_software needs `claude-shining`, and each wrapper takes different parameters and
sets up its own environment first. A bare `claude` session in those repos runs without
that environment, so the OS could not drive them.

## Decision

A **session dispatch layer** sits between a work order and the process that runs its
turn. Given a work order, it resolves the order's project, reads that project's
**launcher** from the local catalog, and launches the turn through it.

```
work order ─► project ─► worker.launcher (local catalog) ─► <launcher> <claude argv…>
                                                              └─ exec "$JARVIS_REAL_CLAUDE" "$@"
```

* `session_dispatch.Launcher` is the parsed `worker.launcher`.
* `session_dispatch.for_work_order(project, wo, resume=…, transport=…)` builds a
  `Launch`: the launcher plus who is launching what.
* `claude_cli.spawn_bg_turn`, `resume_bg_turn` and `spawn_turn` take `launch=` and run
  `launch.argv(args)` with `launch.env()` on top of the usual environment.
* `worker_session._launch` builds the `Launch`. It is the one point every turn passes
  through: dispatch, message, compaction and retry.

A project with no launcher behaves exactly as before: `launch.argv(args)` is
`[claude_bin(), *args]` and the environment gains nothing.

### Only launches go through the launcher

Control traffic stays on the real CLI: `claude agents --json`, `stop`, `rm`, `respawn`,
`--version`, Neo's and the validators' `run_headless` calls. Those act on the
supervisor's shared roster or on the OS itself, not on a project's session, and a
wrapper that set up an environment for them would only add latency and failure modes.

## Where the mapping lives: the local catalog

```json
{
  "name": "agentic-env",
  "path": "~/projects/agentic-env",
  "worker": {
    "launcher": {
      "command": ["~/.jarvis/launchers/agentic-env.sh"],
      "env": {"ASE_REPO": "agentic-env"}
    }
  }
}
```

`worker.launcher` accepts a path string, a list (path plus fixed arguments), or an object
with `command` and an optional string-to-string `env`. The first element is
`~`-expanded. Anything else is a catalog error naming the project.

The catalog is already per machine and outside version control (PROJECT_ONBOARDING §1),
so the mapping and the scripts it names stay local. Nothing about a launcher is
committed to the OS repo or to the adopted project.

## The launcher contract

A launcher is invoked as `<command…> <claude argv…>`, with the turn's working directory
and this environment:

| variable | value |
|---|---|
| `JARVIS_REAL_CLAUDE` | the real CLI to exec (`JARVIS_CLAUDE_BIN` or `claude`) |
| `JARVIS_LAUNCH_PROJECT` | catalog project name |
| `JARVIS_LAUNCH_PROJECT_PATH` | project root |
| `JARVIS_LAUNCH_WO_ID` | the work order |
| `JARVIS_LAUNCH_WO_KIND` | `worker`, `planner`, `investigator`, … |
| `JARVIS_LAUNCH_PHASE` | `open` (first turn) or `resume` (every later turn) |
| `JARVIS_LAUNCH_TRANSPORT` | `background` or `headless` |
| *`env` keys* | whatever the catalog entry set |

It must:

1. **End by `exec`-ing `$JARVIS_REAL_CLAUDE "$@"`.** The headless transport tracks the
   turn by pid, and on Linux `process_alive` checks the pid's cmdline names `claude`. A
   launcher that forks instead of exec-ing reads as a dead turn.
2. **Pass the argv through.** It may insert flags before the `--` fence on a `open` turn.
   On a background `resume` it must add **nothing**: a flagged `--bg --resume` forks a
   copy of the session, which the OS detects, removes and refuses
   (workers-on-background-sessions, facts 5–8).
3. **Not change directory.** Turn 1 runs at the project root with `--worktree <wo-id>`,
   later turns inside that worktree. Transcripts are keyed by the creation cwd.
4. **Write nothing to stdout before the exec.** The background transport reads the
   minted session id off `claude --bg`'s output, and the headless one reads the result
   JSON from it.

Everything else is the launcher's business: exporting variables, sourcing a profile,
staging skills into the worktree, starting a sandbox.

A minimal launcher:

```bash
#!/usr/bin/env bash
set -euo pipefail
export ASE_REPO="${ASE_REPO:-agentic-env}"
# project-specific preparation goes here; print nothing to stdout
exec "$JARVIS_REAL_CLAUDE" "$@"
```

## Health

`INV-LAUNCHER-RUNNABLE` (`invariants.check_launcher_runnable`, part of `check_catalog`)
flags a launcher that is missing or not executable. Without it the first sign would be
every turn of the project failing with `could not start`. Those errors now name the
launcher rather than `claude`.

## What this does not solve

Wrappers such as `claude-ase` and `claude-shining` are session **managers**, not argv
pass-throughs. `claude-shining start <name> --agentic` creates its own worktree under
`<repo>.worktrees/<name>`, stages skills, and spawns its own `claude --bg`. The OS
already owns the worktree and the session, so it cannot call those entry points as-is.
Driving one of them fully needs a pass-through mode on the wrapper's side, such as
`claude-shining exec -- <claude argv>`, which prepares the environment for the cwd and
then execs claude. Until that exists, a project's local launcher reproduces the parts of
the wrapper it needs.

## Tests

`tests/test_session_dispatch.py`: parsing, the catalog mapping, argv and environment, each
primitive through a recording launcher that execs the fake CLI, end-to-end orders on both
transports, the bare path for a project without a launcher, the error naming a missing
launcher, and the doctor check.
