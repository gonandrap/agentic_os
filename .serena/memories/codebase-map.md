# Jarvis OS codebase map

`jarvis-os` Python package, stdlib-only core (argparse + sqlite3 + json). Source in
`src/jarvis/`, 20 modules. Read this instead of re-exploring the tree.

## Modules (responsibility — key symbols — intra-package imports)

**Leaves (import nothing from `jarvis`):**
- `paths.py` — on-disk layout of all Jarvis state. `jarvis_home()`:17, `central_db_path()`:21,
  `neo_db_path()`:25, `project_db_path()`:54, `ensure_home()`:43, `daemon_pidfile()`:39.
- `db.py` — raw SQLite setup + shared helpers. `connect()`:13 (WAL, autocommit,
  `busy_timeout=10000`, `foreign_keys=ON`), `now()`:23, `new_id()`:27, `to_json`/`from_json`:31/35.
- `catalog.py` — parse/validate the catalog JSON into typed specs. `ProjectSpec`:58,
  `Catalog`:96, `WorkerDefaults`:50, `load_catalog()`:112, `parse_catalog()`:123,
  `CatalogError`:45, `DEFAULT_PERMISSION_MODE = "auto"`:25.
- `claude_cli.py` — ALL interaction with the `claude` binary (override via `CLAUDE_BIN_ENV`).
  Worker turns: `turn_args()`, `spawn_turn()`, `read_turn_result()`, `TurnResult`,
  `process_alive()`, `kill_process_group()`. Background sessions (ad-hoc + migration only):
  `spawn_background()`, `list_background_sessions()`, `stop_session()`, `BgSession`.
  Also `run_headless()` (Neo), `_briefing_args()`, `ClaudeCliError`.
- `timeline.py` — render `wo_events` into a human timeline. `build_timeline()`:95,
  `count_debug()`:123, `event_level()`:40, `DEBUG_KINDS`:19, `STATUS_LABEL`:28.
- `testing.py` — reusable pytest fixtures + the fake `claude` executable so suites never
  touch the real CLI. `FAKE_CLAUDE`:16, fixtures `jarvis_home`:124, `fake_claude`:131,
  `claude_json`:194, `project`:209, `catalog_file`:216, `make_git_project()`:184.

**Storage (import only `db` + `paths`):**
- `central_store.py` — OS-wide DB. `CentralStore`:63, `upsert_project()`:75, `add_inbox()`:103,
  `add_backlog()`/`list_backlog()`:158/179, `relevant_knowledge()`:226, `get_state`/`set_state`:250/244.
- `project_store.py` — per-project DB + the WO state machine. `WO_STATUSES`:16,
  `OPEN_STATUSES`:26, `ProjectStore`:110, `create_work_order()`:133, `claim_next_pending()`:207,
  `set_status()`:235, `add_event()`:282, `delete_work_order()`:258.
- `neo_store.py` — Neo's DB. `NeoStore`:59, `ask()`:71, `claim_next()`:82, `record_answer()`:94,
  `review()`:153, `add_learning()`/`learnings()`:166/175.

**Adapters:**
- `bootstrap.py` — make a project OS-ready (settings injection, gitignore, README/OPERATION.md,
  workspace trust, `.jarvis/`). `bootstrap_project()`:226, `build_settings()`:66,
  `settings_drift()`:76, `deep_merge()`:50, `BootstrapReport`:38, `TEMPLATE_VERSION = 2`:24.
- `hooks.py` — the `jarvis _hook` endpoint: PreToolUse preflight + session lifecycle → WO state.
  `handle_hook()`:100, `main_hook()`:183, `preflight_decision()`:55, `find_project_root()`:85.
- `notify.py` — notification sinks + routing inbox rows outward. `route_new_inbox()`:105,
  `SINKS`:98, `sink_telegram()`:57, `wo_url()`:37.
- `neo.py` — Neo the answerer agent: persona, headless answering, verdict parsing.
  `drain_queue()`:118, `answer_question()`:102, `build_system_prompt()`:56, `parse_verdict()`:83.

**Middle:**
- `worker_session.py` — **the conversation layer**: the ONLY module that knows how a
  worker turn is run. `start()`, `send()`, `poll()`, `busy()`, `cancel()`,
  `briefing_for()`, `is_stalled()`, `TURN_STALL_SECONDS`. Everything above it speaks in
  turns, not processes or sessions. Its module docstring is the design rationale; see
  `mem:work-order-lifecycle`.
