# Work-order lifecycle and worker spawning

The core flow of the OS. Module map in `mem:codebase-map`.

## States

`WO_STATUSES` at `project_store.py:16`, `OPEN_STATUSES` at `:26`. Every transition goes
through `ProjectStore.set_status()` (`project_store.py:235`) — nothing writes status directly.

1. **`pending`** — `ops.create_work_order()` (`ops.py:261`) resolves the project path via
   `registered_project_paths()` (`ops.py:252`, reads the central `projects` table), then
   `ProjectStore.create_work_order()` (`project_store.py:133`) inserts with status `pending`.
   Entry points: CLI `cli.cmd_wo()` (`cli.py:342`), UI `ui/app.py:181` `create_wo`.
2. **`dispatching`** — `Daemon.dispatch_pending()` (`daemon.py:139`) loops while
   `store.count_active() < project.max_concurrent` and calls
   `ProjectStore.claim_next_pending()` (`project_store.py:207`).
3. **`running`** — `dispatch.dispatch_work_order()` (`dispatch.py:139`) spawns the worker,
   writes `job_id`/`worktree`/`model`, sets `running` (`dispatch.py:192`) and adds a
   `dispatched` event (`:193`). On `ClaudeCliError` it sets `failed` (`dispatch.py:170`).
   `hooks.handle_hook()` `SessionStart` binds the session id and corrects
   `dispatching`→`running` (`hooks.py:139-146`).
4. **`waiting_input`** — `hooks.py:150-161` on a `Notification` hook, plus attention flag.
5. **`needs_review`** — three independent paths, which is why review can trigger without a
   clean finish:
   - `ops.finish()` (`ops.py:374`) — the worker's own `jarvis wo finish`: records
     `result_summary`, then `needs_review` if `pending_assumptions()` else `completed`
     (`ops.py:379-386`).
   - `hooks.py` `SessionEnd` → `needs_review` with "session ended without `jarvis wo finish`"
     (`hooks.py:167`); `hooks._finalize()` (`:174-176`) → `needs_review` when assumptions pend.
   - `Daemon.reconcile_project()` (`daemon.py:371`) when the bg session reports `done`:
     `needs_review` for pending assumptions (`daemon.py:440`) or "worker idle without
     `jarvis wo finish`" (`daemon.py:446`).
6. **Close-out** — `ops.review_work_order()`:458, `ops.cancel()`:399, `ops.hide_work_order()`:411,
   `ops.delete_work_order()`:428. Delete cascades: `ProjectStore.delete_work_order`
   (`project_store.py:258`) + `CentralStore.purge_work_order` (`central_store.py:111`) +
   `NeoStore.purge_work_order` (`neo_store.py:109`).

## How a worker is actually launched

`claude_cli.spawn_background()` (`claude_cli.py:103-153`) via `_run()` (`:35`):
`subprocess.run([claude_bin(), *args], cwd=…, capture_output=True, timeout=120)`.

```
claude --bg --name "[WO <id>] <title[:60]>" [--resume <sid>] [--worktree <wo-id>]
       [--model <model>] [--effort <effort>] [--permission-mode <mode>]
       [--append-system-prompt <sp>] [--settings <path>] [--add-dir <dir>]…
       -- <prompt>
```

**The `--` before the prompt is load-bearing — never append an arg after it.**
`--add-dir <directories...>` is *variadic*: commander keeps eating positionals until a
`-`-prefixed token or `--`. With `--add-dir` emitted last (as it is), an unfenced prompt
is consumed as a second directory, so the session boots with nothing to do and parks at
the welcome screen — the user sees "the session was created but never started" and has to
type into it by hand. Symptoms in the supervisor's `state.json`: `detail: "stuck on a
startup dialog"` / `needs: "send a prompt to start"`, and the prompt is absent from the
session transcript entirely. Regression tests: `tests/test_worker_spawn_args.py`.

Flags assembled `claude_cli.py:134-149`, invoked `:150`. Job id scraped from stdout with
`_JOB_ID_RE = re.compile(r"claude stop ([0-9a-f]{6,})")` (`claude_cli.py:100`, used `:151-152`).
Binary is `JARVIS_CLAUDE_BIN` or `claude` (`:22`).

**Model / effort / permission mode** are resolved in `dispatch.dispatch_work_order()`
(`dispatch.py:151-154`): per-WO override first, else `project.worker.*` from the catalog
(`WorkerDefaults` `catalog.py:50`, `DEFAULT_PERMISSION_MODE = "auto"` `catalog.py:25`).
`--worktree` is the WO id itself (`dispatch.py:147`).

