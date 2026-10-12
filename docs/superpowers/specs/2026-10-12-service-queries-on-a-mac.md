# Service queries on a Mac

Work order wo-07f5914a · project `jarvis_os` · 2026-10-12

Branch cut from `origin/worktree-mac-prod-services`, which already ported the SHELL half:
`deploy/com.jarvis.daemon.plist.template` and `deploy/com.jarvis.ui.plist.template`, the
`JARVIS_SERVICE_MANAGER` knob in `scripts/install_prod_service.sh:48-55` and
`scripts/shipit.sh:98-107`, and the `docs/DEPLOYMENT.md` section "macOS: launchd instead
of systemd" (`:179-203`). This work order ports the PYTHON half. The design below is
DECIDED — it is written up with its reasoning, not re-opened.

## The problem

The macOS production host logs this on every boot:

```
jarvisd.systemd WARNING could not query ActiveState of jarvis-ui.service: [Errno 2] No such file or directory: 'systemctl'
```

That string is emitted by `systemd_units._show` (`src/jarvis/systemd_units.py:190-197`,
the `except (OSError, subprocess.SubprocessError)` arm). `jarvis-ui.service` identifies the
caller exactly: `Daemon.check_ui_health` at `src/jarvis/daemon.py:2122` is the only reader
of `systemd_units.unit_active` that ever passes `release.UI_UNIT` — `worker_session.py:870`
passes a per-turn transient unit name.

Three call paths shell out to systemd unconditionally. None of them is reachable on a Mac,
and none of them says so.

### 1. The dashboard self-heal is silently OFF (`daemon.py:2122`)

```python
if not systemd_units.unit_active(release.UI_UNIT):
    return "down"
```

`unit_active` reads `_show(unit, "ActiveState")`, which swallows the missing-binary
`OSError`, logs the warning above and returns `""`; `"" not in ("active", …)` so
`unit_active` is False, so `check_ui_health` returns `down` for EVERY wedge.

`down` is the deliberately-stopped branch (`daemon.py:2093-2098`): it restarts nothing,
raises nothing, and leaves the healthy-sighting state untouched. So on this host the whole
of `docs/superpowers/specs/2026-10-08-the-dashboard-reports-and-heals-its-own-wedge.md` —
probe, stack dump, inbox item, restart, cap — is dead code behind one `systemctl` that does
not exist. The failure is INVISIBLE: the function returns a normal verdict, the only trace
is a `WARNING` nobody reads, and `jarvis doctor` reports nothing.

### 2. A staged release can never apply (`release.py:230-248`, called at `:306-307`)

`SystemdRunner.restart_unit` runs `["systemctl", "--user", "restart", unit]`;
`restart_unit_detached` runs `systemd-run --user --collect … /bin/sh -c "sleep 3;
systemctl --user restart <unit>"`. Neither binary exists on a Mac. `SystemdRunner._run`
(`release.py:221-228`) does NOT catch `OSError`, so the `FileNotFoundError` propagates out
of `maybe_restart` after the marker has already been written `state=restarting`
(`release.py:301-303`) — i.e. the marker records a restart that never happened.

### 3. Every staged release would then park `failed_verification` (`release.py:250-266`)

`unit_start_time` reads `ExecMainStartTimestampMonotonic` via `systemctl show`.
`verify_on_boot` requires a start time on BOTH units (`release.py:350-357`) and appends a
problem per unit when it is `None`, so on a Mac the verdict is two problems, marker parked
`failed_verification`, attention flag, notification — for a release that may have applied
perfectly.

### Root cause

**The OS has two answers to "which init system owns production's two services", and only
the shell half was given one.** `scripts/install_prod_service.sh:48-55` and
`scripts/shipit.sh:98-107` resolve `JARVIS_SERVICE_MANAGER`, else `uname -s`. The Python
half has no such question: `systemctl` is spelled as a literal in `release.py:232`,
`release.py:247`, `release.py:257` and implicitly in `systemd_units.systemctl_bin()`, and
the systemd assumption is baked into the module layout itself.

This is NOT "the warning is noisy". Suppressing the log would leave all three paths broken
and remove the only evidence they are. The fix is to give Python the same answer, from the
same variable, with the same precedence, in ONE place.

### What already works on a Mac, and must keep working

