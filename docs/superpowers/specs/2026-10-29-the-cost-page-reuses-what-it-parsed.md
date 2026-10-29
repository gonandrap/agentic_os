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
