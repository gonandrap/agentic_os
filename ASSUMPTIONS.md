# ASSUMPTIONS — Jarvis OS build

Every decision made autonomously while building the OS. Review each; mark ✅ accepted /
❌ change (with a note) and I'll adjust. Ordered by impact.

## A. Architecture

1. **The per-project "orchestrator background agent" is a deterministic daemon, not a
   Claude agent.** Your spec said each project bootstraps a background *agent* that
   waits for work and spawns workers. Since that role needs zero intelligence (poll DB,
   spawn, track), implementing it as an LLM session would burn tokens idling and be less
   reliable. jarvisd runs one poller per project and behaves exactly as specified
   (waits for work orders, spawns worker agents, never does the work itself). Workers
   ARE real Claude agents. If you want a literal Claude orchestrator per project, the
   dispatch layer is abstracted so it can be swapped.

2. **Workers are native `claude --bg` background sessions** (not SDK processes), so they
   show up in your agents view, are supervised by Claude's own daemon, survive jarvisd
   restarts, and you can open/chat with them natively. Naming convention
   `[WO <id>] <title>` marks framework-managed sessions in the agents view; unmanaged bg
   sessions found in a project are auto-registered as `adhoc` shadow work orders so the
   UI shows them with a warning badge.

3. **Feedback routing** (`jarvis wo send`, UI) delivers queued messages via
   `claude --resume <session-id> -p "<msg>"` when the worker isn't mid-turn. Answer to
   your question #9: yes, Claude Code supports programmatic input to an existing session
   this way (documented; messages append to the same transcript). Caveat: pushing into a
   session *while it is actively running a turn* can interleave; jarvisd therefore
   queues and delivers between turns. A `ManagedBackend` (stream-json stdin, guaranteed
   mid-turn delivery) is designed as fallback but not the default.

4. **One central DB + one DB per project.** Work orders/events/messages/assumptions live
   in `<project>/.jarvis/jarvis.db` (per your #3, gitignored). Anything that must be
   unified — notification inbox, backlog, knowledge, project registry — lives centrally
   in `$JARVIS_HOME/os.db` (default `~/.jarvis`). Rationale: your #12–14 explicitly ask
   for unified handling of those.

5. **Jarvis-the-conversation is a Claude session in this repo** (CLAUDE.md persona)
   using the `jarvis` CLI; the CLI/daemon is the actual OS. From your phone you open a
   session in `agentic_os` and you're talking to Jarvis. Proactive pings reach you via
   notification sinks (Telegram MVP) rather than injecting into an idle chat, to avoid
   context pollution — the persona surfaces the inbox at the start of each turn instead.

## B. Technology

6. **Python 3.11+, stdlib-first.** Core CLI has zero runtime deps (argparse + sqlite3);
   the web UI is an optional extra (`pip install jarvis-os[ui]` → FastAPI + uvicorn +
   Jinja2). Packaged with pyproject/uv. Chosen for reproducibility (OSS goal) and
   because your stack is Python-heavy.

7. **SQLite in WAL mode** for both DBs (your suggestion; concurrent daemon/UI/CLI reads
   are fine at this scale).

8. **Web UI is server-rendered FastAPI + htmx** (no node build step) bound to
   127.0.0.1, no auth in MVP. Access from phone = via the Jarvis persona, not the UI.

## C. Behavior & policy

9. **Default worker permission mode is `acceptEdits`** (not `bypassPermissions`) — safe
   default for an OSS project; your catalog can set per-project
   `worker.permission_mode` (e.g., auto/bypass for sandboxed projects). Blocked
   permission prompts surface as `needs_attention`.

10. **Per-project concurrency limit = 2 simultaneous work orders** (catalog-tunable).

11. **Work orders don't auto-merge anything.** Workers work in worktree `wo-<id>`,
    commit, push, open PRs per each repo's conventions; OPERATION.md instructs them.
    Requirement #6 (new worktree per work order) is satisfied via Claude's native
    `--worktree` flag (worktrees land in `<project>/.claude/worktrees/`).

12. **Assumptions workflow:** workers run `jarvis wo assume <id> "text"`, which appends
    to the project's `ASSUMPTIONS.md` *and* records a DB row; any pending assumption
    flips the WO to `needs_review` so both `jarvis status` and the UI dashboard flag it.
    You accept/reject from the UI or `jarvis wo review`.

13. **Backlog is central with cross-item dependencies** (`depends_on`); promoting an
    item with unfinished deps warns and requires `--force`. Workers are instructed to
    file leftovers there rather than leaving "future work" notes in chat.

14. **Knowledge base MVP is plain text rows** (project, topic, tags, content) injected
    into new worker prompts by recency (project-specific + global, top 8). No
    embeddings/retrieval yet — flagged post-MVP as you suggested (#13 "could be post
    MVP").

15. **Settings injection owns `<project>/.claude/settings.json`.** Original is backed up
    once to `settings.json.pre-jarvis`; a `_jarvis` marker detects manual drift (start
    warns; `--force-config` reapplies). `settings.local.json` remains user-owned.
    Project-specific needs (e.g., auto_heycrypto's credential-guard hooks) must be
    declared in the catalog `settings_overrides` — I ported them there in
    `catalogs/gonzalo.json` so nothing is lost.

16. **Notification sinks:** `log` always; `telegram` enabled when env vars
    (`JARVIS_TELEGRAM_TOKEN`, `JARVIS_TELEGRAM_CHAT_ID` by default) are set; `desktop`
    (notify-send) optional. Existing project telegram scripts keep working until each
    project is migrated to call `jarvis notify` (migration guide included).

## D. Migration

17. **I did not modify any of your real projects.** `jarvis adopt` + `catalogs/
    gonzalo.json` are ready, `MIGRATION.md` gives the order (shared_schedule →
    tesis_grado → rest), but running adoption on real repos is left for you (one
    command each) since it writes to their working copies.

18. **vpn-setup needs `git init` first**; adopt detects and instructs rather than
    auto-initializing a repo you may want structured differently. It also has no
    README.md — adopt generates a stub from its INSTALL_STEPS.md headline for you to
    edit.

19. **auto_heycrypto's monitor daemon keeps its own pipeline for now.** Rerouting its
    production alerts through `jarvis notify` is a one-line change in
    `scripts/notify_telegram.sh` documented in MIGRATION.md, deliberately last in the
    rollout (production trading system — you flip it when you trust the OS pipeline).

## E. Scope cuts (MVP)

20. UI has no auth and no websockets (htmx polling refresh).
21. No Windows support yet (Linux/macOS).
22. E2E tests use a fake `claude` shim; a real-CLI smoke test exists behind
    `JARVIS_E2E_REAL=1` (not run in CI to avoid token burn).
23. Cross-project learning *synthesis* (summarizing learnings into curated docs) is
    backlogged; MVP only captures + injects.