`jarvis ui` and `jarvisd` run fine under launchd today; `shipit` from the user's own shell
restarts both agents with `launchctl kickstart -k` (`scripts/shipit.sh:529`, `:580`).
Verified live on the production Mac:

```
$ launchctl print gui/503/com.jarvis.ui
	state = running
	pid = 73299
	last exit code = 0
$ launchctl print gui/503/com.jarvis.nope
Could not find service "com.jarvis.nope" in domain for user gui: 503   # exit 113
```

Tab-indented `key = value` lines; a label that is not loaded exits non-zero.

## The fix

Six changes. Every one names the file and symbol it lands in.

### 1. New module `src/jarvis/service_manager.py` — the one place that knows

It owns two things and nothing else: WHICH init system runs production's two services, and
HOW to ask launchd about them. It must NOT import `release` at module scope (`release`
imports it — see §2), so the unit names appear here as literals with a drift test (§6.7).

```python
MANAGER_ENV = "JARVIS_SERVICE_MANAGER"
SYSTEMD, LAUNCHD = "systemd", "launchd"
LAUNCHCTL_BIN_ENV = "JARVIS_LAUNCHCTL_BIN"

#: systemd unit -> launchd label. The UNIT NAME stays canonical everywhere in the code
#: (`release.UI_UNIT`, `release.DAEMON_UNIT`); the label is derived at the edge, here.
LABELS = {"jarvis.service": "com.jarvis.daemon", "jarvis-ui.service": "com.jarvis.ui"}
```

`manager() -> str`: `os.environ.get(MANAGER_ENV, "").strip().lower()` when it is one of the
two values; otherwise `LAUNCHD` if `platform.system() == "Darwin"` else `SYSTEMD`.

* **Same precedence and same spelling as `install_prod_service.sh:48-50`** (env, then
  `uname -s`, Darwin → launchd), so the installer and the daemon can never disagree about
  which service to restart. A host where they disagreed would install launchd agents and
  then restart systemd units, or the reverse — a release that half-applies, which is the
  exact fault class `release.verify_on_boot` exists to catch.
* **An unrecognised value falls back to the uname answer and does not raise.** The shell
  scripts exit 2 on junk (`install_prod_service.sh:55`) and that is right for a one-shot
  install a human is watching; it is wrong here. This variable must not be able to stop the
  daemon booting, exactly as `systemd_units.transport()` (`systemd_units.py:80-84`) reads a
  junk value as `auto` rather than as a failure.
* **No caching.** Read the environment on every call: the suite flips `MANAGER_ENV` per
  test (§5), and `platform.system()` is a cheap `uname` the daemon makes at most a few
  times a tick.

`launchctl_bin() -> str` returns `os.environ.get(LAUNCHCTL_BIN_ENV, "launchctl")`, exactly
mirroring `systemd_units.systemctl_bin()` (`systemd_units.py:76-77`). This is the seam the
suite points at a stub, and it is the ONLY reason a test can exercise the launchd branch
without touching the user's real services (§5).

`label(unit)` → `LABELS[unit]`, raising `ValueError` naming the unit if it is not one of the
two. `target(unit)` → `f"gui/{os.getuid()}/{label(unit)}"`, the same domain shell builds as
`DOMAIN="gui/$UID"` (`install_prod_service.sh:126`, `shipit.sh:107`).

Everything that shells out to `launchctl`/`ps` lives here, so `release.LaunchdRunner` (§2)
is a thin adapter and there is one module to read when launchd behaviour is in question:

| function | what it does |
|---|---|
| `_print(unit) -> dict[str, str] \| None` | runs `launchctl print <target>`; `None` on non-zero exit or `OSError` |
| `_parse_print(text) -> dict[str, str]` | pure: tab-indented `key = value` lines into a dict. Pure so it is unit-testable against the captured output above |
| `_parse_etime(text) -> float \| None` | pure: `[[DD-]HH:]MM:SS` → seconds |
| `unit_active(unit) -> bool` | §1.1 below |
| `start_time(unit) -> float \| None` | §1.2 |
| `restart(unit) -> None` | §1.3 |
| `restart_detached(unit, tag) -> None` | §1.3 |
| `restart_hint(unit) -> str` | the command to print at a human: `systemctl --user restart <unit>` or `launchctl kickstart -k <target>` (§4) |

#### 1.1 `unit_active(unit)`

