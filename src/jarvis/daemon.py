"""jarvisd — the deterministic OS daemon.

One process, one poll loop over every project in the catalog. Per tick, in this order:
  1. route project notification outboxes to the central inbox, then to sinks
  2. reap finished worker turns and settle their work orders against what came back
  3. deliver queued user messages as the next turn of their conversation
  4. dispatch pending work orders (respecting per-project concurrency) — last, so it
     sees the concurrency slots steps 2 and 3 just freed
  5. let Neo (the OS answerer agent) drain queued worker questions

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
from .project_store import PRE_APPROVED_KEY, UNGOVERNED_ORIGINS, ProjectStore

log = logging.getLogger("jarvisd")

RECONCILE_EVERY_TICKS = 6  # refresh `claude agents --json` every N ticks (injected only)
#: Ask GitHub about parked pull requests every N ticks — ~2 minutes at the default 5s
#: interval. Its own cadence rather than the reconcile one because it is the only step
#: that leaves the machine: one `gh` subprocess per parked work order per poll. Two
#: minutes is well inside what a user perceives as "it noticed my merge", and a fleet
#: with five PRs parked spends ~150 calls/hour against `gh`'s 5000/hour authenticated
#: limit. Not catalog-configurable on purpose: a knob nobody will tune is a knob that
#: only ever gets set wrong.
PR_POLL_EVERY_TICKS = 24


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
        # Projects already warned that their pull requests cannot be polled (no `gh`, no
        # credentials, an unreachable host). Same idea: say it once, not every 2 minutes
        # forever. Reset by restarting the daemon, which is also what fixes it.
        self.pr_poll_warned: set[str] = set()

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
        poll_prs = self.tick_count % PR_POLL_EVERY_TICKS == 1
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
                self.deliver_messages(project, store)
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
                    store, self.central, project, wo, os_config=self.catalog.os,
                )
            except claude_cli.ClaudeCliError as e:
                log.error("[%s] dispatch of %s failed: %s", project.name, wo["id"], e)

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
          minutes after the user merges, so this costs nobody a step.
        * **`failed` when ANY child is `failed` or `cancelled`.** Deliberately without
          the design's "and the remainder cannot proceed" qualifier: a feature with a
          dead child needs a human whichever siblings could still run, so the
          reachability check buys nothing and is easy to get subtly wrong. A cancelled
          child counts too — it did not settle successfully, so `completed` would be a
          lie — but the reason says cancellation rather than failure, because the two ask
          the user for different things.

        No notification is raised here, on purpose. A failed child has already pinged the
        user through `settle_work_order`, and `notify.route_new_inbox` applies no level
        filter — every inbox row reaches every sink — so a second row would be the same
        event arriving on the phone twice. The feature-level flag is what the user finds
        when they follow the first one.
        """
        from .invariants import FEATURE_CHILD_CANCELLED, FEATURE_CHILD_FAILED

        for fo in store.list_feature_orders(statuses=("executing",)):
            children = store.feature_children(fo["id"])
            if not children:
                continue  # released with nothing in it; nothing to settle against
            dead = [c for c in children if c["status"] in ("failed", "cancelled")]
            if dead:
                first = dead[0]
                template = (FEATURE_CHILD_FAILED if first["status"] == "failed"
                            else FEATURE_CHILD_CANCELLED)
                reason = template.format(id=first["id"])
                store.set_feature_status(fo["id"], "failed")
                store.flag_feature_attention(fo["id"], reason)
                log.info("[%s] feature %s failed: %s", project.name, fo["id"], reason)
            elif all(c["status"] == "completed" for c in children):
                store.set_feature_status(fo["id"], "completed")
                store.clear_feature_attention(fo["id"])
                self._close_feature_backlog(fo)
                log.info("[%s] feature %s completed (%d work orders)", project.name,
                         fo["id"], len(children))

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
                if pstore and q.get("kind") not in ("approval", "plan"):
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
                             reason=verdict["reason"], decided_by="neo")
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

    # -- 6. pull requests parked on a human ------------------------------------------

    def poll_pull_requests(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Ask GitHub what happened to the pull requests this project is parked behind.

        `settle_work_order` can only see as far as the worker's last turn, and a work
        order that ends in a pull request outlives that turn: the merge is the real
        ending and it happens somewhere Jarvis cannot see. This is the one step that
        looks outside the machine, and it exists so the user does not have to type
        `jarvis wo done` after every merge they already performed.

        Three answers, from `github.pr_view`:

        * **merged** — the work landed; the work order ends (`ops.complete_merged`).
        * **closed, unmerged** — someone refused the work; it goes to `needs_review`
          and asks for the user (`ops.record_pr_closed`).
        * **open** — nothing to do, and nothing written. The overwhelmingly common case,
          so it costs exactly one `gh` call and no database write.

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
            except Exception:  # noqa: BLE001
                log.exception("[%s] settling %s against its PR failed", project.name,
                              wo["id"])

    def _warn_pr_poll_broken(self, project: ProjectSpec, store: ProjectStore,
                             error: Exception) -> None:
        """Tell the user once per daemon run that this project's PRs are not polled.

        Silence would be worse than a warning here: the OS would look like it had a
        feature it does not, and the user would keep waiting for merges to register.
        Once is the other half of that — a broken `gh` is broken on every poll, and an
        inbox entry every two minutes is how an inbox stops being read.

        The likely cause is documented in `bugreport.create_issue` and worth repeating
        in the body: a daemon-spawned process often cannot reach `gh`'s keyring
        credentials, so the service environment needs `GH_TOKEN`.
        """
        if project.name in self.pr_poll_warned:
            return
        self.pr_poll_warned.add(project.name)
        log.warning("[%s] pull-request polling unavailable: %s", project.name, error)
        store.add_notification(
            title=f"auto-complete on merge is off for {project.name}",
            body=(f"{error}\n\nWork orders parked behind a pull request will stay on "
                  f"the open list until you close them with `jarvis wo done`. If this "
                  f"is the daemon, `gh`'s keyring credentials may be out of reach — "
                  f"set GH_TOKEN in the service environment."),
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
