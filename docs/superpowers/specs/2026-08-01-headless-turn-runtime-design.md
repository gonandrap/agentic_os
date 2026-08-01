# Headless turn runtime — replacing background sessions as the worker transport

*2026-08-01*

## Problem

Jarvis drives a worker's conversation through Claude Code **background sessions**. Turn
one is `claude --bg -- <prompt>`; every later turn is a *new* background agent resuming
the previous session (`claude --bg --resume <sid>`), after which the superseded agent is
stopped with `claude stop <bg-id>`.

That transport is a hack in three separable ways.

**It leaks sessions.** Each delivered turn creates a background agent and retires the
previous one on a best-effort `claude stop`. Any failure of that stop — the daemon
restarting mid-delivery, a stop that returns non-zero, a turn whose fork succeeded but
whose retirement did not — leaves a spent agent in the roster forever. The live fleet
currently carries **63 background sessions**, nearly all of them dead worker turns.

**It makes the session id a moving target.** `--bg --resume` forks: the conversation
carries over but under a *new* session id assigned by the supervisor, which Jarvis cannot
choose and does not learn until the session reports it. A whole subsystem exists to cope:
`bind_session`, the `prior_sessions` trail, `INV-SESSION-FORWARD`, SessionStart-hook
binding, `[WO <id>]`-name reconciliation, a "worker session never appeared" timeout, and
`deliver_messages` preferring the roster's session id over the recorded one because the
two genuinely disagree. Every one of these is scaffolding around a pointer that moves
when it should not.

**It observes turns through someone else's internals.** Turn completion is polled out of
the Claude supervisor's private `~/.claude/jobs/<id>/state.json`, guarded by a
three-strikes `MAX_REPLY_CAPTURE_ATTEMPTS` retry because the file is written
asynchronously and sometimes never appears.

## Approach

Run each turn as a **headless print-mode invocation that Jarvis owns end to end**:

```
claude -p --session-id <uuid>  [briefing] -- "<prompt>"     # turn 1
claude -p --resume     <uuid>  [briefing] -- "<prompt>"     # every later turn
```

The process is detached and its JSON result is written to a file Jarvis chose. Nothing
about the worker lives in the background-agent roster or the supervisor's jobs directory
any more.

### Verified CLI behaviour (Claude Code 2.1.220, tested live)

Every claim below was checked against the real binary before the design was written.

| Fact | Evidence |
|---|---|
| `-p --resume <sid>` **reuses** the session id — it does not fork | two consecutive turns both reported `session_id: 2af15817-…`; `--fork-session` exists precisely to opt *into* a new id |
| `--session-id <uuid>` is honoured under `-p` | requested `6e5b7e0b-…`, result JSON returned the same id, and `--resume` of it carried context |
| context carries across turns | turn 2 correctly answered "what word did you just say?" |
| `--worktree <name>` works under `-p` and becomes the session cwd | worker reported `pwd` = `<proj>/.claude/worktrees/wt1`; `git worktree list` shows it |
| resuming with `cwd` set to the worktree works, same id, no `--worktree` flag needed | turn 2 from inside the worktree resumed cleanly |
| all hooks fire under `-p` | `SessionStart` (`source: resume` on later turns), `PreToolUse`, `Stop`, `SessionEnd` all fired with correct `cwd` and `session_id` |
| `--settings` env reaches the worker | `JARVIS_WO_ID` read back correctly from inside the turn |
| `-p` sessions do **not** appear in `claude agents --json` | roster of 63 did not list the test session |
| the result JSON carries what we need | `session_id`, `result`, `is_error`, `subtype`, `num_turns`, `total_cost_usd`, `permission_denials`, `usage` |
| resuming a bogus id fails loudly | `rc=1`, stderr `No conversation found with session ID: …` |
| the `Stop` hook payload carries `last_assistant_message` | usable as a backup source for the reply |
| `--tools` is variadic, like `--add-dir` | an unfenced prompt was swallowed → `Input must be provided…`; the existing `--` fence rule covers it |
| `-p` waits ~3s on stdin unless redirected | `Warning: no stdin data received in 3s` → launch with `stdin=DEVNULL` |

Two consequences follow directly and drive most of the design:

1. **Jarvis can mint the session id up front.** It is known before the process starts and
   never changes for the life of the work order.
2. **`SessionEnd` now fires at the end of every turn**, not at the end of the
   conversation. Today that hook files the work order for review. Left alone, every
   single turn would file the work order for review.

## The layer

A new module, `src/jarvis/worker_session.py`, becomes the **only** place that knows how a
worker turn is run. Everything above it speaks in turns, not in processes, sessions or
supervisor state.

```
paths / db / claude_cli          (leaves — argv and subprocess mechanics)
        ↓
central_store / project_store    (state)
        ↓
worker_session                   ← NEW: the conversation layer
        ↓
dispatch / ops                   (compose the prompt, the settings, the briefing)
        ↓
daemon / cli / ui
```

Public surface — six functions, no classes to subclass, no strategy registry:

