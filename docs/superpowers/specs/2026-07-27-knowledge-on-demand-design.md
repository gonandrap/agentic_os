# Knowledge on demand — the KB as an index, not a payload

**Status:** implemented (index + pinned tier + retrieval verbs, `jarvis learn show|topics|pin|unpin`)
**Motivation:** every worker prompt pasted the knowledge base into itself, so the cost of
starting *any* work order grew with everything the fleet had ever learned.

## 1. Problem

`dispatch_work_order` did this on every spawn:

```python
knowledge = central.relevant_knowledge(project.name, limit=8)   # ORDER BY ts DESC
prompt = build_worker_prompt(wo, project, knowledge)            # ...pasted in full
```

Two failures, and they pull in opposite directions:

**Cost.** Every entry arrived verbatim. Entries are not uniformly small: the
`capture_memory_write` hook mirrors a worker's whole Claude-memory *file* into a single
row (`record_memory_file`), so one row can be several KB. Eight of those is tens of
thousands of tokens charged to every work order in the fleet, forever, whether or not the
task has anything to do with them.

**Relevance.** The selector was pure recency — `ORDER BY ts DESC LIMIT 8`. At 8 entries
that is the whole base and recency is irrelevant. At 80 it is a 10% sample chosen by a
criterion unrelated to the task. The learning that would have saved this worker is
overwhelmingly likely to sit outside the window.

So the knob was jammed: raising the limit to fix relevance multiplies the cost, and
lowering it to fix the cost destroys relevance. Any design that ships content in the
prompt has this shape. The way out is to stop shipping content.

There was also a quieter asymmetry. The operating contract told workers *how to write* to
the knowledge base (`jarvis learn add`) and never *how to read* it. Retrieval was not
merely expensive — it was undiscoverable. A worker had no reason to believe there was
anything to look up beyond what it had already been handed.

## 2. Design

Three tiers, each with a hard bound, degrading into the next as the base grows.

### 2.1 Tier 1 — pinned (verbatim)

Entries tagged `pinned` are pasted in full into every worker prompt. This is the escape
hatch for safety rails: rules where "the worker didn't think to search" is an
unacceptable failure. Capped at `os.knowledge_inject_limit` (default 8).

Pinning reuses the existing `knowledge.tags` column, so there is no migration. It is a
deliberate curation act, exposed as `jarvis learn pin <id>` / `--pin` on add, and a
toggle on the dashboard's knowledge page.

### 2.2 Tier 2 — the index (headline + id)

Everything else appears as one line: `` `kn-3f2ab1` deploy runs through shipit, never by
hand `` — the entry's first line truncated to 160 chars, under a `### topic` heading.
~20 tokens instead of ~150+, and for the many entries that *are* one short sentence, the
headline is the whole entry anyway.

The id is the point. It turns retrieval from blind keyword guessing into a targeted
`jarvis learn show kn-3f2ab1`. The worker can see that a thing is known, and fetch it.

**Selection is round-robin across topics, not straight recency.** This is the one
non-obvious choice. With recency, a burst of activity in one topic evicts every other
topic from the index — and an index that omits a topic entirely tells the worker there is
nothing there to ask about, which is precisely the failure being replaced. Round-robin
guarantees every topic is represented before any topic gets a second line. Recency still
decides *which* entry represents a topic, and rendering re-groups by topic so the block
reads as a map.

Bounded by `os.knowledge_digest_limit` (40 lines) and `os.knowledge_digest_chars` (4000).

### 2.3 Tier 3 — the overflow roll-call

What did not fit is not silently dropped; it is counted by topic:

```
## Not indexed above — 30 further entries, by topic
ci (9), deploy (8), testing (8), (no topic) (5)
```

Cost here grows with the number of *topics*, not entries — flat in practice. This is what
makes the whole thing scale: at 10,000 entries the prompt block is the same size as at
100, and the worker still knows the shape of what it has not been shown.

### 2.4 The retrieval verbs

The index is worthless without a cheap way to cash it in:

| Command | Returns |
|---|---|
| `jarvis learn search "<term>" --project P [--topic T]` | full text of matches, scoped to P + global |
| `jarvis learn show <id> [<id>…]` | full text of specific entries |
| `jarvis learn list --project P --topic T [--full]` | a topic, headlines by default |
| `jarvis learn topics --project P` | topics and their entry counts |

`learn list` defaults to headlines rather than full text — otherwise the *first* thing a
curious worker runs re-creates the original problem inside its context window.

### 2.5 Telling the worker it should

Mechanism without motivation does nothing, so three things push:

1. The contract gained a READ bullet alongside the WRITE one: *"Look up any area you are
   about to touch before you touch it; a past worker probably already paid for the lesson.
   Do not assume the index headline is the whole entry."*
2. The block is labelled **"This section is an INDEX, not the knowledge"**, with the four
   commands inline — no recall required.
3. The index itself is the strongest nudge. Seeing `` `kn-8a1` [deploy] Never restart the
   jarvis systemd services by hand… `` while about to restart a service is a far better
   prompt than any instruction.

## 3. What this costs and buys

Measured on a synthetic 63-entry base (`tests/test_knowledge_ondemand.py` pins the
property at 1040 entries): the knowledge block renders in ~6k characters and **stays
there** — growing the base 26× moved the prompt by under 200 characters, all of it
counters. Under the old scheme the same base at a limit raised for comparable recall would
have been an order of magnitude larger and still task-blind.

The trade is a round-trip: a worker that needs an entry pays a tool call for it. That is
the right trade — it is paid once, by the one worker who needs it, instead of upfront by
every worker who does not.

### Accepted risk

A worker that never searches now sees less than it used to. Mitigations: the pinned tier
for anything that must not be missed, the contract bullet, and the index making absence
visible. If real sessions show workers skipping retrieval, the next lever is a
SessionStart nudge or an eval scenario that grades it — not going back to bulk injection.

## 4. Migration

`os.knowledge_inject_limit` changes meaning: from "N most-recent entries injected in full"
to "max **pinned** entries injected in full". Existing catalogs keep parsing and simply
get the new behaviour — nothing is tagged `pinned` yet, so they move to a pure index. Pin
what matters with `jarvis learn pin <id>` or the dashboard toggle.

## 5. Code

| Where | What |
|---|---|
| `src/jarvis/central_store.py` | `headline()`, `KnowledgeBrief`, `knowledge_brief()`, `get_knowledge`, `knowledge_topics`, `count_knowledge`, `pin_knowledge`, project/topic-scoped `search_knowledge` |
| `src/jarvis/dispatch.py` | `render_knowledge_block()`, the contract's READ bullet, `dispatch_work_order(os_config=…)` |
| `src/jarvis/catalog.py` | `knowledge_digest_limit`, `knowledge_digest_chars` |
| `src/jarvis/cli.py` | `learn show|topics|pin|unpin`, `--pin`, `--topic`, `--limit`, `--full` |
| `src/jarvis/ui/` | knowledge page: ids, topic roll-call, pin/unpin toggle |
| `tests/test_knowledge_ondemand.py` | the boundedness property and every tier |