```python
def unit_active(unit: str) -> bool:
    if manager() != LAUNCHD:
        return systemd_units.unit_active(unit)
    ...
```

The systemd branch **delegates byte-identically** to `systemd_units.unit_active`
(`systemd_units.py:222-231`). Not a copy: that function is the one the Linux production
host has been answering with, and a re-implementation here would be a second definition of
"active" to keep in step. §6.5 pins the argv.

The launchd branch reads `_print(unit)`:

* `None` (non-zero exit — the not-loaded case above, or `launchctl` missing) → `False`.
* `state = running` → `True`.
* `state = waiting` / `spawn scheduled` → `True`, the same way the systemd branch counts
  `activating`: the job is loaded and launchd owns bringing it up. Consequence, stated so
  nobody is surprised by it: a `KeepAlive` job between crashes reads as active, so
  `check_ui_health` will kickstart it rather than call it `down` — and `kickstart -k` on a
  job launchd is already restarting is harmless. The case `down` exists for is
  `launchctl bootout`, which makes the label unfindable and lands on the first bullet.
* anything else → `False`.

A non-zero `launchctl print` is the NORMAL answer for a stopped service, so it logs at
DEBUG. Logging it at WARNING would re-create the line in "The problem" with a different
spelling.

#### 1.2 `start_time(unit)` — pid from launchd, elapsed from `ps`

launchd has no `ExecMainStartTimestampMonotonic`. Take `pid = …` from `_print(unit)`, then
`ps -p <pid> -o etime=`, parse `[[DD-]HH:]MM:SS` with `_parse_etime`, and return
`time.time() - elapsed`. `None` when there is no pid, when `ps` fails, or when the elapsed
string does not parse.

**NOT `ps -p <pid> -o lstart=`.** That is locale- and timezone-dependent prose — the same
trap `release.unit_start_time`'s own docstring documents for `ExecMainStartTimestamp`
(`release.py:253-255`). `etime` is a duration: no locale, no timezone, no year.

**Resolution is ONE SECOND**, and the truncation runs in the direction that cannot fail a
genuine restart — §3.

#### 1.3 Restarts

`restart(unit)` → `[launchctl_bin(), "kickstart", "-k", target(unit)]` via
`subprocess.run`. `-k` kills the running job first and launchd starts it again; it is what
`install_prod_service.sh:149` and `shipit.sh:529` already use, so the daemon's restart and
the installer's restart are the same operation.

`restart_detached(unit, tag)` has NO `systemd-run` counterpart. It is:

```python
subprocess.Popen(["/bin/sh", "-c", f"sleep 3; {launchctl_bin()} kickstart -k {target(unit)}"],
                 stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)
```

* **`Popen`, not `subprocess.run`.** The point of the detached form is that the restart
  outlives this process; `run` would block for the 3 seconds and then be killed with the
  daemon mid-sleep.
* **`start_new_session=True` is load-bearing.** launchd takes the job's whole process group
  down when it kickstarts it, so a child left in our group dies before its sleep ends. This
  is the same reason `install_prod_service.sh:152` uses `nohup … &` and `shipit.sh:580`
  uses `nohup /bin/sh -c 'sleep 3; launchctl kickstart …' &`. On systemd the equivalent
  guard is the transient unit's own cgroup (`release.py:234-248`); on launchd a new session
  is the whole mechanism, and dropping it silently reproduces the half-applied release.
* The 3-second sleep is kept, for `release.py:240`'s reason: it lets this daemon finish its
  tick and exit cleanly first.
* `tag` is unused in the argv (there is no unit name to slug) and is kept in the signature
  so both runners satisfy one protocol. It goes in a log line instead.

### 2. `release.LaunchdRunner` beside `SystemdRunner`, and `release.service_runner()`

`src/jarvis/release.py`.

**`SystemdRunner` (`release.py:215-266`) keeps its name, its API and its behaviour, byte for
byte.** Tests inject fakes shaped like it (`tests/test_release_staging.py:37-51`) and two
tests monkeypatch the CLASS (`tests/test_validation_release_skip.py:34`,
`tests/test_validation_void.py:33`). Any edit to its argv is a regression, and §6.5 asserts
that.

New sibling:

```python
class LaunchdRunner:
    """The same three questions, asked of launchd. See `service_manager`."""
    def restart_unit(self, unit): service_manager.restart(unit)
    def restart_unit_detached(self, unit, tag): service_manager.restart_detached(unit, tag)
    def unit_start_time(self, unit): return service_manager.start_time(unit)
```

