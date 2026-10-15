# A worker turn in flight is not stuck

Work order wo-6f4f60f9. GitHub issue 1059. The design is settled by the issue and by the
work order's brief and is not re-opened here: a named cause in `stuck.py`, a
caller-computed `turn_in_flight: bool` on the pure `assess`, scoped to `running` at the
caller, surfaced as `Verdict.excluded` and never a `continue`. This document is the
mechanism, the precedence, the wording and the tests.

Precedent and house shape: `docs/superpowers/specs/2026-10-12-a-managers-by-design-idle-
is-not-a-standstill.md` (the `by_design_idle` parameter this copies) and
`docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md` (the
exclude-and-discount pair, kn-0a222b38, which §3 below declines on evidence).

## The problem

### 1. The sweep judges `running` on a proxy for liveness, not on liveness

`ops.stuck_scan` (`src/jarvis/ops.py:9657`) hands `assess` the activity clock for a
`running` order — `now - (durations.last_activity_ts or since)` (`ops.py:9727-9728`) —
because `running` is the one member of `stuck.ACTIVITY_STATUSES` (`src/jarvis/stuck.py:28`).
`last_activity_ts` comes from `ops._last_activity` (`ops.py:2073`) over
`ops._activity_of` (`ops.py:2021`): the newest non-quiet `wo_events` row, `MAX` over
`wo_turns`, `wo_messages`, `validation_rounds`, `approvals`, plus the session transcript's
mtime (`ops._transcript_activity`, `ops.py:1984`).

**A worker inside ONE turn writes none of the first five while its tools run.** The turn
row is stamped once, at `create_turn` (`src/jarvis/project_store.py:4531`), before the
process is spawned; the next write to any of those tables is the reap
(`worker_session._reap`, `worker_session.py:872`), which is the END of the turn. So the
activity clock measures the gap between turns and not the work inside one.

The sixth source is a **proxy and it is best effort by construction**: it needs
`session_id` on the row, the `worktree` column to resolve to the real cwd, and the
transcript file to have been flushed — and `inspection.py:1879-1882` already records why
the last of those cannot be relied on, verbatim: "Claude Code appends one only when a
content block COMPLETES". Every failure of the proxy is swallowed
(`except (OSError, KeyError): return []`, `ops.py:2016-2017`) and reads as silence.

**Root cause, stated once: the sweep asks "has anything been written about this order
lately" when the question it needs answered for `running` is "is a turn in flight".** The
OS already holds that fact authoritatively — `wo_turns.state`, default `'running'`
(`project_store.py:1078`), reaped the tick a process dies (`worker_session.poll`,
`worker_session.py:839`) — and `stuck.assess` is never shown it.

### 2. The ruling behind the 5-minute threshold assumed the proxy worked

`2026-09-30-an-order-that-stops-moving-gets-investigated.md` §4 sets
`thresholds["running"]` to 5 minutes on the user's 2026-10-01 ruling (Neo 1148) and
defends the number with: "`running` is still judged on time since the last ACTIVITY and not
since entry … which is what makes a five-minute number safe for a turn that is working: a
long turn that keeps calling tools is never over threshold." That sentence is false as
built. A turn that keeps calling tools writes nothing the activity clock reads. The
threshold is not mis-calibrated; the clock does not measure what the ruling says it
measures.

### 3. Measured harm

wo-a577c9fd: 116 Bash calls, 45.4s mean, a live turn 10.6m in, **0% idle**. The sweep
judged it "0.1h of active time past the 0.1h threshold for running, judged on time since
its last activity" and opened **inv-ef8def7e**, whose own brief admits "nothing the record
can name" blocks it — `stuck_scan`'s empty-blocker fallback (`ops.py:9743-9746`) printed
over a record whose `wo_turns` row said a turn was running. One investigation session
bought, plus its attention item, to be told the worker was working.

`stuck.assess` has no cause available for this: the composed `excluded_cause` at
`ops.py:9730-9733` covers `PAUSE_USAGE_LIMIT`, `VALIDATION_CI_CAUSE` and
`stuck.LIVE_DEPENDENCY` only, and the other two exclusions are `HOLD_EXEMPT_KIND` (an
`exempt_kinds` member) and `IDLE_MANAGER_CAUSE` (`by_design_idle`, status `idle`). A
`kind='worker'` order in `running` can match none of the five.

## The fix

One named cause, one bool parameter, one caller-computed scalar. No new catalog key, no
schema change, no new store reader.

