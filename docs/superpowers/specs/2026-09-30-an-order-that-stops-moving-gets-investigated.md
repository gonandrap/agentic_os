# An order that stops moving gets investigated

wo-9f00e3b5 Part B. Design settled by the user and by Neo questions 1073 and 1074 (both
approved). This document is the mechanism: the seams, the homes, the constants, the tests.

Companion: **docs/superpowers/specs/2026-09-30-why-no-check-caught-a-stuck-order.md**
(Part A) is the audit and the problem statement. Read it first; nothing here restates it.

## The problem

Part A is the evidence and this spec does not duplicate it. What Part B is answerable
for, in one line each:

1. `jarvis investigate` (docs/superpowers/specs/2026-09-27-investigation-orders.md) ships
   a diagnostician with **no caller but a human typing**. `ops.create_investigation_order`
   (src/jarvis/ops.py:8266) says so in its own docstring — "the companion fleet-health
   order calls this from the daemon" — and that caller does not exist. The seam was built
   for it in §2.7 and is unused.
2. Nothing in the OS asks "is this order not progressing?". The two shipped watchers each
   answer something else. `health.due` (src/jarvis/health.py:93) decides when to spend a
   model call to LOOK at a unit, whatever its state; `invariants.INVARIANTS`
   (src/jarvis/invariants.py:4355) is ~30 hand-written checks, each added after a specific
   thing broke. Neither is a time-in-state predicate, and `ops.state_durations`
   (src/jarvis/ops.py:1765) — which computes exactly the two numbers such a predicate
   needs — landed with **read-only callers only**: `jarvis wo show`, `jarvis fo show`,
   two dashboard pages, `rules.FACT_FIELDS`. Nothing acts on it. `project_store.py:747`
   already names the missing consumer: "`ops.state_durations`, and the fleet-health
   checker that consumes it".
