# The hour, on worker turns only

Amends `docs/superpowers/specs/2026-08-22-the-five-minute-write-everywhere.md`, which
named this reversal itself: *"Re-open this if a future OS-side agent is paced at one call
every 10–30 minutes against a stable prefix. Use the `env_extra` override to A/B it rather
than editing the constant."* (§"The honest caveat, and what would reverse this", line 159).
This is that reversal, scoped to `claude_cli.spawn_turn` and to nothing else.

Filed from io-edacb3ea. Measurement and traps: kn-a31cad62, kn-b123f589, kn-5dd784f5.

## The problem

**The fleet buys one prompt-cache TTL for two populations whose call spacing differs by two
orders of magnitude, and only one of them was measured.**

`claude_cli.PROMPT_CACHE_5M_ENV` (src/jarvis/claude_cli.py:42) forces
`FORCE_PROMPT_CACHING_5M=1` on every process the OS starts — both launchers, `_run`
(src/jarvis/claude_cli.py:237) and `spawn_turn` (src/jarvis/claude_cli.py:974) — and
`dispatch._write_worker_settings` writes the same constant into every worker settings file
(src/jarvis/dispatch.py:248).

The decision that put it there was priced over the fleet as one population:
`docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md` finding 2 — TTL-expiry
tokens 10.6% of all cache writes over 30 days, 18.5–20.4% over 7, against the 39.5%
break-even `usage.TTL_BREAK_EVEN` derives from the three rates
(src/jarvis/usage.py:106-121). Switching the whole fleet loses ~$273.

The two populations are not alike:

* **OS-side calls** (`_run`: Neo, the panel seats, the dashboard digest) arrive in
  sub-minute bursts out of `Daemon.drain_queue`. 1.25x is right for them and this spec
  does not touch them.
* **Worker turns** (`spawn_turn`) have their boundaries set by usage-limit holds, Neo
  waits, panel rounds and daemon polls. Measured: a **258.7-minute** hold between turns on
  wo-fb7c0fc2 (kn-a31cad62). Every one of those boundaries is a `ttl-expiry` re-write
  (`inspection.TTL_5M`, src/jarvis/inspection.py:104) that re-sends the whole conversation
  at 1.25x — the ~12% re-write tax `jarvis cost` breaks out.

Nothing in the tree can tell the two apart today. The population that pays for cold
boundaries is the one with no way to buy a longer entry, the constant is fleet-wide, and
the §"reversal criteria" instruction to A/B through `env_extra` cannot be followed:
`spawn_turn` takes no `env_extra` and merges `env = {**os.environ, **cache_env()}` with no
seam a caller can reach.

**Root cause, named:** the TTL was modelled as a property of the *transport* when it is a
property of the *call spacing*, and the 2026-08-22 spec says so in the heading of its own
"What shipped" section. One constant for both launchers is the defect; this spec splits the
decision by launcher and does not touch `_run`.

This spec **buys no hour**. It ships the apparatus and the default `5m`, so nothing changes
until a project opts in. The arithmetic above is why: the fleet number says do not switch,
and the worker-only number has never been measured because there was no way to measure it.

## The fix

Five pieces. `claude_cli.PROMPT_CACHE_5M_ENV`, `cache_env()` and `_run` are **untouched**.

### 1. The setting — `worker.cache_ttl`

`src/jarvis/catalog.py`, beside `VALID_BASH_FIRST` (line 94) in every respect:

```python
VALID_WORKER_CACHE_TTL = ("5m", "1h", "cli")
DEFAULT_WORKER_CACHE_TTL = "5m"
```

* `5m` — `FORCE_PROMPT_CACHING_5M=1`. Today's behaviour, the control arm.
* `1h` — `ENABLE_PROMPT_CACHING_1H=1` and **no** 5m key: finding 2 of the 2026-08-22 spec
  reads the short-circuit out of the binary — the 5m flag wins over the 1h one, so an arm
  that set both would be the control arm wearing a label.
* `cli` — **write nothing**, which on a Claude subscription gets the CLI's documented
  automatic 1h.

Resolution is `bash_first`'s, line for line: `OsConfig.default_cache_ttl` from
`os.defaults.cache_ttl` (catalog.py:1849 is the model), `WorkerDefaults.cache_ttl`
(catalog.py:488), the fleet check at catalog.py:3094 and the per-project one at
catalog.py:3147 with the same `project {name}: worker.cache_ttl {v!r} not in [...]` shape —
that message *is* what `jarvis config set <project> worker.cache_ttl` prints, because
`ops.set_config` re-parses the document to validate.

