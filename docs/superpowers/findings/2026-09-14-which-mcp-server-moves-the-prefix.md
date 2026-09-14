# Which MCP server actually moves the prompt prefix

**wo-8778138e**, answering finding 4 action 3 of
`2026-08-30-where-the-800-dollars-went.md` — "settle the MCP question, since it is 35% of
the remainder" — and issue #164 comment item 4.

**The answer is Serena**, and the mechanism is not the one the question assumed. Five
findings, same Area / Finding / Root cause / Follow-up shape as the document above.
**Nothing here is applied**; every follow-up is a decision for the user, and the one that
narrows what a worker can reach is explicitly out of scope for this order.

Method: transcript arithmetic per `kn-2137076d`, over all **4,326 sessions** indexed by
`usage.index_sessions` on 2026-09-14. Boundaries come from Jarvis's own parsers —
`usage.session_calls` then `inspection.classify_writes` — so nothing here re-implements
the accounting. The addition is the `deferred_tools_delta` / `mcp_instructions_delta`
attachment rows falling in the window between the previous API call and the write, with
each row's names folded to one canonical server. Every figure was produced twice, minutes
apart, and was identical both times.

---

## Finding 1 — Serena is the culprit, and presence is not what proves it

**Area.** `claude_cli._briefing_args` (passes no MCP configuration, so a worker inherits
the user's whole global set); `.serena/project.yml`.

**Finding.** Serena is present at **147 of the 164** prefix-miss boundaries, but so is
almost everything else, and presence is worthless as evidence — Serena is in nearly every
worker session by construction. The test that discriminates is the **sole mover**: a
boundary at which exactly one server moved in the window and no other did.

| | boundaries | tokens re-written |
|---|---:|---:|
| **Serena alone moved** | **28** | **808,889** |
| any other single server alone | 0 | 0 |

Two independent checks agree. The **base rate** — how often a delta event is followed by
a conversation-sized write at all — separates the shapes rather than the servers:

| event shape | events | followed by a ≥20k write | rate |
|---|---:|---:|---:|
| **disconnect** (names removed) | 350 | 189 | **54.0%** |
| append-only (`ToolSearch`) | 245 | 54 | 22.0% |
| connect (names + instructions added) | 3,338 | 551 | 16.5% |

and **disconnect/reconnect cycles per server** — the same server removed and later
re-added inside one session — put Serena first:

```
serena 333   notion 311   (builtin-deferred) 83   context7 77   drive 5   all others <=3
829 cycles total; gap between removal and re-add: median 12s, p90 53s
```

**Root cause.** Serena is the one server Jarvis actively pushes every worker towards
(`CLAUDE.md`: "use Serena's symbol tools rather than grepping"), so it is the one server
that is loaded, used, and — being a local language-server process that restarts under
load — the one that *drops and comes back* most. Every drop and every return is a
position-0 change to the tools array, which invalidates tools, system and messages
together. The census in `kn-f94abf34` (3) measured Serena as 96% of MCP **calls** and
concluded it earns its place; this measures Serena as the leading source of MCP
**churn**, and those are opposite conclusions from the same server.

**Follow-up actions.**

1. **Do nothing about Serena.** It is the only MCP server with a demonstrated payoff.
   · Pro: costs nothing to decide; the churn is ~$5 of re-writes at list (28 boundaries,
   809k tokens) on the clean attribution, and the navigation it buys is real.
   · Con: leaves the largest single named cause of the prefix-miss tax in place, and the
   figure grows with fleet volume rather than staying fixed.
2. **Make Serena stable rather than absent** — a long-lived server the workers attach to
   instead of one started per session.
   · Pro: keeps the capability and removes the churn, which is the only part that costs
   money; nothing about what a worker can reach changes, so this is not a capability
   decision.
   · Con: Serena's own process model is upstream of Jarvis and may not support it; a
   shared server across concurrent workers is a new failure mode (one crash takes out
   every running order), and Jarvis has no supervision for it.
3. **Drop Serena from workers along with everything else**, the maximal reading of
   `kn-f94abf34` (3).
   · Pro: removes the top mover outright; the standing prompt cost falls to zero.
   · Con: directly contradicts `CLAUDE.md`'s navigation instruction and the code map the
   repo commits for exactly this purpose — and finding 4 below shows the standing cost
   being removed is ~3,900 tokens, so this pays for itself only through the churn.

---

## Finding 2 — it is the instructions block at least as often as the tools block, and Serena's is 154 characters

**Area.** The `mcp_instructions_delta` attachment; the invalidation hierarchy in
`kn-f94abf34` (2).

**Finding.** The order asked whether the delta is the tools block, the instructions
block, or a connect/disconnect. Split across the 149 attributed boundaries:

| what moved | boundaries | tokens |
|---|---:|---:|
| both blocks | 98 | 9,817,496 |
| **instructions block only** | **49** | **1,875,187** |
| tools block only | 2 | 186,615 |

An instructions-only delta is not a footnote — it is a third of the attributed boundaries
and 1.9M tokens. And it is **visibly cheaper in the transcript**, exactly as the published
hierarchy predicts. Three worker sessions, within four minutes of each other, each
re-added Serena's instructions block and nothing else:

```
9be366ce  15:34:01  write   3,935  read 17,560
          15:34:05  mcp_instructions_delta  + plugin:serena:serena   (154 chars)
          15:34:10  write  28,564  read 56,261   <- messages re-written, tools cache HELD
```

`read` climbing to 56,261 is the signature: the tools array was untouched, so its cache
survived; the system prompt changed, so system and messages did not. Compare a *tools*
delta in the same corpus:

```
2c976f26  06:19:16  write     924  read 100,359
          06:19:42  deferred_tools_delta + mcp_instructions_delta  - plugin:serena:serena
          06:19:47  write  86,982  read  15,862   <- read falls to the static head
```

A 154-character instructions block therefore costs 28,564 tokens to re-add, and the
payload has nothing to do with the price.

**Root cause.** Claude Code emits the two deltas independently, and a server can come back
with its instructions before (or without) its tools. Both land ahead of the conversation
in the rendered prompt, so both invalidate it; they differ only in whether the tools cache
also dies. Nothing in Jarvis reads either row, so the distinction has never been visible.

**Follow-up actions.**

1. **Label a `prefix-miss` with the server that moved, in `jarvis inspect`** —
   recommended. `inspection.read_transcript` already walks these files; the attachment
   rows sit beside the calls it already reads.
   · Pro: turns "a defect" into "Serena dropped at 15:34:05" on the surface that already
   exists, at no model cost and no new storage — the same read-only arithmetic
   `inspection` is built from. Makes finding 4 action 2 of the previous document checkable
   per server rather than in aggregate.
   · Con: a delta in the window is evidence, not proof (`kn-f94abf34` (2): `ToolSearch`
   emits an identical row and *preserves* the cache), so the label must name the shape —
   connect / disconnect / append — or it will confidently blame the one shape that is
   innocent 78% of the time.
2. **Do nothing.** The distinction is real but the instructions-only case is 14% of the
   prefix-miss tax by volume.
   · Pro: no code.
   · Con: the next person to read `kn-f94abf34` will re-derive it, as this order did.

---

## Finding 3 — the 61% "unexplained" is mostly the boundary definition, not a second cause

**Area.** `usage._usage_of` vs `inspection.classify_writes` — two boundary definitions
that have never been stated side by side.

**Finding.** Finding 4 reported 39% of prefix boundaries attributable and left 61%
unexplained. **That 61% is largely an artefact of which boundaries were counted.**

| definition | prefix boundaries | attributed | by count | by volume |
|---|---:|---:|---:|---:|
| `usage._usage_of` (what finding 4 counted) | 356 | 139 | **39.0%** | 30.8% |
| `inspection.classify_writes`, floor 20,000 | 164 | 149 | **90.9%** | 90.0% |

The 39.0% reproduces finding 4's headline to the decimal over the whole corpus, which is
what says the window in the method above matches the one it used.

The two definitions do not measure the same event. `usage._usage_of` calls a boundary "the
cache read went backwards", with **no write-size floor** — deliberately, so it needs no
magic number. `inspection.classify_writes` counts only writes at or over
`inspect.report_write_floor`, on the reasoning its own docstring gives: inside a turn,
every call writes the few thousand tokens it just added, and only a write large enough to
*be* a re-send of the conversation is a defect at all. The 192 boundaries in the gap
between the two are mostly that — small drops that were the cache working.

**What is still genuinely unexplained is 15 boundaries and 1,320,636 tokens** — 9.1% by
count, 10.0% by volume, under the stricter definition. No MCP delta of any kind sits
between the previous call and those writes. This order did not establish what moved their
prefixes, and the remaining candidates (`git status` in the dynamic system-prompt section
per `kn-f94abf34` (4), a model switch, a CLI upgrade mid-session) were not separated.

For scale, at Opus list with the 5-minute write rate the fleet now buys:

| | tokens | list |
|---|---:|---:|
| attributed to a named MCP server | 11,879,298 | $74.25 |
| unattributed | 1,320,636 | $8.25 |

**Root cause.** Two correct definitions of the same word, each right for its own surface,
and a finding that quoted one while implying the other. Finding 1 of the previous document
is about exactly this failure — a number that is real and an explanation that is not what
it was taken for — and this is the same mistake one layer down.

**Follow-up actions.**

1. **Say which definition a prefix figure uses, wherever one is printed.** `jarvis cost`
   and `jarvis inspect` both report prefix boundaries and they do not agree.
   · Pro: a documentation and labelling change; no arithmetic moves.
   · Con: draws attention to a discrepancy that is not a bug, which invites someone to
   "fix" it by unifying them — which would cost `usage` its magic-number-free property.
2. **Investigate the residual 15 boundaries.** They are concentrated: 119 sessions carry
   a prefix-miss at all, the top 10 carry 41.1% of the tokens, and one session carries 14
   misses / 1.92M tokens on its own.
   · Pro: a small, bounded corpus; one session would probably settle it.
   · Con: 10% of an 90%-explained tax, and the likeliest answer (`git status` in the
   system prompt) is already documented with **no exit** — `kn-f94abf34` (4) closed all
   three.

---

## Finding 4 — the standing cost is ~3,900 tokens; what costs money is the position, not the payload

**Area.** The premise shared by `kn-f94abf34` (3) and finding 4 action 3 — that "every
server contributes schemas and instructions to the front of every prompt".

**Finding.** **The schemas are not there.** MCP tools reach a Claude Code prompt as
*deferred* tools: the model is given a bare name list and fetches the JSON schema on
demand via `ToolSearch`. In 108,470 tool entries across the corpus, `addedLines` is
byte-identical to `addedNames` **108,325 times (99.87%)** — the "line" rendered into the
prompt *is* the name.

Everything all 23 servers put in the prefix, added up:

| | |
|---|---:|
| tools advertised | 276 |
| chars of name list | 11,501 |
| chars of instructions blocks | 4,171 |
| **≈ tokens** | **~3,900** |

The two largest contributors are Notion (46 tools, 2,119 chars) and Serena (30 tools,
1,307 chars). Cutting the fleet to Serena only would save roughly **2,700 tokens per
prompt** of standing cost. Against a median worker context of tens of thousands, that is
noise.

**Root cause.** The mental model came from the API, where a tool definition is its full
schema. Claude Code defers them precisely to keep the prefix small — and having done so,
the residual cost is not what the servers weigh but **where they sit**: a name list at
position 0 that can change at any moment. Twenty-three servers is twenty-three things
that can move the first token of the prompt, and the price of each move is the whole
conversation behind it.

**Follow-up actions.**

1. **Re-file `--strict-mcp-config --mcp-config` on the churn argument, not the size one.**
   `kn-f94abf34` (3) justifies it with "every server contributes schemas"; that premise is
   wrong and the proposal survives it anyway — 22 fewer things that can move.
   · Pro: the proposal is already written and its cost is now honestly stated; Gmail,
   Google Calendar, Crypto.com, PubMed, Mermaid Chart and WordPress move as one 49-event
   block that precedes a big write **83.7%** of the time — the worst rate of any cohort
   measured — and `kn-f94abf34` (3)'s census recorded **zero** calls to any of them in
   13,061 tool calls.
   · Con: **it does not remove the top mover.** Serena is the sole mover in all 28 clean
   attributions, and a Serena-only fleet keeps every one of them. This buys the tail, not
   the head — and it is a fleet capability decision, which this order is told to leave
   to the user and to the sibling `/config` order.
2. **Retract or amend `kn-f94abf34` (3)'s size claim.** `jarvis learn retract` exists for
   a superseded entry.
   · Pro: the entry is read by every worker that searches this area, and its arithmetic
   premise is measurably false.
   · Con: clauses (0), (1), (2), (4) and (5) of that entry are correct and load-bearing;
   retracting the whole thing to fix one clause loses far more than it fixes. An amending
   entry that links back is the smaller move.

---

## Finding 5 — the transcript records that a server dropped, never why

**Area.** What this investigation could not establish.

**Finding.** 829 disconnect/reconnect cycles, median 12 seconds apart, and **no reason is
recorded for any of them**. Scanning the rows adjacent to every disconnect for
`error`, `timeout`, `disconnect`, `failed`, `closed`, `econnreset`, `sigterm` found **5
hits in total**, all `timeout`. The delta rows carry names and nothing else.

There is a shape in the timing worth naming. The three instructions-only examples in
finding 2 are **three different worker sessions** re-adding Serena within four minutes of
each other on 2026-08-24 — consistent with one Serena process restarting underneath every
worker attached to it, and not with per-session flakiness. This session reproduced the
same thing live: Serena and Notion both finished connecting mid-turn during this order
and during its parent, `wo-9722bb7b`.

**Root cause.** MCP transport health is Claude Code's business and it logs none of it to
the transcript. Jarvis reads only what the transcript holds, so the cause of the single
largest named contributor to its prefix-miss tax is, today, outside anything it can see.

**Follow-up actions.**

1. **Count the churn even though the cause is invisible** — a per-server
   disconnect-rate line, as a `jarvis doctor` post-condition. This is finding 4 action 2
   of the previous document made specific.
   · Pro: the count is exactly what changed when Serena got worse, and it needs no reason
   to be actionable — a rate that doubles after a CLI upgrade is the signal.
   · Con: a threshold that will not cry wolf needs a baseline nobody has yet, and the
   metric moves for reasons outside the fleet (a laptop under load).
2. **Look for the cause outside the transcript** — Claude Code's own MCP logs.
   · Pro: would turn finding 1 from "Serena churns" into "Serena churns *because*", which
   is the difference between a workaround and a fix.
   · Con: an unbounded search in files Jarvis does not own, whose format is undocumented
   and unstable; this order did not attempt it.

---

## What this did not settle

Stated plainly, because finding 1 of the previous document is about an investigation that
explained part of a thing and implied it had explained all of it:

- **10.0% of the prefix-miss tax by volume (15 boundaries, 1.32M tokens) has no MCP delta
  anywhere near it** and remains unexplained. See finding 3 action 2.
- **Why any server disconnects** is not in the transcripts. See finding 5.
- **Whether removing a server would actually help** is a prediction, not a measurement.
  Every number here is observational; nothing was A/B'd. The two controlled runs in
  `2026-08-10-resume-cost-and-the-cache.md` are the only experiment in this area and they
  stayed *warm* while carrying the whole global server set — which is precisely why
  "server present" was never the right variable and "server moved" is.
- **Cohort.** 156 of 164 prefix-miss boundaries and 12.4M of 13.2M tokens are in worker
  sessions, so this is the fleet's bill and not the user's own. That was checked, not
  assumed.
