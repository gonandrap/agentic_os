# A stopped daemon is a liveness gap

Design decided by Neo question 1960, answer (a). The mechanism below is not re-opened
here; what this document exists for is the evidence, and a section a code comment can
cite instead of re-arguing itself.

## 1. The problem: no row predicate can see a dead daemon

Per-row derivation suppresses a blocker precisely when a daemon pass is going to act.
`invariants.parked_reason` (`src/jarvis/invariants.py:1617`) returns `None` for any row
whose wait is in `SPOKEN_FOR_WAITS` (`invariants.py:574`), built on `IN_FLIGHT_WAITS`
(`invariants.py:567`), whose own comment states the rule: "the `ops.waiting_on` answers
that mean the OS itself will do the next thing, with nobody typing". §5a of
`docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md` swaps that
predicate for `ops.scheduled_actor` (`src/jarvis/ops.py:17372`), which widens the same
suppression from "a wait the OS owns" to "a pass that selects this row" — wo-05461ee0.
Tree state, stated so nothing here reads as landed when it is not: this worktree's
`parked_reason` still reads `ops.waiting_on`, so the suppression is live today in its
narrower form and the defect below is the same under either predicate.

The suppression is correct. A row a pass WOULD select needs nobody, because the pass acts
the moment the daemon ticks. It is correct conditionally, and the condition is never
checked: *the daemon ticks*. Stop the daemon, or wedge one tick, and every open row still
names a pass, every blocker is still suppressed, and nothing is flagged. `tests/
test_parked_work.py:16-18` records that rule as the suite's own noise discipline — an
order waiting on "a dependency, a gate, Neo, a queued message, a scheduled retry or a
turn in flight is waiting CORRECTLY and must stay silent."

No row predicate can close this, by construction. Liveness is a fact about the PROCESS;
no column of any work order carries it, and a derivation that tried would be guessing
from silence. kn-4fb740a9 is this shape.

What exists today, so the delta is honest:

* `ops.os_status` (`src/jarvis/ops.py:518`) reports
  `daemon: {running: pid is not None, pid, catalog}` and `healthy: pid is not None and
  not attention`. Both read `daemon.daemon_running()` (`src/jarvis/daemon.py:10954`),
  which is PIDFILE-ONLY: it reads the pidfile and sends signal 0. A daemon whose process
  is alive and has stopped ticking reads `running=True`, and `healthy` stays True while
  every blocker it would have derived is suppressed.
* No last-reconcile-tick timestamp is persisted anywhere. Central state holds
  `daemon_pid`, `daemon_started_at` and `catalog_path`, all written once by
  `Daemon._write_pidfile` (`daemon.py:1301`) — a START time, which says nothing about
  whether the loop is still turning. The nearest per-pass stamp is
  `Daemon.STUCK_RUN_KEY` (`daemon.py:5555`, written `daemon.py:5621`).
* `invariants.check_stuck_sweep_alive` (`invariants.py:3887`) and
  `check_os_health_sweep_alive` beside it are the precedent for a fleet-wide fact
  reported once from a per-project checker — and NEITHER can see this. Both run from
  `Daemon.check_invariants`, on the reconcile beat, inside the process whose death is the
  question. A dead or wedged daemon runs no checker, so a checker cannot be the detector.
  That is the whole argument for read-time derivation.
* `uilog.UI_HEALTHZ_OK_AT` (`src/jarvis/uilog.py:104`), stamped by
  `Daemon.check_ui_health` (`daemon.py:2443`), is the OS's one existing positive-liveness
  stamp. It works because the observer and the subject are different processes. The
  daemon cannot be its own.

## 2. The fix: the daemon stamps, the CLI reads

Split across the only two processes available: the daemon writes a liveness stamp, and
`ops.os_status` — which runs in the reader's process — judges it.

### 2a. Writer

`Daemon.tick` (`daemon.py:1312`) stamps central-state key `reconcile_tick` at the END of
`tick()`, only on the reconcile beat it already computes at `daemon.py:1316`
(`reconcile = self.tick_count % RECONCILE_EVERY_TICKS == 1`).

