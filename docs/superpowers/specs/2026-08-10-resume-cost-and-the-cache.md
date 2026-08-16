# Why resuming two work orders burned 40% of a fresh usage window in two minutes

**Work order:** wo-5668a3f7 · **Measured:** 2026-08-10 · **Subjects:** wo-67d4f8b0, wo-996c7344
**Companions:** `docs/superpowers/specs/2026-08-09-where-the-tokens-go.md` (kn-1485b845),
`docs/superpowers/specs/2026-08-08-token-spend-findings.md` (kn-625e79f1)

## The report

Both work orders were parked on the 5-hour Claude usage limit at ~08:00 local. The window
reopened at 12:10. At 12:41 the user sent each worker "session limit restored, continue".
Within two minutes the fresh usage window was ~40% spent.

The hypothesis in the work order was that `claude -p --resume <sid>` re-loads the whole
conversation and that the fix is to detect a cache miss (proposed threshold: 4.5 minutes)
and compact before sending. **The first half is true but is not the defect; the second
half is wrong in both of its numbers.** What follows is the measurement.

## Method

All figures come from Claude Code's own transcripts under
`~/.claude/projects/<slugified-cwd>/`, read with the discipline `kn-2137076d` established:
dedupe by `message.id`, take the **max** of each usage field (rows are written two or three
times as a message streams), and include `.../<session-id>/subagents/agent-*.jsonl`, which
is where a third of a fan-out work order's spend lives.

"Context of an API call" below means `input + cache_creation + cache_read` — what was
actually put on the wire for that call.

## Finding 1 — five conversations woke up, not two

| session | calls | cache-create | cache-read | output |
|---|---:|---:|---:|---:|
| `wo-996c7344` worker | 9 | 486,001 | 1,597,829 | 3,317 |
| `wo-67d4f8b0` worker | 3 | 182,841 | 407,064 | 1,741 |
| subagent `a1f97d76…` | 5 | 131,872 | 518,905 | 2,494 |
| subagent `a88422dd…` | 5 | 147,633 | 612,762 | 3,109 |
| subagent `aaea60f3…` | 1 | 94,029 | 6,010 | 1,602 |
| **total, first 2 min** | **23** | **1,042,376** | **3,142,570** | **12,263** |

`wo-67d4f8b0` had fanned out to three subagents before it was refused. Its **first act on
resume** was to `SendMessage` all three "the session limit is restored — continue" (12:42:27,
12:42:31, 12:42:33). Each of those is a full conversation of its own, and each resumed cold.

Weighted as input-equivalents (cache-write **2×** — see Finding 2b on the TTL — cache-read
0.1×, output 5×), those two minutes cost ~2.46M tokens, of which **85% is cache creation**.
That is the spike.

It does not stop at two minutes, and around the ten-minute mark the larger term flips:

| window | calls | cache-create | cache-read | weighted | dominant term |
|---|---:|---:|---:|---:|---|
| 2 min | 23 | 1,042,376 | 3,142,570 | 2.46M | writes (85%) |
| 5 min | 64 | 1,105,733 | 10,583,197 | 3.61M | writes (61%) |
| 15 min | 146 | 1,184,173 | 26,304,623 | 5.60M | **reads (47%)**, writes 42% |

146 API calls in fifteen minutes, each re-reading a 250–290k context.

> **Correction.** The first version of this document weighted cache writes at 1.25×. That
> is the **5-minute** TTL rate; every write in these transcripts is a **1-hour** write,
> which costs **2×**. The table above is corrected. The error understated the boundary
> term and moved the read/write crossover earlier; it does not change any conclusion, and
> it makes the case for both fixes below stronger rather than weaker.

## Finding 2 — the wait did not cause the cold cache, and no threshold could have

Two independent corrections to the proposed fix:

**The TTL is one hour, not 4.5 minutes.** Every cache write in both transcripts is
`ephemeral_1h_input_tokens`; the 5-minute field is zero. Across all 1,050 transcripts on
this machine: 60.7M tokens written at 1h TTL against 3.1M at 5m. (5m appears only in usage
overage.) A 4.5-minute rule would throw away a *warm* cache for ~55 minutes of every hour —
and one hour is the longest TTL the API offers, so nothing could have survived a 4h41m wait.