### 1. The cause, in `stuck.py`, beside the other two it names itself

```python
TURN_IN_FLIGHT_CAUSE = "a worker turn is in flight"
```

Next to `HOLD_EXEMPT_KIND` / `IDLE_MANAGER_CAUSE` (`stuck.py:40-41`). `_CAUSE`-suffixed
and a **phrase, not a slug**, on `IDLE_MANAGER_CAUSE`'s precedent: the value is rendered
raw in the `excluded` column of `jarvis stuck` and `/stuck` (`stuck.html:60`), so it must
read as English there. The wording is the issue's Expected, unmodified.

### 2. Its own sentence in `EXCLUSION_REASON`

```python
TURN_IN_FLIGHT_CAUSE: ("a worker turn is in flight and writes nothing to the timeline "
                       "while its tools run — it clears by itself when the turn ends, so "
                       "nothing is judged while it does"),
```

**Why not fall through to `DEFAULT_EXCLUSION_REASON`.** Its substance is right — a turn
ends by itself, which is exactly the self-clearing test kn-6e836793 and the CI spec's
reconciliation apply — but its text is `"held by {cause}, which clears by itself — nothing
is judged while it does"`, and `{cause}` here is a sentence fragment. Composed, it reads
"held by a worker turn is in flight, which clears by itself". Same defect the 2026-10-12
spec fixed for its two causes, same remedy: keep the default's clause **"clears by itself
— nothing is judged while it does"** verbatim so the five causes read as one family, and
prepend the fact the reader of an excluded row actually needs — that the silence is the
turn's shape, not the order's state. No `{cause}` / `{kind}` slot: `assess` calls
`.format(cause=…, kind=…)` on whichever sentence it picks, so unused slots are free and
stray braces are not permitted.

### 3. No held-seconds discount, and this is a decision not an omission

kn-0a222b38 is the pair rule: on a status judged on **time in status**, an exclusion alone
only postpones the defect, so the held seconds must be discounted too — the CI spec's §1(2)
("the moment the hold lifts, `seconds_in_status` is already past 15 minutes").

**It does not apply here, because `running` is in `stuck.ACTIVITY_STATUSES`**
(`stuck.py:28`) and is therefore judged on the ACTIVITY clock. The pair rule's second half
exists for a clock that keeps running through the hold; this clock **restarts by itself the
moment the hold ends**: every reap path writes a `wo_events` row — `turn_ended`,
`turn_failed`, `turn_paused`, `turn_budget_exhausted` (`worker_session.py:892`, `:966`,
`:997`) — and `finish_turn` stamps `wo_turns.ended_at`, which is a second `_activity_of`
source (`ops.py:2041`). None of those kinds is in `health.observer_kinds()`
(`health.py:82`), so all of them count as activity. The first sweep after the turn settles
therefore sees an activity age of seconds, not of the turn's length.

So `discounted_seconds` is untouched: `holds.union_seconds` keeps its two contributors
(usage spans, CI spans) and `holds.HOLD_CAUSES` gains nothing. A discount here would be
actively wrong in the other direction — it would hand a worker that has gone quiet AFTER a
long turn a grace period equal to that turn's length, which is the stuck class the
5-minute threshold exists to catch.

### 4. `turn_in_flight: bool = False` on the pure `assess`

```python
def assess(status: str, seconds_in_status: float, seconds_since_activity: float,
           discounted_seconds: float, thresholds: Mapping[str, float],
           fallback_seconds: float,
           activity_statuses: Sequence[str] = ACTIVITY_STATUSES,
           excluded_cause: str = "",
           kind: str = "worker",
           exempt_kinds: Sequence[str] = (),
           by_design_idle: bool = False,
           turn_in_flight: bool = False) -> Verdict:
```

Last parameter, defaulting False, keyword-passed at the one call site — `by_design_idle`'s
shape exactly (`stuck.py:95-102`), so every existing caller and every test that passes
positionals is unchanged.

**A caller-computed scalar, so `stuck.py` still opens nothing.** The module's own docstring
is the rule — "nothing here calls a model, opens a store, reads a file or writes a row"
(`stuck.py:1-14`) — and `health.due`'s reason it quotes: a spend decision that reads state
inside itself is untestable without a store. `stuck.py` gains no import of
`worker_session`, `project_store` or anything else.

**Why inside `assess` and not a filter before it.** The 2026-10-12 spec §1's argument,
unchanged: a decision taken at the call site has no unit test and no `Verdict` to show for
it, which is how the `continue`s became the only part of the sweep with nothing behind them.

