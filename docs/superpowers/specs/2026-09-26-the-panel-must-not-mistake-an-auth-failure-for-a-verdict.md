# The panel must not mistake an auth failure for a verdict

GitHub issue #778. Work order wo-20150312. Design settled by Neo, question 723 — sections
"Rejected alternatives" and "Out of scope" record what was decided against and why; do not
re-open either.

## The problem

`validation.decide`'s all-seats-down branch (`src/jarvis/validation.py:1000-1017`) has
exactly one hold and everything else escalates:

```python
refusal = next((op.refused for op in opinions if op.refused is not None), None)
if refusal is not None:
    raise claude_cli.UsageLimitError(refusal)
return _out("escalated", "nobody could be reached to review this submission, so "
                         "the work has not been judged.", opinions, round_no=round_no)
```

`escalated` is in `COUNTED_VALIDATION_OUTCOMES` (`project_store.py:106`) and is terminal:
the round is spent, the work order goes `needs_review`, the user is flagged, and nothing
re-derives it.

Evidence, 2026-09-25/26. Four work orders — wo-ea5b9856, wo-3a7d9bda, wo-15f5d969,
wo-8d964c56 — had all four seats fail in ~1.5s each with

```
failed (rc=1): Failed to authenticate: OAuth session expired and could not be refreshed
```

The usage window had just reopened and many `claude` processes raced one OAuth refresh. The
refresh was transient: the same accounts' worker turns recovered by themselves through
`worker_session.PAUSE_AUTH`. Every one of those four rounds was closed `escalated` on the
FIRST auth failure, spent a round number, and was never retried. Four users' attention
bought nothing.

Why the existing budget did not catch it: `Daemon._validation_outage`
(`daemon.py:2137-2161`, `VALIDATION_OUTAGE_LIMIT = 3` at `daemon.py:282`) only runs when
the validator RAISES a `ClaudeCliError` (`daemon.py:1821-1826, 1849-1851`). `decide`
returned a verdict, so that path was never entered.

Root cause, stated plainly: **`decide` classifies exactly one kind of all-seats silence and
treats every other kind as a judgement.** A failure nobody could classify falls through to
the one outcome that costs a round number and a human.

## The fix

Classify the all-seats-down failure BEFORE escalating, and give an auth failure a hold of
its own on the same mechanism the usage window and CI already use.

### 1. The classifier — `validation.py:1000-1017`

The `replied` gate above it is unchanged: a seat that replied, even with garbage, counts as
reached and escalates exactly as today. Inside the branch, in this order:

1. usage-window refusal (`op.refused`) — today's behaviour, byte for byte.
2. auth failure on ANY seat — `raise claude_cli.AuthFailureError(auth)`.
3. ALL seats transient (`claude_cli.transient_failure(op.raw)` for every opinion) — `raise
   claude_cli.ClaudeCliError(…)`, so the existing `_validation_outage` budget applies
   unchanged.
4. only then `_out("escalated", …)`.

Order is load-bearing. Auth before transient for the same reason
`worker_session._diagnose` does it: an auth refusal is the one failure here that retrying
on a transport schedule cannot fix, and reading one as transient spends the 3-attempt
budget in ~15s against an account that can answer none of them. ALL seats for the transient
branch, not any: one seat with a 503 beside three that each failed differently is a panel
that is genuinely down.

Why here and not in the daemon: this is the only place the per-seat evidence exists. By the
time `_validate_work_order` sees anything, the opinions are gone and the verdict is a dict.

### 2. `Opinion.auth`, and the correction this spec makes to the brief

**`claude_cli.auth_failure(op.raw)` does not work and must not be written.** `_AUTH_RE`
(`claude_cli.py:1300-1310`) is anchored with `^` under `re.MULTILINE` — deliberately, so
that a worker writing PROSE about an expired login is not read as one — and
`claude_cli._cli_failure` (`:132-150`) hands the seat a single-line message PREFIXED with
`claude -p … failed (rc=1): `. The incident string above is that prefixed form. The anchor
never matches it, so a classifier reading `op.raw` would silently keep escalating.

So classify where the text is still unprefixed, mirroring the usage window end to end:

* `claude_cli._cli_failure` gains an auth branch, AFTER the `usage_limit` one (a spent
  window must keep winning; it is the failure that states a deadline) and before the
  generic return: `auth = auth_failure(detail)` and, if set,
  `return AuthFailureError(auth)`.
* `class AuthFailureError(ClaudeCliError)` beside `UsageLimitError`
  (`claude_cli.py:973-984`), same shape — `__init__(self, auth: AuthFailure)`,
  `super().__init__(auth.message)`, `self.auth = auth`. A `ClaudeCliError` subclass, so
  every existing `except claude_cli.ClaudeCliError` keeps working unchanged; what it adds
  is `.auth`.
