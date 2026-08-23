# The 5-minute cache write on every path, and pricing the OS half at the rate it paid

**Work order:** wo-b4f207ad · **Measured:** 2026-08-22 · **Subjects:** wo-cd73c537, wo-b9563d2b
**Completes:** `docs/superpowers/specs/2026-08-10-resume-cost-and-the-cache.md` (wo-5668a3f7,
kn-f94abf34) · **Closes:** bl-01fd7e5a · **Settles the open question in** kn-2e0a6317

A prompt-cache **write** costs 1.25x base input at the 5-minute TTL and **2x** at the
one-hour one. A **read** is 0.1x under either. wo-5668a3f7 established that and switched
workers to the 5-minute write. This is the other half: everything else Jarvis runs.

<a id="the-question"></a>
## The question, and why the bill that prompted it was innocent

> look at the bill for wo-cd73c537 — there are a lot of calls with write-cache at 2x, why
> is that?

**That bill is history, not a live leak.** wo-cd73c537 ran turns 1–6 on 2026-08-08;
`FORCE_PROMPT_CACHING_5M=1` landed for workers on 2026-08-15 (commit `1f8a48d`). Its own
transcripts split exactly on that line:

| day (UTC) | cache write | at 1h (2x) | at 5m (1.25x) |
|---|---:|---:|---:|
| 2026-08-09 — turns 1–6 | 1,899,028 | **1,630,440** | 268,588 |
| 2026-08-20 — turn 7 | 1,737,983 | **0** | **1,737,983** |

Method as kn-2137076d: dedupe by `message.id`, max of each usage field, subagent
transcripts included.

<a id="workers-are-not-everything"></a>
## Finding 1 — "every worker" was not "everything Jarvis runs"

The flag shipped inside `dispatch._write_worker_settings`. That reaches worker turns and
nothing else. Neo, the panel's seats and the dashboard digest all run on
`claude_cli.run_headless_result`, which passed no settings file and no env — so they went
on buying the hour for another ten days, invisibly. Measured on wo-b9563d2b:

| | cache write | at 1h (2x) | at 5m (1.25x) |
|---|---:|---:|---:|
| the worker's own turns | 362,028 | 0 | **362,028** |
| Neo answering + the digest | 28,804 | **28,804** | 0 |

Fleet-wide at the time of the fix: **13.8M of 33.8M cache-write tokens at 2x**, an
effective 1.56x.

