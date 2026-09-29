# A usage limit is not a failed sweep

Work order wo-8a68e024, issue #835. The third time the same ordering bug has been fixed in
a different caller: `neo.drain_queue` learned it from issue #235, `Daemon.validation_tick`
learned it from the same issue, and the health sweep never did.

## The problem

### 1. `review_health` catches the account's refusal as a transport failure

`src/jarvis/supervisor.py:920` (in `review_health`, body 852-969) has exactly one except
clause around the model call:

```python
    except claude_cli.ClaudeCliError as exc:
        reply = _nothing_found(f"the health sweep could not be reached: "
                               f"{_clip(str(exc), cfg.reason_chars)}")
```

`claude_cli.UsageLimitError` is a SUBCLASS of `ClaudeCliError`, so an account-window
refusal takes that branch. Consequences, in order:

1. `reply["failed"]` is true, so :930-938 writes
   `record_health_review(..., outcome="failed", detail="the health sweep could not be
   reached: You've hit your session limit · resets 2:10am")` and returns
   `{"outcome": "failed"}`.
2. `last_health_attempt_ts` (src/jarvis/project_store.py:3346-3362) counts that row, so
   `health.due`'s floor (src/jarvis/health.py:111-112) defers the next look by one
   interval — and then allows it, still inside the same window. The sweep re-buys the
   refusal every interval for the whole window, on every due unit, in every project.
3. After ten such rows `invariants.check_health_sweep_produces_judgements`
   (src/jarvis/invariants.py:2679-2724, INV-HEALTH-SWEEP-MUTE) reads
   `recent_health_reviews(10)`, finds every outcome `failed` and the newest inside
   `HEALTH_SWEEP_FAILURE_WINDOW_MINUTES`, and yields a violation. `Daemon.check_invariants`
   (src/jarvis/daemon.py:3982-3988) turns a non-repairable violation into a notification,
   which reaches Telegram. The OS tells the user its health sweep is broken when what
   actually happened is that the account was asleep.

The detail string it reports is the refusal message, so the alarm is not even wrong about
the facts — it is wrong about whose fault they are, and it asks the user to fix a prompt.

The two callers that got this right:

