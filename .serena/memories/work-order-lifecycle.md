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
settings file, `--add-dir` skills, `--autocompact`. A resumed session re-derives all of it
from argv, not from the transcript, so anything omitted vanishes from that turn onwards.

**`--autocompact` bounds how large the conversation may grow** — default 400,000
(`catalog.DEFAULT_AUTOCOMPACT_WINDOW`), fleet-wide `os.defaults.autocompact_window`,
per project `worker.autocompact_window`, explicit `null` to opt out. It is the effective
context WINDOW, not the trigger: the CLI takes `min(model window, this)` and arms
compaction at a fraction of it. Alone among the briefing flags it is read from the CATALOG
on every turn rather than resolved onto the work-order row at dispatch — it is a spend
control, so tightening it must reach work orders already running. Why it exists and what it saves:
`kn-81a91bac`, `docs/superpowers/specs/2026-08-10-resume-cost-and-the-cache.md`.

**Workers run with `includeGitInstructions: false`** (written by
`dispatch._write_worker_settings`). It is a CACHE lever, not a git preference: Claude Code
rebuilds a git-status snapshot into the system prompt once per process, a worker turn IS a
process, and the worker changes that snapshot by editing files — so the cached prefix for
the whole conversation died at every turn boundary. Measured on 2.1.233: turn 2 goes from
writing 10,983 / reading 15,995 to writing 552 / reading 26,113. The same gate removes the
CLI's git and commit/PR blocks, so `worker_brief.git_briefing()` restates them as static
text on `--append-system-prompt` (composed in `briefing_for`, project instructions appended
after it). `tests/test_stable_prefix.py` holds the flag and the briefing together — the
flag alone silently strips the attribution trailers from every fleet commit and PR.
Write-up: `docs/superpowers/specs/2026-08-15-a-stable-prefix-for-resumed-workers.md`.

**The `--` fence is load-bearing.** `--add-dir` AND `--tools` are variadic; an unfenced
prompt is swallowed as an option value.

**The knowledge base is INDEXED into the briefing, not pasted into it.**
`CentralStore.knowledge_brief()` returns a bounded `KnowledgeBrief`; `render_knowledge_block()`
renders it inside `_common_briefing()`, so worker AND planner prompts get it. Three tiers:
entries tagged `pinned` in full (cap `os.knowledge_inject_limit`), then one headline + id per
entry selected **round-robin across topics** (caps `os.knowledge_digest_limit`/`_chars`), then
a by-topic count of what did not fit. Retired entries appear in none of them — retraction has
to remove a ruling from the map as well as the payload. Workers cash an id in with
`jarvis learn show|search|list|topics`. Prompt cost is therefore flat in the size of the base;
the worker that needs an entry pays one tool call for it.

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
| `failed` **on the usage limit** | UNTOUCHED — see the next section |
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

## Self-healing when the transport fails (usage limit: wo-996c7344, PR 89;
## API errors: wo-50958234)

TWO reasons a turn dies without the WORK being wrong, one shared mechanism. Both are
`worker_session.PAUSE_*` values on one `TurnPause`, because four readers share the
predicate and a second parallel one would be four more places to forget.

| | usage limit | transient |
|---|---|---|
| what | the account's window is spent | a 500/529/dropped connection |
| did it run? | **no** — 0ms, $0, nothing sent | **yes**, possibly for minutes and dollars |
| when to retry | the moment the refusal names | `TRANSIENT_BACKOFF` = 60/120/360/600/1200s |
| what to re-send | the prompt, verbatim | a continuation nudge (see below) |
| cap | `MAX_RATE_LIMIT_RETRIES` (8) | 5, the length of the backoff |

### The usage limit


Claude Code refuses a turn outright once the account's window is spent. The refusal is
free and instant (`duration_api_ms: 0`, `total_cost_usd: 0`) and arrives as the turn's
own result — `is_error: true`, `subtype: "success"`, body
`You've hit your session limit · resets 11:50pm (America/Los_Angeles)`.

