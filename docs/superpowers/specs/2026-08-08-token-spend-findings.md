# Where the tokens go — a measurement of Jarvis OS's own spend

**Work order:** wo-eb9b6337. **Question asked:** why did feature order `fo-e353491c`
exhaust a Max 5x subscription without finishing?

**Answer in one line:** it did not, on its own — it cost about 4% of what the project
has spent — but it exposed a structural tax that costs the fleet roughly an eighth of
every token it spends, and a process failure that made this particular feature order
pay it six times over.

Everything below is measured from Claude Code's own session transcripts under
`~/.claude/projects`. `jarvis cost` (shipped with this document) reproduces every
figure. Snapshot taken 2026-08-08; `fo-e353491c` was still running at the time, so its
numbers are a floor, not a total.

---

## 1. The bill

`fo-e353491c` at the time of measurement, having produced **zero** work orders:

| Component | List cost |
|---|---|
| Planner `wo-cd73c537`, main session (51 messages, 6 turns) | $17.43 |
| Its three subagents (`jarvis-architect` x2, `jarvis-test-lead`) | $6.49 |
| Neo's plan-review and answer calls (4 headless calls) | $0.93 |
| **Total** | **~$24.35** |

For scale, the whole `jarvis_os` project across 48 measurable work orders comes to
**~$410**. So this feature order is ~6% of the project's lifetime spend, and its
planner alone cost more than all but four work orders ever run — while producing a
plan document rather than any code. The seven children it planned would add an
estimated $35–125 on top.

The user's instinct is right. A feature order whose *planning phase* costs as much as
a large work order does not scale to a subscription.

### Prices are a proxy, not a bill

Every dollar figure here is Anthropic list price ($5/$25 per MTok for Opus 5, cache
writes at 1.25x, cache reads at 0.1x). The user is on a subscription, which meters
differently and which this analysis has no visibility into. The figures are a common
unit that lets a cache-read token be compared against an output token — nothing more.
Token counts are the primary evidence throughout.

---

## 2. Hotspot one: the re-write tax (structural, systemic, ~12% of everything)

### What was measured

Every worker turn is a separate `claude -p --resume` process (`claude_cli.turn_args`).
On the first API call of each turn after the first, `cache_read` collapses to exactly
15,465 tokens — the identical value the session's *very first* call reported — and the
whole accumulated conversation is re-sent as a cache **write**, at 1.25x, rather than
read at 0.1x.

In the planner: 6 turns, 5 such boundaries, **1,273,058 tokens re-sent**, which is 81%
of everything it ever wrote to the cache and ~$7.32 of its $23.92.

Across the whole `jarvis_os` project: **9.0M tokens re-sent across 67 turn boundaries,
~$51.52 — 12.6% of the project's entire spend.**

The tax follows a simple shape:

```
re-write tax  ≈  (turns − 1)  ×  context size at each boundary  ×  1.25
```

which is why it falls hardest on exactly the work Jarvis most wants to do well: long,
many-turn orders that have accumulated a large context.

### It is not cache TTL expiry

The obvious explanation is wrong, and ruling it out is what makes this actionable.
Within the planner's own transcript:

| Gap before the call | Cache read | Outcome |
|---|---|---|
| 644 seconds | 93,248 | **hit** |
| 29 seconds | 15,465 | **missed** |
| 57 seconds | 15,465 | **missed** |
| 133 seconds | 15,465 | **missed** |

A ten-minute gap kept the cache; a twenty-nine-second gap lost it. Time is not the
variable.

### What the variable appears to be

The transcripts carry `deferred_tools_delta` and `mcp_instructions_delta` records. At
every turn boundary the entire MCP tool set is **removed** and then **re-added**:

```
00:15:21  removed: ListMcpResourcesTool, mcp__claude_ai_Notion__*, plugin:serena, plugin:context7 …
00:26:57  added:   (the same set back again)
00:33:21  removed: …
00:34:13  added:   …
```

A change to the tool list invalidates tools, system and messages alike — the whole
prefix. That matches the observed collapse to a fixed 15,465-token remainder, which is
the part of the prompt that renders *before* the tool list.

Jarvis contributes to this by omission: `claude_cli._briefing_args` passes **no MCP
configuration at all**, so every worker inherits the user's entire global set — Notion,
Gmail, Google Calendar, Google Drive, Crypto.com, PubMed, Mermaid Chart, WordPress,
context7 and serena. Several of those are unauthenticated in this environment, so they
connect, fail, and churn. `dispatch.py:317`'s own docstring already notes there is no
`mcpServers` key anywhere in `src/`.

**This is a correlation with a plausible mechanism, not a proven cause.** The MCP
handshake is Claude Code's internal behaviour; Jarvis can observe it and can change
what it hands the CLI, but cannot directly control the ordering. That is precisely why
the recommended next step is to change the config and *measure the tax again* rather
than to assert a fix. See backlog `bl-*` (MCP pinning).

