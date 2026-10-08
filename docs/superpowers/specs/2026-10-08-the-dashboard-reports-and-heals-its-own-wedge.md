# The dashboard reports and heals its own wedge

Work order wo-be05ab99 · GitHub issue 986 · project `jarvis_os` · 2026-10-08

Ruling that scopes this spec: Neo question 1492. The six pieces below are DECIDED — do not
re-open them. Also decided: **no guessed hardening of the suspect paths** (the per-request
`NeoStore` at `src/jarvis/ui/app.py:926`, the slow `/cost` build, the SQLite handles). That
waits until a stack dump names the holder.

## The problem

`jarvis ui` — uvicorn serving `create_app()` (`src/jarvis/ui/app.py:827`), launched by
`cli.py:5374` `uvicorn.run(create_app(), …, log_level="warning")` — stopped serving **every
routed page** after about 4 hours of uptime, and no surface in the OS noticed.

Observed state of the wedged process:

- `systemctl --user` reported the unit `active (running)`.
- Main thread idle in `ep_poll`; all 11 anyio threadpool threads parked in `futex`; no
  child processes; CPU ~0.5%.
- **Unrouted** paths still answered 404 in ~1ms. Routed paths never answered at all.
- `systemctl --user restart jarvis-ui.service` fixed it instantly.

### Evidence

Production access log `/home/gonzalo/workspace/production/state/logs/ui-access.log:7139-7144`:

- last routed success `05:36:49  GET /config 171ms`;
- then at `05:38:27` only instant 404s — `GET /healthz 1ms`, the reporter probing an
  endpoint that **does not exist** (confirmed: no `healthz` route anywhere in
  `src/jarvis/ui/app.py`);
- recovery at `05:42:58`, after the restart.

Same log, minutes before the wedge: `GET /cost 15424ms`,
`GET /cost?window=week… 13325ms`, `GET /wo/jarvis_os/wo-9f00e3b5 6000ms`. The dashboard
also self-refreshes every `REFRESH_SECONDS = 15` (`app.py:143`) from every open tab, so
those multi-second pages are re-entered on a timer from each tab.

**The 11 threads were not saturated.** The limiter's `total_tokens` is 40 and the process
held 11 threads, so a round-trip through the pool would have been **granted a token at
once** during the fault. Any detector that asks only "is the pool full?" reports this
process healthy while every page hangs — see §2.

### Why all four existing UI-failure surfaces were blind

The OS already has four (kn-57fb4919, kn-e6cd8ca8), and **every one of them is fed by an
error the UI managed to RECORD**:

| Surface | Fed by | Why blind here |
|---|---|---|
| `uilog.record_error` (`app.py:944`, in the `@app.exception_handler(Exception)` at `app.py:939`) | an exception reaching a handler | no exception was raised |
| `access_log` middleware (`app.py:955`) | a completed response — it calls `uilog.record_access` **after** `await call_next` (`app.py:966-978`) | the request never completed, so no line was written |
| `Daemon.check_ui_log` (`src/jarvis/daemon.py:1763`, called at `daemon.py:1002`) | new `[ERROR]` entries in `ui.log` via `uilog.read_errors` | `ui.log` got no `[ERROR]` |
| `invariants.check_ui_healthy` (`src/jarvis/invariants.py:3812`, `OS_INVARIANTS` member at `:4399`) | `uilog.recent_errors()` | same file, same silence |

A wedge produces no error, so silence is indistinguishable from health. That is the defect:
**the OS has no positive liveness signal from the dashboard at all** — it only has a
negative one, and the negative one was never written.

### Root cause: NOT identified. State that plainly.

A rig on the **exact installed versions** (anyio 4.14.1, starlette 1.3.1, fastapi 0.139.0,
uvicorn 0.49.0 — identical in dev and production) settled two things:

1. **DISPROVED: a leaked thread-limiter token from a client disconnect.** starlette 1.3.1's
   `BaseHTTPMiddleware` (`.venv/…/starlette/middleware/base.py`) has no disconnect-driven
   cancellation at all. 120 forced mid-handler disconnects — RST, FIN, raising handler,
   `StreamingResponse`, pre-header, cancelled-while-queued, and plain anyio cancellation
   with no HTTP in play — **all** settled at `borrowed_tokens 0 / available 40`.
