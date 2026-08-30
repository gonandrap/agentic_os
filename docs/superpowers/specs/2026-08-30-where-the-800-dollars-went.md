# Where the fleet's $800 went

**wo-237d6dc4, 2026-08-30.** Three questions were asked. The headline is that the first
one rested on a misreading, and correcting it reverses the recommendation.

Method throughout: transcript arithmetic per `kn-2137076d` (dedupe by `message.id`, MAX
each usage field), over all 1,612 transcripts on the machine. No `jarvis cost` run was
repeated — the investigation cost is a few seconds of local file reading.

Everything below is in **base-input-token equivalents**: read 0.1x, 5-minute write
1.25x, 1-hour write 2.0x, output 5x (every model in `usage.PRICES` has a 5:1
output:input ratio, so the unit is model-independent and a price is a scalar on top).

---

## Finding 1 — do NOT buy the one-hour cache. The 157k re-write was a 12-second gap.

**Recommendation: keep `FORCE_PROMPT_CACHING_5M`. No change.**

### The premise was wrong

The work order reads wo-5a6b2d6d turn 2 — `ctx 173k, read 16k, wrote 157k` — as a
five-minute cache dying during a fourteen-minute turn. The transcript says otherwise.
Turn 1 *ran* for fourteen minutes, but it made 32 API calls while it ran, and **every
call rewrites the cache**. The gap between the last call of turn 1 and the first call of
turn 2 was **12 seconds**:

```
 31  01:11:39                    (turn 1's last call)
 32  01:11:51 gap=12s ctx=172,962 read=15,862 write=157,098   COLD
```

The entry was alive. It was not matched, which is a different failure with a different
cure. A turn's *duration* is not the age of its cache; the age is the gap since the
previous **call**, and inside a turn those are seconds apart.

This is the mistake `kn-81a91bac` recorded and `kn-625e79f1` diagnosed — "a 10-second
boundary after an edit is cold" — arriving again from the other direction.

### The discriminator

Two causes make a boundary cold, and they leave different fingerprints:

