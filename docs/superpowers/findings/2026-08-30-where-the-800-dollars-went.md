# Where the fleet's $800 went

**wo-237d6dc4.** Investigating the `jarvis cost` run of **2026-08-27** — 52 work orders,
~$800.78 at list, re-write tax ~$193.74 (24%), 43.1M cache-write tokens of which 4.3M
bought the one-hour TTL.

Five findings. Each is stated as Area / Finding / Root cause / Follow-up actions with
pros and cons. Nothing here is applied: every follow-up is a decision for the user.

Method: transcript arithmetic per `kn-2137076d`, over the transcripts backing that run.
Where a number is fleet-wide rather than from the 08-27 set, it says so.

---

## Finding 1 — the headline re-write number was misread

**Area.** `jarvis cost`'s re-write tax; `usage._usage_of` boundary accounting.

**Finding.** The $193.74 re-write tax is real, but the example used to explain it is not
what it was taken for. wo-5a6b2d6d turn 2 (`ctx 172,962 / read 15,862 / write 157,098`)
was read as a five-minute cache expiring during a fourteen-minute turn. The transcript
timestamps say the gap between the last API call of turn 1 and the first of turn 2 was
**12 seconds**:

```
 31  01:11:39                                                   turn 1's last call
 32  01:11:51  gap=12s  ctx=172,962  read=15,862  write=157,098  COLD
```

Across all transcripts, **81.8% of the re-write tax sits at boundaries that no cache TTL
could have helped** — 386 of 472 boundaries, 55.9M of 71.3M re-written tokens.

**Root cause.** Two different failures produce an identical-looking row, and the report
printed one number for both. A turn is dozens of API calls and *every* call rewrites the
cache entry, so an entry's age is the gap since the previous **call**, not the turn's
wall-clock duration. At call 32 the entry was alive; the prompt prefix had changed, so
the cached prefix no longer matched. `read=15,862` is the signature — the static head of
the system prompt survived, everything after it did not. A genuinely expired entry leaves
nothing at all and reads ≈ 0.

**Follow-up actions.**

1. *(applied in this PR)* **Report the two causes separately.** `jarvis cost` now prints
   the split, and `os.cold_prefix_floor` is the threshold.
   · Pro: the mistake cannot recur silently; the TTL decision becomes checkable.
   · Con: one more configuration key, and the threshold is a heuristic — a project whose
   static head is under the floor would be misclassified.
2. **Do nothing else here.** The measurement is now correct.

---

## Finding 2 — the one-hour cache would cost more, not less

**Area.** `claude_cli.PROMPT_CACHE_5M_ENV`; the fleet-wide TTL choice.

**Finding.** Switching the fleet from the 5-minute to the 1-hour cache write **loses
money at every cohort measured**. The deciding ratio — TTL-expiry tokens as a share of
*all* cache writes — is 10.6% over the trailing 30 days and 18.5–20.4% over the trailing
7, against a break-even of 39.5%. Fleet-wide the switch costs ~54.7M base-input-token
equivalents (~$273 at Opus list) more than it saves.

But it is moving: 0.3% before the `includeGitInstructions` fix (2026-08-15), 15.4% after,
~20% last week.

**Root cause.** The 1-hour premium is paid on **every** written token (2.0x vs 1.25x) and
is recovered only on tokens that a longer entry would have kept. Most cold boundaries are
prefix changes (finding 1), which no TTL touches. The ratio is climbing only because the
git-instructions fix removed prefix churn, leaving expiry a larger share of a smaller
problem.

**Watch the denominator.** The TTL's share **of the re-write tax** is roughly double the
deciding ratio (40.6% vs 20.4% last week). Comparing *that* against 39.5% says "switch
now" and is wrong. This draft made that error; `scripts/cache_ttl_cohort.py` prints both,
labelled.

**Follow-up actions.**

1. **Keep the 5-minute write; re-measure monthly.**
   `uv run python scripts/cache_ttl_cohort.py --days 30`, switch when it says to.
   · Pro: costs nothing, and the trigger is a single printed number.
   · Con: needs someone to actually run it; nothing alerts on the crossing.
2. **Make it a per-project setting** (backlog `bl-1bd68f28`).
   · Pro: a project with long, chatty orders has genuinely different economics.
   · Con: the TTL is fixed when a turn *starts* but collected by the *next* turn's first
   call, so at decision time neither the gap nor the prefix's fate is known — the setting
   is a guess about a project's shape, not a per-order optimum.
