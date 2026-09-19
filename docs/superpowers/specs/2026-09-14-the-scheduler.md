# The scheduler, and the daily doctor

GitHub issue #164 item 2. Work order `wo-db644a20`; design ruled by Neo, question 292.

> I am thinking that jarvis can schedule an automatic work order per day to run jarvis
> doctor, from which work orders can be created on demand to fix and ship fixes.
> — the user

## 1. What this is, and why the guard rails are the design

Before this there was no scheduler anywhere in Jarvis. Every work order in the system's
history was filed by a person, by the dashboard, or by a model mid-answer (`origin=neo`).
`bus.py` records the pattern being followed at the time: feature orders "reused
`claim_next_pending` rather than inventing a scheduler".

This is the first mechanism that files work — and therefore spends the user's money —
because a clock said so. That is the whole of what makes it different, and it is why the
decisions below are about restraint rather than about capability. The capability is
twenty lines.

Two things are delivered: the **vehicle** (§2–§4) and its **first rider** (§5).

## 2. Where the cadence is declared

`catalog.ScheduleConfig`, fleet-wide at `os.schedule` and per project, with the
field-level inheritance `_parse_inspect` uses. Policy in the catalog rather than as a
module constant is kn-67cdb54b's rule, and this is the strongest case for it in the
codebase: "how often, and which jobs" is not a fact about the OS, it is a standing
instruction to spend.

Four keys:

| key | default | what it is |
|---|---|---|
| `enabled` | `false` | the master switch. Ships OFF |
| `interval_hours` | `24` | the cadence. Hours, not a cron expression — see below |
| `jobs` | `("daily-doctor",)` | which riders this project runs; ids of `schedule.JOBS` |
| `held_alarm_intervals` | `3` | how long a held job may stay silent (§4) |

**Two switches rather than one**, `RemedyConfig`'s pattern: `enabled: false` with a
meaningful `jobs` roster means turning the scheduler on is one setting, not two, while an
upgrading fleet still schedules nothing until somebody says so.

**Hours, not cron.** A cron expression can express "every minute". The first mechanism
that spends money on its own should not be one typo away from doing so, and nothing in
the ask needs more than an interval.

**`*.schedule.*` is in `SAFETY_KEYS`.** The whole block, not just the switch: enabling
the scheduler and shortening its interval are the same act at two magnitudes, and a
change here is invisible until the morning it starts spending.

The job ROSTER is code, not config: `schedule.JOBS`, each entry an id, a title, and a
`describe(JobContext) -> str` built at firing time. Adding a rider is one entry. The
catalog says *which* jobs run, never *what* they are — an id in `jobs` that names no job
is a `CatalogError` at boot, `GateConfig.parse`'s rule.

## 3. The cadence itself

`schedule.decide(state, interval_seconds, now, blocker) -> Decision` is a pure function
of a stored row, a number and a clock. The one thing that must never be wrong — "may the
OS spend money right now" — is therefore testable with no store, no catalog and no
daemon. `Daemon.schedule_tick` is the only thing that turns a `Decision` into a work
order.

State lives in `project_store.scheduled_jobs`, one row per job: `last_fired_at` (never
null), `last_wo_id`, `held_since`, `held_reason`. In the PROJECT store rather than
`os_state`, because what a firing produces is a project work order — two projects sharing
one clock would mean the first to fire silenced the rest.

Three properties, each a ruling:

**A restart never fires.** `last_fired_at` is seeded to the moment the job is first seen
enabled and lives on disk, so neither enabling a job nor bouncing the daemon makes one
due. The first run of a newly enabled job lands one interval later.

**At most one catch-up, never a backfill.** Due-ness is `now - last_fired_at >= interval`
and firing sets `last_fired_at = now`, not `last_fired_at + interval`. A daemon that was
off for a week comes back and files one order. Advancing by whole intervals instead would
turn an outage into a burst of spending at exactly the moment the fleet is least well.

**Held, not queued.** A job whose previous order has not settled does not fire and does
not advance its clock, so the backlog can never exceed one. It raises no attention: six
stacked doctor orders is precisely the alarm nobody reads. A *deleted* previous order is
not a blocker — the clock outlives the work order it filed, and a scheduler killed by
housekeeping would be a worse failure than a duplicate.

## 4. Surfacing a hold — the condition on holding silently

Neo's condition: hold silently, but persist the hold somewhere `jarvis doctor` and
`jarvis status` can surface it, so a parked scheduler is not an invisibly dead one.
A mechanism that has quietly stopped and one that is politely waiting look identical from
outside, and only the row remembers how long it has been either.

Two surfaces, at two volumes:

* `jarvis status` prints `⏰ <job> held: <reason>` under the project, from
  `os_status()["projects"][…]["schedule_held"]`. Only held jobs appear — a scheduler that
  is ticking along is not news, and a line per job per project would spend exactly the
  budget this design protects.
* `invariants.check_schedule_progresses` (**INV-SCHEDULE-HELD**) reports a job held past
  `held_alarm_intervals` whole intervals. Unrepairable by construction: the remedy is to
  settle the order in the way, which is a judgement about that work. An unrepaired
  violation raises a notification, so a dead scheduler reaches the inbox.

