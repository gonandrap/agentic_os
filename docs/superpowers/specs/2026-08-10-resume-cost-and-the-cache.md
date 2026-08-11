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

Weighted as input-equivalents (cache-write 1.25×, cache-read 0.1×, output 5×), those two
minutes cost ~1.68M tokens, of which **78% is cache creation**. That is the spike.

It does not stop at two minutes, and after ~3 minutes the larger term flips:

| window | calls | cache-create | cache-read | weighted | dominant term |
|---|---:|---:|---:|---:|---|
| 2 min | 23 | 1,042,376 | 3,142,570 | 1.68M | writes (78%) |
| 5 min | 64 | 1,105,733 | 10,583,197 | 2.78M | writes (50%) |
| 15 min | 146 | 1,184,173 | 26,304,623 | 4.71M | **reads (56%)** |

146 API calls in fifteen minutes, each re-reading a 250–290k context.

## Finding 2 — the wait did not cause the cold cache, and no threshold could have

Two independent corrections to the proposed fix:

**The TTL is one hour, not 4.5 minutes.** Every cache write in both transcripts is
`ephemeral_1h_input_tokens`; the 5-minute field is zero. Across all 1,050 transcripts on
this machine: 60.7M tokens written at 1h TTL against 3.1M at 5m. (5m appears only in usage
overage.) A 4.5-minute rule would throw away a *warm* cache for ~55 minutes of every hour —
and one hour is the longest TTL the API offers, so nothing could have survived a 4h41m wait.

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

That is a correlation across four boundaries, not a controlled result — recorded as the
leading hypothesis, not as fact. What *is* established is the rate and the cost: fleet-wide,
**25 of 252 cold boundaries write the context twice** and one writes it three or more times,
totalling 4.45M redundant tokens, 13% of all cold-boundary cache creation. It is upstream
behaviour; Jarvis cannot fix it, only carry less context into it.

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
