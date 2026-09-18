"""jarvisd — the deterministic OS daemon.

One process, one poll loop over every project in the catalog. Per tick, in this order:
  0. reload the catalog if its file changed, so a `jarvis config set` reaches a running
     fleet — once, at the top, so the whole tick runs under one configuration
  1. route project notification outboxes to the central inbox, then to sinks
  2. reap finished worker turns and settle their work orders against what came back
  3. route queued envelopes to whoever fills the role they name (src/jarvis/bus.py),
     then deliver queued messages — the routed envelope among them — as the next
     turn of their conversation
  4. dispatch pending work orders (respecting per-project concurrency) — last, so it
     sees the concurrency slots steps 2 and 3 just freed
  5. let Neo (the OS answerer agent) drain queued worker questions
  5b. shorten over-long answered questions for the dashboard (src/jarvis/digest.py) —
     display only, on its own thread, and nothing the OS acts on depends on it

Every RETRY_EVERY_TICKS ticks it additionally:
  2b. relaunches the turns the TRANSPORT lost rather than the work — the account's usage
     window running out, or the API itself failing (a 500, a 529, a dropped connection).
     The OS's one self-healing loop, and the reason a work order that runs out of tokens
     at midnight is working again by morning, and one that catches a 500 is working
     again a minute later, without anyone retrying either by hand

Every PR_POLL_EVERY_TICKS ticks it additionally:
  6. asks GitHub what happened to the pull requests its work orders are parked behind,
     and ends the ones that were merged — the only step that leaves the machine, and
     the reason a merge does not need a `jarvis wo done` after it

Every RECONCILE_EVERY_TICKS ticks it additionally:
  7. tracks the sessions the user *injected* (`jarvis wo inject`) — the only step that
     still needs `claude agents --json`, since workers are headless and never enter that
     roster. Sessions Jarvis was not given are not looked at, let alone recorded
  8. checks the OS's own post-conditions (src/jarvis/invariants.py) and repairs the
     state that is unambiguously wrong — the only step that does not trust the others

The daemon is an orchestrator, never a doer: all actual work happens inside the worker
turns it launches (see worker_session.py, which owns how a turn is actually run).
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import bugreport, bus, claude_cli, db, fleet, inspection, worker_session
from .catalog import Catalog, ProjectSpec, load_catalog
from .central_store import CentralStore
from .dispatch import dispatch_work_order
from . import invariants as invariants_mod
from .invariants import PR_REPAIR_STATUSES
from .paths import daemon_pidfile, ensure_home, logs_dir
from .project_store import (
    FO_OPEN_STATUSES,
    FO_TERMINAL_STATUSES,
    OPEN_STATUSES,
    PRE_APPROVED_KEY,
    RETRY_SWEEP_STATUSES,
    TERMINAL_STATUSES,
    UNGOVERNED_ORIGINS,
    ProjectStore,
    resume_spends_slot,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .evidence import EvidencePacket

log = logging.getLogger("jarvisd")

RECONCILE_EVERY_TICKS = 6  # refresh `claude agents --json` every N ticks (injected only)
SECONDS_PER_HOUR = 3600    # a unit, not a setting
#: Ask GitHub about parked pull requests every N ticks — ~2 minutes at the default 5s
#: interval. Its own cadence rather than the reconcile one because it is the only step
#: that leaves the machine: one `gh` subprocess per parked work order per poll. Two
#: minutes is well inside what a user perceives as "it noticed my merge", and a fleet
#: with five PRs parked spends ~150 calls/hour against `gh`'s 5000/hour authenticated
#: limit. Not catalog-configurable on purpose: a knob nobody will tune is a knob that
#: only ever gets set wrong.
PR_POLL_EVERY_TICKS = 24

#: Which work orders' pull requests get asked about. EVERY STATUS WHERE A PULL REQUEST
#: CAN SIT WITH NOBODY MOVING IT — issue #224: `waiting_pr_merge` alone left a work order
#: that escalated into `needs_review` behind a red build completely unpolled, which is
#: precisely when the user is about to decide whether to merge it.
#:
#: THE SAME TUPLE `invariants.true_blockers` DERIVES THE REPAIR BLOCKERS FOR, aliased
#: rather than repeated: a status this polls and that does not derive raises a give-up
#: flag nothing can re-derive. See `invariants.PR_REPAIR_STATUSES` for that half.
#:
#: The in-flight statuses (`running`, `dispatching`, `validating`) are deliberately out.
#: Something already owns those — a live turn, or the round machine, which
#: `settle_work_order` refuses to touch for the same reason — and `complete_merged` on
#: one would end a work order out from under a worker that is still writing to it. They
#: also cannot SIT: whatever is driving them will settle them into a status that is here.
#:
#: `pending` is out too: an order that has not been dispatched has no pull request. So
#: are the terminal ones — a merged or cancelled work order's pull request is over.
PR_POLL_STATUSES = PR_REPAIR_STATUSES

#: Look for a work order the transport parked — the usage limit or a broken API — every
#: N ticks, which is ten seconds at the default 5s interval. Its own cadence rather than
#: the reconcile one, and a cheap one to run: the moment it may go again is already
#: decided (`worker_session.turn_pause`), so the pass compares it to the clock and
#: relaunches. No subprocess, no network, one indexed query per active work order.
#:
#: THIS USED TO BE 12 — a minute — which was sized for the usage limit alone, where a
#: minute of slop after a five-hour window is nothing. It cannot stay there now that the
#: shortest wait is itself a minute: a pass that runs every 60s turns a 60s backoff into
#: anything up to 120s, so the first and quickest step of the schedule would be the one
#: it distorted most. Ten seconds bounds the slop to a sixth of that step while still
#: costing a sixth of what checking every tick would.
RETRY_EVERY_TICKS = 2

#: Sweep for completed work orders whose code never reached the default branch every N
#: ticks — an hour at the default 5s interval. Its own cadence for `PR_POLL_EVERY_TICKS`'
#: reason one step removed: it does not leave the machine, but it is the only check that
#: shells out, at a `git` invocation per touched file of every completed order whose
#: verdict is not already cached. An hour is far inside the window that matters — the
#: orders GitHub issue #232 found had been stranded for SEVEN WEEKS — and a settled
#: verdict is recorded once and never recomputed, so a mature project's steady-state cost
#: is the orders nobody has landed yet, which is the number this exists to drive to zero.
#:
#: A MULTIPLE OF `RECONCILE_EVERY_TICKS` on purpose: the sweep runs inside
#: `check_invariants`, which only runs on the reconcile tick, so a cadence that did not
#: line up would silently sweep at some beat frequency of the two. 720 % 6 == 0, and both
#: fire on tick 721.
LANDING_SWEEP_EVERY_TICKS = 720

#: Look at the scheduler's clock every N ticks — a minute at the default 5s interval. Its
#: own cadence because it is the cheapest pass in the daemon and the one whose lateness
#: matters least: the shortest interval a job can declare is an hour, so a minute of slop
#: is noise, and running it every tick would be 12x the reads for no difference anybody
#: could observe. One indexed `scheduled_jobs` read per enabled job per pass, and NOTHING
#: AT ALL for a project that has not switched the scheduler on, which is every project by
#: default (`catalog.ScheduleConfig`).
SCHEDULE_EVERY_TICKS = 12

#: Walk the transcript tree for one-hour cache writes every N ticks — six hours at the
#: default 5s interval. ITS OWN CADENCE BECAUSE IT IS BY FAR THE DEAREST READ IN THE
#: DAEMON: a substring pass over every transcript on the machine (843MB and 4,584 files
#: here) plus a full parse of the few that match, measured at ~20s. That is 0.1% of a
#: six-hour period and 60x the whole reconcile tick, which is why it cannot live there.
#:
#: Six hours rather than daily because the condition is a SETTING going missing and the
#: window it is judged over is a week: four looks a day bounds how long a lost line runs
#: unreported without making the scan's cost noticeable. The dedupe is what stops four
#: looks becoming four alarms — `Daemon.check_cache_ttl` raises once per kind per window.
#:
#: Not catalog-configurable, for `PR_POLL_EVERY_TICKS`' reason: the THRESHOLDS are
#: settings because a project may genuinely want to hear sooner, but how often the OS
#: reads its own disk is not a decision anyone has information to make.
CACHE_TTL_EVERY_TICKS = 4320

#: WHICH tick of each period, and the only cadence here that is not `== 1`. Tick 1 is the
#: daemon's FIRST, when it is starting the fleet: a 20-second disk walk there delays every
#: dispatch behind it, and a daemon restarted more often than the period would pay that
#: cost on every boot and never reach a later tick to do the scan it skipped. Five minutes
#: in is past the start-up burst and still inside any session anybody is watching.
CACHE_TTL_TICK_OFFSET = 60

#: How many dashboard digests one batch may produce. Bounds the cost of the FIRST batch
#: on an instance upgrading into the feature with a backlog of long questions already in
#: `neo.db` — the rest are picked up on later ticks and render in full until then. It is
#: not a rate limit on steady state: questions long enough to earn a digest arrive at a
#: rate of a few a day.
DIGEST_BATCH = 5

#: THE VALIDATION SEAM. A validator is any callable of
#:
#:     (ProjectStore, the round row, the evidence packet) -> {
#:         "outcome": "passed" | "rejected" | "escalated",
#:         "reason":  str,   # <= 1500 chars, second person, addressed to the submitter;
#:                           # empty only when the outcome is "passed"
#:         "seats":   [{"seat", "status", "verdict", "model", "latency_ms", "reply"}, …],
#:     }
#:
#: and NOTHING else is known about it here — not that it calls a model, not that it has
#: seats, not that it exists at all when `_validator` says None. That is what lets the
#: panel be built, replaced or switched off without this module changing.
Validator = Callable[[ProjectStore, dict, "EvidencePacket"], dict]

#: THE REJECTION, WORDED ONCE. Nothing else may reformat it: a submitter that is told
#: what to do differently every round learns to read past the words, and the two
#: sentences at the end are the whole contract — resubmit through `finish`, and bring
#: something new when you do.
#:
#: `bus.render` frames this the way it frames every payload, because the bus is payload
#: agnostic by design and carries neither the work order id nor `max_rounds` (Neo,
#: question 136). The framing is the envelope's; the words below are the reviewer's.
REVIEW_FEEDBACK = """REVIEW FEEDBACK (round {n} of {max})
{reason}

Address this and then run `jarvis wo finish {wo_id} --summary "..." --evidence "..."`
again. Re-submitting without changed code or new evidence will end the review."""

#: THE SAME REJECTION, ADDRESSED TO A DIFFERENT JOB. A manager does not fix code — it
#: files work orders that do — so the closing instruction cannot be the implementor's, and
#: a manager that read "address this" would go and edit the repository itself.
#:
#: The feature order id IS in the text, unlike the work-order wording where it is
#: incidental: `jarvis fo submit` takes it as an argument, and the manager owns one
#: feature but may be holding several messages about it.
FEATURE_REVIEW_FEEDBACK = """REVIEW FEEDBACK ON THE FEATURE (round {n} of {max})
{reason}

