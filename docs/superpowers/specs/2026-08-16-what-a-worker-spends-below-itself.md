# What a worker spends below itself

*wo-76e021aa, 2026-08-16, closing issue #103. Companion to
`2026-08-16-what-jarvis-spends-on-itself.md`, which built the same mechanism pointing the
other way and is unchanged by this.*

## The gap

The other document counted the calls Jarvis makes ABOVE a worker — Neo answering it, the
panel deliberating on it, the digest shortening it. This one counts the calls made BELOW
it: `claude` processes a worker's own tool call spawns.

wo-52a6164d, shipping 0.5.4, reported 3.4M tokens / $4.98. During that work order the
worker ran the opt-in LLM eval suite twice — once on its branch, once against the previous
tag as an A/B comparison — and each suite spawns a real `claude` process per scenario. None
of that was in the 3.4M. The reports were most wrong on exactly the expensive work order
someone was investigating, which is the worst place for a cost report to be wrong.

## Why it cannot be recovered after the fact either

The same dead end, for the same reasons. A descendant call gets a session id Jarvis never
minted, so `work_orders.session_id` does not reach it. Its transcript is filed under the
slugified cwd it happened to run in — and for the eval suites that is a pytest tmp
directory nowhere near the worktree, so even "look in the worktree's slug" fails. Nothing
in the transcript names a work order.

Measured while building this: `~/.claude/projects/` has one directory per slugified cwd,
wo-52a6164d's contains exactly one transcript (its worker's), and the eval scenarios are
scattered across `-tmp-pytest-of-gonzalo-pytest-*` directories with nothing tying them to
anything. So: **recorded when the call returns, or lost.**

## The mechanism

`claude_cli.run_headless_result` records an `agent_calls` row of kind `worker_subprocess`
whenever `JARVIS_WO_ID` is present in the environment. That variable is the only thing
that reaches an arbitrary descendant: `dispatch._write_worker_settings` puts it in the
worker's `--settings` env block, and such a block reaches the CLI's own `process.env` (not
just its children — kn-522c6103), so every subprocess inherits it however deep it is.

The label is the program name from `sys.argv[0]`, not the cwd. An eval suite's cwd is a
fresh tmp directory per scenario, so labelling by directory produces forty labels naming
nothing; the program name collapses them into `pytest: 40 calls, ~$3.10`, which is the
sentence a reader wants.

The OS's own call sites — `neo.answer_question`, `panel._run_seat`, `panel._run_chair`,
`digest.CALL` — pass `attribute=False`, because each already records itself with the work
order AND the question it was made for, detail this seam cannot know. Without the opt-out
they would double-record the moment their process carried a `JARVIS_WO_ID`, which is what
happens when the LLM evals drive Neo in-process inside a worker.

## `JARVIS_SPEND_HOME`, and the hole it deliberately opens

Recording alone would not have fixed the case in the issue. The repo-root test-isolation
gate redirects `JARVIS_HOME` for every suite, `evals/` included — that gate exists because
on 2026-07-27 a worker's test run wrote live state and Telegrammed the user twice. So a
row written during `JARVIS_EVALS_LLM=1 uv run pytest evals/llm` would land in a tmp
directory deleted at teardown, and the whole fix would be a no-op precisely where it was
asked to work.

`agent_usage` therefore reads its own sink from `JARVIS_SPEND_HOME`, set at dispatch beside
`JARVIS_WO_ID`. `testing.gate_environment` redirects that too, and lifts the redirect only
when `_bills_real_tokens()` — BOTH `JARVIS_EVALS_LLM` (the run reaches the real model) and
`JARVIS_WO_ID` (a work order is paying). A human running the evals by hand has no work
order and stays fully sandboxed; a worker's ordinary test run is faking every call and
stays sandboxed too.

This mirrors the carve-out the gate already makes for the `claude` binary itself, for the
same reason: these evals exist to call the real model. It opens the usage-row write path
and nothing else — no other central-store write follows the variable, and no notification
path does. `tests/test_worker_subprocess_spend.py` asserts that by grepping the modules.

## The third class

Ruled on this work order: descendant spend is reported APART from Jarvis's overhead, not
folded into it. It is the work order's cost, so it is inside `total_cost_usd`; but a work
order that ran an eval suite spends nothing like one that asked Neo four questions, and a
single column would say they were the same — destroying the distinction the report exists
to show. So `cost_report` now carries three classes: `list_cost_usd` (the worker's own
conversation), `os_…` (Jarvis's overhead), `subproc_…` (what the worker spent below
itself). `jarvis cost` gains a `sub` column and a `subprocesses` totals line, the drill-down
gains a panel grouped by what ran the calls, and `/cost` splits its headline three ways.

Grouped rather than listed, unlike the OS's per-call table: an OS call is a deliberate,
countable act (five seats on one gate review) and a descendant call comes in batches. The
grouping key moved into SQL (`agent_call_totals` now groups by kind, label and model) so
the sum has no row limit to truncate against — a truncated sum understates exactly the
expensive work order being investigated.

## The floor, and why it is unconditional

This cannot be complete. A worker can run a bare `claude -p` from a shell — `evals/llm/
test_navigation_judgment.py` does — and such a call comes through no seam Jarvis owns.

So every cost report carries `floor: True` and `ops.COST_FLOOR_NOTE`, rendered identically
by the CLI and both dashboard pages, whether or not any descendant call was recorded. The
alternative considered was a heuristic — scan the worker's transcript for Bash commands
that look like they run `claude` — and it was rejected: `uv run pytest evals/llm` contains
no such string, so the heuristic would be blind in exactly the case it was built for, and
a "no descendants detected" that is wrong is worse than no claim at all. A flat statement
is always true and costs one line. (Ruled by Neo on this work order.)

## What it does not do

It does not backfill: wo-52a6164d's eval spend is gone, like every OS call before #101
shipped. It does not catch descendants outside Jarvis's transport — hence the floor. And
it changes nothing about how a token is counted or priced; `_call_spend` is one loop
shared by both classes, so the two cannot drift apart in arithmetic, only in meaning.
