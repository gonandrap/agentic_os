# The lead buys the hour; the crew does not

**Measured:** 2026-10-11 · Claude Code **2.1.296** · `claude-sonnet-5-5` for every probe ·
**Supersedes the fleet-wide half of** `docs/superpowers/specs/2026-08-22-the-five-minute-write-everywhere.md`
(Jarvis's own one-shot calls keep the five-minute write; what changes is the worker) ·
**Answers the reversal clause of** `docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md` finding 2
· **Closes** the question behind `jarvis cost`'s `rewrite-tax-ttl` alarm and `INV-CACHE-TTL-TRIGGER`.

A prompt-cache WRITE costs 1.25x base input at the five-minute TTL and 2x at the hour; a
READ is 0.1x under either. Since 2026-08-15 every process Jarvis starts has been forced
onto the five-minute write (`FORCE_PROMPT_CACHING_5M=1`), on the measurement that 98.1% of
cache-read tokens land within five minutes of the previous call. This is the re-reading of
that decision after three things changed underneath it: the crew became mandatory (a lead
may not edit, so every work order delegates and WAITS), the OS started refusing any
foreground call over 240 s so the lead POLLS to keep its cache warm, and the CLI grew a
separate TTL for the main thread and for subagents.

The short form: **the lead's conversation should live an hour, every subagent's should
live five minutes, and the CLI lets Jarvis say exactly that.** Shipped as
`worker.cache_ttl` (default `1h`) and `worker.subagent_cache_ttl` (default `5m`).

---

## Finding 1 — the CLI decides the TTL per thread, and Jarvis was overriding both

Read out of the 2.1.296 binary (`strings -n 8`, the `Zfe`/`ysn` pair). Precedence, top
wins:

1. `FORCE_PROMPT_CACHING_5M` → 5m, everything.
2. `CLAUDE_CODE_PROMPT_CACHE_TTL` for the main thread (`querySource` in
   `repl_main_thread*`, **`sdk`** — which is what `claude -p` presents — `auto_mode`,
   `memdir_relevance`); `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` for every other request
   in the process.
3. Settings `promptCacheTtl` / `subagentPromptCacheTtl`.
4. Agent frontmatter `experimental.cacheTtl`.
5. `ENABLE_PROMPT_CACHING_1H` (API-key, Bedrock, gateway only).
6. Automatic: **1h on a subscription within its limits for the main thread, 5m for
   subagents**; 5m for both on an API key. Overage silently drops the main thread to 5m
   (`overage_changed` is a named cache-break cause).

So the vendor's own default for a subscriber is already the split this document argues
for. Jarvis's force flag was the only thing in the way. Verified live, one `-p` turn with
`FORCE_PROMPT_CACHING_5M=0 CLAUDE_CODE_PROMPT_CACHE_TTL=1h CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=5m`
and a foreground `Agent` call:

| request | thread | write | read | split |
|---|---|---:|---:|---|
| 1 | lead | 16,405 | 12,616 | `ephemeral_1h: 16,405` |
| 2 | subagent | 24,086 | 0 | `ephemeral_5m: 24,086` |
| 3 | subagent | 3,733 | 24,086 | 5m |
| 4 | subagent | 34,949 | 27,819 | 5m |
| 5 | lead | 9,292 | 29,021 | `ephemeral_1h: 9,292` |
| 6 | lead | 309 | 38,313 | 1h |

Two threads, two TTLs, one process. The force flag has to be written as `"0"` and not
merely dropped: the user's own `~/.claude/settings.json` sets it (the `cache-1h-foreign`
alarm's remedy line told them to), and a settings-file `env` reaches every `claude` the
machine starts.

## Finding 2 — a TTL cannot be changed mid-conversation; it can only be chosen cold

The adaptive policy that first suggests itself — five-minute at a cold start, where the
whole context is written anyway, then the hour on the warm turns that follow, where only
deltas are written — is not available. Two parallel sessions, same prompt, same files:

| arm | T1 (fresh, 5m) | T2 (`--resume` right away) | T3 (`--resume` after 380 s) |
|---|---|---|---|
| **ctrl**: 5m → 5m → 5m | write 19,343 / read 12,741 | write **1,589** / read 70,730 | write **63,287** / read 12,741 |
| **switch**: 5m → 1h → 1h | write 30,791 / read 0 | write **73,086** / read **0** | write **1,217** / read 75,745 |

Switching the marker from 5m to 1h on a warm resume was a **total miss**: the 1h-marked
request read nothing of a prefix the 5m entry still held, and re-wrote 73k at the 2x
rate. The control's T3 is the other half of the ledger, and it is the fleet's everyday
boundary: six minutes idle on the five-minute write cost a 63k re-write; the same six
minutes on the hour cost 1.2k.

Consequences for the design:

* The TTL is a property of the **session**, decided at dispatch and held. Changing a
  project's `worker.cache_ttl` while an order is running costs that order one re-write
  at its next turn, which is the same price as any other briefing change.
* The only free switch points are the ones where the whole context is written anyway: a
  fresh session, a relaunch past the old entry's TTL, the turn after a compaction. None
  of those is worth a policy today; the per-session constant is.

## Finding 3 — the poll that keeps a five-minute cache warm costs more than the hour

Under the five-minute write a lead waiting on its crew must send a request at least every
five minutes or lose the conversation. The OS enforces exactly that: `jarvis-implementer`
and `jarvis-spec-writer` are refused in the foreground, the lead backgrounds them and
polls with `TaskOutput` every 3–4 minutes, and no single call may block past 240 s
(`hooks.long_foreground_decision`, spec 2026-09-29-a-lead-must-not-block-past-its-cache).

Each poll is a request: a read of the whole lead context at 0.1x plus a few hundred
output tokens. Over a wait of W minutes with context C the lead pays, in base-input-token
equivalents:

| | five-minute write, polling | one-hour write, joined |
|---|---|---|
| keeping warm over W min | `0.1 C × W/4` | 0 (one blocking call, no request in flight) |
| W = 20 min | 0.5 C | 0 |
| W = 50 min | 1.25 C — **the price of just letting it expire** | 0 |
| premium on the turn's own writes (growth G) | 0 | 0.75 G |

A worker turn's growth G is the tool results and replies it appends — tens of thousands
of tokens at a warm boundary, against a context C in the 100k–300k band. The hour costs
`0.75 G` once and buys back `0.1 C` per poll avoided and `1.25 C` per expiry avoided. It
wins on the first delegation of any order.

**Why the cohort numbers never said so.** `scripts/cache_ttl_cohort.py` and
`INV-CACHE-TTL-TRIGGER` price the switch as *TTL-expiry re-writes as a share of all cache
writes* against a 39.5% break-even, measured at 30.5% (30 d) and 36.1% (7 d) on
2026-09-16 — "a close call". Two things make that ratio understate the hour:

1. **The denominator mixes the crew in.** Every subagent is born cold and writes its
   whole prefix at a fresh start — in one measured session the crew wrote 259,744 tokens
   (spec 2026-10-02-subagent-cache-anatomy) — and a subagent is never resumed, so it
   contributes writes and almost no expiries. Restricted to the lead, where the expiries
   live, the ratio is higher than the fleet figure by the crew's share of writes.
2. **Polling converts expiries into reads, and the ratio does not see reads.** Since
   2026-09-29 the lead has been paying the poll tax instead of the expiry; the numerator
   fell while the cost moved to a column the cohort script does not count.

Neither correction has been run against a production cohort — there is no fleet
transcript on this machine. The arithmetic above does not need one: it is per order and
holds whenever a lead waits longer than about eight minutes on a 100k context.

## Finding 4 — the pino proxy is neutral at best, and its defaults make things worse

From the A/B of 2026-10-11 (`~/projects/research/pino-vs-baseline-cache-comparison.md`,
n = 1 per arm, Sonnet, Claude Code 2.1.296):

* Any non-Anthropic `ANTHROPIC_BASE_URL` makes Claude Code switch tool search **off**
  unless `ENABLE_TOOL_SEARCH=true`; every MCP schema then rides inline (133–141 tools,
  ~110k tokens a request against ~31k). That was +400% on its own. Jarvis already pins
  `ENABLE_TOOL_SEARCH=true` on workers (`worker.tool_search: on`), so a fleet behind a
  proxy would not hit this — but a person's own session would.
* Pino's `AUTO_CACHE` default rewrites the conversation marker to **5m**; with
  `TAIL_TTL=1h` and tool search on, the per-turn cost with a warm cache was **identical**
  to direct. Its extra markers on `tools` and `messages[0]` bought nothing measurable:
  caching is prefix-based and the client's marker on `system` already covers the tools.
* Proxied requests form a separate cache lineage (gateway mode changes the beta set and
  pino moves the markers), so a proxy session never warms off a direct one.

**The proxy is not a lever.** Its one real use is `LOG_BODIES=1` for inspection. Nothing
in this change depends on it, and the recommendation is not to run the fleet behind one.

## Finding 5 — what the hour changes elsewhere in the OS

Every rule that was sized from the five-minute window now reads the TTL the lead bought
(`JARVIS_CACHE_TTL`, written beside the CLI's pins in the worker's settings `env`):

| surface | five-minute write | one-hour write |
|---|---|---|
| foreground cap (`hooks.foreground_cap_seconds`) | 240 s | 2,880 s |
| long seats refused in the foreground | `jarvis-implementer`, `jarvis-spec-writer` | none — a join is one blocking call |
| polling rhythm quoted in the brief, OPERATION.md, the hook advice and the Stop guard | every 3–4 minutes | every 30–40 minutes |
| `compact_before_relaunch` clock | cold at 300 s | cold at 3,600 s |
| `usage.classify_boundaries` | flat 300 s | 3,600 s after a call that wrote at 1h (what `inspection.classify_writes` already did) |
| `cache-1h-dispatched` alarm | a breach | silent — it is the policy; `cache-1h-foreign` stands |
| `INV-CACHE-TTL-TRIGGER` | watches the crossing | silent — the hour is bought |

Two things do **not** move. `MCP_TOOL_TIMEOUT` stays at five minutes: a Serena call that
runs longer is a runaway, whatever the cache does. And Jarvis's own one-shot calls (Neo,
the panel, the digest, the validation seats) keep the five-minute write: they arrive in
bursts off a 5 s poll tick, which is the shape the 2026-08-22 spec measured, and nothing
here changes it.

