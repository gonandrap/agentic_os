# No test reads the real clock

wo-059520b9 · GitHub issue 1066 · Neo question 1756 (option C)
Precedent: kn-898350cc (the relative-reset ruling), kn-1d6cbc82 (no path literal in `src/`).
Shape to follow: `tests/test_spec_location.py`.

## The problem

### The flake, measured

`tests/test_health_sweep.py:1033` staged a usage-limit refusal as a WALL-CLOCK string:

```python
fake_claude.health_rate_limited(reset="11:50pm (America/Los_Angeles)")
...
assert row["reopens_at"] > db.now()          # :1039
```

Two different clocks are read across those two lines.

* `claude_cli.usage_limit` resolves the clause through `_reset_moment`
  (`src/jarvis/claude_cli.py:1306-1353`), which does
  `datetime.fromtimestamp(now, _zone(...))` against the REAL clock and returns the next
  occurrence of that time of day.
* The file's `clock` fixture (`tests/test_health_sweep.py:72-76`) replaces `jarvis.db.now`
  with `_Clock`, a hand-advanced fake, and `_sweep` advances it by `STEP_MINUTES = 2` fake
  minutes before each tick (`:37`, `:118`).

So `reopens_at` is a real-clock deadline and `db.now()` is a fake now that has stepped past
it.

Measured: at `T = 23:49:17 America/Los_Angeles`, `usage_limit(..., now=T).reset_at == T + 43`
— the next 11:50pm is 43 seconds away, and the fake advance overshoots it. CI run
**38693984070** on PR **#1053** failed with `reopens_at=1792047000.0` vs
`now=1792047043.1`.

The failing band is strictly BEFORE 23:50, not after: `_reset_moment` rolls a time of day
that has already gone past forward by a day (`claude_cli.py:1351-1352`,
`if when <= ref: when += timedelta(days=1)`), so a run starting at 23:51 gets a deadline
~24h out and passes. A run in the few fake-minutes-wide window before 23:50 fails. That is
why it looked random.

`reset="in 5h"` is clock-free arithmetic — `ref + 18000` at any `T` — which is why the
sibling test at `tests/test_health_sweep.py:1048-1061` already used it and documented why.

### Root cause, named

The root cause is NOT the string in the test. It is that the OS has two clocks and only one
of them is injectable: `jarvis.db.now` is patchable, while `claude_cli` resolves reset
moments against `time.time()` through `datetime.fromtimestamp` with no `now` seam reaching
the test. A test can therefore fake half the time in the process and get no error saying so.

This branch does NOT unify the clocks. It removes the one test's dependence on the un-faked
clock and makes the SHAPE that caused it unwritable. Threading an injectable clock through
production code is class 2 below, and is deferred on purpose.

### The census: three classes, and only one is a defect

The user widened the ask on #1066 to "no test may read the real clock, timeouts and waits
included", and Neo settled it as option C. A census of `tests/` found ~250 real-clock reads
across ~45 files. They are not one problem:

| Class | Shape | Count | Disposition |
|---|---|---|---|
| 1 | Test fakes `jarvis.db.now` AND stages a deadline production code resolves against `time.time()` | exactly 1 site | FIXED in this branch |
| 2 | Test back-dates a row with `time.time() - N`, and the code under test reads `time.time()` itself | ~200 sites | DEFERRED to a follow-up feature order |
| 3 | `deadline = time.monotonic() + t` / `while …: time.sleep(0.01)` spin-waits on real daemon threads and subprocesses | ~40 sites | REVIEWED AND EXEMPT |