- `neo.drain_queue`, src/jarvis/neo.py:418-427 — `except claude_cli.UsageLimitError`
  FIRST, `reopens = e.limit.reset_at or (time.time() + RATE_LIMIT_FALLBACK_DELAY)`,
  `store.hold_claim(...)`, outcome `"held"`, no retry spent. Its own comment names the
  rule: "BEFORE the generic outage below, and the ordering is the fix: a spent window is
  not a transport fault and must not spend a retry (issue #235)".
- `Daemon.validation_tick` / `_hold_rounds_for_outage`, src/jarvis/daemon.py:1763-1829 —
  reads the account's state BEFORE buying a seat and records a hold instead.

kn-96bc2417 is that lesson written down. `review_health` predates neither.

### 2. Nothing guarantees the OS's own sweep runs at all

USER RULE, 2026-09-28, kn-7312c7de: the OS's own project must ALWAYS have the health sweep
on and producing judgements. Today nothing enforces it and two things work against it:

- `supervisor.health_enabled` ships **False** (src/jarvis/catalog.py), and
  `Daemon._health_projects` (src/jarvis/daemon.py:3724-3736) requires
  `p.supervisor.enabled AND p.supervisor.health_enabled`. Either switch, set false or
  simply unset, takes the OS's sweep dark.
- `jarvis config set` / `unset` reach those keys like any other, through `ops.set_config`
  (src/jarvis/ops.py:9301-9328) and `ops.unset_config` (:9331-9356). Neither has any
  notion of a setting that may not be turned off.

And INV-HEALTH-SWEEP-MUTE is deliberately silent on a project that is not sweeping — its
docstring says so at :2706-2707 ("Silent on a project that has never swept: the sweep ships
disabled, and no rows is not a run of failures"). Correct for an ordinary project; for the
OS-owning project it means the one state the user asked to be impossible is the one state
nothing reports.

### 3. Root cause, named

One root cause with two faces: **the health sweep has no concept of "not now"**. Its
outcome space is `("clear", "findings", "failed")` (src/jarvis/project_store.py:519) — a
judgement, a judgement, or a defect. An account window is none of those, so it is recorded
as the only thing left, and every reader downstream is then honest about the wrong fact.
The fix adds the missing outcome rather than widening an except clause.

## The fix

### 1. `held` is a fourth health outcome

src/jarvis/project_store.py:

- `HEALTH_OUTCOMES = ("clear", "findings", "failed", "held")` (:519). Extend the comment
  above it: `held` is neither a judgement nor a defect — the account refused the call, so
  nothing was judged and nothing is broken.
- `health_reviews` (CREATE TABLE at :699-709) gains
  `reopens_at REAL NOT NULL DEFAULT 0` — the moment the hold expires, `0` on every other
  outcome. Added to `ADDED_COLUMNS["health_reviews"]` (:1076 onward) as well, because this
  table already ships and a live database gets the column only there.
- `record_health_review` (:3314-3327) takes `reopens_at: float = 0.0` and writes it.
- `last_health_review` (:3329-3344) — its predicate becomes
  `AND outcome NOT IN ('failed','held')`. Same reasoning as the existing exclusion: a hold
  recorded no judgement, so it must not suppress the retry by looking like a look.
- `last_health_attempt_ts` (:3346-3362) — **a held row must NOT count**, predicate
  `AND outcome!='held'`. This is the half that stops the retry storm being replaced by a
  stall: a refusal bought no model call, so flooring the next real look on it would make
  one refused tick cost a whole interval of watching. It still counts a `failed` row, for
  the reason in its docstring (issue #216): that one DID spend money.
- `recent_health_reviews` (:3371-3381) gains `include_held: bool = True`. With it false the
  query adds `WHERE outcome!='held'`. Default true so `jarvis doctor`-style readers keep
  seeing everything; §6 is the one caller that passes false.

A `held` row therefore changes nothing about scheduling except through §3's explicit read.

### 2. The ordering in `review_health` is the fix

src/jarvis/supervisor.py, immediately BEFORE the `except claude_cli.ClaudeCliError` at
:920, and the position is the mechanism — Python takes the first matching clause, so a
subclass caught after its base is dead code:

```python
    except claude_cli.UsageLimitError as exc:
        # BEFORE the generic outage below, and the ordering is the fix: a spent window
        # is not a transport fault and must not be recorded as a broken sweep
        # (issue #235's lesson, kn-96bc2417; `neo.drain_queue` src/jarvis/neo.py:418).
        from .worker_session import RATE_LIMIT_FALLBACK_DELAY

        reopens = exc.limit.reset_at or (time.time() + RATE_LIMIT_FALLBACK_DELAY)
        pstore.record_health_review(kind, subject_id, fingerprint=fingerprint,
                                    trigger=trigger, outcome="held",
                                    detail=_clip(exc.limit.message, cfg.reason_chars),
                                    reopens_at=reopens)
        log.info("[%s] health sweep of %s held until the usage window reopens",
                 project, subject_id)
        return {**_nothing_found(exc.limit.message), "raised": [], "outcome": "held"}
```

What it does NOT do, each deliberate:

- **no alarm and no finding** — there is nothing to report about the unit;
- **no `health_reviewed` event** — that event means "a judgement happened" and every
  timeline reader takes it that way;
- **no attention flag** — the user has nothing to do about a usage window;
- **no `log.warning`** — this is not a failure, and the log line is what a human greps.

`detail` carries `exc.limit.message` (the refusal as the account phrased it, e.g.
`You've hit your session limit · resets 2:10am`), NOT prefixed with
`the health sweep could not be reached` — that sentence is the false claim this spec
removes.

### 3. The hold is read BEFORE the next call is bought

A recorded hold that nobody reads is just a quieter retry storm. Two reads, one per unit
and one per account.

**`ProjectStore.health_sweep_hold()`** — new, beside `recent_health_reviews`
(src/jarvis/project_store.py:3371):

```python
def health_sweep_hold(self) -> tuple[float, str] | None:
    """The project's newest usage-limit hold if it has not expired — (reopens_at, detail).

    PROJECT-WIDE, not per unit: the account's window is not a property of a work order,
    and a per-unit read would re-buy the refusal once per candidate.
    """
```

One indexed read: newest `outcome='held'` row, returned only while
`reopens_at > db.now()`.

**`Daemon._health_sweep`** (src/jarvis/daemon.py:3793-3814) — per project, before
`_health_candidates` is computed: if `pstore.health_sweep_hold()` is not None, log once at
debug and `continue`. Nothing is swept, no candidates are derived, no row is written. The
hold row that exists is the record.

**The account half.** `Daemon.health_tick` (:3738-3751) gains
`state: fleet.Fleet | None = None` and the tick's call site (:905) passes the one reading
already taken at :742 — the same value `validation_tick` gets, for the same reason it is
read once per tick. `health_tick` threads it into `_health_sweep(due, state)`; the store is
opened on the health thread, so `health_tick` itself still opens nothing (its docstring's
promise at :3743-3744 holds). In `_health_sweep`, when `state is not None and state.shut()`
(src/jarvis/fleet.py:92-104):

- record ONE hold per project per window, then `continue` — sweep nothing;
- **deduped on the MOMENT, not on the tick**: skip the write when
  `health_sweep_hold()` already returns a `reopens_at >= state.outage.reopens_at`. At a 5s
  tick a three-hour window would otherwise write two thousand identical rows per project;
  `_hold_rounds_for_outage`'s rule verbatim (daemon.py:1805-1807).
- **but do record it**, which is kn-22ba6087: a guard that returns early must still write
  down why. This row is the only place the account fact becomes readable to a
  per-project invariant — §5 cannot derive an account window from one work order's turns.

The account-level row has no unit. `record_health_review`'s subject assert therefore moves
off `ALARM_SUBJECTS` onto a new constant beside `HEALTH_OUTCOMES`:

```python
#: What a `health_reviews` row can be ABOUT. The two unit kinds, plus the ACCOUNT — a
#: usage window is not a property of any unit, and recording it against an arbitrary one
#: would make a fleet fact read as a work order's problem. Deliberately not a widening of
#: ALARM_SUBJECTS: no alarm is ever about an account.
HEALTH_SUBJECTS = ALARM_SUBJECTS + ("account",)
```

The account hold is written as `subject_kind="account"`, `subject_id=""`,
`fingerprint=""`, `trigger="account-window"`, `outcome="held"`, `detail=state.outage.message`
clipped to 500, `reopens_at=state.outage.reopens_at`. `health.TRIGGERS`
(src/jarvis/health.py:21) gains `"account-window"`. No existing reader is affected:
`_health_candidates`, `health_reviews_of` and `probes_reported_at` all query a real
`subject_kind`; `recent_health_reviews` sees it, which is exactly why §6 filters.

**Resumption needs no state to clear.** The first tick after `reopens_at` finds no live
hold and sweeps. Nothing un-holds anything.

### 4. The OS's own sweep cannot be switched off

In `ops`, so the CLI (`jarvis config set/unset`) and the dashboard's config console
inherit the refusal from one place — neither has its own validation layer.

New module-level helper beside `_key_path` (src/jarvis/ops.py:9131):

```python
#: The OS-owning project's own health sweep is not a setting the user may turn off —
#: USER RULE 2026-09-28, kn-7312c7de. Checked on the RESOLVED document, so it holds
#: however the value was spelled: at the project level, on the `os.` block it inherits
#: from, or by unsetting a key whose default is False.
OS_SWEEP_KEYS = ("supervisor.enabled", "supervisor.health_enabled")


def _refuse_os_sweep_off(document: dict[str, Any], resolved: dict[str, Any],
                         file: Path) -> None:
```

- The OS project is derived, NEVER hardcoded: `schedule.os_owner((p["name"], Path(p["path"]))
  for p in document["projects"]), fallback=False)`. `None` (no project contains the running
  install) means there is nothing to protect and the helper returns.
- **`fallback=False`, deliberately unlike `Daemon._os_owner`.** `os_owner`'s default picks
  the first project in catalog order when none holds the install, which answers "who runs
  the fleet checks once" — not "which project IS the OS". A project that merely happens to
  be listed first is not the OS and must not be refused this write.
- Predicate: for the owning project `name`, refuse when
  `resolved[f"projects.{name}.{k}"]` is falsy for either `k` in `OS_SWEEP_KEYS`.
  `config_version.resolve` materialises every default (src/jarvis/config_version.py:130-143),
  so both keys are always present and inheritance is already applied — this is what makes
  one predicate cover the project write, the `os.`-block write and the unset.
- Called in `set_config` after `after = _resolved_of(document, file)` (ops.py:9320) and in
  `unset_config` after the same line (:9348), i.e. against the document the write WOULD
  commit, and before `_commit_document`. Nothing is written on a refusal.
- `raise OpsError` naming the rule, the project and the key:

  ```
  projects.jarvis_os.supervisor.health_enabled would leave the health sweep of
  jarvis_os OFF, and that project runs the OS itself — its sweep must always be on and
  producing judgements (user rule, 2026-09-28). Change it on another project, or turn
  the sweep off nowhere.
  ```

  The project name in that message comes from the derivation, so a renamed or relocated OS
  checkout produces the right sentence with no edit.

### 5. INV-OS-HEALTH-SWEEP-DARK — a liveness check on the OS's own sweep

New checker in src/jarvis/invariants.py, beside
`check_health_sweep_produces_judgements` (:2679). Runs ONLY on the OS-owning project:
`schedule.os_owner(..., fallback=False)` over the live catalog's projects, compared against
the store's own project; any other project returns immediately, and so does an unresolvable
owner (an invariant must never be the thing that raises — `_validation_timeout`'s rule,
:2651-2663). `fallback=False` for the same reason as §4: the first project in catalog order
is an arbitrary pick, and an arbitrary project must never be told to run the OS's own sweep.

```python
#: How long the OS's own sweep may produce no judgement before the user is told. Six
#: missed sweeps at the shipped 30-minute floor: long enough that a transport blip, a
#: daemon restart or one capped tick cannot trip it, short enough that a sweep switched
#: off by a bad edit is reported the same working day. Time inside a live usage-limit
#: hold does not count against it.
OS_HEALTH_SWEEP_DARK_MINUTES = 180
```

Predicate: no `health_reviews` row with outcome `clear` or `findings` inside the window,
where the elapsed time EXCLUDES time spent inside a usage-limit hold — summed from the
`held` rows' `[ts, reopens_at]` intervals, the same active-clock shape the duration alarms
use. A sweep that is silent only because the account was is not dark.

The detail names WHICH of the three causes it is, because they have three different fixes:

| Cause | How it is told | Detail says |
|---|---|---|
| disabled | `supervisor.enabled` or `supervisor.health_enabled` false for this project in the LIVE catalog, read the way `_validation_timeout` reads `ops.validation_config()` — best-effort, `None` catalog means do not claim this cause | `the OS's own health sweep is DISABLED (supervisor.health_enabled=false) and must not be` |
| failing | newest row is `failed` | `… has produced no judgement for Nm; the newest sweep FAILED: <detail, 200 chars>` |
| not scheduled | enabled, and no rows at all | `… is enabled but has never run — no sweep has been scheduled` |

`repaired=False`, and NOT repairable: re-enabling the sweep would be the OS editing the
user's catalog, and a failing sweep's cause is not derivable from state.

**Notification level.** This one must not arrive as a `warning` beside a stale attention
flag. `invariants.Violation` (src/jarvis/invariants.py:451-471) gains a field
`level: str = "warning"`, and `Daemon.check_invariants` (src/jarvis/daemon.py:3982-3988)
passes `level=v.level` to `store.add_notification` instead of the literal `"warning"`.
`add_notification` already accepts `"critical"`. Every existing violation keeps its level
by default, so this is additive. The new checker yields `level="critical"`.

Register it in `invariants.check_project`'s checker list. NOT in `SLOW_INVARIANTS` — it
shells out to nothing, and a liveness check that runs hourly is a liveness check with an
hour of blind spot.

### 6. INV-HEALTH-SWEEP-MUTE tells failing from held

`check_health_sweep_produces_judgements` (src/jarvis/invariants.py:2679-2724):

1. read `store.recent_health_reviews(HEALTH_SWEEP_FAILURE_RUN, include_held=False)` — held
   rows are FILTERED OUT BEFORE the last ten are taken, not skipped in the loop. Two
   consequences, both wanted: ten holds can never trip it, and a hold in the middle of a
   genuine failure run does not RESET that run either. A window that interrupted a broken
   prompt did not fix the prompt.
2. return early while `store.health_sweep_hold()` is not None: the check claims the sweep
   IS SPENDING model calls right now, and during a hold it is spending none.
3. the detail says **failing**, not held —
   `"the last 10 health sweeps all FAILED (the account's usage window is a hold, not a
   failure), so the sweep is spending model calls and producing no judgement. Most recent
   reason: …"`. The word "held" is reserved for §3's rows so the two are never confused in
   an inbox line.

### 7. Verified non-finding: the sweep does not give up after a failure run

Issue #835 part (4) claims "the sweep silently gives up after the failure run". It does
not, and this is recorded here so nobody removes a give-up path that does not exist:

- `health.due` (src/jarvis/health.py:88-117) floors on `last_attempt` only. It counts no
  failures and has no failure branch.
- `Daemon._health_candidates` (src/jarvis/daemon.py:3753-3791) and `_health_sweep` have no
  failure predicate; the only cap is `health_max_units_per_tick`, a rotation
  (:3756-3759).
- `check_health_sweep_produces_judgements` yields a Violation. It writes nothing that
  scheduling reads.

The reporter's symptom was the hold-less retry storm of §1 plus the mute alarm firing — a
sweep that looks stopped because its every row is `failed` and its judgement rows are
absent. Fixing §1-§3 removes it without touching scheduling.

## Non-goals

- The 30-minute sweep floor, `health_every_ticks` and `health_max_units_per_tick` are not
  changed. Nothing here is about cadence.
- `supervisor.health_enabled`'s shipped default stays **False**. Other projects opt in;
  §4 only stops the OS's own project opting out.
- No retry, no backoff, no queue. A hold expires by the clock; the next tick sweeps.
- INV-HEALTH-SWEEP-MUTE is not deleted or merged into the new invariant. It answers "is
  this project's sweep broken" for every project; the new one answers "is the OS's own
  sweep alive" for one.

## Rejected alternatives

1. **Just add `UsageLimitError` to the existing except clause's tuple.** It is already
   caught — it is a subclass. Nothing about the record changes, which is the whole defect:
   the row would still say `failed`, still floor the next look, still trip the mute alarm.
   The ordering is the fix, and a tuple is not an ordering.
2. **Catch it, record nothing, return.** Cheapest edit and it loses the account fact
   entirely. §5's invariant would then have no way to tell "dark because the account was
   asleep" from "dark because somebody turned it off", and kn-22ba6087 exists because that
   trade was made once already.
3. **Record the hold as `failed` and teach the invariant to recognise the refusal message
   by its text.** Puts a model's (or a vendor's) prose in a predicate, and leaves the
   false claim in the DATA — `health_reviews.detail`, the log line, any future reader. The
   same reasoning that rejected teaching the supervisor's judge about awaiting turns in
   spec 2026-09-27.
4. **Skip the sweep whenever `fleet.shut()` without recording anything** (the pure guard).
   Correct behaviour, unreadable record: a per-project invariant cannot see the fleet
   object, so §5 would have to either alarm through every window or never alarm at all.
5. **Hardcode the OS project as `"jarvis_os"` in the `ops` refusal.** Breaks in production,
   where the OS project is a different checkout, and breaks on a rename with no test
   failing. `schedule.os_owner` already derives it from which directory contains the
   running `jarvis` package, and is what the daemon uses.
6. **Enforce the OS sweep in the daemon — `_health_projects` forcing it on for the owner —
   instead of refusing the write.** The catalog would then say one thing and the fleet do
   another, which is the shape of bug that takes a day to find. Refuse at the write; the
   record and the behaviour stay equal.
7. **A per-unit hold read instead of the project-wide one.** The refusal is an account
   fact. Per unit, a ten-candidate project buys ten refusals per tick to learn it ten
   times.
8. **Raise the mute alarm's `HEALTH_SWEEP_FAILURE_RUN` so a window cannot reach it.** The
   window is hours and the tick is seconds; no run length is both high enough to survive a
   window and low enough to catch a broken prompt. And it would still be counting the
   wrong thing.

## Tests

Drive the refusal through the REAL classifier, not by raising the exception by hand: the
fake CLI's 429 helpers, `fake_claude.refuse_seat` and `fake_claude.turns_rate_limited`,
already produce the output `claude_cli` parses into `UsageLimitError.limit`. A test that
raises the exception itself proves nothing about the except clause's ordering, because the
classifier is half of what is being fixed.

1. **A refusal records `held`, not `failed`, and costs no second call.** `review_health`
   against a rate-limited fake: the newest `health_reviews` row has `outcome='held'`,
   `reopens_at` equal to the parsed reset moment, `detail` is the refusal message with no
   `could not be reached` prefix; no `health_reviewed` event, no alarm, no attention flag.
   Then a second `_health_sweep` before `reopens_at` makes NO call at all (fake's call
   count unchanged) and writes no second row; the first tick after `reopens_at` sweeps
   again.
2. **A held row does not floor the next real look.**
   `last_health_attempt_ts` ignores it, `last_health_review` ignores it, and `health.due`
   therefore returns a trigger immediately once the hold expires.
3. **Ten held rows never trip INV-HEALTH-SWEEP-MUTE**, and one held row in the middle of
   ten failures does not save the sweep from it either — the genuine failure run still
   fires. A live hold keeps it silent even with ten `failed` rows present.
4. **`config set`/`unset` disabling the OS project's sweep is refused**, all four
   spellings: `supervisor.health_enabled false` and `supervisor.enabled false` on the
   owning project, the same two on the `os.` block when that is what the project resolves
   from, and `unset` of either. `OpsError` names the project and the key; the catalog file
   on disk is byte-identical afterwards and no `os_config_versions` row is written. The
   same write on a NON-owning project succeeds.
5. **INV-OS-HEALTH-SWEEP-DARK** fires with `level="critical"` on each of the three causes
   — disabled, newest-row-failed, enabled-and-never-run — with the cause named in the
   detail; is silent on an OS project whose newest row is `clear`; is silent for a
   non-owning project in every one of those states; and is SILENT through a usage-limit
   window long enough to exceed `OS_HEALTH_SWEEP_DARK_MINUTES` in wall clock.
   Plus: `Daemon.check_invariants` passes the level through, so the notification row has
   `level='critical'`, and every pre-existing violation still lands as `warning`.
6. **The fleet-shut guard records one hold per window, not one per tick.** With
   `fleet.Fleet.shut()` true, run twenty ticks: exactly ONE `subject_kind='account'` row
   exists, its `reopens_at` is the outage's, and `review_health` was never called. Raise
   the outage's `reopens_at` (a second, later window) and a second row appears.
7. **Live-database migration**: a `health_reviews` table created before this spec gains
   `reopens_at` through `ADDED_COLUMNS`, existing rows read back as `0.0`, and
   `health_sweep_hold()` returns None on them.