### Finding 2b — the 1-hour TTL is bought and almost never used

The TTL is not just "long enough"; it is **priced**. A prompt-cache write costs **1.25×**
base input at the 5-minute TTL and **2×** at the 1-hour TTL. A read costs 0.1× under either.
So the longer window is a 60% surcharge on every write, and it earns that only for reads
that land more than five minutes after the write.

Across all 1,075 transcripts, grouping every warm read by the gap since the previous call
in its session:

| gap since previous call | reads | share | read tokens | share |
|---|---:|---:|---:|---:|
| under 1 min | 10,834 | 92.8% | 1,405,514,413 | **92.4%** |
| 1–5 min | 659 | 5.6% | 88,135,184 | 5.8% |
| 5–15 min | 74 | 0.6% | 6,461,298 | 0.4% |
| 15–60 min | 23 | 0.2% | 2,433,208 | 0.2% |
| over 1 hour | 80 | 0.7% | 1,635,882 | 0.1% |

**98.1% of cache-read tokens would have been served by a 5-minute TTL.** The reads that
matter are the agentic loop *inside* one turn — seconds apart — not the gap between turns.
And the gap between turns is a cold boundary regardless (Finding 2), so the long window
cannot rescue it; it only makes the write that follows 60% dearer.

Costed over that corpus at Opus list prices: the 1h premium was **~$232**, against **~$162**
of reads that would have missed on a 5m TTL and been re-written. Net saving ≈ **$70**, about
5% of the cached-token bill. Small — because reads dominate everything — but free.

**Shipped:** `FORCE_PROMPT_CACHING_5M=1` in every worker's settings env. Claude Code decides
the TTL in `L0e` (2.1.227): this env var forces 5m, `ENABLE_PROMPT_CACHING_1H` forces 1h,
and the default allowlists `repl_main_thread*`, `sdk`, `auto_mode`, `memdir_relevance` by
`querySource` — a headless worker turn matches, which is why every write above is a 1h one.
(Usage overage already forces 5m; that is the 3.1M of 5m writes in the corpus.)

**Verified live, not inferred.** A settings-file `env` block is not obviously the same thing
as the CLI's own `process.env` at request-build time — Jarvis's existing env vars are all
read by *child* processes (hooks, Bash tool calls), so none of them proves this path. Two
`claude -p` runs on a scratch directory, differing only in `--settings`:

| arm | cache write | `ephemeral_1h` | `ephemeral_5m` |
|---|---:|---:|---:|
| control, no settings file | 8,774 | **8,774** | 0 |
| `FORCE_PROMPT_CACHING_5M=1` via `--settings` | 26,962 | **0** | **26,962** |

(The write sizes differ because each arm used a unique `--append-system-prompt` marker to
force a fresh write; the TTL split is the variable under test, and it flips completely.)
The mechanism in the binary is `Object.assign(process.env, filterSettingsEnv(...))` at
settings-load time, which precedes request construction — and `br()` accepts `"1"`,
`"true"`, `"yes"`, `"on"`, so the value matters too.

**More importantly, the boundary would have been cold anyway.** Classifying every cold
boundary in every transcript — an API call that writes >40k while reading <40k — by the gap
that preceded it:

| gap before the boundary | count | median write |
|---|---:|---:|
| under 5 minutes | **149** | 106,486 |
| 5 min – 1 hour | 14 | 137,938 |
| 1 – 4 hours | 31 | 83,427 |
| over 4 hours | 44 | 103,629 |

**149 of 252 cold boundaries had a gap under five minutes.** This is `kn-625e79f1`'s finding
seen from the other side: a worker's `git status` rides in Claude Code's dynamic system
prompt, the worker changes it by doing its job, so the prompt prefix differs on the next
turn and everything after it is re-sent as a write. Time is not the variable. The 4h41m wait
cost these two work orders **nothing extra**.

So there is no cache to "leverage" on resume. The only lever is to make the thing that gets
re-sent — and re-read on every subsequent call — **smaller**.

