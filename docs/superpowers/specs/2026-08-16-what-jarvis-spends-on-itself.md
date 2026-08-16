# What Jarvis spends on itself

*wo-0fea6edb, 2026-08-16. Companion to `2026-08-09-where-the-tokens-go.md`, which built
the worker half of this and is unchanged by it.*

## The gap

`jarvis cost` measured one thing: the worker's conversation, read back from Claude Code's
session transcript. Everything the OS spent ANSWERING that worker was invisible.

A work order asks Neo a question. That is a `claude -p` call. If the panel is on it is
five — four seats blind plus the chair. If the answer is long enough to need shortening
for the dashboard, that is a sixth. None of them appeared in any report, because
`claude_cli.run_headless` parsed the CLI's result JSON for its `result` string and dropped
the `usage` object sitting beside it.

The scale is not marginal. A short work order that asked three questions under a panel
pays fifteen OS calls against a handful of its own turns.

## Why it cannot be recovered the way worker turns are

Worker spend is recoverable after the fact by construction: Jarvis mints the session id
(`--session-id`), Claude Code keeps a transcript per session, and `work_orders.session_id`
is the join. An OS call has none of that. It is a one-shot `claude -p` with a session id
Jarvis never chose, no work order named anywhere in it, and a transcript filed under the
slugified `$JARVIS_HOME` shared with every other OS call ever made.

So there is nothing to attribute later. **The envelope is recorded at the moment the call
returns, or it is lost.** That is the whole reason this is a persistence change and not a
reader change.

## The three layers

**Persistence.** `agent_calls` in `os.db` (`central_store`): one row per OS-side call,
carrying project, wo_id, kind, label, model, question id, the CLI's own `cost_usd`, the
token classes as columns, and the full `derive_turn_usage` envelope as JSON. Columns *and*
JSON: the columns are what the fleet report sums in one SQL pass over every work order,
the JSON keeps what no column holds (the ephemeral 1h/5m split, the per-call context
peak). `purge_work_order` deletes them with the rest of a deleted order's history.

Central rather than per-project or in `neo.db` — Neo, the panel and the digest are three
subsystems and the next OS call will be a fourth, while `os.db` is already the store that
unifies across projects and already owns the work order's purge path.

**Capture.** `claude_cli.run_headless_result` is the new primitive and returns
`HeadlessResult(text, usage, session_id, model)`; `run_headless` is now a thin wrapper that
keeps the text, for callers with nothing to account. Every OS call site takes a `record=`
seam defaulting to `agent_usage.record`, mirroring `neo.drain_queue`'s existing `answer=`
seam — which keeps the store out of `panel._run_seat` (it runs on a pool thread, where a
connection opened elsewhere would be a bug) and lets a test assert on what would have been
written without a database at all. `structured.request` grew an `on_usage` callback,
called once per ATTEMPT: a retry is a second call the OS paid for.

**Report and UI.** `ops.cost_report` adds `os_*` fields per unit and a `total_cost_usd`
that is worker + OS. `jarvis cost` grows a `jarvis` column and a `jarvis itself` total
line; the per-work-order view lists the OS's calls one by one. The dashboard's `/cost`
splits its headline into `workers` and `jarvis`, and the drill-down gets a panel listing
every OS call with its seat.

## Three decisions worth keeping

**One row per seat, never one per question.** Whether the panel earns its price is exactly
what its seats cost against what the single agent would have cost, and which seat is the
expensive one. An aggregate row can answer neither. (Ruled by Neo on this work order.)

**Two currencies, never blended.** The CLI's own `total_cost_usd` is exact;
`usage.priced()` re-prices the same tokens at Anthropic list prices. The report carries
both (`os_recorded_cost_usd`, `os_cost_usd`) and only ever adds like to like — the
list-priced OS figure to the list-priced transcript estimate, the exact to the exact. This
is the same discipline `_turn_summary`'s provenance label exists to enforce: an exact
figure added to an estimate produces a number that is neither.

**Recording never raises.** Accounting observes. A work order must not fail, and Neo must
not stop answering, because a row could not be written — so every failure in
`agent_usage.record` is logged and swallowed. The cost is a missing row, and the number
the report gives is therefore a floor.

## What it does not do

It does not backfill. Spend before this shipped is not recoverable — the transcripts are
undifferentiated and nothing in them names a work order. Every report over a period
spanning the release under-reports the OS's half for the part before it.

It also does not touch the user's own Jarvis sessions (this conversation, in a terminal or
the desktop app). Those are the user's, not the OS's, and Jarvis never adopts a session it
did not start.
