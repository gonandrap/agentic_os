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

This is the prefix mode (`command`). It must:

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

Everything else is the launcher's business: sourcing a profile, staging files, starting a
sandbox. Variables it exports reach a headless turn, which is its child. They do NOT reach
a background session, which the supervisor spawns with its own environment (see template
mode below).

A minimal launcher:

```bash
#!/usr/bin/env bash
set -euo pipefail
export ASE_REPO="${ASE_REPO:-agentic-env}"
# project-specific preparation goes here; print nothing to stdout
exec "$JARVIS_REAL_CLAUDE" "$@"
```

## Template mode: a session manager opens the session

Wrappers such as `claude-jarvis`, `claude-ase` and `claude-shining` are session
**managers**: `start` builds its own worktree (`<repo>.worktrees/<name>`), stages skills,
plugins and settings, and runs `claude --bg` there. They are not argv pass-throughs, so
the prefix contract above cannot drive them. `worker.launcher.open` is an argv template
that replaces the whole `claude --bg …` call for the session's FIRST turn:

```json
"worker": {
  "transport": "background",
  "launcher": {
    "open": ["claude-jarvis", "start", "@jarvis", "--model", "{model}",
             "--compact-pct", "{compact_pct}", "--prompt", "{prompt}", "{wo_name}"]
  }
}
```

| placeholder | value |
|---|---|
| `{prompt}` | the turn's prompt; over `BG_PROMPT_ARGV_LIMIT` it is a pointer to the prompt file, as for any background turn |
| `{wo_name}` | the work-order id with everything but letters and digits removed (`wo-61a28e47` → `wo61a28e47`); `claude-shining` refuses any other name |
| `{wo_id}`, `{title}`, `{project}`, `{project_path}` | the order and its project |
| `{model}`, `{effort}`, `{permission_mode}`, `{append_system_prompt}`, `{settings_file}` | the order's briefing |
| `{autocompact_window}` | the briefing's window in tokens |
| `{compact_pct}` | that window as a percentage of `context_window` (default 1,000,000), clamped to 1–100; no window is `100` |

An unknown placeholder is a catalog error. The template is only for background sessions:
a catalog that pairs it with `transport: headless` is refused.

What the OS does with it (`claude_cli.spawn_bg_open`, from `worker_session._launch_bg`):

1. Run the filled template in the project root, with `JARVIS_LAUNCH_*` in its env.
2. Read the minted short id off its stdout (`backgrounded · <id> · <name>`), then the full
   session from `claude agents --json --all`.
3. Record the session AND the roster's `cwd` as the order's `worktree`, as an absolute
   path. Every reader that joins `<project>/.claude/worktrees/<worktree>` gets that path
   back unchanged, because joining an absolute path replaces the prefix.
4. Every later turn is the ordinary bare `claude --bg --resume <sid> -- <msg>` inside that
   worktree. The manager is never called again.

### What does not reach a template-opened session

Measured live on CLI 2.1.309: **the caller's environment does not reach a `--bg`
session.** The supervisor spawns it with its own environment. A probe exported a variable
around `claude-jarvis start`, and the session's `echo` printed it empty. So:

* `launcher.env` and `JARVIS_LAUNCH_*` reach the launcher process only, never the worker.
* The worker settings file (`--settings`: `JARVIS_WO_ID` and the other worker env, the
  PreToolUse gates, the Stop and PostToolUse hooks) cannot be passed, because
  `claude-shining start` takes no `--settings` and pins `--setting-sources project,local`.
  `{settings_file}` exists so a manager that grows such a flag can be handed the file.

What still works without it, verified live: the order's id reaches the worker in its
prompt, so `jarvis wo finish <id>` works. The turn settles from the transcript, and the
Stop hook was only ever a backup there. What does not work is privileged-action gates,
mid-turn message delivery, and any hook or env-driven worker policy. Closing that gap is a
change to the session manager: pass `--settings <file>` through to `claude`.

### The resume race this exposed

After settling a background turn, `poll` stopped the idle session with a bare
`claude stop`. The roster drops the pid about 0.6s before the process exits (fact 8), so a
resume issued right after saw no live session, resumed, and the CLI forked a copy. The
live run hit it on its second turn. The daemon can hit it too when a queued message goes
out in the same tick. The settle now uses `stop_bg`, which waits for the process.

### Verified live (2026-10-22, CLI 2.1.309, haiku)

Project: a detached worktree of agentic_os. Launcher: the template above.

| run | result |
|---|---|
| turn 1 | `claude-jarvis` built `agentic_os.worktrees/wo61a28e47` on `gonandrap/wo61a28e47` and wrote `.model`=haiku and `.compact` pct=40. The OS learned the session and the worktree, and the turn settled from the transcript as `PONG-1` |
| turn 2 | a bare resume in that worktree continued THE SAME session (`PONG-2`), with no fork |
| prompt over 1 KB | it went by the prompt-file pointer, and the session read the file (`PONG-BIG`) |

## Health

`INV-LAUNCHER-RUNNABLE` (`invariants.check_launcher_runnable`, part of `check_catalog`)
flags a launcher that is missing or not executable. Without it the first sign would be
every turn of the project failing with `could not start`. Those errors now name the
launcher rather than `claude`.


## Tests

`tests/test_session_dispatch.py`: parsing, the catalog mapping, argv and environment, each
primitive through a recording launcher that execs the fake CLI, end-to-end orders on both
transports, the bare path for a project without a launcher, the error naming a missing
launcher, the doctor check, and template mode: parsing, filling, `{wo_name}`,
`{compact_pct}`, opening through a fake session manager that builds its own worktree, and
an order whose later turns resume bare inside it. `tests/test_bg_transport.py` holds the
settle-waits-for-the-process case.
