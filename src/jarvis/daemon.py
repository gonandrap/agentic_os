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

from . import bugreport, bus, claude_cli, db, worker_session
from .catalog import Catalog, ProjectSpec, load_catalog
from .central_store import CentralStore
from .dispatch import dispatch_work_order
from .paths import daemon_pidfile, ensure_home, logs_dir
from .project_store import (
    ACTIVE_STATUSES,
    FO_TERMINAL_STATUSES,
    OPEN_STATUSES,
    PRE_APPROVED_KEY,
    UNGOVERNED_ORIGINS,
    ProjectStore,
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
        # `None` means "the roster was not read this tick" — either nothing is injected
        # or the listing failed — and is NOT the same as an empty roster, which would
        # mean every injected session ended. Session tracking is skipped on None.
        sessions_by_project: dict[str, list[claude_cli.BgSession]] | None = None
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
                    self.retry_paused_turns(project, store)
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
                self.dispatch_pending(project, store)
                if poll_prs:
                    self.poll_pull_requests(project, store)
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
                    # Last: check the state everything above just produced.
                    self.check_invariants(project, store)
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
        # Straight after, and never merged into it: a proposal filed above cannot be
        # applied on the tick that filed it (its gate is `pending`), so the two only ever
        # meet across ticks — and the separation is what keeps the deciding half free of
        # the acting half. See spec 2026-09-02, §5.
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

    def dispatch_pending(self, project: ProjectSpec, store: ProjectStore) -> None:
        while store.count_active() < project.max_concurrent:
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

    def retry_paused_turns(self, project: ProjectSpec, store: ProjectStore) -> None:
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

        Every work order this touches is in an ACTIVE status, because the settler
        deliberately did not fail it (see `settle_work_order` and `_park_on_signin`).
        What the relaunch SENDS is `worker_session.retry`'s business, and it is not the
        same for all: a refused turn was never sent, so the prompt goes again verbatim; a
        turn that died in flight already reached the model, so the worker is nudged to
        continue instead.
        """
        for wo in store.list_work_orders(statuses=ACTIVE_STATUSES):
            if wo["origin"] in UNGOVERNED_ORIGINS:
                continue  # the user's own session; Jarvis does not drive it
            try:
                pause = worker_session.turn_pause(store, wo["id"])
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] could not diagnose %s", project.name, wo["id"])
                continue
            if pause is None or not pause.resumable or not pause.due():
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
            log.info("[%s] %s resumed after %s (attempt %s/%s, turn %s)",
                     project.name, wo["id"], pause.reason, pause.attempts,
                     pause.max_attempts or "∞", turn["seq"])
            store.add_event(wo["id"], "turn_resumed", {
                "seq": turn["seq"], "retried_seq": pause.turn["seq"],
                "attempt": pause.attempts, "of": pause.max_attempts,
                "reason": pause.reason,
            })
            # Only an auth pause is ever parked out of `running` (`_park_on_signin`), so
            # this is where that gets put back — the same two lines `_deliver` uses, for
            # the same reason: the turn is out, and a record still saying "waiting on
            # you" about a worker that is working would be a lie.
            if wo["status"] != "running":
                store.set_status(wo["id"], "running")
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
        """
        pending: dict[str, list[dict[str, Any]]] = {}
        for msg in store.queued_messages():  # chronological, so the joins stay in order
            try:
                wo = store.get_work_order(msg["wo_id"])
            except KeyError:
                store.mark_message(msg["id"], "failed")
                continue
            if not wo.get("session_id"):
                continue  # not dispatched yet; the worker prompt will carry it instead
            if worker_session.busy(store, wo["id"]):
                continue  # mid-turn: one turn at a time, and resume would refuse anyway
            pause = worker_session.turn_pause(store, wo["id"])
            if pause is not None and pause.resumable:
                # Parked on the usage limit or on a broken API. The lost turn has to go
                # out first — it is holding a message already marked `delivered`, so
                # sending this one now would silently jump the queue. Held, not dropped:
                # the same queue delivers it as the next turn once the retry gets
                # through.
                #
                # `resumable`, not `exhausted`: the two answer differently only for an
                # auth pause whose sign-in has not changed, and that one never exhausts
                # by design — holding on it would hold `jarvis wo send … "retry"` for
                # ever, which is the manual escape hatch for a sign-in the OS cannot see
                # (Neo, question 169).
                continue
            pending.setdefault(wo["id"], []).append(dict(msg))
        for wo_id, msgs in pending.items():
            self._deliver(project, store, store.get_work_order(wo_id), msgs)

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
        """Judge every work order parked in `validating`, off this thread.

        THE KILL SWITCH IS NOT CHECKED HERE, and that is the point of the whole design:
        `os.validation.enabled` gates OPENING a round (`ops.finish`) and never settling
        one. A user who turns the panel off at three in the morning because it is
        misbehaving must not thereby strand every unit already inside it — so this
        drains what is open either way, and a round with no validator settles its unit
        exactly where the OS settles it with the feature switched off.

        Everything expensive — collecting the diff, calling the seats — happens on the
        pool thread. What is left here is one indexed query, so a fleet with nothing in
        `validating` pays a lookup that finds nothing.
        """
        for wo in store.list_work_orders(statuses=("validating",), include_hidden=True):
            wo_id = wo["id"]
            if wo_id in self.validating:
                continue  # its round is in flight; a second tick must not start another
            round_row = store.latest_validation_round(wo_id=wo_id)
            if round_row is None:
                # In `validating` with no round at all: nothing this machine can judge.
                # INV-VALIDATION-STRANDED (a later work order) is what finds these.
                log.warning("[%s] %s is validating with no round on record",
                            project.name, wo_id)
                continue
            if round_row["outcome"] not in ("pending", "failed"):
                continue  # already judged — settlement is what moves it, not a re-run
            self.validating.add(wo_id)
            future = self.validate_pool.submit(
                self._validate_work_order, project, wo_id, int(round_row["id"]))
            future.add_done_callback(
                lambda f, k=wo_id: self.validating.discard(k))

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
                diff_chars=cfg.diff_chars, spec=specs.spec_of(store, wo))

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
                ops.land_finished(store, wo)
                log.info("[%s] %s: round %d settled unjudged (no validator)",
                         project.name, wo_id, n)
                return

            # An EMPTY DIFF never reaches the validator. A reviewer handed nothing to
            # review will approve it, and that single silent pass would make the whole
            # feature theatre.
            if not packet.files:
                self._escalate(store, wo, round_id, n,
                               "this submission changes no files, so there is nothing "
                               "to review. Nobody has judged the work.")
                return

            # A REPEAT of the IMMEDIATELY PRECEDING round only. Compared against every
            # earlier round it would punish a submitter that was told to go back to a
            # shape it had already tried, which is a legitimate answer to feedback.
            previous = self._preceding_round(store, n, wo_id=wo_id)
            if previous and previous["fingerprint"] == round_row["fingerprint"]:
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
            outcome = str(verdict.get("outcome") or "")
            reason = str(verdict.get("reason") or "")

            if outcome == "passed":
                store.close_validation_round(round_id, "passed", reason)
                store.add_event(wo_id, "validation_passed",
                                {"round": n, "round_id": round_id})
                status = ops.land_finished(store, wo)
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
    def _escalate(store: ProjectStore, wo: dict, round_id: int, n: int,
                  reason: str) -> None:
        """Give up on this unit and ask the user.

        The attention reason is `VALIDATION_STUCK_BLOCKER` VERBATIM, because
        INV-ATTENTION-REASON rewrites any reason `invariants.true_blockers` cannot
        re-derive — a better sentence here would simply be overwritten on the next
        reconcile tick, and the user would read the generic one.
        """
        from .invariants import VALIDATION_STUCK_BLOCKER

        wo_id = wo["id"]
        store.close_validation_round(round_id, "escalated", reason)
        store.add_event(wo_id, "validation_escalated",
                        {"round": n, "round_id": round_id, "reason": reason})
        store.set_status(wo_id, "needs_review")
        store.flag_attention(wo_id, VALIDATION_STUCK_BLOCKER)

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
        from . import ops

        store = ProjectStore(project.path)  # thread-local connection — see the docstring
        try:
            fo = store.get_feature_order(fo_id)
            round_row = store.get_validation_round(round_id)
            if round_row is None:  # pragma: no cover - deleted mid-flight
                return
            cfg = self._round_config(project, round_row)
            n, max_rounds = int(round_row["round"]), int(cfg.max_rounds)
            packet = ops.collect_feature_evidence(
                store, project.path, fo, declared=str(round_row["evidence"] or ""),
                summary=str(round_row["summary"] or ""), cfg=cfg)

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
            if not packet.base:
                self._escalate_feature(
                    store, fo, round_id, n,
                    "this feature order has no recorded base commit, so there is no "
                    "honest way to say what it changed. It was released before the OS "
                    "started recording one. Nobody has judged the work.")
                return
            if not packet.files:
                self._escalate_feature(
                    store, fo, round_id, n,
                    "nothing has changed on the default branch since this feature "
                    "started, so there is nothing to review. Nobody has judged the work.")
                return

            previous = self._preceding_round(store, n, fo_id=fo_id)
            if previous and previous["fingerprint"] == round_row["fingerprint"]:
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
        """
        from . import ops
        from .invariants import VALIDATION_STUCK_BLOCKER

        fo_id = fo["id"]
        store.close_validation_round(round_id, "escalated", reason)
        ops.feature_event(store, fo_id, "validation_escalated",
                          {"round": n, "round_id": round_id, "reason": reason,
                           "feature_order": fo_id})
        store.flag_feature_attention(fo_id, VALIDATION_STUCK_BLOCKER)

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
                if q.get("kind") == "approval":
                    self._deliver_gate_verdict(central, pstore, q, verdict)
                elif q.get("kind") == "plan":
                    self._deliver_plan_verdict(central, store, pstore, q, verdict)
                elif q.get("kind") == "alarm":
                    # ABOVE the escalate branch, because this kind owns both outcomes —
                    # and the branch below it messages the WORKER, which no alarm may
                    # ever do (§3).
                    self._deliver_alarm_verdict(central, pstore, q, verdict)
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
                if pstore and q.get("kind") not in ("approval", "plan", "alarm"):
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
            central.add_inbox(
                project=q["project"], level="warning",
                title=f"Approval needed: {approval['kind']} from {q['wo_id']}",
                body=(f"Neo declined to decide: {verdict['reason']}\n\n"
                      f"Command: {approval['command']}\n\n"
                      f"Approve it with: jarvis gate approve {approval['id']} "
                      f"--reason \"...\"\n"
                      f"Deny it with:    jarvis gate deny {approval['id']} "
                      f"--reason \"...\"\n"
                      f"Full request:    jarvis gate show {approval['id']}"),
                wo_id=q["wo_id"],
            )
            pstore.mark_approval_escalated(approval["id"], verdict["reason"])
            pstore.flag_attention(
                q["wo_id"],
                f"gate approval escalated by Neo: {approval['kind']} "
                f"(request {approval['id']})",
            )
            pstore.add_event(q["wo_id"], "gate_escalated", {
                "approval_id": approval["id"], "reason": verdict["reason"],
            })
            return

        ruling = verdict.get("verdict") or ("approved" if verdict.get("approve")
                                            else "denied")
        gates.apply_decision(pstore, approval["id"], verdict=ruling,
                             reason=verdict["reason"], decided_by="neo",
                             central=central, project=q["project"],
                             exempt_pattern=verdict.get("exempt_pattern", ""))
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
            central.add_inbox(
                project=q["project"],
                level="info" if ruling == "approved" else "warning",
                title=f"Neo {ruling} {approval['kind']} for {q['wo_id']}",
                body=(f"{verdict['reason']}\n\nCommand: {approval['command']}\n"
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

    # -- 7. invariants (post-conditions) --------------------------------------------------

    def check_invariants(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Verify the OS's own state and repair what is unambiguously wrong.

        Runs after reconcile so it judges the state this tick actually produced. Every
        other step here trusts that its writes stuck; this is the only one that checks.
        Repairs are recorded on the work order's timeline so a self-healed inconsistency
        is visible rather than silently papered over, and each distinct violation is
        reported once per daemon run.
        """
        from .invariants import check_project

        try:
            violations = check_project(store, repair=True)
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
        from . import inspection
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
                                                now=now, index=index)
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
        for wo in store.list_work_orders(
                statuses=("running", "waiting_input", "dispatching")):
            if wo["origin"] in UNGOVERNED_ORIGINS:
                continue  # not ours to run; track_injected_sessions follows these
            try:
                self.settle_work_order(project, store, wo)
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] settling %s failed", project.name, wo["id"])

    def settle_work_order(self, project: ProjectSpec, store: ProjectStore,
                          wo: dict) -> None:
        from .invariants import awaiting_neo

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
                if fresh["status"] != "waiting_pr_merge":
                    store.set_status(wo["id"], "waiting_pr_merge")
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
            elif fresh["status"] != "waiting_input":
                store.set_status(wo["id"], "waiting_input")
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

        Four answers, from `github.pr_view`:

        * **merged** — the work landed; the work order ends (`ops.complete_merged`).
        * **closed, unmerged** — someone refused the work; it goes to `needs_review`
          and asks for the user (`ops.record_pr_closed`).
        * **open and conflicting** — the worker is asked to resolve it, so the user
          never has to (`ops.nudge_pr_conflict`, and the whole of
          docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md).
        * **open and mergeable** — nothing to do, and nothing written unless a conflict
          episode is being closed. The overwhelmingly common case, so it costs one `gh`
          call, one indexed read and no write.

        Hidden work orders are polled too. Hiding drops a record from listings and the
        attention list; it does not mean the record may go on saying something untrue.

        The step is skipped whole when nothing is parked — one indexed query — so a
        fleet with no open pull requests never spawns a subprocess for this.
        """
        parked = [wo for wo in store.list_work_orders(statuses=("waiting_pr_merge",),
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
                    ops.complete_merged(store, wo, merged_at=pr.merged_at)
                elif pr.closed_unmerged:
                    log.info("[%s] %s closed unmerged — %s needs the user",
                             project.name, wo["pr_url"], wo["id"])
                    ops.record_pr_closed(store, wo)
                elif pr.conflicting:
                    self.heal_pr_conflict(project, store, wo, pr)
                elif pr.mergeable_now and ops.clear_pr_conflict(store, wo):
                    log.info("[%s] %s merges again — %s stopped conflicting",
                             project.name, wo["pr_url"], wo["id"])
            except Exception:  # noqa: BLE001
                log.exception("[%s] settling %s against its PR failed", project.name,
                              wo["id"])

    def heal_pr_conflict(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                         pr: Any) -> None:
        """A parked pull request that conflicts: ask its worker to resolve it.

        The three guards are all this adds over `ops.nudge_pr_conflict`: no session to
        resume, a nudge already queued, a turn already in flight. Spec §3 for why each
        of them would otherwise cost a duplicated turn or silently spend the budget.
        """
        from . import ops

        if not wo.get("session_id"):
            return
        if store.queued_messages(wo["id"]) or worker_session.busy(store, wo["id"]):
            return
        out = ops.nudge_pr_conflict(store, wo, base=pr.base_ref)
        if out["gave_up"]:
            log.info("[%s] %s still conflicts after %s attempts — %s needs the user",
                     project.name, wo["pr_url"], out["attempts"], wo["id"])
        else:
            log.info("[%s] %s conflicts — asking %s to resolve (attempt %s)",
                     project.name, wo["pr_url"], wo["id"], out["attempts"])

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