**The message is assembled, so match its SHAPE and never its words.** From 2.1.226's
string table (`WJe`, map `HUt`): `You've hit your ${label} · resets ${when}`, where the
label is keyed by rate-limit type — `five_hour` → "session limit", `seven_day` →
"weekly limit", `seven_day_opus` → "Opus limit", `seven_day_sonnet` → "Sonnet limit",
`seven_day_overage_included` → **"Fable 5 limit"**, `overage` → "usage credit limit".
Two of those carry a MODEL name, so a word list rots at the next launch — it already
missed the last two. `claude_cli._LIMIT_RE` therefore matches "limit" … "resets" within
60 chars, and the reset having to PARSE is the real false-positive gate (it is what
keeps a worker's own `the rate limit reset logic in daemon.py:942` out).

Spend caps deliberately do **not** match: they render `· run /usage-credits to raise it`
instead of a reset, and they do not reopen on their own — the user has to act.

`claude_cli.usage_limit(text) -> UsageLimit | None` resolves four renderings, which the
CLI's formatter (`dye`) picks between by distance: `11:50pm (TZ)` under 24h;
`Aug 14, 9:50am (TZ)` over it (the ordinary 7-day case); `Jan 3, 2027, 11:50pm (TZ)`
when it crosses a year — **read that year, an unread one parses as the HOUR**; and
`resets in 2h 15m` (fast mode). Minutes are dropped when zero. An unknown timezone falls
back to LOCAL, never UTC. `Claude AI usage limit reached|<epoch>` is a legacy branch,
kept but UNVERIFIED — that literal is absent from 2.1.226.

To re-verify on a new CLI: `strings -n 8 ~/.local/share/claude/versions/<v> > /tmp/cc.txt`
then search with python `re` (shell grep fights the `${}` and backticks).

### Reading an API failure — STRUCTURED, unlike the limit

`claude_cli.transient_failure(text, *, terminal_reason, api_error_status)` needs no prose
parsing: the result envelope carries the CLI's own diagnosis, and Jarvis used to drop it.
Verified live on wo-4f460495 turn 2 — `terminal_reason: "api_error"`,
`api_error_status: 500` (the CLI's schema documents that as "HTTP status code of the API
error"). Both are now persisted on `wo_turns`, because the result file they come from is
pruned by Claude Code while the verdict must stay re-derivable.

**Retriable = `api_error_status >= 500`, and nothing else** (Neo, question 126). Every
failed turn the fleet had ever recorded when this shipped — ten — was one of three
shapes: `api_error`+429 (usage limit, 6), `api_error`+500 (1, the one that stalled 27h),
and `aborted_streaming`/`aborted_tools` with no status (3).

* **429 is excluded on purpose.** It is the limit's own code, and the form `usage_limit`
  declines is a SPEND cap that never reopens by itself — backing off five times would
  burn attempts on something only the user can clear. `transient_failure` re-checks
  `usage_limit` itself so the ordering cannot be got wrong by a caller.
* **Aborts are excluded** because they land mid-tool-use and a replay can re-run a side
  effect. The CLI's own telemetry does not count them as errors either (`m3n`).

A text branch (`_TRANSIENT_TEXT_RE`) catches the two status-less shapes the CLI
assembles — `Connection to the API was lost`, `The API is at capacity` — and old rows
predating the columns. It is anchored on the `API Error: ` prefix so a worker's own
stderr quoting a 500 cannot park its own work order.

### The shared state

`worker_session.turn_pause(store, wo_id) -> TurnPause | None` is the state.
**There is no column, no flag and no status for it**: the whole condition is re-derived
from the latest turn each time it is asked for, per the rule in `project_store.py:281-285`
("`waiting_pr_merge` earned a status because nothing derived it; this does not").
Neo settled that explicitly — question 83 on wo-996c7344. Four readers share the one
predicate so they cannot drift:

* `Daemon.settle_work_order` — returns early, so the work order keeps the ACTIVE status
  it had. This is load-bearing: `failed` is a `DEPENDENCY_DEAD_STATUS` (strands
  dependents), `Daemon.settle_features` fails the parent feature order off one failed
  child, and `true_blockers` re-derives the attention reason FROM the status, so
  "leave it failed and retry quietly" is not available.
* `Daemon.deliver_messages` — HOLDS newer messages while paused, so the refused turn
  (which carries a message already marked `delivered`) goes out first. The hold lifts
  once retries are exhausted, which keeps `jarvis wo send … "retry"` working by hand.
* `Daemon.retry_paused_turns` — relaunches via `worker_session.retry` every
  `RETRY_EVERY_TICKS` (2, ~10s). It was 12 (~1min) while the usage limit was the only
  reason; a 60s pass cannot honour a 60s backoff step.
* `invariants.pause_note` — the one user-facing string, rendered by `status_label` (CLI)
  and `ops.os_status` → `open_work_orders[].pause` (dashboard):
  `running — Claude usage limit reached, retrying by itself at 23:50`, or
  `running — Claude API error 500, retrying by itself at 14:07 (attempt 2 of 5)`.

Constants in `worker_session.py`: `RATE_LIMIT_MIN_DELAY` (60s floor — the reset is a
clock time rounded to the minute and can parse into the past), `RATE_LIMIT_FALLBACK_DELAY`
(15min when no time is readable), `MAX_RATE_LIMIT_RETRIES` (8 consecutive refusals, then
it fails for real). `pause_streak(store, wo_id, reason)` counts off the tail via
`ProjectStore.recent_turns` (NOT `list_turns`, whose LIMIT applies from the front) and is
**per reason**: a conversation that hit the limit and then a 500 is on its first 500, so
the limit's attempts cannot cut the backoff short or start it already exhausted.

`worker_session.retry` re-decides two flags **from the filesystem** rather than copying
them off the dead turn: `--resume` only if `claude_cli.session_transcript_path` exists
(a session never written cannot be resumed), and `--worktree` only if the worktree
directory does not exist yet (that flag is what creates it).

**And it re-decides WHAT TO SEND** (Neo, question 126). Re-sending the prompt verbatim is
right only for a turn refused before it ran. wo-4f460495's transcript held its prompt
followed by ~55 messages and $1.99 of work, so re-sending there would duplicate a message
the worker had already acted on. When `_reached_model(turn)` — `duration_api_ms > 0` from
the stored usage envelope — AND the session exists, `retry` sends `_nudge(pause)` instead:
"your previous turn was cut short … carry on … do not start again". This applies to the
usage-limit path too; one refusal on record had already done 15s of work.

Timeline kinds: `turn_paused`, `turn_resumed`, `turn_retries_exhausted`, each carrying
`reason` in its payload — all signal, and `turn_failed` is deliberately NOT emitted.
The legacy `rate_limited`/`rate_limit_retry`/`rate_limit_exhausted` kinds still RENDER
(rows written before this) but are no longer written; an absent `reason` reads as the
usage limit, which is what those rows all were.

Tests: `tests/test_rate_limit_retry.py` and `tests/test_transient_retry.py`. The fake CLI
has `turns_rate_limited()` (refuses BEFORE writing the transcript) and `turns_api_error()`
(fails AFTER writing it, reporting API time) — that asymmetry is the property under test,
so do not "tidy" it away.

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