At the end, not the start: a reconcile pass that hangs mid-tick must go dark rather than
report itself alive. Stamping first would make the one failure mode this detects —
a process that lives and stops finishing passes — write a fresh timestamp on its way into
the hang.

The value is JSON (`db.to_json`, read back with `db.from_json`, as `STUCK_RUN_KEY` is)
carrying BOTH the timestamp AND the cadence the writer was launched with:
`RECONCILE_EVERY_TICKS * self.poll_interval` seconds. The cadence is stamped by the writer
and never recomputed by the reader, because `poll_interval` is a launch argument — default
`5.0` in `Daemon.__init__` (`daemon.py:990`), overridden by `ops.start_os`
(`ops.py:134`, passed through as `--poll-interval` at `ops.py:192`) and by the CLI — and no
reader can see what the running daemon was started with. A reader that recomputed it from
its own default would judge a 60-second fleet by a 30-second expectation.

### 2b. Reader

`ops.os_status` derives ONE fleet-level attention item at read time. One item for the
fleet, never one per work order: the fact is about the daemon, and a per-row flag would
put the same sentence on every open order in every project. The shape is the existing
`dashboard errors` item in the same function — `project: "os"`, `wo_id: None`.

Two causes, one item:

* `stopped` — `daemon_running()` returned `None`. Carries
  `decide: jarvis start --catalog <catalog_path>`, the catalog read from central state
  (the key `_write_pidfile` already writes, and the same value `daemon.catalog` reports).
* `dark` — the pid is alive and the stamp is older than
  `max(DAEMON_DARK_MIN_SECONDS, interval * DAEMON_DARK_INTERVALS)`, with `interval` the
  cadence off the stamp. NO `decide`: no `jarvis` verb restarts a wedged daemon, and
  offering one would send the user at a command that cannot help. The reason names the
  pid and the dark minutes instead.

With no `reconcile_tick` stamp at all, darkness is measured from `daemon_started_at`. With
neither, the `dark` arm does NOT fire. That is `check_stuck_sweep_alive`'s own rule, in its
words: "NEVER SWEPT is only dark while the OS is UP" — a fresh install, or a fleet whose
first tick has not landed, must not report as broken.

## 3. What it must not do

1. No new arm in `invariants.true_blockers` (`invariants.py:1084`), and no new
   `invariants` checker. Both run on the reconcile tick, inside the process under
   question — §1's argument, and the reason this is read-time.
2. No per-work-order flag, and no change to `parked_reason`'s suppression. The
   suppression is right; what was missing is the check that its condition holds.
3. Nothing in `src/jarvis/health.py`. That module is pure and per-unit, writes no row and
   calls no model (its own docstring, `health.py:1-11`), and the DAEMON runs it — so it
   cannot observe its own death any more than a checker can. Neo's original rider naming
   `health.py` is SUPERSEDED by question 1960; said once, here, and not repeated.

## 4. The numbers, and why those

* `DAEMON_DARK_INTERVALS = 10`. The multiple is the mandate: the writer states its own
  cadence, so darkness is measured in missed reconcile passes rather than in wall clock.
  Ten missed passes is unambiguous — one slow pass, a catalog reload, a long project loop
  all cost one or two.
* `DAEMON_DARK_MIN_SECONDS = 300`. The floor, and it only ever binds below the mandate. A
  fleet launched with `--poll-interval 1` has a 6-second cadence, and 10 intervals there is
  60 seconds: without the floor the OS would cry at a minute of quiet on a fleet
  deliberately configured to tick fast. The shipped default (`poll_interval=5.0`,
  `RECONCILE_EVERY_TICKS = 6` at `daemon.py:135`) gives a 30-second cadence, so 10
  intervals is 300 seconds and the two numbers meet exactly there — the floor is the
  shipped mandate, not a second opinion about it.
* The stamp costs one `set_state` per reconcile beat: once per 30 seconds at the default,
  not once per tick and not once per project.