`ops.APPLY_RULES` (src/jarvis/ops.py:13093) gains `("*.cache_ttl", "next-dispatch")`, for
`*.bash_first`'s reason: read once per spawn into the settings file, and a running worker's
session already holds the environment it was launched with.

**Deterministic, not randomised.** The arm *is* the resolved setting. No per-order hash, no
RNG: an order's arm must be reproducible from the catalog version it ran under, and a
randomised arm would be unreproducible for exactly the orders worth re-reading.

### 2. The env helper — `dispatch.cache_ttl_env`

Mirrors `dispatch.bash_first_env` (src/jarvis/dispatch.py:60) exactly, including the
all-strings rule (Claude Code's settings `env` is a `Record<string,string>`):

```python
def cache_ttl_env(cache_ttl: str) -> dict[str, str]:
    if cache_ttl == "cli":
        return {}
    if cache_ttl == "1h":
        return {claude_cli.PROMPT_CACHE_1H_ENV_KEY: "1"}
    return dict(claude_cli.PROMPT_CACHE_5M_ENV)
```

The key strings stay in `claude_cli` and are never spelled twice — the argument
`_write_worker_settings` already makes at dispatch.py:243. `claude_cli` gains, beside
`PROMPT_CACHE_5M_ENV`:

```python
PROMPT_CACHE_1H_ENV_KEY = "ENABLE_PROMPT_CACHING_1H"
#: Every variable this decision OWNS. Absence is a value (the `cli` arm), so a caller
#: that sets the TTL must also be able to say "none of these".
CACHE_TTL_KEYS = (*PROMPT_CACHE_5M_ENV, PROMPT_CACHE_1H_ENV_KEY)
```

**Before trusting the `1h` arm the implementer re-reads finding 2's binary extract at the
shipped CLI version** and confirms key and accepted value (`Vn` takes exactly
`1`/`true`/`yes`/`on`, lowercased, trimmed; kn-522c6103). The failure mode of getting it
wrong is silence.

### 3. Both paths the flag reaches a worker

**The settings file.** In `dispatch._write_worker_settings`, `**claude_cli.PROMPT_CACHE_5M_ENV`
(dispatch.py:248) becomes `**cache_ttl_env(project.worker.cache_ttl)`, keeping the comment's
point: the settings file and the spawn environment must not be able to disagree.

**The spawn environment.** `claude_cli.spawn_turn` gains an explicit keyword parameter —
**not** through `**kwargs`, which is forwarded whole to `turn_args` and would raise:

```python
def spawn_turn(..., cache: dict[str, str] | None = None, **kwargs: Any) -> SpawnedTurn:
    ...
    env = {k: v for k, v in os.environ.items() if k not in CACHE_TTL_KEYS}
    env.update(cache_env() if cache is None else cache)
```

`None` means "no caller opinion" and keeps today's behaviour exactly. `{}` is the `cli`
arm and is **not** the same as `None` — which is why the parameter is a dict-or-None and
not a string.

**This does not re-break the ambient-loses rule** the 2026-08-22 spec states in
§"where the flag lives". That rule has three levels: ambient loses, the default sits above
it, an explicit caller above both. Stripping `CACHE_TTL_KEYS` out of `os.environ` first
makes the first level *stronger*, not weaker: ambient can no longer contribute even where
the arm writes nothing. `_run` keeps its opposite merge (`env.update(cache_env(env_extra))`)
and is not touched — the two call sites still merge in opposite directions for the reason
the spec gives.

**The `cli` arm, the subtle case, stated explicitly:** the variable must be ABSENT from the
settings file (`cache_ttl_env` returns `{}`, so no key is written) and ABSENT from the spawn
environment **including when the daemon's own process has it set** — which it does whenever
jarvisd was itself started from a Jarvis worker session. The `os.environ` filter is the only
thing that makes that true; without it the `cli` arm silently runs as `5m` and the
experiment reports a null result it never ran.

**The caller.** `worker_session._launch` (src/jarvis/worker_session.py:752) resolves the arm
once per turn — `arm = project.worker.cache_ttl` — and passes
`cache=dispatch.cache_ttl_env(arm)` to `spawn_turn`. Both halves read the same resolved
`project.worker.cache_ttl`, so they cannot drift. `claude_cli` imports nothing from
`dispatch`: the mapping lives in `dispatch`, the key strings in `claude_cli`.

### 4. The per-order stamp — what makes the comparison sound

A new `wo_turns` column, added through `project_store.ADDED_COLUMNS` (`CREATE TABLE IF NOT
EXISTS` is a no-op on a live database):

```python
"wo_turns": {
    # WHICH CACHE-TTL ARM this turn was launched on (catalog.VALID_WORKER_CACHE_TTL).
    # '' is "not recorded" — a turn that ran before this column existed — and is NEVER
    # read as an arm: every such turn did run at 5m, but a reader that assumed it would
    # be asserting a fact about a row that does not carry it.
    "cache_ttl": "TEXT NOT NULL DEFAULT ''",
}
```

**Per TURN and not per order**, because the catalog can change between turns and the
question the data answers is "what did THIS boundary cost at THIS arm".

* **Written by** `worker_session._launch`, through `ProjectStore.create_turn(...,
  cache_ttl=arm)` (project_store.py:4530) — the row that already exists before the process
  does, so a turn can never run unstamped. Also on the `turn_started` event payload
  (worker_session.py:773) beside `head`, for the timeline.
* **Read by** `ops._turn_row` (src/jarvis/ops.py:13961), which gains
  `"cache_ttl": turn.get("cache_ttl") or ""` in the base dict (not the `u is not None`
  block: the arm is known even for a turn with no usage). That is the per-turn table
  `jarvis cost <wo-id>` and the work-order dashboard page render, so the EXISTING
  `cache_1h` / `cache_5m` / `cache_write` columns at ops.py:13994-13997 group by arm with
  no new cost column and no new query.

**No new cost column, and no new alarm**: the experiment's report is the existing columns
grouped by this stamp.

### 5. `Daemon.check_cache_ttl` becomes arm-aware

`Daemon.check_cache_ttl` (src/jarvis/daemon.py:6327) raises `CACHE_1H_DISPATCHED` with the
words *"a BREACH of that guarantee and a defect in the OS"*
(`inspection.hour_alarms`, src/jarvis/inspection.py:2143). The moment the `1h` or `cli` arm
buys an hour on purpose, that sentence is false and the alarm is an OS accusing itself of a
defect it was told to have.

**The oracle is the stamp**, not a second policy. `inspection.one_hour_writes`
(src/jarvis/inspection.py:2039) today splits each session's 1h tokens into
`dispatched` / `foreign` by `promptSource: "sdk"`. It gains an optional
`arms: Mapping[str, Sequence[tuple[float, float, str]]] | None` — session id to
`(started_at, ended_at, arm)` intervals — and a third bucket on `HourWrites`:

| call | bucket |
|---|---|
| not `sdk` (a person's own `claude`) | `foreign` — **untouched, same alarm, same `REMEDY_LINE`** |
| `sdk`, inside a stamped turn whose arm is `1h` or `cli` | `deliberate` — **not an alarm** |
| `sdk`, inside a turn stamped `5m` | `dispatched` — **still a real breach** |
| `sdk`, no stamp or no matching interval | `dispatched` — unknown is not a licence |

`hour_alarms` keeps thresholding `dispatched` alone, at the unchanged
`alarm_cache_1h_dispatched_tokens`. Its reason text drops the unconditional "breach of
`claude_cli.cache_env`" claim in favour of one that says which turns it is about — a 1h
write from a turn whose arm was `5m` — and names the `deliberate` total beside it as
context, so a reader can see the hour that WAS bought on purpose without being alarmed
about it.

The daemon supplies `arms`: `check_cache_ttl` already runs only for `self._os_owner()`,
while the transcript scan is fleet-wide, so it builds the index across
`self.catalog.projects` with `self.store_for(project)` (the loop at daemon.py:1211 is the
shape) from a new `ProjectStore.turn_arms(since)` — `wo_turns` joined to `work_orders` for
`session_id`, returning `(session_id, started_at, ended_at, cache_ttl)` for stamped turns
since the window opened. An unreadable project is skipped and its turns fall to
`dispatched`, which is the safe direction.

## Trap 1 — a refused hour looks exactly like an arm that did not apply

The CLI **ignores a 1h request while the subscription is in overage** (kn-b123f589). So
`cache_1h == 0` across the `1h` arm is a SIGNAL that the request was refused, not evidence
the arm failed to apply. The reader tells them apart with two facts that are already on
record and one that this spec adds:

1. **Did the arm apply?** `wo_turns.cache_ttl == "1h"` on those turns — the stamp is
   written at launch from the resolved setting, before any API call, so it is true
   regardless of what the account was doing.
2. **Did the CLI honour it?** `cache_5m > 0` with `cache_1h == 0` on a turn stamped `1h` is
   the refusal: the request was made and the CLI wrote at the short TTL anyway.
3. **Was the account in overage?** `jarvis status` / the usage-meter samples
   (`Daemon.sample_usage_meter`, daemon.py:941) over the same window.

A configuration failure looks different and is distinguishable: the stamp would be `5m`, or
the settings file for the order would carry `FORCE_PROMPT_CACHING_5M`. **Any report of this
experiment states which of the three it is observing**; "the 1h arm saved nothing" with no
overage check attached is not a result.

## Trap 2 — a spawn-environment test run by a Jarvis worker is vacuous

kn-5dd784f5 §4 and the 2026-08-22 spec §"The test trap": `tests/test_prompt_cache_ttl.py`
passed all seven assertions **with `_run` and `spawn_turn` reverted**, because the worker
running pytest carries `FORCE_PROMPT_CACHING_5M=1` in its own session, pytest inherits it,
and the fake `claude` inherits it from pytest. The autouse `_no_inherited_flag` fixture
(tests/test_prompt_cache_ttl.py:34) is what makes the file mean anything.

**Every new test here clears the variable it is about.** The fixture must also
`monkeypatch.delenv` `ENABLE_PROMPT_CACHING_1H` (it already does) and any key added to
`CACHE_TTL_KEYS`. The **`cli`-arm test is MEANINGLESS without the delenv** — its whole
assertion is that the key is absent, which is trivially false in a worker's environment and
trivially true in a clean one unless the ambient variable is set *deliberately* inside the
test and then asserted gone. Write it that way: `monkeypatch.setenv(FLAG, "1")`, spawn,
assert the key is **absent** from the child's environment.

## The existing guards, and what each must become

| file / line | today | required change |
|---|---|---|
| `tests/test_prompt_cache_ttl.py:27` `LAUNCHERS` AST seam guard | `{"_run", "spawn_turn"}`, no new function may start a `claude` | **unchanged, and must stay green.** This spec adds no launcher. Its failure message gains nothing. |
| `tests/test_prompt_cache_ttl.py:74` `test_the_flag_value_is_one_the_cli_actually_parses` | pins `PROMPT_CACHE_5M_ENV == {FLAG: "1"}` | **unchanged**, plus a sibling pinning `PROMPT_CACHE_1H_ENV_KEY` and the `"1"` value, for the same reason: the failure mode of a value the CLI does not parse is silence. |
| `tests/test_prompt_cache_ttl.py:82,93` precedence | ambient loses; `cache_env(explicit)` wins | **unchanged.** `cache_env` and `_run` are not touched. |
| `tests/test_prompt_cache_ttl.py:115` `spawn_turn` carries the flag | no `cache=` passed | **unchanged** — it is now the `cache=None` default path, which is the regression that matters most. |
| `evals/test_prefix_drift.py:251` | asserts EVERY fleet worker settings file carries `PROMPT_CACHE_5M_ENV` verbatim | **must become arm-aware.** As written it fails the moment any project sets `1h` or `cli`. Rewrite: for each fleet record assert `r.settings["env"]` matches `dispatch.cache_ttl_env(<that project's resolved cache_ttl>)` exactly — present for `5m`/`1h`, and **no `CACHE_TTL_KEYS` key at all** for `cli`. The scenario's claim ("the cache TTL the writes are billed at" is pinned) survives; what changes is that it is pinned to the project's arm instead of to one constant. `includeGitInstructions is False` stays as is. |
| `tests/test_worker_session.py:407` `test_workers_buy_the_five_minute_cache_not_the_one_hour_one` | asserts the literal `settings["env"]["FORCE_PROMPT_CACHING_5M"] == "1"` and says so deliberately ("Asserting the literal… it should take a deliberate edit and a fresh measurement to reverse") | **keep the literal and keep the docstring**, now as the DEFAULT-arm test: the fleet default is still `5m` and this is what proves no project changed by accident. Add its two siblings (below) rather than parametrising it away. |
| `tests/test_turn_transport.py:231` `test_the_turn_in_a_unit_still_buys_the_five_minute_cache_write` | `--setenv` is the only way the flag reaches a worker under systemd; already does `monkeypatch.delenv` | **unchanged** for the default arm, plus one sibling on the `cli` arm: `systemd_units.run_prefix` builds `--setenv` from the same `env` dict, so an arm that writes nothing must produce **no** `--setenv` for `CACHE_TTL_KEYS` on that transport either. This is the one place the absence could survive the unit boundary unnoticed. |

## The new tests, written first

1. `tests/test_catalog.py` — `cache_ttl` defaults to `5m`, inherits `os.defaults.cache_ttl`,
   is overridden per project, every value in `VALID_WORKER_CACHE_TTL` parses, and an invalid
   one names `worker.cache_ttl` and the valid list (mirror lines 191-239 for `bash_first`).
2. `tests/test_prompt_cache_ttl.py` — `cache_ttl_env` for all three arms: `5m` is
   `PROMPT_CACHE_5M_ENV`, `1h` is the 1h key **and no 5m key**, `cli` is `{}`.
3. `tests/test_prompt_cache_ttl.py` — the spawn environment per arm, each with the ambient
   variable **set inside the test and then asserted gone or replaced**: `cache=None` keeps
   `FORCE_PROMPT_CACHING_5M=1`; the `1h` arm has the 1h key and no 5m key; the **`cli` arm
   has neither key, with `FORCE_PROMPT_CACHING_5M=1` ambient**. Trap 2 governs all three.
4. `tests/test_worker_session.py` — settings file per arm, read off disk like line 424, and
   for `cli` assert neither key is present in `settings["env"]`.
5. `tests/test_worker_session.py` — the stamp: a dispatched turn's `wo_turns.cache_ttl`
   equals the project's resolved arm, and `ops._turn_row` carries it through to the
   per-turn table.
6. `tests/test_inspection.py` / `tests/test_daemon_*.py` — `one_hour_writes` with an `arms`
   index: a 1h write inside a `1h`-stamped turn lands in `deliberate` and raises **no**
   alarm; the same write with the turn stamped `5m` still raises `CACHE_1H_DISPATCHED`; an
   unstamped turn still raises it; a **foreign** write raises `CACHE_1H_FOREIGN` with
   `REMEDY_LINE` unchanged.

A new-column test is not listed because `ADDED_COLUMNS` is exercised on every store open;
test 5 covers the write and the read.

## Rejected alternatives

**Flip `PROMPT_CACHE_5M_ENV` to the hour for everyone.** The obvious fix, and the finding
already priced it: ~$273 worse fleet-wide, 10.6% TTL-expiry share against a 39.5%
break-even. It also silently re-prices Neo and the panel, whose spacing is sub-minute and
for whom 1.25x is simply correct.

**Randomise the arm per work order.** A clean A/B on paper. Rejected: an order's arm must be
reproducible from its record, the fleet runs too few orders a week for randomisation to beat
a per-project assignment, and an arm nobody can re-derive makes every later question about
one expensive order unanswerable.

**A new `cache_ttl` cost column, or a new report.** Rejected: `jarvis cost` already has
`cache_1h`, `cache_5m` and the re-write tax per turn. The only thing missing was the
grouping key, which is the stamp. A column would be a second number to keep in agreement
with the first.

**Suppress `CACHE_1H_DISPATCHED` while any project runs a non-`5m` arm.** The cheap way to
stop the false alarm. Rejected: it also suppresses the real breach — a 1h write from a turn
whose arm was `5m` — across the whole fleet, for as long as the experiment runs, which is
exactly the window in which a plumbing mistake is most likely.

**Extend the stamp to OS-side calls (`agent_calls`).** Deliberately out of scope: `_run` is
untouched, so there is one arm there and nothing to group by. If `_run` is ever split the
same way, `agent_calls.usage_json` is where the stamp would go.

## Out of scope, stated

* No arm is turned on. The fleet default stays `5m`; a project opts in with
  `jarvis config set <project> worker.cache_ttl 1h`.
* `_run`, `cache_env`, `PROMPT_CACHE_5M_ENV`: unchanged, by design, not by omission.
* The `foreign` half of the 1h alarm, and `inspection.REMEDY_LINE`: unchanged. A person's
  own `claude` is not an arm.
* `invariants.check_cache_ttl_trigger` (src/jarvis/invariants.py:4783), which watches the
  39.5% break-even: left alone. Its denominator is every cache write in the fleet, and
  until an arm is actually enabled this spec changes none of them. Once an arm runs, its
  reading becomes a mixture — **note it in the first report; do not pre-emptively change
  it**, because a threshold rewritten before the data exists is a guess.
* No dashboard work beyond what `ops._turn_row` already feeds.
