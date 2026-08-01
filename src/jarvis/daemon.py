"""jarvisd — the deterministic OS daemon.

One process, one poll loop over every project in the catalog. Per tick, in this order:
  1. route project notification outboxes to the central inbox, then to sinks
  2. reap finished worker turns and settle their work orders against what came back
  3. deliver queued user messages as the next turn of their conversation
  4. dispatch pending work orders (respecting per-project concurrency) — last, so it
     sees the concurrency slots steps 2 and 3 just freed
  5. let Neo (the OS answerer agent) drain queued worker questions

Every RECONCILE_EVERY_TICKS ticks it additionally:
  6. adopts the user's own background sessions as `adhoc` work orders (visibility) —
     the only step that still needs `claude agents --json`, since workers are headless
     and never enter that roster
  7. checks the OS's own post-conditions (src/jarvis/invariants.py) and repairs the
     state that is unambiguously wrong — the only step that does not trust the others

The daemon is an orchestrator, never a doer: all actual work happens inside the worker
turns it launches (see worker_session.py, which owns how a turn is actually run).
"""

from __future__ import annotations

import logging
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import claude_cli, worker_session
from .catalog import Catalog, ProjectSpec, load_catalog
from .central_store import CentralStore
from .dispatch import dispatch_work_order
from .paths import daemon_pidfile, ensure_home, logs_dir
from .project_store import ProjectStore

log = logging.getLogger("jarvisd")

