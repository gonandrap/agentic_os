# Jarvis — Agentic OS Design

Date: 2026-07-03
Status: v1 (MVP) — designed autonomously; all decisions mirrored in `/ASSUMPTIONS.md`

## 1. Problem

Gonzalo runs many independent projects, each with its own Claude Code session. Context,
learnings, configuration, notifications, and backlogs are siloed per project. This does
not scale and wastes Claude's ability to transfer knowledge between projects.

Jarvis is an OS layer every Claude session sits on top of: a central orchestrator (CLI +
daemon + web UI + conversational persona) that dispatches work to per-project worker
agents, unifies configuration, routes notifications, and centralizes backlog and
knowledge.

## 2. Core principles

1. **Deterministic substrate, intelligent workers.** Everything that can be a plain
   program (polling, dispatch, routing, persistence) is deterministic Python. Claude
   agents do the actual work. LLMs are never used to move bytes around.
2. **Native over custom.** Workers are native Claude Code background sessions
   (`claude --bg`), so they appear in the agents view, are supervised by the Claude
   daemon, and can be interacted with directly. Jarvis adds orchestration around them,
   not a parallel agent runtime.
3. **Reproducible.** No hardcoded user paths in the package. Everything user-specific
   lives in a catalog file and `$JARVIS_HOME`. Installable with `uv`/`pip`.
4. **Unification.** One settings baseline, one notification pipeline, one backlog, one
   knowledge base — all injected/aggregated by the OS.

## 3. System components

```
┌────────────────────────────────────────────────────────────────────┐
│  User surfaces                                                     │
│  • Jarvis persona (Claude session in agentic_os, incl. phone)      │
│  • Web UI (FastAPI dashboard)                                      │
│  • `jarvis` CLI directly                                           │
│  • Claude agents view (native)                                     │
│  • Telegram (notification sink)                                    │
└──────────────┬─────────────────────────────────────────────────────┘
               │ all mutate/query through the same Python API
┌──────────────▼─────────────────────────────────────────────────────┐
│  jarvis CLI + library  (deterministic)                             │
│  start/stop/status • wo create/list/show/send/assume • notify      │
│  backlog • learn • adopt • ui • daemon • _hook                     │
└──────┬───────────────────────────────────────────┬─────────────────┘
       │                                           │
┌──────▼──────────────┐                 ┌──────────▼──────────────────┐
│ jarvisd (daemon)    │                 │ Central state $JARVIS_HOME  │
│ per-project pollers │                 │ os.db: projects, inbox,     │
│ dispatcher          │                 │ backlog, knowledge          │
│ message deliverer   │                 └─────────────────────────────┘
│ notification router │
│ reconciler          │
└──────┬──────────────┘
       │ spawns / observes
┌──────▼─────────────────────────────────────────────────────────────┐
│ Per project                                                        │
│  .jarvis/jarvis.db (work orders, events, messages, notifications,  │
│                     assumptions)  ← gitignored                     │
│  .claude/settings.json  ← injected (base + catalog overrides)      │
│  OPERATION.md, README.md, ASSUMPTIONS.md                           │
│  workers: claude --bg --worktree wo-<id> --name "[WO wo-<id>] ..." │
└────────────────────────────────────────────────────────────────────┘
```

### 3.1 The catalog

`jarvis start --catalog <file>` takes a JSON catalog describing the fleet:

```jsonc
{
  "os": {
    "defaults": {
      "model": "sonnet",             // default model for workers
      "effort": null,                 // optional
      "permission_mode": "acceptEdits"
    },
    "notifications": {
      "sinks": ["log"],              // + "telegram", "desktop"
      "telegram": { "token_env": "JARVIS_TELEGRAM_TOKEN", "chat_id_env": "JARVIS_TELEGRAM_CHAT_ID" }
    },
    "ui": { "port": 8787 }
  },
  "projects": [
    {
      "name": "shared_schedule",
      "path": "~/workspace/shared_schedule",
      "model": "sonnet",             // overrides os.defaults.model
      "description": "Family shared schedule web app",
      "settings_overrides": { },      // deep-merged into injected .claude/settings.json
      "worker": { "permission_mode": "acceptEdits", "append_system_prompt": null },
      "notifications": { "level_threshold": "info" }
    }
  ]
}
```

Schema validated on load with clear errors. `catalog.example.json` ships in the repo;
`catalogs/gonzalo.json` holds the real fleet.

### 3.2 State stores

**Per-project** `<project>/.jarvis/jarvis.db` (SQLite, WAL; `.jarvis/` gitignored):

- `work_orders(id, title, description, status, origin, created_at, updated_at,
  model, effort, permission_mode, append_system_prompt, session_id, bg_id, worktree,
  branch, needs_attention, attention_reason, result_summary, backlog_id, metadata)`
  - `status`: `pending → dispatching → running → (waiting_input | needs_review) →
    completed | failed | cancelled`
  - `origin`: `jarvis | ui | manual | adhoc` — the framework-vs-adhoc indicator.
- `wo_events(id, wo_id, ts, kind, payload)` — audit trail (dispatched, hook events,
  status changes, message deliveries).