---

## 3. Hotspot two: rework from a reversed ruling (the avoidable one)

The planner wrote `plan.json` three times, ~30k output tokens each, because the plan
was rejected twice. The sequence:

| Turn | Prompt | What it cost |
|---|---|---|
| 1 | dispatch | the initial exploration |
| 2 | *"[Neo] Q1: Ship the 6 children for work-order validation; file the feature-order case as a backlog item"* | the planner builds the entire plan on this ruling |
| 3 | *"sent back by neo. Revise it and resubmit"* | plan rewritten |
| 4 | *"sent back by user. Revise it and resubmit"* | plan rewritten |
| 5 | *"feature-orders … should be in scope"* | **the user reverses Neo's turn-2 ruling** |
| 6 | *"limit is restored, resume"* | the user had hit their cap |

The planner asked the right question at the right moment — before writing anything —
and got a clear answer. That answer was then overturned four turns and two full plan
rewrites later. Every token between turns 2 and 5 was spent building on a premise the
principal did not hold.

This is not a bug in any component. It is the delegation seam working exactly as
designed and still producing waste, because a scope ruling is the one kind of answer
where being wrong invalidates everything downstream of it. Filed as a backlog item
rather than fixed here: the remedy is a process change (scope questions on a feature
order's *ask* routed to the user rather than settled by the delegate, or a plan
checkpoint before the expensive document is written), and that is the user's call.

---

## 4. Hotspot three: subagents (a third of the planner)

Three `Agent()` calls cost **$6.49 of the planner's $23.92**, each re-establishing an
~80k-token context of its own to read code the parent had largely already read. Worth
noting rather than condemning — the architect's analysis is visible in the quality of
the questions the planner then asked — but a third of the bill is a number that should
be seen when choosing to fan out.

`jarvis cost` reports subagent spend as its own line for this reason.

---

## 5. What shipped with this document

`jarvis cost [project | wo-id | fo-id]` — token accounting read back from the
transcripts, because **the OS records none of its own**: there is no usage column
anywhere in the three databases, which is the reason answering this question cost a
work order in the first place.

```
$ jarvis cost fo-e353491c
fo-e353491c — 1 measured

      $ turns  output  re-write  work order
  23.92     6    241k      1.3M  wo-cd73c537  Plan: I want to expand loops within jarvis_o

total ~$23.92 at list prices (14.6M in, 241k out)
  re-write tax  ~$7.32 — 1.3M tokens re-sent across 5 turn boundaries
  subagents     ~$6.49
```

Three definitions in it are load-bearing and threshold-free on purpose:

- **`rewrite_excess` = `sum(cache_write) − max(context)`.** In a perfectly cached
  session every token is written to the cache exactly once, so the total written can
  never exceed the largest context reached. Everything above that line was paid for
  twice. No magic number is involved, which matters because the first version of this
  analysis used a "big write next to a small read" heuristic that happened to agree
  here only because this session's boundaries were unusually stark.
- **A turn boundary is `cache_read` going *backwards*** relative to the previous call,
  not a large write. Counted this way it lands on exactly `turns − 1` for every work
  order measured.
- **A missing transcript reports `found: false`, never zero.** Claude Code prunes
  transcripts on its own schedule; an unmeasurable cost and a zero cost are different
  answers and must not render the same, or a gap in the evidence becomes a claim about
  the spend.

### The trap that nearly made this report wrong

The first hand measurement reported 2.7M cache-write tokens for the planner where the
true figure is 1.58M. Claude Code writes each assistant message to the transcript
**two or three times** — once as each content block arrives, and again as its text
grows — repeating the same `usage` object every time except `output_tokens`, which
climbs. Summing the rows counts input once per copy. `usage._assistant_messages` keeps
one entry per message id and takes the **max** of each field (the first copy reports
`output_tokens: 1`), and `tests/test_usage.py` pins it with the duplicate and a
distinct second message in the same test.

---

## 6. Recommendations, in order of expected saving

1. **Pin a deliberate MCP set for worker turns** and re-measure the tax with
   `jarvis cost`. Upper bound if the churn is the whole cause: ~12% of fleet spend.
   Deliberately *not* done in this work order — serena is load-bearing for workers
   (`dispatch._navigation_briefing` exists to push them to it), and getting this wrong
   silently strips symbol tools from every worker in the fleet. Measure first, then
   change, then prove.
2. **Route feature-order scope questions to the user, not to Neo**, or checkpoint the
   plan's shape before the full document is written. On this feature order alone that
   was ~$5–8 and four turns.
3. **Keep worker turn counts down where the context is large.** The tax is linear in
   turns and in context size at once; a work order that takes eight turns at 250k
   context pays about 2.5M tokens for the privilege of being resumed.
4. **Watch subagent fan-out on planners specifically.** A third of this planner's cost,
   for analysis the parent then had to read anyway.
