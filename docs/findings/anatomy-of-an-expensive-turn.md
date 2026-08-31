# Anatomy of an expensive turn

**Subject:** `wo-5a6b2d6d` — the planner for feature order `fo-306b8f48`, a design-only
work order that spent **$19.28 / 19.0M tokens** across three turns of ~14 minutes each.

**Question asked:** how does an agent that only reads code and writes a design document
take fourteen minutes per turn, and are we missing parallelism?

**Answer, up front:** parallelism is not the problem — the planner overlapped its subagent
correctly. Three things cost the time, and only one of them is work:

| | share of wall clock |
|---|---|
| model generating tokens (thinking + writing) | **66%** (1252s) |
| blocked waiting on a subagent, zero API calls in flight | **31%** (576s, in exactly 2 calls) |
| actually executing tools | **3%** (58s, across 79 calls) |

Reading the entire codebase cost 45.6 seconds. The expensive part of this work order was
the part where nothing was being computed.

---

## 1. Method — how to take a turn apart

This is the reproducible procedure, and it is what the OS should eventually do for itself.
Nothing here needs a paid call; every input is already on disk.

### Step 1 — get the money view and the session id

```bash
jarvis cost <wo-id>            # human table: per turn, per agent, per token class
jarvis cost <wo-id> --json     # same numbers, machine-readable; carries `session_id`
```

Read three things off it before going further:

- **`context peak` per turn** — how large the conversation got.
- **the `re-write tax` line** — tokens re-sent to the cache across turn boundaries.
- **the cache-write TTL split** — 5-minute writes vs one-hour writes.

### Step 2 — find the transcript

`jarvis cost --json` gives `session_id`. The Claude Code transcript is:

```
~/.claude/projects/<slugified-cwd>/<session_id>.jsonl
```

```bash
find ~/.claude/projects -name "<session_id>.jsonl"
```

One JSON object per line. The records that matter are `type: "assistant"` (carries
`message.usage` and `message.content[]` with `tool_use` blocks) and `type: "user"`
(carries `tool_result` blocks). `isSidechain: true` marks subagent records; a spawned
`Agent` usually writes its own file instead.

### Step 3 — find the turn boundaries

```python
[r for r in rows if r["type"] == "user" and r.get("promptSource") == "sdk"]
```

These are the prompts Jarvis injected: the dispatch prompt, each `wo send`, each Neo
answer, each `<task-notification>`. **Read what each one says** — it tells you *why* the
turn restarted, which is usually the finding.

### Step 4 — partition the wall clock

Three quantities, from timestamps alone:

- **tool execution** — `tool_use` timestamp to its matching `tool_result` timestamp,
  bucketed by tool name. This is the only bucket that is genuinely I/O.
- **model latency + generation** — everything not inside a tool call.
- **blocking joins** — tool execution where the tool is `TaskOutput`. Called out
  separately because it is neither I/O nor computation: it is the lead agent idling with
  its whole context live.

### Step 5 — line every large cache write up against a timestamp

For each `assistant` record read `message.usage`:
`cache_creation_input_tokens`, `cache_read_input_tokens`, and the
`cache_creation.ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens` split.

Flag every write over ~20k and print the gap since the previous call. **A large write with
a small preceding gap is a bug; a large write after a >5-minute gap is the TTL.** Telling
those two apart is the entire point of this step, and they have completely different fixes.

---

## 2. What this turn actually did

Timeline, offsets from the dispatch prompt (2026-08-28 00:57:16Z):

```
 0.00m   dispatch prompt lands            -> 45,169 tokens written, 0 read   [COLD START]
 0.05m   reads the codebase: 55 Bash calls, 45.6s of execution in total
 1.43m   spawns `jarvis-architect` subagent (background)
 3.45m   "Drafting the design doc while the architect consult runs."   <- correct overlap
 5.28m   writes the design doc (110s + 82s of generation)
 7.67m   TaskOutput  -> BLOCKED 136s waiting for the architect
10.46m   "Architect came back with two findings that change the plan. Verifying"
12.95m   rewrites the design doc (112s of generation)
13.85m   files a defect, asks Neo a scope question
14.37m   "Question 170 is with Neo; ending my turn to let it answer."
---- turn 1 ends: 865s ----
14.46m   Neo's answer + a task-notification arrive, 12.4s later
14.49m                                    -> 157,098 written, 15,862 read   [RE-WRITE]
15.09m   spawns `jarvis-test-lead` subagent (background)
16.10m   TaskOutput  -> BLOCKED 440s waiting for the test lead
23.53m                                    -> 193,139 written, 0 read        [TTL EXPIRY]
---- turn 2 ends: 885s ----
29.22m   turn 3: 136s, no subagents, no blocking
```

Per turn:

