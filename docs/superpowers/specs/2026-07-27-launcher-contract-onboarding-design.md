# Onboarding, part 2: the launcher contract

**Status:** design, implemented in the same branch.
**Work order:** `wo-036e8be2`.

## Problem

`jarvis adopt` prepares a project's *files* (OPERATION.md, settings, `.jarvis/`) and
stops there. Everything about *running a worker* is hard-coded to one shape:

```
claude --bg --name … --worktree … --settings … -- <prompt>
claude agents --json                     # roster
~/.claude/jobs/<id>/state.json           # per-turn result
claude --resume <sid> …                  # message delivery
claude stop <bg-id>                      # teardown
```

That is `src/jarvis/claude_cli.py`, called directly from `dispatch.py`, `daemon.py` and
`ops.py`. The first external user does not start background sessions that way: they go
through a wrapper of their own, and that wrapper keeps changing. Adoption therefore has
to learn, per project, **how sessions are launched here** — and be able to re-learn it.

## Shape of the solution

Three pieces:

1. **A launcher contract** — a JSON manifest describing the five verbs Jarvis needs
   (`spawn`, `list`, `result`, `send`, `stop`) plus the capabilities the launcher has.
   It is data, not code: diffable, fingerprintable, reviewable.
2. **A bootstrap work order** — a work order with a completely different prompt whose
   deliverable is that manifest. The onboarding session interviews the user about their
   wrapper and writes the contract. This is the "contract between LLMs": Jarvis states
   the protocol, the project-side agent implements it for the local reality.
3. **Re-evaluation** — the contract records what it was derived from (files + hashes,
   wrapper `--version`), so drift is detectable and a fresh bootstrap can be raised
   without a human noticing the breakage first.

Nothing changes for existing fleets: with no contract present, Jarvis uses a built-in
`native` contract that *is* today's behaviour.

## 1. The contract

Resolution order (first hit wins):

1. catalog `projects[].launcher` (a path)
2. `<project>/.jarvis/launcher.json`
3. `$JARVIS_HOME/launcher.json` — fleet-wide default
4. built-in `native`

```json
{
  "schema_version": 1,
  "name": "acme-bgwrap",
  "description": "Background agents via the ACME wrapper",
  "capabilities": {
    "worktree": false, "resume": false, "settings_file": true,
    "add_dirs": false, "hooks": true
  },
  "spawn": {
    "command": ["bgwrap", "run", "--label", "{name}",
                {"if": "model", "args": ["--model", "{model}"]},
                {"if": "settings_file", "args": ["--settings", "{settings_file}"]},
                "--", "{prompt}"],
    "job_id": {"from": "stdout", "regex": "job:([a-z0-9]+)"}
  },
  "list": {
    "command": ["bgwrap", "ps", "--json"],
    "sessions": {"from": "stdout_json", "path": "jobs",
                 "fields": {"id": "job", "session_id": "conversation",
                            "cwd": "dir", "name": "label", "state": "status"}},
    "state_map": {"RUNNING": "working", "WAITING": "blocked", "EXIT": "done"}
  },
  "result": {
    "file": "~/.bgwrap/{job_id}/out.json",
    "state": {"path": "status"}, "text": {"path": "final_message"}
  },
  "send":  {"command": ["bgwrap", "say", "{session_id}", "{message}"]},
  "stop":  {"command": ["bgwrap", "kill", "{job_id}"]},
  "provenance": {
    "derived_at": 1753600000.0,
    "onboarding_wo": "wo-1234abcd",
    "wrapper_version": "bgwrap 3.2.1",
    "sources": [{"path": "~/bin/bgwrap", "sha256": "…"}],
    "notes": "…"
  }
}
```

**Templating.** An argv item is either a string (with `{placeholder}` substitution) or a
conditional group `{"if": "<var>", "args": [...]}` included only when that variable is
non-empty. A group whose `if` names a *list* variable (`add_dirs`) repeats its args once
per element, with `{item}` bound to each. Unknown placeholders are a validation error —
a typo must fail at verify time, not at 3am on a dispatch.

Placeholder vocabulary, by verb:

| verb | placeholders |
|---|---|
| `spawn` | `prompt` `cwd` `name` `model` `effort` `permission_mode` `append_system_prompt` `settings_file` `worktree` `resume_session_id` `add_dirs` `wo_id` `project` |
| `list` | `cwd` `project` |
| `result` | `job_id` |
| `send` | `session_id` `message` `cwd` `job_id` |
| `stop` | `job_id` |

