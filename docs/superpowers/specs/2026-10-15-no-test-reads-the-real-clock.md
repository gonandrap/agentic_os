# No test reads the real clock

Feature order `fo-2891ead5`, project `jarvis_os`. Follow-up to `wo-059520b9` / issue 1066,
filed under Neo's answer to question 1756 (option C). Scope confirmed by Neo in answer to
question 1759.

## 0 — The rule, the seam, and the census

### 0.1 The rule

Knowledge entry `kn-4c922b4c`, stated by the user on issue 1066:

> No test may read the real clock. Every time a test needs comes from an injectable or
> patched time function; timeouts and waits are faked too.

The failure this prevents: `tests/test_health_sweep.py` staged a usage-limit reset of
`11:50pm (America/Los_Angeles)` and asserted `row["reopens_at"] > db.now()` against the real
wall clock. CI run 38693984070 ran it at 06:50:43 UTC — 23:50:43 PDT — and it failed. A test
that passes 1439 minutes out of 1440 is not a passing test.

`wo-059520b9` fixed that one site and added a hygiene guard. This feature burns the guard's
exemption list down.

### 0.2 The seam: `db.now()`, and nothing else

`src/jarvis/db.py:46`:

```python
def now() -> float:
    return time.time()
```

This is already the fleet's clock. Two facts make it the whole answer:

1. **No module anywhere does `from .db import now`** — zero hits across `src/jarvis`. Every
   production read goes through the module attribute, so a single
   `monkeypatch.setattr("jarvis.db.now", c)` moves all of it at once.
2. **Neo confirmed (question 1759) that the guard flags raw `time.time()` in tests and does
   NOT flag `db.now()`.** `db.now()` is the sanctioned clock.

So the per-function design decision this feature was created to make has one answer almost
everywhere: **convert the raw `time.time()` age read to `db.now()`.** It is one line per
site and no signature changes. Threading a `now=` parameter through forty-odd production
functions buys nothing that the module attribute already gives, and Neo ruled against it.

A `now=` parameter is added in exactly two places, both in section 6, and both
optional-with-default — never required. `kn-2bb3a44d` (a spec that makes a parameter
required must enumerate the call sites another unmerged branch is adding, or main goes red
at the merge) therefore does not bite, and **no child may make a clock parameter required.**

### 0.3 The governing rule: within one answer, one instant

This is not a new argument. `invariants.clock`'s docstring already makes it
(`src/jarvis/invariants.py:1974`), and `src/jarvis/health.py:177-181` documents the live
hazard it describes:

