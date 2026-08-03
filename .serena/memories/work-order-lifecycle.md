# Work-order lifecycle and how a worker turn runs

The core flow of the OS. Module map in `mem:codebase-map`.

## States

`WO_STATUSES` at `project_store.py:16`, `OPEN_STATUSES` at `:26`. Every transition goes
through `ProjectStore.set_status()` — nothing writes status directly.

1. **`pending`** — `ops.create_work_order()` resolves the project path via
   `registered_project_paths()`, then `ProjectStore.create_work_order()` inserts with
   status `pending`. Entry points: CLI `cli.cmd_wo()`, UI `ui/app.py` `create_wo`.
2. **`dispatching`** — `Daemon.dispatch_pending()` loops while
   `store.count_active() < project.max_concurrent` and calls `claim_next_pending()`.
3. **`running`** — `dispatch.dispatch_work_order()` composes the prompt/settings and
   hands off to `worker_session.start()`, then sets `running`.
4. **`waiting_input`** — `hooks.py` on a `Notification`, or the reconciler when the
   work order is parked on a privileged-action gate.
5. **`needs_review` / `waiting_pr_merge` / `completed` / `failed`** — settled by
   `Daemon.settle_work_order()` from the latest **turn row**, see below.
6. **Close-out** — `ops.review_work_order()`, `cancel()`, `hide_work_order()`,
   `delete_work_order()`, and `mark_done()` (the user's own "this is finished", and the
   only way out of `waiting_pr_merge`).

**`waiting_pr_merge`** is set by `jarvis wo finish --pr <url>` (`ops.finish`, which
stores `work_orders.pr_url`). It is an OPEN status that deliberately raises **no**
attention flag — `invariants.true_blockers` has no branch for it, on purpose: it is a
merge queue the user works through in the dashboard, not a decision blocking the fleet.
Pending assumptions outrank it (`needs_review` wins).

`Daemon.poll_pull_requests` is what ends it, on its own `PR_POLL_EVERY_TICKS` (24, ~2min)
cadence — the only step in the OS that leaves the machine. It runs
`github.pr_view` (`gh pr view <url> --json state,mergedAt`) for each parked work order
and writes the answer to `work_orders.pr_state`:

* MERGED → `ops.complete_merged` — `completed`, event `pr_merged`, backlog item closed,
  worker stopped. Deliberately the same close-out as `jarvis wo done`
  (`ops.close_out`), with a different event so the record does not claim the user did it.
* CLOSED unmerged → `ops.record_pr_closed` — `needs_review` + attention. The reason is
  `invariants.PR_CLOSED_BLOCKER` and it MUST stay a `true_blockers` branch:
  INV-ATTENTION-REASON rewrites any reason that derivation does not produce.
* OPEN → nothing written.

Skipped entirely when no work order is parked, so an idle fleet spawns no subprocess.
A `gh` that cannot answer warns ONCE per project per daemon run (`pr_poll_warned`) and
leaves the work order parked; `jarvis wo done` is still the manual way out.

## The transport: headless turns (replaced background sessions, 2026-08-01)

**Read `src/jarvis/worker_session.py`'s module docstring first — it is the layer, and it
explains the why.** Design doc:
`docs/superpowers/specs/2026-08-01-headless-turn-runtime-design.md`.

A work order's conversation is a sequence of **turns**, one row each in `wo_turns`
(`project_store.py`). Each turn is a detached `claude -p` process Jarvis owns:

```
claude -p --output-format json --session-id <uuid> -n "[WO <id>] <title>" \
       --worktree <wo-id> [briefing] -- "<prompt>"      # turn 1
claude -p --output-format json --resume <uuid> -n "…" [briefing] -- "<prompt>"   # after
```

* `claude_cli.turn_args()` builds the argv, `spawn_turn()` detaches it
  (`start_new_session=True`, `stdin=DEVNULL`, stdout → `.jarvis/turns/<wo-id>/<seq>.json`).