**Both surfaces filter by the project's `ScheduleConfig`, and the reason is sharper than
symmetry.** A hold SURVIVES being switched off: `held_since` is cleared by a firing and by
nothing else, so a job held at the moment somebody sets `enabled: false` or drops it from
`jobs` can never reach the event that would clear it. Guarding the invariant and not
`jarvis status` leaves the OS printing `⏰ … held:` for ever about a mechanism the user has
already turned off — `check_health_sweep_produces_judgements`' lesson, where a switched-off
mechanism with rows still on disk alarmed for ever, arriving by a second route. **And they filter through ONE resolver, `ops.schedule_config_at`.** Writing the same rule
in two places is what let them drift the first time: `_held_jobs` resolved by project NAME
with a disabled fallback while the invariant resolved by resolved PATH and fell back to
`catalog.os.schedule`, so a project in the central store but absent from the catalog — or
registered under a different name — got opposite answers from the two surfaces.

Resolution is **by path**, because that is the only key both callers hold: an invariant is
handed a store and no name. A caller that has already loaded a catalog may pass it, which
saves a file read per project and is not a second code path.

**A project the catalog does not list resolves to `ScheduleConfig()` — disabled — and
deliberately not to `catalog.os.schedule`,** where `inspect_config_at` and
`messaging_config_at` go. Those answer "by what threshold shall I judge this work", which a
fleet-wide default answers perfectly well for an unconfigured project. This answers "is this
mechanism supposed to be running here", and for a project absent from the catalog the
daemon's answer is no: `Daemon.tick` iterates `catalog.projects`, so its jobs can never fire
and its holds can never clear. Inheriting an enabled `os.schedule` would report a permanent
hold for a project the OS does not drive — this section's failure, reached by a third route.

## 5. Labelling, and the attention budget

A scheduled work order carries `origin="schedule"` (`WO_ORIGINS`, badge `⏰ scheduled`).
It is there for the reason `neo` is, one notch up: not even a model decided to file this,
a clock did, and "why am I paying for this work order" is unanswerable without it. It is
**not** in `UNGOVERNED_ORIGINS` — the daemon dispatches it with a full briefing, so it
owes the worker contract like any other.

**Exactly one scheduled order per job stays visible: the newest.** As today's is filed,
yesterday's is hidden (`Daemon._retire_previous`). Hiding is earned, not automatic — a
previous order that is flagged, or that holds an assumption the user has not ruled on, is
left exactly where it is, because those are the two states in which it is still asking for
something. What a run FOUND does not live on the scheduled order either way: findings
leave as their own work orders, under their own origin, and nothing hides those.

This is the answer to the constraint the work order set: *a new item every single morning
that usually says nothing is exactly the alarm nobody reads.*

## 6. The first rider: the daily doctor

`schedule.DOCTOR_JOB`. It runs `jarvis doctor <project> --repair`, then triages.

**`--repair`, not read-only.** The daemon already applies these same repairs on every
reconcile tick, so the daily run repairs nothing the OS would not have, and repairing is
consistent with what is already happening hourly.

> Superseded 2026-09-18 (wo-16a488ee): the second half of this argument used to be that a
> read-only run pays full price for INV-WORK-LANDED, because its cache of settled verdicts
> was a timeline write a plain run could not make. That check no longer measures content
> and keeps no cache, so `repair` changes nothing about its cost or its answer.

**`--skip-os` everywhere but one project.** `run_doctor` runs `check_os` regardless of
`--project` on purpose: a fleet scoped to one project still wants to know its dashboard is
broken. That is right for a human typing the command and wrong for a scheduled sweep — a
fleet of six would report one broken dashboard six times every morning, which is §5's
failure by another route. So `ops.run_doctor` gained `include_os` (CLI: `--skip-os`), and
`schedule.os_owner` picks the single project that keeps it: the one whose directory
contains the running `jarvis` package — the dev checkout in dev, the deployed tag in
production. Derived, not declared, because a catalog key would be one more thing to get
wrong on a deployment that already knows the answer. Where no project contains the install
it falls back to the first project in catalog order: arbitrary, but deterministic and
unique, which is the property that matters.

**The check list is not in the description.** `invariants.INVARIANTS` grows — two sibling
orders on issue #164 are adding to it as this ships — and a description that enumerated
the checks would have to be rewritten by each one. The order runs the doctor; the doctor
decides what the doctor checks. The worker is told explicitly not to enumerate them in its
summary either, so the record does not rot as checks are added.

**Triage, not repair.** The order files one work order per genuine defect, a second
depending on the first where the fix has to be shipped, and writes no code itself. It is
told to check the open list first and `jarvis wo send` an existing order rather than file
a duplicate — this job runs every day and the same fault will still be there tomorrow.
That guard is worker discipline rather than machinery; if it proves insufficient the next
step is a fingerprint on the filed order, not a longer description.

## 7. What was deliberately not built

* **No per-job interval.** One interval per project covers the ask and the only rider.
* **No cron, no time-of-day.** "Daily" here means "every 24h since the last firing", so a
  fleet's orders drift relative to the wall clock. Naming a time of day means a timezone,
  a catch-up policy for a daemon that was down at that minute, and a second way to express
  the same thing.
* **No fleet-wide job.** Every job is per project, because what it produces is.
