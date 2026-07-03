# Migrating existing projects onto Jarvis

Adoption is **per project, one command, reversible**. Nothing happens to a project
until you run `jarvis adopt` on it (or include it in the catalog passed to
`jarvis start`, which adopts everything listed).

## What adoption does (idempotent)

| Change | Detail | Undo |
|---|---|---|
| `README.md` | kept as-is; stub generated only if missing | delete stub |
| `OPERATION.md` | generated (worker contract); your "Project specifics" section survives regeneration | delete file |
| `.jarvis/` | state dir with the project's queue DB | delete dir |
| `.gitignore` | `.jarvis/` entry appended | remove line |
| `.claude/settings.json` | replaced by OS baseline + catalog `settings_overrides`; original backed up to `settings.json.pre-jarvis` **the first time** | restore backup |

`settings.local.json` is never touched — it stays your per-machine escape hatch.

Try it safely first:

```bash
jarvis adopt ~/workspace/shared_schedule --catalog catalogs/gonzalo.json --dry-run
```

**Workspace trust:** each project must be trusted by Claude Code (open `claude` there
once and accept the dialog) — untrusted workspaces ignore permission rules and workers
stall. `jarvis start` warns per project if trust is missing. Projects you already work
in are fine.

**After adopting, commit the generated files** (`OPERATION.md`, `.gitignore` change,
`README.md` if stubbed) — workers run in fresh worktrees checked out from git, so
uncommitted files don't exist for them:

```bash
git add OPERATION.md .gitignore README.md && git commit -m "Adopt Jarvis OS"
```
(`.claude/settings.json` stays uncommitted by design — Jarvis re-injects it and passes
it to workers directly.)

## Rollout order for this fleet

1. **shared_schedule** — small, clean, only a `settings.local.json` (untouched). Pilot.
2. **tesis_grado** — no `.claude` config at all; zero-risk adoption.
3. **hermes_sandbox**, **openclaw_sandbox** — small infra repos.
4. **painforwisdom** — has 9 agents + 3 skills (untouched — Jarvis only manages
   `settings.json`, which today only sets `worktree.bgIsolation`; that becomes a
   catalog override if you still need it). Watch the obsidian-vault submodule note in
   the catalog.
5. **auto_heycrypto** — PRODUCTION. Its credential-guard and strategy-review hooks are
   already ported into `catalogs/gonzalo.json` `settings_overrides`, so the injected
   settings are a superset of today's. Adopt last, verify with
   `diff <(jq -S 'del(._jarvis)' .claude/settings.json) <(jq -S . .claude/settings.json.pre-jarvis)`
   that nothing you rely on was lost.
6. **vpn-setup** — run `git init && git add -A && git commit -m init` first, then adopt.

Then start the OS over the whole fleet:

```bash
jarvis start --catalog catalogs/gonzalo.json
```

## Migrating notification pipelines

Goal: every alert flows through the OS (`jarvis notify`), which fans out to Telegram/
log/desktop from one place.

- **auto_heycrypto** (`scripts/notify_telegram.sh`): replace the `curl` to the Telegram
  API with:
  ```bash
  jarvis notify --project auto_heycrypto --level critical "$TITLE" "$BODY"
  ```
  Keep the old path as fallback until you trust the pipeline
  (`jarvis notify ... || ./scripts/notify_telegram.sh ...`). The monitor daemon
  (`scripts/live_order_monitor/daemon.py`) needs no other change.
- **painforwisdom** (`telegram_io.sh` summaries): same substitution, `--level info`.

Set once in your shell profile (values from your existing bot):
```bash
export JARVIS_TELEGRAM_TOKEN=...   # reuse the bot token you already have
export JARVIS_TELEGRAM_CHAT_ID=...
```

## Migrating deferred work / TODO piles

Anything "we'll do this later" scattered in project notes becomes backlog:

```bash
jarvis backlog add painforwisdom "retry queue for failed pipeline runs" \
    --description "from .planning/pipeline-evolution-2026-06.md"
```

Workers do this automatically going forward (OPERATION.md contract).

## Rollback

```bash
jarvis stop
cd <project>
mv .claude/settings.json.pre-jarvis .claude/settings.json   # restore old settings
rm -rf .jarvis OPERATION.md                                  # drop OS state/contract
```

The project is exactly as before (minus a `.gitignore` line that does no harm).
