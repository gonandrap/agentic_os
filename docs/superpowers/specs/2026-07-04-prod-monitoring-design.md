# Jarvis — Production Monitoring Design

Date: 2026-07-04
Status: v1 (spec) — designed autonomously; all decisions mirrored in `/ASSUMPTIONS.md` §I

## 1. Problem

Two of the user's projects run in production with real users: the virtual coach inside
`painforwisdom` and `auto_heycrypto` (live trading). Each grew its own "production
observer" — a per-project daemon that watches prod and tries to fix issues
autonomously — and both are unreliable in the same ways: the observer itself dies
silently, its communication channel rots, and nobody notices until a user does.

The OS should own everything that made those observers fragile — scheduling, liveness,
agent lifecycle, escalation, reporting — while each project keeps owning what only it
can know: *what "healthy" means* and *how to fix it*. This spec defines the handshake
interface between the two, and the OS infrastructure behind it.

## 2. Division of ownership

| | Owner |
|---|---|
| What to check, how often, what "unhealthy" means | **Project** (manifest + probe artifacts) |
| How to diagnose and fix each class of issue | **Project** (playbooks) |
| Running probes on schedule, forever | **OS** (jarvisd monitor scheduler) |
| Noticing that monitoring itself died | **OS** (watchdog, self-incidents) |
| Opening/deduping/closing incidents | **OS** (incident lifecycle) |
| Spawning the fix agent, worktree, review gates, PR | **OS** (existing work-order machinery) |
| Verifying the fix actually cleared the issue | **OS** (re-runs the originating probe) |
| Telling the user / Jarvis / Neo / UI what's going on | **OS** (inbox, attention, status, UI) |

The invariant: **no project logic ever lives in the OS package, and no scheduling or
lifecycle code ever lives in a project.** The manifest is the only coupling point.

## 3. The handshake: `monitoring/manifest.json` (interface v1)

A project opts in by committing a manifest to its repo and flipping
`"monitoring": {"enabled": true}` on its catalog entry. Default manifest path is
`monitoring/manifest.json`, overridable in the catalog (for repos that already have a
`monitoring/` directory of their own).

```jsonc
{
  "interface": 1,                          // handshake version; OS refuses unknown majors
  "defaults": { "model": "haiku", "timeout_s": 120 },
  "probes": [
    {
      "name": "api_health",
      "kind": "command",                   // deterministic: exit 0 = healthy
      "run": "scripts/health_check.sh",    // repo-relative, executed in the project dir
      "interval": "5m",
      "severity": "critical",
      "playbook": "monitoring/playbooks/api_down.md",
      "remediation": "auto"                // auto | propose | alert
    },
    {
      "name": "coach_quality",
      "kind": "agent",                     // judgment: a claude session runs instructions
      "instructions": "monitoring/probes/coach_quality.md",
      "interval": "24h",
      "model": "sonnet",
      "severity": "warning",
      "playbook": "monitoring/playbooks/coach_quality.md",
      "remediation": "propose"
    }
  ],
  "limits": {
    "max_concurrent_fixes": 1,             // per project
    "max_auto_fixes_per_day": 3,           // circuit breaker; excess → escalate
    "min_agent_interval": "1h"             // token-burn floor for agent probes
  }
}
```

Validated on `jarvis start` / `jarvis mon adopt` with the same clear-error style as the
catalog. `jarvis mon adopt <project>` scaffolds the directory (manifest + one example
probe + playbook template + a `MONITORING.md` explaining the contract); the user
reviews and commits it — the OS never commits into user repos.

### 3.1 Probe kinds

**`command`** — a script the OS runs in the project directory with a timeout. Exit 0 =
healthy; non-zero = unhealthy, stdout+stderr captured as evidence. Optional last stdout
line `fingerprint: <token>` groups distinct failure modes into distinct incidents.
Zero tokens; this is the workhorse (health endpoints, error-log greps, balance drift
checks, queue depth).