2. **ESTABLISHED: the wedge's shape.** Every page handler in `app.py` is a sync `def` (e.g.
   `dashboard` at `app.py:983`), so every page goes through that one shared pool; the 1ms
   404s prove the event loop was healthy, which places the stall between "request accepted"
   and "handler body ran". A sync handler that never returns holds its token inside
   `CancelScope(shield=True)` (`anyio/_backends/_asyncio.py:2558-2559`), so no timeout can
   reclaim it — which is why only a restart helps.

The one surviving hypothesis is a **process-global lock or handle held for ever by a pool
thread** (kn-5b1be5c3; note `render()` opens a `NeoStore` per request at `app.py:926`).
Under that hypothesis the limiter is NOT saturated — 11 threads, 40 tokens — and the threads
are blocked on each other, not queued for capacity. Naming the holder needs **Python
stacks**, and `py-spy` could not attach (ptrace not permitted).

**So this work order does not fix the wedge. It makes the next occurrence explain itself,
answer while wedged, and recover without the user.** Anything that silently patched the
suspect paths now would be a symptom fix bought against an unnamed cause.

## The fix

Six pieces. Each one's location is load-bearing.

### 1. `GET /healthz` — an `async def` that never touches the threadpool

Where: `src/jarvis/ui/app.py`, inside `create_app()`, registered **before** the page
routes. Why there: it must be part of the app object `cli.py:5374` serves and
`TestClient(create_app())` builds, with no second entry point to keep in sync.

Rules that make it work at all:

- `async def`. A sync `def` would be queued onto the very pool it reports on and would hang
  exactly when it is needed. This is the whole point of the endpoint.
- Touches **no** store, no catalog, no template, no `render()` (which opens a `NeoStore`),
  no filesystem read that can block on a lock. Everything it reports is already in process
  memory, written by the probe task and the in-flight register (§2).
- Not routed through `render()`, so the chrome's badges cannot drag it into the pool.
- JSON response. `_quiet` (`app.py:286`) must cover it so a 5-second daemon poll does not
  bury the user's navigation in `ui-access.log`, exactly as `/api/status` is covered
  (`QUIET_PATHS`, `app.py:276`) — add the path to `QUIET_PATHS`. A FAILING probe response
  still logs, because `access_log` logs any status >= 400 (`app.py:974`).

Payload, at least:

| Field | Meaning |
|---|---|
| `pool_healthy` | bool: the last probe round PASSED — neither failure condition of §2 fired |
| `limiter` | `borrowed`, `available`, `total`, `waiting` from the anyio capacity limiter |
| `oldest_inflight_seconds` | how long the oldest in-flight ROUTED request has been in flight; null when none |
| `inflight` | how many routed requests are in flight |
| `probe_failure_reason` | which condition failed: `pool_timeout`, `inflight_stall`, or null |
| `last_ok_age_seconds` | age of the last passing probe round |
| `consecutive_failures` | failed probe rounds in a row |
| `uptime_seconds` | process uptime |
| `wedged_since` | first trip's time once a trip has happened (§3); null otherwise |

