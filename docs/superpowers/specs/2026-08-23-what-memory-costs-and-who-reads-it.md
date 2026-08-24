# What memory costs, and who reads it

**Measured 2026-08-23, wo-a2c2375e**, against a read-only snapshot of live production
state (`~/workspace/production/state/os.db`, 183 live entries). Companion to
`2026-08-09-where-the-tokens-go.md`, which established that prompt bloat is not why
Jarvis is expensive, and to `2026-07-27-knowledge-on-demand-design.md`, which built the
index this measures.

The question that prompted it, from the `/knowledge` page: **each entry has a lot of
text — is that bad for the model, what technology are we using for agent memory, and
what observability do we have over it?** The first two had answers. The third did not,
and that is what this work order shipped.

## 1. Long entries do not cost tokens. They cost RETRIEVAL.

Entry body text never reaches a worker prompt. `CentralStore.knowledge_brief` ships a
bounded INDEX — one truncated headline per entry, `id` included — and the worker fetches
what it wants with `jarvis learn show`. Measured on the live base, for a work order whose
description is empty:

| | chars | ~tokens |
|---|---|---|
| Body text standing in the base | 346,381 | ~86,600 |
| …of it that reaches a dispatch prompt | 0 | 0 |
| The index block | 7,877 | ~1,970 |
| The whole opening prompt | 11,702 | ~2,930 |

So the base could double tomorrow and the prompt would not move. That half of the worry
is settled, and `kn-1485b845` already says not to re-derive it: the fleet's bill is
workers re-reading their own conversations, and the entire knowledge block is a rounding
error against it.

**The cost is somewhere else, and it is real.** The index budget is 4,000 characters of
headline, and a headline is at most 160 characters (`HEADLINE_CHARS`). Both numbers bind:

* **183 of 183 entries reach the index as a first line cut mid-sentence.** The median
  entry's first line is 796 characters — because entries are written as one long
  paragraph opening with a capitalised title clause, the "headline" is a fragment of a
  sentence, not a summary.
* **25 entries are indexed; 158 are overflow** — visible to a worker only as a topic name
  and a count. The 4,000-character budget buys 25 lines precisely because every headline
  runs to the full 160.

The index line is the ONLY thing that decides whether an entry is ever opened. A shorter
first line therefore buys twice: a headline that says what the entry is, and more entries
visible at all under the same budget. That is the actionable form of "each entry has a
lot of text" — not *shorten the entries*, but **make the first line a real headline.**

Second-order costs, for completeness. A fetch is charged at what it returns: median entry
1,474 chars (~370 tokens), largest 6,605 (~1,650). A worker reading five entries spends
~2k tokens — deliberate, aimed, and cheap. And `search_knowledge` is `LIKE` over
`content`, so a longer entry is a bigger target for accidental matches; the ranking
change of PR 27 mitigates it, `bl-dde1f708` (FTS5) is the real fix.

## 2. The memory stack, as it actually exists

Six layers, none of them a vector database:

| Layer | Where | How it reaches an agent |
|---|---|---|
| Knowledge base | `os.db` `knowledge`, SQLite | bounded INDEX in every worker prompt; full text on demand via `jarvis learn show/search` |
| Pinned entries | same table, `pinned` tag | pasted verbatim into every prompt, capped at 8. Safety rails only; the live count is 0 |
| Neo's learnings | `neo.db` `learnings` | pasted IN FULL into Neo's system prompt, capped at 20,000 chars, oldest evicted first (`neo.LEARNINGS_CHAR_BUDGET`). Neo's calls are headless with the question as their only input, so it cannot be given an index — there is nothing for it to look up with |
| Claude-code memory mirror | `hooks.capture_memory_write` | a worker writing a memory file has it mirrored into the knowledge base, tagged `claude-memory` |
| Serena code map | `.serena/memories`, committed | symbol-level navigation; ships with the release tag |
| The work-order record | per-project `wo_events` | episodic memory of one order; re-asserted into the session after a compaction (`hooks.compaction_brief`) |

Retrieval is substring matching, word-ORed and ranked by how many query words a row
matched. **No embeddings, no chunking, no FTS5, no reranker.** That is a deliberate
floor, not an oversight — the measured access pattern (`kn-844df5e3`) is that workers
mostly do not search at all: they read an id straight off an index headline. Which is
exactly why headline quality, not search quality, is the lever.

## 3. Observability: there was none, and now there is

Before this work order the OS could say what it KNEW and nothing about what was READ.
`jarvis cost` accounts for every token, including per-call context size — but nothing
attributed any of it to memory, no counter existed for `jarvis learn` verbs, and the only
evidence that workers consult the base at all was `evals/llm/test_knowledge_retrieval.py`,
an opt-in paid eval somebody had to remember to run.

What shipped:

**`knowledge_reads` + `knowledge_read_hits` (os.db).** Every `jarvis learn
show/search/list/topics` is recorded where it happens, with the work order that ran it,
what it asked for, how many entries came back and how many characters they cost.
Recorded at the read for the same reason `agent_calls` is recorded at the call: nothing
can recover it afterwards. `record_knowledge_read` never raises — an observer that can
fail the read it observes is worse than no observer.

Only `show` and `search` write per-entry hits. `list` and `topics` sweep the index; if a
sweep counted as a retrieval, every entry would look consulted and "never read" would be
unreachable.

**`jarvis learn stats [--project p] [--days n] [--json]`**, and the same figures as a
panel on `/knowledge`, answering the three questions in order:

* *How much context?* — the index block measured by building a real dispatch prompt twice,
  with and without it, so the number cannot drift from what `build_worker_prompt` emits;
  the body text never sent beside it; and how many entries reach the index truncated.
* *How often is it used?* — reads, by verb, by worker vs. person, orders that read, chars
  fetched, and per-entry hit counts, which give the two lists that ask for a decision:
  most-read entries, and entries nobody has ever opened.
* *When could it have been used and was not?* — work orders that completed with zero
  reads, and of those, the ones whose own title matches an entry that already existed
  when they started. **A title match is a hint, not a verdict**, and it is labelled that
  way on every surface: it is scored with the same `LIKE` search a worker would have run,
  so it is blind to synonyms in both directions. Also recorded: reads that came back
  EMPTY, which is the opposite signal — an agent asked and the base had nothing.

**The honesty boundary.** Every "never looked" figure is scoped to work that started
after the read log's first row. Applied to the historical fleet the naive version reports
125 completed orders that "never consulted memory"; all 125 predate the table. An absent
measurement is not a finding, so `observed_from` gates those counts and both renderers
say when the log began.

## What this does not do

* No per-entry attribution of what a read did to the resulting work. "Read and then did
  the right thing" is the eval's question, and the eval stays the instrument for it.
* No automatic retraction of entries nothing reads. The report names them; a human
  decides. An entry can be correct, unread and load-bearing the one time it matters.
* `--days` filters reads and orders. It does not filter the base itself: an old entry
  read yesterday is a current entry.