> ON `db.now()`, THE OS'S CLOCK, which is what the sweep itself measures every window
> against (`due`'s `now`). `true_blockers` defaults to `time.time()`, and the two differ for
> anything whose age is read from a row rather than from the wall — a message queued at the
> OS's clock would otherwise read as months undelivered.

A caller that pins one clock while its callee reads another moves half of the comparison and
gets an answer belonging to neither. Every child works to this rule: **one answer, one
instant.**

### 0.4 A `now=` default that reads the wall clock is still a real-clock read

The idiom `now = time.time() if now is None else now` appears nine times and looks
injectable. It is not, when the caller omits the argument — which is the common case:

| Site | Function |
|---|---|
| `src/jarvis/invariants.py:1379` | `stuck_message` |
| `src/jarvis/invariants.py:1600` | `parked_reason` |
| `src/jarvis/ops.py:2108` | — |
| `src/jarvis/ops.py:9780` | — |
| `src/jarvis/ops.py:15252` | — |
| `src/jarvis/holds.py:205` | — |
| `src/jarvis/holds.py:223` | — |
| `src/jarvis/fleet.py:138` | — |
| `src/jarvis/daemon.py:2271` | — |

Every one of these defaults becomes `db.now()`. Section 6 owns all nine.

### 0.5 The census

Measured on this branch, 2026-10-15, counting raw `time.time()` under `tests/` — the
population the guard flags:

**182 sites across 40 files.**

The important shape, and the reason this feature is cheaper than it looks: **most sites are
fixture stamp CONSTRUCTION, not clock reads under test.** `time.time() - 60` building a
back-dated row becomes `db.now() - 60`, needs no production change and no reasoning.
`tests/test_background_orphan.py` is the largest single file at 19 sites and every one of
them is a local `now = time.time()` passed straight into a `since=`/`until=` argument.

Two clock-free techniques are already prior art in the tree and are the whole recipe:

* **Build the stamp from `db.now()`**, so a patched clock moves the fixture with the code.
* **Back-date the ROW, not the clock** — `tests/test_message_stuck.py:407-415` documents
  this and when to prefer it.

Section 1 lands that recipe as a knowledge entry so sections 3-7 do not each re-derive it.

### 0.6 The exemption mechanism

`wo-059520b9` shipped the guard with a per-FILE allowlist in one file. That file is the one
artifact all seven children would edit: seven PRs each deleting adjacent lines from one
sorted list conflict constantly, and every conflict costs a full work-order relaunch to
notice.

Neo approved relaying a request to `wo-059520b9`, before it merges, to make the exemption a
**marker comment inside each test file** that the guard reads:

```python
# clock-exempt: class-3 spin-wait on a real daemon thread
```

The reason then lives next to the code it exempts, the list cannot drift from the tree, and
each child edits only files it already owns. The request was sent as message 2974.

**Plan against per-file markers.** Neo's instruction was explicit: do not hold for the
sibling. If `wo-059520b9` merges a central list anyway, section 2 converts it — section 2
owns the exemption mechanism either way, and is the only section that may touch it.

### 0.7 Done, for the feature as a whole

The guard's exemption set contains only class-3 and class-4 files, each with its reason
recorded next to it, and every convertible file has left it.

**This is a feature-close check, not a child's acceptance criterion.** No single child can
evaluate it — it is true only of the union of the seven pull requests, and it cannot be
evaluated at all until `wo-059520b9` has merged. Each child proves its own files instead,
per the criterion in its section. Do not ask a child to verify section 0.7.

### 0.8 The two exempt classes

**Class 3 — reviewed and exempt. No child may convert it.** Roughly 40
`deadline = time.monotonic() + t` / `while ...: time.sleep(0.01)` spin-waits on REAL daemon
threads and subprocesses. A fake clock cannot make another thread run, so faking these would
mean inventing a thread-synchronisation layer and the tests would stop testing the daemon's
own guards. The user's "timeouts included" is answered by this exemption being recorded, not
by converting them. **Any change that converts a class-3 site is wrong.**

Known sites: `tests/test_health_sweep.py:120-122`, `tests/test_worker_session.py:191`, and
the same shape in `test_supervisor`, `test_validation_loop`, `test_turn_transport`,
`test_remedies`, `test_wo_fix`, `test_feature_validation`, `test_forced_validation`,
`test_rejudge_moved_head`, `test_alarm_escalation`, `test_config_reload`.

**Class 4 — new, legitimised by Neo in answer to question 1759, and this feature owns
enumerating it.** A test comparing an in-process instant against a stamp written by the
KERNEL or by the fake-claude SUBPROCESS. No fake clock can reach either, because neither
reads the patched attribute. The hazard is already documented in four places in the tree:

| Site | Why no fake clock reaches it |
|---|---|
| `src/jarvis/testing.py:229, 317, 475` | inside the `FAKE_CLAUDE` source string — a different process |
| `src/jarvis/testing.py:2446-2453` | coarse-mtime inversion; broke on 3.13 and not 3.11/3.12 |
| `tests/test_config_reload.py:75, 185, 221` | `os.utime` forges an mtime for a real `stat()` the kernel makes |
| `tests/test_uncollected_task_block.py:32-36, 141-147` | the guard bounds its scan with `until=time.time()`; a turn stamped `now` lands ON that bound |
| `tests/test_ui_observability.py:428-429, 473-475` | measures real elapsed wall time to prove the dashboard answers while wedged |
| `src/jarvis/release.py:264` | converts `CLOCK_BOOTTIME` to a wall epoch, compared against systemd's own `usec` field |

Section 2 enumerates class 4 with a reason per file. No other section may add to it: a child
that believes it has found a new class-4 site reports it rather than exempting itself.

### 0.9 Sites that stay real, and why

* **`src/jarvis/release.py:264`** — `boot_epoch = time.time() - time.clock_gettime(CLOCK_BOOTTIME)`.
  Converts between two REAL clocks. A frozen wall clock plus a live boottime gives a boot
  epoch wrong by the freeze offset. Exempt, class 4.
* **`src/jarvis/ui/app.py:348` (`started_at`) and `:470` (`uptime_seconds`)** — a
  self-consistent pair on one seam. Converting would not break it; it is left alone because
  no test needs to control it. Section 7 leaves both with a comment saying so.
* **`src/jarvis/fleet.py:199-201`** — already the `at=`-everywhere shape this feature is
  arguing for, and its comment cites `invariants.clock` by name. Do not touch.

### 0.9a The fixture's public interface — fixed here, not negotiable per section

Sections 3-7 are dispatched into fresh sessions that cannot see section 1's pull request, so
"use the shared fixture" would be an instruction pointing at something the worker cannot
read. The names are therefore fixed in this spec and every section uses them verbatim.
Section 1 implements exactly this surface in `src/jarvis/testing.py`:

```python
TEST_EPOCH = 1760000000.0          # 2025-10-09T07:33:20Z — a constant, never time.time()

clock                              # the pytest fixture; NOT autouse
clock.at                           # the current fake instant, a float
clock.advance(seconds)             # move it forward; SECONDS, positional, no default
clock.ticking()                    # every subsequent read is strictly later than the last
with clock.moved(seconds): ...     # a scoped different instant, restored on exit even if
                                   #   the block raises
```

`clock.advance()` with no argument and `clock.advance(minutes=...)` must both raise
`TypeError`. That is deliberate: it converts section 3's rewrite of
`tests/test_health_sweep.py` from a silent 60x unit error into an immediate failure.

If a section finds this surface insufficient, it reports that — it does not extend it
(section 0.10 rule 5).

### 0.10 Rules every section works to

1. **One instant per answer** (section 0.3).
2. **No clock parameter may be required** (section 0.2).
3. **Thresholds are catalog keys, never module constants.** If a section needs a new
   threshold, it is a catalog key.
4. **No test file belongs to two sections.** `tests/test_health_sweep.py` alone is 2300+
   lines; two children editing it is a certain conflict. Each section below lists its files
   exhaustively and owns them completely — including their exemption markers.
5. **Only section 1 may edit `src/jarvis/testing.py`.** If the shared fixture is
   insufficient for your file, report it; do not extend it in your PR.
6. **Do not run the full suite** (pinned user ruling, 2026-09-18). Run the targeted tests
   for the files you changed and cite CI's check-runs for the suite. The full suite takes
   21-22 minutes, which guarantees your whole conversation is re-sent at the cache-write
   rate, and a local green that contradicts CI is what gets a round rejected.

### 0.11 How every section proves itself

The guard from `wo-059520b9` may not be on `main` when you start, so none of these name it.
All of them run today.

**Residual sites in your own files.** The pattern deliberately excludes
`time.monotonic()` and `time.sleep(` — those are class-3 and must survive:

```bash
rg -n -e 'time\.time\(\)' -e 'time\.localtime\(\)' -e 'datetime\.now\(\)' -e 'datetime\.utcnow\(\)' <your files>
```

Done when every line this still prints is a class-3 or class-4 site carrying a
`# clock-exempt: <reason>` comment, and your PR lists each remaining hit as
`path:line — class 3|4 — reason`. It is an enumeration to defend, not a number to hit.

**You may not ADD a raw clock read.** This is what catches a worker "preserving behaviour"
in a new helper, and catches section 1 seeding its epoch from the wall clock:

```bash
git diff origin/main -- . | rg '^\+' | rg -e 'time\.time\(\)' -e 'time\.localtime\(\)'
```

**Record the before-state at the START of your session**, or the after-state means nothing:
the per-file raw-site counts, and `uv run pytest <your files> -q --collect-only | tail -2`.
Both go in the PR.

**The four checks that catch a green-but-wrong conversion.**

1. *Targeted run*: `uv run pytest <your files> -q` — zero failed, zero errors, and zero NEW
   skips.
2. *Collected count must not fall.* Deleting or renaming a test is the commonest false
   green.
3. *Assertions must not be deleted.* `git diff -U0 origin/main -- <your files>` must not
   remove more `assert` lines than it adds; name any net loss, with the reason.
4. *No new skip or xfail.*

**The mutation check — the only one that proves a converted test still asserts anything.**
Take the test in your files that most depends on time. Break the thing it is supposed to
catch — one line, in production code or in the clock mode. Run that one test. Prove it goes
RED. Revert. Quote in the PR: the line you mutated, the test that failed, and the first line
of the failure. **A section whose mutation check stays green has shipped a vacuous test,
whatever the suite says.** Each section below names its own mutation.

**Three things that cannot be proven by a test, so do not invent one.** Section 0.7 is a
feature-close check over all seven PRs. A class-3 exemption is a negative — that no fake
clock could make another thread run — and the check is the reviewed evidence: your PR quotes
the `time.monotonic()` or `time.sleep(` line each mark sits on, and a mark with no spin-wait
on the quoted line is wrong. And `tests/test_knowledge_retrieval_eval_harness.py:539` has
not been read closely by anyone; its criterion is an outcome, not an assertion — read it and
its shim, then state the verdict and the reason in the PR.

---

## 1 — The shared clock fixture

Deliver one clock fixture in `src/jarvis/testing.py`, re-exported through
`tests/conftest.py`, that every later section uses.

**It must not be autouse.** `db.now()` is the stamp source for every row in all three
databases — roughly 60 call sites in `project_store.py`, 30 in `central_store.py`, 10 in
`neo_store.py`, writing `ts`, `created_at`, `updated_at`, `wo_events.ts`,
`wo_turns.started_at`. Patching it does not merely move comparisons, it rewrites the data
the fixture writes. A test that does not ask for a controlled clock must not get one.

**A frozen clock is not sufficient**, and in some tests is actively wrong.
`tests/test_parked_work.py:77-79` already proves it and says why:

> Freezing `db.now` is not good enough here: the whole of `STALE_FINISH_BLOCKER` is that one
> recorded moment is earlier than another, and a constant clock makes every row in the
> fixture simultaneous.

So the fixture carries four modes. All four are already in the tree as prior art; point at
it rather than inventing an interface:

| Mode | What it is for | Prior art |
|---|---|---|
| frozen | one instant, nothing moves | `tests/test_supervisor.py:860, 888, 938`; `tests/test_gates.py:475, 823`; `tests/test_wo_why.py:69, 312, 347`; `tests/test_cost_meter_payload.py:467` |
| hand-advanced | the test moves it deliberately | `tests/test_scheduler.py:33`; `tests/test_health_sweep.py:50` |
| ticking | row ORDER matters; every read is later than the last | `tests/test_parked_work.py:83` (`_rewind`) |
| scoped move | one block sees a different instant | `tests/test_health_sweep.py:1005` (`_MovedNow`) |

Two interface decisions, fixed here because the two existing `_Clock` classes disagree and a
later section must not have to guess:

* **`advance()` takes SECONDS.** `tests/test_scheduler.py:33` uses seconds;
  `tests/test_health_sweep.py:67` uses `minutes` with a default. Seconds wins — it is the
  unit `db.now()` is in. Section 3 rewrites the `minutes=` callers.
* **The seed is a CONSTANT epoch, never `time.time()`.** All five existing ad-hoc patchers
  seed from the real clock, which is to say the existing fixtures are themselves instances of
  the bug this feature exists to kill. Pick a fixed, readable epoch and put it in
  `src/jarvis/testing.py` beside the fixture.

Both forms of patching are in use in the tree — `monkeypatch.setattr("jarvis.db.now", c)`
and `monkeypatch.setattr(db, "now", c)`. The fixture settles on one and later sections use
only the fixture.

Prove it with a test file of the fixture's own, `tests/test_clock_fixture.py`. **Every
assertion in it is against a LITERAL, never against another clock read** — that is what
makes it trustworthy to the five sections downstream.

1. **The constant epoch, the property the whole feature turns on.** A test that does not
   request the fixture asserts the constant against a hard-coded number:
   `assert testing.TEST_EPOCH == 1760000000.0`. Then a frozen-mode test asserts
   `db.now() == 1760000000.0` — the literal, not `clock.at`, not `TEST_EPOCH`. A wall-clock
   seed cannot pass that, which is exactly what all five pre-existing ad-hoc patchers would
   fail. Do **not** try to prove this by running the test twice and comparing output:
   nothing compares the two runs, so it proves nothing the literal does not.
2. **Frozen.** `a = db.now(); b = db.now(); assert a == b == 1760000000.0`. Then the
   assertion that proves the patch reaches `db.now`'s ~100 store call sites and not merely
   comparisons: create a work order and assert `float(wo["created_at"]) == 1760000000.0`.
3. **Hand-advanced, in seconds.** `clock.advance(90); assert db.now() == 1760000090.0`. Then
   both traps as explicit failures: `with pytest.raises(TypeError): clock.advance()` and
   `with pytest.raises(TypeError): clock.advance(minutes=61)`. These two are worth more than
   the rest of the file.
4. **Ticking, non-vacuously.** `db.now() < db.now() < db.now()` is not enough on its own;
   also assert that two rows written through the store land in order — create two turns and
   assert `float(second["started_at"]) > float(first["started_at"])`. That is the property
   `tests/test_parked_work.py` consumes.
5. **Scoped move.** Inside the block, `db.now() == 1760000000.0 + delta`; after it, back to
   `1760000000.0` exactly. Then the leak test: raise inside the block
   (`with pytest.raises(RuntimeError): ...`) and assert the clock was restored anyway.
   `_MovedNow` at `tests/test_health_sweep.py:1013/:1019` assigns `jarvis.db.now` directly,
   outside monkeypatch, so an exception inside its block leaks the fake into the next test.
   Yours must not.
6. **Not autouse, with zero clock reads.** Capture `_REAL_NOW = db.now` at module import.
   One test that does not request the fixture asserts `db.now is _REAL_NOW`; one that does
   asserts `db.now is not _REAL_NOW`. Do not prove this by comparing `db.now()` against
   `time.time()` — that would be a real-clock read in the one file whose whole point is that
   it has none.

**Mutation check:** change the fixture's seed to `time.time()`, run
`uv run pytest tests/test_clock_fixture.py -q`, prove the constant-epoch test fails, revert,
quote the failure.

Then migrate exactly one caller as an end-to-end proof — `tests/test_scheduler.py`, which has
one site and one fixture. `rg -n 'time\.time\(\)' tests/test_scheduler.py` then prints
nothing.

`tests/conftest.py` is an import list today (19 lines, `# noqa: F401`): add the fixture to
its existing `from jarvis.testing import (...)` block and reorder nothing.
`uv run pytest tests/test_isolation_gate.py -q` (21 tests) is the cheap proof that
`jarvis.testing` still imports clean for every suite under the root `conftest.py`.

Do **not** migrate the other four ad-hoc patchers. Section 3 owns them. This section stays
small on purpose: five sections wait on its merge, and `waiting_pr_merge` does not satisfy a
dependency edge, so every edge here is a real wall-clock wait.

You also add the class-4 markers for the four exempt sites **in this file**, because you are
the only work order permitted to edit it: `src/jarvis/testing.py:229, 317, 475` (inside the
`FAKE_CLAUDE` source string — a different process, which cannot see the patch) and
`src/jarvis/testing.py:2446-2453` (the coarse-mtime inversion in `signin`, which broke on
3.13 and not 3.11/3.12). The existing 10-line comment at `:2447-2453` is already a good
reason; it needs only the marker token added. These four are the one exception to the
otherwise strict rule that the work order defining class 4 records it — it cannot, because
it may not touch this file.

Land the recipe from section 0.5 as a knowledge entry, scoped to the project
(`jarvis learn add ... --project jarvis_os --topic testing`): build the stamp from
`db.now()`, back-date the row rather than the clock, and which of the four modes to reach
for. Sections 3-7 read it instead of re-deriving it.

## 2 — The exemption mechanism, and the two exempt classes

Own the guard's exemption surface end to end, and record class 3 and class 4 on it with a
reason per file.

This section converts nothing and changes no production code. It exists so that no other
section has to decide whether a file is exempt — a question that, decided six times in
parallel, gets six answers.

Three jobs:

1. **Settle the mechanism.** Per section 0.6, the intended shape is a marker comment in each
   exempt file that the guard reads. If `wo-059520b9` merged a central allowlist instead,
   convert it to markers here; this section is the only one that may touch the mechanism
   either way. Read what the guard actually scans before assuming — `src/jarvis/testing.py`
   is not under `tests/`, which is correct and necessary (its `FAKE_CLAUDE` string runs in
   another process) but is also a loophole worth a comment in the guard.
2. **Record class 3** from the enumeration in section 0.8, each file with the reason that
   applies to it. Convert nothing in these files.
3. **Record class 4** from the table in section 0.8, each file with its own reason — the
   reasons differ per file and a generic one is not acceptable.

**You do NOT edit `src/jarvis/testing.py`**, even though section 0.8's class-4 table lists
four sites in it. Section 0.10 rule 5 gives that file to one owner, and that owner adds
those four markers itself. Your class-4 job covers the files on your list below and nothing
else. This is the one place where the work order defining a class does not record every
instance of it.

Files owned by this section, exhaustively: `tests/test_worker_session.py`,
`tests/test_config_reload.py`, `tests/test_uncollected_task_block.py`,
`tests/test_turn_transport.py`, `tests/test_knowledge_retrieval_eval_harness.py`,
`tests/test_auth_resume.py`, and the class-3 files that carry no raw `time.time()` of their
own: `tests/test_supervisor.py`, `tests/test_validation_loop.py`, `tests/test_remedies.py`,
`tests/test_wo_fix.py`, `tests/test_forced_validation.py`,
`tests/test_rejudge_moved_head.py`, `tests/test_alarm_escalation.py`.

Within those files, class-3 and class-4 sites are marked and left alone; any site that is
neither is converted per the section 0.5 recipe. `tests/test_uncollected_task_block.py` is
the file where both appear together and the distinction is written out in its own comments at
`:32-36` and `:141-147` — read them rather than guessing.

`tests/test_knowledge_retrieval_eval_harness.py:539` names "the shim's `time.time()` and the
sandbox's file mtimes" and may have a clock shim of its own. Determine whether it needs
class-4 standing or ordinary conversion; it has not been read closely.

### 2.1 The false green: a mark that is not true

**A marker silences the guard for ever, so a mis-mark is a permanent hole and nothing
downstream will ever fail.** That makes this section's risk unlike every other section's: it
cannot be caught by running tests.

For every class-3 mark, your PR quotes the `time.monotonic()` deadline or
`while ...: time.sleep(` line the marker sits on. **A mark with no spin-wait on the quoted
line is wrong.**

Second false green: marking a whole FILE where only two sites are exempt, which silently
exempts every site added later. So the marker sits on the site's own line or the line
directly above it — never at module top, unless every site in the file is exempt. The seven
pure class-3 files qualify; `tests/test_uncollected_task_block.py` does not, because it has
convertible sites too.

A duplicated reason string is a generic reason, and generic reasons are not acceptable.
Check it mechanically: `rg -o -N 'clock-exempt:.*' tests/ | sort | uniq -d` must print
nothing.

For the seven files that are pure class 3, the diff must be comment-only by construction:

```bash
git diff -U0 origin/main -- tests/test_supervisor.py tests/test_validation_loop.py \
  tests/test_remedies.py tests/test_wo_fix.py tests/test_forced_validation.py \
  tests/test_rejudge_moved_head.py tests/test_alarm_escalation.py \
  | rg '^[+-]' | rg -v '^(\+\+\+|---)' | rg -v 'clock-exempt'
```

prints nothing.

Keep `tests/test_auth_resume.py`'s use of the `signin` fixture working unmodified:
`src/jarvis/testing.py:2441-2456` stamps the mtime from `time.time()` deliberately, and the
10-line comment at `:2447-2453` explains the kernel's coarse clock inverting on 3.13.

## 3 — The files that already hand-rolled a clock

Migrate the four remaining ad-hoc clock patchers onto the shared fixture and delete them.

**The shared fixture already exists in `src/jarvis/testing.py`** — another work order landed
it before you started, and you must not edit that file. Its surface, which you use verbatim:

```python
TEST_EPOCH = 1760000000.0          # a constant epoch, never time.time()
clock                              # the pytest fixture; NOT autouse
clock.at                           # the current fake instant, a float
clock.advance(seconds)             # SECONDS, positional, no default
clock.ticking()                    # every read strictly later than the last
with clock.moved(seconds): ...     # scoped instant, restored even if the block raises
```

`clock.advance()` and `clock.advance(minutes=...)` both raise `TypeError` by design, so a
missed unit conversion fails immediately rather than being wrong by 60x.

Files owned, exhaustively: `tests/test_health_sweep.py`, `tests/test_parked_work.py`,
`tests/test_evolution_report.py`.

What goes:

* `_Clock` + the `clock` fixture at `tests/test_health_sweep.py:50-76`
* `_MovedNow` at `tests/test_health_sweep.py:1004`
* `_rewind` at `tests/test_parked_work.py:74`
* the `clock` fixture at `tests/test_evolution_report.py:61`

These are one artifact with four copies, not four artifacts: `tests/test_scheduler.py:34`
points at `test_health_sweep`'s `_Clock` for its own rationale. That is why they move
together.

The mechanical part, and the reason this is its own session: `advance()` now takes seconds,
so every `minutes=` caller in `tests/test_health_sweep.py` is rewritten —
`clock.advance()` at `:117`, `clock.advance(minutes=61)` at `:846, :855, :975, :990, :1072`,
and `clock.advance(invariants.OS_HEALTH_SWEEP_DARK_MINUTES + STEP_MINUTES)` at `:2342`.
`STEP_MINUTES` and the default-advance behaviour disappear with the old class; decide
per-caller what the explicit second count is rather than wrapping the new fixture to restore
the old signature.

`tests/test_health_sweep.py` also carries class-3 spin-waits at `:120-122` and a site at
`:2167` that patches `db.now` to `time.time() - 600` — still a real-clock read, and one of
the clearer illustrations of why seeding from the wall clock is the bug. Mark the class-3
sites in this file; section 2 does not own them, because section 0.10 rule 4 gives each file
to exactly one section and this file is yours.

`tests/test_parked_work.py` is the file that proves the ticking mode is load-bearing, and
this is your **mandatory mutation check**: swap the ticking mode for the frozen mode at all
six former `_rewind` call sites (`:255, :274, :287, :310, :334, :464`), run
`uv run pytest tests/test_parked_work.py -q`, and prove at least one test FAILS. Revert, and
quote the failing test name and assertion in the PR. If every test still passes frozen, the
migration has made the fixture simultaneous and the file no longer tests what its own
docstring at `:76-79` says it tests.

**A second mutation, for the `advance()` unit rewrite.** A too-small second count leaves
nothing past the thresholds, and `_sweep` at `tests/test_health_sweep.py:110-124` only
asserts that the sweep FINISHED — so a sweep that did nothing still passes, and so does
every test asserting an absence. Replace one rewritten `advance(N)` with `advance(1)`, run
the file, and prove the first-look and dedupe tests go red; revert. If they stay green your
second counts are not load-bearing and the thresholds are untested.

Convert `tests/test_health_sweep.py:2167` too — it patches `db.now` to `time.time() - 600`,
which is both a real-clock read and the clearest instance of the bug in the tree. After
conversion the offset comes from the fixture's epoch.

Do not change the thresholds themselves (`FLOOR_MINUTES`,
`invariants.OS_HEALTH_SWEEP_DARK_MINUTES`): the rewrite changes the UNIT of the advance,
never what counts as due. `tests/test_scheduler.py` is already migrated and is not yours,
but its comment at `:34` points at the `_Clock` in your file — update that reference when
you delete the class it names.

## 4 — Bulk stamp construction: no production change

Convert the largest group, which is also the cheapest: test-side stamp construction where
the production code already reads `db.now()`.

Files owned, exhaustively: `tests/test_background_orphan.py`, `tests/test_live.py`,
`tests/test_invariants.py`, `tests/test_investigation_orders.py`,
`tests/test_turn_usage.py`, `tests/test_budget.py`, `tests/test_budget_blind_spot.py`,
`tests/test_ui_cost.py`, `tests/test_config_version.py`, `tests/test_scheduled_actor.py`,
`tests/test_repair_resettle.py`. Roughly 45 sites.

**This section changes no file under `src/jarvis/`.** If you find yourself needing to, the
site belongs to another section — report it rather than reaching across the seam.

**You need no clock fixture.** Every site here is a stamp your own test writes, so
`db.now() - 60` is the whole change. A shared `clock` fixture does exist in
`src/jarvis/testing.py` and introducing it here would be actively harmful: production stamps
rows through `db.now()`, so freezing it makes every row in a fixture simultaneous and any
assertion that turns on one row preceding another goes vacuous while still passing.
`git diff origin/main -- tests/ | rg '^\+.*clock'` must print nothing.

The dominant shape, `tests/test_background_orphan.py` (19 sites, every one of them): a local
`now = time.time()` passed straight into a `since=`/`until=` argument of
`background.jobs_left_running`. `now = db.now()`. That is the whole change.

`tests/test_live.py` (8 sites) stamps transcript rows — `prompt_row(time.time() - 40, ...)`,
`assistant_row`, `tool_use_row` at `:623-625, :648-649, :675-677`. Same change. Note these
are rows the test itself writes, not rows written by the fake-claude subprocess, so they are
not class 4.

`tests/test_uncollected_task_block.py` looks like it belongs here and does not — it is
section 2's, because its sites are class 4 and the distinction is subtle.

### 4.1 The false green, and the one criterion that catches it

**This section cannot fail loudly.** `db.now()` and `time.time()` return the same value when
nothing patches them, so every test here passes whether or not your conversion is correct.
The acceptance criterion is therefore about the DIFF, not the result:

```bash
git diff -U0 origin/main -- tests/ | rg '^[+-]' | rg -v '^(\+\+\+|---)' \
  | rg -v 'time\.time\(\)' | rg -v 'db\.now\(\)' | rg -v '^\+from jarvis import'
```

must print nothing. Every changed line differs from `main` only in the clock call, plus
added imports. This section is ~45 one-token edits; anything else in the diff is an
unplanned behaviour change. It also catches the "tidy a boundary while you are in there"
failure — turning `until=db.now()` into `until=db.now() + 1`.

`tests/test_repair_resettle.py:249-295` asserts `parked_reason(...) == STALE_FINISH_BLOCKER`
with `long_after = float(turn["ended_at"]) + 365*24*3600`. Those assertions must stay
inequality-driven off the ROW's `ended_at` and not off a frozen clock. Quote them in the PR
as unchanged.

The residual-site command from section 0.11 must print nothing at all over your eleven
files: none of them has an exempt site, so you add no marker comments.

## 5 — Usage limits, rate-limit holds, the fleet brake

Convert the usage-window group and the two production sites behind it.

**The shared fixture already exists in `src/jarvis/testing.py`** — another work order landed
it and you must not edit that file. `TEST_EPOCH = 1760000000.0` is its constant epoch; the
fixture is `clock` (not autouse), with `clock.at`, `clock.advance(seconds)`,
`clock.ticking()` and `with clock.moved(seconds):`.

Files owned, exhaustively: `tests/test_rate_limit_retry.py`,
`tests/test_usage_window_is_account_wide.py`, `tests/test_transport_resilience.py`,
`tests/test_validation_usage_limit.py`, `tests/test_fleet_cap.py`,
`tests/test_ui_fleet_brake.py`, `tests/test_parked_retry.py`. Roughly 29 sites.

Production surface, exhaustively — the same expression in four places:

| Site | Expression |
|---|---|
| `src/jarvis/neo.py:454` | `reopens = e.limit.reset_at or (time.time() + RATE_LIMIT_FALLBACK_DELAY)` |
| `src/jarvis/supervisor.py:974` | `reopens = exc.limit.reset_at or (time.time() + RATE_LIMIT_FALLBACK_DELAY)` |
| `src/jarvis/daemon.py:3416-3417` | the same fallback |
| `src/jarvis/daemon.py:4111-4112` | the same fallback |

All four become `db.now()`. `daemon.py:3416` and `:4111` are in this section rather than
section 6 so that no two sections edit `src/jarvis/daemon.py` within a few hundred lines of
each other.

Already correct, and not to be touched: `src/jarvis/fleet.py` (`at=`/`now=` throughout),
`claude_cli.parse_limit`, `worker_session.Pause.due` — all already take an instant. The
`holds` module already takes `now=`; its two wall-clock DEFAULTS are section 6's.

This is the group where the original bug lived: issue 1066's failing assertion was a
usage-limit reset string parsed against the wall clock. A test here that stages a reset
time must derive it from the controlled clock, never from a wall-clock-relative literal
like `11:50pm`.

**The false green here is the exact tautology that caused issue 1066.** A test that stages
`reopens_at = db.now() + DELAY` and then asserts `row["reopens_at"] > db.now()` under a
frozen clock is true no matter what the production code computed — including if it computed
nothing at all. Assert the exact value instead:

```python
assert row["reopens_at"] == pytest.approx(testing.TEST_EPOCH + supervisor.RATE_LIMIT_FALLBACK_DELAY)
```

**Mutation check:** drop the delay from one of the four fallbacks so it reads plain
`db.now()`, run the file, prove a test goes red, revert, quote it. An inequality that
survives that mutation is a vacuous assertion.

**Timezone replay, aimed directly at issue 1066:** run your targeted command three times —
once as-is, once under `TZ=UTC`, once under `TZ=America/Los_Angeles`. All three green. The
original failure only appeared between 23:50 and midnight Pacific, so a test whose
expectation still depends on the reader's local date fails one of these three runs.

You change `src/jarvis/neo.py` and `src/jarvis/supervisor.py`, so also run
`uv run pytest tests/test_neo.py tests/test_supervisor.py -q` and report it green.
`tests/test_supervisor.py` belongs to another section: if it fails, that is a report, not a
fix.

Never assert a literal time-of-day string. `invariants.clock`
(`src/jarvis/invariants.py:1974-1998`) appends `on %a %d %b` whenever the stamp is not today
by the real local clock, and a constant epoch is never today — another section owns that
line. `tests/test_fleet_cap.py:540-541` is safe only because it calls `invariants.clock` on
both sides of the comparison; do the same.

## 6 — One instant per answer: the two signatures and the nine defaults

The only section that changes a function signature, and the section that retires the
two-clocks-for-one-question hazard.

**The shared fixture already exists in `src/jarvis/testing.py`** — another work order landed
it and you must not edit that file. `TEST_EPOCH = 1760000000.0` is its constant epoch; the
fixture is `clock` (not autouse), with `clock.at`, `clock.advance(seconds)`,
`clock.ticking()` and `with clock.moved(seconds):`.

Files owned, exhaustively: `tests/test_message_stuck.py`, `tests/test_validation_ci_hold.py`,
`tests/test_validation_auth_hold.py`, `tests/test_validation_void.py`,
`tests/test_validation_release_skip.py`, `tests/test_precheck_before_the_panel.py`,
`tests/test_feature_validation.py`. Roughly 18 sites.

### 6.1 The asymmetry

`invariants.parked_reason` (`src/jarvis/invariants.py:1565`) takes `now=` and forwards it
into `holds.held(store, wo_id, now=now)` and `h.overlap(ended, now, now)` at `:1613-1617` —
then calls `ops.waiting_on(store, wo)` with no instant. So one answer is computed against two
instants today. The test docstring at `tests/test_message_stuck.py:140-146` documents this
and is the clearest statement of it in the tree:

> THE FIXTURE HAS TO BE OLD IN THE ROWS, not in a `now` argument. `parked_reason` takes one
> and `ops.waiting_on`, which it consults, does not — it reads `time.time()` itself — so
> passing `now=` ages only half the question.

The repair:

* `ops.waiting_on` (`src/jarvis/ops.py:1427`) gains `now: float | None = None`. It reads no
  clock itself; it forwards to `invariants.stuck_message`
  (`src/jarvis/invariants.py:1351`), which does.
* `invariants.check_messages_are_delivered` (`src/jarvis/invariants.py:2604`, bare
  `now = time.time()` at `:2635`) gains the same. It is an `INVARIANTS` registry entry
  called as `fn(store)`, so optional-with-default is the only shape available.
* Update the now-obsolete comment at `src/jarvis/health.py:177-181` in the same PR — it
  describes the hazard being fixed.

`waiting_on`'s call sites: `src/jarvis/ops.py:1628` (`resume_in_auto`), `:2410` (`diagnose`),
`:2877` (`fix`), `:12165` (`rule_facts`), `src/jarvis/ui/app.py:1562`. Both parameters are
keyword-with-default, so `kn-2bb3a44d` does not apply and **neither may be made required.**
`src/jarvis/invariants.py`, `src/jarvis/ops.py` and `src/jarvis/daemon.py` are the three
files most likely to be under concurrent edit on another branch, so re-run
`find_referencing_symbols` on `waiting_on` when you start rather than trusting this list.

### 6.2 The nine defaults

Every `now = time.time() if now is None else now` from the table in section 0.4 becomes
`db.now()`. `invariants.py:1600` (`parked_reason`) is the one `true_blockers` inherits, and
the one `health.py:177-181` complains about. This section owns all nine, including
`src/jarvis/holds.py:205, 223`, `src/jarvis/fleet.py:138` and `src/jarvis/daemon.py:2271`
— the only `daemon.py` line this section touches.

### 6.3 `invariants.clock`'s own today-test

`src/jarvis/invariants.py:1996` — inside `clock(ts)` — reads
`time.localtime(time.time())[:3]` to decide whether `ts` is today. Convert the
`time.time()` to `db.now()`.

This is the function whose docstring argues this feature's governing rule, and it is
already most of the way there deliberately: it uses `time.localtime(time.time())` rather
than a no-argument `time.localtime()` precisely because the no-arg form "reads the C clock
directly, so it is a SECOND clock this function cannot be told about". Moving the remaining
`time.time()` to `db.now()` finishes its own argument.

Why it matters beyond tidiness, and why no other section may touch it: under a constant
epoch nothing is ever "today", so this branch renders every stamp in the long form
`"%H:%M on %a %d %b"` instead of a bare `"%H:%M"`. Sections 5 and 7 are told that asserted
time-of-day literals must go through `invariants.clock` rather than being written out, and
this conversion is what eventually lets a frozen-clock test assert the short form at all.
Do not change the format strings or the today/not-today rule — only the clock the test
reads.

### 6.4 The validation holds

`src/jarvis/invariants.py:1732` — `if until <= time.time():` — is the same defect in the
hold-expiry path, and the test files above are what cover it. Convert it here.

`tests/test_feature_validation.py` carries class-3 spin-waits as well as convertible sites.
Mark its class-3 sites; it is your file, not section 2's.

### 6.5 Proving the forwarding, which is the whole point

**The false green here is subtle and it is the likely one:** the parameter gets added, the
test passes `now=`, and the test ALSO back-dates the row — so the new parameter is never
load-bearing, and `parked_reason`'s forwarding could be missing entirely while everything
passes.

Two required checks.

1. At least one new or rewritten test in `tests/test_message_stuck.py` proves the blocker
   fires from `now=` ALONE, with no `_age_the_turn` or `_age_the_message` call: the row stays
   current and the answer comes entirely from the passed instant.
2. **Mutation:** revert the forwarding at `src/jarvis/invariants.py:1617` back to
   `waiting_on(store, wo)`, run `uv run pytest tests/test_message_stuck.py -q`, prove it
   goes RED, revert, quote the test. Green after that mutation means the asymmetry
   documented at `tests/test_message_stuck.py:140-146` is still there and your PR proves
   nothing.

`check_messages_are_delivered` has **no test that names it today** —
`rg 'check_messages_are_delivered' tests/` returns nothing — so changing it is invisible to
the suite. Add coverage that reaches it through `check_project` with `now=`, and mutation-check
it the same way.

Confirm the signatures mechanically, and that the registry call shape still works:

```bash
uv run python -I -c "import inspect; from jarvis import ops, invariants; print(inspect.signature(ops.waiting_on)); print(inspect.signature(invariants.check_messages_are_delivered))"
```

`check_messages_are_delivered` must remain callable as `fn(store)` — it is an `INVARIANTS`
registry entry.

**The regression set matters more here than your own file list**, because `waiting_on`'s
test-side callers mostly live outside it: `tests/test_auth_resume.py:342`,
`tests/test_manager_order.py:679`, `tests/test_parked_work.py:198`,
`tests/test_resume_auto_diagnosis.py:122, :394`, `tests/test_waiting_on_neo.py:496, :507`.
Run them and report green. **Do not edit any of them** — three belong to other sections and
two to none. A failure there is a report, not a fix.

Keep `_age_the_turn` and `_age_the_message` in `tests/test_message_stuck.py` working: they
are the prior art the knowledge entry cites and other sections read them.

## 7 — Display ages, logs, and release staging

Convert the read-side group: ages rendered for a human, log windows, and the release
staging marker.

**The shared fixture already exists in `src/jarvis/testing.py`** — another work order landed
it and you must not edit that file. `TEST_EPOCH = 1760000000.0` is its constant epoch; the
fixture is `clock` (not autouse), with `clock.at`, `clock.advance(seconds)`,
`clock.ticking()` and `with clock.moved(seconds):`.

**`invariants.clock` is not yours and reads the real clock on purpose.**
`src/jarvis/invariants.py:1996` renders "is this today?" through
`time.localtime(time.time())[:3]`, and another section owns that line. Until it is
converted, a constant epoch is never "today", so every stamp renders in the long form
`"%H:%M on %a %d %b"` rather than a bare `"%H:%M"`. **Never assert a literal time-of-day
string**: call `invariants.clock` on both sides of the comparison, as
`tests/test_fleet_cap.py:540-541` already does. Check yourself with
`rg -n '[0-2][0-9]:[0-5][0-9]' <your files>`.

Files owned, exhaustively: `tests/test_release_staging.py`, `tests/test_ui_observability.py`,
`tests/test_base_red.py`, `tests/test_release_red_defer.py`, `tests/test_ui_wiring.py`,
`tests/test_uilog.py`, `tests/test_nav_volume.py`. Roughly 32 sites.

Production surface: `src/jarvis/ui/app.py:193, 209`, `src/jarvis/cli.py:446`,
`src/jarvis/nav_volume.py:393`, `src/jarvis/wiring.py:239, 273, 276, 325`,
`src/jarvis/daemon.py:5209` and the base-health freshness sites around
`src/jarvis/daemon.py:7051`.

### 7.1 `uilog`'s writer and its readers are one unit

Five lines, one PR, and converting a subset is worse than converting none.
`src/jarvis/uilog.py:150, 166, 188` write `stamp = time.strftime(_STAMP, time.localtime())`
— a no-arg `localtime()`, which is the C clock, a *third* clock. `uilog.py:263` reads that
stamp back with `time.mktime(time.strptime(...))` and compares it against
`cutoff = time.time() - within_seconds` (`:316, :370`). Writer and reader are already two
clocks.

Convert all of them or none — the no-arg `time.localtime()` becomes
`time.localtime(db.now())`. A test that froze the reader while the writer stamped live would
silently include or drop everything in the 24-hour window, exactly the failure
`invariants.clock`'s docstring forbids.

**No existing test would notice**, which is why this needs a new one: `tests/test_uilog.py:89`
builds its own stamps with `time.strftime(uilog._STAMP, time.localtime(time.time() - seconds_ago))`,
so test and writer drift together and the suite stays green either way. Add one boundary
test with the clock frozen: a row stamped `cutoff - 1s` is EXCLUDED and one stamped
`cutoff + 1s` is INCLUDED.

**Mutation check:** revert the writer to the no-arg `time.localtime()` while leaving the
reader converted, run `uv run pytest tests/test_uilog.py -q`, prove the boundary test goes
red, revert, quote it. That test is the only thing between this section and a silent
off-by-a-whole-window.

Run your targeted command three times — as-is, under `TZ=UTC`, and under
`TZ=America/Los_Angeles`. The TZ replay is not optional here: `uilog`'s stamp format goes
through `localtime`, and `invariants.clock`'s today-branch is local-date sensitive.

### 7.2 Pairs that must move together

`src/jarvis/wiring.py:239, 273, 276, 325` are a cache-freshness pair on one seam: convert
all four or none. Low value either way, but not separable.

### 7.3 Leave alone, with a comment

* `src/jarvis/release.py:264` — class 4, section 0.9. Add the comment saying why.
* `src/jarvis/ui/app.py:348, 470` — the uptime pair, section 0.9. Add the comment.
* `tests/test_ui_observability.py:428-429, 473-475` — class 4: real elapsed wall time,
  proving the dashboard answers while wedged. Mark, do not convert. The other six sites in
  that file do convert.

`tests/test_release_staging.py` is the largest file here at 15 sites, mostly `staged_at` and
`restart_at` fields of a JSON marker.

`:592` and `:605` — `before = int(time.time())` …
`assert before <= marker["staged_at"] <= time.time() + 1` — are **class 4, and this is
decided, not a decision for you to make.** `staged_at` is written by
`scripts/shipit.sh:475`, which stamps it with `$(date +%s)` in a shell subprocess that
cannot see any patched Python attribute. Mark both lines with that reason — "the marker is
stamped by `scripts/shipit.sh:475` in another process" — and keep the sandwich asserting
real elapsed time.

Converting both ends to `db.now()` is the trap: the sandwich would then compare a frozen
Python epoch against a live shell `date`, and the test would pass always or fail always
depending on the sign of the epoch relative to now. Neither outcome tests anything.

---

## Agent profile

You are a test-hygiene engineer working on Jarvis OS, a Python agentic operating system in
`~/workspace/agentic_os`. Your assignment is one section of
`docs/superpowers/specs/2026-10-15-no-test-reads-the-real-clock.md`. You convert tests that
read the real wall clock onto a controlled clock, and you make the smallest production change
that lets them.

**The rule you serve.** No test may read the real clock. A test that passes 1439 minutes out
of 1440 is not a passing test — the bug that started this feature was an assertion that
failed only when CI ran it between 23:50 and midnight Los Angeles time. A hygiene guard now
fails the build on any raw `time.time()` in a test file that is not explicitly exempt.

**The seam, and it is the whole answer.** `src/jarvis/db.py:46` is
`def now(): return time.time()`. Nothing in `src/jarvis` does `from .db import now`, so one
`monkeypatch.setattr("jarvis.db.now", c)` moves every production read at once. The guard
flags raw `time.time()` and does not flag `db.now()`. So your default move is to convert a
raw `time.time()` age read to `db.now()` — one line, no signature change. Do not thread a
`now=` parameter through production to solve a problem the module attribute already solves;
that option was considered and ruled out.

**The governing rule: within one answer, one instant.** A caller that pins one clock while
its callee reads another moves half of the comparison and gets an answer belonging to
neither. `invariants.clock`'s docstring at `src/jarvis/invariants.py:1974` argues this in
full and `src/jarvis/health.py:177-181` documents a live instance. Read both before you
change a comparison. Corollary you will meet: a `now=` parameter whose default is
`time.time()` is still a real-clock read whenever the caller omits it.

**Traps, in the order you will hit them.**

* A frozen clock is often wrong. `db.now()` stamps every row in all three SQLite databases
  — roughly a hundred call sites writing `ts`, `created_at`, `updated_at`,
  `wo_turns.started_at`. Patching it rewrites the data your fixture writes, not just the
  comparisons. Where a test turns on one row being earlier than another, a constant clock
  makes the whole fixture simultaneous and the test passes for the wrong reason.
  `tests/test_parked_work.py:77-79` explains this; use the fixture's ticking mode there.
* Most sites are not clock reads under test, they are fixture stamp construction. Prefer the
  boring fix: `db.now() - 60` instead of `time.time() - 60`, or back-date the row rather
  than the clock (`tests/test_message_stuck.py:407-415`). Reach for a controlled clock only
  when the test genuinely needs to control time.
* Never convert a class-3 site: a `time.monotonic()` deadline or a
  `while ...: time.sleep(0.01)` spin-wait on a real daemon thread or subprocess. A fake
  clock cannot make another thread run. These are reviewed and permanently exempt, and
  converting one means the test stops testing the daemon's own guards.
* Never exempt yourself. If you believe you have found a site no fake clock can reach,
  report it; one section owns the exemption surface and it is probably not yours.
* Never make a clock parameter required. `kn-2bb3a44d`: a spec that makes a parameter
  required must enumerate the call sites another unmerged branch is adding, or main goes red
  at the merge. Keyword-with-default, always.
* Stay inside your section's file list. It is exhaustive, and no test file belongs to two
  sections — several of these files are over 2000 lines and two workers editing one is a
  certain conflict. Only the fixture section may edit `src/jarvis/testing.py`; if the shared
  fixture cannot do what you need, say so rather than extending it in your PR.
* Thresholds are catalog keys, never module constants.

**How you work.** Serena is wired to this repo and its code map is committed: read the
`testing`, `codebase-map` and `work-order-lifecycle` memories before exploring, and use
`find_symbol` and `find_referencing_symbols` rather than grepping for symbols. Read the
knowledge base before you start — `jarvis learn search "<term>" --project jarvis_os`, then
`jarvis learn show <id>` — and navigate the spec with `jarvis spec section` rather than
opening it whole. Any doubt about scope goes to Neo: `jarvis wo ask <your-wo-id> "..."`, then
end your turn. Record decisions you made without doubt as assumptions, including small ones.

**Do not run the full test suite.** It takes 21-22 minutes, which guarantees your whole
conversation is re-sent at the cache-write rate, and a local green that contradicts CI is
what gets a round rejected. Run the targeted tests for the files you changed and cite CI's
check-runs for the suite. Your PR must say which files left the guard's exemption set and
which you left on it with what reason.

**Never** convert a class-3 spin-wait, edit a file outside your section's list, make a clock
parameter required, or seed a clock from `time.time()` — that last one is the bug this whole
feature exists to kill, and all five of the fixtures that existed before it did exactly
that.
