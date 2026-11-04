# A project's config takes precedence over the fleet's

## The problem

Issue #1553. `projects.jarvis_os.evolution.bug_weekly_usd` was raised twice (150, 500,
1500), recorded by `jarvis config history`, and ignored: `jarvis steward ledger` kept
reporting `reported bugs $151.42 of $150.00 (os.evolution.bug_weekly_usd)` and the steward
held bugs under rule `bug-budget`.

Root cause: nothing connects "a project can write this key" to "the read site asks the
project". `_parse_evolution` resolved a project block, and `steward.gate`, `drain` and
`digest` read `catalog.os.evolution` anyway, as spec 2026-10-21 §3.3 then said to. Any
block can drift the same way, so the fix is the rule below plus a test that enforces it.

## The fix

A setting a project can write is read from the project's resolved block
(`ProjectSpec.<block>`, which inherits `os.<block>` field by field). A setting with no
project at its read site is REFUSED on a project at parse, so `jarvis config set` fails
with the reason instead of recording a value nothing reads.
`tests/test_config_precedence.py` fails on any new `catalog.os.<block>` read of a block a
project can override.

## The review

Every block on `ProjectSpec`, and where its fields are read.

| Block | Read from | Verdict |
|---|---|---|
| `evolution.*` | `steward.gate`, `drain`, `_reoffer`, `digest`, `cli.cmd_steward`; `ops._evolution_min_samples` | **Fixed.** Steward read `os.evolution` only. Now `steward.evolution_for(catalog, project)` |
| `fleet_health.cooldown_minutes` | `daemon._stuck_cooled` | **Fixed.** Read the fleet's with the project in hand |
| `fleet_health.max_per_day`, `sweep_dark_minutes` | `daemon._open_investigations`, `invariants` | Fleet-only: already refused on a project |
| `fleet_health`, the rest | `ops` stuck sweep, `daemon` via `project.fleet_health` | Honoured |
| `validation.timeout` in `INV-VALIDATION-STRANDED` | `invariants._validation_timeout` | **Fixed.** Closed a project's round on the fleet's number. Now `ops.validation_config_at` |
| `validation`, the rest | `ops.validation_config(project)`, `daemon` via `project.validation` | Honoured |
| `ui_health.*` | `daemon.check_ui_health`, `invariants`, `ui/app` | **Now refused on a project.** One dashboard per machine |
| `cost.meter_stale_minutes`, `meter_gap_notice_samples`, `meter_burn_warn_lead_minutes` | `invariants`, `daemon.notice_meter_gap`, `notice_burn_rate` | **Now refused on a project.** One account meter |
| `cost`, the rest | `fleetcost.cost_config(project)`, `daemon` via `project.cost` | Honoured |
| `worktree.reclaim_after_hours` | `disk.py` via `project.worktree` | Honoured. **Fixed** the low-disk message, which quoted the fleet's number |
| `worktree.tool_managed_paths` | `dispatch` via `project.worktree` | Honoured |
| `worker`, `gates`, `concision`, `bugs`, `release`, `schedule`, `observability` | via `project.<block>` or the `_at`/scoped accessors | Honoured |
| `inspect`, `navigation`, `messaging`, `pull_requests`, `rules`, `wiring` | scoped accessors in `ops` (`project=None` means the fleet's own view) | Honoured |
| `supervisor` | `daemon` via `project.supervisor`, `_supervisor_config` | Honoured |

Blocks with no per-project form (`os.neo`, `os.morpheus`, `os.autonomy`, and `os.defaults`
scalars such as `max_in_flight`) have nothing to take precedence over.

## Decisions (Neo, Q3444 and Q3445)

**A project's cap is measured against the fleet-wide ledger.** The candidate's project
supplies the cap, the mode and every threshold; spend stays the one fleet-wide figure. A
project with a lower cap can therefore be held back by another project's spend. A
per-project ledger is a separate decision, not taken here. `jarvis steward ledger` prints
every project's caps beside the single spend figure.

**Machine-wide settings are refused, not warned about.** Same pattern as
`FLEET_HEALTH_FLEET_ONLY_KEYS`. A warning can be missed; a refusal cannot.

## Consequences

`*.evolution.*` caps are safety keys at project scope too: a raise needs `--reason`.

Values already written on a project take effect on the release that ships this.

The onboarding interview's `ui_health` area moves to scope `os`, or its preset would write
the refused project spelling.