- `dispatch.py` — composes what the worker is TOLD (prompt, settings, resolved
  model/effort/permission mode) and hands the running of it to `worker_session`.
  `dispatch_work_order()`, `build_worker_prompt()`, `_write_worker_settings()`,
  `worker_name()`.
- `ops.py` (620 L) — business logic shared by CLI and UI. `start_os()`:63,
  `create_work_order()`:261, `finish()`:374, `find_work_order()`:281, `os_status()`:142, `OpsError`:31.

**Top:**
- `daemon.py` (496 L) — jarvisd supervision loop. `Daemon`:40, `tick()`:102,
  `dispatch_pending()`:139, `reconcile_project()`:371, `run_daemon()`:470, `daemon_running()`:485.
- `cli.py` (676 L) — argparse surface + output formatting. `build_parser()`:72, `main()`:631,
  `cmd_start()`:254, `cmd_wo()`:342, `cmd_status()`:281. Every jarvis import is lazy, inside handlers.
- `ui/app.py` — FastAPI/Jinja dashboard. `create_app()`:50, routes at :75/:103/:154,
  POST actions :181-263 (all delegate to `ops`).

## Layering

Imports run strictly downward: leaves → stores → adapters → `dispatch`/`ops` → `daemon`/`cli`/`ui`.
No import cycles at module-import time: `cli.py` imports everything lazily inside function
bodies, and `daemon.py:134` imports `notify` lazily inside `tick()`. One upward-looking edge:
`ops.py:26` imports `daemon.daemon_running` — a pure pidfile probe (`daemon.py:485`), harmless.

## Databases — three, all opened through `db.connect()` (`db.py:13`)

No module calls `sqlite3.connect` directly.

| DB | Path | Tables |
|---|---|---|
| Central | `paths.central_db_path()`:21 = `$JARVIS_HOME/os.db` (`JARVIS_HOME` defaults `~/.jarvis`, paths.py:18) | `projects`, `inbox`, `backlog`, `knowledge`, `os_state` (central_store.py:16,25,36,46,54) |
| Neo | `paths.neo_db_path()`:25 = `$JARVIS_HOME/neo.db` | `questions`, `learnings` (neo_store.py:29,45) |
| Per-project | `paths.project_db_path()`:54 = `<project>/.jarvis/jarvis.db` | `work_orders`, `wo_events`, `wo_messages`, `wo_turns`, `notifications`, `assumptions`, `approvals` |

`ProjectStore.__init__`:111 runs `_migrate()`:118 applying `ADDED_COLUMNS`:99.
Also under `$JARVIS_HOME`: `logs/` (paths.py:31), `run/jarvisd.pid` (paths.py:35-40).

## Entry point

`pyproject.toml:23-24` — one console script: `jarvis = "jarvis.cli:main"`.
`jarvis start` chain: `cli.main()`:631 → `cmd_start()`:254 → `ops.start_os()`:63 (load catalog →
`ensure_home` → assert `claude_cli.available()` → per-project `bootstrap_project()`:78 +
`upsert_project()`:81 → `set_state("catalog_path")`:88) → `ops._spawn_daemon()`:116 re-execs
`python -m jarvis.cli daemon run` detached → `daemon.run_daemon()`:470 → `Daemon.run_forever()`:64.

`jarvis --version` / `-V` (`cli.py` `_VersionAction`) prints the installed dist version via
`bugreport.jarvis_version()` — resolved lazily so hooks don't pay for dist metadata.

## How users install it (there is no PyPI release)

`install.sh` at the repo root is the curl target
(`raw.githubusercontent.com/gonandrap/agentic_os/main/install.sh`): it resolves the newest
`jarvis-X.Y.Z` tag with `git ls-remote`, shallow-clones that tag, and installs it with
`uv tool install` → `pipx` → `venv`+pip (whichever exists), dropping `jarvis` in
`~/.local/bin` and scaffolding `$JARVIS_HOME/catalog.json` if absent. Re-running upgrades.
The script ships from `main`; what it installs is always a release tag. **`jarvis-os` on
PyPI is an unrelated project** — never point anyone at `pip install jarvis-os`.
Covered by `tests/test_install_script.py` (`--dry-run` against a throwaway local remote;
real install behind `JARVIS_TEST_INSTALL=1`).

Also under `<project>/.jarvis/`: `turns/<wo-id>/<seq>.json` (a turn's raw result),
`worker-settings/<wo-id>.json`, `agent-skills/`. All gitignored.

See `mem:work-order-lifecycle` for the WO state machine and how a turn runs, and
`mem:testing` for the suite.
