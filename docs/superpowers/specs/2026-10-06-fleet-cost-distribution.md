# Fleet cost distribution — `jarvis cost --fleet`

Work order wo-38456776. Build order, not an essay. Deterministic, read-only, no model
call.

## The problem

`jarvis cost` can say what ONE order cost and which orders cost the most. It cannot say
what a TYPICAL order costs, so nothing in the OS answers "is this order an outlier" or
"did last week get dearer". Four concrete defects:

1. **No distribution anywhere.** `ops.cost_report` (src/jarvis/ops.py:12035) returns
   `units` sorted dearest-first plus `_rollup`'s SUMS (ops.py:12125). Sums with no n /
   avg / p90 / max. `cli.cmd_cost` (src/jarvis/cli.py:2714) renders exactly those sums.
   A reader looking at `total ~$203.67` has no way to tell 20 normal orders from 2
   runaway ones.
2. **No time window at all.** `cost_report(project, target, limit, include_hidden)` takes
   no `since`/`until`, and `CentralStore.agent_call_totals`
   (src/jarvis/central_store.py:1821) has no `ts` clause — it groups the whole table.
   "What did this usage week cost" is unanswerable today, even though `agent_calls.ts`
   is there.
3. **The `sub` column renders the wrong quantity.** NOT broken arithmetic. `sub` is
   `_unit_row`'s `subproc_cost_usd` (ops.py:11954 docstring, printed at cli.py:2756),
   which is `agent_calls.kind='worker_subprocess'` — bare `claude` processes a worker
   shelled out to. Fleet-wide that is **$0.57 for the whole week**, so the dashes are
   honest about what they measure. Task-tool SUBAGENTS are a different quantity:
   `_unit_row`'s `subagent_count` / `subagent_cost_usd` (ops.py:11980-11981) read from
   `usage.read_session(...).subagents` (`<session>/subagents/*.jsonl`). Those ARE
   computed, ARE in `jarvis cost --json`, and are **never printed in the fleet table** —
   cli.py:2801 prints `subagent_cost_usd` only in the footer totals, never per order.
   Measured on wo-6dcd484a: `sub` shows $0.39 while subagent spend is $3.12 of a $7.03
   worker bill — **44%**. The reader concludes subagents are free.
4. **Compaction payoff lives outside the product.** `scripts/compaction_payoff.py` has
   the only arithmetic that answers "did the compactions pay off" (`assess`:163,
   `summarise`:293) and `tests/test_compaction_payoff.py` imports it **by file path**
   through `importlib.util.spec_from_file_location` (test:19-23). A script under
   `scripts/` is not importable by `ops`, the CLI or the dashboard, so the answer cannot
   reach a user who did not read the repo.

Acceptance evidence (production `os.db`, `$JARVIS_HOME=/home/gonzalo/workspace/production/state`,
`agent_calls` over 2026-09-29 04:00 → 2026-10-06 04:00 UTC — one Claude usage week):
`compaction` 44 calls / $50.24 (the number the work order demands), `health` 408 /
$51.32, `validation_seat` 473 / $47.05, `neo_answer` 265 / $41.77, `digest` 379 / $9.75,
`supervisor` 20 / $2.97, `worker_subprocess` 139 / $0.57, plus `observe_*` kinds at $0.00.
Whole week: 14531 calls / $203.67.

Root cause of 1 and 2: the cost payload was designed as a *listing* (one row per order,
dearest first) and a listing has no place to put a population statistic or a window. The
fix therefore ADDS a section rather than re-shaping rows. Root cause of 3: two different
quantities ended up with adjacent names and only one of them reached the renderer.

## The fix

New flag `jarvis cost --fleet [--since D] [--until D] [--project P] [--json]`. The
existing per-order list stays the default and is untouched; `--fleet` adds one section.
One payload serves the CLI text render, `--json` and the dashboard cost page.

### 1. Where the code lives

**New `src/jarvis/fleetcost.py`.** Plain functions, each unit-testable on synthetic data.
Arguments for it over the alternatives:

