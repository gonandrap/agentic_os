# The usage meter, and the spend Jarvis cannot see

Work order wo-7ec62134. Extends
docs/superpowers/specs/2026-10-07-cost-window-selector.md and
docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md. Build order, not an essay.
Deterministic, read-only except for one minute sampler. No model call anywhere in it.

## The problem

Jarvis reports what it spent. The account is rate-limited on what the MACHINE spent, and
nothing in the OS measures the difference — so every cost surface is a lower bound of
unknown size, presented as a total.

1. **The account's usage windows are not recorded anywhere.** Nothing in `src/jarvis`
   reads `utilization`, `resets_at` or the OAuth usage endpoint: the only hits for
   `claudeAiOauth` are `claude_cli.py:1550` (the token, for spawning `claude`) and
   `testing.py:2441` (its sandbox fixture). The three databases have no table for it —
   `central_store.SCHEMA` carries `projects`, `inbox`, `backlog`, `knowledge`,
   `os_state`, `agent_calls`, `gate_rules`, `detectors`, `remedy_rules`, `rule_fires`,
   `os_config_versions` and nothing about account utilisation. So "how close am I to the
   limit, and what pushed me there" is unanswerable from the record, at any range.
2. **The 5h window Jarvis reports is a GUESS, and today it was wrong by 80 minutes.**
   `fleetcost.session_window` (fleetcost.py:143-151) slices the usage week into 5h
   blocks, and `SESSION_ANCHOR_NOTE` (fleetcost.py:109-114) says so in the payload: "no
   table and no transcript records Claude's own session boundary". Measured 2026-10-08:
   `jarvis cost --window 5h` reported 04:00-09:00 PDT while the live window was
   05:20-10:20 PDT. Every per-window figure on the CLI and `/cost` was therefore summed
   over the wrong 5 hours.
3. **Spend on this machine that Jarvis did not spawn is structurally invisible.**
   `fleetcost.report` (fleetcost.py:1106-1205) sums exactly two populations: `wo_turns`
   rows joined to `work_orders` (`turn_rows`, fleetcost.py:339-356) and `agent_calls`
   rows with a non-empty `wo_id` (`_os_calls`, fleetcost.py:587-607). A transcript under
   `~/.claude/projects` that no work order and no `agent_calls` row points at is read by
   nothing. Measured 2026-10-08: interactive session `139cd19d`, in the `agentic_os`
   checkout, spent $2.48 of list-price tokens in 35 minutes and appears on no cost
   surface. It was burning the same 5h window as the fleet.
4. **There is no residual, so an accounting error and third-party usage are
   indistinguishable — both read as zero.** Because the payload's total IS the sum of its
   own two populations, it can never disagree with itself. A phone session, a claude.ai
   tab, another machine, and a bug in Jarvis's own attribution all produce the same
   output: a confident total.
5. **`jarvis cost` ignores its own window flags unless `--fleet` is passed.**
   `cli.cmd_cost` declares `--since/--until/--window/--offset/--tz` and passes them only
   on the `--fleet` branch (cli.py:3023-3028). The ordinary listing calls
   `ops.cost_report(project=…, target=…, limit=args.limit)` (cli.py:3049-3050) with NO
   window, while `/cost` calls `ops.cost_report(project=…, window=picked)`
   (ui/app.py:1473). So `jarvis cost --window 5h` silently windows nothing, the CLI
   listing is all-time where the page's is one window, and the `--json` payload carries
   no window block. This is defect 3 of the window-selector spec, fixed on the page and
   left broken on the CLI.

**Root cause** of 1-4, one sentence: Jarvis measures its OWN artifacts (turn rows, agent
calls, the transcripts of sessions it minted) and the limit it is actually governed by is
an ACCOUNT-level meter it never reads, so there is no second, independent number for the
first one to be reconciled against. 5 is a separate, smaller root cause: the window was
threaded into the page's two payload builders and into one of the CLI's two branches.

