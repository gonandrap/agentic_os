# A killed validation panel opens an investigation

GitHub issue #1039. Work order wo-fdd39919. Project `jarvis_os`.

## The problem

On wo-ab6c309d round 1 all four seats died at the same instant:

| seat | rc | latency | output |
|---|---|---|---|
| tester | 143 | ~18.5s | empty |
| security | 143 | ~18.5s | empty |
| architect | 143 | ~18.5s | empty |
| maintainer | 143 | ~18.5s | empty |

rc=143 is SIGTERM. Four concurrent seats ending within the same second, at the same
elapsed time, with no bytes on either stream, is one external event — a daemon
restart or deploy, a process-group kill, the OOM killer, an outer timeout — not four
model failures.

The classification path reads that event as nothing at all:

1. `claude_cli._cli_failure` (src/jarvis/claude_cli.py:267-290) computes
   `detail = _result_field(stdout) or stderr.strip() or stdout.strip()`, which is `""`
   here. `usage_limit("")` is None (claude_cli.py:1226), `auth_failure("")` is None
   (claude_cli.py:1509), so it returns a plain `ClaudeCliError` whose whole content is
   `failed (rc=143): `.
2. `seats._run_seat` (src/jarvis/seats.py:225-230) catches it, records
   `status="abstained"`, `replied=False`, `refused=None`, `auth=None`.
3. `validation.decide`'s all-seats-silent block (src/jarvis/validation.py:1029-1063)
   finds no refusal, no auth failure, and `claude_cli.transient_failure("")` returns
   None at its first line (claude_cli.py:1421-1422) — an empty detail has no status
   code and no transient text — so the `all(...)` transport branch is False and control
   falls through to the last statement:
   `return _out("escalated", "nobody could be reached to review this submission, so the
   work has not been judged.", ...)`.
4. `Daemon._validate_work_order` therefore sees no `failure`, takes the `escalated`
   outcome, and the order goes to `needs_review` with its assumptions held. It sat
   there 9h+.

Nothing else caught it either. `INV-VALIDATION-STRANDED` keys on a round still
`pending` past `2 × os.validation.timeout`
(docs/superpowers/specs/2026-08-08-validation-panel-design.md:708); this round was
CLOSED, `escalated`, so the invariant had nothing to find.

**Root cause: a signal death is an unclassified failure, and the unclassified case is
wired to a judgement.** The escalation sentence is a correct statement of fact
("nobody could be reached") pointed at the wrong reader: it spends the user's attention
on an event the user cannot diagnose from the record, because the record holds no rc,
no co-incidence, and no pointer to what else happened at 18.5s. kn-6ede3367 and the
pinned never-fabricate-a-default-answer-from-a-failure ruling: a model that was never
reached has made no judgement, and the OS must say which failure it was.

A blind retry is explicitly NOT the fix. A retry cannot name what SIGTERMed four
processes at once and cannot stop it recurring; it can only hide the next occurrence.

## The fix

Five parts. One new classified error, carried to the panel the way the two existing
"never asked" facts are; one new daemon branch that re-judges instead of parking; an
investigation order carrying the facts; a catalog-keyed bound; a registry detector so a
recurrence is caught mechanically.

### 1. `claude_cli`: the signal case becomes its own error

New, beside `UsageLimit`/`AuthFailure` (claude_cli.py:1126-1164):

```python
@dataclass
class Killed:
    """A seat whose process was killed by a signal and said nothing at all."""
    message: str
    rc: int

class KilledError(ClaudeCliError):
    def __init__(self, killed: Killed) -> None:
        super().__init__(killed.message)
        self.killed = killed
```

`ClaudeCliError` subclass for the documented reason the other two are: every existing
`except claude_cli.ClaudeCliError` keeps working unchanged.

In `_cli_failure` (claude_cli.py:267-290), a third classification clause **after** the
usage-limit branch (line 281-283) and the auth branch (line 286-288), and before the
generic return:

```python
if not detail and _is_signal_rc(rc):
    return KilledError(Killed(message=f"... was killed by a signal (rc={rc}) and "
                              f"wrote nothing", rc=rc))
```

