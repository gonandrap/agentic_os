# The cost page never waits (mv-d58a1b67, wo-0d358979)

Follows 2026-10-22-a-slow-request-is-not-a-wedge.md, which this amends.

## The problem

PR #1241 (release jarvis-0.12.27) failed production validation mv-d58a1b67: `/cost`
returned 200 with the `computing` state, but took **6.8s on two consecutive checks**. The
user's ruling: "6s is too much, current window limit (5h and 7d) should be instantaneous."

`CostCache.serve` did `event.wait(WAIT_SECONDS)` (5s) whenever a window had no entry at
all, and only then rendered `computing.html`. So the cheapest possible response — "it is
being computed, the page refreshes itself" — cost 5s plus the window resolution. The
aggregation no longer ran *on* the request path, but the request still blocked *for* it.

Morpheus checks right after the release restarted `jarvis-ui.service`, so `CostCache.run`'s
first round had not landed yet: the current week window missed `entries`, fell through to
`serve`, and ate the full 5s. Twice, because each check was inside that same first round.

## The fix

`WAIT_SECONDS` is deleted. `serve` starts the compute thread and returns what it has:

- entry younger than `REFRESH_SECONDS`: served as it is;
- entry older: a recompute starts, the stale entry is served at once (`computed Ns ago`);
- **no entry: a compute starts and `None` comes back immediately**, so `cost_page` renders
  `computing.html` — the 2s meta-refresh page — in the time a template render takes.

`CostCache.start` no longer returns the `threading.Event`: nothing waits on it, and the
`computing` dict is what dedupes concurrent requests for one window (including each 2s
refresh of `computing.html`, so a reader cannot spawn threads by waiting).

`CostCache.key` buckets its `until` by `REFRESH_SECONDS` **when the window is open-ended**.
Without the wait, the 2s meta refresh is now what drives an uncached window to completion,
and a `since=…` window with no `until` ends at NOW: an exact key missed on every visit, so
each refresh started another compute and the page could never resolve. The bucket is the
same staleness every entry is already served with. A window with both bounds given keeps
its exact `until`, so two distinct bounded ranges inside one bucket cannot share an entry —
`resolve_window` marks the difference with `open_ended`.

The current 5h and week windows stay instantaneous the way #1241 made them: the
`CostCache.run` precompute, served through `lookup`, no aggregation on the path.

> Deleting the wait also stopped accidentally serialising the request thread against the
> compute thread, which exposed a deadlock in SQLite 3.51.0/3.51.1 itself (`kn-8e43df8e`,
> `io-6fdf5c66`). Not this repository's bug and not fixed here: `#1306` pins an
> unaffected interpreter.
>
> Amended by [2026-10-29-the-cost-page-reuses-what-it-parsed.md](2026-10-29-the-cost-page-reuses-what-it-parsed.md),
> which caches each transcript's parse and moves round one of the precompute into
> `lifespan`, so the first request after a restart renders the report. Where this spec
> estimated 8-22s for a cold window, the measured figure for one `fleetcost.report` is
> 3.47s, and 0.37s warm; a whole precompute round is 4.49s cold and 2.24s after a restart
> that follows a save.

## What is NOT changed

- The two windows are still computed serially in one round, and a cold request computes
  its window a second time off the path. Both spend CPU, neither delays a response.

Superseded by the 10-29 spec: this spec left a cold `jarvis-ui` serving `computing.html`
for the current window until the first precompute round landed, and declined to persist
anything across restarts (Neo 2094 rejected option B: new schema, cross-process writer,
staleness the UI cannot see). The user refused that, and kn-79da1c34 is the standing rule.
The 10-29 spec persists the parsed transcripts — not a window snapshot, so option B's
objections do not apply — and awaits round one before `lifespan` yields.

## Tests (tests/test_ui_cost.py)

- `test_the_request_path_never_waits_for_the_aggregation`: `ops.cost_report` held open on a
  gate; the request returns `computing.html` in under 1s, and the next visit is the page.
- `test_a_current_window_renders_with_the_aggregation_blocked` (week and 5h): a precomputed
  window renders under 1s while `ops.cost_report` cannot return at all.
- `test_an_open_ended_window_is_computed_once_however_often_it_is_visited`: three visits to
  `/cost?since=…`, one `ops.cost_report` call, no thread left running.
- The helper `cost_page` is what every pre-existing `/cost` test now visits through: a
  window nothing has computed yet takes two visits, which is the behaviour, not a wart.
