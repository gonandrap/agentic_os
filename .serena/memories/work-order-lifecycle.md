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
       [--append-system-prompt <sp>] [--settings <path>] <prompt>
```

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

## Other `claude` invocation shapes

- `send_to_session()` — `claude --resume <sid> -p <msg> --output-format json` (`claude_cli.py:206`)
- `run_headless()` — `claude -p <prompt> --output-format json [--append-system-prompt] [--model]`
  (`claude_cli.py:224-228`); used by Neo and by the LLM evals
- `stop_session()` — `claude stop <bg id>` (`claude_cli.py:189`)