Deliberately thin: the launchctl knowledge is in one module, and this class exists so the
injectable seam stays ONE protocol with three methods rather than two shapes callers have
to discriminate. THREE, not four — see §3: the runner protocol is unchanged, so
`tests/test_release_staging.py:37-51`'s `FakeRunner` and
`tests/test_ui_observability.py:635-645`'s `_FakeRunner` need no new method.

New factory:

```python
def service_runner() -> Any:
    if service_manager.manager() == service_manager.LAUNCHD:
        return LaunchdRunner()
    return SystemdRunner()
```

**The class names must be resolved as module globals inside the function body**, not
captured in a default argument or a module-level dict. `monkeypatch.setattr(release,
"SystemdRunner", FakeRunner)` in the two tests above rebinds the module attribute, and it
keeps working only if the lookup happens at call time.

Every current default construction calls the factory instead:

| site | today | after |
|---|---|---|
| `release.py:305` (`maybe_restart`) | `runner or SystemdRunner()` | `runner or service_runner()` |
| `release.py:340` (`verify_on_boot`) | `runner or SystemdRunner()` | `runner or service_runner()` |
| `daemon.py:2154` (`check_ui_health`) | `self.release_runner or release.SystemdRunner()` | `self.release_runner or release.service_runner()` |

The injected `runner` argument and `Daemon.release_runner` (`daemon.py:745`) are unchanged:
an injected fake still wins, which is what keeps the suite off both init systems.

Type annotations on `maybe_restart`/`verify_on_boot` (`release.py:272`, `:316`) read
`SystemdRunner | None` today. Widen to a small `Protocol` (`ServiceRunner`) declaring the
three methods, in `release.py` beside the classes. A `Union` of the two classes would be
wrong — the fakes are neither.

### 3. `verify_on_boot`: the truncation cannot fail a genuine restart

`release.verify_on_boot` (`release.py:350-357`) asks "did the unit's main process start
AFTER the restart" with `started <= reference` → problem. Under systemd that is exact:
`ExecMainStartTimestampMonotonic` is microseconds.

**Under launchd it stays correct, and the loop is unchanged.** `start_time` is
`now - floor(etime)`. Truncating the elapsed time DOWN makes the computed start LATER than
the true start, never earlier — and `now` is read after the `launchctl` + `ps` round trip,
which pushes it later still. So for a unit that genuinely restarted at some `t > reference`,
the computed `started` is `≥ t > reference` and the comparison passes. The error has one
sign, and it is the safe one.

The direction that would be unsafe is the opposite: a computed start EARLIER than the true
one, which could read a genuine restart as a stale process. `etime` cannot produce it.

So there is no tie-break, no `unit_main_pid`, no `marker["pids"]`, no
`LAUNCHD_CLOCK_SLACK` and no `_unit_restarted` helper. `maybe_restart` keeps writing the
marker it writes today, the per-unit loop at `release.py:350-357` stays exactly as it is,
and the runner protocol keeps its THREE methods — so the fakes at
`tests/test_release_staging.py:37-51` and `tests/test_ui_observability.py:635-645` need no
new method and no test in either file changes.

The residual risk is the mirror image, and it is the one the check already carries on
systemd: a unit that did NOT restart but whose true start sits under a second before
`reference` would be read as restarted. The window is bounded by the truncation (<1s) and
the thing on the other side of it is a release that has just been staged, written to disk
and had its shipping worker settle — minutes, not milliseconds. Widening the comparison to
buy a pid comparison would trade that for a real hole in both directions.

### 4. `Daemon.check_ui_health` asks the right manager

`src/jarvis/daemon.py`:

* `:2103` local import becomes `from . import release, service_manager, uilog` —
  `systemd_units` is then unused in this method and `:2122` is its only use in the file, so
  the name leaves the import.
* `:2122` → `if not service_manager.unit_active(release.UI_UNIT): return "down"`.
* `:2184`, the inbox body for a failed restart, hardcodes
  `` `systemctl --user restart jarvis-ui.service` ``. On a Mac that instruction does not
  work. Use `service_manager.restart_hint(release.UI_UNIT)`, so the one message the user
  acts on by hand names the command their host actually has.
