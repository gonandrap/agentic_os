# Cost tab precompute (wo-21f139ed)

Design ruled by Neo, question 2094. Not reopened here.

## The problem

Cost tab takes 8-22 s per load. Measured on production 2026-10-22 with curl:
`GET /cost` (default window, week) 21.5 s and 22.1 s; `GET /cost?window=5h` 8.2 s.

Root cause: `create_app/cost_page` (src/jarvis/ui/app.py:1212-1243) recomputes three
payloads from transcripts on every request, in series, for the same resolved window
`picked`: `ops.cost_report(window=picked)`, `ops.fleet_cost(resolved=picked)` and
`ops.cost_meter(resolved=picked)`. Window resolution itself (`ops.cost_zone`,
`ops.cost_window` -> `fleetcost.resolve_window`, src/jarvis/fleetcost.py:127) is cheap:
catalog config plus one `usage_meter.latest_sample` read. Nothing caches the expensive part,
and almost every visit asks for the same two default windows.

## The fix

### Mechanism

New class `CostCache` in src/jarvis/ui/app.py, next to `PoolProbe` (line 230), same shape:
- `REFRESH_SECONDS = 60` module constant. No config key.
- `entries: dict[str, dict]`, keyed by window name (`"week"`, `"5h"` = `fleetcost.WINDOW_NAMES`).
  Each entry: `{"window": picked, "report": ..., "fleet": ..., "meter": ..., "computed_at": time.time()}`.
- `compute(name)` (sync): `zone` default, `picked = ops.cost_window(window=name)` (no project,
  offset 0, no tz/since/until), then the three payloads exactly as `cost_page` builds them
  (`fleet_cost(...)["fleet"]`, `cost_meter(...)["meter"]`).
- `async run()`: loop forever; for each name `await anyio.to_thread.run_sync(self.compute, name)`,
  store on success; `await anyio.sleep(REFRESH_SECONDS)`. Window is re-resolved every refresh,
  so a rollover is picked up by the next round at latest.
- `create_app` (line 734) builds it, `tg.start_soon(cache.run)` in `lifespan` beside
  `probe.run`, and sets `app.state.cost_cache`.

Extract the three-payload build into one helper `_cost_payloads(picked, project)` used by
both `cost_page` and `CostCache.compute`, so cached and live output cannot drift.

### Where, and why there

UI process, in-memory. Only the page is slow, the page's process owns the cache, and lifespan
already hosts one background task. Rejected option B, daemon snapshots in os.db: new schema,
a cross-process writer, and staleness the UI cannot see. Rejected: a per-request memo (TTL on
first hit): first visitor after expiry still waits 22 s, which is the complaint.

### Matching rule

`cost_page` still resolves `zone` and `picked` live first (cheap; keeps every OpsError
refusal as today). Serve cache only when ALL hold:
1. `project == ""`, `offset in ("", "0")`, `since == ""`, `until == ""`;
2. `tz == ""` or `tz == ops.cost_zone(None, None)` (the catalog `week_reset_zone`);
3. `picked["window"]` in `cache.entries` (a `None` window means custom; never matches);
4. `(entry["window"]["since"], entry["window"]["until"]) == (picked["since"], picked["until"])`.

Rule 4 handles rollover: once the live window moves past the cached one, the entry misses and
the page computes live until the next refresh catches up. It also catches 5h switching
between `session-window` and `session-meter` bounds. Any miss: live compute, as today.
On a hit, render with the cached `picked` (identical bounds) and the cached payloads.

### Staleness display

Pass `cached_at=entry["computed_at"]` to `cost.html` (None on live). Template prints one line
near the window label: `computed Ns ago` (N = whole seconds, `now - cached_at`). Live pages
print nothing new.

### Failure behaviour

`compute` failure of `cost_report` or `cost_window`: exception caught in `run`, recorded via
`uilog.record_error("TASK", "cost/cache/<name>", e)` (same call the meter error path uses),
existing entry kept untouched, loop continues. `fleet`/`meter` failures inside the shared
helper degrade to `None` exactly as `cost_page` does today (meter still logs). Never store a
synthesized or empty entry in place of a good one (kn-40db1828). Cancellation propagates.

## Tests (tests/test_ui_cost.py)

1. Cache hit: seed `app.state.cost_cache.entries["week"]` with a sentinel report and matching
   bounds; `GET /cost` renders sentinel and the `computed` line; `ops.cost_report` not called.
2. Each non-default param (`project`, `offset=-1`, `since`, `tz=<other zone>`) bypasses cache.
3. Bound mismatch (stale `since`) computes live, no `computed` line.
4. Empty cache: live compute, page identical to today.
5. `compute` raising: `run` round leaves prior entry intact, `uilog` error recorded, loop alive
   (drive one round via a `step`-style method rather than the infinite loop).
6. `tz` equal to default zone still hits.

Not covered: project-scoped or historical windows (stay live); speeding up the transcript
scan itself (the true cost driver; separate work if live pages still matter).
