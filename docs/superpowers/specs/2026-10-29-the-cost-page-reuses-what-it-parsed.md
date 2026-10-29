# The cost page reuses what it parsed

2026-10-29 · wo-0d358979 · amends
[2026-10-28-the-cost-page-never-waits.md](2026-10-28-the-cost-page-never-waits.md)

## The problem

`2026-10-28-the-cost-page-never-waits.md` made the request path never block, which met
the acceptance for mv-d58a1b67. It did not make the *number* arrive any sooner: a cold
`jarvis-ui` still served the self-refreshing `computing` page for the current window
until the first precompute round landed, and every round re-did the whole aggregation
from scratch.

Measured against the live production state, week window, 16 orders:

| | week | 5h |
|---|---|---|
| before | 3.47s | — |
| within one report, per-file parse reused | 1.52s | 0.53s |
| across reports | 0.67s | 0.53s |

The whole 3.47s is one thing. `usage.rows` was called **395 times over 20 distinct
files**: `_assistant_messages`, `calls_of`, `compactions_in`, `tool_results` and
`_usage_of` each re-read the same JSONL from byte 0, 27,056 `json.loads` for 18,436
rows. Windowing happens *after* the parse, so none of that work depends on the window
being asked for.

## The fix

`src/jarvis/transcript_cache.py` holds each transcript's parsed products keyed on that
file's `(size, mtime_ns)`, in memory and in a gzipped JSON file under `JARVIS_HOME`.
`usage._assistant_messages`, `usage.compactions_in` and `usage.tool_results` go through
it; `calls_of` and `_usage_of` derive from the first and are covered for free.

A transcript is append-only, so a file whose size and mtime are unchanged cannot have
new rows, and a cached parse of it stays correct forever. One cache therefore serves
every window, custom ranges included — which is what makes the *second* visit to a
custom window fast rather than only the precomputed ones.

Measured after the change, same data:

| one `fleetcost.report` call | cold | warm | after a reload from disk |
|---|---|---|---|
| week | 1.75s | 0.37s | 0.35s |
| 5h | 0.17s | 0.16s | — |

The payload is byte-identical in every case. The cache file is 0.43MB gzipped for 24
transcripts and loads in 0.02s; the reduced products are 3.2% of the raw transcript
bytes they came from (1.52MB for 47.1MB), so all 694 transcripts on this machine would
be roughly 34MB before compression.

### Where the granularity sits, and why there

Invalidation is per FILE: a transcript whose `(size, mtime_ns)` changed is re-walked
whole. Byte-offset tail parsing was considered and rejected. `_assistant_messages` and
`compactions_in` are pure per-row folds and would resume trivially, but `tool_results`
carries six fold variables plus an end-of-file flush (`_size_group(group, previous,
None, …)`) that must not be baked into a resumable state — bake it in and tool token
sizing is silently wrong at every boundary. The measured gain of byte-level over
file-level is about 0.06s per changed file against a 0.67s warm recompute, so under 10%
for the one walker that is hard to resume.

### One writer

`CostCache.refresh` saves the file after each precompute round, and nothing else writes
it. `jarvis cost` and any other process read it and benefit, but never save, so a CLI
run cannot drop entries the UI had. The write is a tmp file plus `os.replace`, so a
reader never sees half a file.

## Round one happens before the first request

`lifespan` put `CostCache.run` in the task group and yielded at once, so `entries` was
empty for the whole first round: the current-window request fell through `lookup` to
`serve`, got `None`, and rendered `computing.html`. That is exactly what mv-d58a1b67
measures, because Morpheus checks right after the release restarts `jarvis-ui.service`.
kn-79da1c34 is the standing rule: the current 5h and week windows render instantly.

Round one, measured on the live production state, both windows:

| | round one |
|---|---|
| no cache file at all (first ever boot) | 4.49s |
| restarted right after a save — the Morpheus case | 2.24s |

The *request* that follows is then a dict lookup: `CostCache.lookup` returns a hit in
**0.01ms** for week and 0.006ms for 5h. That is what kn-79da1c34 asks for; startup pays
~2-4s once instead.

Note what these numbers are not. The 0.37s/0.35s figures in the table above are one
`fleetcost.report` call. Round one is two windows through `_cost_payloads`, which also
calls `ops.cost_meter` → `usage_meter.reconciliation`, and *that* scans transcripts
through its own path, which this cache does not cover. Most of the remaining 2.24s is
there, not in `fleetcost`.

`lifespan` now awaits one `cost_cache.refresh()` before it yields, and `CostCache.run`
sleeps before its first round rather than after, so the round is not done twice.

The wait is bounded by `FIRST_ROUND_SECONDS = 30`, well inside the daemon's
`boot_grace_seconds` of 60, so a fleet too large to aggregate cannot keep the dashboard
from starting; it simply serves `computing.html` as before. The bound is soft, not hard:
`refresh` awaits `anyio.to_thread.run_sync`, which on cancellation waits for the running
thread rather than abandoning it, so `move_on_after` stops the *next* window, not the one
in flight.

## The computing page