This was known. kn-2e0a6317 found it and filed bl-01fd7e5a rather than flipping it,
because a 1h write is 2x *once* and survives an hour, so it is not self-evidently wrong.
See [the reversal criteria](#reversal-criteria).

<a id="how-the-cli-picks"></a>
## Finding 2 — how Claude Code picks the TTL, re-read at 2.1.240

`L0e` in 2.1.227 (kn-f94abf34) is `EEe` in 2.1.240. Read out of the binary per kn-f6f418a9:

```js
function EEe(e) {                                                    // e = querySource
  if (Vn(process.env.FORCE_PROMPT_CACHING_5M)) return false;         // <- FIRST
  if (Vn(process.env.ENABLE_PROMPT_CACHING_1H) || …bedrock…) return true;
  if (!ds() || qB().isUsingOverage) return false;
  let t = eWs() ?? nt("tengu_prompt_cache_1h_config",
    { allowlist: ["repl_main_thread*", "sdk", "auto_mode", "memdir_relevance"] }).allowlist;
  return e !== undefined && t.some(r => r.endsWith("*") ? e.startsWith(r.slice(0,-1)) : e === r);
}
```

Two things follow, and both are the reason to force the flag rather than lean on a default:

1. **The 5m check is first and short-circuits.** It beats `ENABLE_PROMPT_CACHING_1H` in an
   inherited environment, so nothing has to be unset.
2. **The allowlist is fetched from `tengu_prompt_cache_1h_config`** — remote config. Every
   `claude -p` matches it through `"sdk"`, and Anthropic can change it without a CLI
   release. Forcing the flag is what makes the rate a Jarvis decision.

`Vn` accepts exactly `"1"`, `"true"`, `"yes"`, `"on"` (lowercased, trimmed; kn-522c6103),
so the *value* matters as much as the key. The failure mode of getting it wrong is silence.

<a id="where-the-flag-lives"></a>
## What shipped — the flag is a property of the transport

`claude_cli.PROMPT_CACHE_5M_ENV`, applied by **`_run` and `spawn_turn`** — the only two
functions in the module that start a process. `dispatch._write_worker_settings` reads the
same constant instead of spelling it again, so the settings file and the spawn environment
cannot drift apart.

**Precedence has three levels and the middle one is the point.** `cache_env(explicit)`
returns `{**PROMPT_CACHE_5M_ENV, **explicit}`:

| level | example | wins? |
|---|---|---|
| ambient environment | a stray `ENABLE_PROMPT_CACHING_1H` in the daemon's env | no |
| this default | `PROMPT_CACHE_5M_ENV` | over ambient |
| an explicit caller | `env_extra={"FORCE_PROMPT_CACHING_5M": "0"}` | over both |

The third level exists so [the reversal criteria](#reversal-criteria) stay measurable.

The two call sites merge in **opposite order**, and that is deliberate rather than
inconsistent: `_run` does `env.update(cache_env(env_extra))` because `env_extra` is caller
*intent* and must win, while `spawn_turn` does `{**os.environ, **cache_env()}` because
`os.environ` is *ambient* and must lose.

A worker turn gets the flag twice over — settings file and spawn env. The file is what
reaches a session the CLI reloads settings for; the env is what makes the property hold for
a turn launched without one.

<a id="the-report-under-priced-it"></a>
## Finding 3 — the report was under-pricing the very tokens under investigation

`usage.priced()` falls back to the **1.25x floor** when it is not told the TTL split. The
split lives in `agent_calls.usage_json`, not in a column, and `central_store.
agent_call_totals` never selected it — so `ops._call_spend` charged every one of Jarvis's
own Neo/panel/digest calls at 1.25x. `bill.py` read the same envelope all along, so the two
surfaces disagreed about the same tokens: `jarvis cost <wo>` said 2x, `jarvis cost
<project>` charged 1.25x.

The query now sums `json_extract(usage_json, '$.cache_1h' / '$.cache_5m')`. `COALESCE`
folds "no envelope" and "envelope predates the field" into the same honest zero, so a
pre-split row still prices at the floor: an **absent** split is not evidence of a one-hour
write, and guessing upward would rewrite the history of every OS call the fleet has made.

**Generalise:** a caller that omits `cache_1h`/`cache_5m` from `priced()` is claiming
ignorance on the report's behalf. Only do it when there is genuinely nothing to pass.

<a id="the-footer"></a>
## The footer — one line so this is answerable in one command

`jarvis cost` now states the rate the fleet paid, summed over the worker transcripts **and**
both classes of recorded call. One line for both halves deliberately: they were switched to
the 5-minute write ten days apart, so a total speaking for only one would have read as
all-clear while half the bill was still at 2x.

```
  cache write   33.8M tokens at 1.56x base input — 13.8M of it bought the ONE-HOUR TTL (2x)
                rather than the five-minute one (1.25x)
                every path Jarvis launches now forces the 5-minute write, so a figure here
                is spend that predates that or a `claude` it did not start
```

Silent when nothing was written, and silent when the whole line was at 1.25x. A report that
says "all good" on every run is a line readers stop seeing; it speaks up only for the part
anyone can act on. The rate is derived from the split rather than restated from a constant,
so it says what was **paid** and not what the code intends to pay (kn-2e0a6317).

<a id="reversal-criteria"></a>
## The honest caveat, and what would reverse this

A 1h write is 2x **once** and survives an hour. Over N calls per hour sharing one prefix,
all spaced more than five minutes apart:

- 1h TTL: `2.0P + 0.1P(N-1)`
- 5m TTL: `1.25P·N` (each call re-writes, having missed)

Equal at `N ≈ 1.65`. Above that the **hour** is cheaper.

It wins here because Jarvis's own calls arrive in **bursts**: `Daemon.drain_queue` answers
Neo's whole queue on a 5-second poll tick, so the reads that matter fall inside the
five-minute window. That is the same shape kn-f94abf34 measured over 1,075 worker
transcripts — 92.4% of cache-read tokens land within one minute of the previous call in
their session, 98.1% within five.

**Re-open this if** a future OS-side agent is paced at one call every 10–30 minutes against
a stable prefix. Use the `env_extra` override to A/B it rather than editing the constant.

<a id="the-test-trap"></a>
## The test trap — a spawn-environment test is vacuous when a worker runs it

`tests/test_prompt_cache_ttl.py` passed all seven assertions **with `_run` and `spawn_turn`
reverted.** These tests are usually run *by* a Jarvis worker, whose own session carries
`FORCE_PROMPT_CACHING_5M=1` from its settings file; pytest inherits it, and the fake
`claude` inherits it from pytest. An autouse fixture now `delenv`s it, and reverting the fix
fails 3 of 7. This is kn-95a32178's rule in a new shape: **any test of a spawn environment
must clear the variable it is about.**

The other guard is structural. A per-call-site assertion cannot fail for a call site nobody
thought of — which is precisely how this bug survived ten days with a green suite. So the
test walks `claude_cli`'s AST and fails if any function other than `_run`/`spawn_turn` calls
`subprocess.run`/`Popen`. Mutation-checked: adding a fourth launcher fails it.