### 5. Precedence: exempt kind, `excluded_cause`, `by_design_idle`, `turn_in_flight`

`turn_in_flight` is the LAST `elif` in the existing chain inside `assess`
(`if kind in exempt_kinds` / `elif excluded_cause` / `elif by_design_idle`), and it
short-circuits to `stuck=False` with `active_seconds` and `threshold_seconds` populated
like its siblings.

Why last, in two parts:

1. **Against the first two it must lose.** An exempt kind is not a subject at all, and
   `excluded_cause` carries fleet-wide facts (the account's usage window, a live `jarvis
   pause`, the reopen ramp) plus the CI hold and a live dependency. A fact about the whole
   fleet outranks a fact about one order — the 2026-10-12 spec's rule, verbatim in intent.
   An order whose turn is in flight while the account's window is shut should report the
   window: that is the thing an operator can act on.
2. **Against `by_design_idle` the order is immaterial, which is why last is the right
   place.** `by_design_idle` is gated on status `idle` at the caller and `turn_in_flight`
   on status `running`; the two can never be true together on a real row. Appending rather
   than inserting leaves the shipped three-link chain byte-identical, so no existing
   verdict can change. The pair is still pinned by a test (§8.3) so a later edit cannot
   silently reorder the first three.

### 6. The caller: one line in `ops.stuck_scan`, gated on `running`

Beside `by_design_idle` (`ops.py:9722-9726`), before the `assess` call:

```python
turn_in_flight = bool(wo["status"] == "running"
                      and worker_session.busy(pstore, wo["id"]))
```

passed as `turn_in_flight=turn_in_flight`. The function-local import at `ops.py:9670`
becomes `from . import bus, holds, stuck, worker_session`; `from .worker_session import
PAUSE_USAGE_LIMIT` (`ops.py:9675`) stays as it is. `worker_session.busy` is spelled through
the module to match the five other sites that call it (`ops.py:6279`, `ops.py:6376`,
`invariants.py:3412`, `daemon.py:9261`, `remedies.py:770`) — one spelling, as the sweep's
own alias comment at `ops.py:9612-9614` insists.

**`busy` is reused, not reimplemented.** `worker_session.busy` (`worker_session.py:235`)
is `store.latest_turn(wo_id)` filtered to `state == "running"`, one indexed read, and it is
the OS's single definition of "this order has a turn in flight" — the same read
`_manager_handoff`, the rebind guard and the reconciler gate on. A second definition in the
sweep is the defect the 2026-10-12 spec removed one module along.

**Gated on `running` EXPLICITLY, the way `by_design_idle` is gated on `idle` — never
universally.** A turn row can be `running` while the work order is in another status: a
worker typing its reply to a rejection, a manager mid-turn, an order the reconciler has not
settled yet. In `needs_review` the wait is owed by the **USER**, and hiding a user-owed wait
is the one thing the sweep exists to surface — §1 and §2.2 of the CI spec, and the same
reason that spec gated its exclusion on `validating` alone. §3 of the 2026-09-30 spec is
blunter still: do not add the filter.

**Cost: one indexed read per `running` order per sweep, and nothing for any other status** —
the gating rule every `true_blockers` branch follows ("the read sits behind the status
check, so no other work order pays for it", `invariants.py:1046`).

### 7. Never a `continue`, and nothing to clear on settle

**No `continue` at the call site** (kn-143a46df, kn-e91ff3a5). The order stays scanned, the
`Verdict` comes back `stuck=False` with `excluded == TURN_IN_FLIGHT_CAUSE` and both numbers
populated, so the row appears on `jarvis stuck` and `/stuck` **with the arithmetic** —
`stuck_only=True` drops it before `true_blockers` and before the candidate list, exactly as
the other four causes behave. A `continue` is indistinguishable from an order comfortably
inside its threshold, which is the difference between "the OS decided not to judge this and
says so" and "the OS forgot about it".

**Nothing to clear, no backfill, no state.** `busy` re-derives from the latest turn row
every tick; `poll` reaps a turn whose process has ended on the tick it dies. There is no
flag to lower and no migration — an order excluded from the next sweep onwards and judged
again the sweep after its turn ends.

**One copy change.** `src/jarvis/ui/templates/stuck.html:43-47` enumerates the causes
("the account's usage window, a fleet pause, the reopen ramp, or a validation round waiting
for GitHub … as does one of a kind this sweep never judges, and a manager whose feature has
not finished with it"). It gains the sixth clause — **a worker whose turn is in flight** —
or the page is lying about its own column. `stuck.assess`'s docstring gains the same
sentence; `cli.cmd_stuck` prints `row['reason']` and needs nothing.

`stuck.WHY` needs nothing: an excluded order is never a candidate, so no investigator ever
receives this sentence.

### 8. Tests

`tests/test_health_sweep.py`, in the stuck-sweep section (its `stuck_catalog` / `stuck_os`
fixtures, `park_order` at `:1263`, `usage_hold` at `:1283`, `ci_hold` at `:1345`, `_judged`
at `:1364`, `HOUR`).

Pure-predicate units, one per behaviour, beside
`test_assess_exclusion_precedence_is_kind_then_fleet_then_feature` (`:1721`) and in
`test_usage_limit_hold_is_not_stuck`'s shape (`:1297`) — assertion plus the control that
stops it passing against a threshold that can never fire:

1. **`test_a_turn_in_flight_is_not_stuck`** — the incident's scalars:
   `stuck.assess("running", 10.6 * 60, 10.6 * 60, 0.0, {"running": 5 * 60}, HOUR,
   turn_in_flight=True)` gives `stuck is False`,
   `excluded == stuck.TURN_IN_FLIGHT_CAUSE`, `clock == "activity"`,
   `active_seconds == 10.6 * 60` and `threshold_seconds == 5 * 60` (the row still shows
   the arithmetic — §7). `reason` contains the new sentence.
2. **`test_running_with_no_turn_in_flight_is_judged_as_today`** — the same call with the
   parameter left at its default: `stuck is True`, `excluded == ""`, and `reason` equal to
   today's string character for character
   (`"… of active time past the 0.1h threshold for running, judged on time since its last
   activity"`). This is the regression guard: the exclusion must not generalise to every
   `running` order.
3. **`test_assess_precedence_puts_turn_in_flight_last`** — four cases on one `judged(**flags)`
   helper over status `running`: all flags set gives `HOLD_EXEMPT_KIND`; fleet plus
   `turn_in_flight` gives `PAUSE_USAGE_LIMIT`; `by_design_idle` plus `turn_in_flight` gives
   `IDLE_MANAGER_CAUSE` (immaterial in practice, pinned so the chain cannot be reordered
   silently — §5.2); `turn_in_flight` alone gives `TURN_IN_FLIGHT_CAUSE`. All four
   `stuck is False` with both numbers preserved; the control with no flags is
   `stuck is True`.

Integration, through `ops.stuck_scan` with a **real running turn row**:

4. **`test_a_live_turn_buys_no_investigation`.** One new helper beside `usage_hold` /
   `ci_hold`, in their shape and for `park_order`'s stated reason (the store stamps
   `db.now()` and there is no back-dating call):

   ```python
   def running_turn(store, wo_id: str, started: float) -> dict:
       """One turn in flight, started `started` ago. `wo_turns.state` defaults to
       'running' (project_store.py:1078), so `worker_session.busy` needs nothing else."""
       turn = store.create_turn(wo_id, "dispatch", "go")
       store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                          (started, turn["id"]))
       store.conn.commit()
       return turn
   ```

   The real store call is `ProjectStore.create_turn` (`project_store.py:4531`); the SQL
   back-date is **load-bearing** — `create_turn` stamps `started_at = db.now()`, and
   `wo_turns.MAX(started_at)` is an `_activity_of` source (`ops.py:2041`), so a turn row
   written at `now` would reset the activity clock and the test would pass without the fix.

   Body: `park_order(store, wo["id"], "running", hours=0.2)` (12 minutes, past the shipped
   5-minute `running` threshold), `running_turn(store, wo["id"], started=at)`, then
   `stuck_os["daemon"].stuck_tick(None)`. Assert
   `ops.list_investigation_orders("proj_a", include_settled=True) == []`, and — the
   anti-`continue` assertion — that the order's row in `_judged(store, now)` is present with
   `verdict.excluded == stuck.TURN_IN_FLIGHT_CAUSE`, `verdict.stuck is False` and
   `verdict.active_seconds` ≈ 12 minutes.