* `seats.Opinion` gains `auth: claude_cli.AuthFailure | None = None`
  (`seats.py:135-192`), documented as `refused`'s twin: carried, never raised, because one
  seat that could not authenticate must not take down a panel the other three answered —
  that call belongs to `decide`.
* `seats._run_seat` (`:195-224`) sets it from the same `getattr` line that already carries
  `refused`: `auth=getattr(e, "auth", None)`.

`decide` then reads `op.auth`, not `op.raw`. The transient branch keeps reading `op.raw`:
`transient_failure` is not anchored, so the prefix is harmless there, and that failure
already has its owner in `_diagnose`.

### 3. The hold — `project_store.py:121-161`

```python
#: ...and this one is a round whose seats could not AUTHENTICATE.
VALIDATION_AUTH_CAUSE = "auth"

VALIDATION_HOLDING_CAUSES = frozenset({VALIDATION_HELD_CAUSE, VALIDATION_CI_CAUSE,
                                       VALIDATION_AUTH_CAUSE})
```

plus one row in `VALIDATION_STANDINGS`:

```python
("failed", VALIDATION_AUTH_CAUSE): ("held for authentication", "active", "◑"),
```

Membership in `VALIDATION_HOLDING_CAUSES` is the whole scheduler change. `validation_hold`
/ `validation_hold_until` (`:183-228`) are pure functions of the events and already filter
on that set, and `validation_tick`'s gate (`daemon.py:1616-1618`) already calls
`validation_hold_until`. Nothing in the tick is touched.

The round is closed `failed` with the cause, which is `RUNNABLE`
(`RUNNABLE_VALIDATION_OUTCOMES`) and outside `COUNTED_VALIDATION_OUTCOMES`: the next tick
owns the same round again and the submitter spends NO round number.

### 4. `Daemon._validation_auth_held` — new, `daemon.py` beside `_validation_ci_held` (`:2103`)

```python
AUTH_HOLD_BACKOFF = (60.0, 300.0, 900.0, 3600.0)   # 1m, 5m, 15m, then 60m for ever
```

Signature and body mirror `_validation_ci_held`:

```python
@staticmethod
def _validation_auth_held(store, wo, round_id, n, auth) -> None
```

* attempt = `1 + ` the count of this round's own `validation_failed` events with
  `cause == VALIDATION_AUTH_CAUSE`, read through `store.events_of_kind(wo_id,
  "validation_failed")` — the identical counting shape `_validation_outage` uses for
  `outages` (`:2148-2151`), and counted from the events for the identical reason: a daemon
  restart must not hand the round a fresh schedule.
* `reopens = time.time() + AUTH_HOLD_BACKOFF[min(attempt - 1, len(AUTH_HOLD_BACKOFF) - 1)]`
  — the last entry is a CAP, not a final attempt.
* `store.close_validation_round(round_id, "failed", invariants.AUTH_HOLD_NOTE,
  hold_cause=VALIDATION_AUTH_CAUSE)`.
* `store.add_event(wo_id, "validation_failed", {"round": n, "cause":
  VALIDATION_AUTH_CAUSE, "reopens_at": reopens, "attempt": attempt, "error":
  auth.message[:500]})`.

**It never escalates on a count.** `attempt` is recorded so a reader can see the streak and
so the backoff is derivable, and it is compared against nothing. The reasoning is
`worker_session.TurnPause.exhausted`'s (`worker_session.py:962-972`) one authority along:
what clears an auth failure is a human, `/login` may be thirty seconds or next week away,
and a count that gives up turns something a sign-in fixes into an attention item and a
spent round. The 60m cap is what bounds the cost of waiting for ever — one round, four
seats, ~1.5s and zero tokens each, once an hour.

No `not recorded` backstop, for `_validation_ci_held`'s reason (`:2140-2145`): this holds on
a moment, and an unwritten event costs one unnecessary retry on the next tick rather than a
silent stall.

### 5. The work-order validate loop — `daemon.py:1842-1851`

A third branch, BEFORE the generic outage and beside the usage one:

```python
if isinstance(failure, claude_cli.AuthFailureError):
    self._validation_auth_held(store, wo, round_id, n, failure.auth)
    log.info("[%s] %s: round %d held until Claude Code can authenticate", …)
    return
```

Order in this `isinstance` chain is Neo's HARD CONDITION: an auth failure must never reach
`_validation_outage` and so can never consume one of its 3 attempts. Test (c) pins it.

### 6. The feature-order twin — `daemon.py:2380-2390`

That loop catches on the `try` rather than storing the failure, so add a third `except`
clause between the two existing ones (`except claude_cli.AuthFailureError as e:` — a
subclass of `ClaudeCliError`, so it MUST precede the generic clause at `:2388` or Python
will never reach it), calling a new

```python
def _feature_auth_held(self, store, fo, round_id, n, auth) -> None
```