**Worker settings file** — `_write_worker_settings()` (`dispatch.py:27`) merges
`bootstrap.build_settings(project.settings_overrides)` with per-WO permission allow rules for
`.claude/worktrees/<wo-id>/**` (`dispatch.py:50-60`) plus env `JARVIS_WO_ID`, `JARVIS_PROJECT`,
`JARVIS_PROJECT_PATH`, `JARVIS_HOME`, `PATH` (`:63-72`), written to
`<project>/.jarvis/worker-settings/<wo-id>.json` (`:74-76`).

**Prompt** — `build_worker_prompt()` (`dispatch.py:86`), including central knowledge from
`CentralStore.relevant_knowledge()` (`dispatch.py:148`). The worker sees ONLY this prompt,
which is why WO descriptions must carry the user's full intent.

## One work order, many sessions (the binding rule)

A work order outlives its sessions. Turn 1 is the dispatch; **every delivered message
forks a NEW session** (`claude --bg --resume <sid>`: full context, fresh session id) and
the one it forked from is stopped (`daemon._deliver`). So an N-turn work order leaves N
session ids behind, and `claude agents --json --all` shows all of them — `--all` is the
history. The *default* `claude agents` listing only shows sessions the supervisor still
owns, so a correctly retired predecessor disappears from it. Two or more entries there
for one `[WO …]` name means a session leaked.

**The supervisor's job id is the session id's first segment** (`0686a1b5` ↔
`0686a1b5-2324-…`; verified across 39 live sessions), and a bg session's env carries
`CLAUDE_JOB_DIR=~/.claude/jobs/<job id>`.

`wo.session_id` names the session currently carrying the work order and is written by
the SessionStart hook — which means *any* session of that work order can write it.
Re-opening a finished agent in the agents view respawns it under its ORIGINAL id
(`state.json` keeps `respawnFlags` + `resumeSessionId`) and fires SessionStart again.
Before wo-6e7caf6c that walked the binding backwards, and the damage was all downstream:
the next turn forked from a dead conversation (losing every turn since), "retired" a
session that was already stopped, and orphaned the live agent. Observed on wo-9478c1be.

Two rules now hold this together, both in the store/hook layer:
- `ProjectStore.bind_session()` is the ONLY way to move `session_id`, and it refuses
  any id already in the `prior_sessions` column — bindings move forward only.
  Post-condition `INV-SESSION-FORWARD` (`invariants.py`) is the tripwire for code that
  writes `session_id` around it.
- `hooks._is_current_session()` drops Stop/SessionEnd/Notification from a superseded
  session. Retiring the predecessor makes it fire SessionEnd seconds later; acted on,
  that flipped a freshly-resumed work order straight back to `needs_review`.

A delivery resolves its target session by `wo.job_id` (which Jarvis itself wrote at
spawn) before falling back to `session_id`, and forks are briefed exactly like the first
turn — model, effort, `--append-system-prompt`, `--add-dir` skills. A resumed session
re-derives its system prompt at launch; it does NOT inherit the first turn's from the
transcript, so anything omitted is simply absent from turn two onwards.

**The briefing lives in ONE place: `claude_cli._briefing_args()`.** Both launch paths
(`spawn_background`, `send_to_session`) build their argv through it. Anything that starts
a worker turn must do the same — hand-rolling the flag list is how the headless fallback
came to carry none of them at all (fixed in PR #42; the fork was fixed earlier in #37).
`daemon._deliver` builds the briefing once and passes it to the fork and the fallback.

Also: a resume must run with `cwd` = the directory the session was created in (the
worktree, not `project.path`). Transcripts are stored per-cwd — see
`claude_cli.session_transcript_path` — so the wrong cwd cannot find the conversation.

Note `dispatch_work_order` writes the *resolved* model/effort/permission_mode back to the
work order row (`dispatch.py:255`), so from turn two those columns are populated and the
`or project.worker.*` fallback is moot — except for `adhoc` work orders, which the
reconciler adopts without ever going through dispatch and which therefore have them NULL.

## Other `claude` invocation shapes

- `send_to_session()` — `claude --resume <sid> -p <msg> --output-format json` plus the
  same briefing flags (`claude_cli.py`); the daemon's fallback when the bg fork fails
- `run_headless()` — `claude -p <prompt> --output-format json [--append-system-prompt] [--model]`
  (`claude_cli.py:224-228`); used by Neo and by the LLM evals
- `stop_session()` — `claude stop <bg id>` (`claude_cli.py:189`)
