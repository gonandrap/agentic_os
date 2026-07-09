# Onboarding projects onto Jarvis

Adoption is **per project, one command, reversible**. Nothing happens to a project
until you run `jarvis adopt` on it (or include it in the catalog passed to
`jarvis start`, which adopts everything listed).

## 1. Describe the project in your catalog

Copy `catalog.example.json` somewhere **outside version control** (e.g.
`~/.jarvis/catalog.json` — your fleet is your instantiation, not the OS's) and add an
entry per project:

```json
{
  "projects": [
    {
      "name": "my_project",
      "path": "~/workspace/my_project",
      "worker": {"model": "sonnet", "permission_mode": "auto"},
      "max_concurrent": 5,
      "settings_overrides": {},
      "append_system_prompt": ""
    }
  ]
}
```

- `worker.permission_mode` — `auto` by default: workers run routine tools (grep,
  edits, scripts, tests, git) without a prompt per action, which is the only way a
  background worker can run unattended. Sensitive paths stay protected by
  `settings_overrides` deny guards, which apply in every mode. Set a stricter mode
  per project if you want (e.g. `acceptEdits`, `plan`).
- `max_concurrent` — simultaneous work orders for this project; the rest queue.
  Defaults to `5` (or the fleet-wide `os.defaults.max_concurrent`).
- `settings_overrides` — project-specific hooks/permissions merged on top of the OS
  baseline (e.g. credential guards for a production repo).
- `append_system_prompt` — hard constraints every worker must hear
  (e.g. "never touch live credentials").

## 2. What adoption does (idempotent)

| Change | Detail | Undo |
|---|---|---|
| `README.md` | kept as-is; stub generated only if missing | delete stub |
| `OPERATION.md` | generated (worker contract); your "Project specifics" section survives regeneration | delete file |
| `.jarvis/` | state dir with the project's queue DB | delete dir |
| `.gitignore` | `.jarvis/` entry appended | remove line |
| `.claude/settings.json` | replaced by OS baseline + catalog `settings_overrides`; original backed up to `settings.json.pre-jarvis` **the first time** | restore backup |
| `~/.claude.json` | project path marked `hasTrustDialogAccepted: true` (workspace trusted); every other key preserved | set it back to `false` |

`settings.local.json` is never touched — it stays your per-machine escape hatch.

Try it safely first:

```bash
jarvis adopt ~/workspace/my_project --catalog ~/.jarvis/catalog.json --dry-run
```

## 3. Pre-flight requirements

- **A git repository.** Workers run in fresh worktrees; a project without git must be
  `git init`-ed first (adopt detects this and instructs rather than auto-initializing).
- **Workspace trust.** Untrusted workspaces ignore permission rules and workers stall,
  so Jarvis trusts every catalog project for you: adoption sets
  `hasTrustDialogAccepted` for the project path in `~/.claude.json` (listing a project
  in the catalog *is* the trust decision). No per-project trust dialog.

## 4. Commit the generated files

Workers run in fresh worktrees checked out from git, so uncommitted files don't exist
for them:

```bash
git add OPERATION.md .gitignore README.md && git commit -m "Adopt Jarvis OS"
```

(`.claude/settings.json` stays uncommitted by design — Jarvis re-injects it and passes
it to workers directly.)

## 5. Start the OS

```bash
jarvis start --catalog ~/.jarvis/catalog.json
```

## Migrating notification pipelines

Goal: every alert flows through the OS (`jarvis notify`), which fans out to
Telegram/log/desktop from one place. Wherever a project curls a chat API directly,
substitute:

```bash
jarvis notify --project <name> --level critical "$TITLE" "$BODY"
```

Keep the old path as fallback until you trust the pipeline
(`jarvis notify ... || ./old_notify.sh ...`). Set the sink credentials once in your
shell profile:

```bash
export JARVIS_TELEGRAM_TOKEN=...
export JARVIS_TELEGRAM_CHAT_ID=...
```

## Migrating deferred work / TODO piles

Anything "we'll do this later" scattered in project notes becomes backlog:

```bash
jarvis backlog add my_project "retry queue for failed pipeline runs" \
    --description "from planning notes 2026-06"
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

## Rollout strategy for an existing fleet

Adopt one **low-risk pilot** project first and run a real work order through it before
touching anything that matters. Order the rest by blast radius: config-less repos next,
infra sandboxes after, production systems **last** — and for those, port any existing
guard hooks into `settings_overrides` *before* adopting, then verify the injected
settings are a superset:

```bash
diff <(jq -S 'del(._jarvis)' .claude/settings.json) \
     <(jq -S . .claude/settings.json.pre-jarvis)
```
