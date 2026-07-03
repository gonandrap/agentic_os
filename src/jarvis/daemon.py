"""jarvisd — the deterministic OS daemon.

One process, one poll loop over every project in the catalog. Per tick:
  1. dispatch pending work orders (respecting per-project concurrency)
  2. route project notification outboxes to the central inbox, then to sinks
  3. deliver queued user messages to idle worker sessions
  4. reconcile work order states against `claude agents --json`
     (fix drift, adopt unknown background sessions as `adhoc` work orders)

The daemon is an orchestrator, never a doer: all actual work happens inside the
Claude Code worker sessions it spawns.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import claude_cli
from .catalog import Catalog, ProjectSpec, load_catalog
from .central_store import CentralStore
from .dispatch import dispatch_work_order
from .paths import daemon_pidfile, ensure_home, logs_dir
from .project_store import ProjectStore

log = logging.getLogger("jarvisd")

RECONCILE_EVERY_TICKS = 6  # reconcile via `claude agents --json` every N ticks


class Daemon:
    def __init__(self, catalog: Catalog, poll_interval: float = 5.0):
        self.catalog = catalog
        self.poll_interval = poll_interval
        self.central = CentralStore()
        self.stores: dict[str, ProjectStore] = {}
        self.stop_requested = False
        self.tick_count = 0
        self.delivery_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="deliver")
        self.in_flight_deliveries: set[int] = set()

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
            self.delivery_pool.shutdown(wait=False)
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
                self.dispatch_pending(project, store)
                self.deliver_messages(project, store)
                if reconcile:
                    self.reconcile_project(project, store, sessions_by_project)
                self.central.touch_project(project.name)
            except Exception:  # noqa: BLE001
                log.exception("project %s tick failed", project.name)

        from .notify import route_new_inbox
        route_new_inbox(self.central, self.catalog)

    # -- 1. dispatch -------------------------------------------------------------

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

    # -- 2. notifications ----------------------------------------------------------

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
        for msg in store.queued_messages():
            if msg["id"] in self.in_flight_deliveries:
                continue
            try:
                wo = store.get_work_order(msg["wo_id"])
            except KeyError:
                store.mark_message(msg["id"], "failed")
                continue
            if not wo.get("session_id"):
                continue  # not dispatched yet; prompt will pick it up when it runs
            if wo["status"] in ("completed", "failed", "cancelled", "needs_review",
                                "waiting_input"):
                deliverable = True
            elif wo["status"] == "running":
                # Only deliver between turns to avoid interleaving a live turn:
                # the worker must have gone idle (turn_ended) since our last delivery.
                kinds = [e["kind"] for e in store.list_events(wo["id"], limit=500)
                         if e["kind"] in ("turn_ended", "delivering")]
                deliverable = bool(kinds) and kinds[-1] == "turn_ended"
            else:
                deliverable = False
            if not deliverable:
                continue
            self.in_flight_deliveries.add(msg["id"])
            store.add_event(wo["id"], "delivering", {"msg_id": msg["id"]})
            self.delivery_pool.submit(self._deliver, project, wo, dict(msg))

    def _deliver(self, project: ProjectSpec, wo: dict, msg: dict) -> None:
        store = ProjectStore(project.path)  # thread-local connection
        try:
            log.info("[%s] delivering message %s to %s", project.name, msg["id"], wo["id"])
            result = claude_cli.send_to_session(
                wo["session_id"], msg["content"], cwd=project.path,
            )
            store.mark_message(msg["id"], "delivered")
            if result:
                store.queue_message(wo["id"], result, source="worker",
                                    direction="agent_to_user", status="delivered")
            store.add_event(wo["id"], "message_delivered", {"msg_id": msg["id"]})
            if store.get_work_order(wo["id"])["status"] == "waiting_input":
                store.set_status(wo["id"], "running")
                store.clear_attention(wo["id"])
        except claude_cli.ClaudeCliError as e:
            log.error("[%s] delivery of message %s failed: %s", project.name, msg["id"], e)
            store.mark_message(msg["id"], "failed")
            store.flag_attention(wo["id"], f"message delivery failed: {e}")
        finally:
            self.in_flight_deliveries.discard(msg["id"])
            store.close()

    # -- 4. reconcile -------------------------------------------------------------------

    def reconcile_project(
        self,
        project: ProjectSpec,
        store: ProjectStore,
        sessions_by_cwd: dict[str, list[claude_cli.BgSession]],
    ) -> None:
        proot = str(project.path)
        sessions = [
            s for cwd, group in sessions_by_cwd.items() if cwd == proot or cwd.startswith(proot + "/")
            for s in group
        ]
        by_session_id = {s.session_id: s for s in sessions if s.session_id}
        by_name_prefix: dict[str, claude_cli.BgSession] = {}
        for s in sessions:
            m = re.match(r"\[WO (wo-[0-9a-f]+)\]", s.name)
            if m:
                by_name_prefix[m.group(1)] = s

        # Settle framework work orders against live session states.
        for wo in store.list_work_orders(statuses=("running", "waiting_input", "dispatching")):
            sid = wo.get("session_id")
            if not sid:
                # --bg dispatch assigns its own session id; bind by unique name if the
                # SessionStart hook hasn't reported yet.
                sess = by_name_prefix.get(wo["id"])
                if sess and sess.session_id:
                    store.update_work_order(wo["id"], session_id=sess.session_id)
                    store.add_event(wo["id"], "session_bound", {"via": "reconciler",
                                                                "session_id": sess.session_id})
                    sid = sess.session_id
                else:
                    age = time.time() - wo["updated_at"]
                    if age > 300:
                        store.set_status(wo["id"], "failed")
                        store.flag_attention(wo["id"], "worker session never appeared")
                    continue
            sess = by_session_id.get(sid)
            if sess is None:
                if wo["status"] == "dispatching":
                    continue  # may not have registered yet
                age = time.time() - wo["updated_at"]
                if age > 120:
                    store.set_status(wo["id"], "failed")
                    store.flag_attention(wo["id"], "worker session disappeared")
                    store.add_notification(
                        title=f"{wo['id']} worker disappeared",
                        body=f"Session {sid} no longer exists.",
                        level="warning", wo_id=wo["id"], source="reconciler",
                    )
                continue
            if sess.state == "running" and wo["status"] != "running":
                store.set_status(wo["id"], "running")
            elif sess.state == "blocked" and wo["status"] == "running":
                store.set_status(wo["id"], "waiting_input")
                store.flag_attention(wo["id"], "worker blocked (permission or input needed)")
            elif sess.state == "done":
                fresh = store.get_work_order(wo["id"])
                if fresh.get("result_summary"):
                    if store.pending_assumptions(wo["id"]):
                        store.set_status(wo["id"], "needs_review")
                        store.flag_attention(wo["id"], "assumptions pending review")
                    else:
                        store.set_status(wo["id"], "completed")
                        store.clear_attention(wo["id"])
                elif not store.queued_messages(wo["id"]):
                    store.set_status(wo["id"], "needs_review")
                    store.flag_attention(
                        wo["id"], "worker idle without `jarvis wo finish` — review the session"
                    )

        # Adopt unknown background sessions as ad-hoc work orders (visibility).
        for sess in sessions:
            if not sess.session_id or sess.name.startswith("[WO "):
                continue
            if store.find_by_session(sess.session_id):
                continue
            if sess.state == "done":
                continue  # only surface live ad-hoc sessions
            wo = store.create_work_order(
                title=sess.name or f"ad-hoc session {sess.id}",
                description="Background session not created through Jarvis "
                            "(adopted by the reconciler for visibility).",
                origin="adhoc",
            )
            store.update_work_order(wo["id"], session_id=sess.session_id)
            store.set_status(wo["id"], "running" if sess.state == "running" else "waiting_input")
            log.info("[%s] adopted ad-hoc session %s as %s", project.name, sess.id, wo["id"])


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