* NOT `ops.py`: already ~15k lines, and it is the read/WRITE business layer — it
  constructs `ProjectStore` (ops.py:12083), whose `__init__` runs `_migrate()`
  (project_store.py:118). This report must open every DB **`mode=ro`** via
  `compaction_payoff.connect_ro`, so it must not touch `ProjectStore` at all. That
  constraint alone makes it a different module.
* NOT `usage.py`: a leaf that knows transcripts and prices and must never import a store.
* NOT `bill.py`: that module's contract is one order folded three ways that provably sums
  (`_fold`, `reconcile`, `seal`). A fleet distribution has no order to reconcile against.
* `fleetcost.py` sits where `bill.py` sits in the layering: imports `paths`, `usage`,
  `catalog`, `compaction_payoff`; imported LAZILY inside `ops.fleet_cost()` and inside
  `cli.cmd_cost`. No cycle.

`ops.fleet_cost(**kwargs)` is a four-line wrapper, exactly like `ops.bill` (ops.py:12016),
so the dashboard and CLI reach it the way they reach everything else.

**Move** `compaction_payoff`'s pure core to `src/jarvis/compaction_payoff.py`: `Case`,
`prefix_rate`, `_per_token`, `_turn_index`, `assess`, `analyse`, `summarise`, `_quantile`
(renamed **`quantile`**, public — `fleetcost` is its second caller), `gather`,
`_match_boundary`, `connect_ro`, `_parse_when`, `ROW_KEYS`, `TERMINAL_STATUSES`,
`MODEL_CONTEXT_WINDOW`, `MATCH_SLACK_SECONDS`, `RATE_*`. `scripts/compaction_payoff.py`
keeps only `_fmt_k`, `render_text`, `main` and the module docstring, plus an explicit
re-export line `from jarvis.compaction_payoff import (Case, ROW_KEYS, analyse, assess,
connect_ro, gather, prefix_rate, quantile, summarise, _parse_when)`.
`tests/test_compaction_payoff.py` passes **unchanged**: it loads the script by path and
uses only `cp.Case`, `cp.assess`, `cp.summarise`, `cp.connect_ro`, `cp.ROW_KEYS`, all of
which the re-export provides. Add one new test asserting `cp.assess is
jarvis.compaction_payoff.assess`, so a future edit cannot silently fork the arithmetic.
`--json` schema of the script stays `version: 1`.

### 2. Functions

All in `fleetcost.py`, all pure or read-only:

* `usage_week(now: float, cfg: CostConfig) -> tuple[float, float]` — the default window.
  No helper for this exists anywhere in the codebase (`claude_cli` parses the CLI's
  *reset sentence*, src/jarvis/claude_cli.py:1198-1326, which is a different thing). The
  Claude usage week resets Monday 21:00 America/Los_Angeles. VERIFIED: Mon 2026-09-28
  21:00 PDT = 2026-09-29 04:00 UTC, so the current week is 2026-09-29 04:00 →
  2026-10-06 04:00 UTC. Day / hour / zone are catalog settings (below), never literals,
  because a DST shift and an Anthropic policy change both move them.
* `window_of(since, until, cfg)` — flag values win; otherwise `usage_week`. Flags parse
  with `compaction_payoff._parse_when` (date or ISO datetime, naive = UTC).
* `turn_rows(db_path, since, until) -> list[TurnRow]` — `wo_turns` joined to
  `work_orders`, `started_at` inside the window, read-only. `kind IN ('dispatch',
  'message')` are turns; `kind='compact'` is counted separately and **never** as a turn
  (`project_store.TURN_KINDS`:374, `COMPACT_TURN`:378).
* `metric(values: Sequence[tuple[float, str]]) -> Metric` — the one place n / avg / p90 /
  max are computed. p90 is **nearest-rank** via `compaction_payoff.quantile` (ordered
  values, index `ceil(0.9*n)-1`): deterministic and always an observed value. `max`
  carries the wo-id holding it, from the tuple's second element.
* `per_order(rows) -> dict[str, OrderStats]`, `per_turn(rows) -> ...` — the two
  denominators, kept apart so no caller divides the wrong pair.
