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

| | cold | warm | after a restart (loaded from disk) |
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

This drain is worth having, but it did **not** fix the hang. A second, deeper deadlock
sits underneath it and is filed as `io-6fdf5c66`: the compute thread at
`fleetcost.py:884` (`conn.close()` on a `mode=ro` connection to `os.db`) against an anyio
worker at `db.py:17` (the raw `sqlite3.connect` on the same file, reached through
`render` → `gate_badge` → `list_gates` → `resolve_catalog`). It happens mid-request,
before any drain runs, in 2 of 5 runs of one six-file combination, and 0 of 3 on
`origin/main`. Deleting the wait is what makes those two concurrent; the code they
deadlock on is older and untouched here. The mechanism is not established — `connect_ro`
sets no `busy_timeout` at all, so it should fail fast rather than block.

Two places, because the two scopes cannot see each other:

- `CostCache.quiesce(timeout=10.0)` joins the detached threads, and `lifespan` calls it
  before cancelling the task group. A `jarvis-ui` restart therefore no longer strands a
  compute mid-query. It gives up at the timeout rather than blocking shutdown forever.
- An autouse `no_stray_cost_threads` fixture joins any thread named `jarvis-ui-cost` at
  the end of every test, before `monkeypatch` restores the environment. It matches on the
  thread name rather than the cache object because `jarvis.testing` cannot import the UI
  module (fastapi is optional there).

## What is NOT changed

- The two named windows are still computed serially, one after the other, in
  `CostCache.refresh`.
- Nothing caches the *window* payload across a restart. The user's bar is response
  time, and `2026-10-22-cost-tab-precompute.md` rejected `os.db` snapshots; what
  survives a restart here is the parse, not the aggregate. A cold `jarvis-ui` still
  shows `computing` for the current window — but for ~0.35s of background work, not
  ~3.5s.
- Window-level additive accumulation (persist a total plus a timestamp, add the delta)
  was rejected as incorrect, not merely awkward. The payload does not sum:
  `context_peak` is a max, `rewrite_excess` derives from it, `classify_boundaries` is
  prefix-sensitive over a session's whole call history, `compaction_payoff` is a
  counterfactual over the whole session, and the tool table is shares. Merging two time
  slices would return wrong numbers with no sign that anything was wrong.
- `MAX_FILES = 400` bounds the cache; the least recently used entry goes first.

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

- shutdown waits out a detached cost compute
- `quiesce` gives up rather than blocking a shutdown forever