`_is_signal_rc(rc)`: `rc < 0` (Python's own spelling when `subprocess` reaps a signal)
or `rc in (137, 143)` (128+9, 128+15 — the shell's spelling, which is what the CLI
wrapper produced in the incident). Two conditions, both load-bearing:

* **AFTER usage-limit and auth.** kn-d77b941d: the pre-panel branches have an order and
  it is not cosmetic. A refusal or a sign-in failure that happens to come back on a
  signal rc is still a refusal or a sign-in failure, and both have remedies — wait out
  the window, ask a human — that a kill does not.
* **ONLY when `detail` is empty.** A signal rc that DID carry output is an ordinary
  failure with a message somebody can read; classifying it as killed would throw that
  message away and open an investigation about a seat that explained itself.

### 2. `seats.Opinion`: carried, never raised

New field on `Opinion` (src/jarvis/seats.py:136-180), directly after `auth`
(seats.py:169):

```python
#: THE SEAT'S PROCESS WAS KILLED and it was never asked. `refused`/`auth`'s twin,
#: including the reason it is carried rather than raised.
killed: claude_cli.Killed | None = None
```

Set in `_run_seat`'s `except claude_cli.ClaudeCliError` arm (seats.py:225-230) as
`killed=getattr(e, "killed", None)`, exactly like the two lines above it. Carried
rather than raised for the reason those two state: one dead seat must not take down a
panel the other three answered — whether the round can be judged without it is
`validation.decide`'s call. `Opinion.summary()` (seats.py:189-198) is unchanged; the
field is read by `decide`, not reported out of the seat.

### 3. `validation.decide`: a killed panel raises

Inside the existing all-seats-silent block (validation.py:1029-1063), as a new clause
between the auth branch (line 1050-1052) and the transport branch (line 1053-1060):

```python
killed = next((op.killed for op in opinions if op.killed is not None), None)
if killed is not None:
    raise claude_cli.KilledError(killed)
```

`any(...)`-style first-match, following the refusal and auth precedent rather than an
all-seats rule: one killed seat inside a panel where nobody replied is already the
event, and requiring all four would make the clause miss a kill that raced one seat's
unrelated error.

**Why between auth and transport, and not after transport.** An empty rc=143 reaches
`transient_failure` as unclassifiable (None), so the transport `all(...)` is False and a
clause placed after it WOULD still be reached. The objection is not reachability, it is
what the record then says: the transport branch raises
`"every validation seat failed on the transport: …"`, which `Daemon._validation_outage`
turns into `cause: "transport"` — the OS stating an outage it did not observe, and
spending the outage budget on it. Before the transport branch, the ordering reads the
way the other two do: each named cause is claimed by its own clause, and "transport" is
left as the genuinely-diagnosed case.

Nothing else in `decide` changes. The fall-through escalation at
validation.py:1061-1063 stays as the answer for the case it was written for: every seat
silent, no classified cause, nobody can say why.

### 4. `daemon`: close the round `failed`, re-judge, file the investigation

New branch in the `isinstance(failure, …)` ladder of `_validate_work_order`
(src/jarvis/daemon.py:2989-3013), **before** the generic `_validation_outage` call at
daemon.py:3011-3012 — a `ClaudeCliError` subclass, so a clause after it is never
reached:

```python
if isinstance(failure, claude_cli.KilledError):
    self._validation_killed(store, project, wo, round_id, n, failure.killed, verdict)
    return
```

New `Daemon._validation_killed`, modelled on `_validation_outage` (daemon.py:3473-3497)
and placed beside it:

1. **Count the kills on their OWN counter**, derived from the events the way
   `_validation_outage` derives its own (daemon.py:3484-3487) — `events_of_kind(wo_id,
   "validation_failed")` filtered on `round == n` and `cause == VALIDATION_KILLED_CAUSE`.
   Derived from events, not held in memory, so a daemon restart does not hand the round
   a fresh budget. **Never folded into the transport count** (user ruling, Neo question
   1705, condition i): the transport filter at daemon.py:3487 keys on
   `cause == "transport"` and stays untouched, so neither counter can see the other's
   events.
2. `store.close_validation_round(round_id, "failed", <reason naming the rc>)`.
   `counted_validation_rounds` ignores `failed` rounds, so the submitter pays nothing
   and the next `validation_tick` picks the same round up and re-judges it — point 3 of
   the issue, with no new re-judge mechanism.
3. `store.add_event(wo_id, "validation_failed", {"round": n,
   "cause": VALIDATION_KILLED_CAUSE, "attempt": kills, "rc": killed.rc,
   "error": killed.message[:500], "investigation": inv_id})` where
   `VALIDATION_KILLED_CAUSE = "killed"`, a new module constant beside
   `VALIDATION_OUTAGE_LIMIT` (daemon.py:610-614). Its OWN cause, never `"transport"`:
   every surface that reads these payloads (`holds`, `jarvis wo show`, the rules facts
   of §5) distinguishes the two only by this string.
4. **File the investigation** (§4a).
5. **At the bound, escalate** (§4b).

`project: ProjectSpec` joins the signature — `_validation_outage` does not take one,
but the investigation needs the project name, and `_validation_refused`
(daemon.py:3441) already takes it for the same kind of reason.

#### 4a. The investigation

`ops.create_investigation_order(project.name, wo_id, why=<facts>)` (ops.py:8935-8997).

It already refuses a second live investigation per subject (`live_investigation`,
ops.py:9016-9035), so the filing is **naturally idempotent across retries**: the second
and third kill on the same order find the first investigation still open and raise
`OpsError`. `_validation_killed` therefore calls `ops.live_investigation` first and
files only when it answers `""`, and wraps the create in
`except ops.OpsError: log.warning(...)` regardless — a lost race between two ticks
must not kill the round. **A failed filing never propagates**: the round close and the
`validation_failed` event are the work order's own record and must land even when no
investigation exists.

The `why` text is what a fresh session with no memory of this conversation needs, and
it is **reference, never payload**:

* every seat by name with its rc, latency_ms and status, read off the opinions the round
  already recorded (`record_validation_opinion`, daemon.py:3015-3022) — the seat replies
  themselves are not copied in;
* the round id, the round number, the work order id, and the wall-clock timestamps of
  the round opening and of each seat's end;
* the pointers to check for what else happened at that instant: the daemon's own
  restart/deploy record, `jarvis alarms`, the fleet pause state, the order's budget
  holds and the `os.validation.timeout` in force;
* the one thing it must not conclude: that a retry is the answer.

#### 4b. The bound is a catalog key

User ruling (Neo question 1705, condition ii): a bound the user may want to move is
configuration, resolvable fleet-wide and per project, default 3.
`VALIDATION_OUTAGE_LIMIT` (daemon.py:614) is explicitly NOT acceptable as the number.

`validation.killed_retries`, copying `validation.red_ci_returns` end to end — every
site, all of them existing:

1. `catalog.DEFAULT_VALIDATION_KILLED_RETRIES = 3`, beside
   `DEFAULT_VALIDATION_RED_CI_RETURNS` (src/jarvis/catalog.py:537).
2. `killed_retries: int = DEFAULT_VALIDATION_KILLED_RETRIES` on
   `catalog.ValidationConfig`, beside `red_ci_returns` (catalog.py:644).
3. `catalog._parse_validation`: `killed_retries = int(raw.get("killed_retries",
   base.killed_retries))` beside catalog.py:2183, refused with
   `_err(f"{where}.killed_retries must be >= 0")` beside catalog.py:2184-2188, and
   passed through in the constructor call beside catalog.py:2228. `0` is legal and
   means "escalate on the first kill".
4. `Daemon._round_config` (daemon.py:3499-3534) needs NO change: it returns a
   `ValidationConfig`, and `_validate_work_order` already holds one as `cfg`, so
   `_validation_killed` reads `int(cfg.killed_retries)` the way the red-CI branch reads
   `int(cfg.red_ci_returns)` at daemon.py:2896.
5. `config_version.validation_config_from_resolved` (src/jarvis/config_version.py:184-209)
   needs NO change: it iterates `fields(catalog.ValidationConfig)`, so a new field is
   resolved from `os.validation.killed_retries` /
   `projects.<name>.validation.killed_retries` the moment it exists.
6. `jarvis config set` needs NO change: `ops._key_path` (ops.py:12744-12780) and
   `_document_slot` (ops.py:12783-12814) are generic over the key space, and
   `ops.APPLY_RULES` already classifies `("os.validation.*", "hot")` (ops.py:12497), so
   a change applies to rounds judged after it without a restart.
7. The doc table that lists the validation keys:
   docs/superpowers/specs/2026-08-08-validation-panel-design.md:675-684 — add a
   `killed_retries | 3 | kills in a row one round survives before the user is asked`
   row, and fix the stale sentence at line 685 ("the same eight keys"). NOTE: the
   red-CI work never added `red_ci_returns` to this table; add that row too, or the
   table keeps drifting one key per feature.

At the bound, `self._escalate(store, wo, round_id, n, reason)` (daemon.py:3267-3298) —
and the reason text **attaches the investigation id**, so the user reading the attention
item has somewhere to go that is not a retry:

> the review could not be run: its seats were killed by a signal (rc=143)
> {kills} times in a row. Nobody has judged the work. {inv_id} is investigating what
> killed them.

When no investigation could be filed, the sentence says that instead of naming an id
that does not exist.

### 5. `rules._SEEDS`: a detector for the gap class

One new seed in the existing grammar, appended to `_SEEDS` (src/jarvis/rules.py:1149),
shipped `dry_run` like every other (`seed_rows`, rules.py:1142-1148):

* `gap_class`: `killed-validation-panel`
* `issue_url`: `f"{_TRACKER}/1039"`
* `summary`: a round whose seats were killed by a signal, more than once on the same
  order — the kill is recurring, not a blip
* `remedies`: one `file_work_order` entry. That primitive's `covers` is empty on
  purpose, so it is reachable only by being named — correct here, because a recurring
  kill needs code written, not a state flipped. Note kn-a816ddf6: an armed detector
  raises its alarm even with no armed remedy, so the detector is useful before any
  remedy is ever armed. This one ships `dry_run` regardless.

**The condition needs one new fact field, and the grammar does not change.** The seed's
natural condition is "this order has been killed N times", and the cause lives in the
`validation_failed` payload. The existing fields cannot say it:

* `event_counts` (rules.py:264, source `event_counts`) is `{event kind: count}` and
  `count_gte` keys on the KIND (rules.py:590-592). Every cause — transport, auth,
  killed — writes the kind `validation_failed`, so a condition on it fires on transport
  outages too. That is precisely the conflation §4 step 3 exists to prevent.
* `hold_cause` (rules.py:241) is wrong semantically: a killed round is re-judged on the
  next tick and holds nothing.
* `round_outcome` (rules.py:252) is `passed`/`rejected`/`escalated`; a `failed` round
  carries no outcome to key on.

So add, in this order (the order `FACT_FIELDS`' own docstring requires at
rules.py:213-216 — the table diff first, then the reader):

1. `("validation_failure_causes", MAP, "validation_failures", "`validation_failed`
   event causes to their counts — `transport`, `killed`, the auth cause")` in
   `FACT_FIELDS`, beside the `validation_round` block (rules.py:250-254).
2. Its reader in `ops.rule_facts` (src/jarvis/ops.py:11473-11626), a
   `if need("validation_failures"):` block modelled on the `invariant_events` block
   (ops.py:11609-11618): walk `store.events_of_kind(wo_id, "validation_failed")`,
   count `db.from_json(e["payload"], {}).get("cause")`. Its own source slug, so a
   condition about `status` never pays for the walk — `sources` is the whole cost model
   (ops.py:11487-11494).

The condition is then expressible with the shipped operators, which is the registry's
stated acceptance criterion (spec §3.2/§3.5 of
docs/superpowers/specs/2026-09-27-self-evolution.md):

```python
{"all": [
    {"field": "validation_failure_causes", "op": "count_gte",
     "value": {"kind": "killed", "value": 2}},
]}
```

A new MAP field plus a reader is the extension point `FACT_FIELDS` documents and gates;
no operator, type or node form is added.

## Rejected alternatives

1. **Retry the round and say nothing** (what a blind fix looks like). Refused in the
   issue and refused here: the round IS retried by §4, but retrying without recording
   the cause and without opening the investigation leaves "four processes died together"
   undiagnosed, and the next occurrence looks like the first.
2. **Fold kills into the transport outage count and reuse `_validation_outage`.** The
   cheapest diff, and the user ruled against it (Neo 1705, condition i). It also writes
   a false statement — `cause: "transport"` — into the one record anybody reads later,
   and it lets three unrelated 503s plus one kill escalate an order on a budget neither
   event exhausted.
3. **A module constant `VALIDATION_KILLED_LIMIT = 3` beside `VALIDATION_OUTAGE_LIMIT`.**
   Explicitly not acceptable (same ruling). A fleet that is restarting its daemon during
   deploys wants a different number from one that is not, and the catalog is where a
   number the user may move lives.
4. **Classify on rc alone, ignoring whether output was present.** Simpler predicate,
   worse record: a seat that was killed mid-reply after printing a usage-limit refusal
   would be filed as an unexplained kill, discarding the one sentence that explains it
   (claude_cli.py:268-279 — issue #235 is the same lesson about reading the detail
   first).
5. **Detect it with an invariant instead of a registry detector.** `INV-VALIDATION-
   STRANDED` already demonstrates the limit: it keys on a round still open, and a round
   closed `failed` and re-judged is invisible to that shape. The registry is where a
   condition over recorded facts belongs, and the issue asks for the registry.
6. **Put the seat facts in the investigation as a payload (replies, transcripts).** The
   investigation reads the record itself; copying deliberation into a `why` duplicates
   what `jarvis validation show` already renders and ages the moment the record moves.

## Tests

All unit level, in the existing files.

1. `tests/test_claude_cli.py` — `_cli_failure` classification: rc=143 with empty
   stdout/stderr gives `KilledError` with `killed.rc == 143`; rc=-15 likewise; rc=137
   likewise; rc=143 WITH a readable detail gives a plain `ClaudeCliError` carrying that
   detail; a usage-limit message at rc=143 still gives `UsageLimitError`; an auth
   message at rc=143 still gives `AuthFailureError` (the two ordering pins).
2. `tests/test_seats.py` — `_run_seat` on a killed call returns
   `status="abstained"`, `replied=False`, `killed.rc == 143`, and `refused is None` /
   `auth is None`; `summary()` carries no new key.
3. `tests/test_validation_panel.py` — `decide` raises `KilledError` when every seat is
   silent and one was killed; a refusal and an auth failure each still win over a kill
   in the same block; a kill wins over the transport branch (no "failed on the
   transport" message); one seat killed beside one that REPLIED takes the ordinary
   verdict path and raises nothing.
4. `tests/test_validation_killed_panel.py` (NEW, modelled on
   `tests/test_validation_auth_hold.py`) — the daemon branch: the round closes `failed`;
   the `validation_failed` event carries `cause == "killed"` and the rc;
   `counted_validation_rounds` is unchanged, and the next tick re-judges the same round;
   the order does NOT reach `needs_review` and raises no attention flag; an
   investigation order exists on the subject after the first kill and exactly one after
   the third (the `OpsError` is swallowed and the round still closes); at
   `killed_retries` the order escalates and the attention/escalation text contains the
   investigation id; three transport outages do not escalate on the kill counter and
   three kills do not escalate on the transport counter (the two-counter pin).
5. `tests/test_catalog.py` — `killed_retries` defaults to
   `DEFAULT_VALIDATION_KILLED_RETRIES`, inherits from `os.validation`, is overridable
   per project, accepts `0`, and refuses `-1` with
   `os.validation.killed_retries must be >= 0` (mirroring the `red_ci_returns` block at
   tests/test_catalog.py:1160-1194).
6. `tests/test_config_version.py` — `validation_config_from_resolved({"os.validation.
   killed_retries": 7}).killed_retries == 7`, and the default when the key is absent.
7. `tests/test_rules.py` — `seed_rows()` now lists SIX gap classes, the sixth being
   `killed-validation-panel` (updates the existing
   `test_the_five_gap_classes_are_the_ones_the_spec_names`, tests/test_rules.py:353);
   its condition parses against `FACT_FIELDS`; it ships `dry_run`; its `issue_url` ends
   `/1039`.
8. `tests/test_rules_tick.py` — `ops.rule_facts` exposes
   `validation_failure_causes` as `{"killed": 2, "transport": 1}` for an order with
   those events, is ABSENT when the source is not requested, and the seed's condition
   evaluates True at two kills and False at one.

## Deliberately NOT done

* **No diagnosis of what SIGTERMed the seats.** That is the investigation's job, by
  design: this spec makes the OS file it with enough facts, and takes no position on
  daemon restarts, cgroup limits or the OOM killer.
* **No change to the fall-through escalation** (validation.py:1061-1063). Every seat
  silent with no classified cause still asks the user, and still with that sentence.
* **No `KilledError` handling outside the validation panel.** `worker_session._diagnose`
  and the Neo panel also see `ClaudeCliError`s; both keep today's behaviour, because the
  new class is a subclass. Whether a killed worker turn deserves the same treatment is a
  separate question with a separate incident behind it.
* **No `holds` entry and no new hold cause.** A killed round is re-judged on the next
  tick; nothing is waiting, so a hold would report a wait that does not exist.
* **No automatic remedy.** The seed ships `dry_run` and its only remedy is
  `file_work_order`, which writes code and clears nothing mechanically. Arming is a
  person's decision once the hit history justifies it (self-evolution spec §3.3).
* **No retry/backoff schedule of its own.** A kill is re-judged on the ordinary tick
  cadence, like a transport outage, and is bounded by `validation.killed_retries`.