**`agent`** — for checks that need judgment ("read yesterday's coach conversations and
flag bad answers", "do today's trades look sane given the market?"). The OS spawns a
native background Claude session (same `claude --bg` path as workers, project settings
injected, so credential PreToolUse guards apply) with the project's instructions file
as the prompt plus a reporting contract. The probe ends its turn by calling:

```bash
jarvis mon report <run-id> --healthy
jarvis mon report <run-id> --unhealthy --summary "…" [--evidence-file f] [--fingerprint x]
```

Structured state flows through the CLI, never by parsing model prose — the same
principle as `jarvis wo assume`/`finish`. A probe session that ends without reporting
is an **error** (see §5), not a verdict.

### 3.2 Playbooks

A playbook is a project-owned markdown file: what this failure class means, where to
look, how to fix it safely, what "fixed" looks like, and any hard limits ("never
restart the live trader", "never touch `.env_prod`"). When an incident fires, the OS
builds the fix work order's prompt from: incident evidence → playbook → the standard
OPERATION.md worker contract. The playbook is the project's voice inside the fix
agent; the OS contributes only orchestration boilerplate.

## 4. OS infrastructure

```
jarvisd tick (existing)                        new monitor components
┌─────────────────────────┐   ┌───────────────────────────────────────────┐
│ dispatch_pending        │   │ monitor_tick:                             │
│ route_outbox            │   │  • run due command probes (subprocess)    │
│ deliver_messages        │──▶│  • dispatch due agent probes (claude --bg)│
│ neo_tick                │   │  • collect finished probe runs            │
│ reconcile_project       │   │  • incident engine (open/dedup/resolve)   │
└─────────────────────────┘   │  • remediation (fix WOs via dispatch.py)  │
                              │  • verification re-runs                   │
                              │  • watchdog (stale probes, dead runs)     │
                              └───────────────────────────────────────────┘
```

### 4.1 State (per-project `.jarvis/jarvis.db`, new tables)

- `probes(name, kind, enabled, interval_s, severity, remediation, config_json,
  last_run_ts, last_status, consecutive_errors)` — synced from the manifest each tick
  (manifest is the source of truth; DB adds runtime state).
- `probe_runs(id, probe, ts_start, ts_end, trigger, status, summary, evidence,
  fingerprint, session_id)` — `trigger`: `schedule | manual | verify`;
  `status`: `running → healthy | unhealthy | error | timeout`.
- `incidents(id, probe, fingerprint, severity, status, opened_ts, updated_ts, count,
  summary, evidence, fix_wo_id, escalated, resolution)` —
  `status`: `open → fixing → verifying → resolved | escalated | muted`.

Central `os.db` stays aggregation-only, as today: incidents surface through the
existing `inbox` and the status/attention rollups — no schema fork of truth.

### 4.2 Incident lifecycle

1. **Open/dedup** — an unhealthy run keyed by `(probe, fingerprint)`: no matching
   open incident → open one + inbox notification at the probe's severity; matching
   open incident → `count += 1`, `updated_ts` refreshed, evidence appended. Repeated
   failures never spam.
2. **Remediate** — per the probe's `remediation`:
   - `alert`: stop here; incident sits in attention until acked/muted.
   - `propose`: incident goes to attention with a prepared fix; the user (or Jarvis in
     conversation) launches it with `jarvis mon fix <incident-id>`.
   - `auto`: the OS creates the fix work order immediately (subject to `limits`),
     incident → `fixing`.
   The fix WO is a **normal work order** (`origin=monitor`): worktree, assumptions,
   `needs_review`, branch/PR per repo conventions. Prompt = evidence + playbook +
   OPERATION.md contract. Blocking questions go through `jarvis wo ask` → Neo, whose
   existing policy already escalates production/credentials/money matters to the user.
3. **Verify** — when the fix WO completes (and its review gate passes), incident →
   `verifying` and the OS re-runs the originating probe out of schedule
   (`trigger=verify`). Healthy → `resolved` (info notification, resolution recorded).
   Still unhealthy → reopen and escalate (critical attention item); auto-fixing for
   that incident stops — no fix-fail-fix loops.
4. **Escalate** — anything the machinery can't handle lands as `escalated`:
   verification failure, circuit-breaker trips (`max_auto_fixes_per_day`,
   `max_concurrent_fixes`), or a `critical`-severity probe configured `alert`-only.
   Escalated incidents are first-class attention items in `jarvis status --attention`
   and the UI, and page through the notification sinks (Telegram) at `critical`.

"Come up with a new version" means the fix ships as a reviewed branch/PR, exactly like
any other work order. **Deployment stays outside the OS in v1**: the playbook tells the
fix agent how the project deploys, and the review gate keeps a human before anything
reaches prod. Auto-deploy is explicitly out of scope until the loop earns trust.

### 4.3 The infra keeps itself alive (watchdog)

The defining failure of the current observers is that they die silently. Countermeasures:

- **Stale probe detection** — each tick, any enabled probe with
  `now − last_run > 2 × interval` raises a `monitoring_stale` self-incident
  (warning → critical if it persists). The monitor monitors the monitor.
- **Error ≠ unhealthy** — a probe that crashes, times out, or (agent kind) ends
  without `jarvis mon report` is an `error` run. Errors never open a *project*
  incident (no fix agent chasing a broken probe); `consecutive_errors ≥ 3` opens a
  `probe_broken` self-incident instead — remediation `propose`, playbook = the probe's
  own source, so fixing the monitoring is itself a work order.
- **Daemon liveness** — `jarvis status` already reports jarvisd down; with monitoring
  enabled that state upgrades to an attention item ("production monitoring is OFF").
  A documented systemd unit / cron `jarvis start` keepalive ships with the docs.
- **Heartbeat trail** — `probe_runs` is an append-only audit: the UI shows "last ran
  2m ago ✓" per probe, so *absence* of monitoring is as visible as failures.

### 4.4 Reporting upstream (Jarvis, Neo, UI)

- **`jarvis status`** gains a monitoring block per project: `probes 5 ✓ / 1 ✗ / 0
  stale · incidents 1 open (1 fixing)`. `--attention` includes escalated incidents and
  stale/broken probes. The Jarvis persona's pulse check (prime directive #2) therefore
  covers production automatically.
- **UI** — a **monitoring** tab: probe grid (last run, status, run history), incident
  list → incident detail (evidence, timeline of runs/fix WO/verification, mute/fix
  buttons calling the same ops functions as the CLI).
- **Inbox/sinks** — incident opened (at probe severity), resolved (info), escalated
  (critical → Telegram). All ack-able as today.
- **Neo** — untouched by design: fix workers reach Neo through the normal
  `jarvis wo ask` path, with the incident id in the question metadata for context.
  Monitoring adds no second Q&A channel.

### 4.5 CLI surface

```bash
jarvis mon adopt <project>                  # scaffold manifest + templates (user commits)
jarvis mon list [project]                   # probes, schedules, last status
jarvis mon status [--json]                  # monitoring rollup (same data as jarvis status block)
jarvis mon run <project>/<probe>            # force a run now
jarvis mon enable|disable <project>[/<probe>]
jarvis mon incidents [project] [--all]      # open by default
jarvis mon show <incident-id>               # evidence + timeline
jarvis mon fix <incident-id>                # launch the prepared fix WO (propose mode)
jarvis mon mute <incident-id> [--until ts]  # known issue, stop alerting
jarvis mon resolve <incident-id>            # manual close (recorded as such)
jarvis mon report <run-id> --healthy|--unhealthy …   # agent-probe reporting contract
```

Every command is a thin wrapper over `ops.py` functions, shared with the UI — the CLI
remains the OS.

## 5. Safety rails (auto_heycrypto is live money)

- Probes and fix workers run with the project's **injected settings** — the existing
  PreToolUse credential guards (`credentials.enc`, `key.key`, `.env_prod`) apply to
  monitoring agents identically.
- Fix workers operate in **worktrees** and end at a **reviewed PR**; nothing touches
  the running service. Restarts/deploys are not OS actions in v1.
- `remediation: auto` still passes through the `needs_review` gate whenever the worker
  records assumptions — autonomy in *starting* the fix, not in *shipping* it.
- Circuit breakers (`limits`) bound token burn and prevent runaway fix loops; every
  trip is an escalation, never a silent stop.
- Agent probes default to `haiku` and are floored at `min_agent_interval` — monitoring
  must be cheap enough to never be worth turning off.

## 6. Migration path for the two prod projects

1. `jarvis mon adopt painforwisdom` → move the coach-quality checks the existing
   observer performs into `monitoring/probes/*.md` (agent) and the uptime/error checks
   into `scripts/` (command); port its fix heuristics into playbooks; retire the
   bespoke observer daemon.
2. `auto_heycrypto` last, as per the fleet plan: its monitor daemon's checks become
   command probes (balance drift, order-flow sanity as agent probe), Telegram alerts
   route through the OS sink, and its "never touch prod" rules move into playbooks +
   the already-injected settings guards. Verify with a settings-superset diff before
   enabling, per the adoption rule.

Both migrations are user-driven (`mon adopt` scaffolds; the user ports logic) — the OS
provides the rails, the projects walk over.

## 7. Testing

- Unit: manifest validation, interval parsing, dedup/fingerprint logic, incident state
  machine, circuit breakers, watchdog staleness math.
- Integration (existing fake-`claude` fixture): a fixture project with a flappable
  command probe (touch a file → unhealthy) driving the full loop — open → auto fix WO
  (fake worker) → verify → resolve; agent-probe run reporting via `jarvis mon report`;
  stale-probe self-incident when the scheduler is frozen.
- Evals: extend the existing eval gate with a "monitoring pulse" scenario (Jarvis
  persona answers "how is prod?" from `jarvis status --json`).

## 8. Out of scope for v1 (backlogged)

- Auto-deploy of verified fixes (needs trust + per-project deploy contracts).
- Metrics/time-series storage (probe runs are events, not a TSDB; graphs later).
- Cross-project anomaly correlation ("both prod apps degraded — is it the host?").
- Webhook/push probe sources (external alertmanager → `jarvis mon report`); v1 is
  poll-only, though `mon report` is deliberately shaped to allow push later.
- Windows; UI auth (inherits MVP posture).