* `:2094-2098`, the docstring paragraph about `systemctl --user stop jarvis-ui`, gains the
  launchd spelling (`launchctl bootout gui/$UID/com.jarvis.ui`) — the discriminator is the
  same, the command is not.

Nothing else in `check_ui_health` changes. The verdict vocabulary (`off`, `down`, `unseen`,
`healthy`, `cooldown`, `capped`, `restarted`, `restart_failed`) and every threshold stay as
`docs/superpowers/specs/2026-10-08-the-dashboard-reports-and-heals-its-own-wedge.md` set
them; this work order only makes the `down`/wedge discriminator answerable on a Mac.

### 5. The suite may never touch a real `launchctl`

`src/jarvis/testing.py`.

1. **Pin the manager in the isolation gate.** Add to `gate_environment`
   (`testing.py:107-150`), beside the `systemd_units.TRANSPORT_ENV: systemd_units.DIRECT`
   entry at `:124` and for the same reason spelled out there:

   ```python
   service_manager.MANAGER_ENV: service_manager.SYSTEMD,
   ```

   Without this the suite's answer depends on the machine running it. On the developer's
   Mac `manager()` would resolve to `launchd`, `release.service_runner()` would hand back a
   `LaunchdRunner`, and the two tests that monkeypatch `release.SystemdRunner`
   (`tests/test_validation_release_skip.py:34`, `tests/test_validation_void.py:33`) would
   patch a class nobody constructs — so a test run on a Mac would `launchctl kickstart -k`
   the HUMAN'S OWN dashboard. This single line is what makes the whole change safe to run
   on the host it was written for.

2. **`FAKE_LAUNCHCTL`**, beside `FAKE_SYSTEMCTL` (`testing.py:1522-1586`) and in its shape:
   a Python script written to disk and marked executable, recording every call to
   `$FAKE_LAUNCHD_DIR/ctl-calls.jsonl`, reading per-label state from
   `$FAKE_LAUNCHD_DIR/jobs/<label>.json`. It implements exactly two verbs:
   * `print gui/<uid>/<label>` — prints the tab-indented `state = …` / `pid = …` /
     `last exit code = …` block for a known label, and for an unknown one writes
     `Could not find service "<label>" in domain for user gui: <uid>` to stderr and exits
     non-zero. Both shapes are copied from the live capture in "The problem"; an invented
     output would make the parser pass against fiction.
   * `kickstart -k gui/<uid>/<label>` — records the call and bumps the job's recorded pid,
     so a test can prove a restart happened AND that it was a new process.

3. **`fake_launchd` fixture**, beside `fake_systemd` (`testing.py:1589-1627`): writes the
   stub, sets `FAKE_LAUNCHD_DIR`, `service_manager.LAUNCHCTL_BIN_ENV` to the stub and
   `service_manager.MANAGER_ENV` to `launchd`, and returns a handle exposing `.calls`,
   `.jobs` and a setter for a job's state/pid. Export it from `tests/conftest.py` where
   `fake_systemd` is already exported (`tests/conftest.py:8`).

4. **`ui_unit` in `tests/test_ui_observability.py:660-669` must move its patch** from
   `systemd_units.unit_active` to `service_manager.unit_active`. Its own docstring says why
   this matters: "the suite must never ask the real user manager, where the machine's OWN
   dashboard may be running." After §4 the fixture would patch a function
   `check_ui_health` no longer calls, and the ~8 tests using it would query the host.

**`ps` is NOT stubbed, deliberately.** It is read-only, it exists on every host the suite
runs on, and a second binary knob is a second thing to keep in step. The launchd start-time
test instead points the stub at a pid the TEST owns (§6.2), and the format parsing is
covered by unit tests on the pure `_parse_etime`.

### 6. `docs/DEPLOYMENT.md`

Rewrite the "Not ported:" paragraph (`:200-203`). The staged (`--stage`) restart and the UI
health check ARE ported as of this work order: the daemon restarts `com.jarvis.ui` and
`com.jarvis.daemon` with `launchctl kickstart -k`, verifies a release from
`launchctl print` plus `ps -o etime=`, and tells a wedged dashboard from a stopped one.

What remains not ported, and only this: the **per-turn transient unit**. Worker turns run
on the direct `Popen` transport on a Mac, so a daemon restart still takes a running turn
with it. Keep that sentence — it is the operator-visible consequence — and drop the claim
that `release.py` still calls `systemctl`.

