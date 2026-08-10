# Where the tokens actually go

**Measured 2026-08-09, wo-6e8ca6f1.** Companion to
`2026-08-08-token-spend-findings.md`, which found the CAUSE of the re-write tax. This one
answers a different question: given the whole bill, how much of it is anything Jarvis
puts in a prompt?

The work order that prompted it proposed a specific hypothesis — that Neo's database or
the knowledge base had accumulated large entries that were being injected into every new
work order, so even a tiny one arrived expensive. **That hypothesis is wrong, and the
measurements below are here so nobody has to re-derive that.** One narrower version of it
turned out to be true, in a different place.

## Method

Read-only, against live production state (`JARVIS_HOME=~/workspace/production/state`) and
every Claude Code transcript under `~/.claude/projects` (951 sessions indexed, 910 with
activity since 2026-07-25). Spend is `jarvis.usage`'s arithmetic at Anthropic list prices
— a common unit for comparing a cache-read token with an output token, not a bill. The
dedupe-by-message-id trap documented in `kn-2137076d` applies and was honoured.

## What a tiny work order actually pays for

Built with `dispatch.build_worker_prompt` against the real catalog and the real knowledge
base, for a work order whose whole description is "Fix a typo":

| | chars | ~tokens |
|---|---|---|
| Opening prompt, knowledge block included | 16,034 | 4,008 |
| Opening prompt, knowledge block excluded | 7,973 | 1,993 |
| The knowledge block itself | 7,049 | 1,762 |

The knowledge base held 117 live entries totalling 180,056 characters. **None of that
body text reaches a worker.** `CentralStore.knowledge_brief` ships 25 truncated headlines
under a 4,000-character budget plus a topic roll-call, and only entries tagged `pinned`
are pasted in full — of which there were **zero**. This is `2026-07-27-knowledge-on-demand-design.md`
working exactly as designed.

Deleting the entire knowledge base would therefore save ~1,762 tokens once per session.
On a session that spends 20M tokens that is under 0.5%.

## Where the money is, since 2026-07-25

910 sessions, 10,206 assistant messages, 1.31B tokens, ~$1,123 at list prices.

| Token kind | tokens | $ | share |
|---|---|---|---|
| cache READ | 1,256.0M | 628 | 56% |
| cache WRITE | 49.2M | 308 | 27% |
| output | 7.3M | 184 | 16% |
| plain input | 0.3M | 1 | <1% |

**Cache read is the bill.** It is the accumulated conversation re-read on *every* API call
inside a long session: 1.256B / 10,206 messages ≈ **123k tokens of context per API call**,
averaged over everything. That is not a Jarvis prompt — it is the transcript a worker
builds by reading files, running tests and calling tools. Of the cache-write column,
24.85M tokens are the re-write tax (~$138 above what reading them would have cost).

Splitting by session shape:

| | sessions | $ |
|---|---|---|
| one-shot headless (≤4 messages, <60k peak) — Neo, digests, the panel | 743 | **28** |
| long worker sessions | 167 | **1,095** |

**Everything the OS asks a model on its own behalf is 2.5% of the bill.** The other 97.5%
is workers doing the work.

### Corollary: the fleet page understates the total, correctly

`jarvis cost` / `/cost` reported ~$529 across 57 measured work orders at the time of
writing, against the ~$1,123 above. The difference is real and is not a bug: `cost_report`
attributes spend through `work_orders.session_id`, so it speaks only for sessions Jarvis
dispatched. The user's own interactive sessions, eval runs and headless Neo calls are in
the transcript tree but belong to no work order.

## The one place the hypothesis was right

Neo's own system prompt is persona + **every learning in full**. `learnings_limit` bounded
the row count at 50; nothing bounded the size. Measured: 16 learnings, 23,403 characters —
**86% of a 27,198-character system prompt** — on every Neo call, and once per seat on a
panel round. Two single entries were 5,266 and 4,763 characters.

Unlike the knowledge base, Neo cannot be given an index: its calls are headless with the
question as their only input, so there is nothing to look anything up *with*. The only
available bound is a character budget, and `neo.LEARNINGS_CHAR_BUDGET` (20,000) is it.

It is deliberately a **ceiling rather than a cut**. At 2.5% of spend, trading a third of
the user's accumulated rulings for ~1% of the bill would be a bad deal; what was not
acceptable was the unbounded case, where nothing stopped the block reaching 100k. Eviction
is oldest-first — so an ordinary new learning still extends the cached prefix instead of
rewriting it — and the omission is stated in the prompt, last, where changing it does not
disturb the bytes above.

**Watch the cliff when tuning it.** Entries differ in size by more than 10x, so one
bloated learning displaces many good ones and small budget changes move the count a long
way: on the ledger measured, 24,000 keeps all 16, 20,000 keeps 10, 16,000 keeps 5. The
real remedy is distilling the giants, not raising the number.

## What this means for spending less

1. **Prompt engineering is not the lever.** Every byte Jarvis composes — the worker
   contract, the knowledge index, the navigation briefing, Neo's persona — is a rounding
   error against 123k of average context per API call.
2. **Turn count and context size are the levers**, in that order, and both are properties
   of how a work order is scoped rather than of the OS's prompts. A work order that can be
   finished in two turns without reading half the repo is cheap; one that runs to a 585k
   context is not, whatever its prompt said.
3. **Scope smaller work orders.** The dearest single work order measured (wo-eb9b6337,
   $72.79) reached a 585k peak context. The cheapest useful ones sit under $3.
4. **Batch feedback.** Every `jarvis wo send` round trip is a turn boundary, and a boundary
   on a 400k conversation is ~$4.60. `Daemon.deliver_messages` already coalesces what is
   queued; three separate messages typed a minute apart still cost three boundaries.
5. **Look before guessing.** That is what `/cost` in the dashboard and `jarvis cost` are
   for, and this whole document exists because a plausible theory about prompt bloat
   survived a long time without anyone measuring it.