* `worker_session.poll()` reaps: `claude_cli.process_alive(pid)` (signal-0 **plus** a
  `/proc/<pid>/cmdline` whole-path-component check — substring "claude" matches any
  `.claude/` path, including this repo's own venv) then `read_turn_result()`.
* The turn's `result` **is** the worker's final message → `record_agent_reply()`. If it
  comes back empty, fall back to `last_assistant_message` from the `Stop` hook event.
* One turn at a time per work order (`worker_session.busy`).
* A turn running > `TURN_STALL_SECONDS` (6h) raises attention; it is never killed.
* `worker_session.cancel()` kills the process group.

**The session id is minted by Jarvis and never moves.** `--session-id` is honoured under
`-p`, and a headless `--resume` REUSES the id rather than forking (`--fork-session` is the
opt-in to forking) — verified live against CLI 2.1.220. This is why `bind_session`,
`prior_sessions`, `INV-SESSION-FORWARD`, SessionStart-hook binding and `[WO id]`-name
reconciliation are all **gone**, not ported.

**Every turn re-sends the full briefing** (`worker_session.briefing_for` →
`claude_cli._briefing_args`): model, effort, permission mode, appended system prompt,
settings file, `--add-dir` skills. A resumed session re-derives all of it from argv, not
from the transcript, so anything omitted vanishes from that turn onwards.

**The `--` fence is load-bearing.** `--add-dir` AND `--tools` are variadic; an unfenced
prompt is swallowed as an option value.

**Worktree**: turn 1 runs with `cwd=project.path` + `--worktree <wo-id>`; later turns run
with `cwd=<that worktree>` and no flag (transcripts are keyed by creation cwd).

## Hooks (`hooks.py`) — what each one does now

All hooks fire under `-p`, verified live. Critically, **`SessionStart` and `SessionEnd`
fire on EVERY turn** (`SessionStart.source == "resume"` from turn 2 on).

* `PreToolUse` — gates + auto-approvals. Unchanged, and the gate machinery depends on it.
* `SessionStart` — records the event; only corrects `dispatching`→`running`. No binding.
* `Stop` — records the event, whose payload carries `last_assistant_message` (the backup
  reply source).
* `SessionEnd` — **deliberately inert.** It used to settle the work order
  ("session ended without `jarvis wo finish`"); under headless turns that would file
  every work order for review after its first turn. Settling is the reconciler's job.
* `Notification` — unchanged.

## Settlement (`Daemon.settle_work_order`)

| latest turn | work order |
|---|---|
| `running` | `running` (+ stall flag past 6h) |
| `failed` | `failed` + attention + notification |
| `done` + queued messages | untouched; the next turn goes out this tick |
| `done` + `result_summary` + pending assumptions | `needs_review` |
| `done` + `result_summary` + `pr_url` | `waiting_pr_merge` (re-settles here every tick) |
| `done` + `result_summary` | `completed` |
| `done` + pending approvals | `waiting_input` (parked on a gate — compliance) |
| `done`, none of the above | `needs_review` "idle without `jarvis wo finish`" |

`settle_turns` and `deliver_messages` run on **every** tick (cheap: a signal and a file
read). Only `track_injected_sessions` + `check_invariants` stay on the
`RECONCILE_EVERY_TICKS` cadence, and only the first needs `claude agents --json` — which
it skips entirely on ticks where no project has a live `injected` row.

## Background sessions still exist — for the USER only, and only if handed over

`claude agents --json` contains only sessions the user started. Jarvis does **not** adopt
them (GitHub issue 47): a session the user opened is theirs — not seen, not named, not
flagged, and above all not written into. `jarvis wo inject <session-id>`
(`ops.inject_session`, plus the panel on the project page) is the only way one enters the
OS. It creates the record and nothing else: no rename, no turn. From then on
`Daemon.track_injected_sessions` follows the session's state, and the row is never held to
the worker contract (`retire_ungoverned`, `INV-ADHOC-NOT-GOVERNED`).

`origin="injected"` is the new marker; `origin="adhoc"` is the legacy one from the
auto-adoption era. Both are in `project_store.UNGOVERNED_ORIGINS` and get the same
"not a worker" treatment, but only `injected` is tracked — `INV-ADHOC-LEGACY-RETIRED`
closes leftover `adhoc` rows once on upgrade, and tracking them too would reopen them.

**Migration is automatic**: `worker_session._release_background_owner()` fires when a work
order has no turn on record (a legacy dispatched one, or an injected one) and `claude stop`s
whatever background agent owns its session before resuming — which a headless resume
requires anyway. `jarvis doctor` lists leftover `[WO …]` agents with no open work order
(`ops.orphaned_worker_sessions`) but never stops them.

## Other `claude` invocation shapes

- `run_headless()` — `claude -p <prompt> --output-format json …` — Neo and the LLM evals.
  Note a worker turn is also a `-p` call now, so code/tests telling them apart must key on
  the presence of `--session-id`/`--resume`, not on `-p`.
- `spawn_background()` / `list_background_sessions()` / `stop_session()` — injected-session
  tracking and the migration path only.
- Deleted with the old transport: `job_result()`, `jobs_dir()`, `send_to_session()`.
