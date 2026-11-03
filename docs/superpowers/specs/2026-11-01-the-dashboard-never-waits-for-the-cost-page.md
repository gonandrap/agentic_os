# The dashboard never waits for the cost page

Issue 1495. Supersedes §"Round one happens before the first request" of
`2026-10-29-the-cost-page-reuses-what-it-parsed.md`.

## What is broken

`lifespan` in `src/jarvis/ui/app.py` awaited `cost_cache.refresh()` (bounded by
`FIRST_ROUND_SECONDS = 30`) before it yielded. Uvicorn serves no request until lifespan
yields, so every page was down for round one after each restart. After the 0.14.7 deploy
the daemon logged "dashboard not answering 0.1s / 8.2s after jarvis-ui.service started";
with the daemon restarting at the same time, the user saw the UI down for about a minute.

Root cause: a warm `/cost` (kn-79da1c34) was bought by blocking startup. The only state
that warmth needs is the last round's two entries, and those were never persisted.

## Fix

- `lifespan` yields at once. Round one is the first iteration of `CostCache.run`: it
  refreshes first, then sleeps. `FIRST_ROUND_SECONDS` is gone.
- Each `refresh` ends by writing `entries` to `cost-entries-v1.json` beside the transcript
  cache (`CostCache.save_snapshot`, tmp file plus `os.replace`; the UI is its only writer).
- Before it yields, `lifespan` calls `CostCache.load_snapshot()`, a JSON read of two
  entries. Both default windows' bounds are independent of the clock, so `lookup`'s
  `(since, until)` check drops an entry whose window rolled over while the UI was down.
- Round one in the background recomputes only the delta: `transcript_cache` re-parses
  only the transcripts that changed.

The fix lives in `CostCache` because it already owns the entries and `refresh`, which is
their only writer.

## Result (kn-79da1c34, amended)

- Every page answers as soon as imports finish.
- `/cost` default windows render warm from the snapshot right after a restart. They show
  the snapshot's `cached_at` until round one replaces the entries.
- `computing.html` shows for a default window only on a true cold boot: no snapshot, or
  its window has rolled over.

Not done: on a cold boot, a `/cost` visit still starts an on-demand compute that overlaps
round one.