placed beside `_feature_held` (`:2531-2558`). It is `_validation_auth_held` with one
difference, the same one `_feature_held` and `_feature_outage` already have:
`wo_events.wo_id` is a foreign key into `work_orders`, so the events are written with
`ops.feature_event` and counted through `ops.feature_events_of_kind(store, fo_id,
"validation_failed")`, carrying `"feature_order": fo_id` in the payload.

Not `@staticmethod`, matching `_feature_outage`.

### 7. Surfaces — the cause must read as an ACTIVE hold, never as a failure or a give-up

`VALIDATION_CI_CAUSE`'s precedent is followed exactly: `reopens_at` here is a RECHECK
INTERVAL, not a moment anything promised, so **no surface prints the moment**. See the
`CI_HOLD_NOTE` comment (`invariants.py:1048-1052`). Each surface says what the round is
waiting FOR — Claude Code authentication — so the hold is never silent.

* `invariants.py`: new `AUTH_HOLD_NOTE = "waiting for Claude Code authentication"` beside
  `CI_HOLD_NOTE`, and `validation_hold_note` (`:1055-1066`) becomes a three-way dispatch on
  the cause instead of a ternary. `usage_hold_note` is the only branch that names a moment.
  The wording reuses `worker_session.PAUSE_NOUN[PAUSE_AUTH]` — "Claude Code
  authentication" — because a user reading a quiet fleet must not have to learn that the
  worker-side pause and the panel-side hold are two different things. Both readers
  (`parallel_round_note` `:1069`, and `:1167`) inherit it with no edit.
* `timeline.py:592-620`: a fourth cause branch, before the generic unreachable-reviewer
  fallthrough — `("Validation held — Claude Code could not authenticate", f"attempt
  {attempt}: {error}")`. No `_clock(reopens_at)`, unlike the `usage_limit` branch two lines
  up, and the branch carries a comment saying why.
* `ui/templates/_validation.html`: **no template edit.** The badge is already keyed on
  `project_store.validation_standing` (`:31-38`), so the new row in `VALIDATION_STANDINGS`
  renders `held for authentication` toned `active`. The same is true of `ops.round_line`
  and `automerge.decide`'s hold sentence. What this costs is a test (h) that pins the
  rendering, because the thing that could regress is the table row, not the markup.
* `holds.py:249-260`: the `validation_failed` opener also opens on
  `VALIDATION_AUTH_CAUSE`, keyed `worker_session.PAUSE_AUTH` (already in `HOLD_CAUSES` at
  `:80`, labelled "an expired Claude Code sign-in"), and the
  `validation_submitted`/`validation_forced` closer pops that key too. Without this, an
  hour of auth hold is counted as active time in `jarvis inspect`, which is the same
  mis-accounting the usage-limit opener beside it exists to prevent.

## Rejected alternatives

**Gate the re-run on `claude_cli.signin_changed_at()`, as `worker_session.PAUSE_AUTH` does**
(`worker_session.py:896-908`, `_auth_retry_at`). Rejected by Neo, question 723, for two
independent reasons. It returns None for a keychain or `ANTHROPIC_API_KEY` sign-in, and
"cannot tell" there means NEVER — the round would be stranded for ever, which is worse than
the defect being fixed. And `project_store.validation_hold` is documented as a pure
function of rows; a live `stat` of the credentials file does not belong inside it. The
worker side can afford that clock because a worker turn costs real tokens; a re-run of a
panel whose seats fail on auth costs ~1.5s and zero tokens per seat, so a blind 60m poll is
cheaper than the machinery to be clever about it.

**Escalate after N auth holds.** The failure it produces is the one this spec is fixing,
merely later, and `failed`-adjacent give-ups on auth have already cost a feature order once
(`TurnPause.exhausted`, fo-e353491c, 2026-08-27).

**Match `claude_cli.auth_failure(op.raw)` in `decide`.** Does not work; see §2. Keeping it
by un-anchoring `_AUTH_RE` is worse — this repo's own workers write prose about expired
logins, and that anchor is what stops a work order being held for discussing one.

**Treat the auth failure as transient and widen `VALIDATION_OUTAGE_LIMIT`.** Three
consecutive ticks spend that budget in ~15s; widening it enough to cover an OAuth refresh
race would also change what a genuine transport outage does. Separate fight — see below.

## Out of scope (stated limitation)

`VALIDATION_OUTAGE_LIMIT = 3` is still spent on CONSECUTIVE TICKS, so an all-seats
transient round still escalates about fifteen seconds after the first failure. That is
GitHub issue #235's remaining half and Neo agreed it stays out of this work order. This
spec routes the auth case AWAY from that budget and changes nothing about it; the transient
branch in §1 exists so that a panel that is genuinely down reaches the budget it was always
meant to reach, not so that the budget is repaired.