3. **Have `jarvis doctor` check the ratio and raise an inbox item at 39.5%.**
   · Pro: removes the "someone must remember" failure of option 1.
   · Con: a scan of every transcript on each tick; would need caching or sampling.

---

## Finding 3 — where the 4.3M one-hour writes actually are

**Area.** `dispatch._write_worker_settings`, `claude_cli.cache_env` — and, as it turns
out, neither.

**Finding.** They are not Jarvis's. Of the 1-hour writes after the transport fix landed
(2026-08-22), **none come from a Jarvis-dispatched worker turn**. Fleet-wide, 5.48M
tokens were written at 1h after that date:

| where | tokens | what it is |
|---|---:|---|
| inside a Jarvis worktree | 3,126,902 | **one** work order, wo-2df8828c, two session files |
| outside any worktree | 2,357,105 | the user's own `claude` sessions |

And wo-2df8828c is not a dispatched turn either. Its transcript records
`entrypoint: cli` on 990 rows and `promptSource: typed` on 10 — that session was opened
**interactively, by hand, in the worktree**, and was still being written to on 2026-08-30,
four days after the work order completed. Its Jarvis settings file does carry
`FORCE_PROMPT_CACHING_5M=1`.

**Root cause.** `FORCE_PROMPT_CACHING_5M` reaches a process through the `--settings` file
Jarvis writes and through the environment Jarvis sets. An interactive `claude` started by
a human reads neither, so Claude Code falls back to its own default, which allowlists
`repl_main_thread*` for the 1-hour TTL. Jarvis's own transport is airtight; the leak is
the human sitting next to it.

**Follow-up actions.**

1. **Set the flag in the user's own Claude settings** (`~/.claude/settings.json`, `env`
   block), not in Jarvis.
   · Pro: closes 100% of the remaining 1h spend; one line; outside the OS entirely.
   · Con: it is the user's personal config, so Jarvis cannot own or verify it, and a
   fresh machine silently loses it.
2. **Have `jarvis doctor` report 1h writes it did not cause**, naming the session.
   · Pro: makes an invisible leak visible without Jarvis reaching into personal config.
   · Con: reports a condition Jarvis cannot fix — an alert with no button.
3. **Do nothing.** 5.48M tokens ≈ $27 at Opus list, once, across all history.
   · Pro: honest about the size; this is not where the $800 went.
   · Con: grows with every hand-opened session in a worktree.

---

## Finding 4 — nothing measures whether the prefix is stable

**Area.** `dispatch._write_worker_settings` (`includeGitInstructions: false`),
`worker_brief.git_briefing`, and the absence of a check on either.

**Finding.** The prefix fix works — it is why finding 2's ratio moved from 0.3% to ~20%.
But **the fleet has no ongoing measurement of it**, and prefix invalidation is still
69.7% of the post-fix re-write tax (33.2M tokens, 231 boundaries). Of those, 39% have an
MCP tools/instructions delta immediately before them (11.8M tokens, 35.4% by volume). The
remainder is unexplained.

Nothing today would tell you if a CLI upgrade, a new MCP server, or a change to
`git_briefing` re-broke the prefix. It would surface only as a larger bill.

**Root cause.** The fix was verified once, in a clean room, and then trusted. There is no
post-condition on it, no invariant, and no surface that reports prefix stability — even
though the evidence sits in every transcript and `jarvis cost` already parses those.

**Follow-up actions.**

1. **A `SessionStart`/`PreToolUse` hook that records the prefix and reports drift**, as
   suggested.
   · Pro: catches a regression at the moment it happens, on the machine where it
   happened, with the offending turn named.
   · Con: a hook cannot see the API's cache accounting — it can hash the rendered system
   prompt, but the cache boundary is a fact the *response* reports, so a hook is a proxy.
   It also runs on every session, for a condition that changes rarely.
2. **A `jarvis doctor` post-condition over recent transcripts** — recommended.
   `boundaries_ttl` vs prefix boundaries is already computed by this PR, so the check is
   a threshold on numbers the OS now has.
   · Pro: no new hook, no per-session cost, uses the real cache accounting rather than a
   proxy, and fits the existing "the OS checks its own post-conditions" pattern.
   · Con: detects a regression a tick later rather than instantly, and needs a threshold
   that will not cry wolf on a quiet day.