5. **The control, in the same test.** A second order parked identically in `running` with
   **no turn row at all** IS a candidate and DOES buy one investigation. A finished turn is
   not the control to use: `finish_turn` stamps `ended_at = db.now()`, which restarts the
   activity clock and would make it green for the wrong reason.
6. **`test_a_live_turn_outside_running_is_still_judged`** — §6's scoping rule, and without
   it the `status == "running"` gate is untested. An order parked in `needs_review` past its
   threshold with a `running_turn` on record: a candidate, `excluded == ""`. Repeat with
   `idle` if the manager fixtures (`:1614`) make it cheap.

No new `tests/test_ui.py` case: the `/stuck` render test already covers the page, and the
copy change in §7 is prose.

### 9. Rejected alternatives

1. **Fix the activity clock instead — write a heartbeat event per tool call, or harden the
   transcript proxy.** Loses on cost and on layering. The incident's turn made 116 Bash
   calls; a per-call `wo_events` row is 116 writes per turn plus a `fingerprint`-perturbing
   event class to add to `health.observer_kinds()`. Hardening the transcript read keeps a
   `stat()`-based proxy as the answer to a question `wo_turns.state` answers exactly — and
   `inspection.py:1879-1882` already rules the underlying signal unreliable, because Claude
   Code appends an assistant row only when a content block completes.