## Finding 3 — one boundary in ten pays the write twice

`wo-996c7344` wrote its context twice, fifteen seconds apart, at *both* of its long-gap
boundaries:

```
12:41:37  cache_w 242,129  cache_r 15,757   <- resume
12:41:42  (MCP servers finish connecting: deferred_tools_delta + mcp_instructions_delta)
12:41:52  cache_w 240,089  cache_r 19,331   <- written again
12:41:59  cache_w     165  cache_r 259,420  <- warm from here
```

`wo-67d4f8b0` paid it once (178,605, then warm at 194,366). The matched prefix on the second
call is 19,331 against 15,757 on the first — a delta of exactly 3,574 tokens at both of
`wo-996c7344`'s boundaries, i.e. the second call matched the system prompt *and its dynamic
section* but nothing of the message history. The correlate present in `wo-996c7344`'s two
double-writes and absent from `wo-67d4f8b0`'s single ones is an MCP server finishing its
connection **between** the two calls and injecting tool schemas plus instructions ahead of
the conversation.

**This is now settled, and the mechanism is documented rather than inferred.** Prompt
caching is a prefix match over `tools` → `system` → `messages`, and the invalidation
hierarchy is published:

| what changed | tools cache | system cache | messages cache |
|---|:--:|:--:|:--:|
| **tool definitions (add/remove/reorder)** | ❌ | ❌ | ❌ |
| model switch | ❌ | ❌ | ❌ |
| system prompt content | ✅ | ❌ | ❌ |
| message content | ✅ | ✅ | ❌ |

`tools` renders at position 0, so **adding a tool invalidates everything** — the whole
conversation is re-written. An MCP server finishing its connection mid-turn does exactly
that: it adds entries to the tools array and an instructions block to the system prompt.

That also resolves the anomaly that kept this a hypothesis. `wo-67d4f8b0` called
`ToolSearch` between its two calls and stayed **warm** — because tool search is documented
to *append* discovered schemas rather than swap the tool set, specifically to preserve the
cache. Server connection and tool search look identical in the transcript (both emit a
`deferred_tools_delta`) and have opposite cache effects. That is why the correlation looked
contradictory, and why the resolution is a documentation question rather than an experiment.

The volume is not marginal: **1,402 `deferred_tools_delta` and 716 `mcp_instructions_delta`
events fleet-wide, 1,191 of them carrying names** — i.e. actually changing what the model
can see. Cost: **25 of 252 cold boundaries write the context twice** and one writes it three
or more times, totalling 4.45M redundant tokens, 13% of all cold-boundary cache creation.

Jarvis cannot change when a server connects, but it does control **which servers a worker
carries** — see the next section. `bl-7674c1f9` is closed by this finding.

## Finding 4 — workers pay for MCP servers they do not use

Every tool call in every transcript, split by kind:

| | calls | share |
|---|---:|---:|
| all tool calls | 13,061 | |
| **MCP tool calls** | **287** | **2.2%** |
| transcripts using any MCP tool | 42 of 1,075 | 3.9% |

By server: `serena` 277, `claude-in-chrome` 6, `context7` 3, `Google Drive` 1. Serena is
96% of all MCP use, and it is the one the project's own `CLAUDE.md` instructs workers to
prefer for code navigation — it earns its place. The rest amount to **ten calls in 1,075
sessions**, while every server in the set contributes tool schemas and an instructions
block to the front of every prompt, and every server that connects mid-turn re-writes the
whole conversation (Finding 3).