## Test strategy

New file `tests/test_service_manager.py` unless stated. Every launchd assertion runs through
the stub (`fake_launchd`); no test invokes the real `launchctl`.

1. **`manager()` resolution, four cases.** `JARVIS_SERVICE_MANAGER=launchd` wins on a Linux
   `platform.system()`; `=systemd` wins on `Darwin`; unset + `Darwin` → `launchd`; unset +
   `Linux` → `systemd`; and `="LaunchD "` / `="nonsense"` → the uname answer, NOT a raise.
   Patch `platform.system`, never the real host's answer.
2. **`unit_active` on launchd.** With the stub reporting `state = running` → True; `state =
   waiting` → True; `state = exited` (or any other) → False; label absent (stub exits
   non-zero with the real "Could not find service" line) → False, and no WARNING is logged.
   Assert the recorded argv is exactly
   `["print", "gui/<uid>/com.jarvis.ui"]`.
3. **`start_time` on launchd.** The test spawns a process it owns (e.g. `sleep 30`), points
   the stub's job pid at it, and asserts the returned epoch is within a few seconds of the
   test's own `time.time()` before the call and is not `None`. Plus pure-function tests for
   `_parse_etime` on `"05:12"`, `"01:02:03"`, `"2-03:04:05"`, `""` and `"garbage"`, and for
   `_parse_print` on the captured block verbatim.
4. **`restart_unit` and `restart_unit_detached` on launchd.** Through
   `release.LaunchdRunner`: the first records
   `["kickstart", "-k", "gui/<uid>/com.jarvis.ui"]`; the second RETURNS IMMEDIATELY (assert
   the call takes well under the 3-second sleep) and, after waiting for the stub's record to
   appear, shows the same kickstart argv for `com.jarvis.daemon`. Assert the detached child
   is in its own session (`os.getsid`) — the guard §1.3 depends on.
5. **Regression: the systemd path is byte-identical.** With `MANAGER_ENV=systemd`, a
   `SystemdRunner` whose `_run` is captured issues exactly
   `["systemctl", "--user", "restart", "jarvis-ui.service"]`,
   `["systemd-run", "--user", "--collect", "--unit=jarvis-release-restart-<slug>", …,
   "/bin/sh", "-c", "sleep 3; systemctl --user restart jarvis.service"]` and
   `["systemctl", "--user", "show", "jarvis.service",
   "--property=ExecMainStartTimestampMonotonic", "--value"]`, and
   `service_manager.unit_active` delegates to `systemd_units.unit_active`. This is the test
   that makes the refactor provable rather than plausible.
6. **`check_ui_health` under launchd** (`tests/test_ui_observability.py`). With
   `fake_launchd` and the existing `wedged` fixture: the label reporting `state = running`
   plus a wedged `/healthz` → `restarted` and one `restart_unit` call; the label absent →
   `down`, no restart, no inbox item. This is the defect in the issue, asserted end to end.
7. **The unit/label map cannot drift.** `set(service_manager.LABELS) == {release.DAEMON_UNIT,
   release.UI_UNIT}` — the price of keeping the literals in `service_manager` to avoid the
   import cycle.
8. **Nothing in `tests/test_release_staging.py` changes.** Per §3 the runner protocol and
   `verify_on_boot`'s loop are untouched, so `FakeRunner` (`:37-51`) keeps its three
   methods. The file passing unedited IS the assertion that the refactor left the staged
   release path alone.
9. **The gate pins the manager.** In `tests/test_isolation_gate.py`, beside the existing
   assertions on `gate_environment`: the returned env sets `JARVIS_SERVICE_MANAGER=systemd`.

## Rejected alternatives

* **Teach `systemd_units.unit_active` to branch on the platform.** The smallest diff, and
  wrong. That module is documented top to bottom (`systemd_units.py:1-24`) as the
  WORKER-TURN transport — the cgroup escape — and launchd has no transient-unit equivalent,
  so launchd knowledge there would imply a port that is explicitly not happening. Its
  `unit_active` is also called for per-turn unit names (`worker_session.py:870`), which have
  no launchd label at all and would land in a `KeyError` or a meaningless lookup. The two
  questions share a verb and nothing else.