**Class 2 is not flaky today, and saying so is load-bearing.** Both sides share one clock,
so there is no band: `ops.waiting_on` and `check_project` take no `now` argument at all, and
the tests say so in comments written at the time — `tests/test_message_stuck.py:140-147`
("THE FIXTURE HAS TO BE OLD IN THE ROWS, not in a `now` argument. `parked_reason` takes one
and `ops.waiting_on`, which it consults, does not — it reads `time.time()` itself") and
`tests/test_message_stuck.py:409`. Converting these means adding a `now=` parameter to
production functions and threading it through every caller. That is a large blast radius in
a branch whose code change is one line, so it is out of scope here and filed as a feature
order (below).

**Class 3 is exempt, with the reason recorded.** These waits are not simulations of elapsed
time; they are waits on genuine concurrency — a daemon thread that must finish
(`tests/test_health_sweep.py:110-124`, spinning until `daemon.health_sweeping` clears) or a
subprocess that must die (`tests/test_worker_session.py:189-195`, spinning until
`claude_cli.process_alive(turn["pid"])` goes false). **A fake clock cannot make another
thread run.** Faking them would mean inventing a thread-synchronisation layer — a fake
scheduler every daemon thread and every spawned `claude` process cooperates with — to
replace a 10ms poll loop that is already deterministic in effect (it either observes the
event or fails the assertion). The cost is a new concurrency primitive in the test harness;
the benefit is nil, because these sites cannot produce the class-1 bug: nothing on either
side of them is faked, so there is no second clock to disagree with.

Recording that reasoning IS part of the fix. The user said "timeouts included"; answering it
with a documented exemption keeps the decision in the record instead of silently dropping it.

## The fix

Three things ship in this branch: the one-line correction, a guard against the flake SHAPE,
and a rule that keeps new test files clock-free.

### 1. The one-line fix (already applied)

`tests/test_health_sweep.py:1034` now reads `fake_claude.health_rate_limited(reset="in 5h")`,
with a comment pointing at the sibling test that explains the clock split. Per kn-898350cc,
the relative form is the ruling: a staged usage limit is expressed as a DURATION, never as a
time of day, because the duration never touches the wall clock.

### 2. The flake-shape guard — `tests/test_clock_hygiene.py`, assertion A

A new hygiene test file in the shape of `tests/test_spec_location.py`: enumerate tracked
files with `git ls-files -z` rooted at `Path(__file__).resolve().parent.parent`, filter to
`tests/**/*.py`, `ast.parse` each one. A tracked test file that does not parse fails the
test — that is a defect in its own right, not a case to skip.

For every `FunctionDef`/`AsyncFunctionDef` whose name starts with `test_`, walk its own
subtree (plus its decorator list and parameter names) and decide two predicates:

**(a) does this test fake `jarvis.db.now`?** Any one of:
* a parameter named `clock` — requesting the fake-clock fixture is a parameter name, which
  is why the parameter list is part of the walk;
* a `with` item whose call target resolves to a `_MovedNow`-style context manager (an
  `ast.Call` whose `func` name ends in `MovedNow`);
* a `setattr`/`monkeypatch.setattr` call with the string constant `"jarvis.db.now"` as an
  argument, or with `("jarvis.db", "now")`-shaped arguments;
* an assignment whose target is the attribute chain `jarvis.db.now` (the raw-`setattr` form
  `_MovedNow` uses internally).

**(b) does this test stage a wall-clock time of day?** Any `ast.Constant` string in the same
subtree matching a time-of-day pattern: one or two digits, an optional `:MM`, and either an
am/pm marker or a timezone parenthetical — `11:50pm`, `3am`, `12:01am (UTC)`,
`9:50am (America/Los_Angeles)`. `in 5h`, `2d 3h 15m` and bare integers do not match, which
is the point: the fix for a flagged site is to rewrite the literal as a duration.

Both true in one test function ⇒ FAIL, naming the file, the function, the literal, and the
remedy ("state the reset as a duration — `reset=\"in 5h\"` — see kn-898350cc").

**Why an AST walk and not a line grep.** Two concrete false positives decide it:

1. **Scope.** The halves must co-occur inside ONE test function. A grep has no function
   boundaries, so the honest unit is the file — and `tests/test_health_sweep.py` would then
   fail for ever: it defines the `clock` fixture at module level and legitimately contains
   wall-clock strings in tests that do NOT fake `db.now` (the `_reset_moment` parsing cases
   are exactly the tests that must pass a time of day in). A guard that permanently fails
   the file the bug came from gets deleted within a week.
2. **Literals vs prose.** A grep matches comments. The fix's own comment at
   `tests/test_health_sweep.py:1054-1060` quotes "between 23:46 and 23:50" to explain the
   flake, and the allowlist entries and this spec quote `11:50pm`. An AST walk sees only
   `ast.Constant` strings, so documentation about the defect never trips the check on the
   defect — and that also means the hygiene file needs no self-exemption trick of its own.

### 3. The rule — `tests/test_clock_hygiene.py`, assertion B: a per-file allowlist that can only shrink

Any tracked file under `tests/` that is NOT on the allowlist may contain **no real-clock read
at all**. Detected by AST call resolution, not by text, for the same comment reason as above:
an `ast.Call` whose dotted target is one of

```
time.time        time.monotonic        time.sleep
datetime.now     datetime.utcnow       date.today
```

including the `from time import sleep` / `from datetime import datetime` forms (resolve the
module aliases and `from`-imports in each file's own import table; a bare `sleep()` counts
when `sleep` was imported from `time`).

The allowlist is a module-level mapping in the hygiene file:

```python
ALLOWED: dict[str, str] = {
    "tests/test_message_stuck.py": "class 2 — rows back-dated with time.time() - N; ops.waiting_on takes no now=",
    "tests/test_worker_session.py": "class 3 — spin-waits on a real subprocess dying",
    ...
}
```

Rules on the mapping itself, each its own assertion:

1. **Keyed by FILE, never by file+line.** A line-keyed allowlist breaks on every insertion
   above a listed line, so it would fail on unrelated edits and be ratcheted blindly. The
   file is the unit a reviewer can actually re-judge.
2. **Every value must name its class**: the reason string must match `^class (2|3) — .+`.
   An entry with no class and no reason is not an exemption, it is a shrug.
3. **A stale entry fails.** If an allowlisted file contains no real-clock read any more, the
   test FAILS and tells the author to delete the entry. This is the mechanism that makes the
   list shrink-only: a converted file leaves the list for good and cannot be re-admitted
   without someone adding a line and a reason.
4. **An entry for a path that does not exist fails** (renamed or deleted file).
5. A **new** test file is, by construction, not on the list — so it cannot read the real
   clock, which is the standing rule the user asked for.

The allowlist is generated from the census at implementation time: ~45 files, each line
carrying class 2 or class 3. The spec does not enumerate them; the classes above are the
decision, the lines are mechanical.

### No threshold, by design

Neo's condition was that "any threshold the hygiene test uses is a catalog key, not a module
constant". This design satisfies it **by having no threshold**: no count of permitted reads,
no budget, no ratchet number anywhere. Assertion A is a shape (both halves, or not).
Assertion B is a per-file boolean (listed with a reason, or zero reads). The only module-level
data is the allowlist mapping and the detected-call name set, and neither is a tunable — they
are the rule itself, which is exactly what belongs in the test file.

If an implementer finds a threshold unavoidable, it is a catalog key and never a module
constant: a field on the relevant `catalog.py` config dataclass alongside the existing
`health_every_ticks` / `health_stale_minutes` style settings (`src/jarvis/catalog.py:1726-1728`),
reachable by `jarvis config set <project> <key> <value>`. The reason is the usual one — a
number the OS enforces must be visible and changeable through the CLI, not buried in a
source file where the user cannot see it or move it. Per kn-1d6cbc82, nothing about this
rule lands in `src/`: the hygiene test is a test, it names no path from `src/`, and no
production module learns about `tests/`.

### What the same defect looks like outside this tree

`evals/` and `tests_browser/` are separate suites, not under pytest's `testpaths`, and are
OUT of the walk. Their reason is class-3-shaped wholesale: `evals/llm` makes real model
calls and `tests_browser/` drives a real browser, so both wait on real latency by nature.
Stating it here means a future conversion widens the glob rather than quietly discovering
that the guard never covered them. If a class-1 flake ever appears in either, it is the same
bug — a faked `jarvis.db.now` beside an un-faked production clock — and assertion A is the
check to extend, not to copy.

### Rejected alternatives

1. **Freeze the clock process-wide (autouse fixture, or `freezegun`).** Breaks class 3
   outright: a frozen `time.monotonic` never reaches a spin-wait deadline and a patched
   `time.sleep` never yields, so every test waiting on a real daemon thread or a real
   subprocess hangs or spins for ever. It also adds a dependency to do it.
2. **Fix the one site and stop.** The next test that pairs the `clock` fixture with a
   wall-clock string reproduces the flake exactly, and nothing in the repo records that the
   shape is forbidden. The user widened the ask for this reason.
3. **A count ratchet ("no more than N real-clock reads in `tests/`").** A number says nothing
   about whether the remaining reads are SAFE — 200 class-2 reads are fine and one class-1
   read is a flake, and a count cannot tell them apart. It also drifts upward inside large
   PRs and would need a catalog key to be legitimate. The per-file allowlist with a named
   class makes each survivor argue for itself.
4. **A line-keyed allowlist.** Fails on every line shift; the resulting noise trains people
   to re-run the generator instead of reading the entry.
5. **Thread `now=` through production now (convert class 2 in this branch).** Touches
   `ops.waiting_on`, `check_project` and their callers, with a test diff across ~45 files, on
   top of a one-line fix. Deferred, not dropped — see below.
6. **Grep instead of AST.** Argued and rejected above on two named false positives.

### Tests

The hygiene file is its own test. It must come with proof that it is ARMED, in the style of
`tests/test_spec_location.py`'s two complementary checks:

1. Assertion A fires on a synthesised source string containing a test function that takes
   `clock` and passes `reset="11:50pm (America/Los_Angeles)"` — i.e. the pre-fix text of
   `test_a_refused_sweep_is_held_and_not_recorded_as_a_failure`. Parse-from-string, so no
   fixture file on disk and nothing for the real walk to trip over.
2. Assertion A does NOT fire on the same function with `reset="in 5h"`, nor on a test that
   passes a wall-clock literal WITHOUT faking `db.now` (the `_reset_moment` parsing cases).
3. Assertion B fires on a synthesised unlisted file calling `time.time()`, and does not fire
   on a file that merely MENTIONS `time.time()` in a comment or docstring.
4. The stale-entry rule fires: an allowlist entry for a file with no real-clock read fails
   with the "delete this entry" message.
5. The live suite passes with the shipped allowlist — which is the regression test for the
   census being accurate.

### Follow-ups the lead files, and what goes to the knowledge base

* A **feature order** for class 2: give the production functions an injectable clock
  (`ops.waiting_on`, `check_project` and the rest of the ~200 sites' call targets take
  `now=`), convert the tests, and delete each converted file from the allowlist. That order
  is what eventually empties the class-2 half of the list.
* A **knowledge entry** recording the class-3 exemption AND its reason: spin-waits on real
  daemon threads and subprocesses are exempt from the clock rule because a fake clock cannot
  make another thread run, and faking them would require a thread-synchronisation layer.
  Without that entry in the base, the next agent reading "no test reads the real clock" will
  try to convert them.

### Not covered here

* Unifying the two clocks (`jarvis.db.now` vs `claude_cli`'s `datetime.fromtimestamp`) — the
  named root cause, deferred to the class-2 feature order.
* Converting any class-2 site.
* Converting or re-designing any class-3 spin-wait.
* `evals/` and `tests_browser/`.
* Any change under `src/`. This branch's production diff is empty.
