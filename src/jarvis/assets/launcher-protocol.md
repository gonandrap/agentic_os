# The Jarvis launcher contract (schema_version 1)

This is the interface between Jarvis OS and however *this machine* starts background
agent sessions. Jarvis states what it needs; you supply the local commands that provide
it. The result is one JSON file — `<project>/.jarvis/launcher.json` — which Jarvis
executes verbatim from then on.

Jarvis needs exactly five verbs:

| verb | question it answers | required |
|---|---|---|
| `spawn` | start a background agent session with this prompt, in this directory | **yes** |
| `list` | which sessions exist right now, and what state is each in | **yes** |
| `result` | for a finished turn, what was the agent's final message | no (see below) |
| `send` | deliver a message into an existing session | no |
| `stop` | end a session | no |

## The file

```json
{
  "schema_version": 1,
  "name": "acme-bgwrap",
  "description": "one line: what launches sessions here",
  "capabilities": {
    "worktree": false,
    "resume": false,
    "settings_file": true,
    "add_dirs": false,
    "hooks": true
  },
  "spawn": {
    "command": [
      "bgwrap", "run",
      "--label", "{name}",
      {"if": "model", "args": ["--model", "{model}"]},
      {"if": "settings_file", "args": ["--settings", "{settings_file}"]},
      "--", "{prompt}"
    ],
    "job_id": {"from": "stdout", "regex": "job:([a-z0-9]+)"}
  },
  "list": {
    "command": ["bgwrap", "ps", "--json"],
    "scope": "cwd",
    "sessions": {
      "from": "stdout_json",
      "path": "jobs",
      "fields": {"id": "job", "session_id": "conversation",
                 "cwd": "dir", "name": "label", "state": "status"}
    },
    "state_map": {"RUNNING": "working", "WAITING": "blocked", "EXIT": "done"}
  },
  "result": {
    "file": "~/.bgwrap/{job_id}/out.json",
    "state": {"path": "status"},
    "text": {"path": "final_message"}
  },
  "send": {"command": ["bgwrap", "say", "{session_id}", "{message}"]},
  "stop": {"command": ["bgwrap", "kill", "{job_id}"]},

  "provenance": {
    "wrapper_version": "bgwrap 3.2.1",
    "sources": [{"path": "~/bin/bgwrap", "sha256": "auto"}],
    "notes": "free text: what the user told you, what you could not verify"
  }
}
```

## Command templates

An entry in `command` is either:

- **a string** — `{placeholder}` occurrences are substituted. A string that is *only* a
  placeholder and resolves to empty is dropped from the argv entirely (so
  `"{model}"` alone never becomes an empty argument);
- **a conditional group** — `{"if": "<var>", "args": [...]}` — included only when
  `<var>` is non-empty. If `<var>` is a list (`add_dirs`), the group repeats once per
  element with `{item}` bound to that element.

Arguments are passed to the process directly (no shell), so quoting, spaces and
newlines in a prompt are safe and must not be escaped.

Placeholders available per verb — anything else is a validation error:

| verb | placeholders |
|---|---|
| `spawn` | `prompt` `cwd` `name` `model` `effort` `permission_mode` `append_system_prompt` `settings_file` `worktree` `resume_session_id` `add_dirs` `wo_id` `project` |
| `list` | `cwd` |
| `result` | `job_id` |
| `send` | `session_id` `message` `cwd` `job_id` |
| `stop` | `job_id` |

`spawn` and `list` run with the working directory set to the session's directory; you
do not have to pass `{cwd}` unless the wrapper needs it explicitly.

## Reading output back

Two extraction shapes, used wherever the contract needs a value out of a command:

- `{"from": "stdout", "regex": "job:([a-z0-9]+)"}` — first capture group of the first
  match against stdout. `{"from": "stdout"}` with no regex takes all of stdout, trimmed.
- `{"from": "stdout_json", "path": "a.b.c"}` — parse stdout as JSON, then walk the
  dotted path (`""` or omitted = the whole document).

`result` may read a **file** instead of running a command: `{"file": "<path with
{job_id}>"}`, parsed as JSON, with `state` and `text` as dotted paths into it.

## Session states

`list.sessions.fields` maps your wrapper's field names onto the five Jarvis reads:
`id` (what `stop`/`result` take), `session_id` (stable conversation id), `cwd`, `name`,
`state`. Omit a field and the same-named key is used.

`state_map` translates your wrapper's state words into the vocabulary Jarvis
understands:

| Jarvis word | meaning |
|---|---|
| `working`, `starting`, `queued` | making progress on its own; nobody is needed |
| `blocked` | stopped mid-turn on a permission prompt or a question |
| `done`, `failed`, `cancelled` | the turn ended |

Anything unmapped passes through unchanged, so a wrapper that already emits these words
needs no `state_map`. **Never emit `running`** — that is Jarvis's *work order* status
word, and conflating the two makes every healthy worker read as stuck.

## Capabilities

Declare only what is true. Jarvis degrades deliberately, and a false claim here is worse
than a missing feature.

| capability | true means | what Jarvis does when false |
|---|---|---|
| `worktree` | `spawn` accepts `{worktree}` and creates an isolated git worktree itself | creates `git worktree add .claude/worktrees/<wo-id>` first and passes it as the session's directory |
| `resume` | `spawn` accepts `{resume_session_id}` and continues that conversation | uses the `send` verb for user feedback; if `send` is absent too, the work order is flagged for the user instead of losing the message |
| `settings_file` | `spawn` accepts `{settings_file}` (a Claude Code settings JSON) | passes `JARVIS_WO_ID`, `JARVIS_PROJECT`, `JARVIS_PROJECT_PATH`, `JARVIS_HOME` through the process environment instead |
| `add_dirs` | `spawn` accepts extra readable directories via `{add_dirs}` | OS-shipped agent skills are not available to the worker |
| `hooks` | sessions load Claude Code hooks from the settings file | binds sessions by the `[WO <id>]` name prefix in `list` output |

## Required behaviour of `spawn`

1. It must **not block**: it starts the session and returns.
2. The session must run the prompt **immediately and unattended** — no welcome screen,
   no interactive confirmation. A session that parks waiting for a human is the single
   most common way this contract fails in practice.
3. The prompt is the *whole* task. Do not truncate, wrap, or re-word it.
4. If the wrapper prints an id, capture it with `spawn.job_id` — without it Jarvis
   cannot read that turn's final message back into the work order.

## Verifying

```bash
jarvis launcher verify <project>          # static: schema, placeholders, binaries
jarvis launcher verify <project> --live   # really spawns a throwaway session
```

Only `--live` counts as verification: it spawns a session with a probe prompt, waits
for it to appear in `list`, reads `result` if available, then `stop`s it. Until it
passes once, the contract is reported as unverified everywhere in the OS.

## Provenance and re-evaluation

Wrappers change. `provenance.sources` lists the files this contract was derived from;
put `"sha256": "auto"` and Jarvis computes and stores the digest at save time. When one
of those files later changes — or the contract goes 30 days without a live
verification, or three spawns fail in a row — Jarvis raises an attention item asking
for a fresh onboarding session. It never edits the contract by itself.