**Symptom this spec fixes on purpose, naming the root cause it does not.** Jarvis-side
spend is still attributed to a turn by timestamp interval, because `agent_calls` has no
turn id (fleetcost.py:587-607, that spec's §9). Finer windows make that attribution
finer-grained, not sounder. Likewise: the residual this spec introduces cannot be split
between "another device" and "a bug in Jarvis" — it is labelled as both, and narrowing it
needs per-device accounting Anthropic does not expose. Neither is in scope here.

## The fix

A mechanical minute sampler writes the account's two usage windows into `os.db`, and every
cost surface reconciles its span's measured spend against the meter's movement — Jarvis
spend, plus outside-Jarvis spend measured from the transcripts it does NOT own, plus a
named unexplained residual, said in one plain sentence.

Three decisions taken before this spec and not re-opened: Neo q1501 (`--window 5h`
re-anchors on the real boundary and names its anchor), kn-0c297bdd and kn-9ccc429b (one
window resolver, resolved once per surface, display zone resolved first and moving no
boundary), and the standing rider that nothing tunable is a module constant.

### 1. The sampler: `src/jarvis/usage_meter.py`

A new module, not a function in `daemon.py` and not part of `fleetcost`. It is the only
place that leaves the machine for a non-`gh`, non-Telegram reason; it owns one table, one
parser and the reconciliation arithmetic over its own time series. `fleetcost` is 1200
lines about per-ORDER distribution over a window and must not grow an account-level
network dependency; `daemon.py` holds cadence, not parsing. Layering: imports
`claude_cli` (the token), `db`, `paths`, `central_store`, `usage`, `nav_volume` — the
`adapters` tier, below `ops`.

```python
ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
BETA = "oauth-2025-04-20"            #: the `anthropic-beta` header the endpoint requires
FIRST_CLASS = ("five_hour", "seven_day")   #: the two windows with their own columns

class MeterError(Exception): ...     #: network, auth, or a payload that is not a meter

@dataclass
class Sample:
    ts: float; ok: bool; reason: str = ""; http_status: int | None = None
    latency_ms: int | None = None
    five_hour_pct: float | None = None; five_hour_resets_at: float | None = None
    seven_day_pct: float | None = None; seven_day_resets_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

def token() -> str                   # fresh, every call
def fetch(*, now=None, opener=None) -> Sample
def parse(payload: dict, *, ts: float) -> Sample
def record(central: CentralStore, sample: Sample) -> None
def sample_once(central: CentralStore, *, opener=None) -> Sample
```

**The token is read fresh on every call and never held.** `token()` reads
`claude_cli.credentials_path()` (claude_cli.py:1520-1524) and takes
`claudeAiOauth.accessToken` exactly as `claude_cli.py:1550` does — the token rotates, so a
cached one is a 401 waiting to happen. `CREDENTIALS_ENV` (`JARVIS_CLAUDE_CREDENTIALS`) is
honoured by that helper, which is what `testing.gate_environment` already redirects at a
sandbox file, so no test can reach the developer's sign-in. The token value never enters a
log line, an exception message, a timeline event, an inbox body or a payload key; the
failure reason records the SHAPE of the failure ("401 from the usage endpoint",
"credentials file has no claudeAiOauth.accessToken") and nothing read out of the file.

**The response body is never logged either.** This account's payload carries no secret —
`limit_dollars`/`used_dollars`/`remaining_dollars` are all null and `extra_usage.is_enabled`
is false — but another account's does, and a log line is forever. Only the reason is
logged.

**Transport**: `urllib.request.Request` with `Authorization: Bearer <token>` and
`anthropic-beta: BETA`, `urlopen(req, timeout=10)`, exactly the shape
`notify.sink_telegram` (notify.py:169-178) already uses, so the OS has one HTTP idiom.
`opener` is the test seam, injected the way `Daemon.release_runner` and
`Daemon.validator` are: no test performs a real request, and there is no network in the
suite.

### 2. The schema — two first-class windows and a JSON column for the rest

New table in `central_store.SCHEMA`. NOTHING in `ADDED_COLUMNS`: these are new tables, so
`CREATE TABLE IF NOT EXISTS` creates them in a live `os.db` too — the rule the
`detectors`/`remedy_rules` comment (central_store.py:470-472) already states. Central
rather than per-project because the samples are an ACCOUNT fact: no project owns them, and
N copies would be N chances to disagree.

```sql
-- One minute's reading of the account's usage windows, or one GAP ROW saying why there
-- is none. Account-level, so central: no project owns the meter.
CREATE TABLE IF NOT EXISTS usage_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,                       -- when the sampler READ the endpoint
    ok INTEGER NOT NULL DEFAULT 1,          -- 0 = gap row; every pct/resets column NULL
    reason TEXT NOT NULL DEFAULT '',        -- the failure in words; NEVER the body
    http_status INTEGER,                    -- NULL on a network error that never answered
    latency_ms INTEGER,
    -- The two windows that have existed under stable names since this endpoint did, and
    -- the only two anything reads. NULL on a gap row — never 0.0, which is a MEASUREMENT
    -- meaning "the window just reset".
    five_hour_pct REAL,
    five_hour_resets_at REAL,               -- epoch, parsed from the ISO string
    seven_day_pct REAL,
    seven_day_resets_at REAL,
    -- EVERY OTHER non-null window object, keyed by its top-level name:
    -- {"seven_day_opus": {"utilization": 12.0, "resets_at": 1760...}, ...}. The payload
    -- carries ~20 further keys and the set is NOT stable — `seven_day_oauth_apps`,
    -- `seven_day_sonnet`, `seven_day_cowork`, `seven_day_omelette` and about fourteen
    -- codenames (`tangelo`, `iguana_necktie`, `nimbus_quill`, `cinder_cove`, …), all
    -- null on this account today. A column per name would need a migration for every
    -- codename Anthropic invents, so they live here. `limits` is NOT stored: it is the
    -- same two numbers in a second shape and nothing reads it.
    extra_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_usage_samples_ts ON usage_samples(ts);
CREATE INDEX IF NOT EXISTS idx_usage_samples_ok ON usage_samples(ok, ts);
```

**What is and is not a schema failure.** A key going from null to an object — a new
codename, or Opus usage starting — is NORMAL: it lands in `extra_json` and records an `ok`
row. A failure, and therefore a GAP ROW, is exactly: the request did not complete, a
non-200 status, a body that is not a JSON object, or `five_hour.utilization` /
`seven_day.utilization` missing or non-numeric. `parse` raises `MeterError` with the
reason; `sample_once` catches it and records `ok=0` with that reason. A gap row is the only
honest answer: writing 0.0 would be read by the segment algorithm below as a window reset.

`extra_json` stores `{name: {"utilization": float, "resets_at": epoch}}` for every
non-`FIRST_CLASS` top-level value that is an object with a numeric `utilization`.
Everything else in the payload is dropped.

Retention: `usage_samples` grows by 1440 rows/day, ~50 bytes each — 26 MB/year. No pruning
in this spec; a year of minute samples is the calibration dataset §5 depends on.

### 3. The cadence — one tick constant, outside the project loop

```python
#: Read the account's usage meter every N ticks — one minute at the default 5s interval.
#: ONE MINUTE IS THE SPEC'S OWN UNIT and not a tuned number: the meter moves in whole
#: percent, and at the fleet's observed ~$0.82/point a minute is well under one point of
#: movement, so a coarser sample would put a reset in the middle of an interval and leave
#: the segment sum guessing. One HTTPS request per minute, ~1.4k/day, is nothing against
#: what one worker turn costs. Not catalog-configurable, for `PR_POLL_EVERY_TICKS`' reason
#: (daemon.py:101-108): how often the OS reads its own meter is not a decision anyone has
#: information to make — the ALARM thresholds in §8 are the settings.
USAGE_SAMPLE_EVERY_TICKS = 12
```

Fired in `Daemon.tick` **outside** the `for project in self.catalog.projects` loop, beside
`fleet.read` / `supervisor_tick` / `health_tick`, and before the loop: it is an account
fact, read once per tick like `fleet.read` (daemon.py:803-813), and a fleet with ten
projects must make one request, not ten. `Daemon.sample_usage_meter` wraps
`usage_meter.sample_once(self.central)` and swallows every exception into a gap row and a
`log.warning` — the daemon's one rule, that a failing pass must not take the tick down.

### 4. The segment sum — a window reset inside the span

Utilisation is monotonic within a window and drops to ~0 at `resets_at`, so
`end_pct - start_pct` under-reports by a whole window's worth whenever a reset falls
inside the span. The delta is therefore summed PER SEGMENT.

`usage_meter.segments(samples, *, since, until, key)` where `key` is `"five_hour"` or
`"seven_day"`:

1. Take `ok=1` rows with `since <= ts < until`, ordered by `ts`. Gap rows are skipped
   here and counted by `coverage` in §6 — a failed read is an unknown, never a value.
2. A BOUNDARY sits between consecutive samples `a, b` when `b.resets_at != a.resets_at`.
   That is the authoritative signal: the endpoint tells us when the window it is reporting
   changed. Secondarily, when `resets_at` is unchanged but `b.pct < a.pct - 1.0`, that is
   also a boundary, recorded with `cause: "drop"` and counted as an anomaly — a reset the
   endpoint reported late is still a reset, and the alternative is a negative delta.
3. For the FIRST segment, `start_pct` is the nearest sample at or before `since`, if one
   exists within `cost.meter_nearest_seconds` (default 300). Otherwise `start_pct` is the
   first in-span sample's own value and `coverage.head_uncovered_seconds` records how much
   of the span's head is unmeasured — spend before the first sample is then invisible, and
   the payload says so rather than pretending the span began at that value.
4. For EVERY LATER segment, `start_pct` is **0.0, by definition** — the window reset to
   zero at the boundary, and every point the new window has accumulated was accumulated
   inside this span. Using the first post-reset sample's value instead would silently drop
   whatever was spent between the reset instant and that sample.
5. `segment.delta = max(0.0, last_pct - start_pct)`. A negative delta is clamped and
   counted in `anomalies`; it means the samples disagree with the reset signal and the
   number must not go negative.
6. `delta_points = sum(segment.delta)`, `reset_count = len(segments) - 1`.

Both windows run through the same function. A 7-day reset inside a span is rare and is
handled by exactly the same code, which is why `key` is a parameter rather than two
functions.

### 5. Dollars per point, and its uncertainty

`usage_meter.dollars_per_point(*, now, cfg, home)` returns
`{value, low, high, source, basis_spans, basis_points, basis_usd}`.

Derived from Jarvis's OWN samples over spans where everything was visible. A CLEAN SPAN is
a maximal run of consecutive `ok=1` samples with:

* no gap between consecutive samples longer than 120s (two missed minutes),
* no reset boundary inside it,
* length >= `cost.meter_calibration_min_minutes` (default 30),
* 5h delta >= `cost.meter_calibration_min_points` (default 5),

within the last `cost.meter_calibration_days` (default 7). For each clean span, visible
spend = Jarvis spend (§7) + outside spend (§6) over that span, and the ratio is
`spend / points`.

`value` is the MEDIAN of the per-span ratios — the median and not the mean, because one
span containing an unseen phone session is an outlier that must not move the estimator.

**The uncertainty is quantisation, and it is stated as a band, never as a ± on a single
number.** The meter reports whole percent, so each span's point count is uncertain by ±1
point, and a 30-minute span that moved 5 points is a ±20% figure. With `n` contributing
spans, `low = total_usd / (total_points + n)` and `high = total_usd / max(total_points - n,
0.5)`. `source` is:

* `"measured"` — 3 or more clean spans. The band is printed.
* `"thin"` — 1 or 2 clean spans. The band is printed AND the sentence in §9 says
  "roughly", because a single span's residual is noise.
* `"seed"` — no clean span (a fresh install, or a span before the sampler shipped). Falls
  back to `cost.meter_dollars_per_point`, default **0.82** (measured on this machine
  2026-10-08: ~$0.82 of list-price spend per 5h point). Labelled as the seed everywhere it
  is shown, and never printed as a measurement.

### 6. Outside-Jarvis spend, and the ownership predicate

This is the crux. A session on this machine is OWNED BY JARVIS when its id appears in one
of exactly three records, and OUTSIDE otherwise.

| Owned because | Table, column | Where |
|---|---|---|
| it is a work order's worker session | per-project `work_orders.session_id` | project_store.py:629 |
| it is a work order's SPENT worker session (pre-headless orders forked a new id per turn) | per-project `work_orders.prior_sessions` (JSON list of ids) | project_store.py:1180 |
| it is one of Jarvis's own `claude -p` calls — Neo answers, panel seats, digests, `worker_subprocess` | `os.db` `agent_calls.session_id` | central_store.py:232, ADDED_COLUMNS at central_store.py:492 |

Projects are enumerated with `fleetcost.registered_paths(home)` and each one's database
with `paths.project_db_path`, the same walk `fleetcost.report` does (fleetcost.py:1140).
`wo_turns` has NO session column — `fleetcost.turn_rows` reads `w.session_id` off the
joined `work_orders` row (fleetcost.py:350) — so the per-turn table is not consulted and
the work-order row is the single source.

**Subagents need no record at all: a subagent is owned iff its LEAD is owned.** Claude Code
writes them at `<lead>.jsonl`'s sibling `<lead-stem>/subagents/*.jsonl`, which is a PATH
test, not a lookup — `nav_volume._subagents_of(path)` (nav_volume.py:301-306) is the one
pattern, reused rather than re-globbed so this report and `nav_volume.read_session` cannot
disagree about one session (PR 927 review).

**Explicitly NOT part of the predicate: the project slug.** A session created in a
registered project's checkout that Jarvis never minted is OUTSIDE. That is the whole point
— `139cd19d` lived in the `agentic_os` slug and is exactly the spend being missed.

`usage_meter.outside_sessions(*, since, until, home, index)` → `list[OutsideSession]`:

1. Walk the transcript root's project directories the way `nav_volume.read_tree`
   (nav_volume.py:372-419) does, with a **file mtime pre-filter**: a transcript not
   written since `since` cannot hold a call inside the span, and the tree is 2.7 GB /
   ~11,900 lead files. This is what makes the walk affordable per cost query.
2. For each lead `*.jsonl`, `session_id = path.stem`. Skip if owned. Then read the lead's
   calls with `usage.session_calls(session_id, index=index)` — which already merges every
   segment of a session and keeps subagents separate — and each subagent file's calls with
   `usage.calls_of`.
3. Filter by CALL TIMESTAMP, never by file: `since <= call.ts < until`. A long interactive
   session straddling the span contributes only the calls inside it.
4. Price with `usage.priced` / `usage.PRICES` and nothing else. No second price table
   exists in this spec, and the figures are therefore the same list prices the rest of
   `/cost` shows, with the same floor note.
5. Attribute the session to a project with
   `nav_volume.in_slug_scope(dir_name, slug, exclude=<the other catalog projects' slugs>)`
   (nav_volume.py:350-369), never a bare `startswith`: `slug_of` is not injective — `_` and
   `/` both become `-`, so `/ws/jarvis_os` and `/ws/jarvis/os` collide, and a bare prefix
   test folded a sibling project's sessions into its neighbour's report (PR 927 review). A
   directory matching no project is reported with `project: null` and its raw directory
   name, which is correct: it is spend on this machine outside every registered project.
6. Per session, report: `session_id`, `project_dir` (the raw slug), `project` (or null),
   `title` (the first user prompt, truncated to 120 chars — a new `usage.first_prompt`
   beside the existing `usage._is_prompt_row`, because `said_in_session` returns ASSISTANT
   prose), `models` (the distinct models seen), `calls`, token classes, `usd`, `first_ts`,
   `last_ts`.

**Dedupe.** `(message.id, requestId)` is already solved by one key:
`usage._assistant_messages` (usage.py:592-637) keeps one entry per `message.id` and takes
the MAX of each usage field, because a single assistant message is rewritten to the
transcript as its text grows. `requestId` is NOT needed as a second key — a genuine retry
is a new `message.id` and must be counted, while a repeated usage line shares the id and
is merged. This is reused, not reimplemented, and §10 pins it with a test.

### 7. Reconciliation, and the residual

Jarvis spend for the span, from the records it already has, with no new reader:

* **workers** — `fleetcost.turn_rows` over every registered project, summed as
  `fleetcost.report` already does;
* **jarvis calls** — the by-kind rows of `fleetcost.os_by_kind(since, until, project)`,
  which is how the OS's own calls are already attributed. The payload does NOT add
  `os_unattributed`: that line is a SUBSET of the by-kind rows, so adding it would count
  Neo's between-turn answers twice.

**The residual is computed FLEET-WIDE whatever the query's scope.** `jarvis cost
<project>` and `/cost?project=X` scope the LISTING; the meter is an account-wide reading,
and subtracting one project's spend from it would dump every other project's spend into
the residual and call it unexplained. So `workers_usd`, `jarvis_calls_usd`, `jarvis_usd`,
`implied_usd`, every share and `residual_usd` are always `project=None`. A scoped query
ALSO reports the project's own figures beside them — `spend.scope_project`,
`scope_workers_usd`, `scope_jarvis_calls_usd`, `scope_jarvis_usd`, null when unscoped —
never subtracted from the meter, and the sentence names them in one clause
("…; jarvis_os's own share of that is $3.10").

Then:

```
implied_usd  = delta_points(5h) * dollars_per_point.value
residual_usd = implied_usd - (workers_usd + jarvis_calls_usd) - outside_usd
```

`residual_share = residual_usd / implied_usd` when `implied_usd > 0`, else null.

The residual is LABELLED, never explained: "usage this machine cannot see (claude.ai in a
browser, the phone app, another machine, a cloud session), or an accounting error in
Jarvis". Both halves are said every time, in that order, because a reader who is told only
the first will never suspect the second. A NEGATIVE residual is reported as-is with the
same label: it means the meter moved less than the measured spend implies, which is
evidence about `dollars_per_point`, and clamping it to zero would hide the estimator
drifting.

### 8. Where the failure notice lives, and how it fires once

**The doctor check is derived from the rows, in `invariants.OS_INVARIANTS`**, beside
`check_cache_ttl_trigger` and `check_os_health_sweep_alive` (invariants.py:4397-4407) —
account-wide, so OS-level and not per project, and read-only like every check there.

```
INV-USAGE-METER-STALE — the account's usage meter has not been readable for N minutes.
```

It reads `usage_samples` and nothing else: the newest row, and the length of the current
trailing run of `ok=0` rows. Violation when the newest `ok=1` row is older than
`cost.meter_stale_minutes` (default 15 — fifteen consecutive failed minutes is a broken
token or a broken network, where three is a blip). `detail` carries the latest `reason`
verbatim and the first failure's timestamp, so the fix is writable without a second
investigation; `context` carries `{"last_ok_ts", "gap_rows", "reason"}`.

**The inbox item is raised by the daemon, once per failure streak, with the streak marked
in `os_state`.** `Daemon.sample_usage_meter` raises one `warning` inbox row when the
trailing gap-row run first reaches `cost.meter_gap_notice_samples` (default 10, ~10
minutes), and records `usage_meter_notice_streak` in `os_state` (via
`CentralStore.set_state`) holding the `ts` of the streak's first gap row. A later tick that
reads the same streak start says nothing; an `ok` sample clears the key, so the NEXT
outage notifies again.

**Why `os_state` and not a `pr_poll_warned`-style in-memory set** (daemon.py:588-592): that
set is reset by a daemon restart, which for the PR poll is also what fixes the condition.
Here it is not — a rotated token survives restarts, and a daemon in a restart loop would
re-notify every boot. The pinned rule applies: a gap the OS notices must be provable from
the record, never from in-memory state alone. The in-memory set is rejected for this
reason, and the invariant above derives from rows so it cannot go stale against a key
anyone forgot to clear.

**The residual/outside alarm is a different thing and fires from the supervisor path, not
from a cost query.** A read must never write. `Daemon.check_meter_residual` runs on its own
cadence inside the project loop, and only for the project `schedule.os_owner` names —
`check_cache_ttl`'s rule (daemon.py:4901-4973), for its reason: the reading is a FLEET
fact, and N projects raising the same fleet fault is N-1 alarms nobody reads.

```python
#: Reconcile the meter against measured spend every N ticks — 30 minutes at the default
#: 5s interval. The subject is a 5h window, so half-hourly is six looks per window: often
#: enough that a runaway interactive session is named while it is still running, rare
#: enough that a transcript walk over the fleet is not on a 30-second beat (the argument
#: `CACHE_TTL_EVERY_TICKS` makes, one order of magnitude in).
METER_RECONCILE_EVERY_TICKS = 360
```

It reconciles the CURRENT 5h window and raises at most one alarm per kind per window via
`store.last_alarm_of_kind` + `store.add_finding` + `central.add_inbox`, carried by
`store.latest_settled_order()` — again `check_cache_ttl`'s choice, and for its reason: the
subject is a session the OS never dispatched, so there is no order the number is about and
the carrier is the foreign key and nothing more. Two kinds:

| kind | fires when |
|---|---|
| `cost_outside_spend_high` | `outside_usd / implied_usd >= cost.meter_outside_alert_share` (default 0.25) |
| `cost_residual_high` | `abs(residual_share) >= cost.meter_residual_alert_share` (default 0.25) |

Both are gated on `implied_usd >= cost.meter_alert_min_usd` (default 5.0): a quarter of a
$1 window is 25 cents, and alarming on it would teach the user to ignore the alarm. Both
are also gated on `dollars_per_point.source != "seed"` — an alarm computed from a shipped
constant is an alarm about the constant.

The `cost_outside_spend_high` body NAMES the top three outside sessions by spend, with
session id, project dir and first prompt, so the user can go and stop them. All five
thresholds are `catalog.CostConfig` fields with field-level inheritance via
`catalog._parse_cost` — the repo's existing pattern (catalog.py:1042-1063), fleet-wide or
per project, and the rider that nothing tunable is a module constant.

### 9. The payload, and the plain-words sentence

One builder, `usage_meter.reconciliation(*, resolved, project=None, home=None, now=None)`,
returning one additive subtree under the key `meter`. Version 1 in its own `version` key,
the §10.6 rule the rest of the cost payload already follows.

```
meter.version                     1
meter.window                      {since, until, source}          # echoed from `resolved`
meter.five_hour                   {start_pct, end_pct, delta_points, reset_count,
                                   anomalies, segments: [{since, until, start_pct,
                                   end_pct, delta, cause}]}
meter.seven_day                   same shape
meter.coverage                    {samples, expected, share, gap_rows,
                                   longest_gap_seconds, head_uncovered_seconds,
                                   first_ts, last_ts}
meter.dollars_per_point           {value, low, high, source, basis_spans,
                                   basis_points, basis_usd}
meter.spend                       {implied_usd, workers_usd, jarvis_calls_usd,
                                   jarvis_usd, outside_usd, residual_usd,
                                   residual_share, outside_share, jarvis_share,
                                   scope_project, scope_workers_usd,
                                   scope_jarvis_calls_usd, scope_jarvis_usd}
meter.outside                     {total_usd, n, sessions: [{session_id, project,
                                   project_dir, title, models, calls, tokens, usd,
                                   first_ts, last_ts}]}
meter.timeline                    [{ts, five_hour_pct, seven_day_pct, ok}]
meter.sentence                    the string below
meter.alerts                      {outside: bool, residual: bool,
                                   thresholds: {...}, min_usd}
```

`sessions` is capped at `cost.meter_outside_rows` (default 10), ordered by spend
descending, with the remainder folded into `total_usd` and disclosed as `n` — the cap is on
ROWS SHOWN, never on dollars counted. `timeline` is decimated to at most 300 points
(mean of each bucket, `ok=false` if any sample in the bucket was a gap) so a 7-day span
does not ship 10,000 rows to a browser.

**THE SENTENCE IS BUILT ONCE, IN `usage_meter.sentence(...)`, AND RENDERED VERBATIM BY
BOTH SURFACES.** It is a payload key and not a renderer, for `NOTES`' reason: two
renderers producing two wordings of the same arithmetic is a difference the user has to
explain to themselves.

> the 5h meter rose 12 points (~$9.8); Jarvis spent $6.50 (66%), other sessions on this
> machine $2.48 (25%: session 139cd19d, agentic_os — "look at the cost page"), unexplained
> $0.80 (9%) — either usage this machine cannot see (claude.ai, phone, another machine) or
> an accounting error in Jarvis

Variants, all in the one builder: `source == "thin"` or `"seed"` inserts "roughly" before
the dollar figure and appends the basis ("from 2 measured spans" / "from the shipped
0.82/point estimate, not measured here"); `coverage.share < 0.9` appends "the meter covers
only 64% of this span, so the rise is a lower bound"; `reset_count > 0` appends "the 5h
window reset once inside this span, so the rise is summed per segment"; a window with no
`ok` sample at all produces "no usage-meter samples cover this span" and NOTHING else — no
derived figure, no residual, no percentages.

### 10. The surfaces

**`cli.cmd_cost` (cli.py:3018) resolves the zone, then the window, ONCE at the top**, in
that order (kn-9ccc429b: the display zone is resolved first and moves no boundary), the way
`ui/app.py:1468-1472` already does — `ops.cost_zone` then `ops.cost_window` — and passes
the one resolved dict to `ops.cost_report(window=…)`, to `ops.fleet_cost(resolved=…)` and
to the new `ops.cost_meter(resolved=…)`. The CLI and the page then read one population,
which is what Neo q1420 asked for.

Two consequences, stated because they are behaviour changes and not refactors:
`jarvis cost --window 5h` now reports a WINDOWED listing where it previously reported every
order with a surviving transcript, and the `--json` payload gains the window block it was
missing. Both are the CLI half of the window-selector spec's defect 3, and defect 5 above
is what they fix.

**The one-order branch is left alone.** `wo-`/`fo-`/`io-` still goes to `ops.bill`
(cli.py:3043) with no window and no meter block. A bill's span is the ORDER's own life, not
a window, and the residual is an account-level quantity over a span: printed under one
order's bill it would read as that order's unexplained spend, which is the single most
likely misreading of this whole feature. The meter belongs to spans, and the bill is not
one.

**`ops`** gains two thin wrappers beside `fleet_cost`/`cost_window` (ops.py:12306-12349),
lazily imported for their reason: `cost_meter(**kwargs)` → `usage_meter.reconciliation`,
and `meter_samples(**kwargs)` → the raw timeline for the page. No business logic in `ops`.

**`fleetcost.report` is NOT changed to carry the meter.** It would mean the page computed
the subtree twice (`cost_report` and `fleet_cost` are both called per request,
ui/app.py:1473-1480) and would put an account-level network-sourced time series inside the
per-order distribution payload. The meter is a third payload beside the other two, built
from the same resolved window — the `resolved=` pattern, applied once more.

**`resolve_window` re-anchors the 5h window on the real boundary (Neo q1501, answered
(a)).** `fleetcost.resolve_window(window="5h")` asks `usage_meter` for the latest sample
whose `ts` is within `cost.meter_nearest_seconds` of `now`; when there is one, the window
is `five_hour_resets_at - cfg.session_window_hours*3600` to `five_hour_resets_at` and
`source` is `"session-meter"`. With no such sample it FALLS BACK to the existing
`session_window` week-anchored guess and keeps `source: "session-window"`, so the payload
always names which anchor it used and `SESSION_ANCHOR_NOTE` is emitted for the fallback
only. The guess is not deleted: a fresh install has no samples, and a 5h window that
refused to resolve would be worse than one that says it is a slice of the week.
`offset < 0` with a meter anchor steps back in whole 5h multiples FROM the real boundary.

**`/cost`** renders a new `templates/_meter.html`, included by `cost.html` ABOVE the
listing — §5 of the ask: the sentence is the first thing read, not a row in a table. It
carries the sentence as one paragraph, the two deltas, the coverage share, the outside
session table (session id linking nowhere — these are not Jarvis records), and the
utilisation timeline as an inline SVG sparkline of `meter.timeline`, 5h and 7d as two
lines. The sparkline carries those two lines and NO work-order dispatch marks: lining a
jump in the meter up against what spent in that span is DEFERRED, not promised here.
Built in its own `try` in `cost_page`, exactly as the
distribution section is (ui/app.py:1476-1482): the meter reads a different table from the
listing, and losing it must not take the older, load-bearing half down.

### 11. Rejected alternatives

* **Scrape the meter from `claude`'s own output, or from `/status`.** No machine-readable
  form exists, it would mean spawning a `claude` process a minute (~1400 processes/day
  against an endpoint that answers in ~200 ms), and the parse would break on any CLI
  wording change. The HTTP endpoint is verified and typed.
* **Sample on the reconcile cadence (30s) or on demand at query time.** 30s doubles the
  requests for no resolution gain below one point of movement. On-demand is worse than
  wrong: the reconciliation needs a TIME SERIES over the span, and a query can only ever
  measure now — a span in the past would have no samples at all, for ever.
* **Store the whole response body per sample as JSON.** 20 mostly-null keys a minute, a
  payload shape nobody can query, and `extra_usage` may carry spend limits on another
  account — storing a body wholesale is how a secret ends up in a database that gets
  pasted into a bug report. Two typed columns per first-class window, one JSON column for
  the rest, nothing else.
* **Enumerate the per-model windows as columns** (`seven_day_opus`, `seven_day_sonnet`, …).
  Fourteen of today's names are unreleased codenames; each new one would cost an
  `ADDED_COLUMNS` migration for a column that is null on most accounts.
* **Attribute outside spend by project slug and call anything in a registered project's
  slug "Jarvis's".** This is the bug being fixed: `139cd19d` was in the `agentic_os` slug.
  Ownership is what Jarvis MINTED, recorded in three columns, and nothing else.
* **Write a second transcript reader for outside spend.** `nav_volume.read_tree` already
  walks the tree with the lead/subagent pair and the non-injective-slug trap solved, and
  `usage.session_calls`/`priced`/`_assistant_messages` already price and dedupe. A second
  reader would be a second set of prices to drift.
* **A `pr_poll_warned`-style in-memory set for the failure notice.** §8: a restart loop
  re-notifies, and the condition does not heal on restart.
* **Clamp the residual at zero.** It would hide a drifting estimator — the one thing the
  residual is diagnostic for.
* **Raise the attention item from the cost query itself.** A read that writes is a read
  that cannot be run twice; the supervisor path already owns alarms and dedupe.

### 12. What this spec does NOT cover

* No backfill. Samples begin when the release lands, so any span before that has
  `coverage.share == 0` and the payload says "no usage-meter samples cover this span". No
  figure is synthesised for history.
* No per-model reconciliation. `extra_json` is stored and nothing reads it yet; a per-Opus
  residual needs per-model pricing of the meter, which this spec does not attempt.
* No split of the residual between devices. Labelled, not attributed.
* `agent_calls` still has no turn id (§ above). Unchanged.
* No pruning of `usage_samples`.
* The bill (`jarvis cost <wo-id>`, `/cost/<project>/<id>`) is untouched.

### 13. Tests

New: `tests/test_usage_meter.py` (sampler + schema + segments + estimator),
`tests/test_usage_meter_outside.py` (ownership + the walk),
`tests/test_cost_meter_payload.py` (the subtree, the sentence, the surfaces).
Extended: `tests/test_ui_cost.py`. There is no `tests/test_daemon.py` and no
`tests/test_doctor.py` in this tree: cases 38-42 — the sampler cadence, the
once-per-streak gap notice and the `INV-USAGE-METER-STALE` invariant — live in
`tests/test_usage_meter.py`, and case 43 — the `os_owner` outside-spend alarm — lives in
`tests/test_cost_meter_payload.py`.
Fixtures reused, not invented: `testing.FleetCostFixture.transcript(session_id, rows,
subagents=…)` (testing.py:2960) and `.os_call(kind, ts=…, …)` (testing.py:2868) and
`.turn(...)`, under the `fleet_fixture` fixture (testing.py:2973). No test performs a
request: `fetch(opener=…)` is the seam, and
`testing.gate_environment`'s `JARVIS_CLAUDE_CREDENTIALS` keeps `token()` on a sandbox file.

Sampler and schema

1. A 200 with the measured payload shape writes one `ok=1` row with both utilisations,
   both `resets_at` parsed to epoch, and `extra_json == "{}"` when every other window key
   is null.
2. A payload with `seven_day_opus` as an OBJECT writes it into `extra_json` under that
   key and still records `ok=1` — a key going non-null is not a schema failure.
3. A payload with a NEW codename nobody has seen (`"quartz_lantern"`) lands in
   `extra_json` unchanged, no error, no gap row.
4. `limits` is present in the input and absent from every stored column.
5. **Gap rows**: a connection error, a 401, a 500, a non-JSON body, a missing
   `five_hour.utilization`, and a string `"44"` where a float belongs each write exactly
   one `ok=0` row with NULL utilisation columns and a non-empty `reason` — and `0.0` is
   never written.
6. The token is read fresh on each `fetch` (rotate the sandbox credentials file between
   two calls; the second request carries the new bearer).
7. No log record, no `reason`, no exception string and no stored column ever contains the
   token value or any key from the response body (`caplog` + a row scan, asserted against
   the sandbox token string and against `extra_usage`).

Segments and the reset

8. A span with no reset: delta == end − start.
9. **A 5h reset inside the span**, signalled by `resets_at` changing: two segments, the
   second starting at 0.0, `delta_points` == seg1 + seg2, `reset_count == 1`. The
   end-minus-start answer is asserted to be the WRONG one, explicitly, so a later
   refactor to it fails here.
10. A drop with `resets_at` unchanged: a boundary with `cause == "drop"` and
    `anomalies == 1`, delta still non-negative.
11. Two resets inside one span: three segments.
12. `since` falling between samples: the nearest earlier sample within
    `meter_nearest_seconds` is the start; beyond it, `head_uncovered_seconds > 0` and the
    first in-span sample is the start.
13. A 7-day reset inside the span goes through the same path with `key="seven_day"`.
14. Gap rows inside the span are excluded from the segments and counted in
    `coverage.gap_rows`; `coverage.share` is samples/expected and `longest_gap_seconds`
    spans the run.

Ownership and the outside walk

15. **A Jarvis-owned worker session is excluded**: a transcript whose stem is a
    `work_orders.session_id`, with its `subagents/` sibling, contributes $0 to
    `outside_usd` — lead AND subagent.
16. **An outside session is included**: a transcript no record names, in the SAME project
    slug as an owned one, appears with its spend, its first prompt as `title` and the
    project attributed.
17. A session in `work_orders.prior_sessions` (a pre-headless order's spent id) is owned.
18. A session in `agent_calls.session_id` (a Neo answer) is owned and not double-counted
    as outside while still appearing in `jarvis_calls_usd`.
19. A directory matching no registered project reports `project is None` and the raw
    `project_dir`.
20. **The non-injective slug**: two registered projects whose slugs collide by the
    `_`-vs-`/` rule attribute to the right one via `in_slug_scope(..., exclude=…)`, and a
    bare `startswith` is asserted to give the wrong answer.
21. **A span that cuts through a file**: one session with calls before, inside and after
    the span contributes only the inside ones, and the two outside calls are asserted
    absent from the dollar total.
22. **Dedup of repeated usage lines**: one assistant message written three times with a
    climbing `output_tokens` and an identical `usage` block counts ONCE, at the max; a
    genuine retry with a different `message.id` counts twice. Pins that `requestId` is not
    needed as a second key.
23. The mtime pre-filter skips a file older than `since` and the skipped file's spend is
    absent — asserted by making its calls fall inside the span, so only the filter can
    explain the absence.

The estimator

24. Three clean spans give `source == "measured"`, `value` the median of the ratios, and
    `low < value < high`.
25. One clean span gives `source == "thin"` and the sentence contains "roughly".
26. No clean span gives `source == "seed"`, `value == cost.meter_dollars_per_point`, and
    the sentence names it as not measured.
27. A span containing a reset, a span with a gap > 120s, a 10-minute span and a 2-point
    span are each excluded from the basis (one test per exclusion rule,
    `basis_spans == 0`).
28. Quantisation: `low`/`high` widen as the number of contributing spans grows for the
    same total points.

Payload, sentence, surfaces

29. The subtree's keys and `version == 1` are pinned verbatim (the contract test the rest
    of the cost payload already has).
30. The sentence for a fully covered span with a known outside session matches the shape
    in §9: points, implied dollars, three shares summing to 100% (±1 for rounding), the
    top session named, and both residual labels present.
31. A span with zero `ok` samples produces "no usage-meter samples cover this span" and
    NO derived figure, NO residual, NO percentages.
32. A negative residual is reported negative, not clamped.
33. `jarvis cost --window 5h` (no `--fleet`) now reports a windowed listing: an order
    outside the window is absent, where before the change it was present.
34. `jarvis cost --json` carries the window block and the `meter` subtree; `--fleet --json`
    carries the same `meter` subtree, from the same resolved window (asserted equal).
35. `jarvis cost <wo-id>` prints a bill with NO meter block (the §10 decision, pinned).
36. `/cost?window=5h&since=…` renders the sentence above the listing and the sparkline;
    `ops.cost_meter` raising leaves the listing rendered (the `try` boundary).
37. `resolve_window(window="5h")` with a fresh sample re-anchors on
    `five_hour_resets_at` and reports `source == "session-meter"`; with no sample it falls
    back to the week-anchored slice with `source == "session-window"` and emits
    `SESSION_ANCHOR_NOTE`. A regression case pins today's measured discrepancy: the
    meter-anchored window is 05:20-10:20 where the guess gave 04:00-09:00.

Daemon, doctor, inbox

38. `USAGE_SAMPLE_EVERY_TICKS` fires once per 12 ticks and once per TICK, not once per
    project: a two-project catalog makes one request.
39. A sampler exception leaves the rest of the tick running (every other pass still
    observed) and records a gap row.
40. **The repeated-failure notice fires ONCE**: 10 consecutive gap rows raise one inbox
    row, ticks 11-30 raise none, `os_state.usage_meter_notice_streak` holds the first
    gap's ts, an `ok` sample clears it, and a SECOND outage notifies again.
41. The notice survives a daemon restart: a new `Daemon` over the same `os.db` mid-streak
    raises nothing.
42. `INV-USAGE-METER-STALE` is silent with a fresh `ok` row, fires after
    `meter_stale_minutes` of gap rows with the latest `reason` in its `detail`, and is
    derived from rows only — asserted by firing it with no daemon ever having run.
43. `check_meter_residual` raises `cost_outside_spend_high` once per 5h window, only for
    the `os_owner` project, carried by `latest_settled_order()`, with the top three
    outside sessions named in the body; silent below `meter_alert_min_usd`; silent when
    `dollars_per_point.source == "seed"`; silent for a second project in the same catalog.
44. `jarvis cost` and `/cost` write NOTHING — no inbox row, no alarm, no sample (asserted
    by row counts across a query).