**Published fields are bounded by the endpoint being UNAUTHENTICATED.** No `version` (it
names the build to anyone who can reach the port) and **no stack-dump path** (it discloses
the state directory's layout). Neither is needed on the wire: the daemon and `jarvis doctor`
run in the OS's own process and read the dump path from `uilog.stack_dump_path()` and the
rest from the wedge stamp (§3). `probe_failure_reason` is a fixed enum, not free text, for
the same reason.

HTTP status: 200 when healthy, 503 when `pool_healthy` is false. The daemon must treat
"times out" and "answers 503" identically (§4), so the status code is a convenience for a
human with `curl`, never the only signal.

### 2. An in-process liveness probe with TWO failure conditions

Where: a background async task owned by a FastAPI **lifespan** on the app built in
`create_app()`, plus a register kept by an `http` middleware. Why lifespan and not
`cli.cmd_ui`: it must start for any server of this app and for `TestClient` used as a
context manager, and it must be cancelled on shutdown instead of outliving the server.

Every `ui_health.probe_interval_seconds` the task runs ONE round. A round FAILS if **either**
condition holds; both feed the same `consecutive_failures`, the same `ui_health.trip_threshold`,
the same stack dump, the same wedge stamp and the same daemon restart. They differ only in
the `probe_failure_reason` recorded.

**Condition A — pool round-trip (`pool_timeout`).** Run a trivial job through the threadpool
(`anyio.to_thread.run_sync` of a function that returns a constant — no I/O, no lock, nothing
that can fail for a reason other than the pool) under `ui_health.probe_timeout_seconds`.
Timeout fails the round. This catches a SATURATED limiter.

**Condition B — oldest in-flight routed request (`inflight_stall`).** If the oldest routed
request currently in flight has been in flight longer than
`ui_health.probe_timeout_seconds`, the round fails, whatever condition A said. This catches
everything that is not saturation: a process-global lock, a handle, a subprocess that never
returns, a pool thread blocked on another pool thread.

**Condition B exists because the measured fault was not saturation.** 11 threads against 40
tokens: condition A alone would have been granted a token at once and reported this process
healthy while every routed page hung — the detector would have missed the very fault it was
written for. A saturation-only detector is rejected in the alternatives below.

**This is NOT a guess at the cause.** Condition B measures the SYMPTOM the issue reports — a
routed request that never comes back — and says nothing about why. It fires identically for
the surviving lock hypothesis, for saturation, and for a cause nobody has thought of. The
"root cause: NOT identified" statement above stands unchanged: this spec detects, dumps and
restarts; it does not diagnose.

#### Where condition B's signal comes from, and why there

An `http` middleware (beside `access_log`, `app.py:1135`) registers each request **on the
event loop, BEFORE `await call_next`**, and deregisters it in a `finally`. The register is a
dict of request identity to a `time.monotonic()` start; "oldest in flight" is the minimum
start.

Why there and nowhere else:

- **Before `await call_next` is the only point a wedged request is ever seen.** Every
  existing surface records AFTER the response (`uilog.record_access` at `app.py:1152-1158`),
  and a wedged request has no response — that is exactly why all four surfaces were blind.
- **The event loop was provably healthy during the fault**: unrouted paths answered 404 in
  ~1ms while routed pages never answered. So a loop-side register, written by the middleware
  and read by an `async def`, is the one place in the process that survives the wedge. A
  counter maintained inside the threadpool, or anything that has to round-trip through it to
  be read, is gone with the threads it lives among.
- Both the register's writer and the probe's reader run on the single event loop, so no lock
  is needed to maintain it — and taking one would put a lock on the path whose job is to
  survive a lock.

#### ROUTED-ONLY, and `/healthz` excluded

Only requests that **match a route** are registered. Determine that in the middleware by
matching the scope against `app.router.routes` (`route.matches(scope)` returning a full
match) before registering — `request.scope["route"]` is not yet set, because the middleware
runs before routing.

Why routed-only is load-bearing: **the fault's own signature is unrouted paths answering
instantly while routed pages hang.** An unrouted path never reaches a handler and so can
never be the wedge; counting them would let a stream of 404s — a crawler, a stale bookmark,
the old reporter probing an endpoint that did not exist — be read as a wedge and trigger a
restart of a perfectly healthy dashboard.

`/healthz` is excluded from the register by name. It is routed and it is polled every few
seconds by the daemon; letting the detector watch its own reporting endpoint is a feedback
loop, not a signal.

#### Why the probe cannot make things worse

This is the safety argument and it belongs in the code comment too. For condition A: a token
is only taken once the limiter grants it. Cancelling an acquire that is still *waiting*
releases nothing because nothing was borrowed. The shielded hold at
`anyio/_backends/_asyncio.py:2558-2559` applies to a token already granted, i.e. to a job
already running in a thread — the probe's job is a `return`, so it never sits there. Worst
case under a wedge: the probe queues one more waiter, times out, and is cancelled; the rig
measured `borrowed_tokens 0 / available 40` after 120 cancellations of this exact shape.

For condition B: the register is two dict operations on the event loop per request and reads
nothing off the process. All state is plain module/closure state in the UI process; nothing
is written to a database from the request path.

### 3. On trip: dump every thread's Python stack

When `consecutive_failures` reaches `ui_health.trip_threshold`, the process writes **its own
stacks** — `faulthandler.dump_traceback` / `sys._current_frames()` over every thread — to a
file on disk, plus a small JSON wedge stamp (first-trip time, the limiter figures, the
in-flight figures, `probe_failure_reason`, uptime, version, the dump's path). This is the
only way to get the frames at all: the process can dump itself where py-spy could not
attach. Under condition B the stacks are the whole point — they are what names the holder the
root-cause analysis could not.

Where: **`src/jarvis/uilog.py`** owns both files, because it already owns `ui.log` and
`ui-access.log` and already is the module that both writes and **reads back** the
dashboard's own records (`ui_log_path()`:85, `access_log_path()`:89, `_append`:95). The
daemon and `jarvis doctor` must read the stamp, so it belongs in the module whose job is
reading these files back.

**Do NOT extend the `ui.log` `[ERROR]` format.** It is a parsed contract (kn-57fb4919) —
`_HEADER` at `uilog.py:57-60` with the column-0 rule documented at `uilog.py:19-22`, read by
`read_errors`, `Daemon.check_ui_log` and `check_ui_healthy`. A wedge is not an unhandled
exception, and smuggling one into that format would make every existing reader report a
traceback that does not exist. So: two **new** files beside the others under
`paths.logs_dir()`, with their own accessors and reader in `uilog.py`:

- `logs/ui-stacks.log` — **ONE file, not one per timestamp.** Every trip APPENDS a
  `[WEDGE]` note plus the full all-threads dump, and the file rotates at `uilog.MAX_BYTES`
  into one `.1` sibling exactly as `_append` does. Appending keeps **every** trip's
  evidence, which per-timestamp files would also do — but per-timestamp files have no bound,
  and a flapping wedge would fill the state directory. One rotating file keeps the history a
  comparison between trips needs and cannot grow without limit.
- `logs/ui-wedge.json` — the stamp, written whole via a temp file and `replace`, so a reader
  never sees half of it.

Writing must never raise into the probe task — same rule as `_append`'s swallow
(`uilog.py:108`): a failed dump must still leave the 503 answerable.

### 4. Detection and self-heal in the daemon

Where: a new reconcile-tick check on `Daemon`, called from `tick()` **immediately beside
`check_ui_log()` at `daemon.py:1002`** — i.e. before `route_new_inbox`, inside the same
`try/except` discipline (`daemon.py:1003-1004`: "never let the UI watch stall the tick"), so
a wedge found this tick is notified this tick instead of next.

Steps:

1. `GET /healthz` with the daemon's own HTTP timeout (`ui_health.probe_timeout_seconds` is
   the UI's; the daemon's own request deadline is `ui_health.healthz_timeout_seconds`).
2. **Timeout, connection failure and "reports wedged" are the same verdict.** A wedged
   process answers nothing at all if the loop went too, so a check that only believed a 503
   would miss the worse case.
3. Raise a `jarvis inbox` item (`central.add_inbox(project="os", level=…)`, the shape
   `check_ui_log` uses at `daemon.py:1797-1802`) naming **the stack-dump path** (from
   `uilog.stack_dump_path()`, not from the payload — §1), the `probe_failure_reason`, the
   limiter figures and `oldest_inflight_seconds`. Every restart raises one — no silent
   restarts, ever. The reason is on the item because "the pool is full" and "one request has
   been stuck for 20s with 29 free tokens" are different findings and want different reading.
4. Restart `release.UI_UNIT` (`"jarvis-ui.service"`, `src/jarvis/release.py:71`) via
   `release.SystemdRunner.restart_unit` (`release.py:230`). **Inline is correct here**:
   that method's own docstring restricts it to units that cannot host this process, and
   `jarvis-ui.service` never hosts the daemon (`DAEMON_UNIT = "jarvis.service"`,
   `release.py:72`). `restart_unit_detached` (`release.py:234`) exists for the opposite
   case and is wrong here. Go through the existing `self.release_runner` seam
   (`daemon.py:594`) — the one place systemd is spoken to, and the seam tests replace.
5. **Approved for the OS's own `jarvis-ui.service` and nothing else.** No unit name comes
   from a catalog key, no project may nominate a unit. A self-heal that can restart an
   arbitrary unit is a different and much larger authorisation than the one granted.

Caps, both of them, counters in `os_state` via `central.get_state`/`set_state`:

- **Cooldown**: no restart within `ui_health.restart_cooldown_seconds` of the last one.
  Stops a restart loop against a dashboard that wedges on boot.
- **Daily cap**: at most `ui_health.max_restarts_per_day` restarts in a rolling 24h.

**At the cap**: the check keeps probing, keeps dumping stacks, and raises an inbox item at
`level="critical"` saying the dashboard is wedged, that the daily cap of N is spent, that it
will **not** be restarted again until the window rolls, and where the dumps are. It must not
stop watching and must not fall silent — a cap that quietly disarms the only remaining
signal reproduces the original defect one level up. One item per entry into the capped
state, not one per tick (the `check_ui_log` rule at `daemon.py:1789-1790`: a loop must not
flood the inbox or Telegram).

### 5. An OS-level `jarvis doctor` check

Where: a new function registered in `invariants.OS_INVARIANTS` (`invariants.py:4398-4408`),
alongside `check_ui_healthy` (`:3812`). Why there and not in per-project `INVARIANTS`: the
per-project checks are predicates over one `ProjectStore` (`invariants.py:3800-3805`) and
there is no project that owns the dashboard — it is the OS's own process. Never repairable,
like every member of that tuple: `jarvis doctor` reports, the daemon heals.

Reports: that a wedge stamp exists and how old it is, which condition tripped, the
stack-dump path, how many self-restarts have happened in the window, and whether the daily
cap is spent. Reads the stamp through `uilog`, not by parsing `ui.log`.

### 6. Every tunable is a catalog key under `ui_health.*`

No module constants. Why: these are thresholds about what is NORMAL for a dashboard, and a
threshold that needs a release to change is one that gets left wrong — the same argument
`InspectConfig` records at `catalog.py:918-931`.

Model it exactly on the `inspect.*` block, which is the shape to copy:

- a `UiHealthConfig` dataclass beside `InspectConfig` (`catalog.py:916-953`), one
  `DEFAULT_UI_HEALTH_*` module constant per field, each with the reasoning in its `#:`
  comment;
- a `_parse_ui_health(raw, base, where)` mirroring `_parse_inspect`
  (`catalog.py:1903-1975`): **field-level inheritance** — `os.ui_health` parses against the
  shipped defaults, each project parses against the OS answer, so a project naming one key
  inherits the rest and no caller consults two objects (kn-6ca2bcd9);
- the field on both the OS config and `ProjectSpec`, as `inspect` is at `catalog.py:1426`
  and `:1562`;
- a resolver `ops.ui_health_config(project=None)` in the shape of `ops.inspect_config`
  (`ops.py:12619-12634`): best-effort, falling back to `UiHealthConfig()` rather than to
  `None`, because every default is a measured threshold and having none would mean having
  no probe;
- refuse absurd values where the message can name the key, as `_parse_inspect` does
  (`catalog.py:1965-1975`): every one of these is a count or a duration, so `>= 1`, no
  fractions and no money vocabulary.

Keys, defaults and the reasoning for each default:

| Key | Default | Why |
|---|---|---|
| `ui_health.enabled` | `true` | one switch for "probe and heal nothing". A second way to disable one thing is a second way to be surprised by it (`catalog.py:926-931`) |
| `ui_health.probe_interval_seconds` | `15` | matches `REFRESH_SECONDS = 15` (`app.py:143`), the rate the dashboard already loads itself at — a probe rarer than the traffic it watches learns about the wedge later than the user does, and one much denser adds pool traffic for nothing |
| `ui_health.probe_timeout_seconds` | `20` | ONE value, both conditions of §2. Must sit **above** the slowest legitimate page measured on the wedge day (`GET /cost 15424ms`), or a healthy-but-busy `/cost` build is reported as a wedge — that bound binds condition B hardest, since `/cost` is exactly an in-flight routed request. The real wedge went unanswered for minutes, so the detector loses nothing by waiting past the worst honest page |
| `ui_health.trip_threshold` | `3` | three consecutive failed rounds at 15s is ~45s of total page unavailability — past any `/cost` burst, far inside the ~4 minutes the real wedge went unanswered (05:38:27 to 05:42:58) |
| `ui_health.healthz_timeout_seconds` | `5` | the DAEMON's own deadline on `/healthz`. Small, because the endpoint touches nothing: anything slower than a few seconds is itself the finding, and the daemon must not spend a reconcile tick waiting |
| `ui_health.restart_cooldown_seconds` | `300` | one restart per 5 minutes. Longer than the whole measured outage, so the real case needs exactly one restart; short enough that a second genuine wedge the same hour is still healed |
| `ui_health.max_restarts_per_day` | `3` | a wedge took ~4 hours of uptime to appear, so three in 24h is well above the observed rate and the fourth is evidence of a different, worse fault that wants the user rather than another restart |

## Test strategy

Lives in `tests/test_ui_observability.py` — the file that already holds the UI-failure tests
(10 of them), with the `started` fixture (`:26`) and `TestClient(create_app())` (`:187`,
`:215`) already in place. The root `conftest.py` gate means no test can reach production
state, and `fake_systemd` / an injected `release_runner` means **no test touches real
systemd** (`src/jarvis/testing.py:1522` fake `systemctl`; `daemon.release_runner`,
`daemon.py:594`, is the seam — set it to a recording fake and assert on the argv it was
asked for).

1. **Wedge the pool deterministically (condition A).** Register a test-only sync route (or
   monkeypatch one page handler to a sync function) that blocks on a `threading.Event` the
   test owns. Fire `total_tokens` of the limiter's capacity worth of concurrent requests at
   it — read the capacity from the anyio limiter rather than hardcoding 40 — then assert
   `limiter.borrowed_tokens == limiter.total_tokens`. Set the event in a `finally` so a
   failing assert does not hang the suite.
2. **A wedge that does NOT saturate the pool (condition B).** The test review's demand, and
   the one that reproduces the MEASURED fault: a test-only sync route blocks on a
   `threading.Lock` the test holds, and the test fires **fewer concurrent requests than the
   limiter's `total_tokens`** — assert `limiter.available_tokens > 0` so the test proves the
   pool was NOT saturated. Then assert condition A alone passes (a pool round-trip returns
   at once), that `/healthz` nevertheless reports `pool_healthy` false with
   `probe_failure_reason == "inflight_stall"` and `oldest_inflight_seconds` past the
   timeout, and that the dump names the blocked frame (the test's route function and the
   lock-acquire frame). Without this test the detector can ship blind to the only fault that
   has actually been observed.
3. **`/healthz` answers while wedged.** With the pool saturated as in 1, `GET /healthz`
   returns within the test's own short deadline, status 503, `pool_healthy` false,
   `limiter.available == 0`, and a non-null `last_ok_age_seconds`. A second assertion earns
   its keep: a *sync* control route times out in the same state, so the test proves the
   `async def` is what makes the difference and will fail if someone converts the endpoint.
   Use `TestClient` as a context manager so the lifespan probe task actually runs. Assert
   the payload carries **no** `version` and no filesystem path (§1).
4. **Routed-only, and the dump file.** A burst of 404s to unrouted paths, plus a slow
   `/healthz` poll, leaves `inflight` at 0 and never trips — the regression guard on the
   rule that keeps a crawler from restarting a healthy dashboard. Then drive the probe past
   `trip_threshold` twice (inject a tiny `probe_interval_seconds`/`probe_timeout_seconds`
   through the config resolver, or call the probe's one-shot step directly) and assert
   `logs/ui-stacks.log` holds **both** `[WEDGE]` notes — one appended file, not two. Assert
   the wedge stamp carries the limiter figures, the in-flight figures, the reason and the
   dump path, and — separately — that `ui.log` gained **no** `[ERROR]` line, pinning the
   kn-57fb4919 contract.
5. **Cooldown.** With a fake `/healthz` reporting wedged, run two daemon ticks inside the
   cooldown and assert exactly one `restart_unit("jarvis-ui.service")` on the fake runner;
   advance the `os_state` timestamp past `restart_cooldown_seconds` and assert the second
   tick restarts. Assert `restart_unit`, never `restart_unit_detached`.
6. **Daily cap, and that the capped case still reaches the user.** Drive
   `max_restarts_per_day` restarts (stepping the cooldown each time), then one more tick:
   assert **no** further `restart_unit` call, assert a `level="critical"` inbox row naming
   the spent cap and the dump path, and assert a further tick still probes and still dumps
   while raising no second item. Then roll the window past 24h and assert restarting
   resumes. Plus: `jarvis doctor` reports the wedge and the restart count via
   `invariants.check_os()`.

## Rejected alternatives

- **A saturation-only detector (condition A alone).** The first cut of this spec. Rejected
  on the measurement: 11 threads against 40 tokens means the pool round-trip is granted a
  token immediately during the observed fault, so `/healthz` would have answered 200 while
  every page hung. A detector that misses the one fault on record is worse than none — it
  converts an unexplained outage into an outage the OS has certified as healthy.
- **Count every request in flight, not just routed ones.** The fault's signature is unrouted
  paths answering in 1ms while routed pages hang, so unrouted traffic is evidence of health,
  not of a wedge. Counting it makes any burst of 404s — a crawler, a stale bookmark — look
  like a stall and restarts a healthy dashboard.
- **Keep the in-flight register inside the threadpool** (a thread-local, a counter a sync
  wrapper maintains). It dies with the thing it measures: under the wedge every pool thread
  is parked, so nothing could write it and nothing could read it back. The event loop was
  measurably alive; that is where the register has to live.
- **Patch the suspect paths now** (hoist the per-request `NeoStore` at `app.py:926`, make
  `/cost` async, add SQLite timeouts). Rejected by the ruling and on merit: the root cause
  is not identified, the disproved hypothesis shows how convincing a wrong one looks here,
  and a symptom fix would also remove the conditions under which the next occurrence could
  be diagnosed. Revisit when a dump names the holder.
- **A watchdog that restarts on no-access-log-activity.** The access log is written *after*
  the response (`app.py:966-978`), so it is the surface that already failed; and quiet is
  indistinguishable from idle at 04:00.
- **`systemd` `WatchdogSec` / `Restart=on-failure` on the unit.** The process was `active
  (running)` and healthy by every signal systemd has. A real sd_notify watchdog would need
  the same in-process liveness probe this spec builds anyway, and would then restart with
  **no stack dump and no inbox item** — losing the two things the next occurrence needs.
- **Run page handlers in threads with a timeout / raise the pool size.** The token is held
  inside `CancelScope(shield=True)` (`anyio/_backends/_asyncio.py:2558-2559`), so a timeout
  reclaims nothing; a bigger pool only moves a saturation wedge later and does nothing at
  all for the lock hypothesis, which never needed the last token.
- **Make every page handler `async def`.** A plausible eventual fix and a large, risky sweep
  of `app.py` — every handler does blocking SQLite work, so it would move the blocking onto
  the event loop, where it takes the 1ms 404s down too. Out of scope, and premature before
  the cause is named.
- **Extend `ui.log` with a `[WEDGE]` record instead of new files.** The `[ERROR]` format is a
  parsed contract with three readers (kn-57fb4919). Separate files cost one accessor and
  break nothing.
- **One dump file per timestamp.** Keeps every trip, as appending does, but has no bound: a
  flapping wedge fills the state directory, and the directory the daemon and doctor read is
  the last place to run out of inodes. One appended file rotating at `uilog.MAX_BYTES` keeps
  the same history under a cap.
- **Module constants for the thresholds.** `probe_timeout_seconds` had to be derived from a
  measured page time; the next measurement will move it. A threshold that needs a release is
  one that stays wrong.

## What this spec does NOT cover

- The root cause. Named as unidentified, deliberately. Condition B measures the symptom the
  issue reports, not a hypothesis about why it happens.
- Any change to the page handlers, `render()`, the `NeoStore` lifetime, or `/cost`.
- Cancelling or timing out the stuck request itself. The token is shielded; nothing short of
  a restart reclaims it.
- Self-healing any unit other than `jarvis-ui.service`.
- A dashboard page for wedge history; `jarvis doctor` plus the inbox item is the whole
  reporting surface in this work order.