| turn | wall | blocked on a subagent | tool execution | model generating |
|---|---|---|---|---|
| 1 | 865s | 136s (16%) | 26s | 703s |
| 2 | 885s | **440s (50%)** | 23s | 422s |
| 3 | 136s | 0s | 9s | 127s |

**Half of turn 2 was the lead agent doing nothing**, holding a 193k context.

---

## 3. Findings

### 3.1 A resumed turn re-writes the context even when the cache is 12 seconds old

At 14.49m the agent re-wrote **157,098 tokens and read only 15,862** — about the size of
the system prompt. The gap since the previous call was **12.4 seconds**. The five-minute
TTL was nowhere near expiry.

So the largest component of the fleet's `$193.74` re-write tax **is not the TTL**. A
resumed turn is not landing on the cache prefix the previous turn left warm; the match
breaks immediately after the system prompt and everything downstream is re-sent at 1.25x
instead of read at 0.1x. This is the highest-value item on this page: it is a defect, it
is measurable, and it costs on every turn boundary in the fleet — 158 of them.

### 3.2 The one-hour cache was never once requested

Across all 1,797,566 cache-write tokens in this session: **1h = 0, 5m = 1,797,566.**
`jarvis cost` states the policy plainly — *"every path Jarvis launches now forces the
5-minute write."* Against 14-minute turns and 7-minute blocking joins that default is
inverted. Note the interaction with 3.1: fixing the TTL alone would not have saved the
157k re-write, because that one was 12 seconds old. **Fix 3.1 first; it is the bigger
number, and the TTL change is worth less until the prefix actually matches.**

### 3.3 There is no cheap way to wait, and the agent tried both ways

The planner needed to wait twice, and used a different mechanism each time:

- **Block in-process** (`TaskOutput`, 440s) → the 5-minute TTL expires mid-wait → the next
  call rebuilds 193k from scratch with **zero** cache reads.
- **End the turn and be resumed** (the Neo question) → resume re-writes 157k (3.1).

Both waits cost a full context rebuild. This is the structural finding behind the other
two: **Jarvis's expensive moments are the moments it is idle**, and every idle path it
offers today is billed at the cache-write rate.

### 3.4 Parallelism is not missing — the join is too early

The hypothesis under test was that the design process lacks parallelism. The transcript
says otherwise. The planner spawned the architect at 1.43m and explicitly kept working
(*"Drafting the design doc while the architect consult runs"*), overlapping ~6 minutes of
document generation against the subagent. That is the pattern working as intended, and the
join cost only 136s.

Turn 2 is where it fell over: it spawned the test lead at 15.09m, found roughly 60 seconds
of independent work, then joined and blocked for 440s. Adding more parallel subagents would
not have helped — the lead had nothing left to do that did not depend on the answer.

The lever is not more fan-out. It is (a) not joining until there is genuinely no
independent work left, and (b) making the wait itself cheap, which is 3.1 and 3.2.

### 3.5 The cold start is 45k before the agent reads anything

The first API call wrote 45,169 tokens with nothing read: system prompt, tool schemas,
`CLAUDE.md`, skills, and Jarvis's dispatch prompt. That is the floor every later call in
the session re-reads, and it is paid once per worker — with six children on this feature
order, ~270k of cache write before any of them opens a file. Worth decomposing into what
Jarvis controls versus what Claude Code contributes before assuming it is reducible.

### 3.6 Tool execution is free; generation is not

55 Bash calls averaged **0.8 seconds**. Reading the whole codebase cost 45.6 seconds of a
31-minute session. Meanwhile single generation spans ran 110s, 112s, 86s — writing and
rewriting the design document. **Optimising how the agent searches the codebase would save
nothing.** The cost is in what the model writes and how many times it rewrites it, and the
design doc here was written twice because the architect's findings arrived after the first
draft — 3.4 again: the join landed too late to prevent the rework, and too early to be free.

---

## 4. What the OS should be able to tell you by itself

Everything above came from two files and about forty lines of Python. None of it needed a
paid call, and all of it was available while the work order was still running. Jarvis
already reads these transcripts for `jarvis cost`; it stops at the money and never asks
where the time went.

A self-inspection surface should report, for any running or finished unit:

1. the wall-clock partition — generating / blocked / executing tools;
2. every blocking join over a threshold, with what was being waited on;
3. every cache write over a threshold, **labelled `TTL-expiry` or `prefix-miss`** according
   to the gap that preceded it — the distinction in 3.1 vs 3.2;
4. the turn boundaries with the reason each one happened, quoted from the injected prompt;
5. the tool-execution profile, to confirm (or refute) that I/O is negligible.

And it should be able to raise a live one: a turn past N minutes, or a blocking join past
the cache TTL, is a thing the user would want to know about *while it is still burning*,
not on the bill afterwards.

---

*Method and figures: 2026-08-30. Subject session `ec8236c7-b418-4f09-80f0-1edea61f099f`,
recorded 2026-08-28. Jarvis OS 0.7.3.*