Decide what actually has to change, file a work order under {fo_id} for each thing that
does (`jarvis wo create <project> "..." --parent {fo_id}`), and once they have landed run
`jarvis fo submit {fo_id} --summary "..." --evidence "..."`. Re-submitting without changed
code or new evidence will end the review."""

#: How many transport outages in a row one round survives before the OS gives up on it.
#: An outage is not a verdict, so it consumes no round — but retrying for ever would
#: park a work order in `validating` with nobody watching, which is the exact silent
#: stall this feature exists to remove.
VALIDATION_OUTAGE_LIMIT = 3

#: Why a round closed with no verdict at all. Deliberately NOT phrased as a rejection:
#: nothing judged this work, so there is nothing for its author to fix, and the reason
#: has to say so plainly wherever it is read back (Neo, question 137).
NO_VALIDATOR_REASON = (
    "no validator was configured, so this round was never judged — the work order "
    "settled exactly where it settles with validation switched off"
)

#: The give-up notification, for both round machines (issue #199). ONE shape for both,
#: because a unit that gives up says the same thing to the user whichever machine gave
#: up on it. The body is the round's reason, CUT to `VALIDATION_REASON_CHARS`.
VALIDATION_ESCALATED_TITLE = "{unit} — the review gave up in round {n}"

#: How much of the reason the body carries. A DISPLAY choice and not a schema limit —
#: `notifications.body` is TEXT and would take the whole of it — so raising it is safe
#: and is a decision about what a Telegram push should be, not about the store. A panel
#: reason runs to paragraphs; this row exists to say enough to decide whether to look,
#: and `jarvis wo show` / `jarvis validation show` hold the untruncated text.
VALIDATION_REASON_CHARS = 500

#: Appended when the cut above actually bit, so a reader can tell a short reason from a
#: clipped one. Without it a sentence simply stops and the truncation reads as the
#: machine having nothing more to say.
VALIDATION_REASON_CUT = " […]"


#: What the inbox row for an aggregate re-write-tax alarm says. Up here rather than at
#: its call site because every inbox row reaches every sink, Telegram included, and
#: `remedies`' and `supervisor`'s titles live at the top of their modules for that reason.
#: ONE PER CAUSE, because the inbox is the durable half of this alarm and two rows
#: reading identically merge exactly the two failures this whole change exists to keep
#: apart — a title that says only "the tax" sends the reader to the wrong cure.
REWRITE_INBOX_TITLE = {
    inspection.REWRITE_PREFIX_ALARM:
        "{project} is paying to re-send conversations whose prompt PREFIX moved",
    inspection.REWRITE_TTL_ALARM:
        "{project} is paying to re-send conversations whose cache entry EXPIRED",
}

#: The same, for the ONE-HOUR CACHE pair. ITS OWN DICT AND NOT A THIRD AND FOURTH ENTRY
#: ABOVE: a dict named for one alarm family is a thing a test reads WHOLE — the sibling's
#: does, asserting its two titles are exactly the two inbox rows a raise produced — so
#: adding to it silently changes what an existing assertion means. One family, one dict.
#:
#: ONE PER CAUSE for `REWRITE_INBOX_TITLE`'s reason, though here the two name different
#: CULPRITS rather than different cures. A row saying only "the fleet is buying the
#: one-hour cache" sends the reader to the OS when the answer is their own settings file,
#: or to their settings file when the answer is a bug in here.
CACHE_1H_INBOX_TITLE = {
    inspection.CACHE_1H_DISPATCHED_ALARM:
        "Jarvis's OWN turns are buying the one-hour cache write — a transport defect",
    inspection.CACHE_1H_FOREIGN_ALARM:
        "Hand-opened Claude sessions are buying the one-hour cache write",
}


def escalation_body(reason: str) -> str:
    """The reason as a notification body — see `VALIDATION_REASON_CHARS`."""
    if len(reason) <= VALIDATION_REASON_CHARS:
        return reason
    return reason[:VALIDATION_REASON_CHARS] + VALIDATION_REASON_CUT


class Daemon:
    def __init__(self, catalog: Catalog, poll_interval: float = 5.0):
        self.catalog = catalog
        self.poll_interval = poll_interval
        self.central = CentralStore()
        self.stores: dict[str, ProjectStore] = {}
        self.stop_requested = False
        self.tick_count = 0
        # Neo drains its queue on ONE thread: answering in FIFO order back-to-back
        # keeps the shared persona+learnings prefix inside the prompt-cache TTL.
        self.neo_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="neo")
        self.neo_draining = False
        # Dashboard digests run on their OWN thread, not Neo's. They are display work on
        # questions that have already been answered, so they must never delay a worker
        # parked waiting for an answer — and they use a different model and a different
        # system prompt, so interleaving them into the drain would cost Neo its warm
        # prefix on every other call.
        self.digest_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="digest")
        self.digesting = False
        # The supervisor gets its OWN thread, and NOT Neo's. The panel's seats already
        # run inside the single Neo thread, so the whole question FIFO waits on the
        # slowest of them; adding an alarm review there would make a slow supervisor
        # delay every worker parked on a question. Not the tick thread either — a model
        # call inside the per-project loop stalls the tick for the entire fleet.
        self.supervisor_pool = ThreadPoolExecutor(max_workers=1,
                                                  thread_name_prefix="supervisor")
        self.supervisor_draining = False
        # THE SWEEP GETS ITS OWN POOL AND ITS OWN GUARD, and not the supervisor's — §4
        # of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md. The
        # supervisor is event-driven off a threshold and answers a turn that is burning
        # money right now; a fleet sweep queued in front of it would delay the thing the
        # whole mechanism was built for. The tick thread and Neo's are out for the same
        # reasons they are out for the supervisor.
        self.health_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="health")
        self.health_sweeping = False
        # Applying a remedy shares the SUPERVISOR's pool rather than taking a third —
        # §5. It is not a model call and it must never overlap the review that proposed
        # it, so one thread for both is the guarantee, not a saving.
        self.remedy_applying = False
        # Validation runs off the tick thread for the same reason Neo does, only more
        # so: a round is up to five headless calls at a 300s timeout each, and run
        # inline it would freeze every project in the catalog behind one work order's
        # review. One worker, because the point is to keep the tick moving rather than
        # to validate a fleet at once.
        self.validate_pool = ThreadPoolExecutor(max_workers=1,
                                                thread_name_prefix="validate")
        # Work orders whose round is in flight. Per work order rather than one global
        # flag: two units may legitimately be under review at once, and a second tick
        # must not start a second validation of the SAME one.
        self.validating: set[str] = set()
        # The validator seam (see `_validator`). None means "ask the catalog"; tests
        # inject a callable here, exactly as `release_runner` injects systemd.
        self.validator: Validator | None = None
        # Invariant violations already reported this run, so a standing problem is
        # surfaced once instead of every tick. Keyed by (invariant, wo_id).
        self.reported_violations: set[tuple[str, str | None]] = set()
        # Projects already warned that their pull requests cannot be polled (no `gh`, no
        # credentials, an unreachable host). Same idea: say it once, not every 2 minutes
        # forever. Reset by restarting the daemon, which is also what fixes it.
        self.pr_poll_warned: set[str] = set()
        self.issue_sync_warned: set[str] = set()
        # The systemd seam for staged releases (src/jarvis/release.py). None means the
        # real thing; tests inject a fake so no test can ever touch real systemctl.
        self.release_runner: Any = None
        # What the catalog file looked like when this catalog was loaded, SEEDED HERE so
        # the first tick over an untouched file reloads nothing and cannot undo an
        # in-memory edit. See `reload_catalog`.
        self._catalog_stamp = self._catalog_file_stamp()
        # Reload refused (unreadable, unparseable, or the project roster moved): say so
        # once per daemon run, not once per tick. Same rule as `pr_poll_warned`.
        self.catalog_reload_warned = False

    # -- lifecycle -----------------------------------------------------------

    def store_for(self, project: ProjectSpec) -> ProjectStore:
        if project.name not in self.stores:
            self.stores[project.name] = ProjectStore(project.path)
        return self.stores[project.name]

    def _fleet_stores(self) -> dict[str, ProjectStore]:
        """Every live project's store, for a question about the account (`fleet.read`).

        Built from the catalog rather than handing over `self.stores`, which holds only
        the projects some earlier tick happened to open — on the first tick after a
        restart that is none of them, and an account-wide hold that a restart clears is
        not a hold.
        """
        return {p.name: self.store_for(p)
                for p in self.catalog.projects if p.path.is_dir()}

    # -- configuration reload ---------------------------------------------------

    def _catalog_file_stamp(self) -> tuple[int, str] | None:
        """`(mtime_ns, sha256)` of the catalog file, or None when there is nothing on
        disk to watch."""
        path = self.catalog.source_path
        if path is None:
            return None
        try:
            return (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError:
            return None

    def reload_catalog(self) -> bool:
        """Pick up a config change without a restart, once per tick. Spec §4.

        `self.catalog` is REPLACED here and nowhere else, which is what makes it stable
        for the whole tick and safe to keep as a plain attribute: a pool thread holding
        a `ValidationConfig` for the length of a round cannot have it swapped mid-round
        (§4.1). Never turn it into a re-reading property.

        The mtime is what an unchanged file costs every tick; the hash is what stops a
        file that was touched but not changed from replacing the object anyway.

        Settings only. Everything else — a bad file, a moved project roster — keeps the
        last good catalog and says so once.
        """
        path = self.catalog.source_path
        if path is None:
            return False
        try:
            mtime = path.stat().st_mtime_ns
        except OSError as e:
            self._refuse_reload(f"cannot read the catalog at {path}: {e}")
            return False
        if self._catalog_stamp is not None and mtime == self._catalog_stamp[0]:
            return False
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as e:
            self._refuse_reload(f"cannot read the catalog at {path}: {e}")
            return False
        seen, self._catalog_stamp = self._catalog_stamp, (mtime, digest)
        if seen is not None and digest == seen[1]:
            return False
        try:
            fresh = load_catalog(path)
        except Exception as e:  # noqa: BLE001 — one bad file must not stop the fleet
            self._refuse_reload(
                f"{e}\n\nThe fleet is still running the configuration it started with. "
                f"Fix {path} and it will be picked up on the next tick.")
            return False
        if {p.name for p in fresh.projects} != {p.name for p in self.catalog.projects}:
            self._refuse_reload(
                f"{path} added or removed a project, and that is not a setting: a new "
                f"project has never been through `bootstrap_project`, and a removed one "
                f"leaves an open store behind. Run `jarvis start` to apply it. Setting "
                f"changes in the same file are not applied either until you do.")
            return False
        self.catalog = fresh
        self.catalog_reload_warned = False
        log.info("catalog reloaded from %s", path)
        return True

    def rebase_config_for_release(self) -> dict[str, Any] | None:
        """Re-resolve the head version under the running build, once, at daemon start.

        `resolved_json` is materialised at write time (§2), so a release that moves a
        shipped default leaves the head row describing a configuration nobody is running
        any more. This is what makes the ledger "every change to what the fleet actually
        runs" rather than "changes the user made" (§6.1): without it an upgrade is a
        behaviour change with no row.

        The document is unchanged — only its resolution moved — so the row is addressed
        by document AND build; `CentralStore.add_config_version` does that off
        `actor="release"` (Neo, question 181).

        Returns the row it wrote, or None when nothing moved.
        """
        from . import bugreport, config_version
        from .catalog import CatalogError, parse_catalog

        head = self.central.head_config_version()
        if head is None:
            return None
        build = bugreport.jarvis_version()
        try:
            resolved = config_version.resolve(parse_catalog(head["document"]))
        except CatalogError as e:
            # A historical document this build can no longer parse. Nothing to compare
            # it against, and refusing to boot over it would be worse than a stale row.
            log.warning("config rebase skipped: head %s does not parse under %s (%s)",
                        head["id"], build, e)
            return None
        moved = config_version.diff(head["resolved"], resolved)
        if not moved:
            return None
        shown = "; ".join(f"{c['path']} {c['old']!r} → {c['new']!r}" for c in moved[:5])
        more = f" (and {len(moved) - 5} more)" if len(moved) > 5 else ""
        row = self.central.add_config_version(
            head["document"], resolved, actor="release",
            reason=f"upgrade {head['schema_version']} → {build}: {shown}{more}",
            changes=moved, source_path=head["source_path"], schema_version=build)
        log.info("config rebased for %s: %d default(s) moved (%s)",
                 build, len(moved), row["id"])
        return row

    def _refuse_reload(self, detail: str) -> None:
        """Once per daemon run, not once per tick — the `pr_poll_warned` rule: a file
        that stays broken is broken on every tick, and an inbox item every five seconds
        is how an inbox stops being read."""
        log.warning("catalog reload refused: %s", detail)
        if self.catalog_reload_warned:
            return
        self.catalog_reload_warned = True
        self.central.add_inbox(
            project="os", level="warning",
            title="a catalog change was NOT applied", body=detail)

    def run_forever(self) -> None:
        ensure_home()
        self._write_pidfile()
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        log.info("jarvisd started (pid=%s, projects=%s)",
                 os.getpid(), [p.name for p in self.catalog.projects])
        # Before the first tick, and before anything reads a version: this boot may be
        # the first under a new build, and the head row may no longer describe what it
        # resolves to here (§6.1).
        self.rebase_config_for_release()
        # Before the first tick: if the boot we are living through IS a staged
        # release's restart, prove the release applied and settle its work order —
        # otherwise the reconciler reaps the dead shipping turn first and files the
        # very "failed — review and retry" this flow exists to prevent.
        self.verify_pending_release()
        try:
            while not self.stop_requested:
                started = time.monotonic()
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 — the loop must survive anything
                    log.exception("tick failed")
                elapsed = time.monotonic() - started
                time.sleep(max(0.2, self.poll_interval - elapsed))
        finally:
            self.neo_pool.shutdown(wait=False)
            self.digest_pool.shutdown(wait=False)
            self._remove_pidfile()
            log.info("jarvisd stopped")

    def _on_signal(self, signum: int, frame: object) -> None:
        log.info("received signal %s, shutting down", signum)
        self.stop_requested = True

    def _write_pidfile(self) -> None:
        daemon_pidfile().write_text(str(os.getpid()))
        self.central.set_state("daemon_pid", str(os.getpid()))
        self.central.set_state("daemon_started_at", str(time.time()))
        if self.catalog.source_path:
            self.central.set_state("catalog_path", str(self.catalog.source_path))

    def _remove_pidfile(self) -> None:
        daemon_pidfile().unlink(missing_ok=True)

    # -- main tick -------------------------------------------------------------

    def tick(self) -> None:
        # First, so everything below runs under one configuration — and the same one.
        self.reload_catalog()
        self.tick_count += 1
        reconcile = self.tick_count % RECONCILE_EVERY_TICKS == 1
        poll_prs = self.tick_count % PR_POLL_EVERY_TICKS == 1
        retry_paused = self.tick_count % RETRY_EVERY_TICKS == 1
        sweep_landings = self.tick_count % LANDING_SWEEP_EVERY_TICKS == 1
        run_schedule = self.tick_count % SCHEDULE_EVERY_TICKS == 1
        scan_cache_ttl = \
            self.tick_count % CACHE_TTL_EVERY_TICKS == CACHE_TTL_TICK_OFFSET
        # `None` means "the roster was not read this tick" — either nothing is injected
        # or the listing failed — and is NOT the same as an empty roster, which would
        # mean every injected session ended. Session tracking is skipped on None.
        sessions_by_project: dict[str, list[claude_cli.BgSession]] | None = None
        # The ACCOUNT's state, read ONCE for the whole tick and then spent down as turns
        # launch. Outside the project loop because it is not a project's fact: a fleet
        # count re-read per project would let each project's dispatch pass believe the
        # previous one's launches had not happened, which is how a cap of 3 lets a
        # ten-project fleet start thirty turns.
        #
        # Turns are settled INSIDE the loop, below, so this count is a tick old for the
        # projects settled after the first. It can therefore hold a slot for a turn that
        # has just ended, never release one for a turn that is still running — the safe
        # direction, and self-correcting one tick later.
        state = fleet.read(self.catalog.os.max_in_flight, self._fleet_stores())
        if fleet.announce(self.central, state):
            log.warning("fleet held: %s", state.blocked())
        # The roster is a subprocess, and tracking injected sessions is the only thing
        # left that reads it. With nothing injected there is nothing to track, so the
        # common case — a project driven entirely by dispatched work orders — pays
        # nothing for it. The invariants below run either way: they read the OS's own
        # databases, so a Claude CLI that is missing or broken must not switch off the
        # OS's self-check.
        if reconcile and self._tracking_injected_sessions():
            try:
                sessions_by_project = {}
                for s in claude_cli.list_background_sessions():
                    sessions_by_project.setdefault(s.cwd, []).append(s)
            except claude_cli.ClaudeCliError as e:
                log.warning("agents listing failed: %s", e)
                sessions_by_project = None

        for project in self.catalog.projects:
            if not project.path.is_dir():
                continue
            store = self.store_for(project)
            try:
                self.route_outbox(project, store)
                # Turns are Jarvis's own processes, so reaping them costs a signal and a
                # file read — cheap enough to run every tick rather than on the reconcile
                # cadence. That is what makes a finished turn visible within one poll
                # interval instead of one reconcile interval, and it is why delivery no
                # longer has to wait for a roster refresh either.
                self.settle_turns(project, store)
                # Before delivery, not after: a work order whose window has just
                # reopened must re-send the turn it was refused BEFORE any newer
                # message goes out, or the user's earlier message — already marked
                # delivered, and living only on that turn row — would be skipped.
                if retry_paused:
                    self.retry_paused_turns(project, store, state)
                # Before delivery, not after: an envelope BECOMES a queued message,
                # so routing it first lets it go out as this tick's turn instead of
                # waiting a whole poll interval for the next pass. With no envelope ever
                # posted this is one indexed lookup that finds nothing.
                self.deliver_envelopes(project, store)
                self.deliver_messages(project, store)
                # After delivery, not before: an envelope this round machine posted on
                # an earlier tick is already on its way to the worker, so the work order
                # it rejected has left `validating` and is not looked at again here.
                self.validation_tick(project, store)
                # Beside its twin and for the same reason: an envelope a feature round
                # posted on an earlier tick has already gone out to the manager above,
                # so the feature it rejected has left `validating` and is not looked at
                # again here. Both machines share one thread and one in-flight set.
                self.feature_validation_tick(project, store)
                # Before dispatch, not after: a planner filed this tick is an ordinary
                # pending work order, so it is claimed by the same pass rather than
                # waiting a whole poll interval to start.
                self.plan_features(project, store)
                # Before dispatch for the planner's reason one step removed: an order the
                # scheduler files this tick is an ordinary pending work order, so the
                # same pass claims it instead of leaving it a whole poll interval late.
                if run_schedule:
                    self.schedule_tick(project, store)
                # Its own cadence, and OUTSIDE the reconcile block: a ~20s transcript
                # walk (`CACHE_TTL_EVERY_TICKS`) has no business on a 30-second beat.
                # Only the OS-owning project does any work here — see `check_cache_ttl`.
                if scan_cache_ttl:
                    self.check_cache_ttl(project, store)
                self.dispatch_pending(project, store, state)
                if poll_prs:
                    self.poll_pull_requests(project, store)
                    # AFTER the poll, in the same tick: a merge that just completed a
                    # work order is what closes its issue, and making the tracker wait
                    # a second poll interval for news the OS already has is the gap
                    # issue #240 is about, one step smaller.
                    self.sync_issues(project, store)
                # After the pull-request poll, so the merge that completes a feature's
                # last child settles the feature in the same tick rather than the next
                # one — but outside the `if`, because a child can also finish without
                # ever opening a pull request.
                self.settle_features(project, store)
                if reconcile:
                    # The agents roster holds ONLY the user's own sessions: workers are
                    # headless and never enter it. Jarvis looks at the ones it was
                    # handed and no others.
                    if sessions_by_project is not None:
                        self.track_injected_sessions(project, store, sessions_by_project)
                    # After settlement, so an order that finished this tick is billed on
                    # this tick — the evidence a bill is built from starts expiring the
                    # moment the work stops.
                    self.seal_bills(project, store)
                    # Before the invariants, because it is a fact about work that is
                    # still RUNNING rather than about state that has settled.
                    self.check_burning_turns(project, store)
                    # AFTER `seal_bills`, never before: it reads the sealed bills, so an
                    # order that settled on this tick is in this tick's window rather
                    # than a reconcile interval late.
                    self.check_rewrite_tax(project, store)
                    # Also before them: this is the only thing that ever closes a gate
                    # request no reviewer can see, so leaving it until after would let
                    # the invariants judge a hold the OS was about to refuse.
                    self.abandon_unargued_gates(project, store)
                    # On the reconcile cadence rather than every tick, and that is the
                    # right one: this asks a model about a work order that is waiting on
                    # a person, so the thing it saves is measured in the user's hours.
                    # It only ASKS — the ruling lands through the Neo drain below.
                    self.auto_review(project, store)
                    # Last: check the state everything above just produced.
                    self.check_invariants(project, store, sweep_landings=sweep_landings)
                self.central.touch_project(project.name)
            except Exception:  # noqa: BLE001
                log.exception("project %s tick failed", project.name)

        # After the project loop, so `running_turns` reflects the turns settlement just
        # reaped: the staged-release restart must only fire once the shipping worker's
        # turn has genuinely ended (src/jarvis/release.py).
        self.release_tick()

        self.neo_tick()
        # After the drain is kicked, never before: a question digested this tick is one
        # whose answer has already landed, and the drain is what lands it.
        self.digest_tick()
        # After the project loop, so an alarm `check_burning_turns` raised on this tick is
        # judged on this tick rather than one reconcile interval later — and OUTSIDE it,
        # so no project waits on another project's review.
        self.supervisor_tick()
        # AFTER the supervisor's kick, never before: both run off their own pools, and
        # the order here is the order the two get their turn at the catalog. A sweep
        # that raised on this tick is judged on the next one, which is the cadence §4
        # designs for — the sweep answers "is something wrong", the review answers
        # "does the user need to know".
        self.health_tick()
        # Last of the three, and never merged into the review: a proposal filed above
        # cannot be applied on the tick that filed it (its gate is `pending`), so the two
        # only ever meet across ticks — and that separation is what keeps the deciding
        # half free of the acting half. See spec 2026-09-02, §5.
        self.remedy_tick()

        # Before routing: a dashboard failure raised here goes out with this tick's
        # notifications instead of waiting for the next one.
        try:
            self.check_ui_log()
        except Exception:  # noqa: BLE001 — never let the UI watch stall the tick
            log.exception("ui log check failed")

        from .notify import route_new_inbox
        route_new_inbox(self.central, self.catalog)

    def seal_bills(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Freeze the bill of every order that has settled and has none yet.

        A bill is built from Claude Code's session transcripts and the result JSONs the
        CLI writes, and Claude Code prunes both on its own schedule. An order costed on
        demand therefore gets CHEAPER the longer you leave it — not because it spent
        less but because the evidence went away. Sealing at completion is what makes the
        figure survive; doing it here rather than at each of the six places an order can
        settle is what makes it impossible to forget one.

        A few per tick: the first run after this ships meets every order the project has
        ever completed, and a bill reads files off disk. The backlog drains within the
        hour and nothing else waits on it.
        """
        from . import bill as bill_mod, db, usage

        pending = store.unsealed_terminal_orders()
        features = store.unsealed_terminal_features()
        if not pending and not features:
            return
        # One index of Claude Code's transcript tree for the whole batch: it walks every
        # project directory there is, and five orders would otherwise walk it five times.
        index = usage.index_sessions()
        for order in pending:
            try:
                bill_mod.seal(project.name, project.path, order, index=index)
            except Exception:  # noqa: BLE001 — a bill must never stall the tick
                log.exception("sealing the bill for %s failed", order["id"])
                # Sealed EMPTY rather than left pending, so one order that cannot be
                # costed does not park itself at the head of the queue and block every
                # order behind it on every tick from now on.
                store.seal_bill(order["id"], db.to_json(
                    {"error": "this bill could not be computed when the order settled"}))
        for feature in features:
            try:
                bill_mod.seal(project.name, project.path, feature,
                              feature=True, index=index)
            except Exception:  # noqa: BLE001
                log.exception("sealing the bill for %s failed", feature["id"])
                store.seal_bill(feature["id"], db.to_json(
                    {"error": "this bill could not be computed when the order settled"}),
                    feature=True)

    # -- 4. dispatch -------------------------------------------------------------

    def dispatch_pending(self, project: ProjectSpec, store: ProjectStore,
                         state: fleet.Fleet | None = None) -> None:
        """Claim and launch what this project may run — under BOTH caps.

        `max_concurrent` rations the project; `state` rations the account, which is not
        divided among projects and is what ran out on 2026-09-02 (src/jarvis/fleet.py).
        Whichever is tighter binds, the same way the per-feature cap already sits beside
        the project one in `claim_next_pending`.

        The fleet check goes BEFORE the claim, not after: a claimed work order is already
        `dispatching`, and leaving one there with no turn behind it is the state
        `settle_work_order` fails as "worker turn never started". Held back, it never
        leaves `pending` and nothing is written at all — the same nothing a
        dependency-blocked order costs, and why neither raises attention.
        """
        while store.count_active() < project.max_concurrent:
            if state is not None and state.blocked():
                return
            wo = store.claim_next_pending()
            if wo is None:
                return
            log.info("[%s] dispatching %s: %s", project.name, wo["id"], wo["title"])
            try:
                dispatch_work_order(
                    store, self.central, project, wo, os_config=self.catalog.os,
                )
            except claude_cli.ClaudeCliError as e:
                log.error("[%s] dispatch of %s failed: %s", project.name, wo["id"], e)
                continue
            if state is not None:
                state.launched()

    # -- staged releases (thin hooks; all logic in src/jarvis/release.py) --------------

    def _release_store(self, project_name: str) -> ProjectStore | None:
        """The marker names its project; this is how release.py reaches its store."""
        for p in self.catalog.projects:
            if p.name == project_name and p.path.is_dir():
                return self.store_for(p)
        return None

    def verify_pending_release(self) -> None:
        from . import release

        try:
            release.verify_on_boot(self._release_store, runner=self.release_runner)
        except Exception:  # noqa: BLE001 — a broken marker must not stop the daemon
            log.exception("release boot verification failed")

    def release_tick(self) -> None:
        from . import release

        try:
            release.maybe_restart(self._release_store, runner=self.release_runner)
        except Exception:  # noqa: BLE001
            log.exception("release restart check failed")

    # -- 4a. feature orders: open a planner ------------------------------------------

    def plan_features(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Give every unplanned feature order its planner.

        The daemon does not fan out here and never will: it creates exactly ONE child
        work order, briefed as the planning lead. Everything the OS already does for a
        worker then applies to the planner for free — the headless transport, the
        worktree, `jarvis wo ask`, assumptions, the gate, stall detection, the timeline,
        cancellation — which is the whole reason planning is a work order rather than a
        pipeline inside this process.

        Idempotent by status, not by a flag: the feature order leaves `pending` in the
        same call that files the planner, so a tick that crashes between the two leaves
        the feature order `pending` and simply files it again next time. The opposite
        ordering would strand a feature order in `planning` with no planner.
        """
        for fo in store.list_feature_orders(statuses=("pending",)):
            try:
                wo = store.create_work_order(
                    title=f"Plan: {fo['title']}"[:200],
                    # The ask verbatim. The planner CONTRACT is composed at dispatch
                    # (dispatch._planner_prompt); what lives on the record is what the
                    # user actually asked for, so the description reads the same in
                    # `jarvis wo show` as it does in `jarvis fo show`.
                    description=fo["description"],
                    origin="jarvis", kind="planner", parent_id=fo["id"],
                )
            except Exception:  # noqa: BLE001 — one bad feature order must not stop the rest
                log.exception("[%s] could not open a planner for %s", project.name,
                              fo["id"])
                continue
            store.update_feature_order(fo["id"], plan_wo_id=wo["id"])
            store.set_feature_status(fo["id"], "planning")
            log.info("[%s] planning %s: opened %s", project.name, fo["id"], wo["id"])

    def settle_features(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Close out feature orders whose children have all landed, or one of which has
        not.

        Only `executing` feature orders are looked at, and that single fact is what makes
        "flag once, at feature level" true by construction rather than by bookkeeping: a
        feature that fails leaves `executing` in the same call that raises its flag, so
        the next tick does not see it and cannot raise it again. No `already_reported`
        set, no dedupe key.

        The rules, decided 2026-08-03:

        * **`completed` when every child is `completed`.** `waiting_pr_merge` does not
          count — the same strict rule Phase 1 shipped for dependency edges, and for the
          same reason: a feature is done when its code is on the default branch, not when
          it is sitting on branches. The merge poller closes each child a couple of
          minutes after the user merges, so this costs nobody a step. With validation
          enabled that ending becomes `validating` instead, and `completed` is reached
          from the panel's pass — see `_route_to_validation`. Feature validation happens
          HERE, after the merges, precisely so the diff it judges is real merged code.
        * **`failed` when ANY child is `failed` or `cancelled`.** Deliberately without
          the design's "and the remainder cannot proceed" qualifier: a feature with a
          dead child needs a human whichever siblings could still run, so the
          reachability check buys nothing and is easy to get subtly wrong. A cancelled
          child counts too — it did not settle successfully, so `completed` would be a
          lie — but the reason says cancellation rather than failure, because the two ask
          the user for different things.
        * **A SUPERSEDED child counts for neither rule.** `failed` is a settled status
          that nothing here re-derives — only `executing` features are looked at — so a
          feature stayed failed even after the child that killed it recovered. The two
          ways back are `INV-FEATURE-FALSE-FAILURE`, which reopens a feature with no dead
          children left, and `ops.resume_feature_order`, which is the user answering for
          one that is still dead. See docs/superpowers/specs/2026-08-29-feature-order-resume.md.

        No notification is raised here, on purpose. A failed child has already pinged the
        user through `settle_work_order`, and `notify.route_new_inbox` applies no level
        filter — every inbox row reaches every sink — so a second row would be the same
        event arriving on the phone twice. The feature-level flag is what the user finds
        when they follow the first one.
        """
        from .invariants import (
            FEATURE_CHILD_CANCELLED,
            FEATURE_CHILD_FAILED,
            dead_feature_children,
        )

        for fo in store.list_feature_orders(statuses=("executing",)):
            children = store.feature_children(fo["id"])
            if not children:
                continue  # released with nothing in it; nothing to settle against
            # A superseded child settles its feature NEITHER WAY. `jarvis fo resume` is
            # the user saying "I have answered for this one" — so it stops failing the
            # feature, and it equally stops counting towards completion, or the feature
            # would sit in `executing` for ever waiting on a child that will never move.
            # A feature whose every child is superseded therefore completes, which is
            # right: nothing is outstanding.
            live = [c for c in children if not c["superseded"]]
            dead = dead_feature_children(children)
            if dead:
                first = dead[0]
                template = (FEATURE_CHILD_FAILED if first["status"] == "failed"
                            else FEATURE_CHILD_CANCELLED)
                reason = template.format(id=first["id"])
                store.set_feature_status(fo["id"], "failed")
                store.flag_feature_attention(fo["id"], reason)
                self._close_feature_manager(store, fo["id"])
                log.info("[%s] feature %s failed: %s", project.name, fo["id"], reason)
            elif all(c["status"] == "completed" for c in live):
                if self._route_to_validation(project, store, fo):
                    continue
                self._complete_feature(store, fo)
                log.info("[%s] feature %s completed (%d work orders)", project.name,
                         fo["id"], len(children))

    def _route_to_validation(self, project: ProjectSpec, store: ProjectStore,
                             fo: dict) -> bool:
        """Should this finished feature go to the panel instead of to `completed`?

        True means it has been dealt with and `settle_features` must leave it alone —
        EITHER because round 1 was just opened over the integrated diff, OR because a
        round has already been judged and the feature is waiting on its manager to
        resubmit. Those are one answer because they are the same fact: from the moment a
        feature has a round, "every child is completed" stops being what settles it.

        THE SECOND CASE IS WHAT MAKES THE LOOP TERMINATE. A rejection sends the feature
        back to `executing` so the manager's remediation children can be dispatched, and
        its children are all `completed` again the instant they land — so a version of
        this that only asked "are the children done?" would re-open a round on the very
        next tick with the identical fingerprint, escalate on the repeat, and cut the
        manager out of its own loop. Resubmission is the manager's act (`jarvis fo
        submit`), never the reconciler's.

        Both switches are read here rather than in the round machine, for the reason the
        whole design turns on: `enabled` gates OPENING a round and never settling one, so
        a user who turns the panel off at three in the morning drains what is open and
        strands nothing (see `validation_tick`). `feature_units` is the same switch one
        level down — off, work orders still validate and features settle exactly as they
        do today.
        """
        from . import ops

        cfg = project.validation
        if not (cfg.enabled and cfg.feature_units):
            return False
        if store.validation_rounds(fo_id=fo["id"]):
            return True  # judged once already; the manager owns the next submission
        try:
            round_row = ops.submit_feature_for_validation(
                store, project.path, fo, declared="", summary="", cfg=cfg)
        except Exception:  # noqa: BLE001 — one feature must not cost the tick the rest
            log.exception("[%s] could not open a validation round for %s",
                          project.name, fo["id"])
            return False
        log.info("[%s] feature %s -> validating (round %d)", project.name, fo["id"],
                 round_row["round"])
        return True

    def _complete_feature(self, store: ProjectStore, fo: dict) -> None:
        """The one place a feature order ends successfully.

        Two callers now — `settle_features` with validation off, and the round machine on
        a pass — and they must land a feature in exactly the same state or the day
        validation is enabled becomes the day backlog items stop closing. Same argument
        as `ops.land_finished` makes for a work order, one level up.
        """
        store.set_feature_status(fo["id"], "completed")
        store.clear_feature_attention(fo["id"])
        self._close_feature_manager(store, fo["id"])
        self._close_feature_backlog(fo)

    def _close_feature_manager(self, store: ProjectStore, fo_id: str) -> None:
        """End the project manager order when its feature ends, whichever way it ended.

        The manager is not a child (`feature_children` filters to `kind='worker'`), which
        is what keeps it from deadlocking the completion just decided above — and the
        price of that exemption is that nothing else would ever close it. Left alone it
        would sit in `waiting_input` for ever against a settled feature: an open work
        order on every listing, and a live addressee for envelopes about work that is
        over.

        `completed`, not `cancelled`, on both paths. The manager did its job whether or
        not the feature delivered; a `cancelled` manager under a `failed` feature would
        read as a second thing gone wrong. Nothing is flagged either way — the feature
        order carries the flag, and a duplicate on the manager would ask the user to look
        at the same event twice.
        """
        manager = store.manager_work_order(fo_id)
        if not manager or manager["status"] not in OPEN_STATUSES:
            return
        store.set_status(manager["id"], "completed")
        store.clear_attention(manager["id"])
        store.add_event(manager["id"], "feature_settled", {"feature_order": fo_id})

    def _close_feature_backlog(self, fo: dict) -> None:
        """A feature order promoted from the backlog closes its item when it lands.

        The same courtesy `ops.mark_backlog_done` does for a work order, and it has to be
        here rather than there because a feature order has no `finish` — nobody reports
        its result; it is derived from its children.
        """
        if not fo.get("backlog_id"):
            return
        try:
            self.central.mark_backlog(fo["backlog_id"], "done")
        except Exception:  # noqa: BLE001 — a stale backlog id must not stop the settle
            log.exception("could not close backlog item %s", fo["backlog_id"])

    # -- 0. the dashboard ----------------------------------------------------------

    def check_ui_log(self) -> int:
        """Raise an inbox item for dashboard errors nobody has been told about yet.

        The UI is a separate process (its own systemd unit in production) that can only
        shout into `$JARVIS_HOME/logs/ui.log`. Nothing read that file, so a 500 on the
        work-order page reached exactly one place: the systemd journal. The user's
        report was "when I click on the link I get an internal server error" — the OS
        itself had no idea. The daemon is the component that turns things it notices
        into things the user is told about, so it is the right one to watch the log.

        Runs immediately before `route_new_inbox`, so an error found here goes out with
        this tick's notifications rather than waiting for the next one — hence section 0
        rather than a number of its own in the ordering below.

        Exactly-once by a cursor in `os_state`, not by scanning a time window: a
        standing error must not re-notify every five seconds. `read_errors` additionally
        drops anything older than its reporting window, which is what stops the *first*
        tick after a fresh install — or after any loss of the cursor — from announcing a
        log's whole history as news. Returns the number of new errors found.
        """
        from . import uilog

        errors, cursor = uilog.read_errors(self.central.get_state("ui_log_cursor") or "")
        self.central.set_state("ui_log_cursor", cursor)
        if not errors:
            return 0
        # One item per batch, not per error: a crash loop must not flood the inbox
        # (and Telegram) with hundreds of identical alerts.
        latest = errors[-1]
        paths = sorted({e.path for e in errors})
        body = (f"Latest: {latest.summary}\n"
                f"Affected: {', '.join(paths[:5])}"
                f"{f' (+{len(paths) - 5} more)' if len(paths) > 5 else ''}\n"
                f"Traceback: {uilog.ui_log_path()}")
        self.central.add_inbox(
            project="os", level="warning",
            title=f"dashboard raised {len(errors)} unhandled error"
                  f"{'s' if len(errors) != 1 else ''}",
            body=body,
        )
        log.warning("dashboard errors: %d new (latest: %s)", len(errors), latest.summary)
        return len(errors)

    # -- 1. notifications ----------------------------------------------------------

    def route_outbox(self, project: ProjectSpec, store: ProjectStore) -> None:
        for n in store.unrouted_notifications():
            self.central.add_inbox(
                project=project.name,
                title=n["title"],
                body=n["body"],
                level=n["level"],
                wo_id=n["wo_id"],
            )
            store.mark_notification_routed(n["id"])

    # -- 2b. self-healing after the transport fails -----------------------------------

    def retry_paused_turns(self, project: ProjectSpec, store: ProjectStore,
                           state: fleet.Fleet | None = None) -> None:
        """Relaunch the turns the transport lost, once their wait is up.

        The OS's only loop that repairs a work order without being asked, and it is
        deterministic end to end: `worker_session.turn_pause` says when the turn may go
        again — the reset the refusal named, the next step of `TRANSIENT_BACKOFF`, or the
        moment the user's Claude Code sign-in last changed — and this compares it to the
        clock. No LLM is consulted and none could help: the decision is a comparison.

        All three reasons run through here, because to this pass they differ only in the
        moment they name. That is the whole reason `turn_pause` returns one type: a
        second loop beside this one would be a second place to forget. It is also what
        makes an auth recovery FLEET-WIDE for free — signing in is an account-level fact,
        and `tick` decides `retry_paused` once, outside the project loop, so every
        project's parked orders are swept on the same tick.

        WHEREVER THE WORK ORDER IS PARKED (`RETRY_SWEEP_STATUSES`, issue #259). The
        settler leaves a paused order's status alone, so most of what this touches is
        active — but a turn can be refused on a work order that had already SETTLED into
        `waiting_pr_merge`, `needs_review` or `failed`, and those three were unreachable
        here while the sweep asked for `ACTIVE_STATUSES`. `invariants.stuck_message` has
        always called an undelivered message in exactly those statuses a defect, so the
        OS diagnosed the stall and had nothing that would ever repair it.

        What the relaunch SENDS is `worker_session.retry`'s business, and it is not the
        same for all: a refused turn was never sent, so the prompt goes again verbatim; a
        turn that died in flight already reached the model, so the worker is nudged to
        continue instead.

        UNDER THE FLEET CAP, and this pass is the one that most needs it. Four siblings
        refused by the same window come due in the same second, and on 2026-09-02 they
        resumed together: 362k + 361k + 325k + 322k tokens re-written at the cache-WRITE
        rate, and four `uv run pytest` runs on one machine. Nothing is lost by staggering
        them — a turn held here is not skipped, it is picked up by the next pass ten
        seconds later, because `turn_pause` is re-derived and stays due.

        AND UNDER `max_concurrent`, which is the second door issue #134 had to close: a
        resume is not a dispatch, so without this the project cap would be advisory for
        every order this pass relaunches. It bites narrowly and on purpose — a usage or
        transient pause leaves its work order `running`, which already holds a slot, so
        the only thing held here is the AUTH pause, the one parked out of `running` by
        `_park_on_signin`. Held means the pause stays parked with nothing written.
        """
        budget = project.max_concurrent - store.count_active()
        for wo in store.list_work_orders(statuses=RETRY_SWEEP_STATUSES):
            if state is not None and state.blocked():
                return
            if wo["origin"] in UNGOVERNED_ORIGINS:
                continue  # the user's own session; Jarvis does not drive it
            try:
                pause = worker_session.turn_pause(store, wo["id"])
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] could not diagnose %s", project.name, wo["id"])
                continue
            if pause is None or not pause.resumable or not pause.due():
                continue
            takes_a_slot = resume_spends_slot(wo)
            if takes_a_slot and budget <= 0:
                continue
            try:
                turn = worker_session.retry(store, project, wo, pause)
            except claude_cli.ClaudeCliError as e:
                # Not fatal and not the user's problem yet: the next pass tries again,
                # and the streak cap is what stops this going round for ever.
                log.warning("[%s] retry of %s (%s) failed: %s",
                            project.name, wo["id"], pause.reason, e)
                continue
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] retry of %s failed", project.name, wo["id"])
                continue
            if state is not None:
                state.launched()
            if takes_a_slot:
                budget -= 1
            log.info("[%s] %s resumed after %s (attempt %s/%s, turn %s)",
                     project.name, wo["id"], pause.reason, pause.attempts,
                     pause.max_attempts or "∞", turn["seq"])
            store.add_event(wo["id"], "turn_resumed", {
                "seq": turn["seq"], "retried_seq": pause.turn["seq"],
                "attempt": pause.attempts, "of": pause.max_attempts,
                "reason": pause.reason,
                # WHERE THIS TURN CAME FROM, read back by `ops.resumed_from` when it
                # settles. Recorded here because this is the only moment that knows it.
                "was": wo["status"],
            })
            # The turn is out, so the record must stop saying somebody else has it.
            # Two shapes reach this: the auth pause, parked out of `running` by
            # `_park_on_signin`, and since issue #259 an order that was already settled
            # when its turn was refused.
            #
            # `running` IS REQUIRED FOR THE SECOND, not merely tidier. The statuses
            # issue #259 added are in `PR_POLL_STATUSES`, which excludes the in-flight
            # ones precisely so `complete_merged` cannot end a work order out from under
            # a worker that is still writing to it — so leaving a relaunched order where
            # it was would put a live turn back in front of that poll.
            #
            # `idle` is required for a third reason (issue #264): it asserts that this
            # manager has nothing to act on, which `ops.waiting_on` reports verbatim and
            # `resume-auto` refuses to nudge on. A live turn reading `idle` would be the
            # OS confidently describing a state it had just contradicted.
            if wo["status"] != "running":
                store.set_status(wo["id"], "running")
                # ONLY THE PAUSE'S OWN FLAG. `_park_on_signin` is the one that raises an
                # item this relaunch answers; every other reason on a newly-swept row —
                # assumptions pending review, a red build — is asking for the USER, and
                # the usage window reopening did not answer it (kn-b6977de3). The
                # sign-in item has to go, though: `true_blockers` re-derives it from
                # `waiting_input`, and nothing re-derives it once this row is `running`.
                if wo["attention_reason"] == invariants_mod.AUTH_BLOCKER:
                    store.clear_attention(wo["id"])

    # -- 3. message delivery ----------------------------------------------------------

    def deliver_messages(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Send queued user messages into their work orders' conversations.

        No roster lookup any more, and no thread pool: a turn is a detached process, so
        launching one is instant and the only thing delivery has to wait for is the
        previous turn of the SAME work order finishing (`worker_session.busy`).

        **Everything queued for one work order goes out as ONE turn.** Every turn
        boundary re-sends the whole accumulated conversation at the 1.25x cache-WRITE
        rate instead of reading it at 0.1x — measured at ~12% of this project's entire
        token spend, and ~2x the context per boundary in practice (see `usage.py` and
        `jarvis cost`). Delivering three queued comments as three turns therefore costs
        three of those, for content the worker would rather read together anyway: it can
        act on the whole of what the user said instead of starting down the first
        message's path and being interrupted twice.

        The coalescing is per work order, not global — two work orders' messages are
        independent conversations and must stay separate turns.

        UNDER `max_concurrent`, and this pass is the half of the cap that issue #134
        added. A slot is spent by a turn that is executing (`SLOT_STATUSES`), so a work
        order parked in `waiting_input` or `validating` holds none — and delivery is what
        un-parks it. Uncapped, six rounds rejected by the same validation tick would all
        resume in one pass and a project capped at five would be running six turns; the
        claim-time check in `dispatch_pending` never sees them, because a resume is not a
        dispatch. Held, the message stays `queued` with nothing written, exactly as a
        dependency-blocked order stays `pending`, and the next tick delivers it.

        THIS PASS RUNS BEFORE `dispatch_pending` IN THE TICK, which is what makes the
        hold fair rather than a starvation: a slot that frees goes to the conversation
        somebody is already waiting on before it goes to new work.
        """
        pending: dict[str, list[dict[str, Any]]] = {}
        for msg in store.queued_messages():  # chronological, so the joins stay in order
            try:
                wo = store.get_work_order(msg["wo_id"])
            except KeyError:
                store.mark_message(msg["id"], "failed")
                continue
            # Every per-work-order hold, in the one place that decides them
            # (`worker_session.delivery_hold`). Held, not dropped: the same queue sends
            # it as the next turn once the hold clears. The same call answers
            # `invariants.stuck_message`, which is what keeps "nothing is coming" and
            # "this is why" from drifting apart.
            if worker_session.delivery_hold(store, wo) is not None:
                continue
            pending.setdefault(wo["id"], []).append(dict(msg))
        # Chronological, because `queued_messages` is and a dict keeps insertion order:
        # the oldest waiting conversation takes the free slot.
        budget = project.max_concurrent - store.count_active()
        for wo_id, msgs in pending.items():
            wo = store.get_work_order(wo_id)
            if resume_spends_slot(wo):
                if budget <= 0:
                    log.debug("[%s] holding %s message(s) for %s: all %s slots are full",
                              project.name, len(msgs), wo_id, project.max_concurrent)
                    continue
                budget -= 1
            self._deliver(project, store, wo, msgs)

    def deliver_envelopes(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Route every queued envelope, oldest first (src/jarvis/bus.py).

        The daemon is the only module that knows about the bus and its posters together:
        the bus itself imports nothing above the two stores, so somebody has to turn its
        queue. This is that somebody, and it is deliberately as thin as `route_outbox` —
        every decision, including what to do when a role is unfilled, belongs to the
        router.

        `bus.deliver` handles its own failures (an envelope whose delivery raises stays
        `queued` and is retried), so the guard here is for the unexpected only: one
        envelope that blows up must not cost the project the rest of its tick.
        """
        for envelope in store.queued_envelopes():
            try:
                state = bus.deliver(store, self.central, envelope,
                                    project=project.name)
            except Exception:  # noqa: BLE001
                log.exception("[%s] envelope %s could not be routed",
                              project.name, envelope["id"])
                continue
            if state != "delivered":
                log.info("[%s] envelope %s -> %s", project.name, envelope["id"], state)

    # -- 3b. the validation round machine (see the validation-panel design) -----------

    def validation_tick(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Judge every work order with an open round, off this thread.

        THE KILL SWITCH IS NOT CHECKED HERE, and that is the point of the whole design:
        `os.validation.enabled` gates OPENING a round (`ops.finish`) and never settling
        one. A user who turns the panel off at three in the morning because it is
        misbehaving must not thereby strand every unit already inside it — so this
        drains what is open either way, and a round with no validator settles its unit
        exactly where the OS settles it with the feature switched off.

        Everything expensive — collecting the diff, calling the seats — happens on the
        pool thread. What is left here is two indexed queries, so a fleet with nothing
        in `validating` pays lookups that find nothing.

        **THE QUERY IS OVER ROUNDS, NOT STATUSES** (GitHub issue 212, spec
        docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §3). A work order with
        pending assumptions parks in `needs_review` with its round open beside it, and
        `statuses=("validating",)` — what this used to ask for — could not see one, which
        is the whole of why validation waited on the user.
        """
        for wo in store.work_orders_awaiting_validation():
            wo_id = wo["id"]
            if wo_id in self.validating:
                continue  # its round is in flight; a second tick must not start another
            round_row = store.latest_validation_round(wo_id=wo_id)
            if round_row is None:  # pragma: no cover - the query selected on this round
                continue
            self.validating.add(wo_id)
            future = self.validate_pool.submit(
                self._validate_work_order, project, wo_id, int(round_row["id"]))
            future.add_done_callback(
                lambda f, k=wo_id: self.validating.discard(k))
        # A `validating` row with NO round is the one shape the query above cannot
        # select on, and it is the shape nothing else in the OS looks at either: it
        # raises no attention flag, `settle_work_order` returns early for it, and
        # INV-VALIDATION-STRANDED needs a round to find it by. The warning is all there
        # has ever been, and dropping the status loop would have dropped that too.
        for orphan in store.list_work_orders(statuses=("validating",),
                                             include_hidden=True):
            if store.latest_validation_round(wo_id=orphan["id"]) is None:
                log.warning("[%s] %s is validating with no round on record",
                            project.name, orphan["id"])

    def _validate_work_order(self, project: ProjectSpec, wo_id: str,
                             round_id: int) -> None:
        """One round, start to finish (runs on the single validate thread).

        **This opens its OWN store.** A sqlite connection belongs to the thread that
        created it, and `db.connect` does not pass `check_same_thread=False`, so reusing
        the daemon's would raise rather than corrupt — noisily, which is the good
        outcome, but the round would die every time.

        The round row itself was opened by `ops.finish` on the caller's thread, before
        this was ever queued, so a crash in here leaves a `pending` round something can
        find rather than a work order in `validating` with no trace of why.
        """
        from . import evidence as evidence_mod
        from . import ops, specs

        store = ProjectStore(project.path)  # thread-local connection — see the docstring
        try:
            wo = store.get_work_order(wo_id)
            round_row = store.get_validation_round(round_id)
            if round_row is None:  # pragma: no cover - deleted mid-flight
                return
            cfg = self._round_config(project, round_row)
            n, max_rounds = int(round_row["round"]), int(cfg.max_rounds)
            packet = evidence_mod.collect_work_order(
                project.path, wo, declared=str(round_row["evidence"] or ""),
                diff_chars=cfg.diff_chars, spec=specs.spec_of(store, wo),
                side_effects=ops.side_effects_of(store, wo_id),
                assumptions=store.all_assumptions(wo_id),
                # WHAT EARLIER ROUNDS ALREADY ASKED FOR, so a seat cannot re-litigate
                # settled ground or read an instruction it was given as a defect. ONLY
                # this packet carries it — the one `ops.submit_for_validation` builds
                # exists to be fingerprinted, and `history` is excluded from that hash
                # (spec 2026-09-15-the-panel-blocks-on-blockers.md §5.1, §5.5).
                history=ops.prior_round_history(store, wo_id=wo_id, before=n))
            # WHICH COMMIT THIS ROUND IS JUDGING, recorded before any verdict exists and
            # whatever the verdict turns out to be: it is a fact about the packet, and a
            # rejection that recorded nothing could not later say what it rejected. `""`
            # for a worktree packet, which never auto-merges — spec 2026-09-14 §5.2.
            store.set_validation_head(round_id, evidence_mod.judged_head(packet))

            validator = (self.validator if self.validator is not None
                         else self._validator(cfg))
            if validator is None:
                # Nothing to judge with — the panel is not wired in, or the user turned
                # it off while this round was open. Closed `failed`, never `passed`: a
                # round nobody judged must not read as a verdict on any surface.
                store.close_validation_round(round_id, "failed", NO_VALIDATOR_REASON)
                store.add_event(wo_id, "validation_failed",
                                {"round": n, "cause": "no_validator",
                                 "reason": NO_VALIDATOR_REASON})
                # `panel_cleared`: the round this just closed is `failed`, and that
                # outcome reads as "in flight" to anything that re-reads it.
                ops.land_when_cleared(store, wo, panel_cleared=True)
                log.info("[%s] %s: round %d settled unjudged (no validator)",
                         project.name, wo_id, n)
                return

            # A SUBMISSION WITH NOTHING FOR A REVIEWER never reaches the validator. A
            # reviewer handed nothing to review will approve it, and that single silent
            # pass would make the whole feature theatre.
            #
            # The rule itself is `evidence.nothing_to_judge` — one home, shared with the
            # feature loop below. TWO ANSWERS, and which side of the line each case falls
            # on is the whole of spec
            # docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §3:
            # nothing delivered AT ALL still escalates, because a work order that was
            # supposed to author code and authored none is exactly what the guard is for;
            # a deliverable the OS verifies ITSELF — today only a staged release — is
            # VOIDED, because no seat can add anything to a machine check.
            empty = evidence_mod.nothing_to_judge(packet)
            if empty == "escalate":
                self._escalate(store, wo, round_id, n,
                               "this submission changes no files and records no other "
                               "durable effect, so there is nothing to review. Nobody "
                               "has judged the work.")
                return
            if empty == "void":
                self._void(store, wo, round_id, n, evidence_mod.void_reason(packet))
                log.info("[%s] %s: round %d voided — nothing for a reviewer to judge",
                         project.name, wo_id, n)
                return

            # A REPEAT of the IMMEDIATELY PRECEDING round only. Compared against every
            # earlier round it would punish a submitter that was told to go back to a
            # shape it had already tried, which is a legitimate answer to feedback.
            previous = self._preceding_round(store, n, wo_id=wo_id)
            if self._repeat_submission(round_row, previous):
                self._escalate(
                    store, wo, round_id, n,
                    f"this submission is identical to round {previous['round']} — the "
                    f"same changes and the same declared evidence — so the review has "
                    f"nothing new to judge.")
                return

            try:
                verdict = validator(store, dict(round_row), packet)
            except claude_cli.ClaudeCliError as e:
                self._validation_outage(store, wo, round_id, n, e)
                return

            for seat in verdict.get("seats") or ():
                store.record_validation_opinion(
                    round_id, str(seat.get("seat") or ""),
                    reply=str(seat.get("reply") or ""),
                    verdict=str(seat.get("verdict") or ""),
                    status=str(seat.get("status") or "ok"),
                    model=str(seat.get("model") or ""),
                    latency_ms=int(seat.get("latency_ms") or 0))
            # BEFORE THE OUTCOME BRANCH, not inside it: a follow-up is filed whether the
            # round passed or was rejected. `.get(...) or ()` because `self.validator` is
            # injectable and fakes returning only the three older keys are legitimate.
            self._file_follow_ups(store, project, round_row,
                                  verdict.get("follow_ups") or (), cfg,
                                  unit=wo_id, wo_id=wo_id)
            outcome = str(verdict.get("outcome") or "")
            reason = str(verdict.get("reason") or "")

            if outcome == "passed":
                store.close_validation_round(round_id, "passed", reason)
                store.add_event(wo_id, "validation_passed",
                                {"round": n, "round_id": round_id})
                status = ops.land_when_cleared(store, wo)
                log.info("[%s] %s passed review in round %d -> %s",
                         project.name, wo_id, n, status)
            elif outcome == "rejected" and n < max_rounds:
                self._reject(store, wo, round_id, n, max_rounds, reason)
                log.info("[%s] %s rejected in round %d of %d",
                         project.name, wo_id, n, max_rounds)
            elif outcome == "rejected":
                # The last round it had, and it was refused. Sending feedback now would
                # ask for a resubmission there is no round left to judge.
                self._escalate(store, wo, round_id, n, reason or (
                    f"the review was not satisfied after {max_rounds} rounds."))
            else:
                # `escalated`, or anything the validator returned that is not a verdict
                # this machine knows. Either way a human decides.
                self._escalate(store, wo, round_id, n, reason or (
                    "the review could not reach a verdict."))
        except Exception:  # noqa: BLE001 — a round must never kill the daemon
            log.exception("[%s] validating %s failed", project.name, wo_id)
        finally:
            store.close()

    @staticmethod
    def _file_follow_ups(store: ProjectStore, project: ProjectSpec,
                         round_row: Any, follow_ups: Any, cfg: Any, *, unit: str,
                         wo_id: str | None = None, fo_id: str | None = None) -> None:
        """File this round's non-blocking findings, and say so in the log.

        The filing itself is `ops.file_validation_follow_ups` — business logic the CLI
        can reach, not daemon-private. What is here is the log line §4.4 asks for, in ONE
        place so the two loops cannot word it differently, and the refusal to let filing
        take a round down.

        THAT REFUSAL IS NOT BELT-AND-BRACES, and it matters more now than it did when
        this wrote a backlog row: `_validate_work_order`'s own `except` would abandon the
        round half-settled — no outcome branch, the round left `pending`, the unit
        stranded in `validating`. The verdict has been paid for and every seat is already
        recorded by the time this runs, so a tracker that will not answer must cost the
        follow-ups and nothing else. `ops` already counts the failures it EXPECTS; this
        catches the ones it does not.
        """
        from . import ops

        try:
            filed = ops.file_validation_follow_ups(
                store, project, round_row, follow_ups, cfg, wo_id=wo_id, fo_id=fo_id)
        except Exception:  # noqa: BLE001 — see the docstring
            log.exception("[%s] %s: filing follow-ups failed", project.name, unit)
            return
        if filed["items"] or filed["dropped"] or filed["failed"]:
            log.info("[%s] %s: round %d filed %d follow-up issue(s), dropped %d over the "
                     "cap, %d could not be filed%s",
                     project.name, unit, filed["round"], len(filed["items"]),
                     filed["dropped"], filed["failed"],
                     f" ({filed['reason']})" if filed.get("reason") else "")

    @staticmethod
    def _repeat_submission(round_row: dict[str, Any],
                           previous: dict[str, Any] | None) -> bool:
        """Is this round the SAME SUBMISSION as the one before it, with nothing new?

        A round a PERSON forced is never one, whatever it hashes to. The guard is about a
        SUBMITTER re-delivering unchanged work in answer to feedback; `jarvis validation
        force` has no submitter, and its stated reason is that the JUDGEMENT changed —
        the missing commit of spec 2026-09-14 §5.2, or a panel configuration that has
        moved since. The branch is therefore unchanged BY CONSTRUCTION in every case the
        command exists to serve, so the fingerprints always match and the recovery it
        offers was a no-op that spent a round (issue #272, spec
        docs/superpowers/specs/2026-09-15-forcing-a-validation-round.md §7).

        The panel configuration is NOT the alternative fix: `evidence.fingerprint` hashes
        exactly the evidence and nothing a submitter can move without producing any
        (kn-6f15bba2), and `config_version` is already its own column beside it.

        Shared by both loops so the rule has one home — a feature round cannot be forced
        today, and the copy in `validate_features` is how that stops being true quietly.
        """
        if str(round_row["forced_reason"] or "").strip():
            return False
        return (previous is not None
                and previous["fingerprint"] == round_row["fingerprint"])

    @staticmethod
    def _preceding_round(store: ProjectStore, n: int, *, wo_id: str | None = None,
                         fo_id: str | None = None) -> dict[str, Any] | None:
        """The round before this one, by number. None for the first.

        Keyed the way every round query is — exactly one of `wo_id`/`fo_id` — because
        both loops need it and a shared helper that silently accepted either column would
        be the bad SELECT `ProjectStore._subject` exists to make impossible.
        """
        for row in store.validation_rounds(wo_id=wo_id, fo_id=fo_id):
            if int(row["round"]) == n - 1:
                return row
        return None

    @staticmethod
    def _reject(store: ProjectStore, wo: dict, round_id: int, n: int, max_rounds: int,
                reason: str) -> None:
        """Close the round and send the feedback back — OVER THE BUS, never directly.

        The round machine does not call `queue_message` and never names a work order as
        a recipient: it posts an envelope to the role `implementor` and forgets. What
        fills that role, and what happens when nothing does, is the router's business
        (src/jarvis/bus.py) — which is the whole reason a rejection can serve a feature
        order's manager tomorrow without a line changing here.
        """
        wo_id = wo["id"]
        store.close_validation_round(round_id, "rejected", reason)
        store.add_event(wo_id, "validation_rejected",
                        {"round": n, "round_id": round_id, "of": max_rounds})
        bus.post(store, subject=bus.Subject(wo_id=wo_id),
                 from_role="reviewer", to_role="implementor",
                 payload=bus.ReviewFeedback(
                     round=n, outcome="rejected",
                     reason=REVIEW_FEEDBACK.format(n=n, max=max_rounds, reason=reason,
                                                   wo_id=wo_id)))

    @staticmethod
    def _void(store: ProjectStore, wo: dict, round_id: int, n: int,
              reason: str) -> None:
        """Close a round that had nothing for a reviewer, WITHOUT asking the user.

        The deliberate opposite of `_escalate` below, one line at a time: no attention
        flag, no notification, and the unit settles where it settles with the panel
        switched off. Not a verdict — nothing judged the work — but not a give-up either,
        because nothing was left undecided: spec
        docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §7.

        `panel_cleared` for the no-validator path's reason: this caller just closed the
        round itself and `land_when_cleared` must not re-read what it wrote.

        NOTHING RE-DERIVES A FLAG HERE, which is the half a `set_status` alone would miss:
        `invariants._validation_escalated` keys on `outcome == "escalated"`, so
        `true_blockers` cannot put `VALIDATION_STUCK_BLOCKER` back on the next reconcile
        tick — and `void` is in none of the OPEN/RUNNABLE/COUNTED outcome sets, so no
        other machine picks the unit up either.
        """
        from . import ops

        wo_id = wo["id"]
        store.close_validation_round(round_id, "void", reason)
        store.add_event(wo_id, "validation_void",
                        {"round": n, "round_id": round_id, "reason": reason})
        ops.land_when_cleared(store, wo, panel_cleared=True)

    @staticmethod
    def _escalate(store: ProjectStore, wo: dict, round_id: int, n: int,
                  reason: str) -> None:
        """Give up on this unit and ask the user.

        The attention reason is `VALIDATION_STUCK_BLOCKER` VERBATIM, because
        INV-ATTENTION-REASON rewrites any reason `invariants.true_blockers` cannot
        re-derive — a better sentence here would simply be overwritten on the next
        reconcile tick, and the user would read the generic one.

        THE NOTIFICATION IS THE HALF THE FLAG CANNOT DO (issue #199). An attention flag
        is read by somebody already looking at `jarvis status`; the outbox is what
        reaches the central inbox and every sink, which is how every other "this needs
        you" transition tells the user without being asked. `_reject` deliberately stays
        silent — its feedback travels to the worker over the bus — so the give-up is the
        only transition in this machine that pings.

        NO NEO PRE-STEP, and it is not an oversight (Neo, question 254). By the time a
        give-up could be reviewed the round is closed and the unit is `needs_review`, and
        `invariants.true_blockers` re-derives VALIDATION_STUCK_BLOCKER from exactly that
        pair on every reconcile tick — so a verdict of "do not bother the user" cannot
        take the attention item down, and the call would buy one suppressed sink message
        and nothing else. It becomes worth asking only if Neo may also SETTLE the unit.
        """
        from .invariants import VALIDATION_STUCK_BLOCKER

        wo_id = wo["id"]
        store.close_validation_round(round_id, "escalated", reason)
        store.add_event(wo_id, "validation_escalated",
                        {"round": n, "round_id": round_id, "reason": reason})
        store.set_status(wo_id, "needs_review")
        store.flag_attention(wo_id, VALIDATION_STUCK_BLOCKER)
        store.add_notification(
            title=VALIDATION_ESCALATED_TITLE.format(unit=wo_id, n=n),
            body=escalation_body(reason), level="warning", wo_id=wo_id,
            source="validation",
        )

    def _validation_outage(self, store: ProjectStore, wo: dict, round_id: int, n: int,
                           error: Exception) -> None:
        """The validator could not be reached. That is a transport failure, NOT a
        verdict.

        The round is marked `failed`, which `counted_validation_rounds` ignores, so the
        outage costs the submitter nothing: the next tick picks the same round up and
        tries again. Three in a row is where retrying stops — the outages are counted
        from the events, so a daemon restart does not hand the round a fresh budget.
        """
        wo_id = wo["id"]
        outages = 1 + sum(
            1 for e in store.events_of_kind(wo_id, "validation_failed")
            if db.from_json(e["payload"], {}).get("round") == n
            and db.from_json(e["payload"], {}).get("cause") == "transport")
        store.close_validation_round(
            round_id, "failed", f"the validator could not be reached: {error}")
        store.add_event(wo_id, "validation_failed",
                        {"round": n, "cause": "transport", "attempt": outages,
                         "error": str(error)[:500]})
        if outages >= VALIDATION_OUTAGE_LIMIT:
            self._escalate(
                store, wo, round_id, n,
                f"the review could not be run: the validator was unreachable "
                f"{outages} times in a row. Nobody has judged the work.")

    def _round_config(self, project: ProjectSpec, round_row: dict[str, Any]) -> Any:
        """The validation settings THE ROUND WAS OPENED UNDER, not the live ones.

        What keeps the drain property true once the daemon reloads its catalog: a user
        who disables the panel at 3am must not turn every open round into a `failed`
        one that reads as "nobody judged the work" (config-console design §4.1).

        The fallbacks both land on `project.validation` — the live catalog's answer for
        THIS project, which is what both call sites read before the stamp existed — and
        both mean the same thing: nothing was recorded, so there is nothing to prefer
        over what is running now. A NULL stamp is a round opened before the console
        existed; an unknown id is a ledger that has lost the row.

        BOTH loops read it (Neo, question 176): a feature round is judged under a stamp
        for the same reason a work order's is, and the two settle paths are held
        deliberately identical.

        Opens its own `CentralStore` because this runs on the validate thread, the same
        reason `_validate_work_order` opens its own `ProjectStore`.
        """
        from . import config_version as cv

        vid = round_row.get("config_version")
        if not vid:
            return project.validation
        central = CentralStore()  # thread-local connection — see the docstring
        try:
            row = central.get_config_version(str(vid))
        finally:
            central.close()
        if row is None:
            log.warning("[%s] round %s stamped %s, which is not in the ledger — "
                        "judging under the live catalog",
                        project.name, round_row["id"], vid)
            return project.validation
        return cv.validation_config_from_resolved(row["resolved"], project.name)

    @staticmethod
    def _validator(cfg: Any) -> Any:
        """How a round is judged: the validation panel, or nothing. THE ONLY PLACE
        validation is wired in.

        Returns None when the panel is disabled, exactly as `_panel_answer` does — and
        the feature ships disabled, so on every catalog that has not opted in this stays
        None and not one seat is ever called. A round with no validator settles its unit
        where the OS settles it with validation switched off.

        The import is local for the same reason every other adapter's is: `validation`
        pulls in the seat machinery and the assets, and a daemon on a fleet that never
        enables the panel should not pay to import them.
        """
        if not cfg.enabled:
            return None
        from . import validation

        return lambda store, round_row, packet: validation.decide(
            store, round_row, packet, cfg)

    # -- 3c. the same machine, one level up: feature orders ---------------------------

    def feature_validation_tick(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Judge every FEATURE order parked in `validating`, off this thread.

        The twin of `validation_tick`, and it does not read the kill switch either, for
        the same reason: `enabled` and `feature_units` gate opening a round and never
        settling one, so turning either off drains what is open instead of stranding it —
        and stranding a feature strands its manager too, waiting for a message that would
        never come.

        `self.validating` is SHARED with the work-order machine and holds ids of both
        kinds. That is safe rather than lucky: `db.new_id` prefixes every id with its
        kind, so a `wo-` key and an `fo-` key cannot collide, and one set means one pool
        and one re-entrancy rule to reason about instead of two.
        """
        for fo in store.list_feature_orders(statuses=("validating",)):
            fo_id = fo["id"]
            if fo_id in self.validating:
                continue  # its round is in flight; a second tick must not start another
            round_row = store.latest_validation_round(fo_id=fo_id)
            if round_row is None:
                # In `validating` with no round at all: nothing this machine can judge.
                # INV-VALIDATION-STRANDED covers feature orders and is what finds these.
                log.warning("[%s] feature %s is validating with no round on record",
                            project.name, fo_id)
                continue
            if round_row["outcome"] not in ("pending", "failed"):
                continue  # already judged — settlement is what moves it, not a re-run
            self.validating.add(fo_id)
            future = self.validate_pool.submit(
                self._validate_feature, project, fo_id, int(round_row["id"]))
            future.add_done_callback(
                lambda f, k=fo_id: self.validating.discard(k))

    def _validate_feature(self, project: ProjectSpec, fo_id: str,
                          round_id: int) -> None:
        """One feature round, start to finish (runs on the single validate thread).

        Deliberately the same shape, the same order and the same refusals as
        `_validate_work_order`, including its OWN store — a sqlite connection belongs to
        the thread that made it. What differs is only what the two ends of the round are:
        the packet is the integrated diff rather than one worktree's, and the rejection is
        addressed to the role `manager` rather than `implementor`.

        The one refusal that has no work-order equivalent is the NULL `base_sha`: a
        feature order that predates the column has no honest base to diff from, and
        guessing one produces a confidently wrong diff. It escalates without calling the
        panel, exactly as an empty diff does and for the same reason — a reviewer handed
        the wrong evidence will answer about the wrong evidence.
        """
        from . import evidence as evidence_mod
        from . import ops

        store = ProjectStore(project.path)  # thread-local connection — see the docstring
        try:
            fo = store.get_feature_order(fo_id)
            round_row = store.get_validation_round(round_id)
            if round_row is None:  # pragma: no cover - deleted mid-flight
                return
            cfg = self._round_config(project, round_row)
            n, max_rounds = int(round_row["round"]), int(cfg.max_rounds)
            # `side_effects` is NOT passed here and is not missing: `ops` fills it, the
            # same way and for the same reason it assembles `children` — both are store
            # reads per child, and `evidence.py` may not touch a store. Said out loud
            # because reading this line alone makes the feature path look half-wired.
            packet = ops.collect_feature_evidence(
                store, project.path, fo, declared=str(round_row["evidence"] or ""),
                summary=str(round_row["summary"] or ""), cfg=cfg,
                history=ops.prior_round_history(store, fo_id=fo_id, before=n))

            validator = (self.validator if self.validator is not None
                         else self._validator(cfg))
            if validator is None:
                # Nothing to judge with — the panel is not wired in, or the user turned
                # it off while this round was open. The feature settles exactly where it
                # settles with validation off, which also closes its manager.
                store.close_validation_round(round_id, "failed", NO_VALIDATOR_REASON)
                ops.feature_event(store, fo_id, "validation_failed",
                                  {"round": n, "cause": "no_validator",
                                   "reason": NO_VALIDATOR_REASON, "feature_order": fo_id})
                self._complete_feature(store, fo)
                log.info("[%s] feature %s: round %d settled unjudged (no validator)",
                         project.name, fo_id, n)
                return

            # NO MANAGER, NO LOOP. The manager order is the only addressee a feature-level
            # rejection has, and it is also the only timeline a feature's events can be
            # written to (`ops.feature_event`), so a feature without one can neither act
            # on a verdict nor keep a retry budget. Escalating here rather than after the
            # panel is Neo's ruling on question 153, and it is the same fail-safe as the
            # two refusals below: never spend five headless calls to produce feedback
            # nobody can read. Reachable in normal operation — a plan released while
            # `enabled` was false has no manager, and the user can cancel one.
            if store.manager_work_order(fo_id) is None:
                self._escalate_feature(
                    store, fo, round_id, n,
                    "this feature order has no project manager work order, so a review "
                    "that asked for changes would have nobody to act on it. Nobody has "
                    "judged the work.")
                return
            # THIS GUARD DOES NOT YIELD TO SIDE EFFECTS, and the one below does. They
            # answer different questions: this one asks "can we honestly diff what was
            # delivered", and the next asks "was anything delivered at all". A single
            # knowledge write is an answer to the second and says nothing about the
            # first — so exempting a baseless feature on the strength of one retraction
            # would send a feature whose file changes were never diffable to a panel
            # that has just been told not to read an empty diff as nothing delivered.
            # It could then pass on the retraction alone. Rejected in review, round 1.
            if not packet.base:
                self._escalate_feature(
                    store, fo, round_id, n,
                    "this feature order has no recorded base commit, so there is no "
                    "honest way to say what it changed. It was released before the OS "
                    "started recording one. Nobody has judged the work.")
                return
            # The SAME helper the work-order loop calls, never a second copy of the rule:
            # a feature whose children were releases would otherwise keep escalating for
            # the identical reason after the work-order guard was fixed (spec §8). It
            # runs AFTER the base guard above, which does not yield to it.
            empty = evidence_mod.nothing_to_judge(packet)
            if empty == "escalate":
                self._escalate_feature(
                    store, fo, round_id, n,
                    "nothing has changed on the default branch since this feature "
                    "started and its children record no other durable effect, so there "
                    "is nothing to review. Nobody has judged the work.")
                return
            if empty == "void":
                self._void_feature(store, fo, round_id, n,
                                   evidence_mod.void_reason(packet))
                log.info("[%s] feature %s: round %d voided — nothing for a reviewer "
                         "to judge", project.name, fo_id, n)
                return

            previous = self._preceding_round(store, n, fo_id=fo_id)
            if self._repeat_submission(round_row, previous):
                self._escalate_feature(
                    store, fo, round_id, n,
                    f"this submission is identical to round {previous['round']} — the "
                    f"same integrated diff and the same declared evidence — so the "
                    f"review has nothing new to judge.")
                return

            try:
                verdict = validator(store, dict(round_row), packet)
            except claude_cli.ClaudeCliError as e:
                self._feature_outage(store, fo, round_id, n, e)
                return

            for seat in verdict.get("seats") or ():
                store.record_validation_opinion(
                    round_id, str(seat.get("seat") or ""),
                    reply=str(seat.get("reply") or ""),
                    verdict=str(seat.get("verdict") or ""),
                    status=str(seat.get("status") or "ok"),
                    model=str(seat.get("model") or ""),
                    latency_ms=int(seat.get("latency_ms") or 0))
            # `_validate_work_order`'s line, in the same place and for the same reason.
            self._file_follow_ups(store, project, round_row,
                                  verdict.get("follow_ups") or (), cfg,
                                  unit=fo_id, fo_id=fo_id)
            outcome = str(verdict.get("outcome") or "")
            reason = str(verdict.get("reason") or "")

            if outcome == "passed":
                store.close_validation_round(round_id, "passed", reason)
                ops.feature_event(store, fo_id, "validation_passed",
                                  {"round": n, "round_id": round_id,
                                   "feature_order": fo_id})
                self._complete_feature(store, fo)
                log.info("[%s] feature %s passed review in round %d",
                         project.name, fo_id, n)
            elif outcome == "rejected" and n < max_rounds:
                self._reject_feature(store, fo, round_id, n, max_rounds, reason)
                log.info("[%s] feature %s rejected in round %d of %d",
                         project.name, fo_id, n, max_rounds)
            elif outcome == "rejected":
                # The last round it had, and it was refused. Telling the manager to
                # remediate now would ask for a resubmission there is no round to judge.
                self._escalate_feature(store, fo, round_id, n, reason or (
                    f"the review was not satisfied after {max_rounds} rounds."))
            else:
                self._escalate_feature(store, fo, round_id, n, reason or (
                    "the review could not reach a verdict."))
        except Exception:  # noqa: BLE001 — a round must never kill the daemon
            log.exception("[%s] validating feature %s failed", project.name, fo_id)
        finally:
            store.close()

    @staticmethod
    def _reject_feature(store: ProjectStore, fo: dict, round_id: int, n: int,
                        max_rounds: int, reason: str) -> None:
        """Close the round, tell the role `manager`, and put the feature back to work.

        `executing`, not `validating`: the manager's answer to feedback is remediation
        WORK ORDERS, and a feature that stayed in `validating` would hold children that
        `dispatch_pending` never claimed. The round counter does NOT reset on the way
        back — `counted_validation_rounds` counts judged rounds per subject and knows
        nothing about status — which is the only thing standing between a manager that
        keeps filing children and a loop that never ends.

        The envelope names a ROLE and forgets, exactly as the work-order machine does.
        Whether a manager exists, and what happens when one does not, is the router's
        business: `bus._unfilled` marks the envelope undeliverable AND flags the feature,
        so a feature whose manager is gone reaches the user without this knowing that
        managers can be gone.
        """
        from . import ops

        fo_id = fo["id"]
        store.close_validation_round(round_id, "rejected", reason)
        ops.feature_event(store, fo_id, "validation_rejected",
                          {"round": n, "round_id": round_id, "of": max_rounds,
                           "feature_order": fo_id})
        bus.post(store, subject=bus.Subject(fo_id=fo_id),
                 from_role="reviewer", to_role="manager",
                 payload=bus.ReviewFeedback(
                     round=n, outcome="rejected",
                     reason=FEATURE_REVIEW_FEEDBACK.format(n=n, max=max_rounds,
                                                           reason=reason, fo_id=fo_id)))
        store.set_feature_status(fo_id, "executing")
        store.clear_feature_attention(fo_id)

    def _void_feature(self, store: ProjectStore, fo: dict, round_id: int, n: int,
                      reason: str) -> None:
        """`_void` one level up: close the round and complete the feature, silently.

        `_complete_feature` is the same landing a PASS gets, for the reason the
        no-validator path lands there too — a feature the panel never judged must settle
        exactly where it settles with validation off, and stranding it strands its
        manager with it.
        """
        from . import ops

        fo_id = fo["id"]
        store.close_validation_round(round_id, "void", reason)
        ops.feature_event(store, fo_id, "validation_void",
                          {"round": n, "round_id": round_id, "reason": reason,
                           "feature_order": fo_id})
        self._complete_feature(store, fo)

    @staticmethod
    def _escalate_feature(store: ProjectStore, fo: dict, round_id: int, n: int,
                          reason: str) -> None:
        """Give up on this feature and ask the user.

        THE FLAG GOES ON THE FEATURE ORDER, not on its manager: the manager is a session
        the OS opened to run this loop, and pointing the user at it would send them to
        read a conversation whose whole story is already written down in the rounds.

        The feature stays `validating`. There is no feature-order equivalent of a work
        order's `needs_review` — `FO_STATUSES` has none, deliberately — and the flag is
        what the user reads, not the status: `flagged_feature_orders` is not filtered by
        status, `jarvis status` surfaces it, and the round machine will not pick the
        feature up again because its latest round is `escalated` rather than `pending`.

        `VALIDATION_STUCK_BLOCKER` VERBATIM, for symmetry with the work-order machine.
        `true_blockers` never sees a feature order — it answers "what does this WORK
        ORDER need from me" — so nothing rewrites this reason, but two units giving up
        for the same cause must say the same words to the user.

        The notification carries NO `wo_id`, for the same reason the flag goes on the
        feature: the id this give-up is about is the feature order, the outbox and the
        central inbox only carry a work-order column, and naming the manager there would
        point every sink at a session rather than at the rounds. So the feature id goes
        in the title, where the user reads it (issue #199).
        """
        from . import ops
        from .invariants import VALIDATION_STUCK_BLOCKER

        fo_id = fo["id"]
        store.close_validation_round(round_id, "escalated", reason)
        ops.feature_event(store, fo_id, "validation_escalated",
                          {"round": n, "round_id": round_id, "reason": reason,
                           "feature_order": fo_id})
        store.flag_feature_attention(fo_id, VALIDATION_STUCK_BLOCKER)
        store.add_notification(
            title=VALIDATION_ESCALATED_TITLE.format(unit=fo_id, n=n),
            body=escalation_body(reason), level="warning", source="validation",
        )

    def _feature_outage(self, store: ProjectStore, fo: dict, round_id: int, n: int,
                        error: Exception) -> None:
        """The validator could not be reached. A transport failure, NOT a verdict.

        The round is closed `failed`, which `counted_validation_rounds` ignores, so the
        outage costs the feature no round: the next tick picks the same round up and tries
        again, three times, counted from the events so a daemon restart does not hand it a
        fresh budget.

        The events it is counted from live on the MANAGER's timeline
        (`ops.feature_event`), because `wo_events.wo_id` is a foreign key into
        `work_orders` and a feature order cannot be its own carrier. `_validate_feature`
        refuses to call the panel at all without a manager, so by the time an outage can
        happen the carrier exists — and `not recorded` is the backstop for that being
        wrong, because the failure it would otherwise produce is a round retrying for ever
        against a budget that always reads one.
        """
        from . import ops

        fo_id = fo["id"]
        outages = 1 + sum(
            1 for e in ops.feature_events_of_kind(store, fo_id, "validation_failed")
            if db.from_json(e["payload"], {}).get("round") == n
            and db.from_json(e["payload"], {}).get("cause") == "transport")
        store.close_validation_round(
            round_id, "failed", f"the validator could not be reached: {error}")
        recorded = ops.feature_event(
            store, fo_id, "validation_failed",
            {"round": n, "cause": "transport", "attempt": outages,
             "error": str(error)[:500], "feature_order": fo_id})
        if outages >= VALIDATION_OUTAGE_LIMIT or not recorded:
            self._escalate_feature(
                store, fo, round_id, n,
                f"the review could not be run: the validator was unreachable "
                f"{outages} times in a row. Nobody has judged the work.")

    def _deliver(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                 msgs: list[dict[str, Any]]) -> None:
        ids = [m["id"] for m in msgs]
        log.info("[%s] delivering message(s) %s to %s", project.name, ids, wo["id"])
        store.add_event(wo["id"], "delivering", {"msg_ids": ids})
        # A blank line between messages and nothing else. Anything framing them — a
        # count, a header, "message 2 of 3" — is text the worker can mistake for an
        # instruction from the user, and the user wrote none of it.
        text = "\n\n".join(m["content"] for m in msgs)
        try:
            turn = worker_session.send(store, project, wo, text, msg_id=ids[0])
        except claude_cli.ClaudeCliError as e:
            log.error("[%s] delivery of message(s) %s failed: %s", project.name, ids, e)
            for msg_id in ids:
                store.mark_message(msg_id, "failed")
            store.flag_attention(wo["id"], f"message delivery failed: {e}")
            return
        # Every message in the turn is delivered, not just the one the turn row names:
        # a message left `queued` here would be re-sent on the next tick, so the worker
        # would read it twice and pay a second boundary for the privilege.
        for msg_id in ids:
            store.mark_message(msg_id, "delivered")
        store.add_event(wo["id"], "message_delivered",
                        {"msg_ids": ids, "turn": turn["seq"]})
        # The work order is moving again, whatever it had settled into. A user who sends
        # a message to a finished work order means it to continue, and the turn is
        # already out — leaving the status settled would make the record lie.
        if wo["status"] != "running":
            store.set_status(wo["id"], "running")
            store.clear_attention(wo["id"])

    # -- 5. Neo (answer worker questions) --------------------------------------------

    def neo_tick(self) -> None:
        """Kick a queue drain when questions are waiting and none is running.

        Reclaiming stranded questions happens here, and only here. It runs BEFORE the
        queued count is read, so a question rescued this tick is drained this tick; and
        it runs behind the `neo_draining` guard, so it can never re-queue a question out
        from under a call that is still running.
        """
        if not self.catalog.os.neo.enabled or self.neo_draining:
            return
        from .neo_store import NeoStore
        store = NeoStore()
        try:
            stale = store.reclaim_stale()
            if stale["requeued"] or stale["failed"]:
                log.warning("neo reclaimed stranded questions: requeued=%s failed=%s",
                            stale["requeued"], stale["failed"])
            queued = store.counts().get("queued", 0)
        finally:
            store.close()
        if not queued:
            return
        self.neo_draining = True
        future = self.neo_pool.submit(self._neo_drain)
        future.add_done_callback(lambda f: setattr(self, "neo_draining", False))

    def _neo_drain(self) -> None:
        """Answer every queued question in order (runs on the single neo thread)."""
        from . import invariants
        from . import neo as neo_mod
        from .neo_store import NeoStore

        store = NeoStore()  # thread-local connection
        central = CentralStore()
        paths = {p.name: p.path for p in self.catalog.projects}
        cfg = self.catalog.os.neo

        def deliver(q: dict, verdict: dict) -> None:
            ppath = paths.get(q["project"])
            pstore = ProjectStore(ppath) if ppath and ppath.is_dir() else None
            try:
                if q.get("kind") == "triage":
                    # FIRST, and it returns: this is the one kind with no work order
                    # behind it, so every branch below — each of which reaches for
                    # `q["wo_id"]` — is meaningless here (issue #240).
                    self._deliver_triage_verdict(central, q, verdict)
                    return
                if q.get("kind") == "approval":
                    self._deliver_gate_verdict(central, pstore, q, verdict)
                elif q.get("kind") == "plan":
                    self._deliver_plan_verdict(central, store, pstore, q, verdict)
                elif q.get("kind") == "alarm":
                    # ABOVE the escalate branch, because this kind owns both outcomes —
                    # and the branch below it messages the WORKER, which no alarm may
                    # ever do (§3).
                    self._deliver_alarm_verdict(central, pstore, q, verdict)
                elif q.get("kind") == "assumption":
                    # Above it for the alarm's reason, and one more of its own: the
                    # worker this question is about finished long before it was asked,
                    # so a queued message would start a turn on an order nobody asked to
                    # reopen — `gates.apply_decision`'s `auto_merge` guard, one authority
                    # along.
                    self._deliver_assumption_verdict(central, store, pstore, q, verdict)
                elif verdict["escalate"]:
                    # A headline, never the verbatim question: this row's job is to get
                    # the user's attention, and every inbox row reaches every sink
                    # (Telegram included). Production question #67 was 84KB; the full
                    # text is one `jarvis neo show` away.
                    head = q["question"].strip().splitlines()[0][:200]
                    central.add_inbox(
                        project=q["project"], level="warning",
                        title=f"Neo escalated a question from {q['wo_id']}",
                        body=f"Q: {head}\nWhy: {(verdict['reason'] or '')[:200]}\n"
                             f"Read it in full: jarvis neo show {q['id']}\n"
                             f"Answer it with: jarvis neo answer {q['id']} \"...\"",
                        wo_id=q["wo_id"],
                    )
                    if pstore:
                        # The reason `invariants.true_blockers` re-derives for this
                        # question, not a second phrasing of it: a flag whose reason that
                        # function cannot reproduce is silently relabelled on the next
                        # reconcile tick (kn-78346a2d).
                        pstore.flag_attention(
                            q["wo_id"],
                            invariants.neo_question_blocker(
                                {**q, "status": "failed" if verdict.get("failed")
                                                else "escalated"}),
                        )
                elif pstore:
                    pstore.queue_message(
                        q["wo_id"], f"{neo_mod.ANSWER_PREFIX} {verdict['answer']}",
                        source="neo",
                    )
                    pstore.add_event(q["wo_id"], "neo_answered",
                                     {"neo_question_id": q["id"]})
                    # End the wait the question started, exactly as a gate verdict ends a
                    # gate's. The answer is out; what the work order waits on now is the
                    # OS delivering it, and `waiting_input` outliving that reads as a
                    # USER blocker on every surface that renders it.
                    invariants.end_wait_if_nothing_is_out(pstore, q["wo_id"])
                # `alarm` joins the two exclusions for a reason of its own: a cleanup
                # work order dispatched off a COST OBSERVATION is a work order nobody
                # asked for, and it would spend a worker session to fix the record about
                # a turn that was only ever being watched.
                if pstore and q.get("kind") not in ("approval", "plan", "alarm",
                                                    "assumption"):
                    self._dispatch_neo_cleanup(pstore, q, verdict)
            finally:
                if pstore:
                    pstore.close()

        try:
            results = neo_mod.drain_queue(
                store, model=cfg.model, learnings_limit=cfg.learnings_limit,
                deliver=deliver, answer=self._panel_answer(cfg),
            )
            if results:
                log.info("neo drained %d question(s)", len(results))
        except Exception:  # noqa: BLE001 — the drain must never kill the daemon
            log.exception("neo drain failed")
        finally:
            store.close()
            central.close()

    # -- 5b. dashboard digests (display only — see `jarvis.digest`) -------------------

    def digest_tick(self) -> None:
        """Shorten over-long questions for the `/neo` page, off the critical path.

        This produces NOTHING the OS acts on. It exists because a 7,000-character
        question renders as a wall the user scrolls past, and a review they scroll past
        is a review Neo never gets corrected by. Neo itself still reads every question
        in full, and the page keeps the verbatim text one disclosure away.

        Off when Neo is off, and off when `digest_model` is empty — the one knob that
        turns the extra calls off entirely. The guard mirrors `neo_tick`'s: at most one
        batch in flight, so a slow model cannot pile ticks on top of each other.
        """
        cfg = self.catalog.os.neo
        if not cfg.enabled or not cfg.digest_model or self.digesting:
            return
        from . import digest as digest_mod
        from .neo_store import NeoStore

        store = NeoStore()
        try:
            pending = bool(store.questions_needing_digest(digest_mod.MIN_CHARS, limit=1))
        finally:
            store.close()
        if not pending:
            return
        self.digesting = True
        future = self.digest_pool.submit(self._digest_batch)
        future.add_done_callback(lambda f: setattr(self, "digesting", False))

    def _digest_batch(self) -> None:
        """Digest the questions waiting for one (runs on the single digest thread).

        The batch is capped so one tick cannot spend an unbounded number of calls the
        first time a long-running instance upgrades into this feature — the rest are
        picked up next tick, and until then they render in full.
        """
        from . import agent_usage
        from . import digest as digest_mod
        from .neo_store import NeoStore

        model = self.catalog.os.neo.digest_model
        store = NeoStore()  # thread-local connection
        try:
            for q in store.questions_needing_digest(digest_mod.MIN_CHARS,
                                                    limit=DIGEST_BATCH):
                try:
                    view = digest_mod.summarise(
                        q["question"], model=model,
                        # The question knows which work order it came from, and the
                        # transport does not — so the attribution is bound here.
                        on_usage=agent_usage.recorder(
                            "digest", project=q.get("project") or "",
                            wo_id=q.get("wo_id") or "", model=model,
                            question_id=q["id"]))
                except Exception as e:  # noqa: BLE001 — a digest is never worth a crash
                    # Recorded, not retried: see `digest.encode_failure`. The page falls
                    # back to the full question, which is what it showed before.
                    log.warning("digest failed for neo question %s: %s", q["id"], e)
                    store.set_digest(q["id"], digest_mod.encode_failure(str(e)))
                    continue
                store.set_digest(q["id"], digest_mod.encode(view))
                log.info("digested neo question %s (%d chars)",
                         q["id"], len(q["question"]))
        except Exception:  # noqa: BLE001 — the daemon must survive anything
            log.exception("digest batch failed")
        finally:
            store.close()

    @staticmethod
    def _panel_answer(cfg: Any) -> Any:
        """How this drain answers a question: the panel, or nothing (meaning the single
        agent). This is the ONLY place the panel is wired in.

        Returns None when the panel is disabled, so `drain_queue` falls to its own
        default and the disabled OS makes exactly the calls it always did. The per-question
        `kind` check has to live inside the callable rather than out here, because
        `drain_queue` claims the questions itself and a drain can mix kinds.

        `neo` never imports `panel`; the daemon is the one module that knows about both.
        """
        if not cfg.panel.enabled:
            return None

        from . import neo as neo_mod
        from . import panel

        def answer(store: Any, q: dict, model: str, learnings_limit: int) -> dict:
            if (q.get("kind") or "question") in cfg.panel.kinds:
                return panel.decide(store, q, cfg)
            return neo_mod.answer_question(store, q, model, learnings_limit)

        return answer

    def _dispatch_neo_cleanup(self, pstore: ProjectStore, q: dict, verdict: dict) -> None:
        """File the pre-approved ledger cleanup Neo asked for, if it asked for one.

        The learnings and the knowledge base are append-only, so a superseded ruling sits
        next to the one that replaced it until somebody writes the correction — and the
        only reader positioned to notice is Neo, mid-answer, staring at both. This is the
        hand it gets to fix that: a work order carrying its own authorisation, so the
        worker corrects the record instead of asking permission to.

        Two guards, both load-bearing:
          * The cleanup is pre-approved to CORRECT THE RECORD, not to ship. Privileged
            actions still gate — that is why the marker names its scope in words rather
            than being a bare flag.
          * A cleanup never dispatches a cleanup. Neo answers the cleanup worker's
            questions too, and without this a contradiction it cannot resolve would file
            a fresh work order on every round trip.
        """
        dispatch = verdict.get("dispatch")
        if not dispatch:
            return
        try:
            origin_wo = pstore.get_work_order(q["wo_id"])
        except KeyError:
            origin_wo = {}
        if origin_wo.get("origin") == "neo":
            log.info("neo cleanup dispatch from %s ignored: already a cleanup work order",
                     q["wo_id"])
            return
        description = "\n\n".join(filter(None, [
            dispatch["description"],
            f"Neo filed this while answering question {q['id']} on {q['wo_id']}. "
            f"The correction is ALREADY APPROVED — make it. The stores are append-only, "
            f"so the remedy is to APPEND an entry that supersedes the wrong one "
            f"(`jarvis learn add` for the knowledge base, `jarvis neo learn` for Neo's "
            f"own learnings), naming what it replaces and why.",
        ]))
        wo = pstore.create_work_order(
            title=dispatch["title"], description=description, origin="neo",
            metadata={PRE_APPROVED_KEY: {
                "by": "neo",
                "scope": "correcting the recorded ledger entries this work order names",
                "neo_question_id": q["id"],
                "from_wo": q["wo_id"],
            }},
        )
        pstore.add_event(q["wo_id"], "neo_dispatched",
                         {"neo_question_id": q["id"], "cleanup_wo_id": wo["id"]})
        log.info("neo dispatched pre-approved cleanup %s from %s", wo["id"], q["wo_id"])

    def _deliver_plan_verdict(self, central: CentralStore, neo_store: Any,
                              pstore: ProjectStore | None, q: dict,
                              verdict: dict) -> None:
        """Apply Neo's verdict on a submitted plan.

        Neo releases or sends back through the same `ops.review_plan` the user's
        `jarvis fo approve` uses — the escalation exists because Neo declined to take a
        decision, not because the decision changed shape.

        THE CAP OVERRIDES NEO. A plan at or over the child cap goes to the user whatever
        Neo said, because the cap is one of the two backstops the whole
        Neo-reviews-plans default rests on, and a backstop a reviewer can wave through
        is not one. Neo is still asked first, and its reading is attached to what the
        user sees: the alternative — skipping the call for large plans — hands the user
        a nine-node dependency graph with no read on it, which is the most expensive
        thing to review unaided.
        """
        from . import db as db_mod
        from . import plans

        if pstore is None:
            log.error("plan verdict for question %s has no project store", q["id"])
            return
        fo = pstore.feature_order_for_question(q["id"])
        if fo is None:
            log.warning("plan question %s reviews no feature order (deleted?)", q["id"])
            return
        if fo["status"] != "plan_review":
            # The user got there first through `jarvis fo approve`, or the feature order
            # was cancelled while Neo was thinking. Either way the decision is taken.
            log.info("plan question %s: %s is already %s, dropping Neo's verdict",
                     q["id"], fo["id"], fo["status"])
            return

        plan = db_mod.from_json(fo.get("plan"), {}) or {}
        n_children = len(plan.get("children") or [])
        over_cap = n_children >= plans.CHILD_CAP
        if not verdict["escalate"] and not over_cap:
            from . import ops
            accepted = verdict.get("verdict") == "approved"
            try:
                ops.review_plan(fo["id"], accept=accepted,
                                feedback=verdict["reason"] or "(no reason given)",
                                decided_by="neo")
            except ops.OpsError:
                log.exception("neo's verdict on %s could not be applied", fo["id"])
            else:
                log.info("neo %s the plan for %s", "released" if accepted else
                         "sent back", fo["id"])
            return

        reason = verdict["reason"] or "Neo declined to decide"
        if over_cap and not verdict["escalate"]:
            reason = (f"{n_children} children is at or over the cap of "
                      f"{plans.CHILD_CAP}, so this plan needs you rather than Neo. "
                      f"Neo's reading: {verdict.get('verdict', '?')} — {reason}")
            # Neo answered, but the answer is not what happens. Re-marking the question
            # keeps `jarvis neo list` and `jarvis status` telling the same story: this
            # is now the user's to decide.
            neo_store.mark(q["id"], "escalated", reason=reason)
        pstore.flag_feature_attention(fo["id"], f"plan needs your review: {reason[:160]}")
        central.add_inbox(
            project=q["project"], level="warning",
            title=f"A plan needs your review: {fo['id']}",
            body=f"{fo['title']}\n{n_children} work orders proposed.\n{reason}\n\n"
                 f"Read it with: jarvis fo show {fo['id']}\n"
                 f"Then: jarvis fo approve {fo['id']} [--reject] --feedback \"...\"",
            wo_id=fo.get("plan_wo_id"),
        )
        log.info("plan for %s escalated to the user: %s", fo["id"], reason)

    def _deliver_alarm_verdict(self, central: CentralStore, pstore: ProjectStore | None,
                               q: dict, verdict: dict) -> None:
        """Apply Neo's reading of a cost alarm the supervisor could not settle — §3.

        NOTHING HERE SPEAKS TO THE WORKER, and that is the whole reason this branch
        exists rather than the alarm kind falling through `_neo_drain`'s tail: there was
        no worker question, and a message into a turn already burning money re-sends the
        entire conversation at the cache-write rate — the exact cost the alarm was raised
        to report.

        NEO'S ADVICE ENDS THE ALARM. It does not go back to the supervisor for a second
        opinion: the supervisor already gave the one it had, and the loop would spend a
        call per round to reach an answer the OS is already holding.
        """
        from . import db as db_mod, ops, supervisor

        if pstore is None:
            log.error("alarm verdict for question %s has no project store", q["id"])
            return
        alarm = pstore.alarm_for_question(q["id"])
        if alarm is None:
            log.warning("alarm question %s judges no alarm (work order deleted?)",
                        q["id"])
            return
        if alarm["status"] != "escalated":
            # The user got there first through `jarvis alarms review`, which closes the
            # question on its way past. The decision is taken; Neo's is the stale one.
            log.info("alarm question %s: %s is already %s, dropping Neo's verdict",
                     q["id"], alarm["id"], alarm["status"])
            return

        if verdict["escalate"]:
            # The alarm STAYS `escalated` and is now the user's — the same shape a gate
            # escalation leaves behind, and for the same reason: the row must still be
            # claimable by the command that really decides it.
            central.add_inbox(
                project=q["project"], level="warning",
                title=supervisor.ESCALATED_INBOX_TITLE.format(alarm_id=alarm["id"]),
                body=f"{alarm['reason']}\n"
                     f"The supervisor could not settle it: {alarm['verdict_reason']}\n"
                     f"Neo declined to decide: {(verdict['reason'] or '')[:200]}\n"
                     f"Read it with: jarvis alarms show {alarm['id']}\n"
                     f"The question in full: jarvis neo show {q['id']}",
                wo_id=q["wo_id"])
            pstore.flag_attention(q["wo_id"], supervisor.ALARM_BLOCKER.format(
                alarm_id=alarm["id"]))
            log.info("alarm %s escalated to the user by neo", alarm["id"])
            return

        # The PROJECT's clip, not a literal: `note` is what the user is shown instead of
        # an interruption and it reaches every sink, Telegram included.
        cfg = self._supervisor_config(q["project"])
        note = (verdict["answer"] or "").strip()[:cfg.note_chars]
        pstore.update_alarm(
            alarm["id"], status="acked", verdict="ack", note=note,
            verdict_reason=supervisor.NEO_ANSWERED_REASON.format(
                reason=verdict["reason"] or "(no reason given)"),
            decided_at=db_mod.now())
        pstore.add_event(q["wo_id"], "alarm_advice",
                         {"alarm_id": alarm["id"], "neo_question_id": q["id"],
                          "answer": note})
        # §2's ack path exactly, `ops.ack_attention` and never `clear_attention`: that
        # one wipes `acknowledged_blockers` and discards the user's own dismissals.
        try:
            ops.ack_attention(q["wo_id"])
        except ops.OpsError as exc:
            log.info("alarm %s acked by neo; attention left up: %s", alarm["id"], exc)
        central.add_inbox(
            project=q["project"], level="info",
            title=supervisor.ADVICE_INBOX_TITLE.format(wo_id=q["wo_id"]),
            body=f"{note}\n{supervisor.ALARM_PATH.format(project=q['project'], alarm_id=alarm['id'])}",
            wo_id=q["wo_id"])
        log.info("neo answered alarm %s", alarm["id"])

    def _supervisor_config(self, project: str) -> Any:
        """This project's supervisor settings, falling back to the shipped defaults.

        A verdict can outlive the project's presence in the catalog — the drain reads a
        question filed on an earlier tick — and losing Neo's answer over a missing config
        block would be the failure this feature exists to prevent.
        """
        from .catalog import CatalogError, SupervisorConfig

        try:
            return self.catalog.project(project).supervisor
        except CatalogError:
            return SupervisorConfig()

    def _deliver_gate_verdict(self, central: CentralStore, pstore: ProjectStore | None,
                              q: dict, verdict: dict) -> None:
        """Apply Neo's verdict on a privileged-action request.

        An escalation leaves the request `pending` on purpose: the gate is still shut and
        the user is now the one holding the key, so the row must stay claimable by
        `jarvis gate approve`. Only an explicit approve/deny closes it.
        """
        from . import gates

        if pstore is None:
            log.error("gate verdict for %s has no project store — request %s left pending",
                      q["wo_id"], q["id"])
            return
        approval = pstore.approval_for_question(q["id"])
        if approval is None:
            log.error("neo question %s is an approval with no approval row", q["id"])
            return

        if verdict["escalate"]:
            # A contest must not arrive offering `approve`, the one verb that cannot
            # answer it — spec 2026-09-12 §2.
            if approval["contested"]:
                title = f"Is this a false positive? {approval['kind']} from {q['wo_id']}"
                body = (f"The worker says this command performs no privileged action and "
                        f"the `{approval['kind']}` gate matched it by mistake. Neo "
                        f"declined to decide: {verdict['reason']}\n\n"
                        f"Command: {approval['command']}\n\n"
                        f"It is a false positive:  jarvis gate dismiss {approval['id']} "
                        f"--reason \"...\"\n"
                        f"The worker is wrong:     jarvis gate deny {approval['id']} "
                        f"--reason \"...\"\n"
                        f"The contest in full:     jarvis gate show {approval['id']}")
            else:
                title = f"Approval needed: {approval['kind']} from {q['wo_id']}"
                body = (f"Neo declined to decide: {verdict['reason']}\n\n"
                        f"Command: {approval['command']}\n\n"
                        f"Approve it with: jarvis gate approve {approval['id']} "
                        f"--reason \"...\"\n"
                        f"Deny it with:    jarvis gate deny {approval['id']} "
                        f"--reason \"...\"\n"
                        f"Full request:    jarvis gate show {approval['id']}")
            central.add_inbox(project=q["project"], level="warning", title=title,
                              body=body, wo_id=q["wo_id"])
            pstore.mark_approval_escalated(approval["id"], verdict["reason"])
            pstore.flag_attention(
                q["wo_id"],
                (f"contested gate match escalated by Neo: {approval['kind']} "
                 f"(request {approval['id']})") if approval["contested"] else
                (f"gate approval escalated by Neo: {approval['kind']} "
                 f"(request {approval['id']})"),
            )
            pstore.add_event(q["wo_id"], "gate_escalated", {
                "approval_id": approval["id"], "reason": verdict["reason"],
            })
            return

        asked = verdict.get("verdict") or ("approved" if verdict.get("approve")
                                           else "denied")
        decided = gates.apply_decision(pstore, approval["id"], verdict=asked,
                                       reason=verdict["reason"], decided_by="neo",
                                       central=central, project=q["project"],
                                       exempt_pattern=verdict.get("exempt_pattern", ""))
        # WHAT WAS RECORDED, not what the reviewer said: the two differ on exactly one
        # input, `approved` on a contested row — spec §2. Level, title and body all read
        # from here for that reason.
        ruling = decided["status"]
        # A shipped release is something the user wants to know happened, even when they
        # did not have to authorise it — that is the trade for spending none of their
        # attention on the approval itself.
        #
        # A dismissal is the exception, and silence here is the feature. It reports that
        # the OS's own recogniser misfired on a command that ships nothing; an inbox item
        # for that would spend the user's attention on an OS bug, which is precisely the
        # cost the gate exists to avoid. The false-positive rate is surfaced as a COUNT
        # instead — `jarvis gate list` and the dashboard — because what matters about
        # classifier defects is the rate, not each instance.
        if ruling != "dismissed":
            # A rejected CONTEST stays visible — the one place this feature spends the
            # user's attention on purpose.
            central.add_inbox(
                project=q["project"],
                level="info" if ruling == "approved" else "warning",
                title=(f"Neo rejected a contested {approval['kind']} match from "
                       f"{q['wo_id']}" if decided["contested"]
                       else f"Neo {ruling} {approval['kind']} for {q['wo_id']}"),
                body=(f"{decided['decision_reason']}\n\n"
                      f"Command: {approval['command']}\n"
                      f"Review Neo's call with: jarvis neo review {q['id']}"),
                wo_id=q["wo_id"],
            )
        log.info("gate %s %s by neo for %s", approval["id"], ruling, q["wo_id"])

    # -- 5c. the supervisor: answering a cost alarm (see `jarvis.supervisor`) ---------

    def _supervised_projects(self) -> list[ProjectSpec]:
        """Projects whose supervisor is on, read from the PROJECT's resolved config —
        never from `os.supervisor.enabled` alone, which the per-project block legally
        overrides and which is the expected first configuration."""
        return [p for p in self.catalog.projects
                if p.supervisor.enabled and p.path.is_dir()]

    def supervisor_tick(self) -> None:
        """Kick a review drain when alarms are waiting and none is running — §2.

        Mirrors `neo_tick`, including where the reclaim goes: BEFORE the queued count is
        read, so an alarm rescued this tick is judged this tick, and BEHIND the drain
        guard, so it can never re-queue an alarm out from under a call still running.
        With no project supervised it opens no store and reads no row.
        """
        supervised = self._supervised_projects()
        if not supervised or self.supervisor_draining:
            return
        waiting = 0
        for project in supervised:
            store = self.store_for(project)
            stale = store.reclaim_stale_alarms(project.supervisor.stale_reviewing_seconds,
                                               project.supervisor.max_review_attempts)
            if stale["requeued"] or stale["failed"]:
                log.warning("supervisor reclaimed stranded alarms in %s: "
                            "requeued=%s failed=%s",
                            project.name, stale["requeued"], stale["failed"])
            waiting += len(store.alarms_across(statuses=("raised",)))
        if not waiting:
            return
        self.supervisor_draining = True
        future = self.supervisor_pool.submit(self._supervisor_drain, supervised)
        future.add_done_callback(
            lambda f: setattr(self, "supervisor_draining", False))

    def remedy_tick(self) -> None:
        """Apply the remedies whose gate has since opened — §5 of
        docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.

        SHARES `supervisor_pool`, WHICH IS SINGLE-THREADED, and that is the point rather
        than thrift: an alarm being judged and the same alarm being acted on are two
        writes to one row, and the pool is what serialises them. Nothing here calls a
        model, so the work is cheap; what it is queued behind is a review that is not.

        Only the APPROVED are picked up. A denial or a dismissal is applied at verdict
        time by `remedies.record_verdict`, and one whose approval vanished is closed by
        `invariants.check_proposed_remedies_are_live` — so an alarm still sitting at
        `proposed` here is one whose reviewer has not answered.
        """
        supervised = [p for p in self._supervised_projects()
                      if p.supervisor.remedies.enabled]
        if not supervised or self.remedy_applying:
            return
        ready: list[tuple[ProjectSpec, str]] = []
        for project in supervised:
            store = self.store_for(project)
            for alarm in store.alarms_across(statuses=("proposed",)):
                approval_id = alarm.get("remedy_approval_id")
                approval = store.get_approval(int(approval_id)) if approval_id else None
                if approval is not None and approval["status"] == "approved":
                    ready.append((project, alarm["id"]))
        if not ready:
            return
        self.remedy_applying = True
        future = self.supervisor_pool.submit(self._apply_remedies, ready)
        future.add_done_callback(
            lambda f: setattr(self, "remedy_applying", False))

    def _apply_remedies(self, ready: list[tuple[ProjectSpec, str]]) -> None:
        """Run each approved remedy on the supervisor thread. Re-reads every row.

        Re-read rather than carried: the tick chose these under a snapshot taken before
        the pool ran, and `remedies.apply` refuses on anything that has moved since —
        which is the behaviour that makes the recheck worth having rather than a cost.
        """
        from . import remedies

        central = CentralStore()
        try:
            for project, alarm_id in ready:
                pstore = ProjectStore(project.path)
                try:
                    self._apply_one_remedy(project, pstore, central, alarm_id, remedies)
                except remedies.RemedyRefused as exc:
                    log.warning("[%s] remedy for %s refused: %s", project.name,
                                alarm_id, exc)
                except Exception:  # noqa: BLE001 — one alarm must not stop the rest
                    log.exception("[%s] applying the remedy for %s failed",
                                  project.name, alarm_id)
                finally:
                    pstore.close()
        finally:
            central.close()

    def _apply_one_remedy(self, project: ProjectSpec, pstore: ProjectStore,
                          central: CentralStore, alarm_id: str,
                          remedies: Any) -> None:
        alarm = pstore.get_alarm(alarm_id)
        if alarm["status"] != "proposed":
            return
        approval_id = alarm.get("remedy_approval_id")
        approval = pstore.get_approval(int(approval_id)) if approval_id else None
        subject = self._alarm_subject(pstore, alarm, remedies)
        result = remedies.apply(pstore, central, project.name, approval, alarm, subject)
        log.info("[%s] remedy %s applied for %s: %s", project.name, alarm["remedy"],
                 alarm_id, result)

    def _alarm_subject(self, pstore: ProjectStore, alarm: dict,
                       remedies: Any) -> dict:
        """The work order or feature order an alarm is about — §1's two subject kinds."""
        if remedies.subject_kind(alarm) == "feature_order":
            return pstore.get_feature_order(alarm["fo_id"])
        return pstore.get_work_order(alarm["wo_id"])

    def _supervisor_drain(self, projects: list[ProjectSpec]) -> None:
        """Judge every raised alarm, project by project (on the supervisor thread)."""
        from . import supervisor as supervisor_mod
        from .neo_store import NeoStore

        neo_store = NeoStore()   # thread-local connections, as `_neo_drain` opens its own
        central = CentralStore()
        try:
            for project in projects:
                pstore = ProjectStore(project.path)
                try:
                    self._drain_project_alarms(project, pstore, neo_store, central,
                                               supervisor_mod)
                except Exception:  # noqa: BLE001 — one project must not stop the rest
                    log.exception("supervisor drain failed for %s", project.name)
                finally:
                    pstore.close()
        finally:
            neo_store.close()
            central.close()

    def _drain_project_alarms(self, project: ProjectSpec, pstore: ProjectStore,
                              neo_store: Any, central: CentralStore,
                              supervisor_mod: Any) -> None:
        """Claim and judge one project's alarms until the queue is empty — §2.

        Every exclusion moves the alarm OUT of the queue rather than leaving it in: one
        nothing will look at again must not stay claimable. An alarm on an order that has
        since settled is still judged; only age excludes one.
        """
        cfg = project.supervisor
        max_age = cfg.max_age_hours * SECONDS_PER_HOUR
        while True:
            alarm = pstore.claim_next_alarm()
            if alarm is None:
                return
            age = time.time() - float(alarm["ts"] or 0.0)
            if age > max_age:
                pstore.update_alarm(
                    alarm["id"], status="skipped", decided_at=db.now(),
                    verdict_reason=f"raised {age / SECONDS_PER_HOUR:.0f}h ago, past the "
                                   f"{cfg.max_age_hours}h review window — the spend can "
                                   f"no longer be prevented")
                continue
            try:
                wo = pstore.get_work_order(alarm["wo_id"])
            except KeyError:
                pstore.update_alarm(alarm["id"], status="skipped", decided_at=db.now(),
                                    verdict_reason="the work order is gone")
                continue
            verdict = supervisor_mod.review(
                pstore, neo_store, project.name, wo, alarm, cfg,
                central=central, inspect_cfg=project.inspect)
            log.info("[%s] alarm %s: %s (%s)", project.name, alarm["id"],
                     verdict["decision"], verdict["reason"][:cfg.reason_chars])

    # -- 5d. the health sweep: looking before anything crosses a threshold ------------

    def _health_projects(self) -> list[ProjectSpec]:
        """Projects being swept on THIS tick — §4 of
        docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.

        Two switches, not one: `health_enabled` sits on top of `supervisor.enabled`,
        because a project may want a reviewer for its cost alarms without also paying
        the standing cost of watching. The cadence is per project for the same reason
        every other threshold is.
        """
        return [p for p in self.catalog.projects
                if p.supervisor.enabled and p.supervisor.health_enabled
                and p.path.is_dir()
                and self.tick_count % p.supervisor.health_every_ticks == 1]

    def health_tick(self) -> None:
        """Kick a sweep when a unit is due and none is running.

        Modelled on `supervisor_tick`, including the guard: a sweep still in flight must
        never have a second one queued behind it, because the candidate list would be
        computed against state the first one is still writing. With no project swept it
        opens no store and reads no row.
        """
        due = self._health_projects()
        if not due or self.health_sweeping:
            return
        self.health_sweeping = True
        future = self.health_pool.submit(self._health_sweep, due)
        future.add_done_callback(lambda f: setattr(self, "health_sweeping", False))

    def _health_candidates(self, pstore: ProjectStore,
                           cfg: Any) -> list[tuple[float, dict, str]]:
        """Every open unit due for a look, LONGEST-UNREVIEWED FIRST.

        The order is what makes `health_max_units_per_tick` a rotation rather than a
        starvation: sorted by last look, the units the cap cut off this tick are the
        ones at the front of the next one.

        `waiting_input` is in `OPEN_STATUSES` and belongs here on purpose — an order
        parked behind a message nobody will send is exactly what `waiting-on-nobody`
        exists to catch, and it is invisible to every cost heuristic.
        """
        from . import health

        now = db.now()
        subjects = [{"kind": "work_order", "row": wo}
                    for wo in pstore.list_work_orders(statuses=OPEN_STATUSES)
                    if wo["origin"] not in UNGOVERNED_ORIGINS]
        subjects += [{"kind": "feature_order", "row": fo}
                     for fo in pstore.list_feature_orders(statuses=FO_OPEN_STATUSES)
                     # A feature with no carrier has no session at all and nothing to
                     # record a finding on — see `carrier_for_feature`.
                     if pstore.carrier_for_feature(fo["id"]) is not None]
        out = []
        for subject in subjects:
            row = subject["row"]
            last = pstore.last_health_review(subject["kind"], row["id"])
            # TWO READS, and they disagree precisely when the sweep is failing: `last`
            # is the last JUDGEMENT and skips a `failed` row, `attempt` is the last CALL
            # and counts it. `due` compares the fingerprint against the first and floors
            # the spend on the second — see issue #216.
            attempt = pstore.last_health_attempt_ts(subject["kind"], row["id"])
            trigger = health.due(last, health.fingerprint(pstore, subject), cfg, now,
                                 float(row.get("created_at") or 0.0),
                                 last_attempt=attempt)
            if trigger:
                out.append((float(last["ts"]) if last else 0.0, subject, trigger))
        out.sort(key=lambda c: c[0])
        return out

    def _health_sweep(self, projects: list[ProjectSpec]) -> None:
        """Sweep each project's due units, capped (on the health thread)."""
        from . import supervisor as supervisor_mod
        from .neo_store import NeoStore

        neo_store = NeoStore()   # thread-local connections, as `_supervisor_drain` does
        try:
            for project in projects:
                cfg = project.supervisor
                pstore = ProjectStore(project.path)
                try:
                    for _, subject, trigger in self._health_candidates(
                            pstore, cfg)[:cfg.health_max_units_per_tick]:
                        supervisor_mod.review_health(
                            pstore, neo_store, project.name, subject, cfg.probes, cfg,
                            trigger, inspect_cfg=project.inspect)
                except Exception:  # noqa: BLE001 — one project must not stop the rest
                    log.exception("health sweep failed for %s", project.name)
                finally:
                    pstore.close()
        finally:
            neo_store.close()

    # -- 6b. the scheduler: work orders nobody typed ---------------------------------------

    def _os_owner(self) -> str | None:
        """Which project runs the fleet-wide OS checks — `schedule.os_owner`, per tick."""
        from . import schedule

        return schedule.os_owner((p.name, p.path) for p in self.catalog.projects)

    @staticmethod
    def _schedule_blocker(store: ProjectStore, state: dict[str, Any]) -> dict[str, Any] | None:
        """The job's own previous order, if it has not settled yet.

        A DELETED previous order is not a blocker: the clock outlives the work order it
        filed (`scheduled_jobs` carries no foreign key), and a job that could never fire
        again because somebody tidied up its last receipt would be a scheduler killed by
        housekeeping.
        """
        prev = state.get("last_wo_id")
        if not prev:
            return None
        try:
            wo = store.get_work_order(str(prev))
        except KeyError:
            return None
        return None if wo["status"] in TERMINAL_STATUSES else wo

    @staticmethod
    def _retire_previous(store: ProjectStore, state: dict[str, Any]) -> None:
        """Hide yesterday's receipt as today's is filed — the attention-budget rule.

        A daily job left alone fills the listing with a year of identical rows, which is
        the alarm nobody reads that this whole design is written against. So exactly one
        scheduled order per job stays visible: the newest.

        HIDING IS EARNED, NOT AUTOMATIC. A previous order that ended badly — flagged, or
        holding an assumption the user has not ruled on — is left where it is, because
        those are the two states in which it is still asking for something. What a run
        FOUND does not live here either way: findings leave as their own work orders,
        filed under their own origin, and nothing hides those.
        """
        prev = state.get("last_wo_id")
        if not prev:
            return
        try:
            wo = store.get_work_order(str(prev))
        except KeyError:
            return
        if (wo["status"] in TERMINAL_STATUSES and not wo["needs_attention"]
                and not wo["hidden"] and not store.pending_assumptions(wo["id"])):
            store.set_hidden(wo["id"])

    def schedule_tick(self, project: ProjectSpec, store: ProjectStore) -> None:
        """File the work orders this project's clock says are due.

        THE ONLY PLACE IN THE OS THAT CREATES WORK NOBODY ASKED FOR, and every guard rail
        it leans on is somewhere else on purpose: the cadence is `schedule.decide` (pure,
        so it is tested without a daemon), the roster and the master switch are
        `catalog.ScheduleConfig` (so `jarvis config show` answers "what will this spend"),
        and the clock is `scheduled_jobs` (so a restart cannot re-fire one). What is left
        here is the wiring. Spec: docs/superpowers/specs/2026-09-14-the-scheduler.md.
        """
        from . import schedule

        cfg = project.schedule
        if not cfg.enabled or not cfg.jobs:
            return
        owner = self._os_owner()
        now = db.now()
        for job_id in cfg.jobs:
            try:
                spec = schedule.job(job_id)
            except KeyError:
                # `_parse_schedule` refuses an unknown id at boot, so this is only
                # reachable on a DOWNGRADE — a catalog naming a job the running build no
                # longer ships. Skipping beats refusing the whole tick.
                log.warning("project %s: no scheduled job %r in this build",
                            project.name, job_id)
                continue
            state = store.seed_schedule(job_id, now)
            decision = schedule.decide(
                state, interval_seconds=cfg.interval_seconds, now=now,
                blocker=self._schedule_blocker(store, state))
            if decision.action == schedule.WAIT:
                continue
            if decision.action == schedule.HOLD:
                store.record_schedule_hold(job_id, decision.reason, now)
                continue
            ctx = schedule.JobContext(project=project.name,
                                      owns_os=owner == project.name)
            wo = store.create_work_order(title=spec.title, description=spec.describe(ctx),
                                         origin=schedule.ORIGIN)
            self._retire_previous(store, state)
            store.record_schedule_fire(job_id, wo["id"], now)
            log.info("scheduled job %s filed %s in %s", job_id, wo["id"], project.name)

    def abandon_unargued_gates(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Close every gate request whose case never came. See `gates.sweep_unargued`.

        The other half of holding an unargued request back from review: nothing else can
        ever close one, because there is no Neo question to answer and no escalation for
        the user to see. Without this the hold leaks.
        """
        from . import gates

        if not project.gates:
            return
        try:
            closed = gates.sweep_unargued(store, project.gates.case_ttl_seconds)
        except Exception:  # noqa: BLE001 — one project's sweep must not stop the tick
            log.exception("project %s: closing unargued gates failed", project.name)
            return
        for approval in closed:
            # "abandoned", never "refused": nobody reviewed it — spec 2026-09-12 §4.
            log.info("gate %s (%s) abandoned: no case was made and the match was never "
                     "contested", approval["id"], approval["kind"])

    # -- 7. invariants (post-conditions) --------------------------------------------------

    def check_invariants(self, project: ProjectSpec, store: ProjectStore, *,
                         sweep_landings: bool = False) -> None:
        """Verify the OS's own state and repair what is unambiguously wrong.

        Runs after reconcile so it judges the state this tick actually produced. Every
        other step here trusts that its writes stuck; this is the only one that checks.
        Repairs are recorded on the work order's timeline so a self-healed inconsistency
        is visible rather than silently papered over, and each distinct violation is
        reported once per daemon run.

        `sweep_landings` adds `invariants.SLOW_INVARIANTS` — the checks that shell out —
        on `LANDING_SWEEP_EVERY_TICKS`. `jarvis doctor` runs them every time; the daemon
        cannot, which is the whole reason the flag exists.
        """
        from .invariants import check_project

        try:
            violations = check_project(store, repair=True, slow=sweep_landings)
        except Exception:  # noqa: BLE001 — the checker must never take the daemon down
            log.exception("[%s] invariant check failed", project.name)
            return

        for v in violations:
            if v.key in self.reported_violations:
                continue
            self.reported_violations.add(v.key)
            log.warning("[%s] %s", project.name, v)
            if v.wo_id:
                store.add_event(v.wo_id, "invariant", {
                    "invariant": v.invariant, "detail": v.detail,
                    "repaired": v.repaired, "repair": v.repair, **v.context,
                })
            if not v.repaired:
                # Nothing deterministic to do about it — this one needs a human.
                store.add_notification(
                    title=f"OS invariant violated: {v.invariant}",
                    body=f"{v.detail}" + (f" ({v.wo_id})" if v.wo_id else ""),
                    level="warning", wo_id=v.wo_id, source="invariants",
                )

    # -- 2 & 6. turns, settlement, and injected sessions ---------------------------------------------------

    def check_burning_turns(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Raise a turn that is costing money WHILE it is still costing it.

        The user asked for a self-inspecting mechanism, and the reason is that every
        cost surface Jarvis had answered after the fact: `jarvis cost` and `jarvis
        inspect` both read a bill. This is the same arithmetic run against a turn that
        has not finished, and it reaches the user the way everything else does — the
        attention list — rather than inventing a channel.

        ONE ALARM PER TURN PER KIND, recorded as a `cost_alarm` event and checked against
        that record rather than against the attention flag. The flag is not enough: the
        user putting it down with `jarvis wo ack` would bring the same sentence straight
        back on the next tick, which is precisely how a cost alarm becomes noise and then
        gets ignored. Thresholds and the off switch: `catalog.InspectConfig`.

        Read-only and free of the model: one transcript read per running work order, on
        the reconcile cadence rather than every tick.
        """
        from . import usage as usage_mod

        # The PROJECT's thresholds, already resolved against the OS block by
        # `catalog._parse_inspect`. What counts as a long turn is a statement about what
        # is normal, and normal differs by project — an hour is routine where the work is
        # a design document and a symptom where it is a one-file fix.
        cfg = project.inspect
        if not cfg.enabled:
            return
        now = time.time()
        index = usage_mod.index_sessions()
        for wo in store.list_work_orders(statuses=("running",)):
            session_id = wo.get("session_id")
            if not session_id or wo["origin"] in UNGOVERNED_ORIGINS:
                continue
            turn = store.latest_turn(wo["id"])
            if turn is None or turn["state"] != "running":
                continue
            try:
                raised = inspection.live_alarms(session_id, cfg, wo_id=wo["id"],
                                                now=now, index=index,
                                                dispatched=turn["started_at"])
            except OSError:
                continue  # a transcript Jarvis cannot read is not a work order in trouble
            seen = [db.from_json(e["payload"], {}) or {}
                    for e in store.events_of_kind(wo["id"], "cost_alarm")]
            already = {p.get("kind") for p in seen if p.get("seq") == turn["seq"]}
            fresh = [a for a in raised if a.kind not in already]
            for alarm in fresh:
                # THE ROW IS THE IDENTITY; THE EVENT IS STILL THE DEDUPE MEMORY, and
                # `alarm_id` is purely additive to a payload whose other three keys are
                # what `already` above matches on. Move the dedupe onto `wo_alarms` and
                # this re-raises every tick for the life of the turn.
                row = store.add_alarm(wo["id"], alarm.kind, turn["seq"], alarm.reason)
                store.add_event(wo["id"], "cost_alarm",
                                {"kind": alarm.kind, "seq": turn["seq"],
                                 "reason": alarm.reason, "alarm_id": row["id"]})
                log.info("[%s] %s: %s", project.name, wo["id"], alarm.reason)
            # Every alarm goes on the timeline; only the first reaches the attention
            # line, because `alarms` returns them most-actionable first and a flag can
            # carry one sentence.
            if fresh and not wo["needs_attention"]:
                store.flag_attention(wo["id"], fresh[0].reason)

    def check_rewrite_tax(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Raise a project's STANDING re-write tax, split by the cause that produced it.

        THE AGGREGATE HALF OF `check_burning_turns`. That one reports a single call
        re-sending a single conversation while the turn is still running and already
        names its cause; this one reports the condition ACROSS settled orders, which no
        surface raised and which the user asked for in issue 164 item 1.

        Read off sealed bills, so it costs an indexed query and a JSON parse per order in
        the window — no transcript walk, and no model call.

        THREE THINGS HERE ARE DELIBERATE AND EACH WOULD LOOK LIKE AN OMISSION.

        * NO ATTENTION FLAG. The carrier is a settled order, and
          `invariants.check_no_phantom_attention` clears the flag on every terminal order
          on the next tick — so a flag here would evaporate and the OS would be raising a
          finding it then hides. The INBOX ROW is the durable half, which is
          `remedies._flag_and_tell`'s own reasoning arrived at from the other side. The
          supervisor's queue is what carries it onward: it claims the alarm, judges it,
          and may propose `file_work_order`. The cost of that, stated rather than
          discovered: on a project running the supervisor the user hears twice, once here
          and once at the verdict. It is paid deliberately, because the supervisor SHIPS
          OFF and a finding whose only reader is a disabled subsystem reaches nobody at
          all (Neo question 291).
        * THE CARRIER IS AN EXEMPLAR, NOT A CULPRIT. `wo_alarms.wo_id` is a real foreign
          key and an aggregate finding is about a project, so something must carry it;
          the biggest single contributor is the one order whose evidence packet is
          actually about the number being judged. `SUPERVISOR_PERSONA` says so in as many
          words, because a judge shown a settled order would otherwise look for what is
          wrong with THAT order.
        * THE DEDUPE IS ONE ALARM PER KIND PER WINDOW. The condition is still true on the
          next tick — that is what makes it standing rather than burning — so matching on
          `(kind, seq)` the way `check_burning_turns` does would re-raise it every
          reconcile for ever. `last_alarm_of_kind` is the memory, and the window is the
          same cohort window the arithmetic used.
        """
        from . import bill as bill_mod
        from .project_store import NO_TURN

        cfg = project.inspect
        if not cfg.enabled:
            return
        tax = bill_mod.rewrite_tax(store, days=cfg.alarm_rewrite_window_days)
        if tax is None:
            return
        # BOTH floors, and they answer different questions: too few orders is one
        # order's shape wearing the project's name, too little money is a percentage of
        # nothing. Either alone lets the other case through.
        if tax.orders < cfg.alarm_rewrite_min_orders \
                or tax.bill_usd < cfg.alarm_rewrite_min_usd:
            return
        # `(kind, reason)` and not an `inspection.Alarm`: `bill` is accounting and does
        # not import the config layer to name a string — see `bill.REWRITE_PREFIX_ALARM`.
        for kind, reason in bill_mod.rewrite_alarms(tax, cfg):
            last = store.last_alarm_of_kind(kind)
            if last is not None and float(last["ts"] or 0.0) >= tax.since:
                continue
            row = store.add_finding(tax.worst_id, kind=kind, reason=reason,
                                    seq=NO_TURN, source="cost")
            store.add_event(tax.worst_id, "cost_alarm",
                            {"kind": kind, "seq": NO_TURN,
                             "reason": reason, "alarm_id": row["id"]})
            self.central.add_inbox(
                project=project.name, level="warning",
                title=REWRITE_INBOX_TITLE[kind].format(project=project.name),
                body=f"{reason}\n"
                     f"The supervisor will look before you have to. "
                     f"Read it with: jarvis alarms show {row['id']}",
                wo_id=tax.worst_id)
            log.info("[%s] %s: %s", project.name, kind, reason)

    def check_cache_ttl(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Raise the fleet's remaining ONE-HOUR cache writes, split by who bought them.

        Issue 164 item 3; finding 3 of
        docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md.

        A FLEET READING CARRIED BY ONE PROJECT, which is why this returns immediately for
        every project but the one `schedule.os_owner` names. Transcripts are indexed by
        the cwd a session was created in and a hand-opened one belongs to no project at
        all, so there is nothing to attribute per project — and N projects raising the
        same fleet fault is N-1 copies of an alarm nobody reads (`schedule.JobContext`
        made the same call for the OS-level doctor checks).

        FOUR THINGS HERE MATCH `check_rewrite_tax` AND ONE DOES NOT.

        Matching: no attention flag (the carrier has settled and
        `invariants.check_no_phantom_attention` would clear it next tick), the inbox row
        as the durable half, an EXEMPLAR carrier rather than a culprit, and one alarm per
        kind per window via `last_alarm_of_kind`.

        NOT matching, and it is the whole character of this alarm: the foreign half
        reports a condition THE OS CANNOT FIX. The remedy is a line in the user's own
        `~/.claude/settings.json`, which Jarvis must never write — so what the supervisor
        can do is file a work order telling a person to add it, and the reason carries the
        line verbatim so that order is writable without a second investigation.

        THE CARRIER IS THE PROJECT'S MOST RECENT SETTLED ORDER and stands for nothing but
        the foreign key. `check_rewrite_tax` could pick the biggest contributor because
        its subject WAS a work order; this one's subject is a session the OS never
        dispatched, so there is no order the number is about. `SUPERVISOR_PERSONA` says
        so: the alarm is about the fleet, and the order under it is a hook.
        """
        from .project_store import NO_TURN

        cfg = project.inspect
        if not cfg.enabled or project.name != self._os_owner():
            return
        days = cfg.alarm_cache_1h_window_days
        since = db.now() - days * 86_400
        try:
            found = inspection.one_hour_writes(since)
        except OSError:  # a transcript tree that moved or is unreadable
            log.exception("[%s] the one-hour cache scan could not read transcripts",
                          project.name)
            return
        raised = inspection.hour_alarms(found, cfg, days=days)
        if not raised:
            return
        carrier = store.latest_settled_order()
        if carrier is None:
            # Nothing to hang the foreign key on. The finding is not lost — it is still
            # true on the next pass, and the first order this project settles carries it
            # then. Alarming against an order that does not exist is the alternative.
            log.info("[%s] one-hour cache writes found, but no settled order to carry "
                     "the alarm yet", project.name)
            return
        for alarm in raised:
            last = store.last_alarm_of_kind(alarm.kind)
            if last is not None and float(last["ts"] or 0.0) >= since:
                continue
            row = store.add_finding(carrier["id"], kind=alarm.kind, reason=alarm.reason,
                                    seq=NO_TURN, source="cost")
            store.add_event(carrier["id"], "cost_alarm",
                            {"kind": alarm.kind, "seq": NO_TURN,
                             "reason": alarm.reason, "alarm_id": row["id"]})
            self.central.add_inbox(
                project=project.name, level="warning",
                title=CACHE_1H_INBOX_TITLE[alarm.kind],
                body=f"{alarm.reason}\n"
                     f"The supervisor will look before you have to. "
                     f"Read it with: jarvis alarms show {row['id']}",
                wo_id=carrier["id"])
            log.info("[%s] %s: %s", project.name, alarm.kind, alarm.reason)

    def settle_turns(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Reap finished turns, then move each work order to where its turn says it is.

        Two layers, deliberately: `worker_session.poll` decides whether a *turn* ended
        and records what the worker said; this decides what that means for the *work
        order*. The second half is the settlement logic that used to compare against
        `claude agents --json`, reading a row Jarvis owns instead of a roster it does not.
        """
        for turn in worker_session.poll(store):
            log.info("[%s] turn %s of %s ended: %s", project.name, turn["seq"],
                     turn["wo_id"], turn["state"])
        # `idle` is in the sweep, and it is what MIGRATES the managers this release
        # finds already parked in `waiting_input` (issue #264): each one falls through
        # to the manager branch below on the first tick and is re-statused, before the
        # invariant pass that would otherwise read it as blocked on the user. It also
        # keeps an idle manager reachable by the branch that closes it when its feature
        # settles.
        for wo in store.list_work_orders(
                statuses=("running", "idle", "waiting_input", "dispatching")):
            if wo["origin"] in UNGOVERNED_ORIGINS:
                continue  # not ours to run; track_injected_sessions follows these
            try:
                self.settle_work_order(project, store, wo)
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] settling %s failed", project.name, wo["id"])

    def settle_work_order(self, project: ProjectSpec, store: ProjectStore,
                          wo: dict) -> None:
        from .invariants import awaiting_neo, something_is_out, true_blockers

        if wo["status"] == "validating":
            # THE ROUND MACHINE OWNS THIS WORK ORDER. Everything below re-derives the
            # outcome from the latest turn on EVERY tick — and that turn is a done one
            # carrying a `result_summary` and a `pr_url`, so without this return the
            # reconciler would set `waiting_pr_merge` on the very next tick and put
            # unvalidated work on the user's merge queue. The runner is what moves it.
            return

        turn = store.latest_turn(wo["id"])
        if turn is None:
            if time.time() - wo["updated_at"] <= 300:
                return  # just claimed; give the launch a moment to record its turn
            if wo.get("session_id") or wo.get("job_id"):
                # In flight when this release landed: dispatched under the background
                # -session transport, so it has a conversation but no turn on record.
                # Its agent is still the thing driving it and this reconciler cannot
                # see that agent, so settling it either way would be a guess — failing
                # it would be a lie about work that may be perfectly fine. Surface it
                # instead: the next message migrates it (`worker_session.send` releases
                # the agent and resumes the same session under a turn), and `cancel`
                # still stops it. Either way it passes through here only once.
                if not wo["needs_attention"]:
                    store.flag_attention(
                        wo["id"],
                        "carried over from the old worker transport — send it a "
                        f"message to resume it, or `jarvis wo cancel {wo['id']}`",
                    )
                    store.add_event(wo["id"], "pre_turn_carryover",
                                    {"job_id": wo.get("job_id"),
                                     "session_id": wo.get("session_id")})
                return
            # Claimed but never launched — the daemon died between the two writes.
            store.set_status(wo["id"], "failed")
            store.flag_attention(wo["id"], "worker turn never started")
            return

        if turn["state"] == "running":
            if wo["status"] == "dispatching":
                store.set_status(wo["id"], "running")
            elif worker_session.is_stalled(turn) and not wo["needs_attention"]:
                hours = int((time.time() - turn["started_at"]) // 3600)
                store.flag_attention(
                    wo["id"],
                    f"turn running for over {hours}h — check on it or "
                    f"`jarvis wo cancel {wo['id']}`",
                )
            return

        if turn["state"] == "failed":
            # Lost to the transport, not broken: leave the work order exactly where it
            # is and let `retry_paused_turns` relaunch it when the wait is up — the
            # window reopening, or the next step of the backoff. Failing it would strand
            # its dependents (`failed` is a DEPENDENCY_DEAD_STATUS), fail its parent
            # feature order, and flag the user for something that fixes itself — and the
            # flag would come straight back every reconcile tick, because `true_blockers`
            # derives it from the status.
            #
            # Auth first, and it is the one that does not just fall through: it never
            # exhausts, and unlike the other two the user has something to do about it.
            pause = worker_session.turn_pause(store, wo["id"])
            if pause and pause.reason == worker_session.PAUSE_AUTH:
                self._park_on_signin(store, wo, pause, turn)
                return
            if pause and not pause.exhausted:
                return
            if wo["status"] != "failed":
                store.set_status(wo["id"], "failed")
                store.flag_attention(wo["id"],
                                     "worker turn failed — review and retry")
                if pause:
                    # Retried until the OS ran out of patience. Say so plainly: the
                    # message the user needs is "this is not going to fix itself", and
                    # a bare "worker turn failed" would send them looking for a bug in
                    # the work instead of at the account's limits or Anthropic's status
                    # page.
                    store.add_event(wo["id"], "turn_retries_exhausted",
                                    {"attempts": pause.attempts,
                                     "reason": pause.reason,
                                     "error": pause.message})
                store.add_notification(
                    title=(f"{wo['id']} still failing after {pause.attempts} "
                           f"{worker_session.PAUSE_NOUN[pause.reason]} retries" if pause
                           else f"{wo['id']} worker turn failed"),
                    body=(turn.get("error") or "no error recorded")[:500],
                    level="warning", wo_id=wo["id"], source="reconciler",
                )
            return

        # The turn is done. Everything below decides what the work order does next.
        if store.queued_messages(wo["id"]):
            return  # the next turn goes out this tick; nothing has settled yet
        fresh = store.get_work_order(wo["id"])
        if fresh.get("result_summary"):
            if store.pending_assumptions(wo["id"]):
                if fresh["status"] != "needs_review":
                    store.set_status(wo["id"], "needs_review")
                    store.flag_attention(wo["id"], "assumptions pending review")
            elif fresh.get("pr_url"):
                # Finished behind a pull request: it is the user's merge that ends this
                # work order, not the worker's last turn. Settling it to `completed`
                # here would take it off the open list before anyone had merged it.
                #
                # UNLESS THE OS IS THE ONE THAT WOKE IT. A repair turn — a conflict or a
                # red build — returns the work order where it CAME FROM, which is not
                # always the merge queue. A `needs_review` order nudged about failing CI
                # still needs review, and parking it here would end the repair by
                # silently taking the item off the user's list: the OS would look like it
                # had handled something it had only half handled (issue #224, Neo
                # question 275). The flag comes back on its own once the status does —
                # INV-ATTENTION-MISSING re-derives it — so nothing has to be remembered
                # beyond the status itself.
                from . import ops as ops_mod

                # `resumed_from` is the same rule as `pr_repair_origin` for the other
                # way the OS takes a work order out of its status (issue #259): a
                # relaunched turn must not end by filing the review somebody still owes
                # as a merge-queue entry. It answers for `needs_review` alone — see it.
                back = (ops_mod.pr_repair_origin(store, wo["id"])
                        or ops_mod.resumed_from(store, wo["id"], turn["seq"])
                        or "waiting_pr_merge")
                if fresh["status"] != back:
                    store.set_status(wo["id"], back)
                    if back == "waiting_pr_merge":
                        store.clear_attention(wo["id"])
            else:
                store.set_status(wo["id"], "completed")
                store.clear_attention(wo["id"])
        elif store.pending_approvals(wo["id"]) or awaiting_neo(wo["id"]):
            # Parked on the delegate — a privileged-action gate awaiting a verdict, or a
            # question awaiting an answer. Either way the worker was TOLD to end its turn
            # and wait, so an idle worker here is compliance, not abandonment.
            #
            # The question half was missing, and the `else` below caught those instead:
            # a `jarvis wo ask` whose answer had not landed by the time the turn settled
            # was filed as `needs_review` + IDLE_NO_FINISH_BLOCKER and put in front of
            # the user, for a worker doing exactly what the contract asks of it (GitHub
            # issue 100). Only the tightness of the Neo drain loop kept that rare; a
            # slow or disabled Neo makes it every `wo ask`.
            if fresh["status"] != "waiting_input":
                store.set_status(wo["id"], "waiting_input")
        elif wo.get("kind") == "manager":
            # A project manager order is idle BY DESIGN: it acts on a message and ends
            # its turn, and between messages there is nothing for it to do. The default
            # below would file that as `needs_review` + IDLE_NO_FINISH_BLOCKER on the
            # manager's very first turn and again after every message it handled, so
            # every feature order in the fleet would carry a permanent false flag. It
            # never finishes itself either: `_close_feature_manager` completes it when
            # its feature settles.
            #
            # `idle`, NOT `waiting_input`, since issue #264. This branch used to park it
            # in the status that means "blocked on the user", which every surface renders
            # "Waiting on you" and which `ops.waiting_on` could explain only as an
            # unanswered permission prompt — impossible under the `auto` mode the fleet
            # runs. Suppressing the FLAG was never enough: six other surfaces re-derive
            # meaning from the status alone, which is kn-cffc8905's trap paid a third
            # time. A manager that genuinely ASKED for something stays in
            # `waiting_input`, which is where it belongs — see the guard below.
            #
            # UNLESS ITS FEATURE IS ALREADY OVER, and that ordering is one the feature
            # round machine makes reachable. A manager is created `pending` and claimed
            # by `dispatch_pending`; a feature settling on the VALIDATE thread can close
            # a manager the tick thread is claiming in the same moment, and the claim
            # lands last. Parking that manager in `waiting_input` would leave exactly the
            # row `_close_feature_manager` exists to prevent — an open work order against
            # a closed feature, with nothing left that would ever look at it again.
            # Re-derived from the feature rather than guarded with a lock: noticing is
            # what a reconciler is for.
            parent = wo.get("parent_id")
            feature = store.get_feature_order(parent) if parent else None
            if feature and feature["status"] in FO_TERMINAL_STATUSES:
                self._close_feature_manager(store, str(parent))
            elif something_is_out(store, wo["id"]):
                # HOLD IT WHERE IT IS. `waiting_input` is the only carrier of the fact
                # that this manager ASKED for something, and re-statusing it `idle` would
                # say the opposite — nothing to act on — about an order waiting for a
                # verdict: muted, out of FEATURED_STATUSES, and refused a nudge.
                #
                # NOT LEFT TO THE BRANCH ORDER ABOVE, which is two thirds of the same
                # question and looks like all of it. That `elif` reads `pending_approvals`
                # and misses `awaiting_case` — a gate request the worker filed by running
                # the command before arguing it, which `gates.file_request` parks here
                # just the same. Under the old code missing it cost nothing, because this
                # branch's write was `waiting_input` either way; since issue #264 it is a
                # rewrite, so the predicate has to be the whole one. `something_is_out` is
                # that predicate, shared with `invariants.end_wait_if_nothing_is_out` so
                # the two cannot drift (kn-4ea33fe6).
                pass
            elif fresh["status"] != "idle":
                store.set_status(wo["id"], "idle")
                # The one write the re-status cannot do on its own: a manager carried
                # over from before issue #264 may have been flagged while it was in
                # `waiting_input`, and INV-ATTENTION-PHANTOM only clears terminal rows,
                # so that flag would outlive the status it was derived from for ever.
                #
                # RE-DERIVED AGAINST THE NEW STATUS, never assumed. `true_blockers` is
                # read on the row AS IT NOW IS — an unconditional clear would drop a
                # blocker that survives the move, and reading `fresh` would re-derive the
                # old status's "worker is waiting on your input" and never clear at all.
                moved = store.get_work_order(wo["id"])
                if moved["needs_attention"] and not true_blockers(store, moved):
                    store.clear_attention(wo["id"])
        else:
            from .invariants import IDLE_NO_FINISH_BLOCKER

            store.set_status(wo["id"], "needs_review")
            store.flag_attention(wo["id"], IDLE_NO_FINISH_BLOCKER)

    def _park_on_signin(self, store: ProjectStore, wo: dict[str, Any],
                        pause: worker_session.TurnPause,
                        turn: dict[str, Any]) -> None:
        """Hold a work order whose turn died on authentication, until the user signs in.

        NOT `failed`, and that is the whole of `TurnPause.exhausted`: `failed` is a
        DEPENDENCY_DEAD_STATUS, so it strands dependents and fails the parent feature
        order for something a `/login` fixes — which is what happened to fo-e353491c on
        2026-08-27.

        `waiting_input` is what this state actually is, and it is the status that makes
        every existing surface work with no new exception. Still ACTIVE, so
        `retry_paused_turns` keeps sweeping it and no dependency edge treats it as dead;
        and in `invariants.BLOCKED_STATUSES`, so `true_blockers` can re-derive
        AUTH_BLOCKER — the obligation a flag raised only here would not meet.

        SAID ONCE, and the status is the guard. Every subsequent tick sees the same pause
        on an already-parked order, and a Telegram message per tick about one sign-in is
        how an attention strip earns being ignored. Each `turn_paused` event still lands
        on the timeline, so a resume that fails on auth again is on the record.
        """
        from .invariants import AUTH_BLOCKER

        if wo["status"] == "waiting_input":
            return
        store.set_status(wo["id"], "waiting_input")
        store.flag_attention(wo["id"], AUTH_BLOCKER)
        store.add_notification(
            # The title names the failure rather than the fact that there was one: this
            # is the line the user reads in Telegram, and "worker turn failed" sends them
            # looking for a bug in the work instead of at their own login.
            title=f"{wo['id']} parked — Claude Code could not authenticate",
            body=(turn.get("error") or pause.message)[:500],
            level="warning", wo_id=wo["id"], source="reconciler",
        )

    # -- 6. pull requests parked on a human ------------------------------------------

    def poll_pull_requests(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Ask GitHub what happened to the pull requests this project is parked behind.

        `settle_work_order` can only see as far as the worker's last turn, and a work
        order that ends in a pull request outlives that turn: the merge is the real
        ending and it happens somewhere Jarvis cannot see. This is the one step that
        looks outside the machine, and it exists so the user does not have to type
        `jarvis wo done` after every merge they already performed.

        Five answers, from `github.pr_view`:

        * **merged** — the work landed; the work order ends (`ops.complete_merged`).
        * **closed, unmerged** — someone refused the work; it goes to `needs_review`
          and asks for the user (`ops.record_pr_closed`).
        * **open and conflicting** — the worker is asked to resolve it, so the user
          never has to (`ops.PR_CONFLICT`, and the whole of
          docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md).
        * **open with a failing check** — the same, with a different message
          (`ops.PR_CHECKS`, and
          docs/superpowers/specs/2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md).
          A check that is merely not green is NOT this: `github.RED_CONCLUSIONS`.
        * **open, mergeable and green** — nothing to do, and nothing written unless a
          repair episode is being closed.

        THE LAST ONE IS THE BUDGET, because it is the overwhelmingly common case: one
        `gh` call and THREE indexed reads per pull request, no write. The three are one
        per question this branch has to ask the timeline — was a closure already
        reported (`pr_closure_told`), is a conflict episode open, is a checks episode
        open — and they are reads of `wo_events` by `(wo_id, kind)`, not scans. Nothing
        else on the path touches the database: the work-order row itself is re-read only
        when a clear has just run, and the step's `list_work_orders` is one query for the
        whole project however many pull requests it has.

        That sentence used to say "one indexed read" and had been false since this body
        was rewritten. It is a claim worth keeping honest rather than deleting —
        `tests/test_pr_checks.py` counts the statements, so a fourth read fails a test
        instead of quietly costing the fleet a query every two minutes per open pull
        request.

        **The automatic merge costs that case NOTHING, and its own cost is counted too.**
        `Daemon.auto_merge` returns on `project.validation.auto_merge` before it reads
        anything, so a project that has not opted in — the shipped state of every project
        — pays exactly the budget above. A project that HAS opted in pays THREE more
        indexed reads, and only on a work order actually PARKED behind its pull request:
        the latest validation round (read once and used for both the predicate and the
        wording), its pending assumptions, and the `automerge_held` events the hold
        dedupes against. A `needs_review` order with a pull request pays none of them.
        One more (`latest_approval_for`) arrives only once a merge is armed, which is a
        state a pull request passes through once. Both figures are counted by their own
        test beside the one above, on the rule that a budget nobody executes is a comment
        rather than a guarantee.

        EVERY STATUS THAT CARRIES A PULL REQUEST IS POLLED, not `waiting_pr_merge`
        alone. A work order that escalated into `needs_review` behind a red build was
        invisible here, which is exactly when the user is about to look at it and decide
        whether to merge (issue #224). `PR_POLL_STATUSES` says which, and why the
        in-flight ones are left out.

        Hidden work orders are polled too. Hiding drops a record from listings and the
        attention list; it does not mean the record may go on saying something untrue.

        The step is skipped whole when nothing is parked — one indexed query — so a
        fleet with no open pull requests never spawns a subprocess for this.
        """
        parked = [wo for wo in store.list_work_orders(statuses=PR_POLL_STATUSES,
                                                      include_hidden=True)
                  if wo.get("pr_url")]
        if not parked:
            return
        from . import github, ops

        for wo in parked:
            try:
                pr = github.pr_view(wo["pr_url"], cwd=project.path)
            except github.GitHubError as e:
                # One unreadable pull request must not hide the others: a deleted repo
                # or a typo'd URL is a per-work-order problem, and a missing `gh` will
                # simply fail again on the next one. Either way the user hears once.
                log.debug("[%s] could not read %s for %s: %s", project.name,
                          wo["pr_url"], wo["id"], e)
                self._warn_pr_poll_broken(project, store, e)
                continue
            except Exception:  # noqa: BLE001 — never let one work order stall the rest
                log.exception("[%s] polling %s failed", project.name, wo["id"])
                continue
            try:
                if pr.merged:
                    # Deliberately silent. `route_new_inbox` has no level filter, so an
                    # "info" row here would Telegram the user on every merge — about a
                    # merge they just performed themselves. `jarvis wo done` announces
                    # nothing either, and this is the same event with the typing removed.
                    # The timeline, the status change and `jarvis status` carry it.
                    log.info("[%s] %s merged — completing %s", project.name,
                             wo["pr_url"], wo["id"])
                    ops.complete_merged(store, wo, merged_at=pr.merged_at,
                                        head_oid=pr.head_oid)
                elif pr.closed_unmerged:
                    # ONCE PER CLOSURE. Before the poll widened, `record_pr_closed` moved
                    # the work order to `needs_review` and out of the polled set, so
                    # re-running was unreachable; now the closed order stays in it and
                    # would write a `pr_closed` event every couple of minutes for ever.
                    #
                    # Derived from the timeline, NOT from `pr_state`: that column is
                    # stale by construction and has one permitted reader (kn-dbc4971d),
                    # and reading it here would mean a pull request closed, reopened and
                    # closed again never told the user the second time — this bug's own
                    # silence, reintroduced by the guard against it.
                    if not store.pr_closure_told(wo["id"]):
                        log.info("[%s] %s closed unmerged — %s needs the user",
                                 project.name, wo["pr_url"], wo["id"])
                        ops.record_pr_closed(store, wo)
                elif self._note_reopened(project, store, wo):
                    # The pull request is OPEN and the record still says it was refused.
                    # Nothing used to write this, so `pr_state` said CLOSED for ever
                    # (the gap kn-a94cbd68 filed) and `PR_CLOSED_BLOCKER` went on being
                    # derived from a fact that had stopped being true. Recorded before
                    # any repair below, because both of those would be nudging a worker
                    # about a pull request whose status line still called it dead.
                    log.info("[%s] %s is open again — %s", project.name,
                             wo["pr_url"], wo["id"])
                elif pr.conflicting:
                    self.heal_pull_request(project, store, wo, ops.PR_CONFLICT,
                                           "conflicts",
                                           base=pr.base_ref or "its base branch")
                    # ...and say what is holding the merge NOW. Issue #263: repairing
                    # used to skip `auto_merge` entirely, so the auto-merge line kept
                    # naming whatever held last — CI, while the real blocker was this
                    # conflict. `record_only` cannot merge or propose.
                    self.auto_merge(project, store, wo, pr, record_only=True)
                elif pr.failing:
                    self.heal_pull_request(
                        project, store, wo, ops.PR_CHECKS,
                        f"has failing checks ({', '.join(pr.failing)})",
                        failing=", ".join(pr.failing),
                        # BEHIND rides along with a nudge that was going out anyway and
                        # never causes one: spec §5.
                        behind=(ops.PR_BEHIND_NOTE.format(base=pr.base_ref or "its base")
                                if pr.behind else ""))
                    self.auto_merge(project, store, wo, pr, record_only=True)
                else:
                    healed = pr.mergeable_now and ops.clear_pr_repair(
                        store, wo, ops.PR_CONFLICT)
                    if healed:
                        log.info("[%s] %s merges again — %s stopped conflicting",
                                 project.name, wo["pr_url"], wo["id"])
                    # Not `elif`: a pull request can stop conflicting and go green in the
                    # same poll, and the two episodes are separate budgets.
                    #
                    # `checks_green`, NOT "nothing is failing" — see the property. The
                    # tick right after a worker pushes its fix has every check QUEUED,
                    # and closing the episode there would hand a fix that does not work
                    # three fresh attempts every round.
                    if pr.checks_green:
                        # Re-read ONLY if the clear above ran, because that is the only
                        # thing that can have moved `attention_reason` under us, and
                        # `clear_pr_repair` decides on it. An unconditional re-read cost
                        # a row lookup on every healthy pull request in the fleet, every
                        # two minutes, to notice a change that provably had not happened.
                        row = store.get_work_order(wo["id"]) if healed else wo
                        if ops.clear_pr_repair(store, row, ops.PR_CHECKS):
                            log.info("[%s] %s is green again — %s stopped failing",
                                     project.name, wo["pr_url"], wo["id"])
                    # AFTER the repairs clear, and inside the same branch: a pull request
                    # the OS is still nudging a worker about is not one it may merge.
                    # The repair branches above call this too, `record_only` — which
                    # writes the hold and merges nothing, so that rule is untouched.
                    self.auto_merge(project, store, wo, pr)
            except Exception:  # noqa: BLE001
                log.exception("[%s] settling %s against its PR failed", project.name,
                              wo["id"])

    def auto_merge(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                   pr: Any, *, record_only: bool = False) -> None:
        """Merge this pull request, if six positive facts line up. Usually: do nothing.

        docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md. The decision is
        `automerge.decide` and lives there, pure; this is the half that has a database, a
        clock and a subprocess. Three shapes come out of it — armed with a live grant
        (merge, then complete the work order), armed with no grant (ask Neo), or held
        (say so once, and leave the pull request for the user).

        **THE CONFIG AND STATUS CHECKS ARE FIRST AND THEY ARE NOT THE RULE.** The rule is
        conditions 1 and 2 of `decide`, checked again there and unit-tested there. The
        redundancy is deliberate and is the shape `two-gates-not-a-chain` argues for: a
        project's permission is asserted at both the site that spends and the site that
        decides. What they buy HERE is that a work order the mechanism has no business
        touching is never touched at all —

        * config: a fleet that has not opted in pays no indexed read per parked pull
          request every two minutes for a feature it does not use;
        * status: `PR_POLL_STATUSES` is wider than `waiting_pr_merge` on purpose (issue
          #224 — a `needs_review` order behind a red build must still be polled), so
          without this an order sitting in `needs_review` behind a GREEN pull request
          would reach `decide`, hold on `HELD_STATUS`, and have that hold rendered by
          `ops.automerge_state` as "auto-merge: held — the work order is needs_review".
          The user would be told the automatic merge declined a pull request it was never
          a candidate for, on the one surface that exists to say what the mechanism did.
          `_note_automerge_held` drops `HELD_STATUS` as well, for the same reason from
          the other side.

        **`record_only=True` DECIDES AND RECORDS THE HOLD, AND STOPS THERE.** It is how
        the two repair branches of `poll_pull_requests` keep the auto-merge line honest
        while a worker is being nudged, and issue #263 is why they have to: the hold
        `ops.automerge_state` renders is the last one WRITTEN, so a pull request that
        holds on CI and then stops merging cleanly went on naming CI — the branch that
        saw the conflict never called this at all. A hold is a sentence about a pull
        request, not permission to touch it, so recording one costs the repair nothing.

        Nothing arms under it, and nothing could: `github.PullRequest.conflicting` means
        `mergeable` is not `MERGEABLE`, and a failing check means `checks_green` is false,
        so `decide` holds on conditions 6b and 6c respectively before a grant is ever
        looked up. The flag is belt to that braces — a caller that is repairing must not
        be able to merge even if that implication is one day broken.

        `project.validation` resolves per project — a project that names `auto_merge`
        keeps its answer, one that does not takes the fleet's, and the shipped answer at
        both levels is `false`. So the authority is granted one project at a time.

        Never raises: the caller's `except` would catch it, but a pull request that could
        not be merged must leave the work order exactly where it was, and "exactly where
        it was" is the behaviour the whole feature is an optimisation over.
        """
        from . import automerge, ops

        cfg = project.validation
        if not (cfg.enabled and cfg.auto_merge):
            return
        if wo["status"] != "waiting_pr_merge":
            return
        wo_id = wo["id"]
        # ONE read of the round, used twice: `ProjectStore.validated_head` turns it into
        # the predicate and `decide` reads it again only for the wording. Reading it twice
        # would let the validator — another thread, opening rounds while this poll runs —
        # slip a new round between the two and produce the pair (passed, no head), whose
        # hold is deduped for ever on a reason that was never true.
        round_row = store.latest_validation_round(wo_id=wo_id)
        decision = automerge.decide(
            round_row, wo, pr, cfg,
            validated_head=store.validated_head(round_row),
            pending_assumptions=bool(store.pending_assumptions(wo_id)))
        if not decision.armed:
            self._note_automerge_held(store, wo_id, decision)
            return
        if record_only:
            return          # unreachable: a repairing pull request cannot arm, see above

        approval = store.latest_approval_for(
            wo_id, automerge.GATE_KIND,
            automerge.merge_command(str(wo.get("pr_url") or ""), decision.judged_sha))
        if approval is not None and approval["status"] != "approved":
            # Filed and not granted: pending with Neo, escalated to the user, denied,
            # expired. Every one of those is somebody's business and none of them is
            # this loop's — and `propose` would refuse anyway, after opening a NeoStore
            # to find that out, every two minutes for as long as the PR stays open.
            return
        if approval is None:
            # Not yet permitted. `propose` is idempotent per command string — and the
            # string carries the judged sha — so a pending, escalated or REFUSED request
            # is left standing rather than re-asked every two minutes.
            #
            # The NeoStore is opened here and closed here: a sqlite connection belongs to
            # the thread that made it (`_validate_work_order`'s reason), and the
            # overwhelmingly common tick proposes nothing and must not pay for a second
            # handle to find that out.
            from .neo_store import NeoStore

            neo_store = NeoStore()
            try:
                automerge.propose(store, neo_store, project.name, wo, decision,
                                  checks=tuple(c["name"] for c in pr.checks))
            finally:
                neo_store.close()
            return
        # No attempt cap here: `automerge.GRANT_USES` is 1, so an approved-but-spent grant
        # refuses inside `apply` without reaching GitHub, and that is the bound. See
        # `automerge.attempts` for why spec §8's counter would enforce nothing.
        try:
            merged = automerge.apply(store, wo, decision.judged_sha, approval,
                                     cwd=project.path)
        except automerge.MergeFailed as exc:
            # It reached GitHub and GitHub said no. Counted, recorded and — once per
            # commit — reported.
            log.warning("[%s] auto-merge of %s failed: %s", project.name, wo_id, exc)
            store.add_event(wo_id, "automerge_failed", {
                "head_sha": decision.judged_sha, "approval_id": approval["id"],
                "reason": str(exc), "pr_url": wo.get("pr_url")})
            self._warn_automerge_failed(project, store, wo, decision, str(exc))
            return
        except automerge.AutoMergeRefused as exc:
            # NOTHING WAS ATTEMPTED, so nothing is recorded and nothing is counted. This
            # is the ordinary steady state of a commit whose one authorised attempt has
            # been made: `automerge.GRANT_USES` is 1, so the grant is spent and every
            # later poll lands here. Writing an `automerge_failed` event for it would
            # claim a merge failure that never happened, three polls' worth per commit,
            # and hand the user's inbox row "the grant is spent" as the reason a merge
            # failed instead of the 403 that actually caused it.
            log.debug("[%s] auto-merge of %s not attempted: %s", project.name, wo_id,
                      exc)
            return
        except Exception:  # noqa: BLE001 — a merge must never kill the tick
            log.exception("[%s] auto-merging %s failed", project.name, wo_id)
            return
        log.info("[%s] auto-merged %s — completing %s", project.name, wo["pr_url"],
                 wo_id)
        if merged["after_merge_cause"]:
            # THE MERGE LANDED AND THE COMMAND THAT MADE IT DID NOT SUCCEED (issue #253,
            # spec §5.5). A note on the timeline and nothing else: not `automerge_failed`,
            # which is a claim about the PULL REQUEST, and no inbox row, because neither
            # cause needs a person and the work order completes below either way. The
            # kind comes from the cause — `automerge.AFTER_MERGE_EVENT`'s reason.
            store.add_event(wo_id,
                            automerge.AFTER_MERGE_EVENT[merged["after_merge_cause"]], {
                "head_sha": decision.judged_sha, "approval_id": approval["id"],
                "reason": merged["after_merge_error"], "pr_url": wo.get("pr_url")})
        # No `merged_at`: that column holds GITHUB's `mergedAt`, and this path has not
        # asked GitHub anything since the merge. The `automerge_merged` event's own
        # timestamp is when it landed, and inventing a local clock reading for a remote
        # field would be the record claiming a fact it does not have.
        #
        # `head_oid` IS known, and exactly: `--match-head-commit` means GitHub merged this
        # commit or merged nothing. Passing it keeps an OS-merged order on the landing
        # sweep's exact answer to issue #232's Mode C instead of dropping it to the
        # content heuristic (`landing.assess`).
        ops.complete_merged(store, wo, head_oid=decision.judged_sha,
                            automerge={"approval_id": merged["approval_id"],
                                       "round_id": decision.round_id,
                                       "round": decision.round_n,
                                       "head_sha": decision.judged_sha})

    def auto_review(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Put this project's pending assumptions to Neo, one question each. Usually: none.

        docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md. `Daemon.auto_merge`'s
        shape one authority along: the decision is `autoreview.decide` and lives there,
        pure; this is the half that has a database and a model queue. It ASKS and nothing
        more — the ruling arrives asynchronously through the Neo drain and is applied by
        `_deliver_assumption_verdict`, so nothing here can settle anything.

        **THE CONFIG GUARD IS FIRST AND IT IS NOT THE RULE.** Condition 1 of `decide` is
        the rule, checked and unit-tested there; this is `two-gates-not-a-chain`'s
        redundancy, and what it buys is that a fleet which has not opted in pays two
        attribute reads per project per reconcile tick for a feature it does not use,
        rather than a listing and a read per order on it.

        **NEO'S OWN SWITCH IS READ TOO.** Neo is the reviewer here, so a fleet with
        `os.neo.enabled` off would file questions nothing will ever drain — assumptions
        parked behind a queue that does not run, which is worse than not asking.

        Never raises past one work order: an assumption the OS could not ask about stays
        pending and its order stays where it is, which is the behaviour this whole
        feature is an optimisation over — and one bad order must not cost the rest of the
        project its pass.
        """
        cfg = project.validation
        if not (cfg.enabled and cfg.auto_review):
            return
        if not self.catalog.os.neo.enabled:
            return
        from .neo_store import NeoStore

        # Hidden orders are excluded by the default, and deliberately: the rule
        # `ProjectStore.pending_assumptions`' fleet-wide query already applies — a hidden
        # order's assumptions are not asking for the user, so there is nothing here to
        # take off them and no reason to spend a call finding out.
        #
        # `all_assumptions` once per candidate, reused for the decision, the numbering
        # and the siblings the question carries.
        candidates = []
        for wo in store.list_work_orders(statuses=("needs_review",)):
            rows = store.all_assumptions(wo["id"])
            if any(a["status"] == "pending" for a in rows):
                candidates.append((wo, rows))
        if not candidates:
            return
        # A sqlite connection belongs to the thread that made it
        # (`_validate_work_order`'s reason), so it is opened and closed here.
        neo_store = NeoStore()
        try:
            for wo, assumptions in candidates:
                try:
                    self._review_assumptions_of(project, store, neo_store, wo, cfg,
                                                assumptions)
                except Exception:  # noqa: BLE001 — one order must never stop the rest
                    log.exception("[%s] auto-review of %s failed", project.name,
                                  wo["id"])
        finally:
            neo_store.close()

    def _review_assumptions_of(self, project: ProjectSpec, store: ProjectStore,
                               neo_store: Any, wo: dict, cfg: Any,
                               assumptions: list[dict]) -> None:
        """One work order's assumptions, each decided on its own. See `auto_review`."""
        from . import autoreview, ops

        # ONE read of the round and one of the refusal history for the whole list: both
        # are facts about the ORDER rather than about an assumption, and re-reading them
        # per row would let the validator — running on another thread — change the answer
        # half way down a list that is meant to be judged against one state.
        latest = store.latest_validation_round(wo_id=wo["id"])
        outcome = str((latest or {}).get("outcome") or "")
        answered = ops.refusal_answered(store, wo["id"])
        for a in assumptions:
            decision = autoreview.decide(a, wo, cfg, round_outcome=outcome,
                                         refusal_answered=answered)
            if not decision.armed:
                self._note_autoreview_held(store, wo["id"], decision)
                continue
            autoreview.propose(store, neo_store, project.name, wo, a, assumptions)

    def _note_autoreview_held(self, store: ProjectStore, wo_id: str,
                              decision: Any, *, settling: bool = False) -> None:
        """Record ONCE, per (assumption, reason), that the OS declined to decide.

        `_note_automerge_held`'s reasoning verbatim: the pass runs every reconcile tick
        for as long as the work order sits there, and a timeline that accrued an event
        per tick would bury the events that mean something. Keyed on the reason as well
        as the assumption, because the hold that matters most — "this mentions
        production" — can follow one that does not.

        **DELIBERATELY NOT AN ATTENTION ITEM.** A held assumption is one the user decides
        themselves, which is what they did for every assumption before this existed, and
        the work order is already on their list carrying `assumptions pending review`.

        FOUR HOLDS ARE NOT RECORDED, and all four are things the reader would be told
        about a mechanism that was never a candidate — the asymmetry `_note_automerge_held`
        names, where a missing hold event says nothing and a spurious one is deduped for
        ever and then rendered:

        * `disabled` — the project never opted in, so its timeline must not carry a line
          about a mechanism it does not have;
        * `status` — unreachable from this pass, which lists `needs_review` only;
        * `settled` — the assumption is decided, and saying the OS declined to decide a
          decided thing is noise on every work order the user has ever reviewed;
        * `asked` — the question is already filed, which is the pass working.

        **`settling=True` SUSPENDS ALL FOUR, and the difference is not cosmetic.** Every
        exclusion above rests on "this order was never a candidate" — true of the ask
        pass, which lists `needs_review` and nothing else. The settle site re-runs the
        same table against state read after the model call, and there `status` means the
        user CANCELLED the order between the ask and the ruling and `disabled` means they
        revoked the permission. Those are the two the record most needs, and dropping
        them would leave the OS's decision not to act as the one thing it never wrote
        down.
        """
        from . import autoreview, db

        if not settling and decision.code in (
                autoreview.HELD_DISABLED, autoreview.HELD_STATUS,
                autoreview.HELD_SETTLED, autoreview.HELD_ASKED):
            return
        key = (decision.assumption_id, decision.code)
        for event in store.events_of_kind(wo_id, "autoreview_held"):
            payload = db.from_json(event["payload"], {})
            if (int(payload.get("assumption_id") or 0),
                    str(payload.get("code") or "")) == key:
                return
        store.add_event(wo_id, "autoreview_held", {
            "code": decision.code, "reason": decision.reason,
            "assumption_id": decision.assumption_id, "n": decision.n})

    def _deliver_assumption_verdict(self, central: CentralStore, neo_store: Any,
                                    pstore: ProjectStore | None, q: dict,
                                    verdict: dict) -> None:
        """Apply Neo's ruling on ONE assumption: accept it, or leave it with the user.

        `_deliver_plan_verdict`'s shape — resolve the subject from the question, check it
        is still open, apply through the same `ops` the user's own command goes through —
        and its one hard rule: **the code can override Neo toward the user and never away
        from it.** `autoreview.read_ruling` is where that override lives.

        **THE WHOLE CONDITION TABLE IS RE-RUN BEFORE ACCEPTING, NOT JUST "IS IT STILL
        PENDING".** `autoreview.decide` is asked twice: once to justify spending a model
        call, and once, against freshly read state, to justify the settle it came back
        to do. Only the second one is guarding anything irreversible.

        A ruling that does not accept is NOT an inbox row. The work order is already on
        the user's attention list carrying "assumptions pending review" — the state it was
        in before the OS asked and the state it stays in — and a second announcement of an
        unchanged fact is the attention cost this feature exists to reduce.

        An ACCEPTANCE that clears the LAST pending assumption IS one row, at `info`. That
        is the moment a decision the user used to make stops being theirs and the work
        order leaves their list, and it is the only trace they would otherwise get: the
        order simply completes. One row per work order rather than per assumption, because
        what changed for the user is the work order.
        """
        from . import autoreview, ops

        if pstore is None:
            log.error("assumption verdict for question %s has no project store", q["id"])
            return
        assumption = pstore.assumption_for_question(q["id"])
        if assumption is None:
            log.warning("assumption question %s rules on nothing (deleted?)", q["id"])
            return
        if assumption["status"] != "pending":
            # The user got there first through `jarvis wo review`. Their decision stands;
            # `invariants.check_neo_escalations_are_live` closes the question behind them.
            log.info("assumption question %s: assumption %s is already %s, dropping "
                     "Neo's ruling", q["id"], assumption["id"], assumption["status"])
            return
        try:
            wo = pstore.get_work_order(q["wo_id"])
        except KeyError:
            return
        # `n` is a position in the work order's list and is derived, never stored
        # (`all_assumptions`), so the row fetched by question id does not carry one.
        numbered = next((a for a in pstore.all_assumptions(wo["id"])
                         if a["id"] == assumption["id"]), assumption)
        ruling = autoreview.read_ruling(verdict,
                                        default_model=self.catalog.os.neo.model)
        if not ruling.accept:
            if not verdict.get("escalate"):
                # NEO ANSWERED AND THE ANSWER IS NOT WHAT HAPPENS, so `drain_queue` has
                # already recorded it as `answered`. Re-marking keeps `jarvis neo list`,
                # `jarvis status` and the work order's record telling one story: this is
                # the user's to decide — `_deliver_plan_verdict`'s over-cap branch, for
                # the same reason. Both shapes reach here: a `deny` (there is no machine
                # rejection, so it becomes an escalation) and an acceptance this module
                # overrode on stakes.
                neo_store.mark(q["id"], "escalated", reason=ruling.reason)
            pstore.add_event(wo["id"], "autoreview_escalated", {
                "assumption_id": assumption["id"], "n": numbered.get("n"),
                "reason": ruling.reason, "stakes": ruling.stakes,
                "model": ruling.model, "neo_question_id": q["id"],
                "overridden": ruling.overridden})
            log.info("auto-review left assumption #%s of %s with the user: %s",
                     numbered.get("n"), wo["id"], ruling.reason)
            return

        project = next((p for p in self.catalog.projects if p.name == q["project"]), None)
        if project is None:
            return

        # THE ASK DID NOT FREEZE ANYTHING, and accepting is the step that cannot be taken
        # back: it clears the assumption, and `ops.land_when_cleared` lands the order
        # behind it — with auto-merge on, all the way to merged. A model call is seconds
        # to minutes wide, and in that window the panel can go `pending` -> `escalated`
        # (arming on `pending` is explicitly allowed, so this is reachable, not
        # theoretical), the user can cancel the order, or a refusal can arrive on a
        # sibling. So the whole condition table is re-run against state read NOW —
        # `wo` came from `get_work_order` above, the round and the refusal from these two
        # reads — and Neo's ruling is dropped if it no longer arms. `asked_question_id`
        # excludes the question being delivered from condition 6; see `autoreview.decide`.
        latest = pstore.latest_validation_round(wo_id=wo["id"])
        still = autoreview.decide(
            numbered, wo, project.validation,
            round_outcome=str((latest or {}).get("outcome") or ""),
            refusal_answered=ops.refusal_answered(pstore, wo["id"]),
            asked_question_id=int(q["id"]))
        if not still.armed:
            # Escalated rather than left `answered`: the assumption is the user's again,
            # and `/neo` and `jarvis neo list` have to say so — the same re-mark the
            # non-acceptance branch above does, for the same reason. The hold event is
            # what puts the WHY on the work order, where `ops.autoreview_state` renders
            # it as the one `⚙ auto-review:` line.
            neo_store.mark(q["id"], "escalated", reason=still.reason)
            self._note_autoreview_held(pstore, wo["id"], still, settling=True)
            pstore.add_event(wo["id"], "autoreview_escalated", {
                "assumption_id": assumption["id"], "n": numbered.get("n"),
                "reason": still.reason, "stakes": ruling.stakes,
                "model": ruling.model, "neo_question_id": q["id"],
                "overridden": False, "dropped": still.code})
            log.info("auto-review dropped its ruling on assumption #%s of %s: %s",
                     numbered.get("n"), wo["id"], still.reason)
            return
        try:
            out = ops.accept_assumption(
                pstore, project.path, wo, numbered, reason=ruling.reason,
                model=ruling.model, question_id=int(q["id"]),
                cfg=project.validation)
        except Exception:  # noqa: BLE001 — one ruling must never kill the drain
            log.exception("accepting assumption %s of %s failed", assumption["id"],
                          wo["id"])
            return
        log.info("auto-review accepted assumption #%s of %s (%s left) — %s",
                 numbered.get("n"), wo["id"], out["pending"], out["status"])
        if not out["settled"]:
            return
        decided = [a for a in pstore.all_assumptions(wo["id"])
                   if a.get("decided_by") == autoreview.DECIDER]
        central.add_inbox(
            project=q["project"], level="info",
            title=f"The OS decided {len(decided)} assumption(s) for you on {wo['id']}",
            body=f"{wo.get('title') or ''}\n"
                 f"{wo['id']} is now {out['status']} — it is off your review list.\n"
                 f"What it accepted, and why: jarvis wo show {wo['id']}\n"
                 f"If any of it was wrong: jarvis neo review {q['id']} "
                 f"--correct \"…\" teaches Neo not to do it again.",
            wo_id=wo["id"])

    def _note_automerge_held(self, store: ProjectStore, wo_id: str,
                             decision: Any) -> None:
        """Record ONCE, per (commit, reason), that the OS declined to merge.

        Deduped because a parked pull request is polled every two minutes for as long as
        it is open, and a timeline that accrued an event per tick would bury the events
        that mean something. Keyed on the reason as well as the commit — the spec keys on
        the commit alone — because the two holds a reader most needs to tell apart happen
        at the SAME commit: "CI has not finished" is a wait, and "the panel has not
        passed this" is not, and one of them arriving after the other is the news.

        THE REASON'S TEXT IS IN THE KEY, not just its code, and issue #263 is why: a code
        is coarser than the sentence it names. `ops.automerge_state` renders the NEWEST
        hold, so a changed reason this function drops is a user reading a hold that has
        stopped being true — sent to look at CI for a merge conflict. `automerge`'s codes
        are one per condition for the same reason; the text catches what a code cannot,
        which is a condition whose wording carries the value (`BEHIND` against `DIRTY`,
        round 2 rejected against round 3). A reason is built from a bounded vocabulary
        plus the commit already in the key, so this stays a handful of rows per commit.

        **DELIBERATELY NOT AN ATTENTION ITEM.** A held auto-merge means the user merges
        this one by hand, which is what they did for every pull request before this
        existed. A heal-loop push invalidating a pass is ordinary, and the attention list
        is not a place to put ordinary.

        **TWO HOLDS ARE NOT RECORDED**, and they are the two `Daemon.auto_merge` already
        returned on, so neither is reachable from the poll. They are dropped here as well
        because the cost of being wrong is asymmetric and permanent: a missing hold event
        says nothing, while a spurious one is deduped for ever and is rendered to the user
        by `ops.automerge_state`.

        * `disabled` — the project has not opted in, so its timeline must not carry a
          line about a mechanism it never enabled;
        * `status` — the work order is not parked behind its pull request. Everything
          from `PR_POLL_STATUSES` reaches the poll (issue #224), so this would otherwise
          fire on every `needs_review` order with a green pull request and tell the user
          the automatic merge declined something it was never asked about.
        """
        from . import automerge, db

        if decision.code in (automerge.HELD_DISABLED,   # both unreachable via the poll:
                             automerge.HELD_STATUS):    # `auto_merge` returns before here
            return
        key = (decision.head_sha, decision.code, decision.reason)
        for event in store.events_of_kind(wo_id, "automerge_held"):
            payload = db.from_json(event["payload"], {})
            if (str(payload.get("head_sha") or ""), str(payload.get("code") or ""),
                    str(payload.get("reason") or "")) == key:
                return
        store.add_event(wo_id, "automerge_held", {
            "code": decision.code, "reason": decision.reason,
            "judged_sha": decision.judged_sha, "head_sha": decision.head_sha,
            "round": decision.round_n})

    def _warn_automerge_failed(self, project: ProjectSpec, store: ProjectStore,
                               wo: dict, decision: Any, reason: str) -> None:
        """One inbox row for the FIRST failed merge of a commit. Not again for that commit.

        **This is a deliberate deviation from spec §7, which reports at the third
        attempt.** That threshold assumed a retry budget, and there is none:
        `automerge.GRANT_USES` is 1 and `automerge.propose` files at most one gate request
        per judged commit, so a commit gets exactly one authorised attempt. "Report at
        three" would therefore report never, and a merge that failed on a missing write
        scope — the likeliest cause by far, spec §7's own table — would be silent for
        ever. Failing closed means the refusal is VISIBLE, so the row goes out the first
        time GitHub says no.

        Deduped on the commit all the same, so that a future path which does authorise a
        second attempt at the same diff cannot say the same thing twice.

        The work order is NOT flagged and NOT moved: it stays in `waiting_pr_merge` with
        its link, and merging it by hand works exactly as it always has.
        """
        from . import automerge

        if automerge.attempts(store, wo["id"], decision.judged_sha) != 1:
            return          # the caller records the event first, so 1 == this one
        self.central.add_inbox(
            project=project.name, level="warning",
            title=f"The OS could not merge the pull request for {wo['id']}",
            body=f"{wo.get('pr_url')}\n"
                 f"The panel passed it and the merge was authorised, but GitHub refused "
                 f"it on commit {decision.judged_sha[:10]}: {reason}\n"
                 f"Nothing was merged and nothing is stuck — merging it yourself works "
                 f"exactly as it always has.",
            wo_id=wo["id"])

    def _note_reopened(self, project: ProjectSpec, store: ProjectStore,
                       wo: dict) -> bool:
        """This open pull request was recorded as closed: say so, once. True if it was.

        The re-arming half of `ProjectStore.pr_closure_told` — without it that guard
        would latch on the first closure and the second refusal would never reach the
        user. It also clears `pr_state`, which is the column kn-dbc4971d describes as
        stale BECAUSE this poll "never clears what it wrote": `PR_CLOSED_BLOCKER` is
        derived from it, so leaving it would keep asserting a refusal that has been
        withdrawn.

        THE STATUS IS NOT TOUCHED. The work order is in `needs_review` because somebody
        refused the work, and reopening a pull request does not decide what to do about
        that — moving it back to the merge queue would take the decision off the user's
        list on the strength of a button press. What the user gets is a true reason line
        and a timeline entry; the rest is theirs.
        """
        if not store.pr_closure_told(wo["id"]):
            return False
        store.add_event(wo["id"], "pr_reopened", {"pr_url": wo.get("pr_url"),
                                                  "status": wo["status"]})
        store.update_work_order(wo["id"], pr_state=None)
        if wo["attention_reason"] == invariants_mod.PR_CLOSED_BLOCKER:
            # RELABELLED, not cleared. The order is still in `needs_review` and still
            # owes the user a decision, so clearing would drop the flag for the tick
            # until INV-ATTENTION-MISSING put it back — churn, and a violation logged
            # every time this works correctly. Re-derived here rather than guessed,
            # under the rule PR_CLOSED_BLOCKER's own note states.
            fresh = store.get_work_order(wo["id"])
            blockers = invariants_mod.true_blockers(store, fresh)
            if blockers:
                store.flag_attention(wo["id"], blockers[0])
            else:
                store.clear_attention(wo["id"])
        return True

    def heal_pull_request(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                          repair: Any, what: str, **fields: Any) -> None:
        """A pull request the OS can ask its own worker to fix: conflicts, or a red build.

        ONE function for both, because they are one mechanism — see `ops.PrRepair`. The
        four guards are all this adds over `ops.nudge_pr_repair`: no session to resume,
        a nudge already queued, a turn already in flight, and a validation round that
        owns the work order. Spec §3 for why each of the first three would otherwise
        cost a duplicated turn or silently spend the budget, and §4.1 for the fourth.
        """
        from . import ops

        if not wo.get("session_id"):
            return
        if store.queued_messages(wo["id"]) or worker_session.busy(store, wo["id"]):
            return
        # THE ROUND MACHINE OWNS THIS SESSION. Keyed off the ROUND, not the status, and
        # that distinction is the whole guard: since issue 212 a round runs while the
        # user decides, so `ops.land_when_cleared` parks a work order in `needs_review`
        # with its round still open — a status this poll now selects. Both loops would
        # then claim the same work order in the same moment, the round runner sending
        # the panel's feedback while this sends a repair nudge, and the branch head
        # moving under the seats mid-round. `busy` cannot see it: the worker's session
        # is idle while the panel deliberates. Deferred, never dropped — the work order
        # re-enters this poll on the tick after the round settles, and the pull request
        # is still red. Neo question 283.
        if store.validation_round_open(wo["id"]):
            log.debug("[%s] %s has a validation round open — repair deferred",
                      project.name, wo["id"])
            return
        out = ops.nudge_pr_repair(store, wo, repair, **fields)
        if out["gave_up"]:
            log.info("[%s] %s still %s after %s attempts — %s needs the user",
                     project.name, wo["pr_url"], what, out["attempts"], wo["id"])
        else:
            log.info("[%s] %s %s — asking %s to fix it (attempt %s)",
                     project.name, wo["pr_url"], what, wo["id"], out["attempts"])

    def _deliver_triage_verdict(self, central: CentralStore, q: dict,
                                verdict: dict) -> None:
        """Route a filed bug now that Neo has ruled on the priority it was claimed at.

        The filing agent's rating is a claim; this is the verdict. `issues.settle_triage`
        holds the whole decision — what gets a work order, what stays in the backlog, and
        what the tracker is told — so this is only the part that has to happen in the
        daemon: catching a failure so one bad row cannot stop the Neo queue, and telling
        the user in the two cases they would otherwise never hear about.

        **AN UNCONFIRMED CLAIM IS AN INBOX ROW, NOT A DISPATCH.** Neo escalating, or
        producing output nobody could parse, leaves the bug in the backlog and says so —
        the user's instruction, and the right refusal direction: an unconfirmed `blocker`
        that quietly became a release is the worse failure by a long way.
        """
        from . import issues

        try:
            out = issues.settle_triage(self.catalog, q, verdict)
        except Exception:  # noqa: BLE001 — one unreadable row must not stall the queue
            log.exception("triage verdict for neo question %s failed", q.get("id"))
            return
        log.info("[%s] triage %s: %s -> %s%s", q.get("project"), out.get("outcome"),
                 out.get("claimed"), out.get("settled"),
                 f" ({out['wo_id']})" if out.get("wo_id") else "")
        # The head of the reasoning, and a pointer to the rest. Every inbox row reaches
        # every sink, Telegram included, so the full text stays one `jarvis neo show`
        # away — the rule the escalation row above already follows. This is also the ONLY
        # place it goes now: the tracker comment carries the two levels and nothing else
        # (`issues.TRIAGE_COMMENT`, review round 2).
        why = (out.get("reason") or "").strip()
        head = (why[:300] + ("…" if len(why) > 300 else "")) or "no reason given"
        full = f"Neo's reasoning in full: jarvis neo show {q.get('id')}"
        if out["outcome"] == "unconfirmed":
            central.add_inbox(
                project=q.get("project") or "jarvis-os", level="warning",
                title=f"a `{out['claimed']}` bug report is UNCONFIRMED",
                body=(f"{out['issue_url']}\nNeo could not settle the priority "
                      f"({head}), so NOTHING was dispatched and no release was cut. It "
                      f"is queued at {out.get('backlog_id') or '(no backlog item)'} — "
                      f"promote it with `jarvis backlog promote "
                      f"{out.get('backlog_id') or '<id>'}` if you agree with the "
                      f"rating.\n{full}"))
        elif out["outcome"] == "downgraded":
            central.add_inbox(
                project=q.get("project") or "jarvis-os", level="info",
                title=f"Neo downgraded a `{out['claimed']}` bug to "
                      f"`{out['settled']}`",
                body=(f"{out['issue_url']}\n{head}\n"
                      f"Queued at {out.get('backlog_id') or '(no backlog item)'} rather "
                      f"than dispatched.\n{full}"))

    def sync_issues(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Keep the tracker saying what the OS is actually doing. Issue #240.

        The counterpart to `poll_pull_requests`, pointed the other way: that step asks
        GitHub what happened, this one tells GitHub what happened here. A bug the OS
        filed itself carries `issue_url`, and its issue must be labelled while a work
        order is on it and closed when that work lands — without which the tracker is a
        thing only a human maintains, which is the whole of the issue.

        **A COMPARISON, NOT A SCHEDULE OF POKES.** `issues.desired_state` derives where
        the issue belongs from the work order, `issue_state` records where the OS last
        put it, and nothing is sent while the two agree. So the common case — every
        tracked work order already reconciled — costs ONE indexed query for the whole
        project and no subprocess at all, and a project that has never filed a bug does
        not even pay that (the query returns nothing).

        It is also the retry. `issues.record_applied` writes the column only after
        GitHub accepted the change, so a tick that could not reach `gh` leaves the two
        disagreeing and the next sweep tries again — which is what lets the filing path
        label optimistically without owning a failure (#240 E).
        """
        tracked = store.work_orders_tracking_issues()
        if not tracked:
            return
        from . import github, issues

        for wo in tracked:
            want = issues.desired_state(store, wo)
            if want == (wo.get("issue_state") or ""):
                continue
            try:
                applied = issues.record_applied(store, wo, project.bugs.label)
            except github.GitHubError as e:
                # Same shape as the pull-request poll: one unreachable tracker must not
                # hide the rest, and the user hears once per daemon run.
                log.debug("[%s] could not sync %s for %s: %s", project.name,
                          wo.get("issue_url"), wo["id"], e)
                self._warn_issue_sync_broken(project, store, e)
                continue
            except Exception:  # noqa: BLE001 — never let one work order stall the rest
                log.exception("[%s] syncing the issue for %s failed", project.name,
                              wo["id"])
                continue
            log.info("[%s] %s is now %s (%s)", project.name, wo.get("issue_url"),
                     applied, wo["id"])
            # THE SHIP IS TRIGGERED BY THE TRACKER CLOSING, WHICH IS THE LANDING SIGNAL
            # AND NOT `completed`. `issues.desired_state` only reaches CLOSED for a
            # merged pull request or an order that produced nothing to land, so this
            # inherits issue #232's distinction rather than restating it — a fix sitting
            # on an unmerged branch never gets here. `pr_url` separates those two routes:
            # an order with no code to land has nothing to put in a release.
            if (applied == issues.CLOSED and wo.get("pr_url")
                    and issues.dispatches(wo.get("issue_priority") or "")):
                self.ensure_release(project, store, wo)

    #: The key under a work order's `metadata` that says "this order exists to ship
    #: fixes, and these are the ones it is shipping". The batch lives HERE rather than in
    #: a column because it is a list that grows while the order waits, and because
    #: nothing outside this file has a reason to query it.
    RELEASE_BATCH_KEY = "release_for_issues"

    #: What the release work order is told to do. It runs the ORDINARY release path —
    #: `scripts/shipit.sh --stage`, through the gate, exactly as a human-filed release
    #: would (the user's instruction). `--stage` rather than an inline restart because
    #: restarting inline kills the worker's own session: 0.5.1 landed as `failed` after a
    #: perfect deploy, which is the whole reason staged mode exists.
    RELEASE_BRIEF = """\
Ship a release. These fixes have LANDED on `main` and the bugs they close were confirmed \
`critical` or `blocker` by Neo, so the OS owes the fleet a release carrying them:

{fixes}

Run the ordinary release path and nothing else:

    scripts/shipit.sh --stage --wo {wo_id}

That is a PRIVILEGED ACTION and it will be gated — that is correct and expected. Make \
your case first (`jarvis gate request`), and read `jarvis brief gates` before you do. Do \
NOT invent a second release path, do not restart any service by hand, and do not drop \
`--stage`: an inline restart kills your own session mid-turn.

Check `main` is green before you ask. If it is not, say so and stop — a release is not \
the place to fix a red build."""

    def ensure_release(self, project: ProjectSpec, store: ProjectStore,
                       wo: dict) -> str:
        """Make sure a release carrying this landed fix is on its way. Returns its id.

        **BATCHED, and that is the whole of this function.** Two blockers landing ten
        minutes apart must not cut two releases: an open release order picks the second
        fix up instead, because a release ships whatever is on `main` and the first one
        has not gone out yet. Only once it has settled does the next landed fix earn a
        new one.

        Idempotent for the same reason the rest of the lifecycle is — a fix already in
        the batch is not added twice, so a sweep that runs again changes nothing.
        """
        from . import db
        from .project_store import OPEN_STATUSES

        url = wo.get("issue_url") or ""
        line = (f"- {url} — fixed by `{wo['id']}`"
                + (f" ({wo['pr_url']})" if wo.get("pr_url") else ""))
        for candidate in store.list_work_orders(statuses=OPEN_STATUSES,
                                                include_hidden=True):
            meta = db.from_json(candidate.get("metadata"), {}) or {}
            batch = meta.get(self.RELEASE_BATCH_KEY)
            if not isinstance(batch, list):
                continue
            if url not in batch:
                store.update_work_order(
                    candidate["id"],
                    metadata=db.to_json({**meta,
                                         self.RELEASE_BATCH_KEY: [*batch, url]}),
                    description=f"{candidate.get('description') or ''}\n{line}")
                store.add_event(candidate["id"], "release_batched",
                                {"issue_url": url, "wo_id": wo["id"]})
                log.info("[%s] %s joins the pending release %s", project.name, url,
                         candidate["id"])
            return str(candidate["id"])

        # The brief names the work order's OWN id (`shipit.sh --wo`), which does not
        # exist until the row does — hence create, then fill in.
        fresh = store.create_work_order(
            title=f"Ship the fix for {wo.get('title') or url}",
            description="", origin="jarvis",
            metadata={self.RELEASE_BATCH_KEY: [url]})
        store.update_work_order(
            fresh["id"],
            description=self.RELEASE_BRIEF.format(wo_id=fresh["id"], fixes=line))
        store.add_event(fresh["id"], "release_batched",
                        {"issue_url": url, "wo_id": wo["id"]})
        log.info("[%s] %s landed — filed release %s", project.name, url, fresh["id"])
        return str(fresh["id"])

    def _warn_issue_sync_broken(self, project: ProjectSpec, store: ProjectStore,
                                error: Exception) -> None:
        """Tell the user once per daemon run that the tracker is not being kept up.

        `_warn_pr_poll_broken`'s twin, and it says a different thing on purpose: that
        one warns that merges will not register, this one that issues the OS filed will
        sit labelled and open until somebody closes them by hand. Both are the state the
        OS reverts to, said plainly, rather than a stack trace.
        """
        from . import github

        if project.name in self.issue_sync_warned:
            return
        self.issue_sync_warned.add(project.name)
        log.warning("[%s] issue lifecycle unavailable: %s", project.name, error)
        hint = ("" if isinstance(error, github.GhUnavailable) else
                " If this is the daemon, `gh`'s keyring credentials may be out of "
                "reach — set GH_TOKEN in the service environment.")
        store.add_notification(
            title=f"the bug tracker is not being kept up to date for {project.name}",
            body=(f"{error}\n\nIssues Jarvis filed will stay as they are — labelled and "
                  f"open — until you close them yourself.{hint}"),
            level="warning", source="issue-sync",
        )

    def _warn_pr_poll_broken(self, project: ProjectSpec, store: ProjectStore,
                             error: Exception) -> None:
        """Tell the user once per daemon run that this project's PRs are not polled.

        Silence would be worse than a warning here: the OS would look like it had a
        feature it does not, and the user would keep waiting for merges to register.
        Once is the other half of that — a broken `gh` is broken on every poll, and an
        inbox entry every two minutes is how an inbox stops being read.

        The hint at the end has to match the failure. `GhUnavailable` already carries a
        full PATH diagnosis (`bugreport.gh_missing_message`), so appending the keyring
        advice to it would tell the user to fix credentials for a binary that was never
        found — the misdiagnosis issue #90 was filed about.
        """
        from . import github

        if project.name in self.pr_poll_warned:
            return
        self.pr_poll_warned.add(project.name)
        log.warning("[%s] pull-request polling unavailable: %s", project.name, error)
        hint = ("" if isinstance(error, github.GhUnavailable) else
                " If this is the daemon, `gh`'s keyring credentials may be out of "
                "reach — set GH_TOKEN in the service environment.")
        store.add_notification(
            title=f"auto-complete on merge is off for {project.name}",
            body=(f"{error}\n\nWork orders parked behind a pull request will stay on "
                  f"the open list until you close them with `jarvis wo done`.{hint}"),
            level="warning", source="pr-poll",
        )

    def retire_ungoverned(self, store: ProjectStore, wo: dict, why: str) -> None:
        """Close an injected session's record without passing judgement on it.

        Jarvis did not dispatch this session: the user started it themselves and handed
        it over with `jarvis wo inject`. It never got `JARVIS_WO_ID` or the worker
        briefing, so it owes no `jarvis wo finish` and its ending is not an incident.
        Marking it `failed`/`needs_review` (as this reconciler used to) turned every
        session the user ran into a permanent attention item.

        The record keeps everything it learned — timeline, captured replies, any
        assumptions — it just stops demanding the user.
        """
        store.set_status(wo["id"], "completed")
        store.clear_attention(wo["id"])
        store.add_event(wo["id"], "session_retired", {"why": why})
        log.info("retired injected session %s (%s)", wo["id"], why)

    def _tracking_injected_sessions(self) -> bool:
        """Does any project have a live session Jarvis was handed? Decides whether this
        tick pays for `claude agents --json` at all."""
        for project in self.catalog.projects:
            if not project.path.is_dir():
                continue
            try:
                store = self.store_for(project)
                if any(wo["origin"] == "injected" for wo in
                       store.list_work_orders(statuses=("running", "waiting_input"),
                                              include_hidden=True)):
                    return True
            except Exception:  # noqa: BLE001 — an unreadable store is the next loop's problem
                log.exception("could not check %s for injected sessions", project.name)
        return False

    def track_injected_sessions(
        self,
        project: ProjectSpec,
        store: ProjectStore,
        sessions_by_cwd: dict[str, list[claude_cli.BgSession]],
    ) -> None:
        """Follow the sessions the user handed to Jarvis, and only those.

        Jarvis used to adopt every session it found running under a project path. It no
        longer does: a session the user started is theirs, and Jarvis does not see it,
        name it, flag it or write into it until `jarvis wo inject` hands it over (GitHub
        issue 47). This tracks what was handed over; it never creates a record.

        An injected row is still not held to the worker contract — the session never
        received it. See `retire_ungoverned` and INV-ADHOC-NOT-GOVERNED.

        Only `injected` rows are followed, never the legacy `adhoc` ones: those were
        adopted without consent and INV-ADHOC-LEGACY-RETIRED closes them once on
        upgrade. Following them too would undo that — the reopen rule below would put
        every still-live one straight back to `running` on the next tick.
        """
        proot = str(project.path)
        sessions = [
            s for cwd, group in sessions_by_cwd.items()
            if cwd == proot or cwd.startswith(proot + "/")
            for s in group
        ]
        by_session_id = {s.session_id: s for s in sessions if s.session_id}

        for wo in store.list_work_orders(statuses=("running", "waiting_input")):
            if wo["origin"] != "injected":
                continue
            sess = by_session_id.get(wo.get("session_id") or "")
            if sess is None:
                # Gone from the agents view: the user closed it. Housekeeping, not an
                # incident.
                if time.time() - wo["updated_at"] > 120:
                    self.retire_ungoverned(store, wo, "session left the agents view")
            elif sess.is_blocked and wo["status"] == "running":
                store.set_status(wo["id"], "waiting_input")
                store.flag_attention(wo["id"],
                                     "session blocked (permission or input needed)")
            elif sess.is_active and wo["status"] != "running":
                store.set_status(wo["id"], "running")
                store.clear_attention(wo["id"])
            elif sess.is_finished:
                self.retire_ungoverned(store, wo, "session went idle")

        # A retired session can start another turn — the user just typed again. Reopen
        # the record rather than showing "completed" next to a session that is visibly
        # working. Only for sessions already injected: an unknown one stays unknown.
        for sess in sessions:
            if not sess.session_id or not sess.is_active:
                continue
            known = store.find_by_session(sess.session_id)
            if (known and known["origin"] == "injected"
                    and known["status"] == "completed"):
                store.set_status(known["id"], "running")


def run_daemon(catalog_path: str | Path, poll_interval: float = 5.0,
               log_to_file: bool = True) -> None:
    ensure_home()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_to_file:
        handlers.append(logging.FileHandler(logs_dir() / "jarvisd.log"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    # Before anything is spawned: every worker inherits this process's PATH verbatim
    # (`claude_cli.spawn_turn`, and `systemd_units`' --setenv forwarding), so a unit
    # rendered before the #41/#90 fix hands every one of them a bash with no `gh`.
    added = bugreport.heal_path()
    if added:
        log.warning("PATH did not include %s — appended for this process and every "
                    "worker it spawns; the installed unit is stale, re-run "
                    "scripts/install_prod_service.sh", ", ".join(added))
    catalog = load_catalog(catalog_path)
    Daemon(catalog, poll_interval=poll_interval).run_forever()


def daemon_running() -> int | None:
    """Return the daemon pid if alive, else None (cleaning up stale pidfiles)."""
    pf = daemon_pidfile()
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        pf.unlink(missing_ok=True)
        return None