RECONCILE_EVERY_TICKS = 6  # refresh `claude agents --json` every N ticks (ad-hoc only)


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
        # Invariant violations already reported this run, so a standing problem is
        # surfaced once instead of every tick. Keyed by (invariant, wo_id).
        self.reported_violations: set[tuple[str, str | None]] = set()

    # -- lifecycle -----------------------------------------------------------

    def store_for(self, project: ProjectSpec) -> ProjectStore:
        if project.name not in self.stores:
            self.stores[project.name] = ProjectStore(project.path)
        return self.stores[project.name]

    def run_forever(self) -> None:
        ensure_home()
        self._write_pidfile()
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        log.info("jarvisd started (pid=%s, projects=%s)",
                 os.getpid(), [p.name for p in self.catalog.projects])
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
        self.tick_count += 1
        reconcile = self.tick_count % RECONCILE_EVERY_TICKS == 1
        sessions_by_project: dict[str, list[claude_cli.BgSession]] = {}
        if reconcile:
            try:
                sessions = claude_cli.list_background_sessions()
                for s in sessions:
                    sessions_by_project.setdefault(s.cwd, []).append(s)
            except claude_cli.ClaudeCliError as e:
                log.warning("agents listing failed: %s", e)
                reconcile = False

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
                self.deliver_messages(project, store)
                self.dispatch_pending(project, store)
                if reconcile:
                    # The agents roster now holds ONLY the user's own sessions: workers
                    # are headless and never enter it. Adoption is the sole reason to
                    # keep paying for `claude agents --json`.
                    self.adopt_sessions(project, store, sessions_by_project)
                    # Last: check the state everything above just produced.
                    self.check_invariants(project, store)
                self.central.touch_project(project.name)
            except Exception:  # noqa: BLE001
                log.exception("project %s tick failed", project.name)

        self.neo_tick()

        from .notify import route_new_inbox
        route_new_inbox(self.central, self.catalog)

    # -- 4. dispatch -------------------------------------------------------------

    def dispatch_pending(self, project: ProjectSpec, store: ProjectStore) -> None:
        while store.count_active() < project.max_concurrent:
            wo = store.claim_next_pending()
            if wo is None:
                return
            log.info("[%s] dispatching %s: %s", project.name, wo["id"], wo["title"])
            try:
                dispatch_work_order(
                    store, self.central, project, wo,
                    knowledge_limit=self.catalog.os.knowledge_inject_limit,
                )
            except claude_cli.ClaudeCliError as e:
                log.error("[%s] dispatch of %s failed: %s", project.name, wo["id"], e)

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

    # -- 3. message delivery ----------------------------------------------------------

    def deliver_messages(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Send queued user messages into their work orders' conversations.

        No roster lookup any more, and no thread pool: a turn is a detached process, so
        launching one is instant and the only thing delivery has to wait for is the
        previous turn of the SAME work order finishing (`worker_session.busy`).
        """
        for msg in store.queued_messages():
            try:
                wo = store.get_work_order(msg["wo_id"])
            except KeyError:
                store.mark_message(msg["id"], "failed")
                continue
            if not wo.get("session_id"):
                continue  # not dispatched yet; the worker prompt will carry it instead
            if worker_session.busy(store, wo["id"]):
                continue  # mid-turn: one turn at a time, and resume would refuse anyway
            self._deliver(project, store, wo, dict(msg))

    def _deliver(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                 msg: dict) -> None:
        log.info("[%s] delivering message %s to %s", project.name, msg["id"], wo["id"])
        store.add_event(wo["id"], "delivering", {"msg_id": msg["id"]})
        try:
            turn = worker_session.send(store, project, wo, msg["content"],
                                       msg_id=msg["id"])
        except claude_cli.ClaudeCliError as e:
            log.error("[%s] delivery of message %s failed: %s", project.name,
                      msg["id"], e)
            store.mark_message(msg["id"], "failed")
            store.flag_attention(wo["id"], f"message delivery failed: {e}")
            return
        store.mark_message(msg["id"], "delivered")
        store.add_event(wo["id"], "message_delivered",
                        {"msg_id": msg["id"], "turn": turn["seq"]})
        # The work order is moving again, whatever it had settled into. A user who sends
        # a message to a finished work order means it to continue, and the turn is
        # already out — leaving the status settled would make the record lie.
        if wo["status"] != "running":
            store.set_status(wo["id"], "running")
            store.clear_attention(wo["id"])

    # -- 5. Neo (answer worker questions) --------------------------------------------

    def neo_tick(self) -> None:
        """Kick a queue drain when questions are waiting and none is running."""
        if not self.catalog.os.neo.enabled or self.neo_draining:
            return
        from .neo_store import NeoStore
        store = NeoStore()
        try:
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
                elif verdict["escalate"]:
                    central.add_inbox(
                        project=q["project"], level="warning",
                        title=f"Neo escalated a question from {q['wo_id']}",
                        body=f"Q: {q['question']}\nWhy: {verdict['reason']}\n"
                             f"Answer it with: jarvis neo answer {q['id']} \"...\"",
                        wo_id=q["wo_id"],
                    )
                    if pstore:
                        pstore.flag_attention(
                            q["wo_id"], f"question escalated by Neo: {q['question'][:80]}"
                        )
                elif pstore:
                    pstore.queue_message(
                        q["wo_id"], f"{neo_mod.ANSWER_PREFIX} {verdict['answer']}",
                        source="neo",
                    )
                    pstore.add_event(q["wo_id"], "neo_answered",
                                     {"neo_question_id": q["id"]})
            finally:
                if pstore:
                    pstore.close()

        try:
            results = neo_mod.drain_queue(
                store, model=cfg.model, learnings_limit=cfg.learnings_limit,
                deliver=deliver,
            )
            if results:
                log.info("neo drained %d question(s)", len(results))
        except Exception:  # noqa: BLE001 — the drain must never kill the daemon
            log.exception("neo drain failed")
        finally:
            store.close()
            central.close()

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

        approved = bool(verdict.get("approve"))
        gates.apply_decision(pstore, approval["id"], approved=approved,
                             reason=verdict["reason"], decided_by="neo")
        # A shipped release is something the user wants to know happened, even when they
        # did not have to authorise it — that is the trade for spending none of their
        # attention on the approval itself.
        central.add_inbox(
            project=q["project"],
            level="info" if approved else "warning",
            title=(f"Neo {'approved' if approved else 'denied'} "
                   f"{approval['kind']} for {q['wo_id']}"),
            body=(f"{verdict['reason']}\n\nCommand: {approval['command']}\n"
                  f"Review Neo's call with: jarvis neo review {q['id']}"),
            wo_id=q["wo_id"],
        )
        log.info("gate %s %s by neo for %s", approval["id"],
                 "approved" if approved else "denied", q["wo_id"])

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

    # -- 2 & 6. turns, settlement, and ad-hoc adoption -----------------------------------------------------

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
            if wo["origin"] == "adhoc":
                continue  # not ours to run; adopt_sessions tracks these
            try:
                self.settle_work_order(project, store, wo)
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] settling %s failed", project.name, wo["id"])

    def settle_work_order(self, project: ProjectSpec, store: ProjectStore,
                          wo: dict) -> None:
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
            if wo["status"] != "failed":
                store.set_status(wo["id"], "failed")
                store.flag_attention(wo["id"], "worker turn failed — review and retry")
                store.add_notification(
                    title=f"{wo['id']} worker turn failed",
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
        elif store.pending_approvals(wo["id"]):
            # Parked on a privileged-action gate: it was told to end its turn and wait
            # for the verdict, so an idle worker here is compliance, not abandonment.
            if fresh["status"] != "waiting_input":
                store.set_status(wo["id"], "waiting_input")
        else:
            store.set_status(wo["id"], "needs_review")
            store.flag_attention(
                wo["id"],
                "worker idle without `jarvis wo finish` — review the session",
            )

    def retire_adhoc(self, store: ProjectStore, wo: dict, why: str) -> None:
        """Close an adopted session's record without passing judgement on it.

        Jarvis did not dispatch this session: the user started it in `claude agents` and
        the reconciler adopted it afterwards so it would show up in `jarvis status` and
        on the dashboard. It never got `JARVIS_WO_ID` or the worker briefing, so it owes
        no `jarvis wo finish` and its ending is not an incident. Marking it
        `failed`/`needs_review` (as this reconciler used to) turned every session the
        user ran into a permanent attention item.

        The record keeps everything it learned — timeline, captured replies, any
        assumptions — it just stops demanding the user.
        """
        store.set_status(wo["id"], "completed")
        store.clear_attention(wo["id"])
        store.add_event(wo["id"], "adhoc_retired", {"why": why})
        log.info("retired ad-hoc %s (%s)", wo["id"], why)

    def adopt_sessions(
        self,
        project: ProjectSpec,
        store: ProjectStore,
        sessions_by_cwd: dict[str, list[claude_cli.BgSession]],
    ) -> None:
        """Mirror the background sessions the USER started into work order records.

        Since workers moved to headless turns, nothing Jarvis dispatches appears in the
        agents roster — so everything found here belongs to the user. Adoption exists for
        visibility only (`jarvis status`, the dashboard); an adopted row is never held to
        the worker contract. See `retire_adhoc` and INV-ADHOC-NOT-GOVERNED.
        """
        proot = str(project.path)
        sessions = [
            s for cwd, group in sessions_by_cwd.items()
            if cwd == proot or cwd.startswith(proot + "/")
            for s in group
        ]
        by_session_id = {s.session_id: s for s in sessions if s.session_id}

        for wo in store.list_work_orders(statuses=("running", "waiting_input")):
            if wo["origin"] != "adhoc":
                continue
            sess = by_session_id.get(wo.get("session_id") or "")
            if sess is None:
                # Gone from the agents view: the user closed it. Housekeeping, not an
                # incident.
                if time.time() - wo["updated_at"] > 120:
                    self.retire_adhoc(store, wo, "session left the agents view")
            elif sess.is_blocked and wo["status"] == "running":
                store.set_status(wo["id"], "waiting_input")
                store.flag_attention(wo["id"],
                                     "session blocked (permission or input needed)")
            elif sess.is_active and wo["status"] != "running":
                store.set_status(wo["id"], "running")
                store.clear_attention(wo["id"])
            elif sess.is_finished:
                self.retire_adhoc(store, wo, "session went idle")

        for sess in sessions:
            if not sess.session_id:
                continue
            known = store.find_by_session(sess.session_id)
            if known:
                # A retired ad-hoc session can start another turn — the user just typed
                # again. Reopen the record rather than showing "completed" next to a
                # session that is visibly working.
                if (known["origin"] == "adhoc" and known["status"] == "completed"
                        and sess.is_active):
                    store.set_status(known["id"], "running")
                continue
            if sess.is_finished:
                continue  # only surface live ad-hoc sessions
            wo = store.create_work_order(
                title=sess.name or f"ad-hoc session {sess.id}",
                description="Background session not created through Jarvis "
                            "(adopted by the reconciler for visibility).",
                origin="adhoc",
            )
            store.update_work_order(wo["id"], session_id=sess.session_id)
            if sess.is_blocked:
                store.set_status(wo["id"], "waiting_input")
                store.flag_attention(wo["id"],
                                     "session blocked (permission or input needed)")
            else:
                store.set_status(wo["id"], "running")
            log.info("[%s] adopted ad-hoc session %s (%s) as %s",
                     project.name, sess.id, sess.state, wo["id"])


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