`computing.html` gains a pure-CSS sweeping bar (no JavaScript, per
`2026-10-22-a-slow-request-is-not-a-wedge.md`) that honours
`prefers-reduced-motion`, and the count of transcripts the cache has already parsed.
The count comes from `transcript_cache.stats()`, which touches no disk.

## The strand the no-wait change left behind

Found by a 120s pytest-timeout on the merged branch, not by review. Because `serve` no
longer waits, a `/cost` request returns while its compute thread is still running, and
that thread outlives whatever set `JARVIS_TRANSCRIPT_ROOT` for it. In the suite the
thread survived its test's `monkeypatch` teardown and went on to scan the real
`~/.claude/projects` — 694 files, 1073MB — holding a SQLite read while an anyio worker
thread servicing the next request blocked on the same lock, and the request's blocking
portal could then never shut down. Three threads, no progress, 120s.

This drain is worth having, but it did **not** fix the hang. A deeper one sits underneath
it, `io-6fdf5c66`, and **it is not this repository's bug**. `kn-8e43df8e` settled it:
SQLite 3.51.0 and 3.51.1 have an ABBA mutex inversion in the unix VFS.
`sqlite3WalClose` calls `unixLock`, which holds the inode mutex and waits on a static VFS
mutex; `unixOpen` calls `findReusableFd`, which holds that static mutex and waits on the
inode mutex. Upstream 3.51.2 fixed it as an "obscure deadlock in the new
broken-posix-lock detection logic". It is a pthread mutex, not `SQLITE_BUSY`, so
`busy_timeout` and pragma order are both irrelevant. Two plain Python threads looping
connect, SELECT, close over **one** WAL file hang the same way with no application code
involved.

What this PR did was make a close and an open overlap: before, `serve`'s 5s wait parked
the request thread while the compute ran, so they almost never did. Dumps caught two
different pairs, the second with the threads on different database files, which is what
ruled out per-file lock contention.

Nothing is fixed here, deliberately. A lock pairing every open against every close was
written and then **reverted** on Neo's ruling (question 2446): it does not reach the
cause, and any two threads in `jarvis-ui` or the daemon opening or closing a WAL database
can still deadlock behind it. The fix is to get off those two SQLite versions — `#1306`
pins a uv-managed CPython (3.13.13 bundles SQLite 3.50.4, unaffected) and `wo-da240136`
adds an import-time refusal of 3.51.0 and 3.51.1.

One measurement of mine was wrong and is worth recording: a two-thread open/close probe
over **two** files did not stall (12,600 cycles in 60s), which I read as the mechanism
being something else. The repro needs one shared WAL file.

## What is NOT changed

- The two named windows are still computed serially, one after the other, in
  `CostCache.refresh`.
- Nothing caches the *window* payload across a restart. The user's bar is response time,
  and `2026-10-22-cost-tab-precompute.md` rejected `os.db` snapshots; what survives a
  restart here is the parse, not the aggregate.
- Window-level additive accumulation (persist a total plus a timestamp, add the delta)
  was rejected as incorrect, not merely awkward. The payload does not sum:
  `context_peak` is a max, `rewrite_excess` derives from it, `classify_boundaries` is
  prefix-sensitive over a session's whole call history, `compaction_payoff` is a
  counterfactual over the whole session, and the tool table is shares. Merging two time
  slices would return wrong numbers with no sign that anything was wrong.
- `MAX_FILES = 400` bounds the cache; the least recently used entry goes first.
- `usage_meter`'s own transcript walk (`owned_session_ids` through `ops.cost_meter`) does
  not go through this cache, and is most of round one's remaining 2.24s. Out of scope:
  the requirement is the request, which is now a dict lookup.
- `io-6fdf5c66` is NOT fixed here, and no lock is added for it: the cause is SQLite
  3.51.0/3.51.1 itself (`kn-8e43df8e`), handled by `#1306` and `wo-da240136`.

## `CostCache.key` and bounded ranges

The 10-28 spec bucketed every `until` by `REFRESH_SECONDS`, which also collapsed two
distinct *bounded* ranges that happened to fall in one bucket onto a single entry.
`resolve_window` now sets `open_ended` on a window whose `until` was read off the clock
(`since=` with no `until`), and only that case is bucketed. A window with both bounds
given keeps its exact `until`.

## Tests

`tests/test_transcript_cache.py`

- an unchanged transcript is parsed once, however often it is read
- an appended transcript is parsed again and the new rows show up
- each product of one transcript is cached apart, and a different `chars_per_token`
  is a different product
- `tool_results` are rebuilt per call, so a caller that mutates them cannot poison
  the cache
- what was saved is what loads back, and a transcript that changed while the cache was
  saved is parsed again
- a damaged cache file is ignored rather than raised
- the cache is bounded by file count
- a transcript that cannot be `stat`-ed is still read

`tests/test_ui_cost.py`

- a fresh app renders both current windows without the computing state — the test opens
  the client as a context manager, which is what runs the `lifespan` the service runs
- shutdown waits out a detached cost compute
- `quiesce` gives up rather than blocking a shutdown forever