## Tests

New file `tests/test_validation_auth_hold.py`, modelled on
`tests/test_validation_usage_limit.py` and `tests/test_validation_ci_hold.py`: reuse
`Validator`, `fleet`, `finish`, `passed` from `tests/test_validation_loop.py`, drive the
REAL `Daemon._validate_work_order` and the real `validation.decide`, and keep a module
docstring naming the four live work orders. A fixture constant for the live string:

```python
AUTH = "Failed to authenticate: OAuth session expired and could not be refreshed"
```

1. **(a) four-seat auth failure holds rather than escalates.** `Validator` raising
   `AuthFailureError`; drain five times. Assert the validator was called ONCE, rounds are
   `[(1, "failed")]`, `counted_validation_rounds == 0`, status stays `validating`,
   `true_blockers == []`, and no envelope was sent. Then a second `Validator` returning
   `passed()` after the backoff — patch the clock forward rather than sleeping — judges the
   SAME round and the submitter spends round ONE.
2. **(b) the backoff is 1m/5m/15m/60m and never escalates.** Parametrised over attempts 1-6:
   `reopens_at` minus the event's `ts` is 60, 300, 900, 3600, 3600, 3600, and the round is
   still `failed` with `hold_cause == VALIDATION_AUTH_CAUSE` on the sixth. Not one
   `escalated`, and `VALIDATION_AUTH_CAUSE not in COUNTED_VALIDATION_OUTCOMES`-adjacent:
   assert `counted_validation_rounds == 0` throughout.
3. **(c) Neo's condition — an auth hold consumes no `_validation_outage` attempt.** Three
   auth failures, then three `ClaudeCliError("connection reset")`. Assert the work order is
   still `validating` after each of the first five drains and only the SIXTH escalates. If
   an auth hold counted, the third transport outage would arrive with the budget gone and
   this escalates one drain early. Modelled on
   `test_a_hold_spends_none_of_the_transport_budget`.
4. **(d) an all-seats transient round goes through the outage budget as today.** `decide`
   with every seat carrying transient text raises a plain `ClaudeCliError` —
   `type(e) is claude_cli.ClaudeCliError`, not a subclass — and three drains reach
   `needs_review`.
5. **(e) a genuine usage limit behaves exactly as today.** `refused(3600)` still yields
   `hold_cause == VALIDATION_HELD_CAUSE` and `validation_standing(row) == ("held for the
   usage window", "active", "◑")`. The ordering pin: a seat carrying BOTH a `refused` and
   an `auth` takes the usage branch.
6. **(f) a seat that replied garbage still escalates.** One seat with `replied=True,
   status="failed"` beside three auth-failed ones: `decide` returns `escalated` and raises
   nothing. The `replied` gate is what this pins.
7. **(g) the feature-order twin holds the same way.** In
   `tests/test_feature_validation.py`, beside the existing usage-window pair at `:1370` and
   `:1405`: round `failed` with `VALIDATION_AUTH_CAUSE`, events on the MANAGER's timeline
   via `ops.feature_events_of_kind`, `counted_validation_rounds(fo_id=…) == 0`, feature
   still `validating`, and the same event after a restart still counts the streak.
8. **(h) the three surfaces render it as an active hold.** `validation_standing(row) ==
   ("held for authentication", "active", "◑")`; `ops.round_line` contains "held for
   authentication" and not "· failed ·"; `status_label` names Claude Code authentication
   and contains NO moment (assert "resumes by itself at" is absent — that is the CI
   precedent, and printing a recheck interval as a promise is the specific mistake);
   `timeline._describe` returns the auth label with no clock in it; and the dashboard
   fragment rendered through `TestClient` shows `tone-active`.
9. **The classification unit tests**, at the seam rather than through the fleet:
   `claude_cli._cli_failure(["-p", "judge"], 1, envelope_with_AUTH_in_result, "")` is an
   `AuthFailureError` whose `.auth.message` still contains the live string; the same for
   the transcript-shaped `stderr` carrier; a usage refusal in the same envelope still wins;
   and `seats._run_seat` under a monkeypatched `run_headless_result` carries `op.auth` with
   `(status, replied) == ("abstained", False)`. The prefix trap gets its own named test:
   `auth_failure(str(_cli_failure(…)))` is None — which is WHY the classification lives in
   `_cli_failure` — so a future refactor moving it back to `op.raw` fails here with the
   reason attached.
10. **The `signin_changed_at` clock is not used.** With `testing.py:2225` `signin` NOT
    called — the gate already points that path at a file that does not exist — the hold
    still lifts on the backoff alone. Pairs with a second run where `signin()` IS called
    and the timing is identical. This is Neo's rejected design, pinned so nobody reinstates
    it quietly.

`uv run pytest tests/ evals/` before the PR, per the project's standing instruction.