**Not shipped, proposed.** Jarvis does not currently control the worker MCP set at all — a
worker inherits the machine's global configuration. `claude --strict-mcp-config
--mcp-config <file>` would let the catalog declare exactly which servers a worker gets. The
recommendation is Serena only. This is left as a proposal rather than shipped because it
narrows what every worker in the fleet can reach, and the measurement above says the prize
is prompt-prefix cost and boundary stability, not a capability the fleet is using.

## What shipped: a bound on how large a worker's conversation may grow

`claude --autocompact <100k–1M>` sets the effective context window; auto-compact then arms
at a model-table fraction of it (`min(model window, configured)`, verified in the 2.1.227
string table). Left alone on a 1M-token model a worker does not compact until ~800k, which
is why contexts of 250–585k are normal today and why every API call re-reads one.

Jarvis now passes it on **every** worker turn, from `claude_cli._briefing_args` so no launch
path can forget it (the rule `kn-6352bc0f` set), defaulting to **150,000**.

**What that costs, measured against all 1,070 sessions on disk:**

| peak context of a session | sessions | |
|---|---:|---:|
| under 120k (never compacts) | 967 | **90%** |
| 120k – 150k | 42 | 4% |
| 150k – 300k | 50 | 5% |
| over 300k | 11 | 1% |

Median peak is 27,815 tokens; p90 is 119,136. **Nine sessions in ten are untouched.** The
ones the bound bites are the long ones, which are expensive for exactly the reason the bound
exists.

**What it saves:** had every API call been capped at ~120k of context, fleet cache-read
falls from 1.51B to 1.12B tokens — **a 26% reduction, ~$577 of $2,157 at list prices**
(priced per model tier; Opus is 98% of it). Cache read is the single largest line in the
bill (`kn-1485b845`).

That figure is an **upper bound on the saving**, and honestly so: compaction is itself a
model call that pays one full cold write, a compacted worker may re-read files it had
already read, and neither is counted above. The direction is not in doubt; the magnitude is
a ceiling.

### Configuration

```json
"os":       { "defaults": { "autocompact_window": 150000 } },
"projects": [ { "name": "x", "worker": { "autocompact_window": null } } ]
```

Absent inherits the bound; an explicit `null` opts out and takes the model's own window.
Those are deliberately different: silence must not disable a cost control. The catalog
validates the 100k–1M range at boot, because the CLI's own rejection would otherwise
surface as a worker that dies on dispatch.

The window is re-read from the catalog on **every** turn rather than frozen onto the work
order row at dispatch, which is the opposite of how model, effort and permission mode
behave. It is a spend control, not a property of the task: tightening it has to reach the
long-running work orders that are the reason to tighten it.

## Why `git status` is in the system prompt, and why Jarvis cannot take it out

It is an anti-pattern, and Anthropic's own prompt-caching guidance says so in as many
words: *"Keep the system prompt frozen. Don't interpolate 'current date: X', 'mode: Y' into
the system prompt — those sit at the front of the prefix and invalidate everything
downstream."* Claude Code interpolates `cwd`, env info, memory paths and **git status**
into exactly that position. A worker changes its git status by doing its job, so the
worker's own work invalidates its own cache, every turn.

Per the hierarchy above, a system-prompt change spares the tools cache but takes system and
messages with it — the entire conversation. That is the whole of Finding 2.

**Three exits, all closed:**

- `--exclude-dynamic-system-prompt-sections` moves the volatile block into the first user
  message. Tested on `wo-eb9b6337` (`kn-625e79f1`): the surviving prefix grew from 15,461
  to 17,180 tokens and the boundary stayed cold, because the relocated text still changes
  and still sits ahead of the conversation. It is for cross-user prefix sharing, not for
  resuming one conversation.
- `--system-prompt` replaces the default wholesale and would remove the section — along
  with every tool-use instruction Claude Code's own harness depends on. Not a trade worth
  making to save a cache.
- The API has a proper fix — mid-conversation `{"role": "system"}` messages, which append
  after the cached prefix instead of editing the front of it — but it is available to
  *API callers*. Jarvis shells out to `claude`; it does not build the request.

So this is an upstream defect, correctly diagnosed and not actionable from here. The only
lever Jarvis has is the one already taken: carry less context across each boundary, and pay
1.25× rather than 2× for the write.

## Surviving compaction without losing the thread — SHIPPED

Auto-compaction (now on at 150k) summarizes the conversation and discards the rest. The
concern is exactly right: a model-written summary can drop the details a mid-task worker
needs. The proposed shape — write a summary before, inject it back after — is sound, but
one of its two halves cannot be built as described, and the other can be much better than a
summary.

**`PostCompact` cannot inject.** It fires after compaction and receives the summary, but
`hookSpecificOutput.additionalContext` — the only context-injection channel hooks have — is
accepted on `UserPromptExpansion`, `SessionStart`, `Setup`, `SubagentStart`, `PostToolUse`,
`PostToolUseFailure`, `PostToolBatch`, `Stop`, `SubagentStop` and `Notification`, and
**not** on `PostCompact` (verified against the hook output schema in 2.1.227). A design
that injects from `PostCompact` silently does nothing.

**Jarvis should not be writing a summary at all.** Claude Code already summarizes the
conversation; what a compacted worker actually loses is the *contract* — its work-order id,
the finishing protocol, its pending assumptions, its branch and PR, the gate rules. Jarvis
already holds all of that as structured state, so the re-injection can be **deterministic
rather than summarized**: `jarvis wo show <id>` plus the operating contract, rendered from
the record, with no summarization loss and no second model call.

### What was built

Two hooks and a flag file, in `hooks.py`:

1. **`PreCompact`** records a `compacted` event on the work-order timeline — the record
   never learned this before — and writes `.jarvis/compaction/<wo-id>.pending`.
2. **`PostToolUse`** spends the flag on the next tool call and returns the brief as
   `hookSpecificOutput.additionalContext`. This lands *inside* the compacted turn, which
   `UserPromptExpansion` would not.

The brief is rendered from the record: work-order id, title, status, branch, worktree, PR,
the **pending assumptions by name** (so the worker does not record them a second time), the
original description **verbatim**, and `worker_brief.core_contract` — the same operating
contract the opening prompt carries.

**The `PostToolUse` matcher had to widen** from `Write|Edit|NotebookEdit` to unmatched. A
compacted worker's next call is almost always `Bash`; waiting for a file edit would leave
it running blind until it happened to make one. That costs one `stat()` per tool call —
`_post_tool_compaction` checks the flag before opening the database, so the hot path does
not touch SQLite.

**Spent exactly once**, by `unlink()` rather than a read: re-injecting on every tool call
would re-grow the context the compaction just reclaimed. The flag is unlinked *before* the
brief is rendered, so a failure while rendering costs one re-assertion rather than
repeating forever. Nine tests cover it, including a second compaction re-arming; the
exactly-once property, the verbatim description and the timeline event were each
mutation-checked.

Cost is bounded and one-off: a few thousand tokens against the 150k the compaction just
reclaimed.

**Not yet verified against a live compaction.** The hooks are exercised against synthetic
payloads, and `PreCompact`'s payload shape (`trigger`, `custom_instructions`) is taken from
the CLI's own hook table rather than from an observed firing. If it never fires in headless
`-p` mode, the failure is silent — the flag is never written and nothing is injected — so
the first real 150k work order is the thing to watch.

## What was considered and not shipped

**Compact before resuming, when the boundary will be cold** (the work order's proposal).
Feasible — `/compact` is `type:"local"` with `supportsNonInteractive:true` in 2.1.227 and
takes optional custom summarization instructions, so `claude -p --resume <sid> -- "/compact …"`
works. Not shipped, for two reasons. First, "only when the boundary is cold" degenerates to
"on every resume", because Finding 2 shows the boundary is essentially always cold — so the
rule is not the conditional it appears to be. Second, silently compacting a worker mid-task
destroys the detail the work-order record depends on, and it would happen on a message the
user sent for an unrelated reason. `--autocompact` reaches the same context bound through the
CLI's own machinery, at a point the CLI picks, without an extra turn boundary.

One mechanical correction to the proposal: two `claude -p --resume` processes on one session
do not queue. `--resume` refuses a session another process holds (`worker_session.busy`
exists for this), so a compact turn would have to *complete* before the real one was sent —
two boundaries where there is now one.

**Staggering the retries.** The daemon woke both work orders within 25 seconds. Spreading
them would flatten the spike but change no total, and the usage window is global.

## The part Jarvis does not control

`wo-67d4f8b0` chose to resume three subagents. That is a worker's decision, taken inside a
turn, and no daemon setting reaches it. Worth knowing when reading a cost report: a fan-out
work order has N conversations, each paying its own boundary, and `jarvis cost` already
breaks subagents out for this reason.
