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

### Interaction with retraction

Retraction (#74) gave both ledgers `retired_at`, so that a superseded ruling "leaves the
prompt". **An index headline is still the prompt**, so retraction has to remove an entry
from the map as well as from the payload — otherwise the worker reads the superseded
headline, believes the OS knows something, and goes and fetches it. `knowledge_brief`
therefore inherits `relevant_knowledge`'s filter across all three tiers, and
`knowledge_topics` excludes retired entries too: a topic whose only entries were
retracted must not advertise itself in the overflow roll-call as somewhere to go looking.

The audit surfaces keep main's semantics. `search_knowledge` still returns retired
entries — and it is now also the worker's retrieval verb, deliberately: a worker that
looked something up and got nothing back would conclude the OS knows nothing about it,
when the truth is that it knew and changed its mind. The row says which, and
`cmd_learn`'s digest marks it, so a truncated headline cannot pass for standing advice.

### Does the worker actually retrieve?

The mechanism being correct is not the claim; the claim is that a model handed an index
notices what it needs and aims a retrieval at the right entry. Two evals, deliberately
different in kind:

**`evals/llm/test_knowledge_retrieval.py` — behaviour.** A *tooled* subject in a sandbox
with a real `jarvis` on its PATH and a real task, where the sandbox itself is silent or
actively misleading about the answer, so imitating local precedent gets it wrong and only
the knowledge base gets it right. Batteries escalate: a read verb ran → it ran *before*
the first file was written → the change *reflects what the entry said* → it did not dump
the base into its own context. Plus a **blind control**: the least guessable cases re-run
with no knowledge base must FAIL, or the eval is scoring the model's priors rather than
this plumbing. This is the measurement to trust about worker behaviour.

**`evals/llm/test_knowledge_retrieval_judgment.py` — is the map aimable?** Tool-less
subjects answering with one command, cheap enough to run eleven across two adversarial
batteries a tooled subject structurally cannot measure: **precision** (five billing and
deploy decoys, one right answer — "there is something about deploys in here" is not
findability) and **no-phantom** (four areas the index does not cover; without this, a
model answering everything with `learn show` sweeps the rest). A tooled subject can
search three times, read the wrong entry, notice and recover — the retry is realistic,
and it hides exactly the property these two batteries exist to measure.

Retrieval is graded against the store rather than by string matching: when a reply
searches, the term it chose is run through `search_knowledge` and the target has to come
back. A plausible-looking `learn search "deployment"` that retrieves nothing scores as
the miss it is.

**Its first run scored 2/7 and both causes were real defects in this design, not in the
eval.** Worth recording, because neither was visible to any structural test:

1. **The retrieval verb did not answer the way agents ask.** Two misses were the subject
   searching correctly — `learn search "cents rounding format"` — against a store doing
   `LIKE '%cents rounding format%'`, which requires that literal phrase and returned
   nothing. Agents search in phrases. `search_knowledge` ORs the words and ranks rows by
   how many matched; a single word behaves exactly as before. An index whose lookup verb
   only answers single keywords was not a lookup verb. That word-OR pass is now the
   FLOOR under an FTS5/BM25 tier, which closed the stemming gap this bullet used to end
   on — "rounding" retrieves "rounded" on its own —
   see `2026-08-24-ranked-knowledge-search.md`.
2. **Reading lost to asking.** Three misses went to `jarvis wo ask` on questions the
   index plainly covered — the READ bullet was quietly losing to the (deliberately very
   loud) "Neo is your first responder, any doubt goes to it" rule. Spending the user's
   attention re-deciding what the fleet already recorded is the exact cost this OS
   exists to avoid, so the ordering is now explicit in both places: *a lookup is not a
   doubt — look it up first when a headline names the area; if nothing fits, ask.*

The fix for (2) then broke a third thing, which is why the guard eval matters:
`test_worker_judgment.py` dropped a point because a subject answered a **branch-naming**
call with `jarvis learn search "branch name"`. That eval briefs with an *empty* knowledge
base — so the contract had sent it looking things up in an index that was not in its
prompt. **Both knowledge bullets are therefore conditional on the brief being non-empty**
(as is the `WRITE to it:` prefix, which only parses when a READ bullet precedes it to be
the "it"). With no index the worker contract is now byte-identical to the one before this
change — asserted directly in `test_empty_base_also_removes_the_instructions_to_read_it`,
which makes that eval a clean baseline rather than a thing this branch perturbs.

The general lesson, and the reason it is written down here: **a prompt must not instruct
an agent to consult a resource that is not in that prompt.** The instinct is to state the
rule unconditionally so it is always available; the effect is an agent that goes looking
for something that does not exist, and burns a turn discovering it.

### Accepted risk

A worker that never searches now sees less than it used to. Mitigations: the pinned tier
for anything that must not be missed, the contract bullet, the index making absence
visible, and the eval above measuring whether any of it works. If those scores fall, the
next lever is a SessionStart nudge — not going back to bulk injection.

## 4. Migration

`os.knowledge_inject_limit` changes meaning: from "N most-recent entries injected in full"
to "max **pinned** entries injected in full". Existing catalogs keep parsing and simply
get the new behaviour — nothing is tagged `pinned` yet, so they move to a pure index. Pin
what matters with `jarvis learn pin <id>` or the dashboard toggle.

## 5. Code

| Where | What |
|---|---|
| `src/jarvis/central_store.py` | `headline()`, `KnowledgeBrief`, `knowledge_brief()`, `get_knowledge`, `knowledge_topics`, `count_knowledge`, `pin_knowledge`, project/topic-scoped `search_knowledge` |
| `src/jarvis/dispatch.py` | `render_knowledge_block()`, called from `_common_briefing` so the worker AND planner contracts both get it; the READ bullet in each; `dispatch_work_order(os_config=…)` |
| `src/jarvis/catalog.py` | `knowledge_digest_limit`, `knowledge_digest_chars` |
| `src/jarvis/cli.py` | `learn show|topics|pin|unpin`, `--pin`, `--topic`, `--limit`, `--full` |
| `src/jarvis/ui/` | knowledge page: ids, topic roll-call, pin/unpin toggle |
| `tests/test_knowledge_ondemand.py` | the boundedness property, every tier, retraction, and that each indexed id is retrievable by its own headline |
| `evals/llm/test_knowledge_retrieval_judgment.py` | whether a model actually cashes the index in |