- `wo_messages(id, wo_id, ts, direction, content, source, status)` — user feedback
  queue (`queued → delivered | failed`) and agent-to-user notes.
- `notifications(id, ts, level, title, body, wo_id, source, status)` — project outbox.
- `assumptions(id, wo_id, ts, content, status)` — mirror of ASSUMPTIONS.md entries for
  the UI (`pending → accepted | rejected`).

**Central** `$JARVIS_HOME/os.db` (`JARVIS_HOME` defaults to `~/.jarvis`):

- `projects(name, path, description, model, status, last_seen, catalog_json)`
- `inbox(id, ts, project, level, title, body, wo_id, status, sink_results)` — aggregated
  notifications (`new → notified → acked`).
- `backlog(id, project, title, description, status, depends_on, promoted_wo_id, created_at)`
- `knowledge(id, project, ts, topic, content, tags)`
- `os_state(key, value)` — daemon pid, catalog path, started_at, etc.

Per-project DB is the authoritative record for that project's work orders (requirement:
UI reads it, users *may* insert directly). Central DB holds everything that must be
unified across projects (notifications, backlog, knowledge, registry).

### 3.3 Work order lifecycle

1. **Create** — `jarvis wo create <project> "title" [--description ... --model ...
   --append-system-prompt ...]`, the UI form (same Python function), or direct DB insert
   (discouraged; picked up fine but marked `origin=manual`). Metadata defaults inherit
   catalog project → OS defaults.
2. **Dispatch** — jarvisd poller claims `pending` orders (oldest first, per-project
   concurrency limit, default 2). Dispatcher:
   - builds the worker prompt: work order text + OPERATION.md contract (work in the
     worktree, record assumptions via `jarvis wo assume`, report leftovers via
     `jarvis backlog add`, report learnings via `jarvis learn add`, notify via
     `jarvis notify`, end with a result summary)
   - injects relevant knowledge-base entries (project + global, most recent N)
   - writes the merged worker settings (project injected settings + per-WO env:
     JARVIS_WO_ID, JARVIS_PROJECT, JARVIS_PROJECT_PATH, JARVIS_HOME, PATH) to
     `.jarvis/worker-settings/<id>.json` — required because the fresh worktree lacks
     the untracked `.claude/settings.json` (verified live)
   - spawns: `claude --bg --worktree <id> --name "[WO <id>] <title>" --model <m>
     --settings .jarvis/worker-settings/<id>.json` in the project directory
   - status → `running`. The supervisor assigns the session id (`--session-id` is
     ignored for `--bg`, verified live); it binds to the work order via the
     SessionStart hook, with reconciler name-matching (`[WO <id>]`) as fallback.
3. **Track** — two channels:
   - *Hooks (event-driven):* injected project settings add SessionStart / Stop /
     Notification / SessionEnd hooks running `jarvis _hook <event>`; the hook reads
     `JARVIS_WO_ID` from env (no-op when absent, so interactive sessions are untouched)
     and updates the work order + events table.
   - *Reconciler (poll):* jarvisd periodically runs `claude agents --json --all`,
     matches sessions to work orders by session id, fixes drift, and registers unknown
     background sessions in a project cwd as `origin=adhoc` shadow work orders so ad-hoc
     agents are visible (and labeled) in the UI.
4. **Interact** — `jarvis wo send <id> "msg"` (CLI or UI) enqueues a message; when the
   worker's session goes idle (`done` in the agents roster) jarvisd dispatches a new
   background agent resuming that conversation (`claude --bg --resume`) — context
   carries over, the turn is visible in the agents view, the SessionStart hook rebinds
   the work order to the fork's session id, and the reply is captured from the job
   result into the message thread. Fallback: stop + headless `--resume -p`. Mid-turn
   injection is not supported by the CLI — for live interruption users open the
   session in the agents view (native path). All deliveries are logged to `wo_events`.
5. **Complete** — worker's Stop/SessionEnd hook flips status. If the worker recorded
   assumptions, status becomes `needs_review` and `needs_attention=1` until the user
   accepts. Workers ship their own branch/PR per repo conventions (OPERATION.md says to).

### 3.4 Worker backend abstraction

`WorkerBackend` interface with one production implementation, `BgBackend`
(`claude --bg`). A `ManagedBackend` (daemon-owned `claude -p --input-format
stream-json`, stdin kept open for guaranteed mid-run message injection) is stubbed as a
fallback if `--resume`-based delivery to live bg sessions proves unreliable. Tests use a
`FakeBackend`/fake `claude` shim.

### 3.5 Configuration injection

- `src/jarvis/assets/settings.base.json` is the OS baseline: the `jarvis _hook`
  lifecycle hooks (SessionStart/Stop/SessionEnd/Notification, by absolute path), a
  PreToolUse hook that auto-approves Bash commands that are pure `cd`/`jarvis` chains
  (workers must never stall on their own contract commands), and
  `permissions.allow: ["Bash(jarvis *)"]`.
- On `jarvis start` / `jarvis adopt`, per project: deep-merge base ← catalog
  `settings_overrides` → write `<project>/.claude/settings.json`.
