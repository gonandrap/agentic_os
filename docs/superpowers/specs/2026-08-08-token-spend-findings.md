# Where the tokens go — a measurement of Jarvis OS's own spend

**Work order:** wo-eb9b6337. **Question asked:** why did feature order `fo-e353491c`
exhaust a Max 5x subscription without finishing?

**Answer in one line:** it did not, on its own — it cost about 6% of what the project
has spent — but it exposed a structural tax that costs the fleet roughly an eighth of
every token it spends, and a process failure that made this particular feature order
pay it six times over.

**Note on §2, added after the first draft:** that section originally named MCP tool-list
churn as the likely cause and recommended pinning the worker MCP config. A controlled
A/B **disproved it**, and the section now records what has been ruled out instead. The
tax and every figure attached to it are unchanged; only the diagnosis moved. The fix
that did ship (§6) targets turn *count*, which is Jarvis's own and needs no diagnosis.

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

And it is worse than one re-write per boundary: the context is written **twice**, on two
consecutive calls. In this work order's own session, calls 119 and 120 wrote 457,112 and
then 458,948 tokens for a 457k context. Anyone estimating a saving should budget ~2×
context per boundary, not 1×.

The most direct evidence available is this work order itself — a real worker on the real
dispatch path: **1,619,659 excess tokens in three turns, ~$9.31.**

The tax follows a simple shape:

```
re-write tax  ≈  (turns − 1)  ×  context size at each boundary  ×  1.25
```

which is why it falls hardest on exactly the work Jarvis most wants to do well: long,
many-turn orders that have accumulated a large context.

### It is not cache TTL expiry

Measured over **all 153 real worker turn boundaries** in the fleet's transcripts:
**124 went cold, 29 stayed warm**, and the gap does not separate them.

| | Gap range | Median gap |
|---|---|---|
| Cold boundaries (124) | **10 s** – 12 days | 52 s |
| Warm boundaries (29) | 6 s – **3,233 s** | 89 s |

The coldest boundary in the fleet had a **ten-second** gap. A warm one had a
**fifty-four-minute** gap. Any TTL story has to explain both at once, and none does.

### It is not the ambient MCP configuration either — the first hypothesis was wrong

The transcripts do carry `deferred_tools_delta` and `mcp_instructions_delta` records
showing the entire MCP tool set removed and re-added at every turn boundary, and a
tool-list change does invalidate the whole prefix. That made MCP churn the obvious
suspect, and `claude_cli._briefing_args` passes **no MCP configuration at all**, so
every worker inherits the user's whole global set — Notion, Gmail, Google Calendar,
Google Drive, Crypto.com, PubMed, Mermaid Chart, WordPress, context7 and serena,
several of them unauthenticated and so connecting, failing and churning.

**It is not sufficient to cause the tax.** Two controlled A/B runs of a plain
`claude -p --session-id` / `--resume` pair, inheriting that entire global MCP set,
stayed **warm** across the turn boundary:

| Turn-2 first call | `cache_read` | `cache_write` |
|---|---|---|
| Turn 1 = 2 tool calls | 26,381 | 641 |
| Turn 1 = 26 tool calls | 28,819 | 641 |

The second run exists to kill a second hypothesis at the same time: the 20-block
cache-lookback window. Twenty-six tool calls in turn 1 puts the previous cache entry
well beyond it, and the boundary was still warm.

So pinning `--mcp-config` / `--strict-mcp-config` would have bought a fleet-wide
behaviour change — including the risk of silently stripping serena from every worker —
and no saving. It was proposed in the first draft of this document and is retracted
here. **This is what shipping the measurement first was for**: the wrong fix cost four
cheap `claude` calls to disprove instead of a release and a regression nobody could
see.

### What is still unexplained

On a cold boundary `cache_read` lands on one of a **small set of fixed values** —
15,277 / 15,465 / 21,704 / 21,967 — never something arbitrary. The prefix therefore
matches up to a constant point and then breaks. That point is the end of Claude Code's
own system prompt, so the first thing that differs is whatever Jarvis appends after it.

A plain resume does not break there. A worker turn does. The difference between them is
entirely the briefing: `--append-system-prompt`, `--settings`, `--add-dir`, `-n`,
`--model`, `--effort`, `--permission-mode`. Two of those are rebuilt from scratch on
every single turn — `dispatch._write_worker_settings` rewrites the settings file, and
`bootstrap.install_agent_assets` re-materialises the asset trees — so a byte difference
in either would do it, and both are Jarvis's own code.

Finishing this is a bisect, not a guess: add one briefing flag at a time to a two-turn
scratch session and watch `cache_read` on turn 2's first call. Filed as a backlog item
with that procedure written out. It is deliberately not attempted here — the honest
state of the evidence is "large, real, cause narrowed to seven flags", and the next
step is a handful of cheap calls rather than another hypothesis.

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

## 6. The fix that shipped: one turn per delivery, not one per message

The tax is `(turns − 1) × context × ~2`. Its *cause* is still open, but the **turn
count** is entirely Jarvis's own, and `Daemon.deliver_messages` was spending it
needlessly: it iterated `store.queued_messages()` and delivered **one message per
turn**. Three quick comments from the user meant three turn boundaries — three full
re-writes of a context that may be 400k tokens — for content the worker is better off
reading together anyway, since it can act on the whole of what the user said instead of
starting down the first message's path and being interrupted twice.

Everything queued for one work order now goes out as a single turn, joined with a blank
line and nothing else. Per work order, not globally: two work orders' messages are
independent conversations and must stay separate turns, which is its own test.

Two details are load-bearing. **Every** message in the coalesced turn is marked
`delivered` — one left `queued` would be re-sent next tick, so the worker would read it
twice and pay a second boundary for the privilege. And the joined text carries no
framing: no count, no "message 2 of 3", nothing the worker could mistake for an
instruction the user did not write.

Saving: one boundary per extra message, so on a 400k-token conversation roughly 800k
tokens (~$4.60) for every message that would previously have arrived on its own.

## 7. Recommendations, in order of expected saving

1. **Bisect the briefing** to find what actually breaks the prefix — seven flags, a
   two-turn scratch session, a handful of cheap calls. Upper bound if it is fixable:
   ~12% of fleet spend. Filed with the procedure written out. Do *not* re-try MCP
   pinning; §2 disproves it.
2. **Keep worker turn counts down where the context is large.** The tax is linear in
   turns and in context at once, and each boundary costs ~2× context. This is the lever
   that works whether or not the cause is ever found, and the message coalescing above
   is the first instance of pulling it. `jarvis wo ask` round trips are the other big
   source — batch questions into one ask rather than three.
3. **Route feature-order scope questions to the user, not to Neo**, or checkpoint the
   plan's shape before the full document is written. On this feature order alone that
   was ~$5–8 and four turns.
4. **Watch subagent fan-out on planners specifically.** A third of this planner's cost,
   for analysis the parent then had to read anyway.