3. Part A's incident table is the measurement of the cost: every order in it read as
   waiting on the user and was in fact a Jarvis gap (#786 stale panel hold, #788 an
   assumption "with Neo" on an unreachable question, #806 "no rounds left" after catch-up
   merges, #784 a release order overtaken). Each was found by a person, days late.

## The fix

A mechanical sweep, on its own daemon cadence, that reads time-in-state per open order
and opens an investigation order when an order is past a per-status threshold. **No model
call anywhere in the decision**, and no agent pre-screening the open list — the sweep is
arithmetic over rows the OS already writes, and the model spend is the investigation it
opens, not the deciding.

### 1. Home: `src/jarvis/stuck.py`, one pure function

New module. Shaped like `health.py`: nothing in it calls a model, opens a store, reads a
file or writes a row. `health.due` (src/jarvis/health.py:93) is the precedent and its
docstring gives the reason verbatim — "this function is the whole of the spend decision
and reading state inside it would make that decision untestable without a store".

Not `invariants.py`. Appendix A.9 of
docs/superpowers/specs/2026-09-27-investigation-orders.md:1152 rules that out and gives
the reason: an invariant answers "is THIS named gap class present?" and its output is a
remedy or an attention line; this answers "is something wrong here, and what is wrong is
not known?" and its output is a model session. A.9's words: "The two must not be merged".

The whole decision is ONE function over scalars:

```
stuck.assess(status, seconds_in_status, seconds_since_activity, discounted_seconds,
             thresholds, fallback_seconds, activity_statuses, excluded_cause) -> Verdict
```

* `Verdict` is a frozen dataclass: `stuck: bool`, `clock: str` (`"status"` or
  `"activity"`, which of the two was judged), `active_seconds: float` (the judged clock
  minus `discounted_seconds`), `threshold_seconds: float`, `excluded: str` (the hold that
  stopped the judgement, `""` when none), `reason: str` (one sentence, the sweep's `why`
  and the CLI's line both read it — one sentence, one place).
* `thresholds` is a plain `Mapping[str, float]` of seconds. `fallback_seconds` covers an
  open status the mapping does not name, so a status added to
  `project_store.WO_STATUSES` later is watched on the day it ships rather than silently
  unwatched.
* `activity_statuses` names the statuses judged on `seconds_since_activity` instead of
  `seconds_in_status`. Ships as `("running",)` — an order that entered `running` two
  hours ago and is typing is working, and only silence inside `running` is a symptom.
  Every other status is judged on time in status, because nothing is expected to happen
  inside it at all.
* `excluded_cause` non-empty short-circuits to `stuck=False`, `excluded=<cause>`. §2.
* Also exported: `stuck.fingerprint(...)` (§6) and `stuck.WHY` (§5), both pure.

Shaped for adoption, not adopted. fo-69ba1cc4 has FAILED, and what it left in the tree
is **half of what A.9 assumed**: `src/jarvis/rules.py` (the condition grammar,
`FACT_FIELDS`, `seed_rows`), `src/jarvis/gaps.py`, the `detectors` / `remedy_rules` /
`rule_fires` tables (src/jarvis/central_store.py:355) and `jarvis rules
list|show|retract|dry-run` all landed. What did NOT: any remedy primitive that opens an
investigation — `remedies.SHIPPED_REMEDIES` (src/jarvis/remedies.py:1360) is nine entries
and the closest is `file_work_order`. So build standalone. What the registry needs on the
day it adopts this, and what it must NOT need:

| the registry gets | where it already is |
|---|---|
| the predicate, over scalars | `stuck.assess` — callable from a facts pass with no store |
| its parameters as data | the `thresholds` mapping and `fallback_seconds`, already a `Mapping` argument, already catalog-resolved (§4) |
| one detector row per status | `rules.parse_condition` over `status eq <s>` + `gte` on a numeric field; `MAX_NODES = 32` is ample |
| one new `FACT_FIELD` | `active_seconds_in_status`, source slug `stuck` — the hold-discounted clock. `seconds_in_status` (src/jarvis/rules.py:211) is NOT it and must not be reused: it is raw, so a detector over it would fire on every order the usage window held |
| one new remedy primitive | `open_investigation`, `params={"why": str}`. `rules.validate_params` (src/jarvis/rules.py:878) already refuses an unknown primitive and switches its schema check on by itself |

What must NOT be needed: a refactor of `stuck.assess`. It takes no store, no config
object and no row, so the registry's facts pass can call it as a reader and the daemon
tick can call it directly, from the same signature.

### 2. The only exclusion, and time that does not count

**Excluded from being investigated at all, and their seconds discounted from every
threshold:**

1. **The Claude usage / session-limit hold.** `worker_session.PAUSE_USAGE_LIMIT`, carried
   verbatim in the `turn_paused` payload and named by `holds.TRANSPORT`
   (src/jarvis/holds.py:68). Fleet-wide it is `fleet.Fleet.shut()`
   (src/jarvis/fleet.py:252); per project it is `ProjectStore.health_sweep_hold`
   (src/jarvis/project_store.py:3567), whose docstring already makes the argument for
   reading it once per project rather than per candidate.
2. **A live `jarvis pause` fleet brake, and the post-reopen ramp hold.**
   `fleet.load_pause(central)` (src/jarvis/fleet.py:83) and `fleet.ramp`
   (src/jarvis/fleet.py:130). Decided by Neo 1074: both are excluded exactly as the usage
   limit is, and seconds inside either do not count.

Nothing is wrong in either case and both resume by themselves, so an investigation would
buy a $2.00 session (`catalog.DEFAULT_INVESTIGATION_BUDGET_USD`, src/jarvis/catalog.py:166)
to be told the OS is working as designed. The discount is the same arithmetic
`invariants._health_hold_seconds` (src/jarvis/invariants.py:3143) applies for
INV-OS-HEALTH-SWEEP-DARK, and the reasoning is `holds.py`'s module docstring: WALL is
honest about elapsed time, ACTIVE is "wall minus every interval the OS's own record says
the order was held", and a threshold belongs on ACTIVE.

**Nothing else is excluded.** Explicitly NOT excluded, and each is a decision:

* `PAUSE_AUTH` — an expired Claude Code sign-in. It does not clear itself, and an order
  parked on one for four hours is exactly the class Part A is about.
* `PAUSE_TRANSIENT` — a Claude API outage. It clears itself, but nothing bounds how long
  it takes, and "the API has been down for eight hours and no order has moved" is worth a
  session.
* `fleet_cap` — `max_in_flight` saturation. An order queued behind the cap for four hours
  means the fleet is oversubscribed, which is a finding.
* Every hold in `holds.HOLD_CAUSES` other than the usage limit: a gate awaiting a
  verdict, a question with Neo, the validation panel, an exhausted budget, an undelivered
  message. Part A's #788 was an assumption shown as "with Neo" on a question that could
  not be answered; discounting Neo time would have hidden it.

Two seconds sources, because they answer for different orders. For an order that has had
a turn, `holds.by_cause(holds.held(store, wo_id), since, now)`
(src/jarvis/holds.py:360/197) filtered to `{PAUSE_USAGE_LIMIT}` — already clipped to the
gaps between turns and already merged, so no double counting. For an order with no turn
at all (`pending`), `holds.held` returns nothing by construction and the project/fleet
hold above is the whole answer.

**One residual, stated rather than hidden.** `fleet.PAUSE_KEY` is a single central-state
key with no history: a pause that has been LIFTED leaves no record of when it began or
ended, so its seconds cannot be discounted afterwards — only a live pause can. The root
cause is that the brake is a flag and not an episode; the fix is a pause-episode row, and
it is OUT OF SCOPE here. Consequence while it stands: an order can be judged over
threshold on seconds a lifted pause caused. Mitigation is the cooldown (§6) plus the
`excluded` field on the run row, which makes the case visible on `jarvis stuck`.

### 3. It applies to every status, including the ones that look like waiting on you

**Blunt, because this is the rule a later reader will most want to "fix".** There is no
filter for "the user owes this one". An order in `waiting_input`, `needs_review` or
`budget_exhausted` is swept on exactly the same terms as one in `running`. Deciding
whether a user-owed wait is GENUINE is the investigation's job AFTER dispatch, and can
never be a filter before it, because the filter is precisely what Part A's incidents
defeated: every one of them read as user-owed and every one was a Jarvis gap. A sweep
that trusted the blocker sentence would have found none of them. Do not add the filter.
If a later change wants one, it needs Part A's table refuted first, not shortened.

For such an order `stuck.WHY` (§5) asks the investigator three things by name:

1. **Is the blocker true, current and correctly worded?** #786 is a `needs_review` order
   flagged unsatisfiable by a panel whose newest round had since PASSED.
2. **Is it the user's call, or is it the OS failing to decide?** #806 read "no rounds
   left" when what had happened was catch-up merges moving the head.
3. **Does the user have the reason and the link they need to decide?** A true blocker the
   user cannot act on is still a stuck order.

### 4. Config: a new `fleet_health` catalog block, shipped ENABLED

`catalog.FleetHealthConfig` beside `InspectConfig` (src/jarvis/catalog.py:794), parsed by
`_parse_fleet_health` copying `_parse_inspect` (src/jarvis/catalog.py:1535) exactly:
field-level inheritance against a `base`, `os.fleet_health` parsed against the shipped
defaults and each project parsed against the OS answer, so a project naming one key
inherits the rest. Wired at `parse_catalog` in both places the other blocks are —
`os_cfg` (src/jarvis/catalog.py:1976 band) and the per-project band
(src/jarvis/catalog.py:2065 band) — plus the `ProjectSpec` field
(src/jarvis/catalog.py:2103 band).

Shipped constants, as module-level `DEFAULT_FLEET_HEALTH_*` names:

| key | value | who set it |
|---|---|---|
| `enabled` | `True` | this spec — ships on |
| `thresholds["waiting_pr_merge"]` | 60 min | the user 2026-10-01, Neo 1148 (was 180, Neo 1073) |
| `thresholds["needs_review"]` | 60 min | the user 2026-10-01, Neo 1148 (was 240, Neo 1073) |
| `thresholds["validating"]` | 15 min | the user 2026-10-01, Neo 1148 (was 120, Neo 1073) |
| `thresholds["running"]` | 5 min, since last activity | the user 2026-10-01, Neo 1148 (was 120, Neo 1073) |
| `thresholds["pending"]` | 30 min | the user 2026-10-01, Neo 1148 (was 240, Neo 1073) |
| `thresholds["waiting_input"]` | 60 min | the user 2026-10-01, Neo 1148 (was 480, Neo 1073) |
| `thresholds["dispatching"]` | 5 min | the user 2026-10-01, Neo 1148 (was 30, this spec) |
| `fallback_minutes` | 30 | the user 2026-10-01, Neo 1148 (was 240, this spec) — covers `idle`, `budget_exhausted` and any status added to `WO_STATUSES` later |
| `cooldown_minutes` | 720 | Neo 1073 — UNCHANGED, Neo 1148 explicitly: one stuck order must not eat the burst |
| `max_per_day` | 48, fleet-wide | Neo 1148 (was 4, Neo 1073) |
| `sweep_dark_minutes` | 180, fleet-wide | this spec §8 — moved off `invariants.STUCK_SWEEP_DARK_MINUTES` into the catalog, same value |

**THE RULING BEHIND THESE NUMBERS — the user, 2026-10-01, accepted by Neo 1148.** Thirty
minutes of idle is unacceptable; FIVE minutes of idle — no tool call, no script running,
no usage-limit hold — means something is wrong. `running` is still judged on time since
the last ACTIVITY and not since entry (`stuck.ACTIVITY_STATUSES`), which is what makes a
five-minute number safe for a turn that is working: a long turn that keeps calling tools
is never over threshold. The earlier, looser figures were Neo 1073's and are superseded.

Every open status in `project_store.OPEN_STATUSES` (src/jarvis/project_store.py:65) is
therefore covered: seven named, `idle` and `budget_exhausted` on the fallback. `idle` is
a `kind='manager'` order's designed steady state — it is swept anyway, and the
fingerprint (§6) is what keeps a quiet manager to ONE investigation rather than one per
sweep.

Three refusals, each where the message can name the key:

* `_parse_inspect`'s `>= 1` floor applies to every scalar. `thresholds` is a mapping and
  is excluded from the reflective loop by a named constant — `FLEET_HEALTH_MAP_KEYS =
  ("thresholds",)`, `INSPECT_FRACTION_KEYS`' arrangement (src/jarvis/catalog.py:786) —
  then validated per entry: an unknown status is REFUSED naming `OPEN_STATUSES`
  (`GateConfig.parse`'s rule: a typo must not silently leave a status unwatched), and a
  value below 1 is refused.
* `thresholds` inherits **per status**, not whole: a project naming one status keeps the
  fleet answer for the other eight. `probes.resolve`'s merge-by-id rule
  (src/jarvis/probes.py), for its reason — a project disabling one threshold must not
  drop the rest.
* `max_per_day` and `sweep_dark_minutes` on a PROJECT are refused, each naming
  `os.fleet_health.<key>`. They are fleet numbers and no arrangement of per-project
  numbers can express either — `fleet.py`'s opening paragraph, verbatim in intent. One
  named set carries both, `FLEET_HEALTH_FLEET_ONLY_KEYS`, mapping each key to the reason
  the refusal states, rather than a per-key check. `sweep_dark_minutes` is fleet-only for
  `max_per_day`'s reason: the stuck sweep writes ONE fleet-wide run record
  (`Daemon.STUCK_RUN_KEY`), so no arrangement of per-project numbers can say how long that
  one record may be silent, and `check_stuck_sweep_alive` reads it off `catalog.os` the
  way `Daemon.stuck_tick` reads `max_per_day`.

**Neo's one condition: no second dollar knob.** The per-investigation spend is
`worker.investigation_budget_usd` via `budget.investigation_default_for`
(src/jarvis/budget.py:651), which `ops.create_investigation_order` already applies when
`budget_usd` is None (src/jarvis/ops.py:8323). The sweep passes NO `budget_usd`. There is
no `fleet_health` money key and there must not be one: `max_per_day` rations the number
of sessions, `investigation_budget_usd` rations each one, and two answers to "what may
this cost" is how a ceiling stops being checkable.

### 5. The daemon seam and its cadence

`Daemon.stuck_tick(state: fleet.Fleet | None)` beside `Daemon.health_tick`
(src/jarvis/daemon.py:3942), called from `tick()` immediately after `self.health_tick(state)`
(src/jarvis/daemon.py:954), taking the tick's ONE `fleet` reading for the reason
`health_tick` takes it: the account's window is a fleet fact read once per tick.

**On the main thread, no pool.** `health_tick` needs `self.health_pool` because a sweep
makes model calls; this one makes none — its whole cost is indexed reads — so it runs
inline, on `schedule_tick`'s pattern (src/jarvis/daemon.py:856). No `health_sweeping`
flag, no done-callback, no thread-local store question.

**Cadence: `fleet_health.sweep_every_ticks`, default 60** — 5 minutes at the default 5s
`poll_interval` (src/jarvis/daemon.py:508). **Neo 1086 OVERRIDES this section's original
module constant**: the cadence is a per-project catalog config, resolved like every other
`fleet_health` field, because the user has turned down the module-constant precedent before
and prefers a config per project. `Daemon.stuck_cadence()` is the FINEST cadence any
enabled project asked for — that is what `tick()` gates on — and `Daemon._stuck_due` is
what keeps a coarser project on the number it asked for. Why 5
minutes and not the 30 this section first shipped (Neo 1148): a 30-minute cadence cannot
see a 5-minute idle — detection would lag the threshold by up to six times the threshold
itself. Why not every tick: the pass costs one `ops.state_durations` per open
order per project — one indexed read per table plus one `stat()` per transcript file
(src/jarvis/ops.py:1772) — plus one `holds.held`, which is two more indexed reads. That
is `PR_POLL_EVERY_TICKS`-class work, and at 5 minutes it still runs less than half as
often per hour as the pull-request poll already does (`PR_POLL_EVERY_TICKS = 24`, two
minutes).

What the tick does, in order. Each step is a refusal or a read, and the expensive one is
last:

1. If no project has `fleet_health.enabled`, return. Nothing is opened, no store, no row.
2. Read the fleet-wide daily count: investigations with `origin='fleet_health'` created
   in the last 86400s, summed over projects (§6). **Derived from the records, never a
   counter** — `fleet.Outage`'s rule (src/jarvis/fleet.py:154): no column, no flag,
   nothing to drift.
3. If `state.shut()` or a live `fleet.load_pause` / `fleet.ramp`, do the reads and open
   NOTHING, recording the run with `opened=0` and the exclusion named. A paused fleet
   must not have the sweep filing orders — and recording the run is what keeps §8's
   invariant from calling a deliberately quiet sweep dark.
4. Per project, per order in `list_work_orders(statuses=OPEN_STATUSES)`: skip
   `origin in UNGOVERNED_ORIGINS` (the user's own session, `_health_candidates`' rule,
   src/jarvis/daemon.py:3980); skip `kind in ("investigation", "investigator")` and any
   work order whose `parent_id` names an investigation. `ops.create_investigation_order`
   refuses those anyway (src/jarvis/ops.py:8301) but the refusal must not be how the
   sweep learns it — see §6.
5. Assess each with `stuck.assess`. Collect the `stuck` ones, **most overdue first** —
   `active_seconds - threshold_seconds` descending. The opposite sort to
   `_health_candidates`' longest-unreviewed-first (src/jarvis/daemon.py:3963), on purpose:
   that cap is a rotation and this one is a daily spend cap, so the cap must cut the
   least broken order rather than starve the worst.
6. While under `max_per_day`: the two per-subject refusals (§6), then
   `ops.create_investigation_order(project_name, subject, why=stuck.WHY.format(...),
   origin="fleet_health", fingerprint=fp)` — **a direct Python call**, never a subprocess
   shelling out to `jarvis`. §2.7 of the investigation-orders spec built that seam for
   this caller, and its docstring names it.
7. Record the run row (§8), inside a `try/except` that puts the exception's text on the
   same row. One broken project must not stop the rest — `fleet.read`'s per-order
   `log.exception` rule (src/jarvis/fleet.py:310).

`origin="fleet_health"` is a new member of `project_store.WO_ORIGINS`
(src/jarvis/project_store.py:256), which is a closed set asserted on insert
(src/jarvis/project_store.py:2814). It earns its place on the same grounds the comment
gives `schedule`: this is an order NOT EVEN A MODEL decided to file — arithmetic did — so
"why am I paying for this" is unanswerable without the origin. It does NOT join
`UNGOVERNED_ORIGINS`: the investigator is dispatched with a full briefing like any child.

`stuck.WHY` is a module constant in `stuck.py`, formatted from the verdict: the subject
id, its status and label, both ages, the threshold it passed, the blocker sentence
`invariants.true_blockers` derived, and §3's three questions. It must be a template and
not prose composed at the call site, for the reason `create_investigation_order` refuses
an empty `why` (src/jarvis/ops.py:8294): the investigator's first reader is a fresh
session with no memory of the sweep.

### 6. Three rate limits

**(a) One live investigation per subject.** `ops._live_investigation`
(src/jarvis/ops.py:8346) already refuses it, and `create_investigation_order` raises
`OpsError` when it hits. The tick must not spend the attempt to learn it: an exception per
already-investigated order per sweep is a log the operator learns to ignore, and it is
indistinguishable from a real failure on the run row. So promote it to public
`ops.live_investigation(project_name, subject) -> str` — same body, the private name kept
as an alias — and the tick calls it first. The refusal inside
`create_investigation_order` stays exactly as it is: it is the floor for every other
caller.

**(b) A cooldown keyed on the situation, not the clock alone.** A fingerprint recorded on
the investigation's own row at dispatch, so there is no new table:

```
stuck.fingerprint(status, status_since, blocker, event_count) -> str
```

`health.SEP`-joined, readable, not a hash — `health.fingerprint`'s own rule
(src/jarvis/health.py:55): the only question ever asked of it is inequality, and a
readable value is diagnosable by eye. Four parts: the status; `int(status_since)`, which
moves only on a status change; the blocker sentence (one of
`invariants.true_blockers`' own, which is what #786 was wrong about); and a count of
non-observer timeline events, `pstore.count_events(id, exclude=…)` computed by the CALLER
and passed in, so `stuck.py` stays pure.

Why not `health.fingerprint` itself: it is a WO-shaped summary built for a different
question (it counts turns, pending assumptions and queued messages) and it is computed
from a store, so reusing it would drag a `ProjectStore` into a pure module. What IS reused
is its correctness rule — `health.observer_kinds` (src/jarvis/health.py:34), "a
fingerprint that counted any of them would MOVE AS A RESULT OF BEING LOOKED AT". The
verdict event §7 adds to the subject's timeline is exactly such an event, so its kind
JOINS `observer_kinds()`. Miss that and the cooldown can never engage.

Why not a new table: `feature_orders.metadata` already carries `SUBJECT_KEY`
(src/jarvis/ops.py:8264) resolved at creation for the same reason — it is what the
refusals are about. Add `ops.STUCK_FINGERPRINT_KEY = "stuck_fingerprint"` and one keyword
`fingerprint: str = ""` on `create_investigation_order`, merged into the same metadata
dict at src/jarvis/ops.py:8318. One call site, no migration, and `jarvis investigate
show` can render it.

The read: `ops.last_stuck_investigation(project_name, subject) -> dict | None`, beside
`live_investigation`, the newest investigation of ANY status on that subject carrying the
key. The sweep refuses when **either** holds: `now - settled_at < cooldown_minutes * 60`,
or the fingerprint equals the recorded one. So an order whose state and blocker have not
changed is never re-investigated, however long it has been; and one that HAS changed
still waits out the 720 minutes, because "it moved" and "it is better" are not the same
claim.

**(c) The fleet-wide daily cap.** `max_per_day = 48` (Neo 1148; it was 4, Neo 1073),
counted per §5 step 2 over
`origin='fleet_health'` investigations created in the trailing 86400s. Needs one new
indexed reader, `ProjectStore.count_feature_orders(kind, origin, since)`, beside
`list_feature_orders` — a COUNT, not a listing filtered in Python, because the listing
walks every investigation the project has ever had.

**Why 48 and not 4.** At the user's five-minute sensitivity a cap of 4 is spent in the
first hour and then fails CLOSED exactly when a genuinely stuck order appears.
`worker.investigation_budget_usd` is the real money ceiling (Neo's one condition, above),
so this cap rations BURSTS rather than the day. The per-subject `cooldown_minutes` stays
at 720 — Neo 1148 was explicit — so one stuck order cannot eat the burst.

**An investigation or investigator order is NEVER a subject.** Three layers, and that is
deliberate: `create_investigation_order` refuses it (src/jarvis/ops.py:8301), the sweep
skips it (§5 step 4), and the test in §9 asserts it from the sweep rather than from `ops`.
"Diagnosing the diagnostician is a loop with a budget attached", and the loop here would
be driven by a timer rather than by a person typing.

### 7. Surfaces

**One reader, two renderers.** `ops.stuck_report(project_name=None, now=None) ->
list[dict]`, pure and write-free, returning per open order: id, project, title, status
and label, `seconds_in_status`, `seconds_since_activity`, `discounted_seconds`,
`active_seconds`, `threshold_seconds`, `stuck`, `excluded`, `reason`, `fingerprint`, and
the live-or-last investigation (`id`, `status`, `classification`). The CLI and the page
both read this document and neither computes a number — `jarvis wo why`'s rule, stated at
src/jarvis/cli.py:281.

**`jarvis stuck [project] [--json] [--limit N]`**, `cmd_alarms` (src/jarvis/cli.py:2489)
in shape: a count line, then over-threshold rows marked `!`, then the rest as the record.
Each row: id, project, status label, time in status, time since activity, its threshold,
its classification, and the investigation if there is one. Footer names the next command
— `jarvis investigate show <inv-id>`, or `jarvis rules list` for the registry view.

**A `/stuck` dashboard page**, `alarms_page` (src/jarvis/ui/app.py:1595) and
`ui/templates/alarms.html` as the precedent, including its split: the top is the queue
that is an ask, the bottom is the record and is meant to be long. `active="stuck"` in the
nav beside `alarms`.

**The Evolution view is LINKED, not built.** That page is fo-69ba1cc4's and it did not
land — there is no `/rules` route in `src/jarvis/ui/app.py`, only the `jarvis rules`
verbs. So the stuck view carries the pointer as TEXT (`jarvis rules list --gap-class …`)
and gains an `href` the day the route exists. No dead link, and no second Evolution view.

**The verdict must appear on the SUBJECT's timeline.** It does not today.
`ops.submit_verdict` writes `verdict_submitted` onto `fo["plan_wo_id"]` — the
INVESTIGATOR's own work order (src/jarvis/ops.py:8523) — which nobody reading the stuck
order will open. Smallest addition, in the same `store` block at src/jarvis/ops.py:8511,
right after `set_feature_status(inv_id, "completed")`: when the subject is a work order in
this project, one `store.add_event(subject, "investigation_verdict", {"investigation":
inv_id, "classification": …, "filed": …})`. Guarded by `KeyError` like the
`plan_wo_id` reads around it, since a subject can be deleted under it. No `timeline.py`
table edit is required — `timeline.event_level` returns `signal` for an unknown kind
(src/jarvis/timeline.py:101) — but the kind gets a `_describe` line so it renders a
sentence rather than a raw token, and it JOINS `health.observer_kinds()` per §6.

### 8. The sweep's own failures are a first-class alarm

The sweep must never be the thing that is stuck. Two pieces, both minimal.

**One run row.** `central.set_state("stuck_sweep_run", json)` —
`{"ts", "scanned", "candidates", "opened", "skipped": {…}, "excluded", "error"}`. One key
holding the NEWEST run, not a table: `CentralStore.set_base_health`
(src/jarvis/central_store.py:1817) is the precedent for a fleet-shaped fact in `os_state`
and gives the reason. `error` is `""` on a clean run, which is what clears the violation.

**One invariant: `INV-STUCK-SWEEP-DARK`**, in the per-project `INVARIANTS`
(src/jarvis/invariants.py) and therefore run by the daemon's reconcile tick as well as by
`jarvis doctor`. It reads a fleet-wide fact — the central run row — and still takes a
`ProjectStore`, returning at once unless `_os_owning_project` says this store is the
project that runs the OS, so it fires on exactly one project per tick and the sweep is
reported once. `check_os_health_sweep_alive` (src/jarvis/invariants.py:3167) is BOTH the
SHAPE this copies (named causes, one `Violation`, `level="critical"`, **not repairable**)
and the registration precedent, for the same reason.

**It was in `OS_INVARIANTS` and review round 1 rejected that**, correctly: `OS_INVARIANTS`
is run by `jarvis doctor` alone, and only `Daemon.check_invariants` writes the
`violation_reports` rows `ops.os_status` builds its critical attention items from. A
failing or dark sweep would therefore have reached no push surface and left `jarvis status`
reading HEALTHY — the raised-but-invisible class this spec exists to close.

Two causes, named because they have two different fixes:

* `failing` — the newest run carries an `error`. Detail quotes it, clipped.
* `dark` — `fleet_health` is enabled for at least one project and the newest run is older
  than `os.fleet_health.sweep_dark_minutes`, default 180, or there has never been one AND
  THE DAEMON IS UP — a stopped daemon sweeps nothing by construction and `jarvis status`
  already says so, so firing there would report a fresh install as broken. A CATALOG
  SETTING AND NOT A MODULE CONSTANT: the user's standing rule is that no threshold a
  surface judges by is a module constant, so the `STUCK_SWEEP_DARK_MINUTES` this section
  first shipped is gone and `DEFAULT_FLEET_HEALTH_SWEEP_DARK_MINUTES` carries the same
  180. It is fleet-only (§4). 180 is `OS_HEALTH_SWEEP_DARK_MINUTES`' value
  (src/jarvis/invariants.py:3103) and its reasoning holds here: a daemon restart or one
  capped tick cannot trip it, and a sweep switched off by a bad edit is named the same
  working day.

No `disabled` cause, unlike the OS sweep's: `fleet_health.enabled=false` is a legal
choice for a project, and there is no user rule saying otherwise. Not repairable, for
`check_os_health_sweep_alive`'s stated reason — a failing sweep's cause is not derivable
from state. No hold discount is needed: the sweep makes no model calls, so a usage window
cannot silence it, and a fleet pause is recorded as a RUN with `opened=0` (§5 step 3)
rather than as no run at all.

### 9. Tests

Extend existing files. Every assertion named; the fixtures are `jarvis.testing`'s
(`jarvis_home` is autouse, `project`, `fake_claude`).

| test | file | asserts |
|---|---|---|
| `test_usage_limit_hold_is_not_stuck` | `tests/test_health_sweep.py` | an order whose only hold is `PAUSE_USAGE_LIMIT`, over threshold on raw wall clock: `stuck_tick` opens NO investigation, and `stuck.assess(...).excluded == PAUSE_USAGE_LIMIT` |
| `test_held_seconds_do_not_count` | `tests/test_health_sweep.py` | same order after the window reopened: `active_seconds == seconds_in_status - held`, and it is not stuck until `active_seconds` alone passes the threshold |
| `test_stuck_order_gets_exactly_one_investigation` | `tests/test_investigation_orders.py` | one order 5h in `waiting_pr_merge`: first `stuck_tick` creates one `kind='investigation'` with `origin='fleet_health'`; a second tick creates none and the live count stays 1 |
| `test_cooldown_holds_until_the_fingerprint_changes` | `tests/test_investigation_orders.py` | with the first investigation settled and `now` past `cooldown_minutes`: unchanged fingerprint opens none; changing the status (or the blocker sentence) opens one; still inside the cooldown with a changed fingerprint opens none |
| `test_daily_cap_holds_fleet_wide` | `tests/test_investigation_orders.py` | six stuck orders across two projects, catalog `max_per_day=4` (NAMED in the catalog, not the shipped 48 — the test is about the cap's arithmetic, not its value): exactly 4 investigations, and they are the four most overdue |
| `test_an_investigation_is_never_a_subject` | `tests/test_investigation_orders.py` | an `investigator` work order parked past every threshold, plus its `investigation` row: `stuck_tick` opens nothing, and `ops.create_investigation_order` on the investigator still raises `OpsError` naming the kind |
| `test_a_user_owed_order_is_investigated` | `tests/test_investigation_orders.py` | **the §3 regression test.** A `needs_review` order with `needs_attention=1` and `attention_reason == invariants.VALIDATION_STUCK_BLOCKER`, 5h in status: one investigation is opened, and its `why` contains §3's three questions |
| `test_fleet_pause_opens_nothing_but_records_a_run` | `tests/test_fleet_pause.py` | with `fleet.pause(central, …)` live: `opened == 0`, a run row exists with the exclusion named, and `INV-STUCK-SWEEP-DARK` does not fire |
| `test_sweep_error_raises_the_invariant_once` | `tests/test_invariants.py` | a sweep whose read raises: the run row carries the error, `check_stuck_sweep_alive(store)` on the OS-owning project yields exactly one `INV-STUCK-SWEEP-DARK` with `cause == "failing"`, a second call yields it again from the same row (the state, not an event), and a clean run clears it |
| `test_a_failing_sweep_reaches_the_attention_list` | `tests/test_invariants.py` | **the round-1 test.** `ops.stuck_scan` patched to raise, then `stuck_tick` + `Daemon.check_invariants`: exactly one `ops.os_status()['attention']` item names `INV-STUCK-SWEEP-DARK`, `healthy` is False, a second tick still yields one, and a clean sweep judged with `sweep_landings=True` removes it |
| `test_the_fleet_wide_sweep_is_reported_once_not_once_per_project` | `tests/test_invariants.py` | two projects, one owning the install: the owner reports `cause == "failing"` and the ordinary project reports nothing |
| `test_the_stuck_check_is_registered_where_it_can_push` | `tests/test_invariants.py` | `check_stuck_sweep_alive` is in `INVARIANTS` and in neither `OS_INVARIANTS` nor `SLOW_INVARIANTS` |
| `test_sweep_dark_raises_after_the_window` | `tests/test_invariants.py` | enabled, newest run one minute past `DEFAULT_FLEET_HEALTH_SWEEP_DARK_MINUTES`: one violation, `cause == "dark"`; one minute inside it, none |
| `test_the_fleet_catalog_value_is_what_the_check_judges_by` | `tests/test_invariants.py` | a 20-minute silence the 180-minute default does not report fires once `os.fleet_health.sweep_dark_minutes` is 10 — the FLEET catalog value is what the check judges by |
| `test_sweep_dark_minutes_is_refused_on_a_project` | `tests/test_catalog.py` | a project naming `sweep_dark_minutes` raises `CatalogError` naming `os.fleet_health.sweep_dark_minutes` and calling it a FLEET number; set on `os`, it reaches every project |
| `test_every_open_status_has_a_threshold` | `tests/test_catalog.py` | for every `s in project_store.OPEN_STATUSES`, the resolved config answers a positive threshold (named or fallback) — the test that makes a new status a failure rather than a blind spot |
| `test_fleet_health_inherits_per_status` | `tests/test_catalog.py` | a project naming `thresholds["running"]` keeps the fleet values for the other eight; an unknown status key raises `CatalogError` naming it; a project setting `max_per_day` raises `CatalogError` naming `os.fleet_health.max_per_day` |
| `test_no_second_budget_knob` | `tests/test_budget.py` | the investigation the sweep opened carries `budget.investigation_default_for(spec)`, and a project setting `worker.investigation_budget_usd` changes it — i.e. the sweep passes no `budget_usd` |
| `test_verdict_reaches_the_subject_timeline` | `tests/test_investigation_orders.py` | after `submit_verdict`, the SUBJECT's `list_events` contains one `investigation_verdict` carrying the classification, and the kind is in `health.observer_kinds()` so `stuck.fingerprint` is unchanged by it |
| `test_assess_is_pure` | `tests/test_health_sweep.py` | AST walk of `src/jarvis/stuck.py`: no import of `ops`, `daemon`, any `*_store`, `claude_cli` or `time` at module level — `tests/test_remedies.py`'s AST-pin pattern, and `neo`/`panel`'s seam test |
| `test_stuck_report_is_the_only_arithmetic` | `tests/test_ui.py` | `/stuck` renders and `jarvis stuck --json` returns the same rows as `ops.stuck_report`, over one seeded stuck order |

### 10. Rejected alternatives

| instead | why it loses |
|---|---|
| An agent reads the open list each morning and picks what looks stuck | The decision this spec is about is arithmetic over rows the OS already writes, and a model asked to make it costs a session per sweep and answers differently on two identical fleets. Settled point 1, and `health.py`'s split — probes decide what is wrong, `due` decides when it is worth spending — is the shipped form of the same ruling |
| Put the predicate in `invariants.py` as one more `INVARIANTS` entry | Ruled out by A.9 (docs/superpowers/specs/2026-09-27-investigation-orders.md:1152): an invariant names a gap class and its output is a remedy. A check whose output is a model session would also break "no LLM, ever" as the reader understands it, and it would run on the 30-second reconcile beat |
| Extend `health.due` / the supervisor sweep instead of a new tick | `due` answers "when should a model LOOK at this", is already keyed on a fingerprint and an interval, and ships DISABLED per project (`supervisor.health_enabled`). Folding a fleet-wide, always-on, time-in-state trigger into it would put two spend decisions behind one switch, and the OS's own sweep is pinned on by a user rule (kn-7312c7de) that has nothing to do with this |
| Skip statuses that read as user-owed | §3. Part A's four incidents all read that way and all were Jarvis gaps |
| Exclude every hold in `holds.HOLD_CAUSES`, not just the usage limit | An order held by a gate nobody will decide, a Neo question that cannot be answered or an undelivered message IS the stuck order this exists to find. #788 was exactly that |
| Shell out to `jarvis investigate` from the daemon | Forbidden by §2.7 and pointless: `create_investigation_order` is a Python function holding every refusal, and a subprocess would re-parse output to learn what it returns |
| A `stuck_cooldowns` table keyed by subject | `feature_orders.metadata` already carries the subject for the same reason; one more key there is no migration, and the cooldown becomes visible on `jarvis investigate show` for free |
| A counter in `os_state` for the daily cap | Drifts. `fleet.Outage`'s rule: derive from the records, so nothing has to remember to decrement |
| A second dollar knob, `fleet_health.budget_usd` | Neo's one condition on 1073, and it is right: two ceilings on one spend is a ceiling nobody can check |

### 11. Not covered, and what is uncertain

* **A.9's one shared edit is out of scope.** A.9 (line 1175) asks that the sweep skip the
  gap classes whose detector already fired on that subject this episode. `rules.py`,
  `gaps.py` and `rule_fires` landed, but the firing pass that would write those rows and
  the `open_investigation` primitive did not — so the ledger the skip reads is not yet
  written by anything. Adding the read now would couple this sweep to a half-landed
  mechanism. It is a one-condition addition in §5 step 6 when the pass lands.
* **Feature orders are not swept.** `ops.state_durations` takes `fo_id` and would work,
  and `ops.create_investigation_order` accepts an `fo-`/`io-` subject. Left out because
  the thresholds Neo approved are work-order statuses (`FO_STATUSES` is a different
  vocabulary), and a feature's own stall is usually a child's. Its own decision, and the
  extension point is `stuck.assess`'s `thresholds` argument, unchanged.
* **The lifted-pause residual** in §2 is a symptom fix, stated as one: a `jarvis pause`
  that has ended leaves no episode to discount, and the root cause is that the brake is a
  flag rather than a record.
* **Unverified number:** `dispatching` and `fallback_minutes` were mine, not Neo's, and no
  measurement backed them. SETTLED by the user on 2026-10-01 (Neo 1148): 5 and 30
  minutes, under the five-minutes-of-idle bar in §4.

### 12. A standing critical violation is an attention item — Neo 1084

**General invariant plumbing, NOT §8's alarm.** §8 adds a `critical` violation and would
have inherited a defect that predates it: a violation reached the inbox and the work
order's timeline and NOTHING ELSE, while `ops.os_status` (src/jarvis/ops.py:806) computes
`healthy` from the attention list alone. So `jarvis status` read HEALTHY over a critical
post-condition that was false — INV-STUCK-SWEEP-DARK included.

The fix, scoped to `level="critical"` only:

* `violation_reports` carries the violation's `level` and `detail`
  (`ProjectStore.open_violation_report`, `ADDED_COLUMNS`). Kept for a READER, not for the
  announcement: `os_status` reads state and cannot ask the checker again.
* `ProjectStore.standing_violations(level="critical")` is that read, and
  `os_status` appends ONE attention item per row — the report row IS the dedupe, so a
  violation standing for a thousand ticks is one item, and `close_violation_reports` on a
  sweep tick is what takes it away.
* `warning` violations are unchanged: they reach the inbox exactly as before. Promoting
  every violation would spend the attention budget the rollup rules protect.
