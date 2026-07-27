# Onboarding projects onto Jarvis

Onboarding has two halves:

1. **Adoption** — prepare the project's *files* (`jarvis adopt`, §1–5 below). Per
   project, one command, reversible.
2. **The launcher** — teach Jarvis how background agent sessions are *started* here
   (`jarvis onboard`, §6). Skip this and Jarvis uses `claude --bg`, which is right for
   a plain Claude Code install and wrong for anyone who launches sessions through a
   wrapper of their own.

Nothing happens to a project until you run `jarvis adopt` on it (or include it in the
catalog passed to `jarvis start`, which adopts everything listed).

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
      "worker": {"model": "claude-opus-5", "permission_mode": "auto"},
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

## 6. Teach Jarvis how sessions are launched here

Out of the box Jarvis starts workers with `claude --bg …`, reads the roster from
`claude agents --json`, and picks each turn's final message out of the Claude Code
supervisor's job files. If that is how your machine works, you are done — nothing in
this section applies and `jarvis launcher status` will say `native (built-in)`.

If sessions here come from **your own wrapper**, Jarvis has to be told. It cannot be
told by editing a config field, because a wrapper is not one field — it is five verbs,
their flags, their output formats, and the states they report. So it is negotiated in a
session, with you in the room:

```bash
jarvis onboard my_project
```

That raises a **bootstrap work order** — a work order whose prompt is not project work
at all, but an interview. It writes the prompt to
`<project>/.jarvis/onboarding/<wo-id>.md` and hands it to you, because the very thing
missing is Jarvis's ability to start a session here:

```bash
jarvis onboard my_project --print    # the prompt, ready to paste into a session
```

Start a session in the project however you normally do, give it that prompt, and answer
its questions. It will ask you to *run* your wrapper and paste real output rather than
describing it, because a contract derived from a description is a contract that fails
at 3am. Its deliverable is one file:

```
<project>/.jarvis/launcher.json
```

The full protocol lives in `src/jarvis/assets/launcher-protocol.md` (the onboarding
prompt embeds it, so the session never has to go looking). In short, the contract gives
Jarvis five verbs — `spawn`, `list`, `result`, `send`, `stop` — as argv templates, plus
a map from your wrapper's state words onto Jarvis's, plus an honest declaration of what
your wrapper *cannot* do:

| capability off | what Jarvis does instead |
|---|---|
| `worktree` | creates the git worktree itself and spawns with it as the session's cwd |
| `resume` | delivers user feedback through your `send` verb; with neither, it flags the work order instead of losing the message |
| `settings_file` | passes `JARVIS_WO_ID`, `JARVIS_PROJECT`, `JARVIS_HOME` … through the environment |
| `add_dirs` | the OS's own agent skills do not reach the worker |
| `hooks` | binds sessions by the `[WO <id>]` name prefix in your `list` output |

### Verifying

```bash
jarvis launcher verify my_project          # static: schema, placeholders, binaries
jarvis launcher verify my_project --live   # spawns a real throwaway session, then stops it
```

Only `--live` counts. Until it passes once, `jarvis status` reports the project's
launcher as unverified, and says so in the attention list.

### Re-evaluation — wrappers move

The contract records what it was derived from (`provenance.sources`, with hashes, and
the wrapper's version string). Jarvis raises an attention item asking for a fresh
onboarding session when:

- one of those source files changes on disk,
- the contract goes 30 days without a live verification,
- three spawns in a row fail through it.

It never rewrites a contract by itself. The remedy is always another session:

```bash
jarvis onboard my_project --reason "wrapper 4.0 changed the ps output"
```

A re-onboarding is dispatched normally — the *current* launcher still works well enough
to start it — and the session is handed the contract in force so it amends rather than
starts over.

### Where contracts live

Resolution order, first hit wins:

1. the catalog's `projects[].launcher` (a path) — useful for sharing one contract
   across several projects,
2. `<project>/.jarvis/launcher.json` — what the onboarding session writes,
3. `$JARVIS_HOME/launcher.json` — a fleet-wide default,
4. the built-in `native` contract.

Inspect what is in force with `jarvis launcher show my_project` and
`jarvis launcher status`.

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

(`.jarvis/` holds the launcher contract too, so this drops that with it.)

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