3. **Settle the MCP question first, since it is 35% of the remainder.** kn-f94abf34 (3)
   already proposes `--strict-mcp-config --mcp-config <file>` with Serena only; the
   census found 287 MCP calls in 13,061, 96% of them Serena.
   · Pro: removes both the mid-turn tool-set change and the schemas every prompt carries.
   · Con: narrows what every worker can reach — a fleet capability decision, not a free
   arithmetic win.

---

## Finding 5 — the context cap is set above the fleet's ceiling

**Area.** `os.defaults.autocompact_window`, and message delivery into a cold session.

**Finding.** `autocompact_window` is 400,000. The largest context any session reached is
**371,902**, so it has never fired on any session measured. Cache **read** — not the
re-write tax — is the largest line: 1.209B read tokens against 67.6M written, 59% of the
input bill. Reads are context x calls, so they scale from the first call rather than at a
cliff.

Modelled cache-read reduction by cap (upper bounds; compaction is itself a call that pays
a cold write, and a compacted worker may re-read files):

| cap | read tokens | cut |
|---:|---:|---:|
| 400,000 (today) | 1,209,190,623 | 0.0% |
| 200,000 | 1,090,312,111 | 9.8% |
| **150,000** | 951,522,563 | **21.3%** |
| 120,000 | 825,143,307 | 31.8% |

**Root cause.** 400,000 was chosen (wo-6808dd2d) to leave compaction an exception rather
than a routine event, against a 1M model window. It succeeded so completely that it never
happens: peak context is median 21,259 and p90 85,482, so the cap only ever binds on a
tail it was set above.

**Follow-up actions.**

1. **Lower `os.defaults.autocompact_window` to 150,000.**
   · Pro: reaches a fifth of the largest line on the bill; touches only the ~3–4% of
   sessions with the largest contexts, which is where the tokens are.
   · Con: compaction destroys detail the work-order record depends on, and the saving is
   an upper bound — a compacted worker may re-read the files it just lost.
2. **Compact before delivering a message into a session whose cache has expired**, as
   suggested. **This is feasible but currently loses more than it saves.** `/compact` is
   `type: local` with `supportsNonInteractive: true`, so `claude -p --resume <sid> --
   "/compact"` works with the `-p` transport — the `-p` flag is not the obstacle. The
   obstacle is that two `claude -p --resume` on one session **do not queue** (`--resume`
   refuses a session another process holds), so the compact turn must *complete* before
   the real one: two turn boundaries where there was one, and a boundary is the expensive
   event. Reverting to background sessions would remove that constraint.
   · Pro: attacks reads and writes together, and is targeted — only when the cache is
   actually cold.
   · Con: as measured, one extra boundary costs 30.5% of a session's whole bill at the
   median, so this loses unless compaction saves more than a boundary costs; and it
   silently rewrites the worker's context on a message the user sent for another reason.
   Reverting the transport is a large change to un-pick a 21% read saving that option 1
   gets for free.
3. **Re-inject the contract deterministically after a compaction** instead of
   summarising. What a compacted worker loses is its work-order id, branch, PR and
   pending assumptions — all of which Jarvis holds as structured state.
   · Pro: no second model call; makes option 1 much safer to adopt.
   · Con: `PostCompact` cannot inject (`additionalContext` is not accepted on that hook,
   verified against 2.1.227), so it needs the `PreCompact`-flag-then-`PostToolUse` shape.

---

## What a 14-minute, many-turn work order should do

Directly, since it is the shape most orders have:

- **Nothing about the cache TTL.** Within a turn, calls are seconds apart and the
  5-minute entry is always warm. Between turns, the boundary is usually cold because the
  prefix moved, not because time passed (finding 1).
- **Fewer turns.** A boundary costs 30.5% of a session's whole bill at the median. Batch
  questions into one `jarvis wo ask`; `Daemon.deliver_messages` already coalesces queued
  messages for this reason.
- **Less context.** Reads are 59% of the bill and scale with context on every call, so
  scope smaller orders — the strongest lever, and the one that needs no code change.

## Not investigated

Subagents (~$7.28) and Jarvis's own Neo/panel/digest calls (~$8.67) — together ~2% of the
08-27 run, as the work order stated. Confirmed, not pursued.