| | what survives | signature | cure |
|---|---|---|---|
| **prefix moved** | the static head of the system prompt | `read` on a plateau ~15–22k (one value per project's `CLAUDE.md` length) | keep the prefix still |
| **entry expired** | nothing at all | `read` ≈ 0, and gap ≥ TTL | a longer TTL |

`read=15,862` above is the prefix signature exactly — the same number family as the
15,461 / 15,995 measured in the clean room.

### Fleet-wide, classified

All transcripts, cold boundaries (`write > 40k` and `read < 40k`), excluding session
starts:

| cause | boundaries | write tokens | share of tax |
|---|---:|---:|---:|
| cold **inside** the 5m TTL → prefix moved | 187 | 24,763,852 | 34.7% |
| gap > 5m but prefix survived → prefix moved | 199 | 31,098,167 | 43.6% |
| gap > 5m and read ≈ 0 → **TTL expired** | 86 | 15,445,979 | **21.7%** |
| | **472** | **71,307,998** | |

**81.8% of the re-write tax is invisible to any TTL change.**

### The break-even, and why it is not close

Switching to the 1-hour TTL pays 0.75x more on *every* cache-write token and buys back
1.15x on *only* the expired ones:

```
saving  = 1.15 * W_ttl
penalty = 0.75 * (W_total - W_ttl)
1h wins  iff  W_ttl / W_total > 0.75 / 1.90 = 39.5%
```

Measured `W_ttl / W_total` = **13.8%** (15.4M of 112.0M). Net **−54.7M**
base-input-token equivalents ≈ **$273 worse** at Opus list. It is not marginal; it
loses by roughly 4x.

### What would change my mind — and it is moving

Split by era, around the `includeGitInstructions` fix (PR #96, 2026-08-15):

| cohort | sessions | TTL share of tax | verdict |
|---|---:|---:|---|
| pre git-fix | 736 | 4.3% | loss by 31.4M |
| post git-fix | 862 | 30.3% | loss by 23.2M |
| last 7 days | 469 | **39.2%** | loss by 8.0M |

The fix worked: removing the prefix churn leaves TTL expiry as a larger *share* of a
smaller problem. The last-7-day cohort sits at 39.2% against a 39.5% break-even — **the
answer is one good week from flipping.**

So: keep the constant, and re-measure when the fleet's shape changes. The trigger is a
single number, and `jarvis cost` now prints it beside the tax rather than leaving it to
be re-derived — which is the whole reason this work order's premise was available to
misread. **Re-open this when the TTL share of the re-write tax exceeds 39.5% over a
trailing month.**

### On a per-project setting

The work order asks whether a per-project TTL beats a constant. The arithmetic says a
one-turn order wants 5m (a 2x write with no read after it is strictly worse) and a
twelve-turn order wants 1h — but **the setting cannot express that**, because of when
the decision is taken. The TTL is a property of the *process*, chosen when a turn
starts; the benefit is collected by the *next* turn's first call. At the moment Jarvis
must choose, it does not yet know how long the gap will be or whether the prefix will
survive.

That is not fatal — a project whose orders are reliably long and chatty is a real
signal — but it means the setting is a *heuristic about a project's shape*, not a
per-order optimum, and it should be introduced as one. **Not built here**: the key
would land in the catalog, which is the code `fo-306b8f48` is rewriting for
`jarvis config`. Filed as follow-up.

---

## Finding 2 — tiering is defensible, and the population it would help barely exists

**Recommendation: do not tier by work-order kind. The class of small orders it targets
is 0.1% of spend. Run the A/B in the "what would settle it" section before adopting
anything.**

### The counter-risk, priced

The work order states the interaction correctly and demands it be quantified: a cheaper
model that needs extra turns may cost more, because boundaries are where the money is.

Measured over the 65 Jarvis worker sessions since the git fix, the cost of **one extra
turn** as a fraction of the session's whole bill:

| p25 | median | p75 | p90 |
|---:|---:|---:|---:|
| 17.7% | **30.5%** | 86.5% | 114.4% |

With saving `1 − r` and per-extra-turn penalty `r × 0.305` (the cheaper model pays for
its own extra turns too):

| model | input price vs Opus | break-even |
|---|---:|---:|
| Sonnet 5 | 0.60x ($3 vs $5) | **2.2 extra turns** |
| Haiku 4.5 | 0.20x ($1 vs $5) | **13.1 extra turns** |

So the stated counter-risk — "three turns where opus needed one" — **is** disqualifying
for Sonnet, but only just, and Haiku is essentially unlosable on price. Median turns per
session is 4, so Sonnet loses only if it inflates a 4-turn order past 6.

**The important inversion:** the marginal-turn cost rises with session size, so tiering
is *safest where it saves least* (small sessions, p25 → 3.8 extra turns of headroom) and
*riskiest where the money is* (p90 → under one extra turn of headroom). The intuitive
policy — "cheap model for small jobs" — optimises the wrong end.

### Why tiering by kind has no headroom

The 65 Jarvis worker sessions, by shape, weighted by cached tokens:

| shape | sessions | cached tokens | share |
|---|---:|---:|---:|
| 1 turn, peak < 60k | 2 | 1,018,174 | **0.1%** |
| 1 turn, peak ≥ 60k | 18 | 124,115,059 | 10.5% |
| 2+ turns, peak < 60k | 0 | 0 | 0.0% |
| 2+ turns, peak ≥ 60k | 45 | 1,053,827,748 | **89.4%** |

The one-turn triage order the work order imagines tiering to Haiku **is two sessions and
one tenth of one percent of spend**. There is no cheap-and-short population; a Jarvis
work order is a long, large-context conversation almost by construction. 89.4% of the
tokens sit in exactly the sessions where the extra-turn penalty is worst.

Fleet model usage confirms nothing tiers today: Opus 8,188 calls / 1.255B tokens against
Sonnet 722 / 23.7M and Haiku 120 / 2.2M.

### What would settle it

Not an opinion — a paired trial. Same work order text dispatched to Sonnet and to Opus,
n ≥ 10 pairs, drawn from ordinary orders rather than chosen ones, measuring **turns to
completion, validation-panel rejections, and total base-equivalent cost**. Sonnet wins
only if median turns rises by less than 2.2 *and* rejection rate does not rise — a
rejected order pays for its rework at full price and is the failure mode the token
arithmetic cannot see. Until that trial exists, changing `os.defaults.model` is a guess
with a plausible story attached.

---

## Finding 3 — 400,000 is not a setting, it is an off switch

**Recommendation: lower `os.defaults.autocompact_window` to 150,000. NOT DONE HERE —
this is a default and the user decides. The measurement is below.**

Cache **read** is the single largest line on the bill and it is not the re-write tax.
Post-git-fix: 1.209B read tokens (121M base-equivalents at 0.1x) against 67.6M written
(84.4M at 1.25x). Reads are **59%** of the input bill. Reads are context x calls, so a
cap on context cuts them near-linearly from the *first* call — there is no cliff at the
window, which is precisely why a window set above the fleet's ceiling does nothing.

Peak context, 862 post-fix sessions: median 21,259 · p75 38,735 · p90 85,482 ·
p99 279,970 · max 371,902.

**The max is 371,902. The window is 400,000. It has never fired.**

Cache-read tokens under a cap (upper bound — compaction is itself a call that pays a
cold write, and a compacted worker may re-read files; per `kn-81a91bac`, do not quote
these as realised savings):

| cap | read tokens | cut |
|---:|---:|---:|
| 400,000 (today) | 1,209,190,623 | **0.0%** |
| 300,000 | 1,194,700,026 | 1.2% |
| 200,000 | 1,090,312,111 | 9.8% |
| **150,000** | 951,522,563 | **21.3%** |
| 120,000 | 825,143,307 | 31.8% |

150,000 touches roughly the top 3–4% of sessions by peak context and reaches a fifth of
the largest line on the bill, because the fat tail carries the tokens. Below that the
curve keeps paying, but 120k starts compacting p90-ish sessions, and `kn-a5714b40`'s
100k floor is close enough to be a dispatch hazard.

### On opening a later turn from a summary

The work order asks whether an order that has submitted its plan should resume from a
summary. **No — and the shape has already been ruled out twice.** `kn-f94abf34` (5):
`PostCompact` cannot inject (`additionalContext` is not accepted on that hook), and
Jarvis should not write a summary at all, because what a compacted worker loses is the
*contract* — work-order id, branch, PR, pending assumptions — all of which Jarvis holds
as structured state and can re-inject **deterministically, with no second model call**.
`kn-81a91bac` separately rejects a `/compact` before a resume: two `claude -p --resume`
on one session do not queue, so a compact turn must complete before the real one, making
two boundaries where there was one.

`--autocompact` is the lever that already exists. It is currently set to a number that
switches it off.

---

## What was not chased

Subagents (~$7.28) and Jarvis's own calls (~$8.67) are ~2% of spend, as the work order
said. Confirmed, not investigated.