- First injection backs up any existing file to `.claude/settings.json.pre-jarvis`.
- An injected marker key `"_jarvis": {"managed": true, "version": N, "hash": ...}` lets
  Jarvis detect manual edits: if the file changed outside Jarvis, `start` warns and
  requires `--force-config` to overwrite (drift surfaces in `jarvis status`).
- `settings.local.json` is left alone (user's per-machine escape hatch).

### 3.6 Notifications (two-way Jarvis)

- Producers call `jarvis notify --level critical "title" ["body"]` from anywhere inside
  a project (workers, monitoring daemons, cron). Writes the project outbox.
- jarvisd routes outbox → central inbox → configured sinks:
  - `log` — `$JARVIS_HOME/logs/notifications.log` (always on)
  - `telegram` — bot token/chat id via env vars named in the catalog
  - `desktop` — `notify-send` when available
- `jarvis status` and the UI dashboard surface unacked inbox items and every
  `needs_attention` work order. `jarvis inbox ack <id>` clears them.
- The Jarvis persona (see 3.8) checks the inbox each turn, making the conversation
  two-way without polluting context: it reads counts first, details on demand.
- Migration path for existing pipelines (auto_heycrypto, painforwisdom): replace direct
  `notify_telegram.sh` calls with `jarvis notify`; the OS's telegram sink delivers.

### 3.7 Backlog & knowledge base

- **Backlog** (central): `jarvis backlog add <project> "title" [--depends-on id,id]`,
  `list`, `promote <id>` → creates a work order in the project. Promotion warns/refuses
  (`--force` to override) when a dependency isn't `done`. Workers are contractually told
  (OPERATION.md + dispatch prompt) to record deferred work here instead of leaving it in
  chat.
- **Knowledge** (central): `jarvis learn add <project> --topic X "content"`, `list`,
  `search`. Dispatcher injects the most recent/matching entries into every new worker
  prompt. (MVP: recency + same-project + global entries; smarter retrieval post-MVP.)

### 3.8 Jarvis persona (conversational layer)

`agentic_os/CLAUDE.md` instructs any Claude session opened in this repo to act as
Jarvis: answer "how are things going" by running `jarvis status --json`, create work
orders via the CLI, relay inbox items, never bypass the CLI to poke DBs. This is what
the phone app talks to. A `jarvis-status` skill wraps the common queries.

### 3.9 Web UI

FastAPI + server-rendered templates (no build step, stdlib-friendly, htmx for
refresh). Read-only against the DBs except "create work order", "send message",
"ack", "accept/reject assumption", "promote backlog" — each POST calls the same
library functions as the CLI. Views:

- **Dashboard** — attention items (needs_review WOs, unacked critical inbox), per-project
  tiles with WO counts by status, daemon health.
- **Project** — work orders (origin badge: `jarvis`/`ui` = framework ✅, `manual`/`adhoc`
  = ⚠ ad-hoc), backlog slice, notifications.
- **Work order** — timeline of events, messages thread + send box, assumptions with
  accept/reject, links (worktree, branch, session id).
- **Backlog** — dependency-aware promote.
- **Inbox**, **Knowledge**.

### 3.10 Project contract & migration (`jarvis adopt`)

A project is OS-ready when it has: `README.md` (kept if present, stub generated if not),
`OPERATION.md` (generated from template, describes exactly how work orders flow),
`.jarvis/` gitignored, injected `.claude/settings.json`, and an entry in the catalog.

`jarvis adopt <path> [--name ...]` performs all of it idempotently and prints a diff-like
report; `--dry-run` shows what would change. `git init` is suggested (not auto-run) for
non-repos (vpn-setup). `MIGRATION.md` documents the per-project rollout order:
shared_schedule → tesis_grado → the rest, with auto_heycrypto's telegram rerouting last.

## 4. Error handling

- Daemon crash: work orders in `dispatching`/`running` are reconciled from
  `claude agents --json` on restart; orphaned ones flagged `needs_attention`.
- `claude` CLI missing/unauthenticated: `jarvis start` runs preflight (`claude
  --version`, daemon reachable) and fails with actionable errors.
- Worker dies (`state: done` with error / disappears): status → `failed`,
  notification emitted at `warning`.
- Message delivery failure: message row → `failed`, WO flagged, retried on demand.
- DB contention: WAL mode, short transactions, busy_timeout.

## 5. Testing

- Unit: catalog validation, settings merge/injection/drift detection, stores, backlog
  dependency logic, prompt building.
- Integration/e2e: temp git fixture project + a fake `claude` executable on PATH that
  records invocations and simulates bg session lifecycle (JSON roster, hook calls), so
  CI needs no auth/tokens. One optional real-CLI smoke test gated by
  `JARVIS_E2E_REAL=1`.

## 6. Out of scope for MVP (backlogged)

- Smarter knowledge retrieval (embeddings), cross-project learning summarization jobs.
- ManagedBackend full implementation (only if bg-resume delivery proves flaky).
- Auth on the web UI (binds 127.0.0.1 only for now).
- Windows support (Linux/macOS first).
- Automatic migration of auto_heycrypto's monitor daemon (guide provided instead).