**Compaction before relaunch keeps its job, and gets rarer.** It was built to turn a cold
boundary into a warm one; under the hour most boundaries are warm already, so it fires
only on a conversation idle past the hour. A conversation that cold is usually one the
user parked, which is where a summary costs least.

## What this does NOT settle, and how to settle it

* **The per-side cohort on production.** `scripts/cache_ttl_cohort.py` should print the
  deciding ratio for the lead's calls alone, and count the poll tax (requests whose only
  tool call was `TaskOutput`/`BashOutput`/`sleep`) as cost the hour would have removed.
  Run it over the week before this ships and the week after; the dollar line of
  `jarvis cost --fleet` is the number that has to move.
* **Subagents over 48 minutes.** With the long-seat refusal lifted under the hour, a seat
  that runs past `0.8 × 3600 s` in the foreground expires the lead's hour entry and costs
  a 2x re-write. `jarvis inspect`'s `alarm_join_seconds` (300) will name every such join;
  if they are common, `LONG_SEATS` comes back under the hour with a 48-minute line.
* **Cross-worktree prefix sharing — measured, and not a lever.**
  `claude -p --exclude-dynamic-system-prompt-sections` moves cwd, env and memory paths
  out of the system prompt. Two fresh sessions in different directories, same prompt,
  back to back: without the flag the second read 11,054 of the first's 18,043-token
  prefix; with it, 11,871 of 19,269. The static share is already cached across
  directories; the flag moved ~800 tokens and nothing else. Not shipped.
* **Neo on the hour.** A Neo question answered more than five minutes after the last one
  re-writes Neo's whole prefix. `cache_env(lead="1h")` is one line away for `_run`; the
  2026-08-22 spec's reversal clause (one call every 10–30 minutes against a stable
  prefix) is the test to apply, per project, from `jarvis neo stats`.

## Method

Three probes, all headless Sonnet with `--output-format stream-json --verbose`, usage
read per request off the `assistant` rows deduplicated by `message.id`, the TTL off
`usage.cache_creation`. Runner and analyzer scripts:
`~/.claude/jobs/300bcc01/tmp/ttl/{run_arm.sh,run_split.sh,analyze.sh}`; raw stream files
beside them. The CLI's decision procedure is from a `strings` dump of
`~/.local/share/claude/versions/2.1.296` (a Bun-built Mach-O with the JS bundle inside).
