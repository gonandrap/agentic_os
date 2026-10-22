# Workers on background sessions, and a lead that does the work itself

*2026-10-22*

## Problem

Measured on the last ten production orders against plain Claude Code background
sessions (same parser, same list prices):

| | cost | lead API calls |
|---|---|---|
| single-task background sessions | $1–14 | 15–51 |
| the six code-writing orders | $12.6–56 | 74–317 |
| wo-e8ca83ad (+79/−64 lines) | $20.34 | 156 |

The starting context is the same on both sides (~51k first call vs 45–60k). The gap is
the NUMBER of calls, each re-reading a 90–175k conversation. Across seven code orders
($196): lead $99, subagents $65, Jarvis's own calls $31. Of the lead's 1,128 calls, only
22% followed real work (read/edit/search); polling, Stop-hook refusals and task
notifications were 337 calls ($27), git/gh 190 ($16 — 38 of them `git diff --stat`
re-checking a seat's work), `jarvis` calls 95 ($8), tool loading 60 ($4).

Two rules produce it:

1. **Mandatory delegation** — `worker.require_crew` (default on) refuses the lead's own
   edits, so a 20-line change is explored three times: by the lead, by
   `jarvis-spec-writer`, by `jarvis-implementer`; then the lead re-reads the diffs.
2. **The one-shot turn** — a worker turn is a `claude -p` process, so a background seat
   dies when the turn ends. The OS answers with a brief rule (poll every 3–4 minutes), a
   PreToolUse refusal of foreground seats, and a Stop hook that refuses to end the turn
   (`UNCOLLECTED_TASK_BLOCK`). Every poll is a full-context call.

## The transport: background sessions, without the fork

The headless transport (2026-08-01) left `claude --bg` because `--bg --resume` FORKED the
session. On Claude Code 2.1.308 it no longer has to. Measured live (haiku spike,
`scripts/spike_bg_transport.py`):

| # | Fact |
|---|---|
| 1 | `--bg` ignores `--session-id` and mints its own id; it prints `backgrounded · <short>` where `<short>` = `sessionId[:8]` |
| 2 | `claude agents --json --all` lists it: `id`, `sessionId`, `pid`, `status` (`busy`/`idle`), `state` (`working`/`done`/…) |
| 3 | hooks fire (`SessionStart` startup/resume, `Stop` with `last_assistant_message`, `SessionEnd`); `--settings` env reaches them |
| 4 | the idle session keeps a live process (a pre-warmed `claude bg-spare`, whose cmdline does NOT carry the session id) |
| 5 | `--bg --resume <sid>` on a session that is STILL ALIVE (even idle) starts a copy under a new id |
| 6 | `--bg --resume <sid> <any flag>` on a stopped session also starts a copy ("keeps its own saved options") |
| 7 | **`--bg --resume <sid> -- "<msg>"` with NO flags on a stopped session wakes THE SAME session** with its saved options, conversation intact, `SessionStart source=resume` |
| 8 | `claude stop` returns before the process is gone: resuming at once forks, and the copy runs in the CALLER's cwd. The roster drops the `pid` ~0.6s BEFORE the process exits, so wait for the PROCESS (the pid read before stopping) to die, not for the roster |
| 9 | **a background task outlives the turn and wakes the session itself**: `Stop` fires with the interim reply, then again with the final one; no poll, no hook |
| 10 | while that task is in flight `agents` reports `status: busy, state: working` even though `Stop` fired; it flips to `idle/done` only when the session has settled |
| 11 | `claude respawn <id>` restarts the process under the SAME id (crashed or live), sends no prompt |
| 12 | `--bg --worktree <name>` creates and runs in the worktree; trust is per exact path and a project path is trusted by `bootstrap.ensure_trust` |
| 13 | `--max-budget-usd` is `--print`-only |

So a worker conversation is ONE background session for its whole life:

```
turn 1   claude --bg -n "[WO id] …" --worktree <wo-id> [briefing] -- "<prompt>"     (cwd = project)
turn n   claude stop <short>; wait for no pid; claude --bg --resume <sid> -- "<msg>" (cwd = worktree)
```

### What stays

`wo_turns`, settlement, pauses (usage limit / transient / auth / budget), compaction,
harvest, the bill, `deliver_messages`' holds — all unchanged. A turn is still "one prompt
the OS sent, until the worker settles". Only two seams change:

* **launch** — `claude_cli.spawn_bg_turn` / `resume_bg_turn` instead of `spawn_turn`.
* **reap** — a turn is over when the roster says `idle` (fact 10), or the session is gone.
  Its `TurnResult` is built from what the session left: the reply is the LAST `Stop`
  hook's `last_assistant_message` since the turn began (else the transcript's last
  assistant text); the cost, tokens and context peak come from the transcript
  (`usage.turn_usage_between`); a synthetic assistant message in the window is the
  turn's error text, which `usage_limit`/`transient_failure`/`auth_failure` already read.