**States.** `state_map` translates the wrapper's vocabulary into Claude Code's
(`working|starting|queued|blocked|done|failed|cancelled`), which is what
`claude_cli.BgSession.is_active/is_blocked/is_finished` already reads. Unmapped states
are passed through, so a wrapper that already speaks the native words needs no map.
The OS's own work-order words (`running`, …) are never valid here.

**Capabilities and degradation** — explicit, never silent:

| capability off | what Jarvis does instead |
|---|---|
| `worktree` | creates `git worktree add .claude/worktrees/<wo-id>` itself and spawns with that as `cwd` |
| `resume` | delivers user messages through the `send` verb; with neither, the work order is flagged for attention and the message stays queued |
| `settings_file` | omits `--settings`; env (`JARVIS_WO_ID` …) is passed through the child process environment instead |
| `hooks` | session binding falls back to `list` name matching (`[WO <id>]` prefix), which the reconciler already does |

## 2. The bootstrap work order

A work order carrying `kind='bootstrap'` (new column, default `work`). Its prompt is
built by `onboarding.build_bootstrap_prompt()` and shares nothing with the worker
prompt: no "open a PR", no "record assumptions" — it is an *interview*, run with the
user present, whose only deliverable is `.jarvis/launcher.json` plus a live check.

`jarvis onboard <project>`:

- creates the bootstrap work order,
- writes the prompt to `<project>/.jarvis/onboarding/<wo-id>.md` and prints it,
- **first time**: leaves the work order `waiting_input`, flagged "bootstrap session must
  be started by hand", because Jarvis cannot yet spawn a session in this project — the
  user pastes the prompt into a session started their own way;
- **re-onboarding** (a verified contract already exists): leaves it `pending`, and the
  daemon dispatches it through that contract like any other work order.

The onboarding session finishes with `jarvis launcher verify <project> --live`, which
does a real round trip (spawn a throwaway session → find it in `list` → read its
`result` → `stop` it) and, on success, stamps `verified_at` + the contract fingerprint
in the central store. Only a `--live` pass counts as verification; the default static
verify (schema, placeholders, binaries on `PATH`) does not, so an unverified contract
stays visibly unverified.

## 3. Re-evaluation

`jarvis launcher status [project]` reports source, name, capabilities, `verified_at`,
fingerprint, and drift. Drift is raised as an attention item + inbox entry when:

- any `provenance.sources[].sha256` no longer matches the file on disk,
- `verified_at` is older than 30 days (or absent),
- three consecutive spawns through the contract have failed.

Jarvis never edits a contract by itself. The remedy is always the same: run
`jarvis onboard <project> --reason drift`, which raises a fresh bootstrap work order
seeded with the *current* contract so the session amends rather than starts over.

## 4. Where the code goes

| file | change |
|---|---|
| `src/jarvis/launcher.py` | new — contract schema, validation, templating, `NativeLauncher`, `ContractLauncher`, resolution, verify, fingerprint/drift |
| `src/jarvis/onboarding.py` | new — bootstrap prompt, `jarvis onboard` operations, contract state in the central store |
| `src/jarvis/assets/launcher-protocol.md` | new — the protocol spec the bootstrap prompt embeds |
| `src/jarvis/dispatch.py` | spawn through `launcher_for(project)`; bootstrap-kind prompts |
| `src/jarvis/daemon.py` | roster, result capture, delivery and teardown through the launcher; sessions keyed by project instead of cwd |
| `src/jarvis/ops.py` | `stop_worker_session` through the launcher |
| `src/jarvis/project_store.py` | `kind` column |
| `src/jarvis/cli.py` | `jarvis onboard`, `jarvis launcher {status,verify,show}` |
| `src/jarvis/testing.py` | `fake_wrapper` fixture — a second, deliberately un-Claude-like launcher |

Testing strategy: the existing suite exercises `NativeLauncher` unchanged (that is the
regression guard), and a new fake wrapper — different flags, different state words,
different roster shape — drives the same dispatch/reconcile/delivery paths through
`ContractLauncher`. If both pass, the abstraction is real rather than a rename of
`claude_cli`.