2. **Raise `thresholds["running"]`.** Moves the false positive instead of removing it. At
   45.4s mean per tool call a turn is silent for as long as its longest tool, which is
   unbounded; and the user's 2026-10-01 ruling (Neo 1148) is explicit that five minutes of
   genuine idle in `running` means something is wrong. A threshold big enough to cover a
   long turn blinds the sweep to the case the number was chosen for.
3. **Exclude on `busy` for every status, not just `running`.** §6: a `needs_review` order
   owes the USER, and the sweep exists to surface exactly that. Same refusal the CI spec
   made for the `validating`/`needs_review` overlap.
4. **`continue` in `stuck_scan` when a turn is in flight.** kn-143a46df, kn-e91ff3a5: the
   row vanishes from `jarvis stuck` and `/stuck`, and half the spend decision moves to a
   site with no unit test and no `Verdict`. One line, and it is the shape that produced this
   class of defect twice already.
5. **Discount the in-flight turn's seconds as held seconds.** §3: kn-0a222b38's pair rule is
   about a status judged on time in status; `running` is on the activity clock, which
   restarts by itself at the reap. The discount would additionally grant a worker that went
   quiet after a long turn a grace period equal to that turn's length.
6. **Drop `running` from `ACTIVITY_STATUSES` so it is judged on time in status.** Strictly
   worse: every working order crosses its threshold once, by entry age alone.
7. **Read `latest_turn` inside `stuck.py`.** The module docstring and `health.due`'s rule
   (§4); it would make the spend decision untestable without a database.
8. **A `fleet_health` catalog key to switch this off.** Nothing to calibrate: the parameter
   is a fact about the record, not a threshold. kn-1cec46b5 is about numbers a surface judges
   by, and there is no number here.

### 10. Not covered, and what is uncertain

* **Which activity source failed on wo-a577c9fd is NOT diagnosed.** The transcript proxy
  should have moved during that turn and apparently did not; whether the cause was
  `session_id`/`worktree` path resolution, content-block flush latency, or a subagent
  transcript elsewhere is unanswered, and the fix does not depend on the answer — it stops
  reading the proxy for this question. If the proxy is wrong in other readers (`jarvis wo
  show`'s idle line, `rules.FACT_FIELDS`' `seconds_since_activity`) that is a second order,
  and naming it is not this spec's job.
* **The new blind spot, stated rather than hidden.** While a turn row says `running` the
  order is excluded, so a turn whose process has died is invisible to the sweep until it is
  reaped. The window is one daemon tick: `worker_session.poll` checks
  `claude_cli.process_alive` for every running turn every tick (5s default), and a turn whose
  process lives but has hung is reported by `TURN_STALL_SECONDS` (6h,
  `worker_session.py:59`) and by the burn alarms (`inspect.alarm_turn_minutes`). This spec
  adds no coverage there and changes none.
* **inv-ef8def7e is not cleaned up.** No retraction, no backfill; §7's re-derivation means
  the next sweep simply stops repeating it. Closing the existing investigation is the user's
  call.
* **Feature orders.** Unswept today (2026-09-30 §11) and unchanged. A `kind='manager'` or
  `kind='planner'` work order in `running` gets this exclusion like any other — intended:
  it is the same fact about the same table.
* **`holds.HOLD_CAUSES` is not extended**, for the CI spec's reason: nothing renders
  `Verdict.excluded` through it, and its keys drive `jarvis inspect` and the burn alarms,
  which judge a different question.
