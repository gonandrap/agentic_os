# Compact before a turn whose cache has expired

**Work order:** wo-75baf284 · **Measured:** 2026-09-18/19 · **Status:** shipped

> "when we are past the ttl in between turns, we must emit a compact command before
> sending another prompt, otherwise we will pay a huge re-write tax."

## 1. The waste

Every worker turn is a separate `claude -p --resume` process, so the first API call of
each turn re-sends the accumulated conversation. If the previous turn's last call is
older than the prompt cache's write TTL (300s — `usage.WRITE_TTL_SECONDS`), nothing
survives and the whole conversation is re-sent as a cache WRITE at 1.25x base input.

Two production orders on the afternoon of 2026-09-18, both parked for hours on gates and
Neo answers:

| order | peak context | ttl-expiry boundaries | re-written at those boundaries |
|---|---|---|---|
| wo-7e08ac40 | 366,009 | 5 | 1,273,030 |
| wo-16a488ee | 366,848 | 5 | 1,633,056 |

`scripts/cache_ttl_cohort.py` prices the OTHER remedy for the same waste — buying the
one-hour cache write — and still answers "not yet" (kn-1449447a). This is the third
remedy and the only one that acts at the moment the cost is about to be paid.

## 2. What was measured, and why compaction is cheaper at a cold boundary

A paired probe on `claude` 2.1.277, both arms forked from one 293,377-token base
conversation, both run past the write TTL (`/tmp` probe; Opus, `FORCE_PROMPT_CACHING_5M=1`,
`includeGitInstructions: false`):

| | tokens | cost |
|---|---|---|
| **A** — send the prompt (today) | 294,007 written as cache, 0 read | **$1.853** |
| **B1** — `/compact` first | 282,595 as **plain input**, 8,052 out | $1.621 |
| **B2** — then the same prompt | 15,380 written, 12,776 read | $0.128 |
| | | **B total $1.749** |

Both arms answered the same question correctly; B answered it from the summary.

Three facts fell out of the probe, and the design rests on them:

1. **The CLI does not cache-write a compaction call.** It arrives as plain input at
   1.0x where the next prompt would have re-sent the same tokens at 1.25x. So at an
   already-cold boundary the re-send gets 20% cheaper before anything else happens.
2. **The conversation collapses to a near-constant size.** The transcript's own
   `compact_boundary` row reports 293,382 tokens in and 4,697 out; the next call's
   context is 28,156 (15,380 written + the 12,776-token static head read). The tiny
   26k control compacted to 4,078 / 26,866 — the same answer at an 11x smaller input,
   because the size is set by the summarisation prompt rather than by its input.
3. **It is only ever cheaper at a COLD boundary.** At a warm one the next prompt would
   have READ the conversation at 0.1x, so compacting would pay 1.0x for tokens about to
   cost a tenth of that. That asymmetry IS the trigger.

## 3. The decision

`worker_session.compaction_due`, called by `Daemon._compacted_first` before any queued
message is delivered.

**Trigger.** `now - last_turn.ended_at >= usage.WRITE_TTL_SECONDS`. `ended_at` is a
strict LOWER bound on the cache entry's age — the entry was last touched by that turn's
final API call, which is earlier than the moment its process exited — so the decision
needs no margin and no transcript read, and its error is always in the direction of
compacting less often than the bill would justify.

**Floor.** `os.compact_min_context`, default 100,000 tokens of context, taken from the
previous turn's own usage envelope. `scripts/compaction_cohort.py` prices every
TTL-expired boundary in the fleet's transcripts under the model in its docstring; over
the 30 days to 2026-09-19 (414 boundaries):

| context | n | median net | % net-positive |
|---|---|---|---|
| 25k–50k | 3 | −$0.14 | 33% |
| 50k–100k | 38 | +$0.06 … +$0.36 | 60% |
| 100k–150k | 82 | +$0.39 … +$0.54 | 80% |
| 150k–200k | 79 | +$0.80 | 94% |
| 200k+ | 212 | +$1.11 … +$1.31 | 100% |

100,000 is the lowest band where four boundaries in five pay. Set where the MAJORITY
flips rather than where the mean does: this fires unattended, everything below it is
worth $13.04 of a $552 saving, and half of those boundaries lose money.

**Emission.** A turn of its own — `wo_turns.kind = 'compact'`, prompt `/compact`
(a local command the CLI declares `supportsNonInteractive: true`; there is no flag).
Compacting 300k takes ~30 seconds and the reconcile loop cannot stop for that. The
queued message stays QUEUED and goes out on the next tick, into a session that is both
summarised and warm.

