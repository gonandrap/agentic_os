# Where the tokens go — a measurement of Jarvis OS's own spend

**Work order:** wo-eb9b6337. **Question asked:** why did feature order `fo-e353491c`
exhaust a Max 5x subscription without finishing?

**Answer in one line:** it did not, on its own — it cost about 6% of what the project
has spent — but it exposed a structural tax that costs the fleet roughly an eighth of
every token it spends, and a process failure that made this particular feature order
pay it six times over.

**Note on §2, revised twice.** The first draft blamed MCP tool-list churn and
recommended pinning the worker MCP config; a controlled A/B disproved it. The second
draft said the cause was unknown and filed a bisect. The cause is now **found and
confirmed**: `git status` rides in Claude Code's system prompt, and a worker changes it
on every turn by doing its job. Every figure in this document held across both
revisions — only the diagnosis moved, which is exactly what shipping the measurement
first bought.

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

### The actual cause: `git status` is in the system prompt

Claude Code's system prompt carries a **dynamic per-machine section** — its own
`--exclude-dynamic-system-prompt-sections` flag names the contents: "cwd, env info,
memory paths, **git status**". A worker's whole job is to change files in its worktree,
so its `git status` differs on the next turn, the system prompt differs with it, and
every byte of conversation after that point has to be re-sent.

Confirmed in a clean room at **27k** of context — no Jarvis code involved, three arms
differing in one variable:

| Arm | git repo? | turn 1 edited a file? | turn-2 first call |
|---|---|---|---|
| plain directory | no | no | **warm** — read 26,381 |
| git repo | yes | no | **warm** — read 26,480 |
| git repo | yes | **yes** | **cold** — read 15,461, re-wrote 12,352 |

The third arm's `cache_read` collapses to **15,461** — precisely the value its own
*first* call reported, i.e. the static system prompt and nothing after it. That is the
same signature as the 124 cold boundaries in production, whose reads cluster on
15,277 / 15,465 / 21,704 / 21,967: those are static system prompts of differing sizes,
because each project's `CLAUDE.md` is a different length.

Everything the earlier hypotheses failed to explain now follows. TTL was irrelevant
because the variable is *what the worker did*, not *when* the next turn started — a
10-second boundary after an edit is cold, a 54-minute boundary after a read-only turn is
warm. And every warm reproduction earlier in this investigation was warm for the same
uninteresting reason: those scratch directories were not git repositories, or the turn
changed nothing in them.

**`--exclude-dynamic-system-prompt-sections` does not fix it.** Tested: it moves the
section into the first user message, which grows the surviving prefix from 15,461 to
17,180 — and the boundary is still cold, because the relocated section still changes and
still sits ahead of the entire conversation. The flag is for cross-user prompt-cache
reuse, not for resuming one conversation.

**So there is no Jarvis-side fix for the cause**, and this is worth stating plainly
rather than leaving as an open task. Jarvis cannot stop a worker from editing files, and
it cannot make the CLI hold `git status` out of the cached prefix. The tax is a property
of running a file-editing agent across multiple processes with today's Claude Code. What
Jarvis *can* control is how many boundaries it pays for — see §6.

The repro is four `claude` calls and belongs upstream: a session resumed after any
file edit re-sends its whole context. On a 400k-token conversation that is ~$4.60 per
boundary at Opus list prices.

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

1. **Turn count is the only lever on the tax, so spend turns deliberately.** The cause
   is upstream and unfixable here (§2), but the tax is `(turns − 1) × context × ~2`, and
   Jarvis owns the turn count. Message coalescing (§6) is the first instance. The other
   large source is `jarvis wo ask`: every round trip is a boundary, so a worker that asks
   three questions separately pays three of them. Batch questions into one ask — the
   worker contract should say so, and now has a number behind it.
2. **Report it upstream.** A resumed session re-sends its whole context after any file
   edit. Four `claude` calls reproduce it; the arms are in §2.
3. **Put the planner's read-only seats on a cheaper model.** See §8 — a
   measured 40% cut to a third of a planner's bill, at a quality cost the user should
   weigh rather than have chosen for them.
4. **Route feature-order scope questions to the user, not to Neo**, or checkpoint the
   plan's shape before the full document is written. On this feature order alone that
   was ~$5–8 and four turns.

## 8. What is actionable about the subagents (hotspot 3)

Measured on the planner's three seat calls: **6,010,528 billed input tokens against
77,581 output — a 77:1 read-to-write ratio — for $6.49**, a third of the planner's bill.
That ratio is the finding. These seats are *defined* to produce no code (`jarvis-architect`
and `jarvis-test-lead` hold no Write, no Edit, no Bash — the asset files say that is the
point of the seat, not an oversight). They read, and they hand back an opinion.

Two things follow, and only one of them is a code change.

**They run on the dearest model, by omission.** Neither seat's frontmatter carries a
`model:` key, so both inherit the planner's — Opus 5, at $5/$25 per MTok. For work that
is 77:1 reading, the input price is essentially the whole bill: on Sonnet 5 ($3/$15) the
same three calls cost ~$3.9 instead of $6.49, a **40% cut to a third of a planner's
spend**, and on Haiku 4.5 ~$1.3. Adding `model: sonnet` to the two asset files is a
one-line change per seat.

It is deliberately **not** made here. The architect's analysis is what produced the scope
question that this planner asked before writing anything — the one thing in the whole
episode that worked as designed — and trading planning quality for 11% of a planner's
cost is the user's call, not a worker's. It is filed with these numbers attached.

**The seats are not wasting reads.** Worth recording because it was the obvious
suspicion and it is wrong: both seat definitions already carry the full "Serena first,
`list_memories`/`read_memory` before anything else, do not grep for symbols" instruction,
and their tool grants are enumerated read-only symbol tools. They are not re-deriving
the architecture from scratch. The 77:1 ratio is what careful code reading costs, not a
defect to fix — which is why the lever is the price per token and not the number of them.

`jarvis cost` reports the subagent share as its own line so this choice is visible at all,
which it was not before.
