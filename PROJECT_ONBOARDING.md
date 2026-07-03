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
      "worker": {"model": "sonnet", "permission_mode": "acceptEdits"},
      "max_concurrent": 2,
      "settings_overrides": {},
      "append_system_prompt": ""
    }
  ]
}
```

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

`settings.local.json` is never touched — it stays your per-machine escape hatch.

Try it safely first:

```bash
jarvis adopt ~/workspace/my_project --catalog ~/.jarvis/catalog.json --dry-run
```

## 3. Pre-flight requirements

- **A git repository.** Workers run in fresh worktrees; a project without git must be
  `git init`-ed first (adopt detects this and instructs rather than auto-initializing).
- **Workspace trust.** Each project must be trusted by Claude Code (open `claude` there
  once and accept the dialog) — untrusted workspaces ignore permission rules and
  workers stall. `jarvis start` warns per project if trust is missing.

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
