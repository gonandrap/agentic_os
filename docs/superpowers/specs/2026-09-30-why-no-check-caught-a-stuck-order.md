# Why no check caught a stuck order — Part A, the audit

wo-9f00e3b5, Part A. Readers: the user, and the Part B spec. This document specifies no
mechanism. It inventories what the OS checks periodically, and judges each of nine
incidents from the 2026-09-26/27 session against that inventory.

## The problem

Nine faults in one session, every one found by hand by an operator the user had to ask,
none raised by a check. The OS runs 28 per-project invariants, 7 OS-wide ones, four
periodic GitHub polls, a live cost-alarm pass and one model-backed health sweep — and the
sweep, the only general-purpose check among them, was MUTED for the whole session
(`INV-HEALTH-SWEEP-MUTE`, "the last 10 health sweeps all failed … Most recent reason: the
health sweep could not be reached: You've hit your session limit · resets 11:10am"). Every
row in §2 was judged with it off.

Three structural facts do the damage, and §3 states them with evidence:

* every check answers a NAMED question — `true_blockers` enumerates 14 specific blockers
  (`src/jarvis/invariants.py:782-1011`) and nothing asks "is this order moving at all";
* an invariant violation reaches the inbox ONCE and never the attention list, and
  `os_status` computes `"healthy": pid is not None and not attention`
  (`src/jarvis/ops.py:806`) — so the OS reports a healthy fleet over a standing violation;
* the one general check costs a model call, so it is the one with an off switch, a
  per-tick cap and a failure mode that silences it.

## 1. The inventory

Cadences are in ticks at the shipped `poll_interval = 5.0` (`src/jarvis/daemon.py:508`).
"Surface" is where the output actually lands.

### 1.1 Per-project invariants — `invariants.INVARIANTS` (`src/jarvis/invariants.py:4355`)

All run inside `Daemon.check_invariants` (`src/jarvis/daemon.py:4184`), on the reconcile
beat: `RECONCILE_EVERY_TICKS = 6` (`daemon.py:99`), **30s**. No model call, ever
(module rule 1, `invariants.py:16`). All ship ENABLED — there is no switch.

Surface is uniform and is the thing to read: a violation writes a `wo_events` row
(`daemon.py:4228`), and only if `not v.repaired` an `add_notification`
(`daemon.py:4234`), which `Daemon.route_outbox` (`daemon.py:1525`) copies into the central
inbox, which `notify.route_new_inbox` (`src/jarvis/notify.py:204`) sends to the configured
sinks (Telegram). ONCE: `store.open_violation_report` returns True only the first time
(`src/jarvis/project_store.py:4290-4306`). A repaired violation reaches the timeline only.
None of them ever raises an attention flag for itself.

| INV id | Check (`invariants.py`) | Asserts | Repairs |
|---|---|---|---|
| INV-BASE-BRANCH-RED (:246) | `check_default_branch_green` :4306 | this project's default branch is not red | no |
| INV-ASSUMPTION-PERSISTED | `check_assumptions_persisted` :2273 | every recorded assumption exists as a row | yes |
| INV-GATE-ORPHAN | `check_no_orphan_gate_requests` :2088 | no gate request outlives the work order that filed it | yes |
| INV-REPAIR-RESETTLED | `check_repaired_work_rejoins_the_merge_queue` :1973 | a repaired pull request goes back in the merge queue | yes |
| INV-ATTENTION-REASON | `check_attention_reason_is_true` :1928 | a flagged order's reason names a real blocker | yes |
| INV-ADHOC-NOT-GOVERNED | `check_adhoc_not_governed` :2003 | an injected session is not judged as a worker | yes |
| INV-ADHOC-LEGACY-RETIRED | `check_legacy_adhoc_retired` :2042 | a self-adopted session is let go | yes |
| INV-ATTENTION-PHANTOM | `check_no_phantom_attention` :2129 | an order with nothing pending does not ask for the user | yes |
| INV-ATTENTION-PREMATURE | `check_assumption_flags_are_owed` :2152 | no flag for an assumption Neo will answer | yes |
| INV-MESSAGE-STUCK | `check_messages_are_delivered` :2218 | a queued message is not undelivered for ever | no |
| INV-ATTENTION-MISSING | `check_blocked_work_is_surfaced` :2191 | work needing the user says so | yes |
| INV-ATTENTION-BLANK | `check_attention_has_reason` :2314 | a flagged order says what it wants | yes |
| INV-PR-RECORDED | `check_pull_request_recorded` :3382 | a settled order that wrote code carries its pull request | no |
| INV-MANAGER-SLOTS | `check_manager_slots` :3459 | a manager order spends no concurrency slot | no |
| INV-HEALTH-SWEEP-MUTE | `check_health_sweep_produces_judgements` :3038 | the sweep is not billing without judging | no |
| INV-OS-HEALTH-SWEEP-DARK | `check_os_health_sweep_alive` :3166 | the OS's own sweep is on and judging | no |
| INV-PAUSE-OVERDUE | `check_paused_turns_resume` :2702 | a paused turn past its wait was relaunched | no |
| INV-PAUSE-DRIFT | `check_pause_deadline_stable` :2779 | a pause's deadline is still the one it was given | no |
| INV-SCHEDULE-HELD | `check_schedule_progresses` :4190 | a recurring job has not wanted to fire for days | no |
| INV-BUDGET-OVERSPENT | `check_budgets_are_enforced` :4242 | an order past its ceiling cannot still spend | no |
| INV-VALIDATION-STRANDED | `check_validation_progresses` :2893 | no unit sits on an open round for ever | yes |
| INV-FEATURE-FALSE-FAILURE | `check_feature_failures_are_real` :2966 | a failed feature whose children recovered goes back | yes |
| INV-NEO-ESCALATION-STALE | `check_neo_escalations_are_live` :2400 | a question held by the user is still answerable | yes |
| INV-REMEDY-PROPOSAL-STALE | `check_proposed_remedies_are_live` :2639 | a proposal nobody can answer does not sit for ever | yes |
| INV-UNDECLARED-DELIVERY | `check_undeclared_delivery` :1143 | commits pushed past a refusal were declared | no (raises a finding) |
| INV-SPAN-BEHIND | `check_spans_reach_the_status` :1177 | every status write has a span | yes |
| INV-ENVELOPE-STUCK | `check_envelopes_move` :2340 | an envelope does not sit in the queue for ever | yes |
| INV-ENVELOPE-LOST | `check_no_lost_feedback` :2844 | a message that reached nobody is not passed off as delivered | yes |

### 1.2 `SLOW_INVARIANTS` (`invariants.py:4432`)

`check_work_lands` :3242 — INV-WORK-LANDED ("a completed order's pull request must have
merged") plus INV-LANDING-AUDIT-FRESH :3370 (how many pull requests the audit has no
current answer for). Cadence `LANDING_SWEEP_EVERY_TICKS = 720` (`daemon.py:171`), **1
hour**, threaded in as `sweep_landings`. No model call. Ships enabled. Same inbox surface.
Its GitHub round trip is `Daemon.refresh_landings` :5247, on the same hourly beat
(`daemon.py:927`); `landing.FRESH_FOR_SECONDS = 7 days` (`src/jarvis/landing.py:137`),
`REFRESH_PER_SWEEP = 25` (:148). Violation reports are closed ONLY on a sweep tick
(`daemon.py:4217`), so a cleared violation stays "open" up to an hour.

### 1.3 `jarvis doctor` — and the checks that exist only there

`ops.doctor` (`src/jarvis/ops.py` :293-390). Runs `check_project(..., slow=True)`
unconditionally (:342), plus three families the daemon NEVER runs:

| Family | Entry | Members |
|---|---|---|
| OS-wide | `check_os()` :4176, called at `ops.py:326` | INV-UI-HEALTHY :3694, INV-GATE-CANARY :3579, INV-CONFIG-DRIFT :3723, INV-SERVICE-PATH :3775, INV-PROD-CLEAN :3821, INV-CACHE-TTL-TRIGGER :3967, INV-PREFIX-DRIFT :4100 |
| Catalog | `check_catalog` :3555, at `ops.py:304` | INV-GATE-DENY-CONFLICT :3521 |
| `$JARVIS_HOME` | `check_release_marker` :3634, at `ops.py:370` | INV-RELEASE-MARKER-STALE |

Stated in the module docstring: these "never run on the daemon's reconcile tick"
(`invariants.py:31`) and `check_os()` "is called by `jarvis doctor` — NOT by the daemon's
reconcile tick" (:3599). Surface: **stdout of a command a human typed**, and nothing else.
The one push path is the scheduler's `daily-doctor` job
(`src/jarvis/schedule.py:118-122`), which files a work order that runs
`jarvis doctor <project> --repair` and triages what it finds — and
`ScheduleConfig.enabled` ships **False** (`src/jarvis/catalog.py:1024`), so on a fleet that
has not switched it on, every check in this table is pull-only.

### 1.4 The supervisor health sweep — the one general-purpose check

The only check in the OS that costs a model call, and the only one whose question is open
("is something wrong with this unit") rather than named.

| Part | Code | Fact |
|---|---|---|
| trigger | `Daemon.health_tick` `daemon.py:3942`, projects from `_health_projects` :3927 | `tick_count % supervisor.health_every_ticks == 1`, default 20 ticks = **100s** |
| candidates | `Daemon._health_candidates` :3963 | open work orders + feature orders with a carrier, longest-unreviewed first, capped at `health_max_units_per_tick` (default **4**) |
| due rule | `health.due` `src/jarvis/health.py:92-124` | floor on ATTEMPTS not judgements; `first-look` / `changed` after `health_min_interval_minutes` (**30**), `stale` after `health_stale_minutes` (**720** = 12h) |
| the call | `supervisor.review_health` `src/jarvis/supervisor.py:865-996` | one `structured.request`, `attempts=1`, model `opus`, `timeout=300` |
| what it looks for | `probes.DEFAULT_PROBES` `src/jarvis/probes.py:60-146` | `no-progress`, `going-in-circles`, `waiting-on-nobody`, `failing-children` (features), `brief-mismatch` |
| enabled? | `catalog.SupervisorConfig` :1110 | `enabled = False` AND `health_enabled = False` — **two** switches, both off |
| surface | `supervisor.py:970-996` | a `wo_alarms` finding row + `health_finding` event + `flag_attention` on the carrier if nothing is flagged yet; then judged by `Daemon.supervisor_tick` :3737, which can escalate to Neo and thence to `jarvis status` |
| holds | `_health_sweep` :4003-4040, `_hold_sweep_for_outage` :4042 | account window and per-project hold read BEFORE any call is bought |

### 1.5 Live cost alarms — `inspection.py`

`Daemon.check_burning_turns` (`daemon.py:4244`), reconcile beat (**30s**), no model call,
one transcript read per `running` order. Behind `inspect.enabled`
(`catalog.InspectConfig`). Kinds and defaults: `long-turn` /
`alarm_turn_minutes = 60`, `long-join` / `alarm_join_seconds = 300` and
`alarm_subagent_tool_minutes = 5`, `big-rewrite` / `alarm_write_tokens = 300_000`,
`slow-model-response` / `alarm_awaiting_minutes = 60` (informational — never escalated,
`inspection.py:1548`), `stalled-turn` historical only (`inspection.py:1540`).
`catalog.py:631-704`. Surface: `wo_alarms` row + `cost_alarm` event + `/alarms` and
`jarvis alarms`, and `flag_attention` for the first non-informational kind
(`daemon.py:4317-4320`). Siblings, same module's legend: `Daemon.check_rewrite_tax` :4321
(reconcile beat, `rewrite-tax-prefix` / `-ttl`, window 7d, share 0.15) and
`Daemon.check_cache_ttl` :4392 (`CACHE_TTL_EVERY_TICKS = 4320`, **6h**, offset tick 60,
`cache-1h-dispatched` / `-foreign`).

### 1.6 Attention derivation

`invariants.true_blockers` (`invariants.py:782-1011`) is the single source of "what does
this order want from me": a list of 14 named blockers, ordered, filtered against
`acknowledged(wo)`. Read on the reconcile beat by INV-ATTENTION-MISSING (raise),
INV-ATTENTION-PHANTOM and INV-ATTENTION-REASON (lower or rewrite). An attention flag is a
column plus a timeline event and NOTHING else — `ProjectStore.flag_attention`
(`project_store.py:2632-2634`). It is read by `ops.os_status` (`ops.py:500`), i.e. by
`jarvis status` and the dashboard: a pull surface with no notification.

### 1.7 GitHub polls and the validation tick

| Pass | Code | Cadence | Model? | Enabled | Surface |
|---|---|---|---|---|---|
| default-branch health | `Daemon.poll_default_branch` :4824 | `PR_POLL_EVERY_TICKS = 24`, **2min** | no | yes | writes `central.base_health`, read back by INV-BASE-BRANCH-RED |
| parked pull requests | `Daemon.poll_pull_requests` :4983 over `PR_POLL_STATUSES` (`daemon.py:143`) | 2min | no | yes | status change, `pr_merged`, nudges bounded by `PR_REPAIR_STATUSES` |
| issue sync / references | `Daemon.sync_issues` :7264, `sync_issue_references` | 2min | no | yes | tracker labels, release orders |
| overtaken releases | `Daemon.settle_shipped_releases` :7631 | 2min | no | yes | settles the order, `release_overtaken` event |
| red-base release hold | `Daemon.hold_red_release` :7504 | every tick | no | yes | `RED_HOLD_SECONDS = 300`, park after `RED_PARK_AFTER_SECONDS = 6h` |
| landing audit | `Daemon.refresh_landings` :5247 | 1h | no | yes | timeline, then INV-WORK-LANDED |
| validation | `Daemon.validation_tick` :1832 (+ `feature_validation_tick`) | **every tick** | yes (panel) | panel ships disabled | round outcome, `bus` envelope, INV-VALIDATION-STRANDED is the watchdog |
| auto-merge | `automerge.decide` :272 via `Daemon.auto_merge` | on the 2min poll | Neo reviews the gate | `validation.auto_merge` | gate request, `HELD_*` holds |
| paused turns | `Daemon.retry_paused_turns` :1538 | `RETRY_EVERY_TICKS = 2`, **10s** | no | yes | relaunch; INV-PAUSE-OVERDUE / INV-PAUSE-DRIFT watch it |
| Neo drain / digest | `neo_tick` :3130, `digest_tick` :3330 | every tick | yes | yes / `os.neo.digest_model` | answers, escalations |
| remedies | `Daemon.remedy_tick` :3765 | every tick | no | `supervisor.remedies` ships off | applies an approved remedy |
| scheduler | `Daemon.schedule_tick` :4117 | `SCHEDULE_EVERY_TICKS = 12`, **1min** | no | ships off | files the daily doctor order |
| dashboard log | `Daemon.check_ui_log` :1480 | every tick | no | yes | inbox item, exactly once per batch |
| bills | `Daemon.seal_bills` :972 | reconcile, 30s | no | yes | sealed `bill_json` |

Not on any periodic path, and worth naming because Part B will want it: `ops.diagnose`
(`ops.py:2038`, `jarvis wo why`) composes the whole "why is this order not moving" answer
— `waiting_on`, `true_blockers`, holds, spans — and has **no caller in `daemon.py`**. It
runs when a human types it, for one order.

## 2. The incident table

| # | Symptom | Check that should have caught it | Why it did not | Verdict |
|---|---|---|---|---|
| 0 | the health sweep itself: 10 consecutive failed sweeps, reason "the health sweep could not be reached: You've hit your session limit · resets 11:10am" | INV-HEALTH-SWEEP-MUTE — it DID fire | it fired into `jarvis doctor` and a single inbox row; nothing put it on the attention list, and a usage limit was recorded as a FAILURE, so the sweep was dark all session | raised-but-invisible |
| 1 | stale `panel_gave_up` hold on wo-15f5d969 (#786) | nothing — no check reads a hold's freshness | the hold has no storage but an append-only `autoreview_held` event, rendered present-tense | not-modelled |
| 2 | wo-00bd1096 assumption shown "with Neo" 8.5h+ on a question Neo could not be reached for (#788) | nothing; `NeoStore.reclaim_stale` logged and stopped | same fault: `autoreview_asked` is immortal, `questions.status` moved to `failed`, nothing re-derived | not-modelled |
| 3 | `main` red ~4h after a stale-base auto-merge, twice (#790, #795); the detection gap filed as #793 | INV-BASE-BRANCH-RED | it did not exist during the session; every base-health fact the OS held was per work order | not-modelled |
| 4 | Neo question 722, ~151.7K chars, failed `E2BIG` three times (#797) | nothing — no check counts a question's failures | `OSError` errno 7 is neither `ClaudeCliError` nor `UsageLimitError`, so it escaped the drain into `log.exception` | not-modelled |
| 5 | wo-00bd1096 and wo-8736a5c5 stranded in `waiting_pr_merge`, "no rounds are left", after catch-up merges (#806) | `true_blockers` — it DID raise `SHA_MOVED_BLOCKER` | the ask itself was the defect: a flag was up telling the user to force a round on green, mergeable code, and no check asks whether an ask the OS created is avoidable | not-modelled |
| 6 | wo-8736a5c5 parked 13h+ after Neo denied its merge with "rebase and ask again" | nothing derived a blocker from a denied merge | `AUTOMERGE_DENIED_BLOCKER` did not exist; the order sat in `waiting_pr_merge`, which is silent in the ordinary case | not-modelled |
| 7 | wo-4301a7a6, a release order overtaken by another release (#784) | nothing; the health sweep's `no-progress` probe is the only candidate, and it was muted | a release order had no definition of done but "my own worker cut a tag"; `RELEASE_BATCH_KEY` was written and never read back | not-modelled |
| 8 | #797 and #784 merged to `main` and unreleased ~7h | nothing — no check asks whether a landed fix shipped | the release was gated on the RATING, which an expedited order does not carry; expediting was not recorded anywhere queryable | not-modelled |

### 0 — the sweep was muted, and every row above was judged with it off

`jarvis doctor jarvis_os` reported INV-HEALTH-SWEEP-MUTE. Predicate:
`check_health_sweep_produces_judgements` (`invariants.py:3069-3095`) — the last
`HEALTH_SWEEP_FAILURE_RUN` sweeps all `outcome='failed'`, newest inside
`HEALTH_SWEEP_FAILURE_WINDOW_MINUTES`. Not repairable (:3061).

Which surfaces it reached: `jarvis doctor` stdout; a `wo_events` row only if the violation
carried a `wo_id`, and this one does not (`daemon.py:4227`); one `add_notification` at
`warning` level, the first tick it stood, copied to the central inbox and pushed to
Telegram once. Which it did NOT reach: the attention list — `check_invariants` never calls
`flag_attention`, and `os_status` computes `healthy` from `attention` alone
(`ops.py:806`), so `jarvis status` said healthy; `jarvis status`'s `inbox.items[:10]`,
ordered `ts DESC` (`central_store.py:624`), which a day of newer rows pushes it off; and
every later tick, because `open_violation_report` is True once
(`project_store.py:4290-4306`).

The cause of the failure run was a usage limit recorded as a transport failure —
`review_health`'s `except claude_cli.ClaudeCliError` path built
`_nothing_found("the health sweep could not be reached: …")` and
`record_health_review(outcome="failed")`, and `health.due`'s floor is on ATTEMPTS, so a
sweep that always fails has no floor and retries at the tick rate
(`health.py:112-116`). FIXED SINCE, and not re-specified here: issue #835, wo-8a68e024,
spec `docs/superpowers/specs/2026-09-28-a-usage-limit-is-not-a-failed-sweep.md`. A usage
limit now records `outcome="held"` with `reopens_at` (`supervisor.py:932-944`,
`daemon.py:4042-4064`), held rows are excluded from the run
(`invariants.py:3069-3077`), and INV-OS-HEALTH-SWEEP-DARK (`invariants.py:3167`,
`OS_HEALTH_SWEEP_DARK_MINUTES = 180`, `level="critical"`) is the liveness half.

The audit's claim is narrower and stands: for the whole of 2026-09-26/27 the fleet's only
general-purpose check was producing no judgement, and every incident below was judged
against an inventory of named checks with the one open-ended check off.

### 1 — the stale `panel_gave_up` hold (#786)

Evidence, from the landed spec
`docs/superpowers/specs/2026-09-26-a-panel-gave-up-hold-says-which-round-and-stops-when-it-passes.md`:
"The hold has no storage but an `autoreview_held` timeline event
(`Daemon._note_autoreview_held`, `src/jarvis/daemon.py:5334-5390`) … An immutable event is
therefore making a present-tense claim — kn-a2ebbbdb" (spec §"Half 2", lines 45-53). Both
renderers show the newest event for the subject (`ops.assumptions_with_rulings`
`ops.py:2785`, `ops.autoreview_state` `ops.py:2693`). No invariant reads
`autoreview_held`: INV-ATTENTION-REASON re-derives from `true_blockers`, which has no
clause for a hold's age, and the hold is not an attention reason at all. FIXED SINCE —
that spec plus `docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md`.

### 2 — "with Neo" for 8.5h on a dead question (#788)

`docs/superpowers/specs/2026-09-26-an-unreachable-neo-question-is-not-a-question-in-flight.md`
§"The problem": `neo_store.release_claim` (`src/jarvis/neo_store.py:366`) writes
`status='failed'`; `ops.assumption_ruling_line` (`ops.py:3033`) still renders "Asked Neo
(question {question}), awaiting ruling", and `autoreview.decide` condition 6
(`src/jarvis/autoreview.py:846-849`) still holds `HELD_ASKED` — "The outage ends; the
assumption is never re-asked, for the life of the work order." The narrower half is the
detection one: `NeoStore.reclaim_stale` (`neo_store.py:290`) is "a bare UPDATE with no
callback" and `Daemon.neo_tick` (`daemon.py:2864-2867`) only `log.warning`s the ids, so no
inbox row and no flag were posted on the stranded path. Nothing periodic compares a
project-side pointer against `questions.status`. FIXED SINCE, read-side, per that spec and
`2026-09-28-a-dropped-confirmation-must-not-hold-an-assumption-for-ever.md`.

### 3 — `main` red ~4h, twice (#790, #795); the gap filed as #793

`check_default_branch_green`'s own docstring is the incident report: "`main` was red for
~4h after the OS's own merge broke it and no surface said anything, because every
base-health fact the OS held was per work order and written only for an order whose own
pull request was failing" (`invariants.py:4310-4313`). FIXED SINCE: INV-BASE-BRANCH-RED
(`invariants.py:246`, :4306) plus the writer `Daemon.poll_default_branch`
(`daemon.py:4824`) on the 2-minute beat, spec
`2026-09-26-a-red-default-branch-raises-itself.md`; and the merge-side cause —
`automerge.held_base_moved` / `HELD_BASE_MOVED` (`src/jarvis/automerge.py:123, 233`), spec
`2026-09-28-a-merge-checks-the-base-it-lands-on.md`. Note what the fix does NOT do:
`check_default_branch_green` is not repairable and, like every invariant, reaches the
inbox once and the attention list never.

### 4 — Neo question 722, 152K chars, `E2BIG` three times (#797)

`docs/superpowers/specs/2026-09-26-a-prompt-too-big-for-argv.md` §"What it did, measured":
prompt ~151.7K chars against `MAX_ARG_STRLEN` = 131,072; three failures at ~15 minutes
each; "a bare `OSError` is neither, so it propagates RAW out of `claude_cli`", escapes
`neo.drain_queue`, "lands in `Daemon._neo_drain`'s `except Exception`, logged as 'neo
drain failed'", `release_claim` never called, recovered only by `reclaim_stale` after
`STALE_ANSWERING_SECONDS = 900` × `MAX_ANSWER_ATTEMPTS = 3` — "≥45 minutes of a worker
parked on `waiting_input`". No periodic check counts a question's failed attempts; the
only surface was a log line. FIXED SINCE: that spec plus
`2026-09-26-bounded-model-inputs.md`.

### 5 — stranded on "no rounds are left" (#806)

This row is the one where a check DID fire and the firing was the fault.
`docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md` §1: "wo-00bd1096
/ PR #779 and wo-8736a5c5 / PR #794. Both are green and mergeable, both held on
`sha_moved`, both out of rounds, both sitting on the user's attention list with nothing
wrong with the code." The chain is `automerge.decide` condition 5
(`automerge.py:287`) → `ops.rejudge_moved_head` declining at `nxt >= cfg.max_rounds`
(`ops.py:4150`) → `invariants.rejudge_exhausted` (`invariants.py:513`) →
`SHA_MOVED_BLOCKER` (`invariants.py:927-928`). So the detection gap is not "nobody
noticed": it is that every check judges whether the user is owed something, and none
judges whether the OS created the debt by charging a round for a catch-up it demanded.
FIXED SINCE: that spec, widening `ops.carry_validated_head` (`ops.py:5274`) past its one
call site.

### 6 — wo-8736a5c5 parked 13h+ on a denied merge

`true_blockers` derives nothing for `waiting_pr_merge` in the ordinary case, by design —
`BLOCKED_STATUSES`' comment says it is listed "for the one state that does, the conflict
the worker could not resolve" (`invariants.py:76-80`). A merge the reviewer refused was
not that state, so the order sat in a status the OS treats as "the OS is waiting", with
nobody to act on "rebase and ask again". FIXED SINCE: `automerge_denied` →
`AUTOMERGE_DENIED_BLOCKER` (`invariants.py:938-939`), whose comment states the case
verbatim — "Nothing re-asks for that commit, so the order would otherwise sit parked for
ever with nothing owed by anyone (spec 2026-09-24 fix 4b)" — and
`2026-09-27-a-stale-merge-hold-is-not-the-reason-a-pr-is-not-merging.md`.

### 7 — a release order overtaken (#784)

`docs/superpowers/specs/2026-09-26-a-release-order-overtaken-mid-ci-wait-settles-itself.md`
§"The problem": "Root cause, stated plainly: a release order has no definition of done
that is not 'my own worker cut a tag'"; `RELEASE_BATCH_KEY` "is written by
`ensure_release` and never queried outside that function … so no code can ask 'has this
batch shipped'". Twice measured (wo-e5816dd7, wo-4301a7a6), both ended in a worker turn
doing git arithmetic by hand. The only check whose question was open enough to catch a
release order that had stopped mattering is the health sweep's `no-progress` probe
(`probes.py:61-78`) — muted, per row 0, and in any case off by default. FIXED SINCE:
`Daemon.settle_shipped_releases` (`daemon.py:7631`) on the 2-minute beat.

### 8 — merged and unreleased ~7h

`docs/superpowers/specs/2026-09-27-an-expedited-order-that-lands-ships-a-release.md`:
"#797 (wo-956c29bc, PR #800) and #784 (wo-521c9225, PR #802) both merged to `main` and
both sat unreleased ~7h. Nothing was broken in the code; the operator had stopped
watching." Root cause quoted there: `Daemon.sync_issues` gated the release on
`issues.dispatches(wo.get("issue_priority"))`, true only for `critical`/`blocker`, and an
expedited filing carries either an honest low/medium rating or none. There is no periodic
check of the shape "a landed fix the user asked to ship has not shipped": INV-WORK-LANDED
(`invariants.py:3242`) asks whether the pull request merged and stops at the merge. FIXED
SINCE, per that spec.

## 3. The pattern

1. **Every check answers a NAMED question; none answers "is this order moving at all".**
   `true_blockers` (`invariants.py:782-1011`) is a flat list of 14 specific derivations —
   pending assumptions, escalated gate, budget spent, auth park, dead dependency,
   `sha_moved`, `rebind_exhausted`, `automerge_denied`, three ways into `needs_review`,
   stuck message, parked — and the whole of §2 is a list of faults that fell between them,
   each closed afterwards by adding a fifteenth clause. The one open-ended question the OS
   asks is `probes.DEFAULT_PROBES`' `no-progress` (`probes.py:61-78`), and it is asked by a
   model.
2. **A check whose only surface is `jarvis doctor` is invisible unless somebody types it.**
   `check_os()` "is called by `jarvis doctor` — NOT by the daemon's reconcile tick"
   (`invariants.py:3599`, and the module docstring at :31), so INV-UI-HEALTHY,
   INV-GATE-CANARY, INV-CONFIG-DRIFT, INV-SERVICE-PATH, INV-PROD-CLEAN,
   INV-CACHE-TTL-TRIGGER and INV-PREFIX-DRIFT run on no clock at all. The one push path,
   the `daily-doctor` scheduled order (`schedule.py:118`), is behind
   `ScheduleConfig.enabled = False` (`catalog.py:1024`).
3. **A raised invariant is a one-shot inbox row, and the OS still calls itself healthy.**
   `check_invariants` notifies once and only for `not v.repaired` (`daemon.py:4232-4240`),
   deduped for the life of the violation by `open_violation_report`
   (`project_store.py:4290-4306`); it never calls `flag_attention`, and `os_status`
   returns `"healthy": pid is not None and not attention` (`ops.py:806`) with `inbox`
   reported as counts plus `items[:10]` ordered `ts DESC` (`central_store.py:624`). Row 0
   is that arithmetic playing out over a session.
4. **The one general-purpose check costs a model call, so it is the one that gets muted,
   capped and switched off.** `SupervisorConfig.enabled = False` and
   `health_enabled = False` (`catalog.py:1119, 1140`) — two switches, both off, on the only
   check with an off switch at all; `health_max_units_per_tick = 4` (`catalog.py:1080`)
   bounds how many units it may look at; `health.due`'s floor sits on ATTEMPTS, so a sweep
   that keeps failing keeps retrying and produces nothing (`health.py:112-116`). Row 0 is
   the failure mode and #835 is its fix; the structural fact — the general check is the
   expensive one — is untouched.
5. **A blocker's WORDS are derived; its FRESHNESS is not.** `true_blockers` re-derives
   every reason from state so INV-ATTENTION-REASON can rewrite a flag it cannot re-derive
   (`invariants.py:110-114`), but neither it nor any invariant asks how long a blocker has
   stood: rows 1, 2 and 6 are all a present-tense claim rendered from an append-only event
   — the rule the repo already wrote down as kn-a2ebbbdb and kn-96f47efb, each time as a
   READ-SIDE fix for one renderer. The clock-aware pieces that do exist are narrow and
   per-condition: `stuck_message`'s `messaging.stuck_minutes` (`invariants.py:1056`),
   `_parked_minutes`' `inspect.alarm_parked_minutes` (:1067), `HEALTH_SWEEP_FAILURE_WINDOW_
   MINUTES`, `OS_HEALTH_SWEEP_DARK_MINUTES`.

## The fix

The fix is Part B, and this section states its remit rather than its design. Part B must
build the check the inventory has no row for: a periodic, deterministic, model-free pass
that asks of every open unit "how long has this been in this state, and is what it is
waiting for still true", and whose output reaches a PUSH surface — the attention list and
the inbox with a standing re-raise — rather than `jarvis doctor` stdout. Three properties
the audit makes non-negotiable: it must key on time-in-state and hold freshness rather
than on any named blocker (findings 1 and 5); it must not be behind a switch that ships
off, because finding 4 shows the general check is the one that gets disabled; and it must
change what `os_status` calls healthy, because finding 3 shows a raised violation today
leaves `"healthy": true`.

What Part B must NOT duplicate. Issue #835 / wo-8a68e024 /
`2026-09-28-a-usage-limit-is-not-a-failed-sweep.md` already own the sweep's hold semantics
and its liveness check (INV-OS-HEALTH-SWEEP-DARK) — Part B consumes them and re-specifies
neither. fo-69ba1cc4 owns the rule registry: `remedies.resolve`'s backing moving from code
to DATA rows keyed by `gaps` slugs (`src/jarvis/remedies.py:365, 1377-1399`,
`src/jarvis/ops.py:2225, 2497`) and the Evolution view over it — so Part B must not invent
a second registry of conditions, and where its detector needs a name it uses a
`gaps.py` class slug. `ops.diagnose` / `jarvis wo fix` (`ops.py:2038, 2491`) already
compose the per-order answer and the acting path: Part B supplies the CLOCK and the PUSH
that nothing currently runs, and calls what exists for the rest.
