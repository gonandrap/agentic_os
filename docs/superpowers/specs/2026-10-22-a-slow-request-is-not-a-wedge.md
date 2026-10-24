# A slow request is not a wedge (issue 1238, wo-3fbed35e)

## The problem

A `/cost` view with a custom since/until window can never hit the `CostCache` precompute
(2026-10-22-cost-tab-precompute.md §Matching rule, rule 3), so `cost_page` computed
`_cost_payloads` inline in the anyio threadpool. On 2026-10-22 that held one request for
over a minute. `PoolProbe.step` fails a round with `inflight_stall` whenever the oldest
in-flight routed request is older than `ui_health.probe_timeout_seconds` (20s), so three
consecutive rounds failed, the probe tripped, stamped `threadpool wedged`, and the daemon
restarted `jarvis-ui.service` — the third restart in 24h, which spent the daily cap and
raised the `restart cap spent` CRITICAL.

Evidence from the stamp: limiter `borrowed 2 / available 38 of 40, 0 waiting`, one worker
thread in `usage.rows` under `ops.cost_report` under `ui/app._cost_payloads`. The pool and
the loop were serving.

Two defects, one each side:

1. `inflight_stall` reads the DURATION of one request as a wedge.
2. the custom-window cost computation runs on the request path.

## Fix 1: progress, not duration (Neo 2225)

`PoolProbe` counts registered requests that were SERVED (`PoolProbe.served`, incremented by
`end(key, served=True)`). A round fails `inflight_stall` only when the oldest in-flight
request is older than `probe_timeout_seconds` AND `served` has not moved since the previous
round.

The issue proposed suppressing `inflight_stall` whenever the pool round-trip succeeds and
the limiter has free tokens. That was NOT taken: it is exactly the blindness condition B
exists to close — issue #986's wedge had 11 blocked handlers against 40 tokens, so the
round-trip was granted a token at once and the limiter never saturated
(2026-10-08-the-dashboard-reports-and-heals-its-own-wedge.md §2, kn-c01217d6). Under the
progress rule #986 still trips, because nothing routed came back while every page hung.

A cancelled request is not progress: `access_log` passes `served=True` only after
`call_next` returned or raised an `Exception`. An anyio cancellation is a `BaseException`,
so a client that gives up on a wedged page cannot look like a dashboard that is serving.

Accepted gap: a single handler stuck forever while every other route serves no longer trips.
That is the intended reading — a restart then kills live work for a fault it may not clear.

## Fix 2: every cost payload is computed off the request path

`CostCache` gains on-demand entries for the windows no refresh covers (a project scope, an
offset, a custom range, a non-default zone), keyed by `(project, since, until)` — the only
inputs the payloads depend on. The zone and the labels come from the request's own resolved
window, so a cached entry can be rendered in any display zone.

`cost_page` never calls `_cost_payloads` itself. On a miss it calls `CostCache.serve`:

- entry younger than `REFRESH_SECONDS`: served as it is;
- entry older: a recompute starts and the stale entry is served at once, with the
  `computed Ns ago` line already on the page saying how old it is;
- no entry: a compute starts and the request waits at most `WAIT_SECONDS` (5, well under
  `probe_timeout_seconds`), then renders `computing.html` — a 2s meta-refresh page naming
  the window.

Each on-demand compute runs in one daemon `threading.Thread`, not in the anyio pool: it must
not borrow the token a page handler needs. At most `MAX_COMPUTING` (4) run at once, so a
crawler on distinct windows cannot spawn threads without bound, and at most `MAX_ON_DEMAND`
(8) entries are kept, oldest dropped first.

Failure: `uilog.record_error("TASK", "cost/on-demand", e)`, the last good entry untouched
(kn-40db1828). With no entry to fall back on, `serve` raises `ops.OpsError`, which
`cost_page` already renders as `error.html`.

## Tests

- `tests/test_ui_observability.py`: a slow routed request on a dashboard that keeps serving
  other routed requests never trips; the #986 shape (nothing completing) still trips.
- `tests/test_ui_cost.py`: a custom window renders `computing.html` when the compute does not
  finish inside `WAIT_SECONDS`, and the page when it does; a stale entry is served while the
  recompute runs; a compute that raises renders the error page and keeps the last good entry.