* **Keep one class and branch inside `SystemdRunner`'s methods.** The name would lie, and
  the regression test that matters (§6.5 — "the systemd argv is unchanged") becomes
  untestable without first proving which branch ran. Two classes behind one factory makes
  "systemd behaviour is untouched" a statement about a class nobody edited.
* **`launchctl bootout` + `bootstrap` instead of `kickstart -k`.** That is unload + reload:
  it re-reads the plist, so a plist mid-render leaves the service NOT RUNNING rather than
  restarted, and booting our own job out from inside it kills the daemon before it can
  bootstrap anything back. `kickstart -k` restarts in place and is already what the
  installer (`install_prod_service.sh:149`) and `shipit` (`shipit.sh:529`) use, so the
  daemon's restart and the human's restart are one operation.
* **`launchctl stop` + `start`.** Deprecated legacy subcommands against the old domain-less
  API, and `stop` on a `KeepAlive` job races launchd's own restart.
* **`ps -p <pid> -o lstart=` for the start time.** Locale and timezone prose. `release.py`
  already documents this exact trap for `ExecMainStartTimestamp` (`:253-255`) and chose the
  machine-comparable form; this follows it.
* **A pid tie-break in `verify_on_boot`, with a `LAUNCHD_CLOCK_SLACK` window.** It guards
  against an error `etime` cannot make: truncation moves the computed start LATER, so a
  genuine restart always reads as one (§3). The cost was a fourth runner method, a new
  marker key, a module constant and a rewritten per-unit loop — paid against nothing.
* **Widen `verify_on_boot`'s comparison by a couple of seconds.** Opens a real hole in the
  direction that IS reachable: a unit that genuinely did not restart, whose start sits a
  second inside the window, would verify. The comparison stays exact.
* **Read the daemon's own start time from `psutil` / `/proc`.** No `/proc` on macOS, and the
  question is about THE UNIT — including `jarvis-ui.service`, which is not this process.
* **A `JARVIS_PS_BIN` stub knob to go with `JARVIS_LAUNCHCTL_BIN`.** `ps -o etime=` is
  read-only, universal, and cannot reach a service; a second knob is a second thing to
  forget in `gate_environment`. The test owns the pid it asks about instead (§6.3).
* **Suppress the `ActiveState` warning on a Mac.** It addresses the log line and leaves all
  three call paths broken, with the only evidence removed. Symptom fix, named as such and
  refused.
* **Declare macOS unsupported for the staged restart and make `check_ui_health` return
  `off` there.** Honest, and it restates the defect as a policy: the dashboard self-heal
  and `shipit --stage` would stay off on the host this work order exists for, with no path
  back.
* **Port the worker-turn transport at the same time.** A genuinely bigger piece of work —
  `launchctl submit` is deprecated, and per-turn agent plists are an installed-surface
  decision, not a refactor. Out of scope below, with its operator consequence kept in
  `docs/DEPLOYMENT.md`.

## What this spec does NOT cover

* **The worker-turn transport.** `systemd_units.run_prefix` and the transient-unit path stay
  exactly as they are. Turns on a Mac run on the direct `Popen` path, which is already what
  happens: `systemd_units.available()` (`systemd_units.py:87-94`) requires both a
  `systemd-run` binary and `XDG_RUNTIME_DIR`, neither of which a Mac has. The consequence —
  a daemon restart takes a running turn with it — is kept in `docs/DEPLOYMENT.md` (§6).
* **`INV-SERVICE-PATH`** (`invariants.py:4330-4371`). It reads `Environment=PATH=` out of
  installed systemd unit FILES via `release.unit_environment` (`release.py:108-125`); a
  launchd plist is XML in `~/Library/LaunchAgents`, so on a Mac it returns `None` and the
  check is silent. Silent, not wrong — but it means the `gh`-on-PATH drift that cost a
  release on Linux (#90) is unchecked on a Mac. A separate order; naming it here so it is
  not mistaken for covered.
* **`jarvis start` / `jarvis stop` / `jarvis status`.** They operate the fleet, not the two
  production services, and touch neither init system.
* **Installing or bootstrapping the agents.** Already done on the branch this is cut from
  (`scripts/install_prod_service.sh`, the two plist templates).
* **Any change to the wedge detection itself** — probe, thresholds, stack dump, caps, inbox
  wording beyond the one command string at `daemon.py:2184`.
* **Linux behaviour.** Every systemd argv, message and threshold is unchanged, and §6.5
  exists to prove it.