```python
start(store, project, wo, prompt)  -> Turn   # open the conversation (turn 1)
send(store, project, wo, text, msg_id=None) -> Turn   # next turn, resuming
poll(store, project)               -> list[Turn]      # settle turns that ended
busy(store, wo_id)                 -> Turn | None     # the in-flight turn, if any
cancel(store, wo_id)               -> bool            # kill the in-flight turn
briefing_for(project, wo)          -> dict            # the flags a turn is launched with
```

`claude_cli` keeps its role as the pure argv/subprocess wrapper and gains
`spawn_turn()` (build argv, detach, return pid) and `read_turn_result()` (parse the
result file). It loses `job_result()`, `jobs_dir()` and `send_to_session()`, which exist
only to serve the old transport. `spawn_background()`, `list_background_sessions()` and
`stop_session()` **stay**: ad-hoc adoption still watches the roster for the user's own
sessions, and `stop_session` is the migration path (below).

### Turns are rows

A new per-project table, `wo_turns`, makes the conversation explicit instead of implicit
in a pair of `job_id`/`reply_job_id` columns:

```sql
CREATE TABLE wo_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    seq INTEGER NOT NULL,            -- 1-based position in the conversation
    kind TEXT NOT NULL,              -- dispatch | message
    msg_id INTEGER,                  -- the wo_messages row that triggered it
    prompt TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'running',  -- running | done | failed
    pid INTEGER,
    started_at REAL NOT NULL,
    ended_at REAL,
    exit_code INTEGER,
    result TEXT,                     -- the final assistant message
    error TEXT,
    cost_usd REAL,
    num_turns INTEGER,
    outfile TEXT NOT NULL
);
```

One in-flight turn per work order, enforced by `busy()`. A turn's `result` **is** the
final assistant message, so reply capture stops being a retry loop over someone else's
state file and becomes a field read.

Turn artifacts live at `<project>/.jarvis/turns/<wo-id>/<seq>.json` (and `.err`), beside
the existing `.jarvis/worker-settings/`.

### Launching a turn

```python
Popen([claude, "-p",
       "--session-id" | "--resume", session_id,
       "--output-format", "json",
       "-n", "[WO wo-xxxx] title",
       *briefing_args,               # model, effort, permission-mode,
       "--", prompt],                # append-system-prompt, settings, add-dir
      cwd=..., stdin=DEVNULL, stdout=outfile, stderr=errfile,
      start_new_session=True)
```

- `start_new_session=True` puts the turn in its own process group, so it survives a
  jarvisd restart (which `shipit` performs on every release) and can be killed as a group
  by `cancel()`.
- `stdin=DEVNULL` avoids the 3-second stdin wait on every turn.
- The `--` fence stays load-bearing: `--add-dir` and `--tools` are both variadic.
- `-n` keeps the `[WO <id>]` display name, which is what the planned "open this session in
  a terminal" feature will show in the `/resume` picker.
- The briefing is unchanged and still comes from `claude_cli._briefing_args`. A resumed
  session re-derives model, effort, permission mode, system prompt and reachable
  directories from argv rather than from the transcript, so every turn passes the full
  set — the property `#42` established, preserved verbatim.

**Worktree**: turn 1 runs with `cwd=project.path` and `--worktree <wo-id>`, exactly as
today; Claude creates `.claude/worktrees/<wo-id>` and starts there. Later turns run with
`cwd=<that worktree>` and no `--worktree` flag. Verified live.

### Observing a turn

`poll()` runs on the daemon's reconcile tick. For each `running` turn:

1. **Process alive?** `os.kill(pid, 0)`, plus a `/proc/<pid>/cmdline` check that the pid
   still belongs to a `claude` process — pid reuse over a multi-hour turn is unlikely but
   not impossible, and a false "still running" would hang the work order forever. If
   `/proc` is unavailable the liveness check degrades to `os.kill` alone.
2. **Ended?** Read `outfile`. Valid JSON with `is_error: false` → `done`, capturing
   `result`, `total_cost_usd`, `num_turns`. `is_error: true`, unparseable JSON, or an
   empty file → `failed`, with the tail of `.err` as `error`.
3. **Reply.** On `done`, `record_agent_reply(wo_id, result)` — one call, no retries. If
   the result text is empty, fall back to the `last_assistant_message` the `Stop` hook
   recorded on the timeline for that turn.
4. **Stall.** A turn `running` past `TURN_STALL_SECONDS` (6h) is *not* killed — a
   legitimately long turn is indistinguishable from a hung one, and killing loses real
   work. It raises an attention flag naming `jarvis wo cancel` instead.

### Work order state, derived from turns

The reconciler stops asking `claude agents --json` about workers entirely:

| Situation | Work order status |
|---|---|
| turn `running` | `running` |
| turn `failed` | `failed` + attention |
| turn `done`, `result_summary` set, assumptions pending | `needs_review` |
| turn `done`, `result_summary` set, none pending | `completed` |
| turn `done`, approvals pending | `waiting_input` (parked on a gate, as today) |
| turn `done`, messages queued | next turn launched immediately |
| turn `done`, none of the above | `needs_review` "idle without `jarvis wo finish`" |