### What changes meaning

* **The session id is learned, not minted.** It is read off the roster by the short id
  `--bg` printed, and stored on `work_orders.session_id` / `bg_id` before the row says
  `running`. It never moves after that (fact 7).
* **A turn spans the worker's own background work.** The OS holds queued messages while
  the roster says `busy`, so a message never interrupts a seat (fact 10). Stopping a busy
  session would kill its tasks; `deliver` only ever stops an `idle` one.
* **Budget** — no `--max-budget-usd` (fact 13). The launch floor (`budget.launch_refusal`)
  is unchanged; mid-turn, `poll` compares `budget.in_flight` against the remaining cap and
  stops the session, settling the turn as `turn_budget_exhausted` exactly as the CLI's own
  refusal did.
* **A dead session** (roster lists it with no pid while its turn runs): if a `Stop` hook
  fired since the prompt went in, the turn is `done` on that reply; otherwise it is lost to
  the transport — `terminal_reason = claude_cli.SESSION_LOST`, a `PAUSE_TRANSIENT`
  retried on the transient backoff (the nudge if any API call was made, the prompt again
  if none). Waking it is the same bare resume (fact 7).
* **Respawn** (fact 11) restarts the process and sends nothing, so it is not a delivery
  path: the bare resume already continues the same session WITH the message. It is
  exposed as `jarvis wo respawn <id>` for the case it is for — bringing an order's own
  session back on the current binary (a Claude Code upgrade, a hung process) without
  sending it anything. Refused while a turn is in flight.
* **Settled means stopped** — once a turn settles, the idle session is `claude stop`ped:
  its process holds nothing, the conversation is kept, and the next prompt would stop it
  anyway. One stopped `[WO …]` entry per order stays in `claude agents`;
  `ops.orphaned_worker_sessions` (doctor) now reports only `[WO …]` sessions that still
  hold a process. `jarvis wo delete` runs `claude rm` (which keeps a worktree holding
  unpushed work).

### What goes

Under the background transport the hooks that exist only because a turn was one-shot
already key on `JARVIS_TURN_TRANSPORT=headless` and fall silent: the background-Bash
refusal, the long-foreground refusal. The Stop hook's uncollected-task block now keys on
it too. The brief's "poll every 3–4 minutes / never end a turn with a task uncollected"
paragraph is written only for the headless transport; the background one is told the
truth: background anything long, end the turn, you will be woken.

### The switch

`worker.transport`: `"background"` (default) | `"headless"`. Read at DISPATCH and recorded
on the order (`work_orders.transport`), so an order keeps the transport its session was
born on; flipping the catalog moves new orders only. Headless stays fully working as the
fallback.

## The lead does the work

* `worker.require_crew` defaults to **false**. The seats stay installed; the brief says:
  do the work yourself; delegate only a large, independent, well-specified chunk, or work
  you want done in parallel.
* A seat's hand-back IS its report: do not re-verify it with `git diff --stat` polling;
  read the final diff once before committing.
* Load every deferred tool you will need in ONE `ToolSearch` (`select:a,b,c`).

## Not done, and why

* **Changing compaction** — measured with `scripts/compaction_payoff.py` on production
  since 2026-10-01: 150 compactions, $151 spent, ~$489 saved, net +$338, 90.5% of the
  closed ones paid off (median break-even: the first turn after). It stays as it is.

* **Batching assumptions into one Neo question** — reverses the 2026-10-21 ruling that a
  question to Neo is atomic. Left alone.
* **Peer messaging (`SendMessage`) as the delivery path** — Jarvis's daemon is not a
  Claude session; the only sender today would be a model relaying the text. The
  stop-and-resume path is deterministic. Revisit when a non-model sender exists
  (`mem:cross-session-messaging-facts`).