* `boundary_counts(session_id, floor)` — `usage.classify_boundaries` over
  `usage.read_session`'s calls; counts `BOUNDARY_TTL` and `BOUNDARY_PREFIX`
  **separately**. `cold_prefix_floor` comes from the catalog with no fallback, exactly as
  `bill._cold_prefix_floor` (src/jarvis/bill.py:1144) does.
* `subagent_share(session_id, floor)` — `usage.read_session(...).subagents.list_cost_usd`
  over the order's worker cost.
* `os_by_kind(since, until, project)` — windowed `agent_calls`. Needs
  `CentralStore.agent_call_totals` to gain optional `since=`/`until=` keyword args
  (additive; `None` keeps today's whole-table behaviour, so `cost_report` is unaffected).
* `report(...) -> dict` — assembles the payload below, including
  `compaction_payoff.summarise(analyse(gather(home, since=…, until=…, project=…)))`.
  **Do not reimplement that arithmetic.**

### 3. Metrics, their source and their provenance

Neo ruled (question 1289, Option A): turns, cost-per-turn, tokens-per-turn and
time-per-turn come from `wo_turns` — the OS's own recorded envelopes. Exact CLI numbers
via `usage_json` / `claude_cli.derive_turn_usage`, money via `cost_usd` labelled by
`cost_source`, wall clock via `started_at`/`ended_at`; and it survives transcript pruning.
Transcripts are read only for what `wo_turns` cannot say.

| metric key | source | provenance label |
|---|---|---|
| `turns_per_order` | `wo_turns` kind in (dispatch, message) | `wo_turns` |
| `cost_per_turn_usd` | `wo_turns.cost_usd` + the turn's share of `agent_calls` | `envelope` / `transcript` / `mixed` |
| `tokens_per_turn.{input,cache_read,cache_write,output}` | `wo_turns.usage_json` | `envelope` |
| `compactions_per_order` | `wo_turns` kind = `compact` | `wo_turns` |
| `ttl_expiry_per_order`, `prefix_miss_per_order` | `usage.classify_boundaries` | `transcript` |
| `seconds_per_turn` | `wo_turns.ended_at - started_at` | `wo_turns` |
| `cost_per_order_usd` | per-order sum of the above two money sources | `envelope` / `mixed` |
| `subagent_share` | `usage.read_session(...).subagents` | `transcript` |
| `rewrite_tax_share` | `wo_turns.usage_json` cache_write after turn 1, over order spend | `envelope` |
| `validation_rounds_per_order` | `validation_rounds` (project_store.py:867) | `record` |
| `neo_questions_per_order` | neo.db `questions.wo_id` (neo_store.py:210) | `record` |
| `os_cost_by_kind` | `agent_calls.kind` windowed | `record` |

Four labels, and what each means:

* **`envelope`** — `project_store.COST_FROM_ENVELOPE`: the `claude` CLI's own
  `total_cost_usd`. Exact.
* **`transcript`** — `COST_FROM_TRANSCRIPT`: `usage.cost_between`, a FLOOR derived from
  the transcript for a turn whose envelope never arrived (project_store.py:1002-1008,
  issue #471).
* **`mixed`** — the population contains both. Then the metric carries
  `"cost_basis": {"envelope": n, "transcript": n, "unrecorded": n}` and the renderer
  prints `mixed` beside the figure. The two are NOT the same currency and are never
  silently added (kn-e6bb1166). They ARE summed here — a population average of a
  partially-floored set is still the honest answer — but only because the counts travel
  with it. `unrecorded` is `cost_usd IS NULL`: a turn with no cost on record, counted as
  excluded, never as zero.
* **`record`** — a COUNT of rows the OS wrote (rounds, questions, calls). Not a currency
  at all; no cost_basis, no mixing question.

Rules that must be stated in the payload and in `--help`:

* **Attribution of jarvis-side spend to a turn.** `agent_calls` rows carry `wo_id` and
  `ts`; a row is attributed to the turn whose `[started_at, ended_at)` contains its `ts`.
  Rows that fall in no turn (between turns, or on an order with no in-window turn) are
  reported on the order, not on a turn, and counted in
  `metrics.cost_per_turn_usd.excluded.os_unattributed_to_turn`. Hence "attributed to the
  turn **where possible**".
* **Idle is excluded by construction.** `ended_at - started_at` is the turn's OWN wall
  clock, so time before dispatch, time waiting for a Neo answer between turns and time
  parked are all outside every interval. Nothing subtracts idle; there is none to
  subtract.
* **A still-running turn (`ended_at IS NULL`) is EXCLUDED** from `seconds_per_turn`, and
  the count of exclusions is reported (`excluded.running`). Never treated as zero
  duration. Its tokens and cost are excluded too when `cost_usd` is NULL.
* **`observe_*` kinds appear in `os_cost_by_kind`** with their real $0.00. Dropping a
  zero-cost kind would make a by-kind table that cannot be checked against
  `SELECT kind, count(*) FROM agent_calls`, and the point of the line is that it
  reconciles.
* **`--since`/`--until` filter TURNS, not order creation** (Neo's ruling). A per-order
  metric counts only that order's in-window turns; `n` = orders with at least one
  in-window turn. A long-running order is TRUNCATED, never dropped and never
  double-counted across windows. Live orders are included and MARKED (`live: true` on the
  order, `orders.live` count in the payload).

### 4. `--json` schema

Additive: `cost_report`'s existing keys are untouched. `--fleet` adds one key, `fleet`:

```
fleet: {
  version: 1,
  window: {since, until, label, source: "usage-week"|"flags"},
  scope: "fleet" | "<project>",
  orders: {n, live, truncated, excluded_no_turns},
  metrics: {
    "<metric key from the table above>": {
      n, avg, p90,
      max: {value, wo_id, project},
      unit: "usd"|"tokens"|"seconds"|"count"|"share",
      provenance: "wo_turns"|"envelope"|"transcript"|"mixed"|"record",
      cost_basis: {envelope, transcript, unrecorded},   // money metrics only
      excluded: {running, unrecorded, no_transcript, os_unattributed_to_turn}
    }
  },
  os_cost_by_kind: [{kind, calls, cost_usd}],          // dearest first, $0 kinds included
  os_unattributed: {calls, cost_usd},                   // agent_calls.wo_id NULL or ''
  compaction_payoff: { ...compaction_payoff.summarise() verbatim... },
  floor: true,
  floor_reason: ops.COST_FLOOR_NOTE,
  notes: [ "<the idle / nearest-rank / mixed-currency sentences>" ]
}
```

`metrics` is a dict keyed by metric name, not a list: the dashboard and a future skill
both want `metrics["cost_per_turn_usd"].p90` without a scan. Every caveat rides in the
payload (`floor_reason`, `notes`) rather than in the renderer, for the reason
`ops.COST_FLOOR_NOTE` already states at ops.py:12010 — a caveat in one renderer and not
another is one the reader learns to ignore.

### 5. Surfaces

* **CLI.** `sp.add_argument("--fleet", action="store_true")` plus `--since`/`--until`
  (`type=compaction_payoff._parse_when`) and `--project` at src/jarvis/cli.py:536-542.
  `cmd_cost` (cli.py:2714) branches BEFORE the existing render: `--fleet` prints the new
  section (one line per metric: `n avg p90 max(wo-id)`), then
  `os_cost_by_kind`, then the compaction-payoff block. `--fleet --json` prints the whole
  payload. Without `--fleet` nothing changes.
* **Dashboard.** `/cost` (src/jarvis/ui/app.py:1385) calls `ops.fleet_cost(...)` in
  addition to `ops.cost_report(...)` and passes `fleet=payload` to `cost.html`. New
  partial `ui/templates/_fleet_distribution.html` renders `fleet.metrics` as a table and
  `fleet.compaction_payoff` as a sub-block, above the existing per-order table. Same
  numbers, same keys, no second computation. The page already documents why it is a
  deliberate page rather than part of the 15s pulse (app.py:1389-1392) — that argument
  covers this section too.
* **Relabel the `sub` column** in cli.py:2753 from `sub` to `subproc`, and ADD a
  `subagent` column fed by the existing `u["subagent_cost_usd"]`. The column was not
  wrong, it was ambiguously named next to a quantity 5x larger that was never shown.
  `totals.subagent_cost_usd` already exists (ops.py:12170) and stays; no new total.

### 6. Catalog settings — every tunable, fleet-wide and per project

Neo's hard rider: no module constant for anything tunable. Follow `InspectConfig` /
`_parse_inspect` exactly (src/jarvis/catalog.py:858-896 and 1681-1753).

```python
@dataclass
class CostConfig:
    week_reset_weekday: int = 0            # Monday
    week_reset_hour: int = 21
    week_reset_zone: str = "America/Los_Angeles"
    percentile: float = 0.9                # the p90 basis
    max_orders: int = 500                  # cap on orders walked per project
```

* `_parse_cost(raw, base=None, where="os.cost")` with FIELD-LEVEL inheritance: `os.cost`
  parses against the shipped defaults, each project parses against the OS answer, so no
  caller consults two objects (the kn-6ca2bcd9 rule `_parse_inspect` cites).
* Refusals, kept apart the way `_parse_inspect` keeps its three vocabularies apart:
  `week_reset_weekday` in 0..6; `week_reset_hour` in 0..23 (**zero is legal** — the
  `>= 1` rule would reject midnight); `percentile` in `(0, 1)`; `max_orders >= 1`;
  `week_reset_zone` must construct a `ZoneInfo` or be refused naming the bad value.
* Registered on BOTH config objects: `OsConfig.cost` (beside `inspect` at catalog.py:1368)
  and `ProjectSpec.cost` (beside `inspect` at catalog.py:1241), and parsed in both places
  `_parse_inspect` is called — catalog.py:2156 for `os.cost` and catalog.py:2268 with
  `base=os_cfg.cost` for the project.

**What `jarvis config set <project> cost.<key>` needs.** Verified: the dotted-key path is
REFLECTIVE, so almost nothing. `ops.set_config` (ops.py:11265) writes the document, then
`_resolved_of` (ops.py:10995) runs `parse_catalog` and `config_version.resolve`, and
`resolve` (src/jarvis/config_version.py:130-143) flattens the dataclasses by reflection —
its docstring states a new block appears with no edit to that module. So:

1. Adding the dataclass + the two fields is enough for `config set` / `config get` /
   `config history` to see `cost.*`, and `_parse_cost` raising `CatalogError` becomes the
   `OpsError` the user sees on a bad value (ops.py:10999).
2. The one thing that MUST be added: an `APPLY_RULES` entry (ops.py:10863). Unmatched
   paths fall through to `"hot"` (ops.py:10915), which happens to be right — the report
   reads the catalog on every invocation — but `hot`'s note says "in force on the
   daemon's next tick" (ops.py:10896) and nothing in this feature runs on a tick. Add
   `("*.cost.*", "hot")` explicitly so the class is a decision and not an accident.
3. `safety_key` is correctly false for all of it: these change what a report SAYS, not
   what a worker may do. No `--reason` is forced.

### 7. Unit tests

New `tests/test_fleetcost.py`, all synthetic, no real state:

1. `test_usage_week_boundaries` — frozen `now` inside and just either side of
   2026-09-29 04:00 / 2026-10-06 04:00 UTC yields that week; a `now` one second before
   the reset yields the PREVIOUS week; a non-default `week_reset_hour` moves it.
2. `test_metric_nearest_rank_p90` — hand-checked p90 on n = 1, 2, 9, 10, 11; p90 is
   always one of the inputs; `max` carries the right wo-id; empty input gives
   `n: 0` with `avg`/`p90`/`max` null, never 0.
3. `test_compact_turns_are_not_turns` — a store with 3 dispatch/message turns and 2
   `compact` turns reports `turns_per_order` 3 and `compactions_per_order` 2.
4. `test_window_truncates_an_order` — an order with turns either side of the window
   contributes only the in-window ones, `n` counts it once, and
   `orders.truncated` is 1.
5. `test_running_turn_excluded_not_zero` — one `ended_at IS NULL` turn:
   `seconds_per_turn.n` drops by one, `excluded.running` is 1, the average does not move
   toward zero.
6. `test_mixed_cost_basis_labelled` — turns with `cost_source` `envelope` and
   `transcript`: provenance is `mixed` and `cost_basis` counts both; an all-envelope
   population reports `envelope`.
7. `test_ttl_and_prefix_reported_separately` — synthetic `usage.Call` sequence with one
   TTL boundary and one prefix miss: two distinct metrics, never summed.
8. `test_os_by_kind_includes_zero_cost_kinds` — an `observe_*` row appears with
   `cost_usd: 0.0`; a `wo_id=''` row lands in `os_unattributed` and not on any order.
9. `test_payload_keys_stable` — the `fleet` payload's key set matches a literal in the
   test, the guard against a renderer-driven rename.
10. `test_read_only` — point the loaders at a DB opened `mode=ro` and assert no write:
    `ProjectStore` is never constructed (so `_migrate` never runs) and the file's mtime
    is unchanged.

Fixtures needed: a `fleet_fixture` helper building (a) a project `jarvis.db` with
`work_orders` + `wo_turns` + `validation_rounds` rows at chosen timestamps and
`usage_json` blobs, (b) an `os.db` with `agent_calls` rows across the kinds above, (c) a
fake session transcript directory for the two transcript-only metrics. Build on the
existing `jarvis_home` autouse fixture and `jarvis/testing.py`; put the helper in
`jarvis/testing.py`, not in `conftest.py`, per `mem:testing`.

### 8. Rejected alternatives

* **Compute turns and cost from TRANSCRIPTS** (what `_unit_row` does today). Rejected by
  Neo on question 1289: Claude Code prunes transcripts on its own schedule, so the
  population would shrink silently, and transcript-derived cost is a floor where
  `wo_turns.usage_json` is the CLI's exact figure. Transcripts are used only for the two
  things `wo_turns` genuinely cannot say.
* **Reshape `cost_report`'s rows to carry the distribution.** Rejected: `units` is a
  listing contract already consumed by `cost.html` and `--json`; adding population
  statistics to a row is a category error and would break both renderers.
* **Window on order creation instead of turns.** Rejected by Neo: a 3-week order would
  be attributed entirely to the week it was created, making every window wrong in both
  directions.
* **Linear-interpolated percentile** (`statistics.quantiles`). Rejected: produces a value
  no order ever had, so `max`/`p90` could not both name a wo-id, and we would own a
  second percentile implementation beside `compaction_payoff`'s.
* **Reimplement the compaction arithmetic inside `fleetcost`.** Rejected explicitly by
  the work order: two copies of `assess` diverge, and the existing one has 20+ tests.
* **Module constants for the window and p90.** Rejected by Neo's rider — DST, policy
  changes and per-project norms all move them.
* **Keep `connect_ro` in the script and import the script.** Rejected: `scripts/` is not
  on the package path for an installed `jarvis`, which is exactly why the payoff numbers
  never reached a surface.

### 9. Deliberately NOT in scope

* **No new alarm or inbox item.** This is a report someone runs; nothing raises a
  distribution outlier. `jarvis inspect`'s alarms already own "raise it while it burns".
* **No change to `bill.py`, `ops.cost_report`'s existing keys, or the sealed-bill path.**
* **No fix for the floor itself.** Every figure stays a floor (`COST_FLOOR_NOTE`): a bare
  `claude -p` from a worker's shell still leaves nothing naming a work order. Carried as
  a caveat, not repaired here.
* **`agent_calls` has no turn id.** Attribution to a turn is by timestamp interval, which
  is a symptom-level fix: the root cause is that the OS does not stamp its own calls with
  the turn they were made during. Out of scope on cost, and the payload discloses what
  could not be attributed rather than hiding it. A follow-up adding
  `agent_calls.turn_id` would make that exclusion count go to zero.