**Exclusions.** Each is a way this could cost more than it saves:

* the cache is still warm (above);
* the conversation is under the floor (above);
* no turn on record — nothing to summarise, and the opening prompt writes rather than
  re-writes;
* a turn is in flight — never mid-turn: what a compaction discards is what that turn is
  holding in its head;
* the last turn was ITSELF a compaction — nothing was added, and if that one failed,
  repeating it is the loop rather than the fix;
* **the last turn is PAUSED**, tested in `compaction_due` itself. Two reasons, either
  sufficient: `worker_session._nudge` tells the worker "the conversation above is
  intact and is where you left off", which a compaction makes false; and the pause is
  re-derived from the LATEST turn every time it is read, so a compact turn behind a
  paused one would erase the pause and strand the relaunch — a permanent stall, not a
  cost. `Daemon.retry_paused_turns` never asks the question, and `delivery_hold`
  holds a queued message behind a RESUMABLE pause, so two of the three callers never
  get here — but a NON-resumable pause is deliberately not a hold (the message is the
  only thing left that can restart the conversation), so the delivery path does reach
  the decision with a pause outstanding. The exclusion is therefore mechanical rather
  than caller-dependent;
* `os.compact_min_context: null` — the only off switch. There is no per-order opt-in
  and no enable flag, per the pinned self-healing learning.

**Not excluded:** validation-round feedback and gate verdicts (Neo, questions 446/447).
Their content is self-contained and the artefacts they refer to are on disk and in the
work-order record, not only in the conversation.

**Never at the cost of a message.** Every failure in `_compacted_first` returns False
and the delivery proceeds exactly as before: the boundary was going to be paid for
anyway, and paying it beats a work order that stops moving.

## 4. The accounting, which is where a saving would otherwise hide

Two traps, both found by the probe rather than reasoned about:

**The compaction writes no assistant message.** `usage.read_session` — the worker half
of every cost surface — cannot see it at all. Recorded in `agent_usage` under kind
`compaction` when the turn is reaped, on both outcomes. Without that the reported
saving would be gross.

**The write it leaves behind looks exactly like a prefix miss.** The call after a
compaction writes ~15k with the static head served, seconds later. `usage` would have
called it prefix invalidation and `jarvis inspect` would have called it `ttl-expiry`:
two classifiers, two wrong answers. Both now read the transcript's `compact_boundary`
marker (`usage.compactions_in`) and label it a third cause —
`Usage.rewrite_compact_write`, `inspection.COMPACTION` — which is also carried on the
sealed bill (`bill.PAYLOAD_VERSION` 4) so `CacheWrites.prefix_boundaries` stays a
subtraction that means what it says.

**The guard that could still have been tripped.** Both cache-health checks are ratios
over every written token, so removing the TTL writes raises every other share without
anything getting worse. Measured over the cohort: prefix invalidation is 26.3% of all
cache writes today and would read 35.9% after this change — still under
`DEFAULT_CACHE_HEALTH_PREFIX_SHARE` (0.45), so `invariants.check_prefix_stable` keeps
its meaning and no threshold moved. Worth re-measuring, not worth pre-adjusting.

## 5. Before and after

Over the 30 days to 2026-09-19 — 6,193 sessions, 414 TTL-expired boundaries, 85.0M
tokens re-written at them. Each boundary is priced with the rest of its turn attached,
and the after figure includes every compaction's own input and output:

```
floor 100,000 tokens of context
  fires on     373 boundaries (81,772,162 tokens)
    before                            $1,352.26
    after (net of the compactions)      $812.84
    saving                              $539.42     39.9%
  skips         41 boundaries below the floor
```

The two orders that prompted this, priced the same way:

| order | boundaries that fire | before | after | saving |
|---|---|---|---|---|
| wo-7e08ac40 | 5 (181k–348k) | $22.41 | $13.94 | $8.48 |
| wo-16a488ee | 5 (311k–351k) | $25.05 | $13.48 | $11.57 |
| **both** | **10** | **$47.47** | **$27.42** | **$20.05 (42%)** |

What the model leaves out, all of it in the conservative direction: the saving
compaction goes on producing in every LATER turn of the same session, and the reads it
saves are capped at the reads actually observed. What it leaves in: the compaction's
whole cost, at list prices, every time.

Dollars are Anthropic list prices — a common unit, not an invoice (`usage.py`).