This is the same settlement logic that lives in `reconcile_project` today, with the
session-roster lookup replaced by the turn row. The `dispatching` grace period, the
"session disappeared" failure and the name-prefix binding all disappear, because the thing
they compensated for is gone.

### Hook changes

- **`SessionStart`** no longer binds a session id — the id was assigned by Jarvis before
  the process existed. It records the event and corrects `dispatching` → `running`.
- **`SessionEnd`** becomes a pure timeline event. It fires on every turn now, and
  settlement is the turn reconciler's job. *This is the one change that would silently
  break the OS if missed*: leaving it as-is files every work order for review after its
  first turn.
- **`Stop`** additionally records `last_assistant_message` on the timeline, as the backup
  reply source described above.
- **`_is_current_session`** collapses to a plain equality check against the (now
  immutable) bound id.
- `Notification`, `PreToolUse` (gates, auto-approvals) and `PostToolUse` (memory mirroring)
  are untouched.

### What is deleted

- `claude_cli.job_result`, `claude_cli.jobs_dir`, `claude_cli.send_to_session`
- `daemon.capture_worker_reply`, `MAX_REPLY_CAPTURE_ATTEMPTS`, `reply_capture_misses`
- `daemon.delivery_pool`, `in_flight_deliveries` — launching a detached process is
  instant, so delivery no longer needs a thread pool
- `ProjectStore.work_orders_awaiting_reply`, `bind_session`, the `prior_sessions` column's
  purpose (column retained; SQLite cannot drop columns cheaply and old rows still carry
  history)
- `invariants.check_session_binding_moves_forward` / `INV-SESSION-FORWARD` — a binding
  that never moves cannot move backwards
- the `job_id` / `reply_job_id` columns stop being written (retained for old rows)

### Migration and the 63 stale sessions

Work orders already in flight are bound to a supervisor-assigned session id and owned by a
live background agent. `worker_session.send()` handles them without a special case: before
resuming, it looks the session up in the roster and, if a background agent owns it, calls
`stop_session()` first — a headless resume refuses a bg-owned session. So the first
message delivered to a legacy work order releases its background agent and moves the
conversation onto the new transport permanently.

Until that first message arrives, though, a carried-over work order looks exactly like a
launch that never happened: a session id and no turn. The reconciler must not settle it
from that absence — rehearsed against a copy of the live database, the naive reading marked
both real in-flight work orders `failed` on the first tick after the restart. `session_id`
(or the legacy `job_id`) tells the two apart, and the carried-over one is flagged for the
user rather than judged: send it a message to migrate it, or cancel it. Schema migration
needs no script — `ProjectStore` runs the whole `CREATE TABLE IF NOT EXISTS` script on
every open, so `wo_turns` appears on existing databases the first time the new code opens
them.

That same lookup drains the backlog as it goes. For the sessions with no work order left
to drive them, `jarvis doctor` gains a read-only report: how many `[WO …]`-named
background agents are in the roster with no open work order, and the exact `claude stop`
commands to clear them. Read-only on purpose — stopping the user's sessions is theirs to
authorise.

### Ad-hoc adoption stays

Adoption exists so sessions the *user* starts show up in `jarvis status` and on the
dashboard. Now that workers never enter the roster, the roster contains only the user's
own sessions, which makes adoption simpler and unambiguous rather than obsolete. It keeps
its `claude agents --json` poll and `INV-ADHOC-NOT-GOVERNED`.

### Not in scope

Opening a worker session in a browser terminal (`claude resume <sid> --disable-slash-commands …`).
The design makes it possible — the session id is stable, known, named, and no longer owned
by a background agent between turns — but the terminal client itself is a later change.
The UI's current `claude attach <session_id>` hint is corrected to `claude --resume <sid>`
run from the worktree, since `attach` is a background-agent verb.

## Testing

- `tests/test_worker_spawn_args.py` extends its arity table to the turn argv: the `--`
  fence, `--session-id` on turn 1 vs `--resume` after, and no bare positional after a
  variadic option.
- `src/jarvis/testing.py`'s `FAKE_CLAUDE` learns turn semantics: honour `--session-id`,
  reuse the id on `--resume`, write the result JSON to stdout, and expose knobs to force a
  failing turn and a slow turn. Its background-session behaviour stays for the adoption and
  migration tests.
- New `tests/test_worker_session.py`: session id is minted once and never moves; turn 1
  passes `--session-id`, turn 2 `--resume` with the same value; a completed turn records
  the reply exactly once; a crashed process (killed pid, no output) fails the turn rather
  than hanging it; a stalled turn flags attention without being killed; `cancel` kills the
  process group.
- `tests/test_pipeline.py` is rewritten off the roster and onto turns, keeping every
  behavioural assertion it makes today.
- A migration test: a work order bound to a bg-owned session gets that agent stopped
  before its next turn resumes.
